#------------------------------------------------------------------
# 폴더 변경 감지 (자동 인덱싱 설계서 §4 — AI3)
#=> 지정 폴더의 문서가 추가·수정·삭제·이름 변경되면 알아채 "다 써진 뒤에" 알려 준다.
#   무엇을 인덱스에 넣고 뺄지는 여기서 정하지 않는다 — 워커의 /index-plan 이
#   상태 파일과 대조해 정한다(규칙이 한곳에만 있게). 여기는 "어디를 다시 볼지" 만 전한다.
#
#   두 가지를 함께 쓴다
#    - 폴더 알림(ReadDirectoryChangesW): 빠르다. 폴더마다 스레드 하나가 기다린다.
#        · 파일 알림 핸들: 이름·크기·수정 시각이 바뀐 파일 → 그 경로를 "다시 볼 것" 에 넣는다
#        · 폴더 알림 핸들: 하위 폴더가 생기거나 지워지거나 이름이 바뀜 → 폴더 전체 대조
#          (폴더째 복사해 넣으면 안의 파일마다 알림이 오지 않는다 — 폴더 하나만 온다)
#        · 알림이 너무 많아 버퍼가 넘치면 Windows 가 "놓쳤다(0바이트)" 로 알린다 → 전체 대조
#    - 대조 순찰: 시작할 때, rescan_min 분마다, 폴더가 끊겼다 돌아왔을 때 → 전체 대조.
#      알림을 놓쳐도(네트워크 드라이브·꺼져 있던 동안) 결국 따라잡는다.
#
#   안정화 — 다 써진 파일만 알린다
#    - 마지막 알림 뒤 quiet_s 초가 조용해야 본다(Word 저장 한 번에 알림이 여러 개 온다)
#    - 크기·수정 시각이 두 번 연속 같아야 한다(복사 중이면 계속 바뀐다)
#    - 읽기로 열려야 한다. 공유 위반이면 기다렸다 다시(5s→15s→60s), 그래도 안 되면
#      버리고 다음 대조 순찰에 맡긴다
#
#   RAGSearchBox 는 simplerag 를 import 하지 않는다(경계는 프로세스뿐). 그래서 인덱싱 대상
#   확장자를 여기에 한 벌 둔다 — simplerag 의 TEXT_EXTS 와 같은지는 시험이 확인한다.
#------------------------------------------------------------------

import os
import threading
import time

import log as rsb_log

# simplerag/index/indexer.py 의 TEXT_EXTS 와 같아야 한다(tests/test_watcher.py 가 확인)
TEXT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".hwp", ".hwpx", ".txt", ".md", ".html", ".htm", ".py", ".ipynb",
}

# ReadDirectoryChangesW 의 알림 종류
ACTION_ADDED, ACTION_REMOVED, ACTION_MODIFIED, ACTION_RENAMED_OLD, ACTION_RENAMED_NEW = 1, 2, 3, 4, 5

BUF_SIZE = 64 * 1024          # 네트워크 드라이브는 64KB 가 한도다
OPEN_RETRY_S = (5, 15, 60)    # 읽기로 못 열 때 다시 볼 간격 — 다 쓰면 버린다
OFFLINE_RETRY_S = 30          # 폴더가 안 보일 때 다시 열어 볼 간격


#------------------------------------------------------------------
# 인덱싱 대상 문서 이름인가
#=> 확장자가 대상이고, 저장할 때 잠깐 생기는 표시 파일이 아니어야 한다.
#   (Office 는 문서를 열면 "~$이름.docx" 를 만든다 — 확장자가 같아 이름으로 거른다.)
#
# -in: path = 경로(없어진 파일이어도 된다 — 이름만 본다)
#
# -out: True = 볼 만한 문서
# -out: error = 없음
#------------------------------------------------------------------
def is_doc_name(path):
    name = os.path.basename(path)
    if name.startswith("~$") or name.startswith(".~lock."):
        return False
    return os.path.splitext(name)[1].lower() in TEXT_EXTS


#------------------------------------------------------------------
# 숨김·시스템 파일인가 (있는 파일만)
#
# -in: path = 경로
#
# -out: True = 숨김 또는 시스템 속성
# -out: error = 없음 (속성을 못 읽으면 False)
#------------------------------------------------------------------
def is_hidden(path):
    try:
        return bool(getattr(os.stat(path), "st_file_attributes", 0) & 0x6)
    except OSError:
        return False


