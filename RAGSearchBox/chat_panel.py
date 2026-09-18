#------------------------------------------------------------------
# 폴더 패널 — 탐색기 옆에 붙는 대화창 (설계서 §18)
#=> 지정 폴더(또는 그 하위)를 탐색기에서 열면 오른쪽에 뜨는 단독 창이다.
#   위는 주고받은 내용이 쌓이는 칸, 아래는 질문을 쓰는 칸이다.
#
#   §7 의 답변 창과 무엇이 다른가
#    - 답변 창은 탐색기 검색창에 친 질문 한 건을 보여 주고 사라지는 창이었다.
#      이 패널은 폴더를 보는 동안 계속 떠서 여러 번 묻고 답을 쌓는다.
#    - 답변 창은 절대 포커스를 뺏지 않았다(WS_EX_NOACTIVATE). 이 패널은 사용자가
#      직접 글을 써야 하므로 그럴 수 없다. 대신 뜰 때만 활성화하지 않고(SW_SHOWNOACTIVATE),
#      사용자가 눌렀을 때 비로소 포커스를 가져간다.
#
#   tkinter 는 메인 스레드에서만 다룬다. 워커·감시 스레드는 큐에 넣고 app 이 꺼내 부른다.
#------------------------------------------------------------------

import tkinter as tk
from tkinter import font as tkfont

import dock as rsb_dock
import log as rsb_log
import settings as rsb_settings

MAX_TURNS = 20          # 이보다 오래된 대화는 지운다(메모리·그리기 비용)
INPUT_LINES = 3


#------------------------------------------------------------------
# 창 손잡이(HWND) 얻기
#=> tkinter 위젯 id 는 내부 자식 창이라 한 단계 위를 봐야 한다.
#
# -in: win = tkinter 창
#
# -out: HWND 또는 0
# -out: error = 없음
#------------------------------------------------------------------
def hwnd_of(win):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        wid = win.winfo_id()
        return user32.GetParent(wid) or wid
    except Exception:
        return 0


#------------------------------------------------------------------
# 활성화하지 않고 보여 주기
#=> 폴더를 열었을 뿐인데 타이핑하던 창을 뺏으면 안 된다. 보여 주기만 하고
#   포커스는 사용자가 패널을 누를 때 넘어간다.
#
# -in: win = tkinter 창
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def show_no_activate(win):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        h = hwnd_of(win)
        if h:
            user32.ShowWindow(h, 4)          # SW_SHOWNOACTIVATE
    except Exception:
        pass


#------------------------------------------------------------------
# 지금 전경 창
#=> 패널을 띄우기 직전에 기억해 두었다가, 뺏었으면 돌려주는 데 쓴다.
#
# -in: 없음
#
# -out: HWND 또는 0
# -out: error = 없음
#------------------------------------------------------------------
def foreground():
    try:
        import ctypes
        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return 0


#------------------------------------------------------------------
# "활성화되지 않는 창" 표시 켜고 끄기
#=> Windows 는 창을 보여 주는 그 순간에 활성화 여부를 정한다. 그래서
#   보여 주기 직전에 WS_EX_NOACTIVATE 를 켜 두면 아예 포커스를 가져가지 않는다.
#   보여 준 뒤 곧바로 꺼야 사용자가 눌렀을 때 글을 쓸 수 있다.
#   (뺏고 나서 돌려주는 방법만으로는 가끔 그대로 차지해 버렸다 — 실측 20/20.)
#
# -in: win = 패널 창
# -in: on  = True 면 활성화 안 함
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def set_no_activate(win, on):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        h = hwnd_of(win)
        if not h:
            return
        GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
        ex = user32.GetWindowLongW(h, GWL_EXSTYLE)
        user32.SetWindowLongW(h, GWL_EXSTYLE,
                              (ex | WS_EX_NOACTIVATE) if on else (ex & ~WS_EX_NOACTIVATE))
    except Exception:
        pass


#------------------------------------------------------------------
# 뺏은 포커스 돌려주기
#=> deiconify() 는 창을 활성화한다. SW_SHOWNOACTIVATE 만으로는 이미 늦어서,
#   빼앗았으면 곧바로 원래 창으로 돌려준다(답변 창에서 겪은 것과 같은 함정).
#   이 패널은 사용자가 직접 글을 써야 해서 WS_EX_NOACTIVATE 를 걸 수 없다 —
#   그러면 아예 입력을 받을 수 없기 때문이다.
#
# -in: win     = 패널 창
# -in: prev_fg = 띄우기 직전의 전경 창
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def give_focus_back(win, prev_fg):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not prev_fg or not user32.IsWindow(prev_fg):
            return
        # 우리 창이 아니라 "원래 창이 아닌 것" 을 기준으로 본다 —
        # 창 손잡이가 방금 바뀐 경우에도 제대로 돌려주기 위해서다
        if user32.GetForegroundWindow() != prev_fg:
            user32.SetForegroundWindow(prev_fg)
    except Exception:
        pass


