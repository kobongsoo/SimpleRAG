#------------------------------------------------------------------
# 자동 인덱싱 진행 (자동 인덱싱 설계서 §3·§8 — AI4)
#=> 폴더 감시(watcher)가 "여기를 다시 봐라" 하면, 인덱스를 쥔 쪽에게 일을 시킨다.
#
#   흐름
#    1) 감시 묶음이 오면 그 폴더를 "다시 볼 폴더" 로 적는다(무엇이 바뀌었는지는 믿지 않는다)
#    2) 워커가 떠 있으면
#         /index-plan 폴더  → 추가·수정·삭제 목록(판정 규칙은 simplerag 한곳에만 있다)
#         문서마다 /index-doc · /remove-doc 을 하나씩 — 질문이 오면 질문이 먼저 나간다
#         다 끝나고 질문이 한동안 없으면 /bm25 한 번
#    3) 워커가 내려가 있으면(쉬어서 내려갔거나 아직 안 뜸) 깨우지 않고
#         simplerag index --dir 폴더 --max-add N 을 따로 돌린다. 그동안 워커는 띄우지 않는다
#         (인덱스 폴더를 한 프로세스만 열 수 있다). 질문이 오면 끝난 뒤 답한다.
#    4) 지정 폴더 밖 문서 정리 — 인덱싱 폴더 = 패널 폴더([Scope] Folders 하나)를 지키려고,
#       워커가 뜰 때마다 /index-outside 로 밖 문서를 찾아 인덱스에서 뺀다(폴더 한정 검색 설계서 §0)
#    5) 안전장치 — 사람에게 묻고 기다린다(트레이 "지금 인덱싱")
#         · 새 문서가 AskAboveDocs 건을 넘으면(처음 켠 폴더) 새 문서는 넣지 않는다
#         · 한꺼번에 많이 사라지면(simplerag 의 삭제 멈춤) 지우지 않는다
#
#   이 객체는 메인(tkinter) 스레드에서만 다룬다. 워커 콜백·index 명령 스레드는
#   post(fn) 로 메인 스레드에 일을 넘긴다.
#------------------------------------------------------------------

import collections
import json
import os
import subprocess
import threading
import time

import log as rsb_log

SUMMARY_PREFIX = "@index-summary "
CLI_RETRY_S = 60          # index 명령이 실패했을 때(잠금 등) 다시 해 볼 간격
NOTIFY_AT_LEAST = 3       # 이보다 적게 바뀐 묶음은 트레이로 알리지 않는다(저장할 때마다 뜨면 귀찮다)
OUTSIDE_KEY = "(지정 폴더 밖)"   # waiting_delete 에서 "지정 폴더 밖 문서" 를 가리키는 이름


