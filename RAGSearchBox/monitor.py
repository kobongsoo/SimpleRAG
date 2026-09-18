#------------------------------------------------------------------
# 감시 스레드 (설계서 §4·§5, 원본 ExplorerMonitorThreadFunc)
#=> 탐색기 검색창에 ? 로 시작하는 질문을 쓰고 Enter 를 누른 순간을 잡아 낸다.
#
#   원본은 "검색창에 포커스가 들어왔다" 자체가 트리거였다. 여기서는 답변 생성이
#   사람이 기다리는 일이라, 다 쓰고 Enter 를 누른 순간 만 트리거로 삼는다.
#
#   흐름
#    T1 무장(armed) : 포커스가 검색창에 들어오면 그 창 HWND · 검색창 요소 · 폴더를 기억한다
#    T2 확정        : armed 인 동안에만 Enter 를 확인하고, 붙잡아 둔 요소에서 글자를 읽는다
#    T4 넘기기      : 읽은 글자를 그대로 메인 스레드에 넘긴다(접두어·중복 거르기는 app 이 한다)
#
#   실측으로 정해진 것들
#    - Enter 는 누름 비트(0x8000)와 "지난 호출 뒤 눌렸음" 비트(0x1)를 둘 다 봐야 한다.
#      누름 비트만 보면 짧게 친 Enter 를 대부분 놓친다(P0-2).
#    - 글자는 Enter 약 80ms 뒤에 붙잡아 둔 요소 에서 읽는다. 그래야 조합 중이던
#      마지막 한글까지 들어온다. 포커스 요소를 읽으면 파일 이름이 읽힌다(P0-1).
#    - 검색을 실행하면 탐색기 위치가 검색 결과 화면이 되어 폴더를 읽을 수 없으므로
#      폴더는 Enter 전(armed 때)에 구해 둔다(P0-9).
#
#   이 스레드가 지켜야 할 것
#    - COM 은 이 스레드에서 초기화하고, UIA 요소도 이 스레드에서만 만진다
#    - WinEvent 콜백 객체는 self 에 붙잡아 둔다(수집되면 탐색기 이벤트 때 프로세스가 죽는다)
#    - 콜백 안에서 오래 걸리는 일을 하지 않는다 — 판정만 하고 끝낸다
#------------------------------------------------------------------

import ctypes
import queue
import threading
import time
from ctypes import wintypes

import log as rsb_log
import searchbox as rsb_searchbox
from evidence_list import EvidenceList
from explorer import ExplorerLocator

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.restype = ctypes.c_short

VK_RETURN = 0x0D
KEY_DOWN_BIT = 0x8000          # 지금 눌려 있음
KEY_PRESSED_BIT = 0x0001       # 지난 호출 뒤에 눌린 적 있음 (짧은 Enter 를 잡는 열쇠)

EVENT_OBJECT_FOCUS = 0x8005
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

EXPLORER_CLASS = "CabinetWClass"
READ_DELAY_S = 0.08            # Enter 뒤 글자를 읽기까지 기다리는 시간(P0-1 실측)
# 포커스가 검색창을 벗어나 보여도 이 시간 동안은 무장을 유지한다.
# 이유: ① 글자를 칠 때 탐색기가 제안 목록을 열며 포커스가 잠깐 흔들린다
#       ② Enter 를 누른 바로 그 순간 Win11 은 포커스를 파일 목록으로 옮긴다(P0-1).
#       즉시 풀면 그 Enter 를 통째로 놓친다. 대신 이 시간이 지나면 확실히 푼다 —
#       파일 목록에서 누른 Enter 가 질문으로 오해되면 안 되기 때문이다.
FOCUS_GRACE_S = 0.4
# §18 패널이 다른 탐색기 창으로 옮겨 가기 전에 그 창에 머물러야 하는 시간.
# 창을 빠르게 오갈 때마다 창을 물렸다 돌려줬다 하면 화면이 어지럽다.
SWITCH_DWELL_S = 0.8

WinEventProcType = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)