#------------------------------------------------------------------
# 창을 확실히 맨 앞으로 (사용자가 직접 불렀을 때만 쓴다)
#=> Windows 는 뒤에 있는 프로그램이 SetForegroundWindow 로 앞에 나서는 것을 막는다
#   (작업 표시줄만 깜빡이고 끝난다). 트레이 메뉴를 거치면 대개 허락되지만,
#   메뉴가 닫히는 사이 다른 창이 앞을 차지하면 막힌다(실측: 한 번 실패).
#    1) 먼저 그냥 시도한다
#    2) 안 되면 지금 앞에 있는 창의 입력 스레드에 잠깐 붙어서 다시 시도한다
#       — 같은 입력 흐름에 속하면 앞에 나설 수 있다는 Windows 규칙을 쓰는 것이다
#
# -in: h = 앞으로 가져올 창
#
# -out: True = 앞으로 왔다
# -out: error = 없음 (실패하면 False)
#------------------------------------------------------------------
def bring_to_front(h):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if user32.SetForegroundWindow(h) and user32.GetForegroundWindow() == h:
            return True
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        me = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True))
        try:
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
        finally:
            # 붙었던 것은 반드시 떼어 낸다 — 붙은 채로 두면 두 창의 키 입력이 엉킨다
            if attached:
                user32.AttachThreadInput(me, fg_tid, False)
        return user32.GetForegroundWindow() == h
    except Exception:
        return False


#------------------------------------------------------------------
# 지금 마우스 왼쪽 단추가 눌려 있는가
#=> 사용자가 패널을 끌고 있는 중에 우리가 자리를 되돌리면 서로 싸운다.
#   끌고 있는 동안에는 가만히 두고, 놓은 뒤에 다시 붙인다.
#
# -in: 없음
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _mouse_down():
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return False


#------------------------------------------------------------------
# 대화 한 마디 (질문 하나와 그 답)
#=> 화면 위젯과 값을 함께 들고 있어, 답이 흘러 들어올 때마다 그 자리에 채운다.
#
# -필드: question = 물어본 글
# -필드: docs     = 근거 문서 이름들(§16 "근거 파일 보기" 에 쓴다)
#------------------------------------------------------------------
class Turn:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: frame / question = 이 마디가 그려질 칸과 질문
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, frame, question):
        self.frame = frame
        self.question = question
        self.docs = []
        self.answer_chars = 0
        self.lbl_status = None
        self.frm_evidence = None
        self.txt_answer = None
        self.lbl_timing = None


