#------------------------------------------------------------------
# 트레이 아이콘 (설계서 §7)
#=> 모델을 상주시키면 메모리 약 2.5GB 와 인덱스 잠금을 계속 쥐게 된다(P0-8 실측).
#   사용자가 그 상태를 보고 직접 내릴 수 있어야 해서 트레이를 둔다.
#   특히 `simplerag index` 를 돌리려면 먼저 모델을 내려야 한다.
#
#   메시지 창은 전용 스레드에서 돌린다. 트레이 아이콘은 창 메시지로 동작하므로
#   그 스레드가 메시지를 펌프해야 한다(원본 MDriveSearchBox 의 감시 스레드와 같은 구조).
#   메뉴를 누르면 콜백이 이 스레드에서 불리므로, 받는 쪽은 큐에 넣기만 해야 한다.
#------------------------------------------------------------------

import os
import threading

import win32api
import win32con
import win32gui

import log as rsb_log

WM_TRAY = win32con.WM_USER + 20        # 트레이 아이콘이 보내는 알림
WM_UPDATE = win32con.WM_USER + 21      # 다른 스레드가 "툴팁 갱신해라" 하고 보내는 것

# 메뉴 항목 번호와 이름 (app 이 이름으로 받는다)
MENU = [
    (1006, "open", "창 열기"),
    (1001, "unload", "모델 내리기 (인덱싱 전에)"),
    (1002, "reload", "모델 다시 올리기"),
    (0, None, None),                                   # 구분선
    (1007, "index_now", "지금 인덱싱"),
    (1008, "rescan", "전체 다시 확인"),
    (1009, "autoindex", "자동 인덱싱"),
    (0, None, None),
    (1003, "autorun", "로그인 시 자동 시작"),
    (1004, "logs", "로그 폴더 열기"),
    (1005, "exit", "종료"),
]