#------------------------------------------------------------------
# 무장 상태 한 벌
#=> 포커스가 검색창에 있는 동안 기억해 두는 것들. 풀리면 통째로 버린다.
#
# -필드: hwnd    = 그 탐색기 창
# -필드: element = 검색창 UIA 요소(Enter 뒤 글자를 읽을 대상)
# -필드: folders = armed 때 구한 폴더 후보(검색 뒤에는 못 구하므로 미리 잡아 둔다)
# -필드: anchor  = 검색창 화면 위치(답변 창을 그 아래에 놓는다)
# -필드: value   = 마지막으로 읽은 검색창 글자(? 선검사에 쓴다)
#------------------------------------------------------------------
class Armed:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: hwnd / element / folders / why / anchor = 위 필드 설명과 같다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, hwnd, element, folders, why, anchor):
        self.hwnd = hwnd
        self.element = element
        self.folders = folders
        self.folder_why = why
        self.anchor = anchor
        self.value = ""
        self.value_at = 0.0
        # 무장 직전에 눌려 있던 Enter 로 곧바로 확정되지 않게 시각을 남긴다
        self.armed_at = time.monotonic()
        # 포커스가 검색창을 벗어나 보이기 시작한 시각(잠깐 흔들린 것인지 가리는 데 쓴다)
        self.focus_lost_at = None


#------------------------------------------------------------------
# 최근에 사람이 입력했는가
#=> 아무도 키보드·마우스를 만지지 않는 동안에는 검색창 글자를 다시 읽을 이유가 없다.
#   UIA 조회는 프로세스 경계를 넘는 호출이라 공짜가 아니어서, 이 확인으로 건너뛴다.
#   (원본도 같은 목적으로 GetLastInputInfo 를 썼다.)
#
# -in: within_ms = 몇 ms 안의 입력을 "최근"으로 볼지
#
# -out: bool
# -out: error = 없음 (조회 실패는 True — 확인 못 하면 일단 읽는 쪽이 안전하다)
#------------------------------------------------------------------
def recent_input(within_ms):
    try:
        import win32api
        return (win32api.GetTickCount() - win32api.GetLastInputInfo()) <= within_ms
    except Exception:
        return True


#------------------------------------------------------------------
# 전경 창이 탐색기인가
#
# -in: 없음
#
# -out: (탐색기면 HWND, 아니면 None)
# -out: error = 없음
#------------------------------------------------------------------
def foreground_explorer():
    try:
        import win32gui
        h = win32gui.GetForegroundWindow()
        if h and win32gui.GetClassName(h) == EXPLORER_CLASS:
            return h
    except Exception:
        pass
    return None