#------------------------------------------------------------------
# 파일 상태 재기 (크기, 수정 시각)
#
# -in: path = 경로
#
# -out: (size, mtime_ns) 또는 None(없음)
# -out: error = 없음
#------------------------------------------------------------------
def file_stat(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


#------------------------------------------------------------------
# 읽기로 열리는가
#=> 복사 중이거나 저장 중인 파일은 공유 위반으로 못 연다. 연다고 내용을 읽지는 않는다.
#
# -in: path = 경로
#
# -out: True = 열렸다
# -out: error = 없음
#------------------------------------------------------------------
def can_open(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


#------------------------------------------------------------------
# 다시 볼 파일 하나의 상태
#
# -필드: path     = 경로(알림에 온 표기 그대로)
# -필드: last     = 마지막 알림 시각(monotonic)
# -필드: stat     = 지난번에 잰 (size, mtime) — 두 번 연속 같아야 "다 써졌다"
# -필드: tries    = 읽기로 못 연 횟수
# -필드: next_at  = 다음에 볼 시각
#------------------------------------------------------------------
class Pending:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: path = 경로 / now = 알림 시각
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, path, now):
        self.path = path
        self.last = now
        self.stat = None
        self.tries = 0
        self.next_at = now


#------------------------------------------------------------------
# 파일 하나가 "다 써졌나" 판정 (순수 함수 — 시험하기 쉽게 따로 뗐다)
#=> 1) 마지막 알림 뒤 quiet_s 가 안 지났으면 기다린다
#   2) 파일이 없으면 "사라짐"
#   3) 크기·시각이 지난번과 다르면 적어 두고 stable_s 뒤에 다시 본다
#   4) 같으면 읽기로 열어 본다 — 열리면 "바뀜", 안 열리면 간격을 늘려 다시.
#      OPEN_RETRY_S 를 다 쓰면 "버림"(다음 대조 순찰이 주워 간다)
#
# -in: p        = Pending (stat·tries·next_at 이 바뀐다)
# -in: now      = 지금(monotonic)
# -in: quiet_s  = 조용해야 할 시간
# -in: stable_s = 크기·시각을 다시 잴 간격
# -in: stat_fn  = file_stat 대신 쓸 함수(시험용)
# -in: open_fn  = can_open 대신 쓸 함수(시험용)
#
# -out: "wait" | "changed" | "removed" | "drop"
# -out: error = 없음
#------------------------------------------------------------------
def settle(p, now, quiet_s, stable_s=1.0, stat_fn=file_stat, open_fn=can_open):
    if now < p.next_at or now - p.last < quiet_s:
        return "wait"
    st = stat_fn(p.path)
    if st is None:
        return "removed"
    if st != p.stat:
        # 아직 쓰는 중일 수 있다 — 한 번 더 재서 같으면 다 써진 것으로 본다
        p.stat = st
        p.next_at = now + stable_s
        return "wait"
    if open_fn(p.path):
        return "changed"
    if p.tries >= len(OPEN_RETRY_S):
        return "drop"
    p.next_at = now + OPEN_RETRY_S[p.tries]
    p.tries += 1
    p.stat = None           # 기다린 사이 또 바뀔 수 있으니 다시 두 번 잰다
    return "wait"


#------------------------------------------------------------------
# 알림 한 묶음 — 폴더 하나에서 다 써진 변화들
#
# -필드: root    = 지정 폴더
# -필드: changed = 추가·수정된 문서 경로들(지금 있는 것)
# -필드: removed = 사라진 문서 경로들(이름이 바뀐 옛 이름 포함)
# -필드: rescan  = True 면 이 폴더 전체를 대조해야 한다(폴더 변화·알림 넘침·순찰·시작)
# -필드: reason  = 전체 대조 이유(로그용)
#------------------------------------------------------------------
class Batch:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: root / changed / removed / rescan / reason = 위 필드 설명
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, changed=None, removed=None, rescan=False, reason=""):
        self.root = root
        self.changed = list(changed or [])
        self.removed = list(removed or [])
        self.rescan = rescan
        self.reason = reason

    def __repr__(self):
        return "Batch({!r}, changed={}, removed={}, rescan={}, reason={!r})".format(
            self.root, len(self.changed), len(self.removed), self.rescan, self.reason)