#------------------------------------------------------------------
# 트레이 아이콘
#=> 상태 글(툴팁)과 메뉴를 보여 주고, 고른 항목을 콜백으로 알린다.
#
# -필드: tooltip = 지금 보여 줄 상태 글
#------------------------------------------------------------------
class Tray(threading.Thread):
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: on_action    = 메뉴를 고르면 부를 함수 fn(name). 트레이 스레드에서 불린다
    # -in: autorun_flag = 자동 시작이 켜져 있는지 알려 주는 함수 fn() -> bool
    # -in: icon_path    = .ico 경로(없으면 기본 아이콘)
    # -in: flags        = 메뉴 이름별 체크 표시 함수 {이름: fn() -> bool} (예: 자동 인덱싱)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, on_action, autorun_flag=None, icon_path=None, flags=None):
        super().__init__(name="rsb-tray", daemon=True)
        self.on_action = on_action
        self.autorun_flag = autorun_flag or (lambda: False)
        self.flags = dict(flags or {})
        self.flags.setdefault("autorun", self.autorun_flag)
        self.icon_path = icon_path
        self.log = rsb_log.get("tray")
        self.tooltip = "RAGSearchBox"
        self._hwnd = None
        self._added = False
        self._ready = threading.Event()

    #--------------------------------------------------------------
    # 스레드 본체 — 숨은 창을 만들고 메시지를 펌프한다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실패는 로그만 남긴다. 트레이가 없어도 본 기능은 돈다)
    #--------------------------------------------------------------
    def run(self):
        try:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = "RagSearchBoxTray"
            wc.lpfnWndProc = self._wnd_proc
            cls = win32gui.RegisterClass(wc)
            self._hwnd = win32gui.CreateWindow(
                cls, "RAGSearchBox", 0, 0, 0, 0, 0, 0, 0,
                win32api.GetModuleHandle(None), None)
            self._add_icon()
            self._ready.set()
            win32gui.PumpMessages()
        except Exception:
            self.log.exception("트레이를 만들지 못했다")
            self._ready.set()

    #--------------------------------------------------------------
    # 아이콘 얻기
    #=> .ico 가 있으면 그것을, 없으면 Windows 기본 프로그램 아이콘을 쓴다.
    #
    # -in: 없음
    #
    # -out: 아이콘 핸들
    # -out: error = 없음 (읽기 실패 시 기본 아이콘)
    #--------------------------------------------------------------
    def _icon(self):
        if self.icon_path and os.path.isfile(self.icon_path):
            try:
                return win32gui.LoadImage(
                    0, self.icon_path, win32con.IMAGE_ICON, 0, 0,
                    win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
            except Exception:
                pass
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    #--------------------------------------------------------------
    # 트레이에 아이콘 넣기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _add_icon(self):
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_ADD,
            (self._hwnd, 0, flags, WM_TRAY, self._icon(), self.tooltip[:127]))
        self._added = True

    #--------------------------------------------------------------
    # 상태 글 바꾸기 (다른 스레드에서 불러도 된다)
    #=> 실제 갱신은 트레이 스레드가 한다 — 창 메시지로 넘긴다.
    #
    # -in: text = 보여 줄 상태 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_tooltip(self, text):
        self.tooltip = text or "RAGSearchBox"
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, WM_UPDATE, 0, 0)
            except Exception:
                pass

    #--------------------------------------------------------------
    # 알림 풍선 띄우기
    #=> 설정 오류처럼 사용자가 바로 알아야 하는 것에만 쓴다.
    #
    # -in: title = 제목
    # -in: msg   = 내용
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def notify(self, title, msg):
        if not self._hwnd or not self._added:
            return
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_MODIFY,
                (self._hwnd, 0, win32gui.NIF_INFO, WM_TRAY, self._icon(),
                 self.tooltip[:127], msg[:255], 5000, title[:63],
                 win32gui.NIIF_INFO))
        except Exception:
            self.log.exception("알림 풍선 실패")

    #--------------------------------------------------------------
    # 트레이 정리하고 메시지 루프 끝내기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def stop(self):
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass

    #--------------------------------------------------------------
    # 준비될 때까지 기다리기 (시작 순서를 맞추기 위해)
    #
    # -in: timeout = 한도(초)
    #
    # -out: True = 준비됨
    # -out: error = 없음
    #--------------------------------------------------------------
    def wait_ready(self, timeout=5.0):
        return self._ready.wait(timeout)

    #--------------------------------------------------------------
    # 마우스 오른쪽 단추 — 메뉴 보이기
    #=> 자동 시작 항목에는 지금 상태를 체크 표시로 보여 준다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _show_menu(self):
        menu = win32gui.CreatePopupMenu()
        # 맨 위에 상태를 회색으로 보여 준다(누를 수 없는 항목)
        win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, 1000,
                            self.tooltip[:80])
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        for ident, name, label in MENU:
            if name is None:
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                continue
            flags = win32con.MF_STRING
            try:
                if name in self.flags and self.flags[name]():
                    flags |= win32con.MF_CHECKED
            except Exception:
                pass                     # 체크 표시를 못 구해도 메뉴는 띄운다
            win32gui.AppendMenu(menu, flags, ident, label)

        pos = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self._hwnd)   # 메뉴 밖을 눌렀을 때 닫히게 하는 관례
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN, pos[0], pos[1],
                                0, self._hwnd, None)
        win32gui.PostMessage(self._hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    #--------------------------------------------------------------
    # 창 프로시저 — 트레이 알림과 메뉴 선택을 받는다
    #
    # -in: hwnd, msg, wparam, lparam = Windows 가 주는 값
    #
    # -out: 처리 결과(0 또는 기본 처리)
    # -out: error = 없음 (콜백 예외는 로그만 남긴다)
    #--------------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            if lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP):
                self._show_menu()
            return 0

        if msg == WM_UPDATE:
            try:
                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_MODIFY,
                    (hwnd, 0, win32gui.NIF_TIP, WM_TRAY, self._icon(), self.tooltip[:127]))
            except Exception:
                pass
            return 0

        if msg == win32con.WM_COMMAND:
            ident = win32api.LOWORD(wparam)
            for mid, name, _label in MENU:
                if mid and mid == ident:          # 0 은 구분선이다
                    try:
                        self.on_action(name)
                    except Exception:
                        self.log.exception("트레이 동작에서 예외: %s", name)
                    break
            return 0

        if msg in (win32con.WM_CLOSE, win32con.WM_DESTROY):
            try:
                if self._added:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                    self._added = False
            except Exception:
                pass
            win32gui.DestroyWindow(hwnd)
            win32gui.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