#------------------------------------------------------------------
# 감시 스레드
#=> 포커스 이벤트로 무장하고, 주기 폴링으로 Enter 를 확정한다.
#
# -필드: armed  = 지금 무장 상태(없으면 None)
# -필드: status = 트레이에 보일 한 줄 상태(UIA 실패 같은 것)
#------------------------------------------------------------------
class Monitor(threading.Thread):
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: settings = Settings (poll_ms · input_recent_ms · prefix · monitor_mode)
    # -in: scope    = Scope (지정 폴더 판정기)
    # -in: on_query = 질문이 확정되면 부를 함수 fn(text, folders, anchor, hwnd).
    #                 감시 스레드에서 불리므로 받는 쪽은 큐에 넣기만 해야 한다
    # -in: on_note  = 알릴 일이 있을 때 부를 함수 fn(kind, message) (없어도 된다)
    # -in: on_folder= 탐색기가 보고 있는 폴더가 바뀌면 부를 함수 fn(hwnd, 폴더 또는 None).
    #                 폴더가 None 이면 "범위 밖이거나 알 수 없음" 이라는 뜻이다 (§18)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, settings, scope, on_query, on_note=None, on_folder=None):
        super().__init__(name="rsb-monitor", daemon=True)
        self.s = settings
        self.scope = scope
        self.on_query = on_query
        self.on_note = on_note or (lambda kind, msg: None)
        self.on_folder = on_folder or (lambda hwnd, folder: None)
        self.log = rsb_log.get("monitor")
        self.armed = None
        self.status = ""
        self.hook_ok = False
        self._stop = threading.Event()
        self._proc = None          # ⚠️ 콜백 객체를 붙잡아 두는 자리 (수집되면 죽는다)
        self._hook = None
        self._box = None
        self._locator = None
        self._focus_dirty = True   # 다음 주기에 포커스를 다시 보라는 표시
        self._last_forget = 0.0
        self._last_focus_scan = 0.0
        self._period = max(0.02, settings.poll_ms / 1000.0)
        # 근거 목록 요청함 (§16). UIA 요소는 이 스레드 것이라 여기서만 만져야 해서,
        # 메인 스레드는 부탁만 넣고 실제 일은 _tick 이 한다.
        self._requests = queue.Queue()
        self.evidence = None
        # §18 폴더 감시 — 지금 보고 있는 폴더가 범위 안이면 패널을 띄우게 알린다
        self._folder_seen = {}     # {hwnd: 마지막으로 알린 폴더}
        self._last_folder_check = 0.0
        self._watch_hwnd = None    # 패널이 붙어 있는 창(앞에 없어도 계속 지켜본다)
        self._fg_candidate = None  # 패널을 옮겨 갈까 보고 있는 창
        self._fg_since = 0.0

    #--------------------------------------------------------------
    # 스레드 본체
    #=> COM 초기화 → UIA 준비 → 훅 걸기 → 주기 루프 → 정리.
    #   UIA 를 못 만들면 감시를 시작하지 않는다(원본과 같은 동작). 트레이로 알린다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (예상 못 한 예외는 로그에 남기고 스레드만 끝난다)
    #--------------------------------------------------------------
    def run(self):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            self._box = rsb_searchbox.SearchBox()
            if not self._box.ok:
                self.status = self._box.error
                self.on_note("error", self._box.error)
                self.log.error("UIA 실패로 감시를 시작하지 못했다: %s", self._box.error)
                return
            self._locator = ExplorerLocator()
            self.evidence = EvidenceList(self._box)

            if not self.s.searchbox_trigger:
                self.log.info("검색창 감지는 꺼져 있다(Trigger.SearchBox=0) — 폴더 패널만 동작한다")
            elif self.s.monitor_mode == 0:
                self._install_hook()
            else:
                self.log.info("Monitor Mode=1 — 훅 없이 포커스를 주기적으로 조회한다")

            self._loop()
        except Exception:
            self.log.exception("감시 스레드가 예외로 끝났다")
        finally:
            self._remove_hook()
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    #--------------------------------------------------------------
    # 주기 루프
    #=> 메시지를 펌프하면서(그래야 WinEvent 콜백이 온다) PollMs 마다 할 일을 한다.
    #   MsgWaitForMultipleObjects 로 기다리면 메시지가 오는 즉시 깨어나고,
    #   메시지가 없으면 PollMs 뒤에 깨어난다 — 원본의 대기 방식과 같다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _loop(self):
        import win32event
        import win32gui

        period = self._period
        next_tick = time.monotonic()
        while not self._stop.is_set():
            # 남은 시간만큼만 기다린다(메시지가 오면 더 일찍 깨어난다)
            wait_ms = max(1, int((next_tick - time.monotonic()) * 1000))
            try:
                win32event.MsgWaitForMultipleObjects([], False, wait_ms,
                                                     win32event.QS_ALLINPUT)
                win32gui.PumpWaitingMessages()
            except Exception:
                self.log.exception("메시지 펌프에서 예외")
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + period
                try:
                    self._tick()
                except Exception:
                    self.log.exception("감시 주기 처리에서 예외")

    #--------------------------------------------------------------
    # 한 주기 할 일
    #=> ① 포커스가 바뀌었으면 무장 여부를 다시 본다
    #   ② 무장 중이면 상태가 아직 유효한지 확인하고
    #   ③ ? 로 시작할 때만 Enter 를 확인한다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _tick(self):
        # 메인 스레드가 부탁한 일(근거 목록 표시·되돌리기)을 먼저 처리한다
        self._serve_requests()

        # §18 폴더 패널 — 지금 보고 있는 폴더를 확인한다
        if self.s.panel_enabled:
            self._watch_folders()

        # 아래는 옛 방식(검색창에 ? 입력). 꺼져 있으면 검색창을 찾는 일 자체를 하지 않는다.
        if not self.s.searchbox_trigger:
            return

        # Enter 의 "눌렸음" 비트는 읽으면 지워진다. 무장 전에 눌린 Enter 가
        # 나중에 뒤늦게 잡히지 않도록 무장 여부와 상관없이 매 주기 한 번 읽어 비운다.
        state = user32.GetAsyncKeyState(VK_RETURN)

        # 훅이 없는 모드(또는 훅 실패)에서는 사람이 입력했을 때만 포커스를 조회한다.
        # 훅이 있어도 놓친 이벤트가 있을 수 있어, 무장 전에는 1초에 한 번 스스로 확인한다.
        now = time.monotonic()
        if recent_input(self.s.input_recent_ms):
            if not self.hook_ok:
                self._focus_dirty = True
            elif self.armed is None and (now - self._last_focus_scan) >= 1.0:
                self._last_focus_scan = now
                self._focus_dirty = True

        if self._focus_dirty:
            self._focus_dirty = False
            self._consider_focus()

        a = self.armed
        if a is None:
            return

        # 창이 바뀌었거나 검색 UI 가 닫혔으면 무장을 푼다
        if foreground_explorer() != a.hwnd:
            self._disarm("전경 창이 바뀜")
            return
        if not self._box.alive(a.element):
            self._disarm("검색창 요소가 사라짐")
            return

        # 포커스가 떠난 채로 유예 시간이 지났으면 무장을 푼다(마지막으로 한 번 더 확인한다)
        if a.focus_lost_at is not None:
            if (time.monotonic() - a.focus_lost_at) > FOCUS_GRACE_S:
                if self._still_in_box(a):
                    a.focus_lost_at = None          # 잠깐 흔들린 것이었다
                else:
                    self._disarm("검색창에서 포커스가 떠남")
                    return

        # 검색창 글자 갱신 — 사람이 막 입력했을 때만 읽는다(UIA 호출을 아끼려고)
        if recent_input(self.s.input_recent_ms):
            v = self._box.read_value(a.element)
            if v is not None:
                a.value = v
                a.value_at = time.monotonic()

        # 가벼운 선검사: ? 로 시작하지 않으면 Enter 를 봐도 할 일이 없다
        if not self._looks_like_question(a.value):
            return

        # 무장한 바로 그 주기에는 확인하지 않는다 — 무장 전에 눌려 있던 Enter 로
        # 엉뚱하게 확정되는 것을 막는다.
        if (time.monotonic() - a.armed_at) < self._period:
            return

        if (state & KEY_DOWN_BIT) or (state & KEY_PRESSED_BIT):
            self._confirm()

    #--------------------------------------------------------------
    # 메인 스레드가 부탁한 일 처리 (§16)
    #=> UIA 요소는 만든 스레드에서만 만질 수 있다. 그래서 답변 창(메인 스레드)은
    #   show_evidence()/restore_search() 로 부탁만 넣고, 실제 조작은 여기서 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실패는 on_note 로 알리고 로그만 남긴다)
    #--------------------------------------------------------------
    def _serve_requests(self):
        while True:
            try:
                what, hwnd, names = self._requests.get_nowait()
            except queue.Empty:
                return
            if self.evidence is None:
                continue
            try:
                if what == "show":
                    ok, detail = self.evidence.show(hwnd, names)
                else:
                    ok, detail = self.evidence.restore(hwnd)
                self.on_note("evidence" if ok else "evidence_fail", detail)
            except Exception:
                self.log.exception("근거 목록 처리에서 예외")
                self.on_note("evidence_fail", "탐색기를 조작하지 못했습니다")

    #--------------------------------------------------------------
    # 근거 파일 목록 띄우기 부탁 (다른 스레드에서 불러도 된다)
    #
    # -in: hwnd  = 질문이 나온 탐색기 창
    # -in: names = 근거 문서 이름 목록
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def show_evidence(self, hwnd, names):
        self._requests.put(("show", hwnd, list(names or [])))

    #--------------------------------------------------------------
    # 원래 검색어로 되돌리기 부탁
    #
    # -in: hwnd = 탐색기 창
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def restore_search(self, hwnd):
        self._requests.put(("restore", hwnd, None))

    #--------------------------------------------------------------
    # 지금 보고 있는 폴더 확인 (§18)
    #=> 앞에 있는 탐색기 창과, 패널이 붙어 있는 창을 함께 본다.
    #   앞에 없어도 붙어 있는 창은 계속 지켜봐야 한다 — 그 창이 다른 폴더로 가면
    #   패널을 내려야 하기 때문이다.
    #   폴더가 바뀐 창만 알린다(같은 폴더를 계속 알리면 화면이 깜빡인다).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (조회 실패는 조용히 넘어간다)
    #--------------------------------------------------------------
    def _watch_folders(self):
        now = time.monotonic()
        if (now - self._last_folder_check) * 1000 < self.s.folder_poll_ms:
            return
        self._last_folder_check = now

        targets = []
        fg = foreground_explorer()
        if fg:
            targets.append(fg)
        if self._watch_hwnd and self._watch_hwnd not in targets:
            targets.append(self._watch_hwnd)

        for hwnd in targets:
            try:
                paths, why = self._locator.folder_of(hwnd)
            except Exception:
                continue
            known = [p for p in paths if p]
            # ⚠️ 폴더를 "알 수 없음" 과 "범위 밖" 은 다르게 다룬다.
            #    IShellWindows 조회가 가끔 빈 결과를 준다(실측: 방금 뜬 창에서 한두 번).
            #    그것을 "범위 밖" 으로 읽으면 패널이 떴다 사라졌다 깜빡인다.
            #    모르겠으면 지난 판단을 그대로 둔다.
            resolved = bool(paths) and len(known) == len(paths)
            if not resolved:
                continue
            ok = all(self.scope.contains(p)[0] for p in known)
            folder = known[0] if ok else None
            if self._folder_seen.get(hwnd) == folder:
                continue
            self._folder_seen[hwnd] = folder
            rsb_log.diag(self.log, "폴더 바뀜: hwnd=%s → %s (%s)", hwnd, folder, why)
            try:
                self.on_folder(hwnd, folder)
            except Exception:
                self.log.exception("폴더 알림에서 예외")

        # 앞에 있는 창이 범위 안인데 패널은 다른 창에 붙어 있다면 그쪽으로 옮긴다.
        # (폴더가 "바뀔 때" 만 알리면, 같은 폴더를 보던 창으로 돌아왔을 때 패널이 따라오지
        #  않는다 — 창을 여러 개 띄워 놓고 오가는 확인에서 실제로 그랬다.)
        # 다만 잠깐 스쳐 지나가는 창까지 따라가면 창을 물렸다 돌려줬다 하며 어지러우므로,
        # 그 창에 SWITCH_DWELL_S 만큼 머물렀을 때만 옮긴다.
        if fg and self._watch_hwnd and fg != self._watch_hwnd and self._folder_seen.get(fg):
            if self._fg_candidate != fg:
                self._fg_candidate, self._fg_since = fg, now
            elif (now - self._fg_since) >= SWITCH_DWELL_S:
                self._fg_candidate = None
                folder = self._folder_seen.get(fg)
                rsb_log.diag(self.log, "패널을 앞의 창으로 옮긴다: hwnd=%s → %s", fg, folder)
                try:
                    self.on_folder(fg, folder)
                except Exception:
                    self.log.exception("폴더 알림에서 예외")
        else:
            self._fg_candidate = None

        # 닫힌 창의 기억은 버린다
        if len(self._folder_seen) > 16:
            try:
                import win32gui
                for h in [h for h in self._folder_seen if not win32gui.IsWindow(h)]:
                    self._folder_seen.pop(h, None)
            except Exception:
                pass

    #--------------------------------------------------------------
    # 패널이 붙어 있는 창 알려 주기 (§18)
    #=> 그 창은 앞에 없어도 계속 지켜본다.
    #
    # -in: hwnd = 탐색기 창(없으면 None)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def watch_window(self, hwnd):
        self._watch_hwnd = hwnd

    #--------------------------------------------------------------
    # 지금 범위 폴더를 보고 있는 탐색기 창 (트레이 "창 열기" 용)
    #=> 감시하면서 알게 된 창들 가운데, 아직 살아 있고 범위 안 폴더를 보던 것을
    #   화면에서 위에 있는 순서(가장 최근에 쓴 창이 먼저)로 돌려준다.
    #    1) 감시 스레드가 쓰는 표를 복사해 읽는다(도중에 바뀌어도 안전하게)
    #    2) EnumWindows 는 위에 있는 창부터 알려 주므로 그 순서를 그대로 쓴다
    #
    # -in: 없음
    #
    # -out: [(hwnd, folder), …] (없으면 빈 목록)
    # -out: error = 없음 (창 목록을 못 얻으면 빈 목록)
    #--------------------------------------------------------------
    def in_scope_windows(self):
        seen = dict(self._folder_seen)
        order = []
        try:
            import win32gui

            # 위에 있는 창부터 차례로 불린다 — 범위 안 폴더를 보던 창만 모은다
            def cb(h, _):
                if seen.get(h) and win32gui.IsWindowVisible(h):
                    order.append((h, seen[h]))
                return True

            win32gui.EnumWindows(cb, None)
        except Exception:
            return []
        return order

    #--------------------------------------------------------------
    # 질문처럼 보이는가 (가벼운 선검사)
    #=> 진짜 거르기는 app 의 query_filter 가 한다. 여기서는 Enter 폴링을 건너뛸지만 정한다.
    #   앞 공백을 뗀 첫 글자가 접두어(기본 ?, 전각 ？ 포함)인지만 본다.
    #
    # -in: text = 검색창 글자
    #
    # -out: bool
    # -out: error = 없음
    #--------------------------------------------------------------
    def _looks_like_question(self, text):
        t = (text or "").lstrip()
        if not t:
            return False
        prefixes = [self.s.prefix]
        if self.s.prefix == "?":
            prefixes.append("？")          # 한글 자판에서 흔히 나오는 전각 물음표
        return any(t.startswith(p) for p in prefixes if p)

    #--------------------------------------------------------------
    # 포커스를 보고 무장/해제 결정 (T1)
    #=> 전경이 탐색기이고 포커스가 검색창 안이면 무장한다.
    #   이때 폴더도 함께 구한다 — Enter 뒤에는 검색 결과 화면이라 못 구하기 때문이다.
    #   구한 폴더가 전부 지정 폴더 안일 때만 무장한다. 폴더를 아직 알 수 없으면
    #   (예: 검색 결과 화면인데 기억이 없음) 일단 무장해 두고 확정 때 다시 본다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _consider_focus(self):
        hwnd = foreground_explorer()
        if hwnd is None:
            if self.armed:
                self._disarm("전경 창이 탐색기가 아님")
            return

        e = self._box.focused()
        inside, why = self._box.is_inside(e)
        if not inside:
            a = self.armed
            if a is None:
                return
            # 곧바로 풀지 않는다 — 붙잡아 둔 검색창에게 직접 물어보고,
            # 그래도 아니면 유예 시간을 재기 시작한다(_tick 이 판정한다).
            if self._still_in_box(a):
                a.focus_lost_at = None
            elif a.focus_lost_at is None:
                a.focus_lost_at = time.monotonic()
                rsb_log.diag(self.log, "포커스가 검색창 밖으로 보임 (%s) — %.1f초 지켜본다",
                             why, FOCUS_GRACE_S)
            return

        if self.armed and self.armed.hwnd == hwnd:
            # 이미 무장돼 있다. 다만 붙잡아 둔 요소가 낡았을 수 있으므로(아래 _still_in_box
            # 설명 참고) 지금 포커스를 가진 요소로 바꿔 둔다.
            self.armed.element = e
            self.armed.anchor = self._box.rect_of(e) or self.armed.anchor
            self.armed.focus_lost_at = None     # 돌아왔다
            return

        value = self._box.read_value(e) or ""
        folders, fwhy = self._locator.folder_of(hwnd, value)
        rsb_log.diag(self.log, "무장 검토: hwnd=%s 판정=%s 폴더=%s (%s)",
                     hwnd, why, folders, fwhy)

        # 폴더를 알아냈는데 하나라도 지정 폴더 밖이면 무장하지 않는다(보수 규칙)
        known = [p for p in folders if p]
        if known and len(known) == len(folders):
            if not all(self.scope.contains(p)[0] for p in known):
                if self.armed:
                    self._disarm("범위 밖 폴더")
                rsb_log.diag(self.log, "범위 밖이라 무장하지 않는다: %s", known)
                return

        self.armed = Armed(hwnd, e, folders, fwhy, self._box.rect_of(e))
        self.armed.value = value
        rsb_log.diag(self.log, "무장됨: hwnd=%s 값=%r", hwnd, value[:20])

        # 닫힌 창의 폴더 기억은 가끔 정리한다(HWND 는 재사용되므로)
        now = time.monotonic()
        if now - self._last_forget > 60:
            self._last_forget = now
            self._locator.forget_closed()

    #--------------------------------------------------------------
    # 아직 검색창 안에 있는가 (요소가 새로 만들어진 경우까지 본다)
    #=> 탐색기는 검색 결과 화면에서 검색어를 지우거나 다시 칠 때 검색창 UI 를 새로 만든다.
    #   그러면 붙잡아 둔 요소는 속성은 읽히지만(살아 있는 것처럼 보이지만) 더 이상
    #   포커스를 쥔 그 칸이 아니다. 실측에서 이것 때문에 두 번째 질문을 놓쳤다.
    #   그래서 "붙잡아 둔 요소가 포커스냐"만 보지 않고, 지금 포커스를 가진 요소가
    #   같은 창의 검색창이면 붙잡는 대상을 그것으로 바꾼다.
    #
    # -in: a = 지금 무장 상태
    #
    # -out: True = 아직 검색창 안(필요하면 element 를 새것으로 바꾼다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _still_in_box(self, a):
        if self._box.has_focus(a.element):
            return True
        e = self._box.focused()
        inside, _why = self._box.is_inside(e)
        if inside and foreground_explorer() == a.hwnd:
            rsb_log.diag(self.log, "검색창 요소가 새로 만들어져 붙잡는 대상을 바꾼다")
            a.element = e
            a.anchor = self._box.rect_of(e) or a.anchor
            return True
        return False

    #--------------------------------------------------------------
    # 무장 풀기
    #
    # -in: why = 사유(DIAG 로그용)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _disarm(self, why):
        if self.armed is not None:
            rsb_log.diag(self.log, "무장 해제: %s", why)
        self.armed = None

    #--------------------------------------------------------------
    # Enter 확정 처리 (T2)
    #=> 붙잡아 둔 검색창 요소에서 글자를 읽어 메인 스레드로 넘긴다.
    #    1) 약 80ms 기다린다 — 조합 중이던 한글이 값에 반영될 시간이다(P0-1)
    #    2) 포커스 요소가 아니라 붙잡아 둔 요소 에서 읽는다
    #    3) 폴더를 다시 확인한다. armed 때 못 구했으면 여기서 구해 본다
    #    4) 지정 폴더 안일 때만 넘긴다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (글자를 못 읽으면 조용히 취소하고 DIAG 로그만 — 오작동보다 무반응이 낫다)
    #--------------------------------------------------------------
    def _confirm(self):
        a = self.armed
        self._disarm("Enter 확정")          # 같은 Enter 로 두 번 들어오지 않게 먼저 푼다
        if a is None:
            return

        time.sleep(READ_DELAY_S)
        text = self._box.read_value(a.element)
        if text is None:
            text = a.value                 # 요소가 이미 죽었으면 마지막으로 읽어 둔 값을 쓴다
        if not text or not self._looks_like_question(text):
            rsb_log.diag(self.log, "확정 취소 — 읽은 값이 질문이 아님: %r", text)
            return

        folders, fwhy = a.folders, a.folder_why
        if not [p for p in folders if p]:
            # 무장 때 폴더를 못 구했다 — 지금 다시 구해 본다(검색어를 알고 있으니 더 잘 구해진다)
            folders, fwhy = self._locator.folder_of(a.hwnd, text)

        known = [p for p in folders if p]
        if not folders or len(known) != len(folders):
            self.log.info("폴더를 가리지 못해 무시한다 (%s) — 질문 %r", fwhy, text[:20])
            return
        outside = [p for p in known if not self.scope.contains(p)[0]]
        if outside:
            self.log.info("범위 밖 폴더라 무시한다: %s — 질문 %r", outside, text[:20])
            return

        self.log.info("질문 확정: %r (폴더 %s)", text[:40], known[0])
        # 나중에 "원래대로" 로 되돌릴 수 있게 지금 검색창 글자를 기억해 둔다(§16)
        if self.evidence is not None:
            self.evidence.remember(a.hwnd, text)
        try:
            self.on_query(text, known, a.anchor, a.hwnd)
        except Exception:
            self.log.exception("질문 전달에서 예외")

    #--------------------------------------------------------------
    # WinEvent 훅 걸기
    #=> 탐색기의 포커스 이동을 알려 준다. 실패해도 프로그램은 돈다 —
    #   그 경우 입력 기반 폴링으로만 감지한다(설계서 §10).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실패는 hook_ok=False + 경고 로그)
    #--------------------------------------------------------------
    def _install_hook(self):
        try:
            self._proc = WinEventProcType(self._on_focus)   # 반드시 self 에 붙잡아 둔다
            self._hook = user32.SetWinEventHook(
                EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, 0, self._proc, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            self.hook_ok = bool(self._hook)
        except Exception:
            self.hook_ok = False
            self.log.exception("WinEvent 훅 설치에서 예외")
        if not self.hook_ok:
            self.log.warning("WinEvent 훅을 걸지 못했다 — 입력 기반 폴링으로 동작한다")
            self.on_note("warn", "포커스 훅 실패 — 폴링으로 동작합니다")

    #--------------------------------------------------------------
    # 훅 떼기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _remove_hook(self):
        if self._hook:
            try:
                user32.UnhookWinEvent(self._hook)
            except Exception:
                pass
        self._hook = None
        self._proc = None

    #--------------------------------------------------------------
    # 포커스 이벤트 콜백
    #=> Windows 가 부르는 함수라 여기서는 표시만 남기고 즉시 돌아온다.
    #   실제 판정은 다음 주기의 _tick 이 한다(콜백을 오래 잡고 있으면 탐색기가 느려진다).
    #
    # -in: 나머지 = Windows 가 주는 값들(쓰지 않는다)
    #
    # -out: 없음
    # -out: error = 없음 (여기서 예외가 나가면 프로세스가 죽으므로 통째로 감싼다)
    #--------------------------------------------------------------
    def _on_focus(self, hook, event, hwnd, id_object, id_child, thread, ts):
        try:
            self._focus_dirty = True
        except Exception:
            pass

    #--------------------------------------------------------------
    # 감시 끝내기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def stop(self):
        self._stop.set()
