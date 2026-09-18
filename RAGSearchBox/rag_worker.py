#------------------------------------------------------------------
# SimpleRAG 워커 수명 관리 (설계서 §6 — 원본 CallMDriveSearch 자리)
#=> 원본 MDriveSearchBox 는 상주 중인 M드라이브 본체에 소켓으로 검색을 요청했다.
#   여기서는 상주 대상이 SimpleRAG 자신이다. `simplerag.exe chat` 을 콘솔 없이 띄워
#   모델을 올려 둔 채로 두고, 질문을 표준입력으로 한 줄씩 넣는다.
#
#   왜 하위 프로세스인가 (설계서 §6 방식 표)
#     SimpleRAG 를 이 프로그램 안에 import 하면 답변을 만드는 동안 GIL 을 잡아
#     감시 스레드와 화면이 멈추고, llama.cpp 가 죽으면 상주 프로그램까지 죽는다.
#     프로세스를 나누면 워커가 죽어도 감시는 살아 있고 다시 띄우면 된다.
#
#   지켜야 하는 것들 (P0 실측으로 확인된 것)
#    - stdin 은 반드시 cp949 : 동결 exe 가 PYTHONIOENCODING 을 무시한다. UTF-8 로 보내면
#      깨진 글자가 토크나이저를 거쳐 워커가 통째로 죽는다(P0-4).
#    - 프롬프트 "질문> " 가 보인 뒤에만 질문을 보낸다 : 출력이 어느 질문의 것인지 섞이지 않게.
#    - 생성 도중에는 멈출 수 없다 : 콘솔이 없어 Ctrl+C 를 보낼 수 없다. 그래서 "취소"는
#      화면에서 버리는 것이고, 정말 멈춰야 할 때(시간 초과)만 프로세스를 죽인다.
#------------------------------------------------------------------

import codecs
import collections
import json
import os
import subprocess
import re
import threading
import time

import log as rsb_log
from chat_parser import PROMPT, ChatParser

# 상태 이름 (트레이 툴팁에도 그대로 쓴다)
STOPPED = "stopped"      # 워커가 없다
STARTING = "starting"    # 모델 적재 중
READY = "ready"          # 질문을 받을 수 있다
BUSY = "busy"            # 답변을 만드는 중
INDEXING = "indexing"    # 인덱싱 명령 하나를 처리하는 중(자동 인덱싱 §8)

# 인덱싱 명령 결과 줄의 머리(simplerag/index/commands.py 와 같아야 한다)
INDEX_PREFIX = "@index "
# 인덱싱 명령 한 건을 기다리는 한도(초) — 큰 문서는 추출에 시간이 걸린다
CMD_TIMEOUT_S = 300

# stdin 인코딩 — 동결 exe 가 환경변수를 무시하므로 코드에 박는다(P0-4)
STDIN_ENCODING = "cp949"
# 워커가 시작하며 내는 줄에서 모델 파일 이름만 집어내는 본 —
# "준비 완료 (9.3초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf (빠름 모드)" 처럼 꼬리표가 붙는다
MODEL_RE = re.compile(r"([^/\\\s]+\.gguf)")
# 재시작 한도를 세는 시간창(초)
RESTART_WINDOW_S = 300
# 인덱스를 다른 프로세스가 잡고 있을 때 SimpleRAG 가 내는 문구
LOCK_HINT = "already accessed"


#------------------------------------------------------------------
# 워커가 우리와 함께 죽도록 묶기 (Job Object)
#=> RAGSearchBox 가 비정상 종료해도 2.5GB 짜리 워커가 혼자 남아 인덱스를 잡고 있으면
#   사용자가 원인을 찾기 어렵다. 작업 개체에 넣어 두면 우리가 죽을 때 같이 정리된다.
#
# -in: 없음
#
# -out: 작업 개체 핸들 또는 None(pywin32 가 없거나 실패)
# -out: error = 없음 (실패해도 워커는 정상 동작한다 — 정리만 못 할 뿐)
#------------------------------------------------------------------
def _create_job():
    try:
        import win32api
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= \
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, info)
        return job
    except Exception:
        return None