#------------------------------------------------------------------
# 폴더 패널
#=> 창 하나를 만들어 두고 폴더가 바뀌면 제목과 대상 창만 갈아 끼운다.
#
# -필드: visible = 지금 떠 있는지
# -필드: folder  = 지금 보고 있는 폴더
# -필드: target_hwnd = 붙어 있는 탐색기 창
#------------------------------------------------------------------
class ChatPanel:
    #--------------------------------------------------------------
    # 생성자
    #=> 위젯은 처음 show() 할 때 만든다.
    #
    # -in: root     = 숨겨 둔 tk.Tk (메인 스레드)
    # -in: settings = Settings
    # -in: on_ask   = 사용자가 질문을 보냈을 때 부를 함수 fn(text)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, settings, on_ask=None):
        self.root = root
        self.s = settings
        self.log = rsb_log.get("panel")
        self.on_ask = on_ask
        self.on_show_evidence = None
        self.win = None
        self.visible = False
        self.folder = None
        self.target_hwnd = None
        self.turns = []
        self._busy = False
        self._last_rect = None
        self._width = settings.win_width   # 사용자가 너비를 바꾸면 여기에 반영된다
        self._frame = None          # 창 테두리가 차지하는 크기(한 번만 잰다)
        self._saved_width = settings.win_width   # INI 에 적혀 있는 값
        self._width_save_at = 0.0   # 너비를 저장한 시각(연달아 쓰지 않으려고)
        self._saved = None          # 탐색기 창을 옮기기 전 상태(되돌리려고)
        self._room_rect = None      # 우리가 옮겨 놓은 자리
        self._closed_for = set()    # 사용자가 닫기를 누른 탐색기 창들

    # ── 창 만들기 ─────────────────────────────────────

    #--------------------------------------------------------------
    # 위젯 만들기 (한 번만)
    #=> 위에서부터 폴더 줄 · 대화 칸 · 입력 칸으로 나눈다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _build(self):
        if self.win:
            return
        s = self.s
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("RAGSearchBox")
        ico = rsb_settings.icon_path()
        if ico:
            try:
                win.iconbitmap(ico)
            except Exception:
                pass
        win.protocol("WM_DELETE_WINDOW", self.close_by_user)

        self.f_base = tkfont.Font(family="Malgun Gothic", size=s.font_size)
        self.f_bold = tkfont.Font(family="Malgun Gothic", size=s.font_size, weight="bold")
        self.f_small = tkfont.Font(family="Malgun Gothic", size=max(7, s.font_size - 2))

        outer = tk.Frame(win, bg="white")
        outer.pack(fill="both", expand=True)

        # ── 머리: 지금 보고 있는 폴더 ──────────────────
        head = tk.Frame(outer, bg="#f5f6f8", padx=10, pady=6)
        head.pack(fill="x")
        self.lbl_folder = tk.Label(head, text="", font=self.f_small, bg="#f5f6f8",
                                   fg="#41485a", anchor="w")
        self.lbl_folder.pack(fill="x")

        # ── 가운데: 주고받은 내용이 쌓이는 칸 ───────────
        mid = tk.Frame(outer, bg="white")
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg="white", highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        # 스크롤 막대를 먼저 붙인다 — 나중에 붙이면 좁은 폭에서 대화 칸에 밀려 사라진다
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg="white", padx=10, pady=8)
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", tags="body")
        self.body.bind("<Configure>", lambda e: self._sync_scroll())
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for w in (self.canvas, self.body):
            w.bind("<MouseWheel>", self._on_wheel)

        self.lbl_hint = tk.Label(
            self.body, font=self.f_small, bg="white", fg="#8b93a7", justify="left",
            anchor="w", wraplength=s.win_width - 40,
            text="이 폴더의 문서에 대해 물어보세요.\n예) 연차 이월 기준은 어떻게 되나요")
        self.lbl_hint.pack(fill="x", pady=(4, 0))

        # ── 아래: 입력 칸 ─────────────────────────────
        foot = tk.Frame(outer, bg="white", padx=10, pady=8)
        foot.pack(fill="x", side="bottom")

        box = tk.Frame(foot, bg="#d7dbe3", padx=1, pady=1)
        box.pack(fill="x")
        self.txt_input = tk.Text(box, height=INPUT_LINES, font=self.f_base, wrap="word",
                                 bg="white", relief="flat", highlightthickness=0, bd=0,
                                 padx=6, pady=5)
        self.txt_input.pack(fill="x")
        self.txt_input.bind("<Return>", self._on_return)
        self.txt_input.bind("<Shift-Return>", lambda e: None)   # 줄바꿈은 그대로

        bar = tk.Frame(foot, bg="white")
        bar.pack(fill="x", pady=(6, 0))
        self.lbl_state = tk.Label(bar, text="", font=self.f_small, bg="white", fg="#647083")
        self.lbl_state.pack(side="left")
        self.btn_send = tk.Button(bar, text="보내기", command=self._send, relief="groove",
                                  font=self.f_small)
        self.btn_send.pack(side="right")
        self.btn_files = tk.Button(bar, text="근거 파일 보기", command=self._show_files,
                                   relief="groove", font=self.f_small, state="disabled")
        self.btn_files.pack(side="right", padx=(0, 6))
        self.btn_clear = tk.Button(bar, text="대화 지우기", command=self.clear,
                                   relief="groove", font=self.f_small)
        self.btn_clear.pack(side="right", padx=(0, 6))

        win.bind("<Escape>", lambda e: self.close_by_user())
        self.win = win

    # ── 바깥에서 부르는 것들 ───────────────────────────

    #--------------------------------------------------------------
    # 폴더를 열었을 때 패널 띄우기 (§18)
    #=> 같은 창·같은 폴더면 아무 일도 하지 않는다. 폴더만 바뀌면 제목만 갈아 끼운다.
    #   사용자가 닫기를 누른 탐색기 창에서는 다시 띄우지 않는다.
    #
    # -in: hwnd   = 탐색기 창
    # -in: folder = 지금 보고 있는 폴더
    #
    # -out: 없음
    # -out: error = 없음 (만들기 실패는 로그만)
    #--------------------------------------------------------------
    def show_for(self, hwnd, folder):
        if hwnd in self._closed_for:
            return
        try:
            self._build()
            same_window = (self.visible and self.target_hwnd == hwnd)
            self.folder = folder
            self.lbl_folder.config(text="📁 " + folder)

            if same_window:
                return                       # 이미 그 창에 붙어 있다 — 폴더 이름만 바꿨다

            if self.visible and self.target_hwnd != hwnd:
                self._give_back()            # 붙어 있던 창은 원래대로 돌려준다
            self.target_hwnd = hwnd
            self._place(first=True)
            prev_fg = foreground()           # 사용자가 쓰던 창(보통 탐색기)
            set_no_activate(self.win, True)  # 보여 주는 순간 활성화되지 않게
            self.win.deiconify()
            show_no_activate(self.win)
            set_no_activate(self.win, False)  # 이제 사용자가 누르면 글을 쓸 수 있다
            give_focus_back(self.win, prev_fg)
            self.visible = True
            self.log.info("패널 표시: %s (창 %s)", folder, hwnd)
        except Exception:
            self.log.exception("패널을 띄우지 못했다")

    #--------------------------------------------------------------
    # 패널 숨기기 (범위 밖으로 나갔거나 창이 사라졌을 때)
    #
    # -in: give_back = True 면 탐색기 창을 원래 자리로 돌려준다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def hide(self, give_back=True):
        if not self.win or not self.visible:
            return
        if give_back:
            self._give_back()
        self.visible = False
        try:
            self.win.withdraw()
        except Exception:
            pass
        self.log.info("패널 숨김")

    #--------------------------------------------------------------
    # 사용자가 닫기를 눌렀을 때
    #=> 그 탐색기 창에서 범위 안 폴더를 오가는 동안은 다시 뜨지 않는다.
    #   범위 밖 폴더에 갔다 오거나(app 이 forget_closed 를 부른다),
    #   트레이 "창 열기" 를 고르면 다시 뜬다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def close_by_user(self):
        if self.target_hwnd:
            self._closed_for.add(self.target_hwnd)
        self.hide()

    #--------------------------------------------------------------
    # 탐색기를 따라가기 (app 이 50ms 마다 불러 준다)
    #=> 창이 사라지면 숨고, 최소화되면 같이 숨는다. 자리가 바뀌면 다시 맞춘다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def follow(self):
        if not self.visible or not self.win or not self.target_hwnd:
            return
        try:
            alive, minimized, rect = rsb_dock.target_state(self.target_hwnd)
            if not alive:
                self._saved = None           # 창이 없어졌다 — 되돌릴 것도 없다
                self.hide(give_back=False)
                return
            if minimized:
                if self.win.winfo_viewable():
                    self.win.withdraw()
                return
            if not self.win.winfo_viewable():
                prev_fg = foreground()
                set_no_activate(self.win, True)
                self.win.deiconify()
                show_no_activate(self.win)
                set_no_activate(self.win, False)
                give_focus_back(self.win, prev_fg)

            # 사용자가 지금 끌고 있는 중이면 건드리지 않는다 — 끌면서 되돌리면 싸움이 된다
            if _mouse_down():
                return

            self._adopt_user_size()
            self._place()
        except Exception:
            self.log.exception("패널이 탐색기를 따라가지 못했다")

    # ── 워커 이벤트 받기 ───────────────────────────────

    #--------------------------------------------------------------
    # 상태 한 줄 (준비 중 · 검색 중 …)
    #
    # -in: text = 보여 줄 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_state(self, text):
        if self.win:
            self.lbl_state.config(text=text or "")

    #--------------------------------------------------------------
    # 상태 한 줄 (답변 창과 이름을 맞춘 것)
    #=> app 이 두 화면을 같은 이름으로 부를 수 있게 둔 별칭이다.
    #
    # -in: text = 보여 줄 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_status(self, text):
        self.set_state(text)

    #--------------------------------------------------------------
    # 근거 보여 주기 (답변보다 먼저 온다)
    #
    # -in: items = [{no, doc, snippet}, …]
    # -in: ms    = 검색에 걸린 시간
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_evidence(self, items, ms):
        t = self._last_turn()
        if not t:
            return
        t.docs = [it.get("doc", "") for it in items]
        tk.Label(t.frame, text="근거 {}건 ({}ms)".format(len(items), ms), font=self.f_small,
                 bg="white", fg="#8b93a7", anchor="w").pack(fill="x", pady=(6, 2))
        for it in items:
            tk.Label(t.frame, text="[{}] {}".format(it.get("no", "?"), it.get("doc", "")),
                     font=self.f_base, bg="white", anchor="w", justify="left",
                     wraplength=self._wrap()).pack(fill="x")
            snip = (it.get("snippet") or "").strip()
            if snip:
                tk.Label(t.frame, text=snip, font=self.f_small, bg="white", fg="#6b7280",
                         anchor="w", justify="left",
                         wraplength=self._wrap()).pack(fill="x", pady=(0, 4))
        if t.docs:
            self.btn_files.config(state="normal")
        rsb_log.diag(self.log, "근거 %d건 그림 (검색 %sms)", len(items), ms)
        self._scroll_bottom()

    #--------------------------------------------------------------
    # 답변 글자 붙이기 (스트리밍)
    #
    # -in: text = 새로 온 조각
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def append_token(self, text):
        t = self._last_turn()
        if not t or not text:
            return
        if t.txt_answer is None:
            tk.Label(t.frame, text="AI 요약 — 위 근거로 확인하세요", font=self.f_small,
                     bg="white", fg="#a8701a", anchor="w").pack(fill="x", pady=(6, 2))
            # width=1 — 기본값(80글자)이면 칸보다 넓게 요구해 줄바꿈 위치가 어긋난다
            t.txt_answer = tk.Text(t.frame, font=self.f_base, wrap="word", bg="white",
                                   relief="flat", highlightthickness=0, bd=0, height=1,
                                   width=1)
            t.txt_answer.pack(fill="x")
            t.txt_answer.bind("<MouseWheel>", self._on_wheel)
        t.txt_answer.configure(state="normal")
        t.txt_answer.insert("end", text)
        t.answer_chars += len(text)
        t.txt_answer.configure(state="disabled")
        self._fit_text(t.txt_answer)
        self._scroll_bottom()

    #--------------------------------------------------------------
    # 답변 마무리
    #
    # -in: info = chat_parser 의 done 정보
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def finish(self, info):
        t = self._last_turn()
        self._busy = False
        self.set_state("")
        if not t:
            return
        bits = []
        if info.get("search_ms") is not None:
            bits.append("검색 {}ms".format(info["search_ms"]))
        if info.get("ttft_s") is not None:
            bits.append("첫 글자 {:.2f}s".format(info["ttft_s"]))
        if info.get("total_s") is not None:
            bits.append("완료 {:.2f}s".format(info["total_s"]))
        if bits:
            tk.Label(t.frame, text=" · ".join(bits), font=self.f_small, bg="white",
                     fg="#8b93a7", anchor="w").pack(fill="x", pady=(4, 0))
        for warn in info.get("warnings", []):
            self.append_token("\n" + warn)
        # 모델이 끝에 빈 줄을 붙이는 일이 잦다 — 그만큼 칸이 늘어 빈 틈이 생기므로 걷어 낸다
        if t.txt_answer is not None:
            try:
                body = t.txt_answer.get("1.0", "end-1c")
                tail = len(body) - len(body.rstrip())
                if tail:
                    t.txt_answer.configure(state="normal")
                    t.txt_answer.delete("end-1c - {}c".format(tail), "end-1c")
                    t.txt_answer.configure(state="disabled")
                    self._fit_text(t.txt_answer)
            except Exception:
                pass
        rsb_log.diag(self.log, "답변 완료: 근거 %d건 · 답변 %d자", len(t.docs), t.answer_chars)
        self._scroll_bottom()

    #--------------------------------------------------------------
    # 해석하지 못한 출력 그대로 보여 주기
    #
    # -in: text = 원문
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def show_raw(self, text):
        self.append_token(text)

    #--------------------------------------------------------------
    # 오류 한 줄
    #
    # -in: msg = 보여 줄 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def show_error(self, msg):
        self._busy = False
        self.set_state("● " + msg)
        t = self._last_turn()
        if t:
            tk.Label(t.frame, text="● " + msg, font=self.f_small, bg="white", fg="#b4462f",
                     anchor="w", justify="left", wraplength=self._wrap()).pack(fill="x",
                                                                               pady=(4, 0))
            self._scroll_bottom()
        self.log.warning("패널에 오류 표시: %s", msg)

    #--------------------------------------------------------------
    # 짧은 알림 (§16 단추 결과 등)
    #
    # -in: msg = 보여 줄 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def notice(self, msg):
        self.set_state(msg)

    # ── 안에서 쓰는 것들 ──────────────────────────────

    #--------------------------------------------------------------
    # 답변 칸 높이를 글 길이에 맞추기
    #=> Text 는 스스로 늘지 않는다. 예전에는 줄바꿈 문자 개수로 높이를 정했는데,
    #   긴 문장은 화면에서 여러 줄로 접히므로 칸이 모자라 안에 스크롤이 생기고
    #   뒷부분이 잘려 보였다(사용자 화면에서 확인). 화면에 실제로 보이는 줄 수로 센다.
    #    1) 줄바꿈 계산이 끝나도록 한 번 그리게 한다
    #    2) "displaylines" 로 접힌 줄까지 센다(첫 줄은 세지 않으므로 +1)
    #
    # -in: txt = 답변 Text 위젯
    #
    # -out: 없음
    # -out: error = 없음 (재지 못하면 그대로 둔다)
    #--------------------------------------------------------------
    def _fit_text(self, txt):
        try:
            txt.update_idletasks()
            n = txt.count("1.0", "end-1c", "displaylines")
            # tkinter 는 0 이면 None, 아니면 (n,) 을 준다
            n = (n[0] if isinstance(n, tuple) else n) or 0
            if int(txt.cget("height")) != n + 1:
                txt.configure(height=n + 1)
        except Exception:
            pass

    #--------------------------------------------------------------
    # 패널 폭이 바뀐 뒤 모든 글의 줄바꿈 다시 맞추기
    #=> 이름표(Label)는 줄바꿈 폭을 숫자로 들고 있어 폭이 바뀌어도 그대로다.
    #   답변 칸은 폭에 따라 줄 수가 달라진다. 둘 다 새 폭에 맞춘다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _refit_all(self):
        self._job_refit = None
        wrap = self._wrap()
        for t in self.turns:
            try:
                for w in t.frame.winfo_children():
                    if isinstance(w, tk.Label) and int(w.cget("wraplength") or 0) > 0:
                        w.configure(wraplength=wrap)
                if t.txt_answer is not None:
                    self._fit_text(t.txt_answer)
            except Exception:
                pass
        try:
            self.lbl_hint.configure(wraplength=wrap)
        except Exception:
            pass
        self._sync_scroll()

    #--------------------------------------------------------------
    # 주고받은 내용 모두 지우기 ("대화 지우기" 단추)
    #=> 처음 떴을 때처럼 안내 글만 남긴다.
    #   답변이 오는 중에 지우면, 남은 글자는 붙을 마디가 없어 조용히 버려진다
    #   (워커는 그대로 끝까지 답하고, 끝나면 다시 보낼 수 있다).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def clear(self):
        if not self.win:
            return
        for t in self.turns:
            try:
                t.frame.destroy()
            except Exception:
                pass
        self.turns = []
        self.btn_files.config(state="disabled")
        if not self._busy:
            self.set_state("")
        self.lbl_hint.pack(fill="x", pady=(4, 0))
        self.canvas.yview_moveto(0.0)
        self._sync_scroll()
        self.log.info("대화 지움")

    #--------------------------------------------------------------
    # "닫기를 눌렀던 창" 표시 풀기
    #=> 그 창이 범위 밖 폴더로 나갔거나, 트레이에서 "창 열기" 를 골랐을 때 부른다.
    #   그러면 다음에 범위 폴더로 돌아왔을 때 다시 뜬다.
    #
    # -in: hwnd = 탐색기 창
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def forget_closed(self, hwnd):
        if hwnd in self._closed_for:
            self._closed_for.discard(hwnd)
            self.log.info("닫았던 창 %s 에서 다시 띄울 수 있게 했다", hwnd)

    #--------------------------------------------------------------
    # 패널을 앞으로 가져와 입력 칸에 커서 두기 (트레이 "창 열기")
    #=> 사용자가 직접 부른 것이므로 이때는 포커스를 가져가도 된다.
    #
    # -in: retry = 막혔을 때 잠시 뒤 한 번 더 해 볼지 (기본 True)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def activate(self, retry=True):
        if not self.win or not self.visible:
            return
        try:
            self.win.deiconify()
            self.win.lift()
            h = hwnd_of(self.win)
            ok = bring_to_front(h) if h else False
            self.txt_input.focus_force()
            if not ok and retry:
                # 트레이 메뉴가 막 닫히는 중에는 가끔 막힌다 — 잠시 뒤 한 번 더
                self.win.after(250, lambda: self.activate(retry=False))
                return
            self.log.info("패널을 앞으로 가져왔다: %s", "성공" if ok else "실패(Windows 가 막음)")
        except Exception:
            pass

    #--------------------------------------------------------------
    # 지금 마디
    #
    # -in: 없음
    #
    # -out: Turn 또는 None
    # -out: error = 없음
    #--------------------------------------------------------------
    def _last_turn(self):
        return self.turns[-1] if self.turns else None

    #--------------------------------------------------------------
    # 글자 줄바꿈 폭
    #
    # -in: 없음
    #
    # -out: 픽셀
    # -out: error = 없음
    #--------------------------------------------------------------
    def _wrap(self):
        # 실제 칸 폭을 알면 그것을 쓴다 — 사용자가 너비를 바꿨을 수 있다
        w = getattr(self, "_canvas_w", None) or self._width
        return max(200, w - 40)

    #--------------------------------------------------------------
    # Enter 로 보내기
    #=> Shift+Enter 는 줄바꿈이라 여기서 가로채지 않는다.
    #
    # -in: event = 키 이벤트
    #
    # -out: "break" (기본 줄바꿈을 막는다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_return(self, event):
        if event.state & 0x0001:            # Shift 가 눌려 있으면 줄바꿈
            return None
        self._send()
        return "break"

    #--------------------------------------------------------------
    # 입력 칸의 글을 질문으로 보내기
    #=> 답변 중이면 알리고 보내지 않는다 — 워커는 한 번에 한 건만 답한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _send(self):
        text = self.txt_input.get("1.0", "end").strip()
        if not text:
            return
        if self._busy:
            self.set_state("답변이 끝난 뒤에 보낼 수 있습니다")
            return
        self.txt_input.delete("1.0", "end")
        self._start_turn(text)
        self._busy = True
        self.set_state("보내는 중")
        if self.on_ask:
            try:
                self.on_ask(text)
            except Exception:
                self.log.exception("질문 전달에서 예외")

    #--------------------------------------------------------------
    # 새 마디 만들기 (질문 줄부터)
    #
    # -in: question = 물어본 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _start_turn(self, question):
        self.lbl_hint.pack_forget()
        frame = tk.Frame(self.body, bg="white")
        frame.pack(fill="x", pady=(0, 10))
        tk.Label(frame, text=question, font=self.f_bold, bg="white", anchor="w",
                 justify="left", wraplength=self._wrap()).pack(fill="x")
        frame.bind("<MouseWheel>", self._on_wheel)
        self.turns.append(Turn(frame, question))
        self.btn_files.config(state="disabled")

        while len(self.turns) > MAX_TURNS:
            old = self.turns.pop(0)
            try:
                old.frame.destroy()
            except Exception:
                pass
        self._scroll_bottom()

    #--------------------------------------------------------------
    # "근거 파일 보기" (§16)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _show_files(self):
        if self.on_show_evidence:
            try:
                self.on_show_evidence()
            except Exception:
                self.log.exception("근거 파일 보기에서 예외")

    #--------------------------------------------------------------
    # 사용자가 바꾼 크기·자리 받아들이기 (§18)
    #=> 패널은 붙어 있는 창이라 아무 데나 떠 있으면 안 된다. 그래서
    #    - 너비를 바꿨으면 그 너비를 받아들이고, 탐색기 자리를 다시 만들어 준다
    #      (안 그러면 넓힌 만큼 탐색기를 덮는다 — 실측에서 그랬다)
    #    - 자리를 옮겼으면 제자리로 되돌린다
    #   높이는 탐색기 창에 맞추므로 사용자가 바꿔도 다음 배치 때 다시 맞춰진다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _adopt_user_size(self):
        h = hwnd_of(self.win)
        if not h:
            return
        try:
            import win32gui
            l, t, r, b = win32gui.GetWindowRect(h)
        except Exception:
            return
        width = r - l
        # ⚠️ 우리가 마지막으로 놓은 너비와 견준다. self._width 와 견주면 창 테두리 두께만큼
        #    차이가 나서, 우리가 만든 그 차이를 "사용자가 넓혔다" 로 잘못 읽는다
        #    (실측: 460 으로 놓았는데 476 이 저장됐다).
        placed = self._last_rect[2] if self._last_rect else self._width
        if abs(width - placed) <= 4:
            return                       # 너비는 그대로다
        if self._frame is None:
            return                       # 테두리 두께를 아직 모른다 — 섣불리 판단하지 않는다

        work = rsb_dock.work_area(self.target_hwnd)
        self._width = max(260, min(width, (work[2] - work[0]) // 2))
        self.log.info("패널 너비를 %dpx 로 바꿨다 — 탐색기 자리를 다시 만든다", self._width)
        self._last_rect = None           # 다시 배치하게 한다
        self._remember_width()           # 다음에 켤 때도 이 너비로 뜨게 (§18)

        # 넓어진 만큼 탐색기가 침범당하지 않게 다시 물린다
        if self.s.shrink_explorer and self.target_hwnd:
            region = rsb_dock.left_region(work, self._width)
            _, _, rect = rsb_dock.target_state(self.target_hwnd)
            if rect and not rsb_dock.fits_in(rect, region):
                if rsb_dock.make_room(self.target_hwnd, region):
                    _, _, now = rsb_dock.target_state(self.target_hwnd)
                    self._room_rect = now

    #--------------------------------------------------------------
    # 바뀐 너비를 설정 파일에 적어 두기 (§18)
    #=> 다음에 프로그램을 켤 때도 그 너비로 뜨게 한다.
    #   끄는 동안 여러 번 불리므로 2초에 한 번만, 값이 실제로 달라졌을 때만 쓴다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (읽기 전용 파일 등으로 실패하면 로그만 남기고 계속 쓴다)
    #--------------------------------------------------------------
    def _remember_width(self):
        import time
        if abs(self._width - self._saved_width) <= 4:
            return
        now = time.monotonic()
        if now - self._width_save_at < 2.0:
            return
        self._width_save_at = now
        ok, detail = rsb_settings.save_window_width(self.s.ini_path, self._width)
        if ok:
            self._saved_width = self._width
            self.s.win_width = self._width      # 이번 실행에도 바로 반영
            self.log.info("패널 너비 %dpx 를 설정에 저장했다", self._width)
        else:
            rsb_log.diag(self.log, "너비를 저장하지 못했다: %s", detail)

    #--------------------------------------------------------------
    # 자리 잡기 — 오른쪽 띠에 두고 탐색기를 왼쪽으로 물린다 (§18)
    #
    # -in: first = 처음 띄울 때인지(이때만 탐색기를 옮긴다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _place(self, first=False):
        work = rsb_dock.work_area(self.target_hwnd)
        strip = rsb_dock.right_strip(work, self._width)
        region = rsb_dock.left_region(work, self._width)

        if first and self.s.shrink_explorer:
            self._saved = rsb_dock.snapshot(self.target_hwnd)
            _, _, rect = rsb_dock.target_state(self.target_hwnd)
            if rect and not rsb_dock.fits_in(rect, region):
                if rsb_dock.make_room(self.target_hwnd, region):
                    _, _, now = rsb_dock.target_state(self.target_hwnd)
                    self._room_rect = now
                    self.log.info("탐색기를 왼쪽으로 물렸다: %s", now)
            else:
                self._saved = None           # 건드리지 않았으니 되돌릴 것도 없다

        drifted = False
        h0 = hwnd_of(self.win)
        if h0 and self._last_rect:
            try:
                import win32gui
                l, t, r, b = win32gui.GetWindowRect(h0)
                drifted = (abs(l - strip[0]) > 4 or abs(t - strip[1]) > 4
                           or abs((r - l) - strip[2]) > 4 or abs((b - t) - strip[3]) > 4)
            except Exception:
                drifted = False

        if strip != self._last_rect or drifted:
            self._last_rect = strip
            x, y, w, ht = strip
            # ⚠️ tkinter 에게도 알려 줘야 한다. SetWindowPos 로만 크기를 바꾸면
            #    tk 가 "내용에 맞는 크기" 로 곧 되돌려 놓는다(실측: 패널이 짧게 나왔다).
            #    다만 geometry 는 "내용 칸" 크기라, 창 테두리·제목 표시줄만큼 빼서 줘야
            #    창 전체가 딱 그 자리에 들어간다(안 그러면 처음 한 번 작업 영역을 넘친다).
            fw, fh = self._frame_delta()
            self.win.geometry("{}x{}+{}+{}".format(max(200, w - fw), max(120, ht - fh), x, y))
            h = hwnd_of(self.win)
            if h:
                rsb_dock.place_above_target(h, self.target_hwnd, strip)

    #--------------------------------------------------------------
    # 창 테두리가 차지하는 크기 재기 (한 번만)
    #=> tkinter 의 geometry 는 내용 칸 기준이고, 화면 배치는 창 전체 기준이다.
    #   그 차이를 한 번 재어 두고 계속 쓴다.
    #
    # -in: 없음
    #
    # -out: (가로 차이, 세로 차이) 픽셀
    # -out: error = 없음 (재지 못하면 (0, 0))
    #--------------------------------------------------------------
    def _frame_delta(self):
        if getattr(self, "_frame", None) is not None:
            return self._frame
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            h = hwnd_of(self.win)
            if not h:
                return (0, 0)
            # ⚠️ "창을 한 번 그려 보고 재는" 방법은 처음 띄울 때 아직 그려지지 않아 0 이 나온다.
            #    그러면 geometry 가 테두리만큼 커지고, 그 차이를 "사용자가 넓혔다" 로 잘못 읽어
            #    너비가 저절로 늘었다 줄었다 했다(실측: 460 → 476 → 450 …).
            #    Windows 에 직접 물어보면 그리기 전에도 정확한 값을 준다.
            style = user32.GetWindowLongW(h, -16)      # GWL_STYLE
            exstyle = user32.GetWindowLongW(h, -20)    # GWL_EXSTYLE
            r = wintypes.RECT(0, 0, 500, 500)
            if not user32.AdjustWindowRectEx(ctypes.byref(r), style, False, exstyle):
                return (0, 0)
            dw = (r.right - r.left) - 500
            dh = (r.bottom - r.top) - 500
            if 0 <= dw < 200 and 0 <= dh < 200:
                self._frame = (dw, dh)
                return self._frame
        except Exception:
            pass
        return (0, 0)

    #--------------------------------------------------------------
    # 옮겨 둔 탐색기 창 돌려주기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _give_back(self):
        if self._saved and self.target_hwnd:
            if rsb_dock.restore(self.target_hwnd, self._saved, self._room_rect):
                self.log.info("탐색기 창을 원래대로 돌려줬다")
        self._saved = None
        self._room_rect = None
        self._last_rect = None

    #--------------------------------------------------------------
    # 굴릴 범위 맞추기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _sync_scroll(self):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    #--------------------------------------------------------------
    # 칸 크기가 바뀌었을 때
    #
    # -in: event = Configure 이벤트
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_canvas_resize(self, event):
        try:
            self.canvas.itemconfigure("body", width=event.width)
        except Exception:
            return
        # 폭이 바뀌면 줄바꿈 위치가 달라진다 — 그리기가 끝난 뒤 높이를 다시 맞춘다
        if event.width != getattr(self, "_canvas_w", None):
            self._canvas_w = event.width
            if getattr(self, "_job_refit", None):
                try:
                    self.win.after_cancel(self._job_refit)
                except Exception:
                    pass
            self._job_refit = self.win.after(80, self._refit_all)
        self._sync_scroll()

    #--------------------------------------------------------------
    # 맨 아래로 굴리기 (새 내용이 붙을 때)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _scroll_bottom(self):
        try:
            self.win.update_idletasks()
            self._sync_scroll()
            self.canvas.yview_moveto(1.0)
        except Exception:
            pass

    #--------------------------------------------------------------
    # 마우스 휠
    #
    # -in: event = 휠 이벤트
    #
    # -out: "break"
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_wheel(self, event):
        try:
            self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        except Exception:
            pass
        return "break"

    #--------------------------------------------------------------
    # 끝내기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def close(self):
        self._give_back()
        self.visible = False
        if self.win:
            try:
                self.win.destroy()
            except Exception:
                pass
        self.win = None