#------------------------------------------------------------------
# 폴더 감시기
#=> 지정 폴더마다 알림 스레드 하나 + 전체를 돌보는 판정 스레드 하나.
#   알림 스레드는 "이 경로를 다시 볼 것" 만 적고, 판정 스레드가 0.5초마다
#   다 써진 것을 모아 on_batch 로 넘긴다. on_batch 는 판정 스레드에서 불린다 —
#   받는 쪽(app)은 큐에 넣고 바로 돌아와야 한다.
#
# -필드: roots = 지정 폴더 목록
#------------------------------------------------------------------
class FolderWatcher:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: roots      = 지정 폴더 목록
    # -in: on_batch   = 묶음을 받을 함수 fn(Batch)
    # -in: quiet_s    = 마지막 알림 뒤 이만큼 조용해야 본다(기본 5초)
    # -in: rescan_min = 대조 순찰 주기(분). 0 이면 시작할 때만
    # -in: stable_s   = 크기·시각을 다시 잴 간격(기본 1초)
    # -in: tick_s     = 판정 주기(기본 0.5초)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, roots, on_batch, quiet_s=5.0, rescan_min=10, stable_s=1.0, tick_s=0.5):
        self.roots = [os.path.abspath(r) for r in roots]
        self.on_batch = on_batch
        self.quiet_s = float(quiet_s)
        self.rescan_s = max(0, int(rescan_min)) * 60
        self.stable_s = float(stable_s)
        self.tick_s = float(tick_s)
        self.log = rsb_log.get("watcher")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pending = {r: {} for r in self.roots}        # {root: {norm경로: Pending}}
        self._rescan_at = {r: None for r in self.roots}    # 전체 대조를 부탁받은 시각
        self._rescan_why = {r: "" for r in self.roots}
        self._last_rescan = {r: 0.0 for r in self.roots}
        self._online = {r: None for r in self.roots}       # None=아직 모름
        self._threads = []
        self._stop_events = []

    # ── 시작·끝 ──────────────────────────────────────

    #--------------------------------------------------------------
    # 감시 시작
    #=> 시작할 때 한 번 전체 대조를 부탁한다 — 꺼져 있던 동안 바뀐 것을 따라잡으려고.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (pywin32 가 없으면 알림 없이 순찰만 한다 — 로그로 알린다)
    #--------------------------------------------------------------
    def start(self):
        for r in self.roots:
            self.request_rescan(r, "시작", immediate=True)
        try:
            import win32event
            for r in self.roots:
                ev = win32event.CreateEvent(None, True, False, None)
                self._stop_events.append(ev)
                t = threading.Thread(target=self._watch_root, args=(r, ev),
                                     name="watch:" + os.path.basename(r), daemon=True)
                t.start()
                self._threads.append(t)
        except ImportError:
            self.log.warning("pywin32 가 없어 폴더 알림 없이 %d분마다 순찰만 한다", self.rescan_s // 60)
        t = threading.Thread(target=self._tick_loop, name="watch:tick", daemon=True)
        t.start()
        self._threads.append(t)
        self.log.info("폴더 감시 시작: %s (조용히 %.0f초 · 순찰 %s)", ", ".join(self.roots),
                      self.quiet_s, "{}분".format(self.rescan_s // 60) if self.rescan_s else "시작 때만")

    #--------------------------------------------------------------
    # 감시 끝
    #
    # -in: timeout = 스레드를 기다릴 초
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def stop(self, timeout=3.0):
        self._stop.set()
        try:
            import win32event
            for ev in self._stop_events:
                win32event.SetEvent(ev)
        except ImportError:
            pass
        for t in self._threads:
            t.join(timeout)
        self._threads = []

    # ── 바깥에서 부르는 것 ────────────────────────────

    #--------------------------------------------------------------
    # 전체 대조 부탁
    #=> 폴더 변화처럼 알림이 연달아 올 수 있는 일은 quiet_s 만큼 모았다가 한 번에 한다.
    #
    # -in: root      = 지정 폴더
    # -in: reason    = 이유(로그용)
    # -in: immediate = True 면 조용해지기를 기다리지 않는다(시작·순찰)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def request_rescan(self, root, reason, immediate=False):
        now = time.monotonic()
        with self._lock:
            if root not in self._rescan_at:
                return
            self._rescan_at[root] = (now - self.quiet_s) if immediate else now
            if reason not in self._rescan_why[root]:
                self._rescan_why[root] = (self._rescan_why[root] + ", " + reason).strip(", ")

    #--------------------------------------------------------------
    # 알림 한 건 받기 (알림 스레드 → 여기)
    #=> 문서 이름이면 "다시 볼 것" 에 넣는다. 같은 파일에 알림이 또 오면 시각만 늦춘다.
    #
    # -in: root   = 지정 폴더
    # -in: path   = 바뀐 경로
    # -in: action = ACTION_* (로그용 — 판정은 나중에 파일을 직접 보고 한다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def note(self, root, path, action=0):
        if not is_doc_name(path):
            return
        now = time.monotonic()
        key = os.path.normcase(path)
        with self._lock:
            pend = self._pending.get(root)
            if pend is None:
                return
            p = pend.get(key)
            if p is None:
                pend[key] = Pending(path, now)
            else:
                # 또 바뀌었다 — 다시 조용해질 때까지 기다리고 처음부터 두 번 잰다
                p.last, p.stat, p.tries, p.next_at = now, None, 0, now

    # ── 판정 스레드 ─────────────────────────────────

    #--------------------------------------------------------------
    # 판정 주기 돌리기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (한 번의 예외로 감시가 멈추지 않게 로그만 남긴다)
    #--------------------------------------------------------------
    def _tick_loop(self):
        while not self._stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception:
                self.log.exception("폴더 감시 판정에서 예외")

    #--------------------------------------------------------------
    # 한 번 판정하기 (시험에서 직접 부를 수 있게 공개)
    #=> 폴더마다 다 써진 파일을 모으고, 전체 대조 부탁·순찰 시각을 확인해 묶음을 넘긴다.
    #   파일 상태를 재는 일(디스크)은 잠금 밖에서 한다 — 알림 스레드를 막지 않게.
    #
    # -in: now = 지금(monotonic). 비우면 실제 시각
    #
    # -out: 넘긴 묶음 목록
    # -out: error = on_batch 예외는 로그만 남긴다
    #--------------------------------------------------------------
    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        out = []
        for root in self.roots:
            with self._lock:
                items = list(self._pending[root].items())
            changed, removed, done = [], [], []
            for key, p in items:
                verdict = settle(p, now, self.quiet_s, self.stable_s)
                if verdict == "wait":
                    continue
                done.append((key, p))
                if verdict == "changed":
                    if not is_hidden(p.path):
                        changed.append(p.path)
                elif verdict == "removed":
                    removed.append(p.path)
                else:
                    self.log.warning("계속 열 수 없어 다음 순찰로 미룬다: %s", p.path)
            with self._lock:
                pend = self._pending[root]
                for key, p in done:
                    # 판정하는 사이 새 알림이 왔으면(시각이 바뀌었으면) 지우지 않는다
                    if pend.get(key) is p and p.last <= now:
                        del pend[key]
                rescan, why = False, ""
                asked = self._rescan_at[root]
                if asked is not None and now - asked >= self.quiet_s:
                    rescan, why = True, self._rescan_why[root]
                elif self.rescan_s and now - self._last_rescan[root] >= self.rescan_s:
                    rescan, why = True, "순찰"
                if rescan:
                    self._rescan_at[root] = None
                    self._rescan_why[root] = ""
                    self._last_rescan[root] = now
            if changed or removed or rescan:
                b = Batch(root, changed, removed, rescan, why)
                rsb_log.diag(self.log, "감시 묶음: %r", b)
                out.append(b)
                try:
                    self.on_batch(b)
                except Exception:
                    self.log.exception("감시 묶음 전달에서 예외")
        return out

    # ── 알림 스레드 ─────────────────────────────────

    #--------------------------------------------------------------
    # 폴더 하나 지켜보기
    #=> 핸들 두 개를 겹쳐(overlapped) 걸어 두고, 알림이나 멈춤 신호를 기다린다.
    #    - 파일 핸들: 파일 이름·크기·수정 시각 → note()
    #    - 폴더 핸들: 하위 폴더 이름 변화 → 전체 대조
    #   폴더가 안 보이거나 끊기면 OFFLINE_RETRY_S 마다 다시 연다. 돌아오면 전체 대조한다
    #   (그동안 무슨 일이 있었는지 알림으로는 알 수 없다).
    #
    # -in: root    = 지정 폴더
    # -in: stop_ev = 멈춤 신호(win32 이벤트)
    #
    # -out: 없음
    # -out: error = 없음 (오류는 로그로 남기고 다시 연결을 시도한다)
    #--------------------------------------------------------------
    def _watch_root(self, root, stop_ev):
        import pywintypes
        import win32con
        import win32event
        import win32file

        FILE_FLAGS = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME | win32con.FILE_NOTIFY_CHANGE_SIZE
                      | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE)
        DIR_FLAGS = win32con.FILE_NOTIFY_CHANGE_DIR_NAME

        while not self._stop.is_set():
            watches = []
            try:
                for flags in (FILE_FLAGS, DIR_FLAGS):
                    h = win32file.CreateFile(
                        root, 0x0001,                     # FILE_LIST_DIRECTORY
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
                        | win32con.FILE_SHARE_DELETE,
                        None, win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED, None)
                    ov = pywintypes.OVERLAPPED()
                    ov.hEvent = win32event.CreateEvent(None, True, False, None)
                    buf = win32file.AllocateReadBuffer(BUF_SIZE)
                    watches.append([h, ov, buf, flags])
            except pywintypes.error as e:
                self._close(watches)
                self._set_online(root, False, e)
                if win32event.WaitForSingleObject(stop_ev, OFFLINE_RETRY_S * 1000) == win32event.WAIT_OBJECT_0:
                    return
                continue

            self._set_online(root, True)
            try:
                for w in watches:
                    win32file.ReadDirectoryChangesW(w[0], w[2], True, w[3], w[1])
                while not self._stop.is_set():
                    rc = win32event.WaitForMultipleObjects(
                        [watches[0][1].hEvent, watches[1][1].hEvent, stop_ev], False, 1000)
                    if rc == win32event.WAIT_TIMEOUT:
                        # ⚠️ 폴더 핸들은 폴더를 "따라간다" — 누가 폴더 이름을 바꾸거나 옮기면
                        #    옮겨 간 폴더를 계속 지켜보며 오류도 내지 않는다(실측). 그러면 옛 경로로
                        #    엉뚱하게 알리고 폴더가 없어진 것도 모른다. 그래서 1초마다 그 자리에
                        #    폴더가 있는지 직접 본다.
                        if not os.path.isdir(root):
                            raise pywintypes.error(3, "watch", "폴더가 그 자리에 없다(이름 변경·이동·분리)")
                        continue
                    idx = rc - win32event.WAIT_OBJECT_0
                    if idx == 2:
                        return
                    h, ov, buf, flags = watches[idx]
                    n = win32file.GetOverlappedResult(h, ov, False)
                    win32event.ResetEvent(ov.hEvent)
                    records = (win32file.FILE_NOTIFY_INFORMATION(buf, n)
                               if n and idx == 0 else [])
                    self.dispatch(root, idx == 1, n, records)
                    win32file.ReadDirectoryChangesW(h, buf, True, flags, ov)
            except pywintypes.error as e:
                # 네트워크가 끊겼거나 폴더가 지워졌다 — 닫고 다시 연결을 시도한다
                self._set_online(root, False, e)
            finally:
                self._close(watches)

    #--------------------------------------------------------------
    # 알림 한 번 처리 (알림 스레드 → 여기)
    #=> 따로 뗀 까닭: "버퍼가 넘치면 전체 대조" 는 실제로 넘치게 만들기가 들쭉날쭉해
    #   (읽는 속도에 달렸다) 이 판단만 직접 시험하려고.
    #    1) 0바이트 = 버퍼가 넘쳐 무엇이 바뀌었는지 모른다 → 폴더 전체 대조
    #    2) 폴더 핸들의 알림 = 하위 폴더 변화 → 폴더 전체 대조
    #    3) 파일 핸들의 알림 = 경로마다 note()
    #
    # -in: root    = 지정 폴더
    # -in: is_dir  = 폴더 핸들에서 온 알림인가
    # -in: n       = 받은 바이트 수
    # -in: records = [(action, 상대경로), …] (파일 알림일 때)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def dispatch(self, root, is_dir, n, records):
        if n == 0:
            self.request_rescan(root, "알림 넘침")
        elif is_dir:
            self.request_rescan(root, "하위 폴더 변화")
        else:
            for action, name in records:
                self.note(root, os.path.join(root, name), action)

    #--------------------------------------------------------------
    # 핸들 닫기
    #
    # -in: watches = [[h, ov, buf, flags], …]
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _close(self, watches):
        import win32file
        for w in watches:
            try:
                win32file.CancelIo(w[0])
                w[0].Close()
            except Exception:
                pass

    #--------------------------------------------------------------
    # 폴더가 보이는지 기록 (바뀔 때만 로그)
    #=> 안 보이다가 다시 보이면 전체 대조를 부탁한다.
    #
    # -in: root   = 지정 폴더
    # -in: online = 지금 보이는가
    # -in: err    = 안 보이는 이유(로그용)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _set_online(self, root, online, err=None):
        prev = self._online.get(root)
        self._online[root] = online
        if prev == online:
            return
        if online:
            if prev is False:
                self.log.info("폴더가 다시 보인다 — 전체 대조: %s", root)
                self.request_rescan(root, "다시 연결됨", immediate=True)
            else:
                rsb_log.diag(self.log, "폴더 알림 연결: %s", root)
        else:
            self.log.warning("폴더를 지켜볼 수 없다(%s초 뒤 다시): %s — %s", OFFLINE_RETRY_S, root, err)