#------------------------------------------------------------------
# 프로세스를 작업 개체에 넣기
#
# -in: job = _create_job() 결과(None 이면 아무것도 하지 않는다)
# -in: pid = 넣을 프로세스 ID
#
# -out: True = 넣었음
# -out: error = 없음 (실패는 조용히 무시 — 부가 기능이다)
#------------------------------------------------------------------
def _assign_job(job, pid):
    if not job:
        return False
    try:
        import win32api
        import win32con
        import win32job

        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                 False, pid)
        try:
            win32job.AssignProcessToJobObject(job, h)
            return True
        finally:
            win32api.CloseHandle(h)
    except Exception:
        return False


#------------------------------------------------------------------
# SimpleRAG chat 워커
#=> 프로세스를 띄우고 상태를 관리하며, 출력에서 뽑은 이벤트를 콜백으로 넘긴다.
#   콜백은 감시·읽기 스레드에서 불리므로, 받는 쪽(app)은 큐에 넣기만 해야 한다.
#
# -필드: state    = STOPPED / STARTING / READY / BUSY
# -필드: detail   = 상태를 사람 말로 (트레이 툴팁용)
# -필드: backend  = 워커가 알려 준 생성 백엔드 줄("생성 iGPU(Vulkan) / 리랭킹 켬")
# -필드: model    = 워커가 올린 생성 모델 파일 이름("Qwen3-0.6B-Q4_K_M.gguf")
# -필드: features = 워커가 시작할 때 알린 기능들({"ask-folder", "index-commands"}). 옛 워커면 빈 집합
#------------------------------------------------------------------
class RagWorker:
    #--------------------------------------------------------------
    # 생성자
    #=> 여기서는 프로세스를 띄우지 않는다. start() 로 감시 스레드를 돌린 뒤
    #   ensure_started() 또는 ask() 가 실제로 띄운다.
    #
    # -in: cmd             = 워커 실행 명령 목록(settings.find_worker_cmd 결과)
    # -in: on_event        = 이벤트 콜백 fn(tuple). 다른 스레드에서 불린다
    # -in: no_stream       = True 면 chat 에 --no-stream 을 붙인다
    # -in: start_timeout_s = 준비를 기다리는 한도(초)
    # -in: answer_timeout_s= 답변 한 건을 기다리는 한도(초)
    # -in: restart_max     = 5분 안 자동 재시작 한도
    # -in: idle_unload_min = 이 분 동안 질문이 없으면 모델을 내린다(0=유지)
    # -in: tick_s          = 감시 주기(초)
    # -in: env             = 워커 프로세스에 더할 환경변수 {이름: 값} (예: 관련 근거 문턱)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, cmd, on_event, *, no_stream=False, start_timeout_s=180,
                 answer_timeout_s=60, restart_max=3, idle_unload_min=60, tick_s=0.2, env=None):
        self.cmd = list(cmd)
        self.env = dict(env or {})
        self.on_event = on_event
        self.no_stream = no_stream
        self.start_timeout_s = start_timeout_s
        self.answer_timeout_s = answer_timeout_s
        self.restart_max = restart_max
        self.idle_unload_s = max(0, int(idle_unload_min)) * 60
        self.tick_s = tick_s

        self.log = rsb_log.get("worker")
        self.wlog = rsb_log.worker_log()

        self.state = STOPPED
        self.detail = "시작 전"
        self.backend = ""
        self.model = ""
        self.ready_line = ""
        self.startup_lines = []
        self.features = set()

        self._lock = threading.RLock()
        self._proc = None
        self._parser = ChatParser()
        self._job = _create_job()
        self._stop = threading.Event()
        self._manager = None
        self._readers = []

        self._pending = None          # 대기 중인 질문(최대 1건) — 새 질문이 오면 갈아 끼운다
        self._pending_folder = None   # 그 질문의 검색 범위 폴더(폴더 한정 검색). None = 인덱스 전체
        self._current = None          # 지금 답변을 만드는 중인 질문
        self._t_state = time.monotonic()
        self._t_last_ask = time.monotonic()
        self._restarts = []           # 최근 재시작 시각들
        self._blocked = None          # 자동 재시작을 막는 이유(설정 오류·인덱스 잠금)
        self._stderr_tail = []
        self._want_running = False

        # 자동 인덱싱 명령 (설계서 §8) — 질문이 언제나 먼저다
        self._cmds = collections.deque()   # 보낼 명령들 [(줄, 콜백)]
        self._cmd = None                   # 보내 놓고 결과를 기다리는 명령 (줄, 콜백)
        self._cmd_buf = ""                 # 그 명령의 출력(프롬프트가 올 때까지 모은다)
        self._hold = None                  # 워커를 띄우지 말아야 할 이유(index 명령이 인덱스를 쥔 동안)

    # ── 바깥에서 부르는 것들 ───────────────────────────

    #--------------------------------------------------------------
    # 감시 스레드 시작
    #=> 프로세스 상태를 주기적으로 보고, 대기 질문 전송·시간 초과·재시작·유휴 해제를 맡는다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def start(self):
        if self._manager and self._manager.is_alive():
            return
        self._stop.clear()
        self._manager = threading.Thread(target=self._manage, name="rsb-worker", daemon=True)
        self._manager.start()

    #--------------------------------------------------------------
    # 워커를 올려 두기 (StartMode=boot 에서 시작 때 부른다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실제 실패는 이벤트로 알린다)
    #--------------------------------------------------------------
    def ensure_started(self):
        with self._lock:
            self._want_running = True
            self._blocked = None

    #--------------------------------------------------------------
    # 질문 보내기 (핵심)
    #=> 지금 답변 중이면 대기 칸에 넣는다. 대기 칸에 이미 있으면 새 질문으로 갈아 끼운다
    #   — 사용자가 연달아 물으면 마지막 것만 의미가 있다.
    #
    # -in: question = 이미 걸러진 질문 글자(한 줄)
    # -in: folder   = 이 폴더(하위 포함) 문서만 근거로(폴더 한정 검색). None 이면 인덱스 전체.
    #                 워커가 ask-folder 를 모르면(옛 워커) 폴더 없이 묻는다
    #
    # -out: 없음 (진행 상황은 이벤트로 알린다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def ask(self, question, folder=None):
        with self._lock:
            self._want_running = True
            self._blocked = None
            self._pending = question
            self._pending_folder = folder
            self._t_last_ask = time.monotonic()
            state = self.state
        self.log.info("질문 접수: %s", question[:20])
        if state == STOPPED:
            self._emit(("state", STARTING, "모델 준비 중 (첫 질문)"))

    #--------------------------------------------------------------
    # 인덱싱 명령 보내기 (자동 인덱싱 §8·§9)
    #=> 대기열에 넣기만 한다. 워커가 준비됐고(READY) 기다리는 질문이 없을 때 하나씩 보낸다
    #   — 질문이 언제나 먼저이고, 답변을 만드는 동안에는 보내지 않는다(CPU 를 다툰다).
    #   결과는 callback(dict) 로 온다. 워커가 없거나 도중에 끝나면 {"ok": False, "stopped": True}.
    #   ⚠️ 워커를 띄우지는 않는다 — 쉬어서 내려간 워커를 인덱싱 때문에 다시 올리면
    #      "안 쓰면 메모리를 돌려준다" 가 무너진다. 내려가 있으면 부르는 쪽이 index 명령을 쓴다.
    #
    # -in: line     = "/index-doc {...}" 같은 한 줄
    # -in: callback = 결과를 받을 함수 fn(dict). 워커의 다른 스레드에서 불린다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def command(self, line, callback):
        with self._lock:
            self._cmds.append((line, callback))

    #--------------------------------------------------------------
    # 인덱싱 명령을 받을 수 있는가
    #=> 워커가 떠 있고 모델까지 올라가 있으면 True. 준비 중이면 곧 받을 수 있으므로 True.
    #
    # -in: 없음
    #
    # -out: "up"(떠 있음) | "starting"(곧 뜸) | "down"(내려감)
    # -out: error = 없음
    #--------------------------------------------------------------
    def availability(self):
        with self._lock:
            if self._proc is not None and self.state in (READY, BUSY, INDEXING):
                return "up"
            if self.state == STARTING:
                return "starting"
            return "down"

    #--------------------------------------------------------------
    # 워커 띄우기를 잠시 막기
    #=> index 명령(별도 프로세스)이 인덱스 폴더를 쥔 동안 워커가 뜨면 잠금 충돌로 죽는다.
    #   그동안 질문이 오면 기다렸다가, 풀리면 띄워서 답한다.
    #
    # -in: reason = 막는 이유(None 이면 푼다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def hold(self, reason):
        with self._lock:
            self._hold = reason
        self.log.info("워커 띄우기 %s", "막음: " + reason if reason else "막음 풀림")

    #--------------------------------------------------------------
    # 마지막 질문 뒤 지난 시간(초)
    #=> 키워드 색인을 "한가할 때" 다시 만드는 데 쓴다.
    #
    # -in: 없음
    #
    # -out: 초
    # -out: error = 없음
    #--------------------------------------------------------------
    def seconds_since_ask(self):
        with self._lock:
            return time.monotonic() - self._t_last_ask

    #--------------------------------------------------------------
    # 모델 내리기 (트레이 메뉴·유휴 해제)
    #=> stdin 에 exit 를 보내 정상 종료시킨다. 인덱스 잠금이 풀려 index 명령을 쓸 수 있다.
    #
    # -in: reason = 로그와 트레이에 남길 이유
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def unload(self, reason="사용자 요청"):
        with self._lock:
            self._want_running = False
            self._pending = None
        self.log.info("모델 내리기: %s", reason)
        self._terminate(graceful=True, reason=reason)
        self._set_state(STOPPED, "내려감 ({})".format(reason))

    #--------------------------------------------------------------
    # 프로그램 종료
    #=> 감시 스레드를 멈추고 워커도 정리한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def shutdown(self):
        self._stop.set()
        with self._lock:
            self._want_running = False
        self._terminate(graceful=True, reason="프로그램 종료")
        if self._manager:
            self._manager.join(timeout=3)
        if self._job:
            try:
                import win32api
                win32api.CloseHandle(self._job)
            except Exception:
                pass
            self._job = None

    #--------------------------------------------------------------
    # 지금 상태 요약 (트레이 툴팁)
    #
    # -in: 없음
    #
    # -out: 사람이 읽는 한 줄
    # -out: error = 없음
    #--------------------------------------------------------------
    def status_text(self):
        with self._lock:
            name = {STOPPED: "내려감", STARTING: "준비 중", READY: "준비됨", BUSY: "답변 중",
                    INDEXING: "인덱스 갱신 중"}[self.state]
            extra = self.backend or self.detail
            model = self.model
            if self.state == STARTING:
                extra = "{:.0f}초째".format(time.monotonic() - self._t_state)
        if model and self.state in (READY, BUSY):
            # 파일 이름 그대로는 길다 — "Qwen3-0.6B-Q4_K_M.gguf" → "Qwen3-0.6B"
            short = model.split("-Q4")[0].split(".gguf")[0]
            extra = "{} · {}".format(short, extra) if extra else short
        return "{} · {}".format(name, extra) if extra else name

    # ── 안에서 쓰는 것들 ──────────────────────────────

    #--------------------------------------------------------------
    # 이벤트 전달
    #=> 콜백에서 예외가 나도 워커가 멈추면 안 된다.
    #
    # -in: ev = 이벤트 튜플
    #
    # -out: 없음
    # -out: error = 없음 (콜백 예외는 로그만 남긴다)
    #--------------------------------------------------------------
    def _emit(self, ev):
        try:
            self.on_event(ev)
        except Exception:
            self.log.exception("이벤트 콜백에서 예외")

    #--------------------------------------------------------------
    # 상태 바꾸기
    #
    # -in: state  = 새 상태
    # -in: detail = 사람이 읽는 설명
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _set_state(self, state, detail=""):
        with self._lock:
            if self.state == state and self.detail == detail:
                return
            self.state = state
            self.detail = detail
            self._t_state = time.monotonic()
        self.log.info("상태: %s (%s)", state, detail)
        self._emit(("state", state, detail))

    #--------------------------------------------------------------
    # 워커 프로세스 띄우기
    #=> 콘솔 없이 띄우고(P0-5) 환경변수는 건드리지 않는다(동결 exe 가 무시한다 — P0-4).
    #
    # -in: 없음
    #
    # -out: True = 띄웠다
    # -out: error = 실행 실패는 이벤트 ("error", 사유) 로 알리고 False
    #--------------------------------------------------------------
    def _launch(self):
        cmd = list(self.cmd) + ["chat"]
        if self.no_stream:
            cmd.append("--no-stream")
        cwd = os.path.dirname(os.path.abspath(self.cmd[0]))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self.log.info("워커 실행: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, creationflags=flags,
                env=dict(os.environ, **self.env) if self.env else None)
        except Exception as e:
            self._emit(("error", "SimpleRAG 를 실행하지 못했습니다: {}".format(e)))
            self._set_state(STOPPED, "실행 실패")
            with self._lock:
                self._blocked = "실행 실패"
            return False

        _assign_job(self._job, proc.pid)
        with self._lock:
            self._proc = proc
            self._parser = ChatParser()
            self._stderr_tail = []
            self.backend = ""
            self.model = ""
            self.ready_line = ""
            self.startup_lines = []
            self.features = set()
        self._set_state(STARTING, "모델 적재 중")

        self._readers = [
            threading.Thread(target=self._read_stdout, args=(proc,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(proc,), daemon=True),
        ]
        for t in self._readers:
            t.start()
        return True

    #--------------------------------------------------------------
    # 표준출력 읽기 (별도 스레드)
    #=> 줄 단위가 아니라 도착한 만큼 읽는다. 프롬프트와 답변 토큰은 줄바꿈 없이 온다.
    #   여러 바이트 글자가 조각나도 깨지지 않게 점진 디코더를 쓴다.
    #
    # -in: proc = 대상 프로세스
    #
    # -out: 없음 (이벤트로 알린다)
    # -out: error = 없음 (읽기 실패는 프로세스 종료로 처리된다)
    #--------------------------------------------------------------
    def _read_stdout(self, proc):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                b = proc.stdout.read(4096)
            except Exception:
                break
            if not b:
                break
            text = decoder.decode(b)
            if not text:
                continue
            with self._lock:
                parser = self._parser
                starting = self.state == STARTING
            if starting:
                self.startup_lines.extend(
                    ln for ln in text.splitlines() if ln.strip())
                for ln in text.splitlines():
                    if ln.startswith("생성 "):
                        self.backend = ln.strip()
                    elif ln.startswith("준비 완료"):
                        # 실제 줄: "준비 완료 (9.3초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf (빠름 모드)"
                        # 어느 모델이 올라갔는지는 여기서만 알 수 있다. 설정 파일을 뒤지지 않고도
                        # 알 수 있게 뽑아 둔다(로그·트레이 툴팁에 쓴다).
                        # 뒤에 "(빠름 모드)" 같은 꼬리가 붙으므로 파일 이름만 집는다.
                        m = MODEL_RE.search(ln)
                        self.model = m.group(1) if m else ""
                        self.ready_line = ln.strip()
                    elif ln.startswith("기능:"):
                        # "기능: ask-folder index-commands" — 옛 워커는 이 줄이 없다
                        self.features = set(ln.split(":", 1)[1].split())
            # 인덱싱 명령의 출력은 답변 해석기에 넣지 않는다 — 다음 프롬프트까지 따로 모은다.
            # (넣으면 "@index {...}" 줄이 답변으로 화면에 뜬다.)
            with self._lock:
                in_cmd = self._cmd is not None
            if in_cmd:
                self._cmd_buf += text
                i = self._cmd_buf.find(PROMPT)
                if i < 0:
                    continue
                out, text = self._cmd_buf[:i], self._cmd_buf[i + len(PROMPT):]
                self._cmd_buf = ""
                self._finish_cmd(out)
                if not text:
                    continue
            for ev in parser.feed(text):
                self._on_parsed(ev)

    #--------------------------------------------------------------
    # 해석된 이벤트 처리
    #=> 상태를 옮기고 그대로 바깥에 넘긴다.
    #
    # -in: ev = chat_parser 가 낸 이벤트
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_parsed(self, ev):
        kind = ev[0]
        if kind == "ready":
            # 워커가 시작하며 낸 안내를 그대로 남긴다 — 어느 모델·백엔드로 올라갔는지
            # 나중에 확인할 길이 로그뿐이다
            line = self.ready_line
            if line:
                self.log.info("워커 %s", line)
            if self.backend:
                self.log.info("워커 %s", self.backend)
            # 옛 simplerag.exe 면 폴더 한정 검색·자동 인덱싱 명령을 모른다 — 로그로 바로 알 수 있게
            self.log.info("워커 기능: %s", " ".join(sorted(self.features)) or "(없음 — 옛 simplerag.exe)")
            self._set_state(READY, self.backend or "준비됨")
            return
        if kind == "done":
            with self._lock:
                self._current = None
            self._emit(ev)
            self._set_state(READY, self.backend or "준비됨")
            return
        self._emit(ev)

    #--------------------------------------------------------------
    # 표준오류 읽기 (별도 스레드)
    #=> llama.cpp 로그와 준비 안내가 나온다. 비우지 않으면 파이프가 차서 워커가 멈춘다.
    #   마지막 50줄은 오류를 보여 줄 때 쓰려고 들고 있는다.
    #
    # -in: proc = 대상 프로세스
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _read_stderr(self, proc):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        while True:
            try:
                b = proc.stderr.read(4096)
            except Exception:
                break
            if not b:
                break
            buf += decoder.decode(b)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip()
                if not line:
                    continue
                self.wlog.info("%s", line)
                with self._lock:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-50]
                    # 인덱스를 다른 프로세스가 잡고 있으면 다시 띄워도 소용없다
                    if LOCK_HINT in line:
                        self._blocked = ("SimpleRAG chat/index 가 실행 중입니다 — "
                                         "끝낸 뒤 다시 질문하세요")

    #--------------------------------------------------------------
    # 대기 중인 질문 보내기
    #=> READY 일 때만 부른다. stdin 은 cp949 로 보낸다(P0-4).
    #
    # -in: 없음
    #
    # -out: True = 보냈다
    # -out: error = 파이프가 끊겼으면 False (감시 루프가 재시작을 처리한다)
    #--------------------------------------------------------------
    def _send_pending(self):
        with self._lock:
            q = self._pending
            folder = self._pending_folder
            proc = self._proc
            if not q or not proc:
                return False
            self._pending = None
            self._pending_folder = None
            self._current = q
            self._parser.begin_question()
            use_ask = bool(folder) and "ask-folder" in self.features
        # 폴더 한정이면 질문 한 줄에 폴더를 함께 싣는다(폴더 한정 검색 설계서 §6).
        # ASCII JSON 이라 cp949 로 못 쓰는 글자(이모지 등)가 든 질문·경로도 깨지지 않는다.
        line = ("/ask " + json.dumps({"q": q, "folder": folder}, ensure_ascii=True)) if use_ask else q
        if folder and not use_ask:
            self.log.info("워커가 폴더 한정을 모른다(옛 simplerag.exe) — 인덱스 전체에서 찾는다")
        try:
            proc.stdin.write((line + "\n").encode(STDIN_ENCODING, errors="replace"))
            proc.stdin.flush()
        except Exception as e:
            self.log.warning("질문 전송 실패: %s", e)
            return False
        self._set_state(BUSY, "검색 중")
        self._emit(("sent", q))
        return True

    #--------------------------------------------------------------
    # 대기 중인 인덱싱 명령 하나 보내기
    #=> READY 이고 기다리는 질문이 없을 때만 부른다. 결과는 다음 프롬프트까지 모아
    #   _finish_cmd 가 콜백으로 넘긴다.
    #
    # -in: 없음
    #
    # -out: True = 보냈다
    # -out: error = 파이프가 끊겼으면 콜백에 실패를 넘기고 False
    #--------------------------------------------------------------
    def _send_cmd(self):
        with self._lock:
            if not self._cmds or not self._proc or self._cmd is not None:
                return False
            line, cb = self._cmds.popleft()
            proc = self._proc
            # 출력을 읽는 스레드가 곧바로 알아보도록, 쓰기 전에 표시해 둔다
            self._cmd = (line, cb)
            self._cmd_buf = ""
        try:
            proc.stdin.write((line + "\n").encode(STDIN_ENCODING, errors="replace"))
            proc.stdin.flush()
        except Exception as e:
            self.log.warning("인덱싱 명령 전송 실패: %s", e)
            with self._lock:
                self._cmd = None
            self._call(cb, {"ok": False, "stopped": True, "why": "워커에 보내지 못함"})
            return False
        self._set_state(INDEXING, line.split(" ", 1)[0])
        return True

    #--------------------------------------------------------------
    # 인덱싱 명령 결과 넘기기
    #=> 프롬프트 앞까지의 출력에서 "@index {json}" 줄을 찾는다. 없으면 옛 simplerag.exe 라
    #   명령을 모르는 것이다("알 수 없는 명령입니다") — unknown 으로 알려 자동 인덱싱을 멈추게 한다.
    #
    # -in: out = 명령을 보낸 뒤 프롬프트 전까지의 출력
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _finish_cmd(self, out):
        with self._lock:
            cmd = self._cmd
            self._cmd = None
        if cmd is None:
            return
        res = None
        for ln in out.splitlines():
            if ln.startswith(INDEX_PREFIX):
                try:
                    res = json.loads(ln[len(INDEX_PREFIX):])
                except ValueError:
                    res = None
        if res is None:
            res = {"ok": False, "unknown": True,
                   "why": "워커가 인덱싱 명령을 모릅니다 — simplerag.exe 를 새로 빌드하세요",
                   "raw": out.strip()[:200]}
        self._set_state(READY, self.backend or "준비됨")
        self._call(cmd[1], res)

    #--------------------------------------------------------------
    # 기다리던·대기 중인 인덱싱 명령을 모두 실패로 끝내기
    #=> 워커가 끝나거나 내려갈 때 부른다. 부르는 쪽(자동 인덱싱)은 stopped 를 보고
    #   그 폴더를 다시 볼 목록에 넣는다(워커가 없으면 index 명령으로 처리한다).
    #
    # -in: why = 이유
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _fail_cmds(self, why):
        with self._lock:
            pending = ([self._cmd] if self._cmd else []) + list(self._cmds)
            self._cmd = None
            self._cmd_buf = ""
            self._cmds.clear()
        for _line, cb in pending:
            self._call(cb, {"ok": False, "stopped": True, "why": why})

    #--------------------------------------------------------------
    # 명령 콜백 부르기 (예외가 워커를 멈추지 않게)
    #
    # -in: cb  = 콜백
    # -in: res = 결과 dict
    #
    # -out: 없음
    # -out: error = 없음 (콜백 예외는 로그만)
    #--------------------------------------------------------------
    def _call(self, cb, res):
        try:
            cb(res)
        except Exception:
            self.log.exception("인덱싱 명령 콜백에서 예외")

    #--------------------------------------------------------------
    # 워커 정리
    #=> 정상 종료는 stdin 에 exit 를 넣고 기다린다. 안 끝나면 강제로 죽인다.
    #
    # -in: graceful = True 면 exit 를 먼저 보낸다
    # -in: reason   = 로그에 남길 이유
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _terminate(self, graceful=True, reason=""):
        with self._lock:
            proc = self._proc
            self._proc = None
            self._current = None
        # 워커가 사라지면 기다리던 인덱싱 명령도 끝난다 — 부르는 쪽이 다시 처리하게 알린다
        self._fail_cmds("워커 종료({})".format(reason))
        if not proc:
            return
        try:
            if graceful and proc.poll() is None:
                try:
                    proc.stdin.write(b"exit\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for s in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    s.close()
                except Exception:
                    pass
            self.log.info("워커 종료(%s) 코드=%s", reason, proc.returncode)

    #--------------------------------------------------------------
    # 자동 재시작을 해도 되는가
    #=> 5분에 restart_max 번까지만 다시 띄운다. 계속 죽는 상황에서 무한히 띄우면
    #   그때마다 모델을 올리느라 PC 가 느려진다.
    #
    # -in: now = 현재 시각(초)
    #
    # -out: True = 재시작 가능
    # -out: error = 없음
    #--------------------------------------------------------------
    def _can_restart(self, now):
        self._restarts = [t for t in self._restarts if now - t < RESTART_WINDOW_S]
        return len(self._restarts) < self.restart_max

    #--------------------------------------------------------------
    # 프로세스가 스스로 끝난 경우 처리
    #=> 종료 코드 2(설정 오류)나 인덱스 잠금이면 다시 띄우지 않는다.
    #   답변 도중 죽었으면 그 질문은 다시 보내지 않는다(같은 입력으로 또 죽을 수 있다).
    #
    # -in: code = 종료 코드
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _handle_exit(self, code):
        with self._lock:
            current = self._current
            tail = list(self._stderr_tail)
            blocked = self._blocked
            proc = self._proc
            self._current = None
            self._proc = None
            if current:
                self._pending = None      # 죽게 만든 질문은 다시 보내지 않는다

        self._fail_cmds("워커가 종료됨(code={})".format(code))

        # 스스로 끝난 경우에도 파이프는 우리가 닫아야 한다(안 닫으면 핸들이 샌다)
        if proc is not None:
            for s in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    s.close()
                except Exception:
                    pass

        self.log.warning("워커가 종료됨 code=%s (답변 중이던 질문: %s)", code, current)
        for line in tail[-10:]:
            self.log.warning("  stderr: %s", line)

        if code == 2:
            with self._lock:
                self._blocked = "설정 오류 — config.yaml 을 확인하세요"
            msg = "설정 오류로 SimpleRAG 가 멈췄습니다: {}".format(
                next((l for l in reversed(tail) if "설정 오류" in l), "config.yaml 확인"))
            self._emit(("error", msg))
        elif blocked:
            self._emit(("error", blocked))
        elif current:
            self._emit(("error", "SimpleRAG 가 종료되었습니다 — 다시 올립니다"))
        self._set_state(STOPPED, "종료됨(code={})".format(code))

    #--------------------------------------------------------------
    # 감시 루프 (별도 스레드)
    #=> 주기적으로 상태를 보고 필요한 일을 한다.
    #    1) 띄워야 하는데 없으면 띄운다(재시작 한도·차단 사유 확인)
    #    2) 프로세스가 죽었으면 뒷정리
    #    3) 준비 대기·답변 대기 시간 초과 처리
    #    4) READY 이고 대기 질문이 있으면 보낸다
    #    5) 오래 안 쓰면 모델을 내린다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (예외는 로그만 남기고 루프를 이어 간다)
    #--------------------------------------------------------------
    def _manage(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                self.log.exception("감시 루프에서 예외")
            self._stop.wait(self.tick_s)

    #--------------------------------------------------------------
    # 감시 한 번
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        with self._lock:
            proc = self._proc
            state = self.state
            want = self._want_running
            pending = self._pending
            blocked = self._blocked
            t_state = self._t_state
            t_last = self._t_last_ask
            hold = self._hold
            has_cmd = bool(self._cmds) and self._cmd is None

        # 2) 죽었는지 먼저 본다
        if proc is not None and proc.poll() is not None:
            self._handle_exit(proc.returncode)
            return

        # 1) 필요한데 없으면 띄운다
        if proc is None and want and state == STOPPED:
            if blocked:
                return
            if hold:
                # index 명령이 인덱스를 쥐고 있다 — 끝나면 띄워서 답한다
                if pending:
                    self._set_state(STOPPED, "인덱스 갱신 중 — 끝나면 답합니다")
                return
            if not self._can_restart(now):
                self._set_state(STOPPED, "재시작 한도 초과 — 트레이에서 다시 올리세요")
                with self._lock:
                    self._blocked = "재시작 한도를 넘었습니다"
                self._emit(("error", "SimpleRAG 가 반복해서 종료됩니다 — 로그를 확인하세요"))
                return
            self._restarts.append(now)
            self._launch()
            return

        # 3) 시간 초과
        if state == STARTING and now - t_state > self.start_timeout_s:
            self._emit(("error", "모델을 올리지 못했습니다(시간 초과) — 다시 시도합니다"))
            self._terminate(graceful=False, reason="준비 시간 초과")
            self._set_state(STOPPED, "준비 시간 초과")
            return
        if state == BUSY and now - t_state > self.answer_timeout_s:
            self._emit(("error", "답변이 멈췄습니다 — 모델을 다시 올립니다"))
            self._terminate(graceful=False, reason="답변 시간 초과")
            self._set_state(STOPPED, "답변 시간 초과")
            return

        if state == INDEXING and now - t_state > CMD_TIMEOUT_S:
            # 명령이 멈췄다 — 출력이 어긋났을 수 있어 워커를 다시 띄운다(명령은 실패로 알린다)
            self._emit(("note", "index", "인덱싱 명령이 멈춰 모델을 다시 올립니다"))
            self._terminate(graceful=False, reason="인덱싱 시간 초과")
            self._set_state(STOPPED, "인덱싱 시간 초과")
            return

        # 4) 보낼 질문이 있으면 보낸다 — 질문이 인덱싱보다 먼저다
        if state == READY and pending:
            self._send_pending()
            return
        if state == READY and has_cmd:
            self._send_cmd()
            return

        # 5) 오래 안 쓰면 내린다 (메모리 2.5GB 와 인덱스 잠금을 돌려준다)
        if (state == READY and self.idle_unload_s
                and now - t_last > self.idle_unload_s):
            self.unload("{}분 유휴".format(self.idle_unload_s // 60))
