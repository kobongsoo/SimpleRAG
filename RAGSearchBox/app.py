#------------------------------------------------------------------
# 메인 스레드 조정자 (설계서 §3·§7·§10, 원본 CMainFrame 자리)
#=> 지금까지 만든 조각들을 하나로 엮는다.
#
#   스레드가 넷이다.
#    - 메인 스레드   : tkinter. 창을 그리는 일은 여기서만 한다
#    - 감시 스레드   : monitor.py — 검색창·Enter 감지
#    - 워커 관리 스레드: rag_worker.py — SimpleRAG 프로세스 수명
#    - 트레이 스레드 : tray.py — 아이콘·메뉴
#    - 폴더 감시 스레드: watcher.py — 문서가 바뀌면 자동 인덱싱(자동 인덱싱 설계서 AI4)
#
#   규칙은 하나다: 바깥 스레드는 큐에 넣기만 하고, 화면은 메인 스레드가 만진다.
#   원본 MFC 판의 PostMessage(WM_MDRIVE_SEARCH_BOX_FOCUSED) 자리를 queue.Queue 가 맡는다.
#   그래서 감시·워커·트레이 콜백은 전부 self.q.put(...) 한 줄로 끝난다.
#
#   큐에서 꺼내는 일은 50ms 마다 root.after 로 돈다(§11 응답 시간 예산).
#------------------------------------------------------------------

import os
import queue
import subprocess

import answer_window
import autorun
import chat_panel
import index_jobs
import log as rsb_log
import query_filter
import settings as rsb_settings
import tray as rsb_tray
from monitor import Monitor
from rag_worker import RagWorker
from scope import Scope
from watcher import FolderWatcher

PUMP_MS = 50
TOOLTIP_MS = 2000                 # 트레이 상태 글 갱신 주기
SCOPE_WARN = "범위 폴더 미설정 — RAGSearchBox.ini 의 [Scope] Folders 를 지정하세요"