#------------------------------------------------------------------
# 자동 인덱싱 관리자
#
# -필드: enabled = 켜져 있는가(트레이에서 끄고 켠다)
# -필드: disabled_reason = 스스로 멈춘 이유(옛 워커 등). 있으면 아무것도 하지 않는다
#------------------------------------------------------------------
class IndexJobs:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: settings = Settings (autoindex_* 값)
    # -in: worker   = RagWorker (없으면 None — 그러면 index 명령만 쓴다)
    # -in: roots    = 지켜보는 폴더 목록
    # -in: base_cmd = simplerag 실행 명령 목록(settings.find_worker_cmd 결과)
    # -in: post     = 메인 스레드에서 fn 을 부르게 하는 함수 post(fn)
    # -in: notify   = 알림 함수 notify(kind, msg) — kind: "doc"(문서 하나 반영) | "info" | "warn"
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, settings, worker, roots, base_cmd, post, notify):
        self.s = settings
        self.worker = worker
        self.roots = [os.path.abspath(r) for r in roots]
        self.base_cmd = list(base_cmd or [])
        self.post = post
        self.notify = notify
        self.log = rsb_log.get("autoindex")
        self.enabled = bool(settings.autoindex_enabled)
        self.disabled_reason = None

        self._dirty = collections.OrderedDict()   # {root: 이유} 대조가 필요한 폴더
        self._approved = set()                    # 사람이 "지금 인덱싱" 으로 허락한 폴더
        self._docs = collections.deque()          # [(op, path, root)] 보낼 문서 명령
        self._queued = set()                      # 중복 방지용 (op, 정규화 경로)
        self._inflight = None                     # 보내 놓고 기다리는 일(설명 글)
        self._bm25_needed = False
        self._retry_at = 0.0
        self._last_avail = None                   # 지난번에 본 워커 상태("up" 이 되는 순간을 잡는다)
        self._outside_needed = True               # 지정 폴더 밖 문서를 확인할 차례인가
        self._approve_outside = False             # 밖 문서가 많아 멈췄던 것을 사람이 허락했는가
        self.waiting_new = {}                     # {root: 새 문서 수} 허락 대기
        self.waiting_delete = {}                  # {root: 이유} 삭제 허락 대기
        self._round = self._new_round()

    # ── 바깥에서 부르는 것 ────────────────────────────

    #--------------------------------------------------------------
    # 감시 묶음 받기 (메인 스레드)
    #=> 무엇이 바뀌었는지는 믿지 않고 "이 폴더를 다시 대조" 로만 쓴다. 판정은 /index-plan
    #   이 상태 파일과 대조해 한다 — 알림을 놓치거나 겹쳐도 결과가 같다.
    #
    # -in: batch = watcher.Batch
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def on_batch(self, batch):
        why = batch.reason if batch.rescan else "변경 {}·삭제 {}".format(len(batch.changed),
                                                                        len(batch.removed))
        rsb_log.diag(self.log, "감시 묶음: %s (%s)", batch.root, why)
        self.mark_dirty(batch.root, why)

    #--------------------------------------------------------------
    # 폴더를 "다시 대조" 로 적기
    #
    # -in: root = 폴더
    # -in: why  = 이유(로그용)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def mark_dirty(self, root, why=""):
        root = os.path.abspath(root)
        if root not in self._dirty:
            self._dirty[root] = why

    #--------------------------------------------------------------
    # 트레이 "전체 다시 확인"
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def rescan_all(self):
        for r in self.roots:
            self.mark_dirty(r, "사용자 요청")

    #--------------------------------------------------------------
    # 트레이 "지금 인덱싱" — 허락을 기다리던 폴더를 진행한다
    #=> 새 문서가 많아 미뤘던 폴더, 한꺼번에 많이 사라져 멈췄던 폴더를 이번 한 번 허락한다.
    #   허락은 그 폴더를 한 번 대조하면 사라진다(다음에 또 많으면 또 묻는다).
    #
    # -in: 없음
    #
    # -out: 허락한 폴더 수
    # -out: error = 없음
    #--------------------------------------------------------------
    def run_now(self):
        roots = set(self.waiting_new) | set(self.waiting_delete)
        if OUTSIDE_KEY in roots:
            roots.discard(OUTSIDE_KEY)
            self._approve_outside = True
            self._outside_needed = True
        if not roots and not self._approve_outside:
            roots = set(self.roots)
        for r in roots:
            self._approved.add(r)
            self.mark_dirty(r, "사용자 허락")
        self.log.info("지금 인덱싱: %s", ", ".join(sorted(roots)))
        return len(roots)

    #--------------------------------------------------------------
    # 켜고 끄기 (트레이 "자동 인덱싱")
    #
    # -in: on = True 면 켠다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled:
            self.disabled_reason = None
            self.rescan_all()                      # 꺼져 있던 동안 바뀐 것을 따라잡는다

    #--------------------------------------------------------------
    # 지금 상태 한 줄 (트레이 툴팁·메뉴)
    #
    # -in: 없음
    #
    # -out: 글자 (할 말이 없으면 "")
    # -out: error = 없음
    #--------------------------------------------------------------
    def status_text(self):
        if not self.enabled:
            return "자동 인덱싱 꺼짐"
        if self.disabled_reason:
            return "자동 인덱싱 멈춤 — " + self.disabled_reason
        r = self._round
        if self._inflight or self._docs:
            if r["total"]:
                return "인덱스 갱신 중 {}/{}".format(r["done"], r["total"])
            return "인덱스 확인 중"
        if self.waiting_new:
            return "새 문서 {}건 — 트레이 '지금 인덱싱'".format(sum(self.waiting_new.values()))
        if self.waiting_delete:
            return "사라진 문서가 많아 멈춤 — 트레이 '지금 인덱싱'"
        return ""

    #--------------------------------------------------------------
    # 한 번 돌리기 (메인 스레드, 50ms 마다)
    #=> 한 번에 하나의 일만 보낸다. 결과가 와야 다음 일을 보낸다.
    #
    # -in: now = 지금(monotonic). 비우면 실제 시각
    #
    # -out: 없음
    # -out: error = 없음 (예외는 로그만)
    #--------------------------------------------------------------
    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.enabled or self.disabled_reason or self._inflight:
            return
        try:
            avail = self.worker.availability() if self.worker else "down"
            if avail == "up" and self._last_avail != "up":
                # 워커가 (다시) 떴다 — 내려가 있던 동안 index 명령이 바꿨으면 워커의 키워드 색인이
                # 옛것일 수 있다. 폴더를 한 번씩 대조하면 /index-plan 이 bm25_stale 로 알려 준다.
                for r in self.roots:
                    self.mark_dirty(r, "워커가 떴다")
                self._outside_needed = True
            self._last_avail = avail
            if avail == "starting":
                return                                  # 곧 뜬다 — 뜨면 워커에게 시킨다
            if avail == "up":
                self._tick_worker(now)
            elif (self._dirty or self._docs) and now >= self._retry_at:
                self._run_cli()
            elif self._round_active():
                self._finish_round()
        except Exception:
            self.log.exception("자동 인덱싱 진행에서 예외")

    # ── 워커가 떠 있을 때 ────────────────────────────

    #--------------------------------------------------------------
    # 워커에게 다음 일 하나 시키기
    #=> 문서 명령 → 폴더 대조 → (한가하면) 키워드 색인 순서.
    #
    # -in: now = 지금
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _tick_worker(self, now):
        if self._outside_needed:
            self._outside_needed = False
            self._send("/index-outside " + json.dumps({"roots": self.roots}), self._on_outside,
                       "지정 폴더 밖 문서 확인")
            return
        if self._docs:
            op, path, root = self._docs.popleft()
            self._queued.discard((op, os.path.normcase(path)))
            if op == "remove-outside":
                # 지정 폴더 밖 문서는 파일이 있어도 뺀다 — 워커가 폴더 목록으로 한 번 더 확인한다
                line = "/remove-doc " + json.dumps({"path": path, "outside_of": self.roots})
            else:
                line = "/{} {}".format(op, json.dumps({"path": path}))
            self._send(line,
                       lambda res, op=op, path=path, root=root: self._on_doc(op, path, root, res),
                       "{} {}".format(op, os.path.basename(path)))
            return
        if self._dirty:
            root, why = self._dirty.popitem(last=False)
            arg = {"path": root}
            if root in self._approved:
                arg["allow_delete"] = True
            self.log.info("폴더 대조: %s (%s)", root, why)
            self._send("/index-plan " + json.dumps(arg),
                       lambda res, root=root: self._on_plan(root, res), "대조 " + root)
            return
        if self._bm25_needed:
            idle = self.worker.seconds_since_ask()
            if idle >= self.s.autoindex_bm25_idle_s:
                self._send("/bm25", self._on_bm25, "키워드 색인")
            return
        if self._round_active():
            self._finish_round()

    #--------------------------------------------------------------
    # 워커에게 명령 보내기
    #=> 결과는 워커의 스레드에서 오므로 post 로 메인 스레드에 넘겨 처리한다.
    #
    # -in: line  = 명령 한 줄
    # -in: done  = 결과를 받을 함수 fn(dict) — 메인 스레드에서 불린다
    # -in: label = 진행 중 설명(로그용)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _send(self, line, done, label):
        self._inflight = label

        def back(res):
            self.post(lambda: self._done(done, res))

        self.worker.command(line, back)

    #--------------------------------------------------------------
    # 명령 결과 공통 처리 (메인 스레드)
    #=> 옛 워커(명령을 모름)면 자동 인덱싱을 멈추고 알린다 — 계속 보내 봐야 소용없다.
    #
    # -in: done = 결과 처리 함수
    # -in: res  = 결과 dict
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _done(self, done, res):
        self._inflight = None
        if res.get("unknown"):
            self.disabled_reason = "simplerag.exe 가 인덱싱 명령을 모릅니다(새로 빌드 필요)"
            self.log.warning("%s — 출력: %s", self.disabled_reason, res.get("raw", ""))
            self.notify("warn", self.disabled_reason)
            return
        try:
            done(res)
        except Exception:
            self.log.exception("자동 인덱싱 결과 처리에서 예외")

    #--------------------------------------------------------------
    # 폴더 대조 결과 → 문서 명령으로 펼치기
    #=> 안전장치를 여기서 건다.
    #    · 새 문서가 AskAboveDocs 를 넘고 허락이 없으면 새 문서는 넣지 않는다
    #    · simplerag 가 삭제를 멈췄으면(delete_blocked) 알리고 기다린다
    #
    # -in: root = 폴더
    # -in: res  = /index-plan 결과
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_plan(self, root, res):
        if res.get("stopped"):
            self.mark_dirty(root, "워커가 도중에 내려감")
            return
        if not res.get("ok"):
            # 폴더가 안 보이면(드라이브 분리) 아무것도 지우지 않는다 — 돌아오면 감시가 다시 부른다
            self.log.warning("폴더 대조 실패: %s — %s", root, res.get("why"))
            return
        approved = root in self._approved
        self._approved.discard(root)
        add, modify, remove = res.get("add", []), res.get("modify", []), res.get("remove", [])

        limit = self.s.autoindex_ask_above
        if limit and len(add) > limit and not approved:
            if self.waiting_new.get(root) != len(add):
                self.notify("warn", "새 문서 {}건이 있습니다 — 트레이 메뉴 '지금 인덱싱' 을 누르면 넣습니다"
                            .format(len(add)))
            self.waiting_new[root] = len(add)
            self.log.info("새 문서 %d건 > %d — 허락을 기다린다: %s", len(add), limit, root)
            add = []
        else:
            self.waiting_new.pop(root, None)

        if res.get("delete_blocked"):
            if root not in self.waiting_delete:
                self.notify("warn", "{} — 정말 지운 것이면 트레이 메뉴 '지금 인덱싱'".format(
                    res["delete_blocked"]))
            self.waiting_delete[root] = res["delete_blocked"]
        else:
            self.waiting_delete.pop(root, None)

        for op, paths in (("remove-doc", remove), ("index-doc", modify + add)):
            for p in paths:
                key = (op, os.path.normcase(p))
                if key not in self._queued:
                    self._queued.add(key)
                    self._docs.append((op, p, root))
                    self._round["total"] += 1
        if res.get("bm25_stale"):
            self._bm25_needed = True
        if add or modify or remove:
            self.log.info("대조 결과 %s: 추가 %d · 수정 %d · 삭제 %d", root, len(add), len(modify),
                          len(remove))

    #--------------------------------------------------------------
    # 지정 폴더 밖 문서 확인 결과
    #=> 한꺼번에 많으면(30%·50건) 빼지 않고 묻는다 — 지정 폴더 설정을 잘못 바꾼 것일 수 있다.
    #
    # -in: res = /index-outside 결과
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_outside(self, res):
        if res.get("stopped"):
            self._outside_needed = True             # 워커가 다시 뜨면 다시 본다
            return
        if not res.get("ok"):
            self.log.warning("지정 폴더 밖 문서 확인 실패: %s", res.get("why"))
            return
        out = res.get("outside") or []
        approved, self._approve_outside = self._approve_outside, False
        if not out:
            self.waiting_delete.pop(OUTSIDE_KEY, None)
            return
        if res.get("delete_blocked") and not approved:
            if OUTSIDE_KEY not in self.waiting_delete:
                self.notify("warn", "지정 폴더 밖 문서 {}건이 인덱스에 있습니다({}) — 트레이 '지금 인덱싱' 을 누르면 뺍니다"
                            .format(len(out), res["delete_blocked"]))
            self.waiting_delete[OUTSIDE_KEY] = res["delete_blocked"]
            self.log.info("지정 폴더 밖 문서 %d건 — 허락을 기다린다", len(out))
            return
        self.waiting_delete.pop(OUTSIDE_KEY, None)
        self.log.info("지정 폴더 밖 문서 %d건을 인덱스에서 뺀다(인덱싱 폴더 = 패널 폴더)", len(out))
        for p in out:
            key = ("remove-outside", os.path.normcase(p))
            if key not in self._queued:
                self._queued.add(key)
                self._docs.append(("remove-outside", p, None))
                self._round["total"] += 1

    #--------------------------------------------------------------
    # 문서 명령 결과
    #
    # -in: op   = "index-doc" | "remove-doc"
    # -in: path = 문서 경로
    # -in: root = 폴더
    # -in: res  = 결과 dict
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_doc(self, op, path, root, res):
        if res.get("stopped"):
            # 워커가 내려갔다 — 폴더를 다시 대조하게 한다(내려가 있으면 index 명령이 한다)
            if root is None:
                self._outside_needed = True        # 지정 폴더 밖 정리는 워커가 다시 뜨면
            else:
                self.mark_dirty(root, "워커가 도중에 내려감")
            return
        r = self._round
        r["done"] += 1
        result = res.get("result", "")
        name = os.path.basename(path)
        if res.get("ok") and result in ("added", "modified", "removed"):
            self._bm25_needed = True
            r["changed"].append(name)
            label = {"added": "추가", "modified": "수정", "removed": "삭제"}[result]
            self.log.info("반영 %s: %s (%s청크, %sms)", label, path, res.get("chunks", res.get("removed", "")),
                          res.get("ms"))
            self.notify("doc", "방금 반영({}): {}".format(label, name))
        elif not res.get("ok"):
            r["failed"].append(name)
            self.log.warning("반영 실패 %s: %s — %s", op, path, res.get("why"))

    #--------------------------------------------------------------
    # 키워드 색인 결과
    #
    # -in: res = 결과 dict
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_bm25(self, res):
        if res.get("stopped"):
            return                                  # 워커가 없으면 다음 index 명령이 만든다
        if res.get("ok"):
            self._bm25_needed = False
            self.log.info("키워드 색인 다시 만듦: %s청크 %sms", res.get("chunks"), res.get("ms"))
        else:
            self._bm25_needed = False               # 같은 실패를 되풀이하지 않는다
            self.log.warning("키워드 색인 실패: %s", res.get("why"))

    # ── 워커가 내려가 있을 때 ─────────────────────────

    #--------------------------------------------------------------
    # simplerag index 를 따로 돌리기
    #=> 워커를 깨우지 않는다(쉬어서 메모리를 돌려준 뜻이 없어진다). 그동안 워커는 띄우지 않고,
    #   질문이 오면 끝난 뒤 답한다. 폴더마다 차례로, 별도 스레드에서 돌린다.
    #    - 새 문서 한도는 --max-add, 삭제 허락은 --allow-delete 로 넘긴다
    #    - 결과는 출력의 "@index-summary {json}" 한 줄에서 읽는다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실행 실패는 로그와 재시도)
    #--------------------------------------------------------------
    def _run_cli(self):
        if not self.base_cmd:
            return
        roots = list(self._dirty) + [r for _, _, r in self._docs if r not in self._dirty]
        roots = list(dict.fromkeys(roots))
        self._dirty.clear()
        self._docs.clear()
        self._queued.clear()
        jobs = []
        for r in roots:
            args = ["index", "--dir", r]
            if r in self._approved:
                args.append("--allow-delete")
            elif self.s.autoindex_ask_above:
                args += ["--max-add", str(self.s.autoindex_ask_above)]
            jobs.append((r, r in self._approved, args))
            self._approved.discard(r)

        # ⚠️ 먼저 막고, 그다음에 정말 내려가 있는지 다시 본다 — 그 사이 질문이 와서 워커가
        #    뜨기 시작했다면 index 명령은 인덱스 잠금에 걸린다. 그때는 워커에게 맡긴다.
        if self.worker:
            self.worker.hold("index 명령 실행 중")
            if self.worker.availability() != "down":
                self.worker.hold(None)
                for r, _, _ in jobs:
                    self.mark_dirty(r, "워커가 떠서 워커에게 맡김")
                return
        self._inflight = "index 명령"
        self.log.info("워커가 내려가 있어 index 명령으로 반영한다: %s", ", ".join(roots))
        threading.Thread(target=self._cli_thread, args=(jobs,), name="autoindex-cli",
                         daemon=True).start()

    #--------------------------------------------------------------
    # index 명령 실행 (별도 스레드)
    #
    # -in: jobs = [(root, 허락여부, 인자목록)]
    #
    # -out: 없음 (결과는 post 로 메인 스레드에)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _cli_thread(self, jobs):
        results = []
        wlog = rsb_log.worker_log()
        for root, approved, args in jobs:
            cmd = self.base_cmd + args
            t0 = time.monotonic()
            summary, code, tail = None, None, ""
            try:
                cwd = os.path.dirname(os.path.abspath(self.base_cmd[0]))
                p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, timeout=3600,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                code = p.returncode
                out = p.stdout.decode("utf-8", errors="replace")
                for ln in out.splitlines():
                    if ln.startswith(SUMMARY_PREFIX):
                        try:
                            summary = json.loads(ln[len(SUMMARY_PREFIX):])
                        except ValueError:
                            pass
                    elif ln.strip():
                        wlog.info("[index] %s", ln.rstrip())
                tail = "\n".join(out.strip().splitlines()[-3:])
            except Exception as e:
                tail = "{}: {}".format(type(e).__name__, e)
            results.append((root, code, summary, tail, time.monotonic() - t0))
        self.post(lambda: self._on_cli(results))

    #--------------------------------------------------------------
    # index 명령 결과 (메인 스레드)
    #
    # -in: results = [(root, 종료코드, 요약dict, 끝줄, 걸린초)]
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_cli(self, results):
        self._inflight = None
        if self.worker:
            self.worker.hold(None)
        failed_any = False
        for root, code, summary, tail, sec in results:
            if not summary or not summary.get("ok"):
                failed_any = True
                self.log.warning("index 명령 실패(code=%s, %.1fs): %s — %s", code, sec, root,
                                 (summary or {}).get("why") or tail)
                self.mark_dirty(root, "index 명령 실패 뒤 다시")
                continue
            changed = summary.get("added", 0) + summary.get("modified", 0) + summary.get("removed", 0)
            self.log.info("index 명령 %s: 추가 %s · 수정 %s · 삭제 %s · 실패 %s (%.1fs)", root,
                          summary.get("added"), summary.get("modified"), summary.get("removed"),
                          summary.get("failed"), sec)
            r = self._round
            r["done"] += changed
            r["total"] += changed
            r["changed"] += ["?"] * changed
            r["failed"] += ["?"] * int(summary.get("failed") or 0)
            if summary.get("held_new"):
                if self.waiting_new.get(root) != summary["held_new"]:
                    self.notify("warn", "새 문서 {}건이 있습니다 — 트레이 메뉴 '지금 인덱싱' 을 누르면 넣습니다"
                                .format(summary["held_new"]))
                self.waiting_new[root] = summary["held_new"]
            else:
                self.waiting_new.pop(root, None)
            if summary.get("delete_blocked"):
                if root not in self.waiting_delete:
                    self.notify("warn", "{} — 정말 지운 것이면 트레이 메뉴 '지금 인덱싱'".format(
                        summary["delete_blocked"]))
                self.waiting_delete[root] = summary["delete_blocked"]
            else:
                self.waiting_delete.pop(root, None)
        if failed_any:
            self._retry_at = time.monotonic() + CLI_RETRY_S

    # ── 묶음 마무리 ─────────────────────────────────

    def _new_round(self):
        return {"done": 0, "total": 0, "changed": [], "failed": []}

    def _round_active(self):
        r = self._round
        return bool(r["total"] or r["failed"])

    #--------------------------------------------------------------
    # 한 차례가 끝났을 때 알리기
    #=> 여러 건이 바뀌었거나 실패가 있으면 트레이로 한 번 알린다. 한두 건은 패널
    #   상태 줄(notify "doc")로 충분하다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _finish_round(self):
        r = self._round
        n, failed = len(r["changed"]), len(r["failed"])
        if n >= NOTIFY_AT_LEAST or failed:
            if n:
                msg = "문서 {}건을 인덱스에 반영했습니다".format(n)
                if failed:
                    msg += " · 실패 {}건(로그 참고 — 옛 버전은 그대로 남아 있습니다)".format(failed)
            else:
                msg = "문서 {}건을 인덱스에 반영하지 못했습니다(로그 참고 — 옛 버전은 그대로 남아 있습니다)".format(
                    failed)
            self.notify("info", msg)
        if n or failed:
            self.log.info("자동 인덱싱 한 차례 끝: 반영 %d · 실패 %d", n, failed)
        self._round = self._new_round()