#------------------------------------------------------------------
# 프로그램 본체
#=> 설정을 읽어 조각들을 만들고, 큐를 돌리며 이어 준다.
#
# -필드: q      = 모든 스레드가 이벤트를 넣는 큐
# -필드: worker = SimpleRAG 워커(실행 파일을 못 찾으면 None)
#------------------------------------------------------------------
class App:
    #--------------------------------------------------------------
    # 생성자
    #=> 여기서는 만들기만 하고 시작하지 않는다. start() 가 스레드를 돌린다.
    #
    # -in: root = tkinter 루트 창(숨겨 둔 것)
    # -in: s    = Settings
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, s):
        self.root = root
        self.s = s
        self.log = rsb_log.get("app")
        self.q = queue.Queue()

        self.scope = Scope(s.scope_folders, s.scope_recheck_min)
        self.window = answer_window.AnswerWindow(root, s)     # 옛 방식(검색창 ?)
        # §18 폴더 패널 — 지정 폴더를 열면 오른쪽에 뜨는 대화창
        self.panel = chat_panel.ChatPanel(root, s, on_ask=self._on_panel_ask)
        self.dedup = query_filter.Dedup(s.dedup_sec)

        self.worker = None
        self.worker_error = None          # 실행 파일을 못 찾았을 때의 안내 문구
        cmd, why = rsb_settings.find_worker_cmd(s)
        if cmd:
            self.log.info("워커 명령: %s (%s)", " ".join(cmd), why)
            self.worker = RagWorker(
                cmd, lambda ev: self.q.put(ev),
                no_stream=s.no_stream, start_timeout_s=s.start_timeout_s,
                answer_timeout_s=s.answer_timeout_s, restart_max=s.restart_max,
                idle_unload_min=s.idle_unload_min)
        else:
            self.worker_error = why
            self.log.error("%s", why)

        # 자동 인덱싱 (plan/자동인덱싱_설계서.html) — 지켜볼 폴더와 simplerag 가 있어야 돈다
        self.jobs = None
        self.watcher = None
        roots = rsb_settings.autoindex_roots(s)
        if roots and cmd:
            self.jobs = index_jobs.IndexJobs(
                s, self.worker, roots, cmd,
                post=lambda fn: self.q.put(("call", fn)),
                notify=lambda kind, msg: self.q.put(("autoindex", kind, msg)))

        self.tray = rsb_tray.Tray(lambda name: self.q.put(("tray", name)),
                                  autorun_flag=autorun.is_enabled,
                                  icon_path=rsb_settings.icon_path(),
                                  flags={"autoindex": lambda: bool(self.jobs and self.jobs.enabled)})
        self.monitor = Monitor(s, self.scope,
                               lambda text, folders, anchor, hwnd:
                                   self.q.put(("query", text, folders, anchor, hwnd)),
                               on_note=lambda kind, msg: self.q.put(("note", kind, msg)),
                               on_folder=lambda hwnd, folder:
                                   self.q.put(("folder", hwnd, folder)))
        # §16 근거 목록 — 지금 답변이 어느 탐색기 창에서 나온 것인지 기억해 둔다
        self._answer_hwnd = None
        self.window.on_show_evidence = self._show_evidence
        self.window.on_restore = self._restore_search
        self.panel.on_show_evidence = self._show_evidence
        self._quitting = False
        self._last_tooltip = ""
        # 예약해 둔 root.after 두 개(큐 처리·상태 갱신). 끝낼 때 취소한다.
        # 목록에 쌓지 않고 마지막 것만 들고 있는다 — 50ms 마다 쌓으면 한 시간에 7만 개가 된다.
        self._job_pump = None
        self._job_tip = None

    #--------------------------------------------------------------
    # 시작
    #=> 트레이 → 워커 → 감시 순서로 켠다. 트레이를 먼저 켜야 경고를 보여 줄 수 있다.
    #   범위 폴더가 하나도 없으면 워커를 올리지 않는다 — 쓸 수 없는 모델에
    #   메모리 2.5GB 를 쓰지 않기 위해서다(설계서 R4).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def start(self):
        self.tray.start()
        self.tray.wait_ready()

        # 설정에 이상한 값이 있었으면 여기서 사용자에게 알린다(프로그램은 기본값으로 돈다)
        for w in self.s.warnings:
            self.log.warning("설정: %s", w)
        if self.s.warnings:
            self.tray.notify("RAGSearchBox 설정 확인", "\n".join(self.s.warnings[:3]))

        if self.worker_error:
            self.tray.notify("RAGSearchBox", self.worker_error)

        if not self.scope.usable():
            self.log.warning("%s", SCOPE_WARN)
            self.tray.notify("RAGSearchBox", SCOPE_WARN)
        elif self.worker and self.s.start_mode == "boot":
            # 로그인 직후 미리 올려 둔다(D7). 첫 질문 때 6.6초를 기다리지 않게 하려는 것이다.
            self.log.info("StartMode=boot — 모델을 미리 올린다")
            self.worker.start()
            self.worker.ensure_started()
        elif self.worker:
            self.worker.start()

        # 자동 시작이 켜져 있는데 프로그램을 옮겼으면 경로를 조용히 고친다(D5)
        fixed = autorun.fix_path_if_moved()
        if fixed:
            self.log.info("자동 시작 경로를 고쳤다: %s", fixed)

        self.monitor.start()
        if self.jobs and self.jobs.enabled:
            self._start_watcher()
        self.root.after(PUMP_MS, self._pump)
        # 첫 갱신을 기다리지 않고 지금 한 번 채운다 — 범위 폴더 미설정 같은 안내가
        # 시작 직후부터 트레이 글에 보여야 한다
        self._update_tooltip(once=True)
        self.root.after(TOOLTIP_MS, self._update_tooltip)
        self.log.info("시작 완료 (범위 폴더 %d개)", len(self.scope.active))

    #--------------------------------------------------------------
    # 큐 비우기 (메인 스레드, 50ms 주기)
    #=> 들어온 이벤트를 종류별로 처리한다. 여기서 예외가 나면 상주가 끊기므로 통째로 감싼다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (처리 중 예외는 로그만 남기고 계속 돈다)
    #--------------------------------------------------------------
    def _pump(self):
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(ev)
            except Exception:
                self.log.exception("이벤트 처리에서 예외: %s", ev[0] if ev else ev)

        # §17 — 붙어 있는 패널을 탐색기 창에 맞춘다. 이 주기(50ms)가 따라오는 속도다.
        # 창을 옮기는 일은 tkinter 스레드에서만 할 수 있어 여기에 얹었다.
        try:
            self.window.follow()
            self.panel.follow()
        except Exception:
            self.log.exception("패널 따라가기에서 예외")

        # 자동 인덱싱 — 한 번에 하나씩, 질문이 먼저다(index_jobs 가 알아서 판단한다)
        if self.jobs:
            self.jobs.tick()

        if not self._quitting:
            self._job_pump = self.root.after(PUMP_MS, self._pump)

    #--------------------------------------------------------------
    # 이벤트 한 건 처리
    #=> 이름은 SimpleRAG pipeline 의 이벤트(evidence/token/done)와 맞춰 두었다.
    #
    # -in: ev = 이벤트 튜플
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _handle(self, ev):
        kind = ev[0]

        if kind == "query":
            self._on_query(ev[1], ev[2], ev[3], ev[4])

        elif kind == "folder":
            self._on_folder(ev[1], ev[2])

        elif kind == "state":
            state = ev[1]
            if state == "starting":
                self._ui().set_status("모델 준비 중 (첫 질문)")
            elif state == "stopped" and len(ev) > 2 and "인덱스" in (ev[2] or ""):
                # index 명령이 인덱스를 쥔 동안 질문이 왔다 — 끝나면 답한다고 알린다
                self._ui().set_status(ev[2])
            elif state == "busy":
                self._ui().set_status("검색 중")
            self._update_tooltip(once=True)

        elif kind == "evidence":
            self._ui().set_evidence(ev[1], ev[2])

        elif kind == "token":
            self._ui().append_token(ev[1])

        elif kind == "done":
            self._ui().finish(ev[1])

        elif kind == "raw":
            self._ui().show_raw(ev[1])

        elif kind == "sent":
            pass                       # 워커에 실제로 보낸 시점 — 로그는 워커가 남긴다

        elif kind == "error":
            self._on_error(ev[1])

        elif kind == "note":
            self._on_note(ev[1], ev[2])

        elif kind == "tray":
            self._on_tray(ev[1])

        elif kind == "watch":
            if self.jobs:
                self.jobs.on_batch(ev[1])

        elif kind == "call":
            ev[1]()                    # 다른 스레드가 메인 스레드에서 해 달라고 넘긴 일

        elif kind == "autoindex":
            self._on_autoindex(ev[1], ev[2])

    #--------------------------------------------------------------
    # 질문 처리 (T4 거르기 → 창 열기 → 워커에 전달)
    #=> 감시 스레드는 "? 로 시작하고 범위 안"까지만 보고 넘긴다.
    #   길이·중복·명령 회피 같은 나머지 거르기를 여기서 한다.
    #
    # -in: text    = 검색창에서 읽은 원문
    # -in: folders = 판정된 폴더 목록(로그용)
    # -in: anchor  = 검색창 화면 위치(창을 그 아래에 놓는다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_query(self, text, folders, anchor, hwnd=None):
        question, why = query_filter.to_question(text, self.s.prefix, self.s.min_chars)
        if question is None:
            self.log.info("무시: %s (%r)", why, text[:20])
            return
        if self.dedup.is_duplicate(question):
            self.log.info("무시: %d초 안 같은 질문 (%r)", self.s.dedup_sec, question[:20])
            return

        self.log.info("질문: %r (폴더 %s)", question, folders[0] if folders else "-")
        self._answer_hwnd = hwnd          # 근거 목록을 띄울 대상 창(§16)
        self.window.show(question, anchor=anchor, status="검색 중", target=hwnd)

        if self.worker is None:
            self.window.show_error(self.worker_error or "SimpleRAG 를 찾지 못했습니다")
            return
        self.worker.ask(question)

    #--------------------------------------------------------------
    # 워커 오류 보여 주기
    #=> 창이 떠 있으면 창에, 아니면 트레이 풍선으로 알린다(설계서 §10).
    #
    # -in: msg = 사용자에게 보여 줄 한 줄
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_error(self, msg):
        self.log.warning("오류: %s", msg)
        if self.panel.visible:
            self.panel.show_error(msg)
        elif self.window.visible:
            self.window.show_error(msg)
        else:
            self.tray.notify("RAGSearchBox", msg)

    #--------------------------------------------------------------
    # 감시 스레드가 보낸 알림
    #=> UIA 실패(감시 불가)처럼 사용자가 알아야 하는 것만 온다.
    #
    # -in: kind = "error" | "warn"
    # -in: msg  = 내용
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_note(self, kind, msg):
        # §16 근거 목록 결과는 트레이 풍선까지 띄울 일이 아니다. 창에 한 줄로 알린다.
        if kind == "evidence":
            self.log.info("근거 목록 표시: %s", msg[:60])
            self._ui().notice("근거 파일을 탐색기에 띄웠습니다")
            return
        if kind == "evidence_fail":
            self.log.info("근거 목록 실패: %s", msg)
            self._ui().notice(msg)
            return
        self.log.warning("감시 알림(%s): %s", kind, msg)
        self.tray.notify("RAGSearchBox", msg)

    #--------------------------------------------------------------
    # 지금 답을 보여 줄 화면 고르기
    #=> 폴더 패널이 떠 있으면 거기로, 아니면 옛 답변 창으로 보낸다.
    #   두 화면은 같은 이름의 메서드를 갖는다
    #   (set_status·set_evidence·append_token·finish·show_raw·show_error·notice).
    #
    # -in: 없음
    #
    # -out: ChatPanel 또는 AnswerWindow
    # -out: error = 없음
    #--------------------------------------------------------------
    def _ui(self):
        return self.panel if self.panel.visible else self.window

    #--------------------------------------------------------------
    # 탐색기가 보고 있는 폴더가 바뀌었다 (§18)
    #=> 범위 안이면 그 창 옆에 패널을 띄우고, 범위 밖으로 나가면 내린다.
    #   패널이 붙은 창은 앞에 없어도 계속 지켜보라고 감시 스레드에 알린다.
    #
    # -in: hwnd   = 탐색기 창
    # -in: folder = 지금 폴더(범위 밖이거나 알 수 없으면 None)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_folder(self, hwnd, folder):
        if not self.s.panel_enabled:
            return
        if folder:
            self.panel.show_for(hwnd, folder)
            if self.panel.visible and self.panel.target_hwnd == hwnd:
                self.monitor.watch_window(hwnd)
                self._answer_hwnd = hwnd
                # 곧 물어볼 참이다 — 미리 올려 두면 첫 질문이 빨라진다(D7)
                if self.worker and self.s.start_mode != "lazy":
                    self.worker.ensure_started()
        else:
            # 닫기를 눌렀던 창이라도 범위 밖에 한 번 나갔다 오면 다시 띄운다
            self.panel.forget_closed(hwnd)
            if self.panel.visible and self.panel.target_hwnd == hwnd:
                self.panel.hide()
                self.monitor.watch_window(None)

    #--------------------------------------------------------------
    # 패널 입력 칸에서 질문을 보냈다 (§18)
    #=> 검색창에서 온 질문과 같은 길로 흘려보낸다. 다만 ? 접두어는 필요 없다.
    #
    # -in: text = 사용자가 쓴 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_panel_ask(self, text):
        question = query_filter.one_line(text)
        if not question:
            return
        self.log.info("패널 질문: %r (폴더 %s)", question[:40], self.panel.folder)
        if self.worker is None:
            self.panel.show_error(self.worker_error or "SimpleRAG 를 찾지 못했습니다")
            return
        self.worker.ask(question)

    #--------------------------------------------------------------
    # "근거 파일 보기" (§16)
    #=> 답변에 나온 근거 문서 이름들을, 질문을 친 그 탭의 검색 결과로 띄운다.
    #   UIA 조작은 감시 스레드 몫이라 여기서는 부탁만 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (대상 창을 모르면 창에 한 줄로 알린다)
    #--------------------------------------------------------------
    def _show_evidence(self):
        if self.panel.visible:
            turn = self.panel.turns[-1] if self.panel.turns else None
            docs = [d for d in (turn.docs if turn else []) if d]
        else:
            docs = [d for d in self.window._docs if d]
        if not self._answer_hwnd or not docs:
            self.window.notice("근거 파일을 알 수 없습니다")
            return
        self.monitor.show_evidence(self._answer_hwnd, docs)

    #--------------------------------------------------------------
    # "원래대로" (§16)
    #=> 질문을 쳤을 때의 검색어로 탐색기를 되돌린다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _restore_search(self):
        if not self._answer_hwnd:
            self.window.notice("되돌릴 창을 알 수 없습니다")
            return
        self.monitor.restore_search(self._answer_hwnd)

    #--------------------------------------------------------------
    # 트레이 메뉴 처리
    #
    # -in: name = "open" | "unload" | "reload" | "autorun" | "logs" | "exit"
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_tray(self, name):
        self.log.info("트레이: %s", name)

        if name == "open":
            self._open_panel()

        elif name in ("index_now", "rescan", "autoindex"):
            self._on_tray_autoindex(name)

        elif name == "unload":
            if self.worker:
                self.worker.unload("트레이 메뉴")
                self.tray.notify("RAGSearchBox", "모델을 내렸습니다 — 이제 index 를 실행할 수 있습니다")

        elif name == "reload":
            if self.worker:
                self.worker.start()
                self.worker.ensure_started()

        elif name == "autorun":
            if autorun.is_enabled():
                ok, msg = autorun.disable()
            else:
                ok, msg = autorun.enable()
                msg = "로그인할 때 자동으로 시작합니다" if ok else msg
            self.tray.notify("RAGSearchBox", msg)

        elif name == "logs":
            self._open_logs()

        elif name == "exit":
            self.quit()

    #--------------------------------------------------------------
    # 트레이 "창 열기" (§18)
    #=> 닫기를 눌러 숨긴 패널을 다시 불러낸다.
    #    1) 이미 떠 있으면 앞으로 가져와 입력 칸에 커서를 둔다
    #    2) 범위 폴더를 보고 있는 탐색기 창이 있으면 가장 최근 창 옆에 띄운다
    #       (닫기를 눌렀던 창이어도 사용자가 직접 부른 것이므로 띄운다)
    #    3) 그런 창이 없으면 첫 번째 범위 폴더를 탐색기로 연다 — 열리면 폴더 감시가
    #       그 창을 알아보고 패널을 붙인다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (탐색기를 못 열면 트레이 알림)
    #--------------------------------------------------------------
    def _open_panel(self):
        if self.panel.visible:
            self.panel.activate()
            return
        wins = self.monitor.in_scope_windows()
        if wins:
            hwnd, folder = wins[0]
            self.panel.forget_closed(hwnd)
            self._on_folder(hwnd, folder)
            self.panel.activate()
            return
        if not self.scope.active:
            self.tray.notify("RAGSearchBox", SCOPE_WARN)
            return
        target = self.scope.active[0]
        self.log.info("창 열기: 범위 폴더를 탐색기로 연다 — %s", target)
        try:
            subprocess.Popen(["explorer.exe", target])
        except Exception as e:
            self.log.warning("탐색기를 열지 못했다: %s", e)
            self.tray.notify("RAGSearchBox", "탐색기를 열지 못했습니다: {}".format(target))

    #--------------------------------------------------------------
    # 폴더 감시 시작 (자동 인덱싱)
    #=> 감시 스레드는 묶음을 큐에 넣기만 한다. 판단은 메인 스레드의 IndexJobs 가 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (시작 실패는 로그만 — 순찰·수동 index 로도 쓸 수 있다)
    #--------------------------------------------------------------
    def _start_watcher(self):
        if self.watcher or not self.jobs:
            return
        try:
            self.watcher = FolderWatcher(self.jobs.roots, lambda b: self.q.put(("watch", b)),
                                         quiet_s=self.s.autoindex_quiet_s,
                                         rescan_min=self.s.autoindex_rescan_min)
            self.watcher.start()
        except Exception:
            self.log.exception("폴더 감시를 시작하지 못했다")
            self.watcher = None

    #--------------------------------------------------------------
    # 폴더 감시 멈춤
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _stop_watcher(self):
        if self.watcher:
            try:
                self.watcher.stop()
            except Exception:
                self.log.exception("폴더 감시를 멈추다 예외")
            self.watcher = None

    #--------------------------------------------------------------
    # 자동 인덱싱 알림 받기 (메인 스레드)
    #=> 문서 한 건 반영은 패널 상태 줄에만 잠깐(저장할 때마다 트레이 알림이 뜨면 귀찮다),
    #   여러 건 반영·허락이 필요한 일·멈춤은 트레이로 알린다.
    #
    # -in: kind = "doc" | "info" | "warn"
    # -in: msg  = 알릴 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_autoindex(self, kind, msg):
        if kind == "doc":
            if self.panel.visible:
                self.panel.notice(msg)
            return
        self.log.info("자동 인덱싱 알림(%s): %s", kind, msg)
        self.tray.notify("RAGSearchBox", msg)
        self._update_tooltip(once=True)

    #--------------------------------------------------------------
    # 트레이의 자동 인덱싱 메뉴
    #    - 지금 인덱싱  : 허락을 기다리던 폴더(새 문서 많음·대량 삭제)를 진행한다
    #    - 전체 다시 확인: 모든 폴더를 곧바로 대조한다
    #    - 자동 인덱싱  : 켜고 끈다(설정 파일에도 적는다)
    #
    # -in: name = 메뉴 이름
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_tray_autoindex(self, name):
        if not self.jobs:
            self.tray.notify("RAGSearchBox", "자동 인덱싱할 폴더가 없습니다 — [Scope] 또는 [AutoIndex] Folders 를 지정하세요")
            return
        if name == "index_now":
            n = self.jobs.run_now()
            self.tray.notify("RAGSearchBox", "폴더 {}개를 인덱싱합니다".format(n))
        elif name == "rescan":
            self.jobs.rescan_all()
            self.tray.notify("RAGSearchBox", "폴더를 다시 확인합니다")
        elif name == "autoindex":
            on = not self.jobs.enabled
            self.jobs.set_enabled(on)
            if on:
                self._start_watcher()
            else:
                self._stop_watcher()
            ok, detail = rsb_settings.save_ini_value(self.s.ini_path, "AutoIndex", "Enabled",
                                                     1 if on else 0)
            if not ok:
                self.log.warning("자동 인덱싱 설정을 저장하지 못했다: %s", detail)
            self.tray.notify("RAGSearchBox", "자동 인덱싱을 {}".format("켰습니다" if on else "껐습니다"))
        self._update_tooltip(once=True)

    #--------------------------------------------------------------
    # 로그 폴더 열기
    #=> 탐색기로 연다. 상주 프로그램이라 사용자가 로그를 찾아갈 길이 여기뿐이다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (열기 실패는 로그만)
    #--------------------------------------------------------------
    def _open_logs(self):
        path = rsb_log.log_dir()
        try:
            os.makedirs(path, exist_ok=True)
            subprocess.Popen(["explorer.exe", path])
        except Exception:
            self.log.exception("로그 폴더를 열지 못했다: %s", path)

    #--------------------------------------------------------------
    # 트레이 상태 글 갱신
    #=> 워커 상태와 범위 폴더 상태를 한 줄로 보여 준다.
    #   범위 폴더가 늦게 연결되는 경우(네트워크 드라이브)도 여기서 알아챈다.
    #
    # -in: once = True 면 다음 주기를 예약하지 않는다(상태 이벤트 때 즉시 갱신용)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _update_tooltip(self, once=False):
        try:
            if self.scope.refresh() and self.scope.usable():
                self.log.info("범위 폴더가 연결되었다: %s", self.scope.active)

            if not self.scope.usable():
                text = "RAGSearchBox — " + SCOPE_WARN
            elif self.worker is None:
                text = "RAGSearchBox — " + (self.worker_error or "SimpleRAG 없음")
            else:
                text = "RAGSearchBox — " + self.worker.status_text()
            # 자동 인덱싱이 할 말이 있으면 덧붙인다(갱신 중 2/5 · 허락 대기 등)
            if self.jobs:
                extra = self.jobs.status_text()
                if extra:
                    text += " · " + extra

            if text != self._last_tooltip:
                self._last_tooltip = text
                self.tray.set_tooltip(text)
        except Exception:
            self.log.exception("트레이 상태 갱신에서 예외")
        if not once and not self._quitting:
            self._job_tip = self.root.after(TOOLTIP_MS, self._update_tooltip)

    #--------------------------------------------------------------
    # 끝내기
    #=> 감시 → 워커 → 트레이 → 창 순서로 정리한다. 워커를 먼저 내려야
    #   인덱스 잠금이 풀린 상태로 끝난다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (정리 중 예외는 로그만 남기고 계속 진행한다)
    #--------------------------------------------------------------
    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.log.info("종료를 시작한다")
        # 예약해 둔 50ms 큐 처리·상태 갱신을 먼저 취소한다.
        # 창이 없어진 뒤에 돌면 Tk 가 "invalid command name" 을 찍는다.
        for job in (self._job_pump, self._job_tip):
            if job is None:
                continue
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._job_pump = self._job_tip = None
        for step, fn in (("폴더 감시", self._stop_watcher),
                         ("감시", self.monitor.stop),
                         ("워커", (self.worker.shutdown if self.worker else lambda: None)),
                         ("트레이", self.tray.stop),
                         ("창", self.window.close),
                         ("패널", self.panel.close)):
            try:
                fn()
            except Exception:
                self.log.exception("%s 정리에서 예외", step)
        try:
            self.root.quit()
        except Exception:
            pass
        self.log.info("종료 완료")
