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
#   화면 모양은 Gemini 옆 창을 따른다(사용자 요청).
#    - 머리: 대화 제목 · 폴더, 오른쪽에 ⋯(대화 기록) · ✎(새 대화)
#    - 질문은 오른쪽 회색 말풍선, 답은 상자 없이 본문으로
#    - 출처는 답 아래 "▸ 참조 원문 N건" — 누르면 펼쳐진다(뉴플로이 AI 처럼)
#    - 아래는 둥근 입력 카드(폴더 칩 · 범위 · 보내기)
#   대화는 history.py 로 최근 10개까지 남기고 ⋯ 메뉴에서 다시 연다.
#
#   tkinter 는 메인 스레드에서만 다룬다. 워커·감시 스레드는 큐에 넣고 app 이 꺼내 부른다.
#------------------------------------------------------------------

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

import dock as rsb_dock
import history as rsb_history
import log as rsb_log
import settings as rsb_settings
from chat_parser import setup_bits

MAX_TURNS = 20          # 이보다 오래된 대화는 지운다(메모리·그리기 비용)
# 검색 범위(폴더 한정 검색 설계서 §3) — 인덱싱 폴더 = 패널 폴더라 "인덱스 전체" 는 두지 않는다
SCOPE_LABELS = {"folder": "이 폴더", "root": "지정 폴더 전체"}
INPUT_LINES = 3

# Gemini 옆 창에 가까운 색 — 흰 바탕, 회색 말풍선, 옅은 회청색 입력 카드
BG = "white"
INK = "#1f1f1f"          # 본문 글자
INK2 = "#444746"         # 출처 본문 등 버금 글자
MUTED = "#6b7280"        # 폴더·소요 줄 같은 흐린 글자
RULE = "#e3e6ea"         # 머리 아래 가는 줄
BUBBLE = "#e9eef6"       # 질문 말풍선
CARD = "#f0f4f9"         # 입력 카드
CHIP = "#dde3ea"         # 입력 카드 안 폴더 칩
HOVER = "#eef1f5"        # 아이콘 단추에 마우스를 올렸을 때
LINK = "#1a5fb4"         # 눌러지는 글자(출처 문서 · [...])
SRC_BG = "#f3f4f6"       # 출처 본문 상자
SEND = "#1f1f1f"         # 보내기 단추
ERR = "#b4462f"          # 오류 글자
BUBBLE_RATIO = 0.78      # 말풍선이 차지할 수 있는 폭(대화 칸 폭에 대한 비율)
SRC_LINES = 4            # 출처 본문 상자 높이(줄) — 넘치면 상자 안에서 굴린다


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
# 근거 전체 본문 팝업 (근거 줄의 [...])
#=> 패널에는 근거 앞부분 160자만 보인다. 답을 확인하려면 LLM 이 실제로 받은 근거 전체를
#   읽어야 해서, 누를 때마다 작은 창을 띄워 본문 전체를 보여 준다.
#    1) 제목은 "[번호] 문서 이름", 맨 위에 문서 경로
#    2) 본문은 읽기 전용 글 칸(스크롤) — 글자를 끌어 복사할 수 있다
#    3) 옛 simplerag.exe 라 전체 본문(full)이 없으면 보이는 앞부분과 안내를 보인다
#
# -in: parent  = 띄울 기준 창(패널)
# -in: item    = 근거 {no, doc, snippet, full?, path?}
# -in: f_base  = 본문 글꼴
# -in: f_small = 경로·안내 글꼴
#
# -out: 만든 Toplevel
# -out: error = 없음 (창을 못 만들면 tkinter 예외가 그대로 올라간다 — 부르는 쪽은 클릭 처리기)
#------------------------------------------------------------------
def show_evidence_popup(parent, item, f_base, f_small):
    pop = tk.Toplevel(parent)
    pop.title("[{}] {}".format(item.get("no", "?"), item.get("doc", "")))
    # 크기는 글꼴 기준으로 — 화면 배율(100%·150%)이 달라도 한글 40자 · 24줄쯤이 보이게
    pop.geometry("{}x{}".format(f_base.measure("가") * 42, f_base.metrics("linespace") * 26))
    pop.configure(bg="white")
    ico = rsb_settings.icon_path()
    if ico:
        try:
            pop.iconbitmap(ico)
        except Exception:
            pass

    head = item.get("path") or item.get("doc", "")
    tk.Label(pop, text=head, font=f_small, bg="white", fg="#647083", anchor="w",
             justify="left", wraplength=600).pack(fill="x", padx=12, pady=(10, 4))

    full = item.get("full")
    if not full:
        # 옛 워커 — 전체 본문을 받지 못했다. 있는 만큼 보이고 이유를 알린다
        full = (item.get("snippet") or "") + \
            "\n\n(전체 본문을 받지 못했습니다 — simplerag.exe 를 새로 빌드하면 보입니다)"

    # 아래 [닫기] 줄을 본문보다 먼저 붙인다 — 본문이 길어도 단추가 창 밖으로 밀려나지 않게
    foot = tk.Frame(pop, bg="white")
    foot.pack(side="bottom", fill="x", padx=12, pady=8)
    tk.Button(foot, text="닫기", command=pop.destroy, relief="groove", font=f_small,
              padx=14).pack(side="right")

    box = tk.Frame(pop, bg="#d7dbe3", padx=1, pady=1)
    box.pack(fill="both", expand=True, padx=12)
    bar = tk.Scrollbar(box, orient="vertical")
    bar.pack(side="right", fill="y")
    # height=1 — 기본값(24줄)을 요구하면 창 크기보다 커진다. 크기는 창이 정하고 글 칸은 채우기만
    txt = tk.Text(box, font=f_base, wrap="word", bg="white", relief="flat", height=1, width=1,
                  highlightthickness=0, bd=0, padx=8, pady=6, yscrollcommand=bar.set)
    txt.pack(side="left", fill="both", expand=True)
    bar.config(command=txt.yview)
    txt.insert("1.0", full)
    # 읽기 전용 — 고치지는 못하고 끌어서 복사만 된다
    txt.config(state="disabled")
    pop.bind("<Escape>", lambda _e: pop.destroy())

    # 패널은 탐색기 옆에 붙어 있어 가려질 수 있다 — 잠깐 맨 앞에 세우고 포커스를 준다
    pop.attributes("-topmost", True)
    pop.after(200, lambda: pop.winfo_exists() and pop.attributes("-topmost", False))
    pop.lift()
    pop.focus_force()
    return pop


#------------------------------------------------------------------
# 대화 한 마디 (질문 하나와 그 답)
#=> 화면 위젯과 값을 함께 들고 있어, 답이 흘러 들어올 때마다 그 자리에 채운다.
#   한 마디는 위에서부터 질문 말풍선 · 답 칸(ans) · 소요 줄(meta) · 출처(src) 로 쌓인다.
#   근거는 답보다 먼저 오지만 칸을 미리 만들어 두므로 출처는 언제나 답 아래에 놓인다.
#
# -필드: question = 물어본 글
# -필드: record   = 기록(history)에 남길 이 마디의 값 {q, scope, evidence, answer, meta, raw, error}
# -필드: docs     = 근거 문서 이름들(§16 "탐색기에서 보기" 에 쓴다)
#------------------------------------------------------------------
class Turn:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: frame    = 이 마디가 그려질 칸
    # -in: question = 질문
    # -in: record   = 기록 dict (같은 것을 대화 기록이 들고 있다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, frame, question, record):
        self.frame = frame
        self.question = question
        self.record = record
        self.docs = []
        self.answer_chars = 0
        self.ans = None          # 답 칸(기다림 글 → 답 본문)
        self.meta = None         # 소요 줄 칸
        self.src = None          # 출처 칸
        self.lbl_wait = None     # "근거를 찾는 중…"
        self.txt_answer = None


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
    # -in: history_path = 대화 기록 파일(비우면 로그 폴더 옆 history.json)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, settings, on_ask=None, history_path=None):
        self.root = root
        self.s = settings
        self.log = rsb_log.get("panel")
        self.on_ask = on_ask
        self.on_show_evidence = None
        # 폴더 한정 검색(폴더 한정 검색 설계서 §8) — app 이 채운다
        self.scope_mode = getattr(settings, "panel_search_scope", "folder")   # "folder" | "root"
        self.on_scope_change = None     # 범위를 바꾸면 부를 함수 fn(mode)
        self.scope_info = None          # 지금 범위 설명을 돌려주는 함수 fn() -> (이름, 문서 수)
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
        # 대화 기록(⋯ 메뉴) — 최근 10개. 지금 보고 있는 대화는 self.conv (새 대화면 None)
        self.history_path = history_path or rsb_history.default_path(rsb_log.log_dir())
        self.history = rsb_history.load(self.history_path)
        self.conv = None
        self._wrap_labels = []      # [(Label, 폭 비율)] — 패널 폭이 바뀌면 줄바꿈 폭을 다시 준다

    # ── 창 만들기 ─────────────────────────────────────

    #--------------------------------------------------------------
    # 위젯 만들기 (한 번만)
    #=> Gemini 옆 창 모양으로 위에서부터 머리 · 대화 칸 · 입력 카드로 나눈다.
    #    1) 머리: 대화 제목과 폴더 경로, 오른쪽에 ⋯(대화 기록) · ✎(새 대화)
    #    2) 가운데: 주고받은 내용이 쌓이는 칸(처음에는 인사 글)
    #    3) 아래: 입력 카드 — 폴더 칩, 질문 칸, 범위 고르기 · 상태 · 보내기(↑)
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
        win.configure(bg=BG)

        self.f_base = tkfont.Font(family="Malgun Gothic", size=s.font_size)
        self.f_bold = tkfont.Font(family="Malgun Gothic", size=s.font_size, weight="bold")
        self.f_small = tkfont.Font(family="Malgun Gothic", size=max(7, s.font_size - 2))
        self.f_link = tkfont.Font(family="Malgun Gothic", size=max(7, s.font_size - 2), underline=True)
        self.f_title = tkfont.Font(family="Malgun Gothic", size=s.font_size + 1, weight="bold")
        self.f_icon = tkfont.Font(family="Segoe UI Symbol", size=s.font_size + 3)
        self.f_hello = tkfont.Font(family="Malgun Gothic", size=s.font_size + 9, weight="bold")

        outer = tk.Frame(win, bg=BG)
        outer.pack(fill="both", expand=True)

        # ── 머리: 대화 제목 · 폴더 / ⋯ · ✎ ────────────────
        head = tk.Frame(outer, bg=BG, padx=14, pady=8)
        head.pack(fill="x")
        icons = tk.Frame(head, bg=BG)
        icons.pack(side="right", anchor="n")
        self.btn_hist = self._icon_button(icons, "⋯", self._show_history, "대화 기록")
        self.btn_new = self._icon_button(icons, "✎", self.new_chat, "새 대화")
        titles = tk.Frame(head, bg=BG)
        titles.pack(side="left", fill="x", expand=True)
        self.lbl_title = tk.Label(titles, text="새 대화", font=self.f_title, bg=BG, fg=INK,
                                  anchor="w")
        self.lbl_title.pack(fill="x")
        self.lbl_folder = tk.Label(titles, text="", font=self.f_small, bg=BG, fg=MUTED, anchor="w")
        self.lbl_folder.pack(fill="x")
        tk.Frame(outer, bg=RULE, height=1).pack(fill="x")

        # ── 아래: 입력 카드 (가운데보다 먼저 붙여 좁은 창에서도 밀려나지 않게) ──
        foot = tk.Frame(outer, bg=BG, padx=12, pady=10)
        foot.pack(fill="x", side="bottom")
        card = tk.Frame(foot, bg=CARD, padx=12, pady=8)
        card.pack(fill="x")
        crow = tk.Frame(card, bg=CARD)
        crow.pack(fill="x")
        # 폴더 칩 — Gemini 의 "…공유 중" 칩 자리. 어느 범위에서 찾는지 늘 보인다
        self.lbl_scope = tk.Label(crow, text="", font=self.f_small, bg=CHIP, fg=INK2,
                                  padx=8, pady=2, anchor="w")
        self.lbl_scope.pack(side="left")
        self.txt_input = tk.Text(card, height=INPUT_LINES, font=self.f_base, wrap="word",
                                 bg=CARD, fg=INK, relief="flat", highlightthickness=0, bd=0,
                                 padx=2, pady=6, insertbackground=INK)
        self.txt_input.pack(fill="x")
        self.txt_input.bind("<Return>", self._on_return)
        self.txt_input.bind("<Shift-Return>", lambda e: None)   # 줄바꿈은 그대로
        # 빈 입력 칸 안내(Gemini 처럼 칸 안에 흐리게) — 글을 쓰거나 칸을 누르면 사라진다
        self.lbl_ph = tk.Label(card, text="무엇이든 물어보세요 (Enter 전송)",
                               font=self.f_base, bg=CARD, fg=MUTED, cursor="xterm")
        self.lbl_ph.bind("<Button-1>", lambda _e: self.txt_input.focus_set())
        for ev in ("<KeyRelease>", "<FocusIn>", "<FocusOut>"):
            self.txt_input.bind(ev, lambda _e: self._sync_placeholder(), add="+")
        # 글자 키를 누르는 순간 바로 걷는다 — 놓을 때까지 기다리면 첫 글자가 안내 글과 겹쳐 보인다
        self.txt_input.bind("<KeyPress>", lambda e: e.char and self.lbl_ph.place_forget(), add="+")
        self._sync_placeholder()

        bar = tk.Frame(card, bg=CARD)
        bar.pack(fill="x")
        # 범위 고르기 — 이 폴더 / 지정 폴더 전체. 기본 OptionMenu 표시는 어색해 글자 단추로 만든다
        self._scope_var = tk.StringVar(value=SCOPE_LABELS.get(self.scope_mode, SCOPE_LABELS["folder"]))
        self.mb_scope = tk.Menubutton(bar, text=self._scope_var.get() + " ▾", font=self.f_small,
                                      bg=CARD, fg=INK2, relief="flat", bd=0, padx=4, pady=2,
                                      activebackground=HOVER, cursor="hand2")
        scope_menu = tk.Menu(self.mb_scope, tearoff=0, font=self.f_small)
        for lbl in SCOPE_LABELS.values():
            scope_menu.add_radiobutton(label=lbl, variable=self._scope_var, value=lbl,
                                       command=lambda v=lbl: self._scope_chosen(v))
        self.mb_scope["menu"] = scope_menu
        self.mb_scope.pack(side="left")
        self.lbl_state = tk.Label(bar, text="", font=self.f_small, bg=CARD, fg=MUTED, anchor="w")
        self.lbl_state.pack(side="left", padx=(6, 0))
        self.btn_send = tk.Button(bar, text="↑", command=self._send, font=self.f_bold,
                                  bg=SEND, fg="white", activebackground="#3c4043",
                                  activeforeground="white", relief="flat", bd=0, width=3,
                                  cursor="hand2")
        self.btn_send.pack(side="right")

        # ── 가운데: 주고받은 내용이 쌓이는 칸 ───────────
        mid = tk.Frame(outer, bg=BG)
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg=BG, highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        # 스크롤 막대를 먼저 붙인다 — 나중에 붙이면 좁은 폭에서 대화 칸에 밀려 사라진다
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg=BG, padx=14, pady=10)
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", tags="body")
        self.body.bind("<Configure>", lambda e: self._sync_scroll())
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        for w in (self.canvas, self.body):
            w.bind("<MouseWheel>", self._on_wheel)

        # 처음 인사 — 대화가 없을 때만 보인다. 마크와 글은 대화 칸 한가운데(가로·세로)에 둔다
        self.frm_hint = tk.Frame(self.body, bg=BG)
        self.frm_hint.pack(fill="x", pady=(12, 0))
        # 모아이 마크 — 그림 파일이 없거나 못 읽으면 글만 보인다
        self._img_moai = None
        moai = rsb_settings.asset_path("moai.png")
        if moai:
            try:
                # PhotoImage 는 변수에 붙잡아 둬야 한다 — 놓치면 지워져 빈 칸이 된다
                self._img_moai = tk.PhotoImage(file=moai)
                tk.Label(self.frm_hint, image=self._img_moai, bg=BG, anchor="center"
                         ).pack(fill="x", pady=(0, 14))
            except Exception:
                self.log.warning("모아이 마크를 읽지 못했다: %s", moai)
        tk.Label(self.frm_hint, text="무엇을 찾아 드릴까요?", font=self.f_hello, bg=BG, fg=INK,
                 anchor="center").pack(fill="x")
        self._wl(tk.Label(self.frm_hint, font=self.f_base, bg=BG, fg=MUTED, justify="center",
                          anchor="center", wraplength=s.win_width - 40,
                          text="이 폴더의 문서에 대해 물어보세요.\n예) 연차 이월 기준은 어떻게 되나요")
                 ).pack(fill="x", pady=(8, 0))
        self._bind_wheel(self.frm_hint)

        win.bind("<Escape>", lambda e: self.close_by_user())
        self.win = win

    #--------------------------------------------------------------
    # 입력 칸 안내 글 보이기/숨기기
    #=> Gemini 처럼 칸이 비어 있으면 늘 보이고, 글을 쓰기 시작하면 사라진다.
    #   (예전에는 포커스가 있으면 숨겼는데, 패널이 뒤에 있을 때 포커스 판단이 어긋나
    #    빈 칸인데도 안내가 안 보였다 — 사용자 화면에서 확인)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _sync_placeholder(self):
        try:
            empty = not self.txt_input.get("1.0", "end-1c")
        except Exception:
            return
        if empty:
            self.lbl_ph.place(in_=self.txt_input, x=2, y=6)
        else:
            self.lbl_ph.place_forget()

    #--------------------------------------------------------------
    # 머리의 아이콘 단추 하나 (⋯ · ✎)
    #=> 테두리 없는 글자 단추. 마우스를 올리면 옅게 칠해 눌러지는 것임을 알린다.
    #
    #   마우스를 잠시 올려 두면 아래에 이름(tip)을 작게 띄운다 — 아이콘만으로는 뜻을 모른다.
    #
    # -in: parent = 붙일 칸
    # -in: text   = 아이콘 글자
    # -in: fn     = 누르면 부를 함수
    # -in: tip    = 마우스를 올렸을 때 보일 이름(예: "새 대화")
    #
    # -out: Label
    # -out: error = 없음
    #--------------------------------------------------------------
    def _icon_button(self, parent, text, fn, tip=""):
        b = tk.Label(parent, text=text, font=self.f_icon, bg=BG, fg=INK2, padx=7, pady=0,
                     cursor="hand2")
        b.pack(side="left")
        state = {"job": None, "win": None}

        # 이름 풍선 띄우기 — 아이콘 바로 아래
        def show_tip():
            state["job"] = None
            if not tip or state["win"] is not None:
                return
            w = tk.Toplevel(b)
            w.wm_overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=tip, font=self.f_small, bg=INK, fg="white", padx=6, pady=2).pack()
            w.geometry("+{}+{}".format(b.winfo_rootx() - 10, b.winfo_rooty() + b.winfo_height() + 2))
            state["win"] = w

        # 이름 풍선 걷기 (마우스가 떠났거나 눌렀을 때)
        def hide_tip():
            if state["job"]:
                b.after_cancel(state["job"])
                state["job"] = None
            if state["win"] is not None:
                state["win"].destroy()
                state["win"] = None

        # 눌렀을 때 — 풍선을 먼저 걷고(메뉴와 겹치지 않게) 동작을 부른다
        def click(_e):
            hide_tip()
            fn()

        # 마우스가 올라왔을 때 — 칠하고, 잠시 뒤 풍선
        def enter(_e):
            b.config(bg=HOVER)
            state["job"] = b.after(450, show_tip)

        # 마우스가 떠났을 때
        def leave(_e):
            b.config(bg=BG)
            hide_tip()

        b.bind("<Button-1>", click)
        b.bind("<Enter>", enter)
        b.bind("<Leave>", leave)
        return b

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
            self.lbl_folder.config(text=folder)
            self.refresh_scope()

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
    #   트레이 "화면 열기" 를 고르면 다시 뜬다.
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
    # 상태 한 줄 (준비 중 · 검색 중 …) — 입력 카드 아래 줄
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
    # 근거 받기 (답변보다 먼저 온다)
    #=> 기록에 남기고, 답 칸 아래 출처 칸에 "▸ 참조 원문 N건" 으로 접어 그린다.
    #
    # -in: items = [{no, doc, snippet, full?, path?}, …] (full·path 는 새 워커만 준다)
    # -in: ms    = 검색에 걸린 시간
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_evidence(self, items, ms):
        t = self._last_turn()
        if not t:
            return
        t.record["evidence"] = [dict(it) for it in items]
        self._draw_sources(t, items)
        if t.lbl_wait is not None:
            t.lbl_wait.config(text="● 답변을 만드는 중…")
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
        self._ensure_answer(t)
        t.txt_answer.configure(state="normal")
        t.txt_answer.insert("end", text)
        t.answer_chars += len(text)
        t.txt_answer.configure(state="disabled")
        t.record["answer"] = t.record.get("answer", "") + text
        self._fit_text(t.txt_answer)
        self._scroll_bottom()

    #--------------------------------------------------------------
    # 답변 마무리
    #=> 소요 줄을 답 아래에 달고, 끝에 붙은 빈 줄을 걷어 내고, 대화 기록을 저장한다.
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
        for warn in info.get("warnings", []):
            self.append_token("\n" + warn)
        self._drop_wait(t)
        bits = []
        if info.get("search_ms") is not None:
            bits.append("검색 {}ms".format(info["search_ms"]))
        if info.get("ttft_s") is not None:
            bits.append("첫 글자 {:.2f}s".format(info["ttft_s"]))
        if info.get("total_s") is not None:
            bits.append("완료 {:.2f}s".format(info["total_s"]))
        # 답을 만든 모델과 parent 설정 — 예전 simplerag.exe 면 줄이 없어 건너뛴다
        bits.extend(setup_bits(info))
        # AI 가 만든 답이면 출처로 확인하라는 말을 앞에 둔다(Gemini 의 작은 안내 자리)
        if t.txt_answer is not None:
            bits.insert(0, "AI 요약 — 아래 출처로 확인하세요")
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
                t.record["answer"] = t.record.get("answer", "").rstrip()
            except Exception:
                pass
        if bits:
            t.record["meta"] = " · ".join(bits)
            self._draw_meta(t, t.record["meta"])
        rsb_log.diag(self.log, "답변 완료: 근거 %d건 · 답변 %d자", len(t.docs), t.answer_chars)
        self._save_history()
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
        # 근거 없이 온 안내(관련 내용 없음·폴더에 문서 없음)는 답 칸에 안내 글로만 보인다
        t = self._last_turn()
        if not t or not text:
            return
        t.record["raw"] = text.strip()
        self._draw_raw(t, t.record["raw"])
        self._save_history()
        self._scroll_bottom()

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
            t.record["error"] = msg
            self._draw_error(t, msg)
            self._save_history()
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

    # ── 대화 기록 (⋯ 메뉴 · ✎ 새 대화) ───────────────────

    #--------------------------------------------------------------
    # 새 대화 (✎)
    #=> 지금 대화는 기록에 그대로 남고, 화면을 비워 새 대화를 시작한다.
    #   답변이 오는 중에는 바꾸지 않는다 — 남은 글자가 엉뚱한 대화에 붙지 않게.
    #   이미 빈 새 대화여도 눌렀다는 것을 알 수 있게 입력 칸에 커서를 두고 잠깐 알린다
    #   (화면이 그대로라 "눌러도 반응이 없다" 로 보였다 — 사용자 지적).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def new_chat(self):
        if not self.win:
            return
        if self._busy:
            self.set_state("답변이 끝난 뒤에 바꿀 수 있습니다")
            return
        already = self.conv is None and not self.turns
        self._clear_turns()
        self.conv = None
        self.lbl_title.config(text="새 대화")
        self._flash_state("이미 새 대화입니다 — 질문을 입력하세요" if already
                          else "새 대화를 시작합니다 (이전 대화는 ⋯ 에 있습니다)")
        try:
            self.txt_input.focus_force()
        except Exception:
            pass
        self._sync_placeholder()
        self.log.info("새 대화")

    #--------------------------------------------------------------
    # 상태 줄에 잠깐만 알리기 (3초 뒤 지운다)
    #
    # -in: text = 알릴 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _flash_state(self, text):
        self.set_state(text)
        job = getattr(self, "_job_flash", None)
        if job:
            try:
                self.win.after_cancel(job)
            except Exception:
                pass

        # 그사이 다른 글로 바뀌었으면(보내는 중 등) 건드리지 않는다
        def clear():
            self._job_flash = None
            if self.lbl_state.cget("text") == text:
                self.set_state("")

        self._job_flash = self.win.after(3000, clear)

    #--------------------------------------------------------------
    # 예전 이름 — "대화 지우기" 단추가 하던 일(지금은 새 대화와 같다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def clear(self):
        self.new_chat()

    #--------------------------------------------------------------
    # 대화 기록 메뉴 (⋯)
    #=> 최근 대화 10개를 최근 것부터 보인다. 지금 보고 있는 대화에는 ✓ 를 붙인다.
    #   맨 위는 "새 대화", 맨 아래는 "기록 모두 지우기".
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _show_history(self):
        menu = tk.Menu(self.win, tearoff=0, font=self.f_small)
        menu.add_command(label="✎  새 대화", command=self.new_chat)
        menu.add_separator()
        if not self.history:
            menu.add_command(label="(아직 기록이 없습니다)", state="disabled")
        for conv in self.history:
            mark = "✓  " if self.conv is conv else "     "
            menu.add_command(label=mark + rsb_history.menu_label(conv),
                             command=lambda c=conv: self.open_conv(c))
        if self.history:
            menu.add_separator()
            menu.add_command(label="기록 모두 지우기", command=self._clear_history)
        try:
            # ⋯ 바로 아래, 오른쪽 끝을 맞춰 연다
            x = self.btn_hist.winfo_rootx() + self.btn_hist.winfo_width() - 260
            y = self.btn_hist.winfo_rooty() + self.btn_hist.winfo_height()
            menu.tk_popup(max(0, x), y)
        finally:
            menu.grab_release()

    #--------------------------------------------------------------
    # 예전 대화 열기 (⋯ 메뉴에서 고름)
    #=> 화면을 비우고 그 대화의 마디를 기록에서 다시 그린다. 이어서 물으면 그 대화에 쌓인다.
    #
    # -in: conv = 대화 dict (self.history 안의 것)
    #
    # -out: 없음
    # -out: error = 없음 (그리다 예외가 나면 로그만)
    #--------------------------------------------------------------
    def open_conv(self, conv):
        if self._busy:
            self.set_state("답변이 끝난 뒤에 바꿀 수 있습니다")
            return
        self._clear_turns()
        self.conv = conv
        self.lbl_title.config(text=conv.get("title", ""))
        try:
            for rec in conv.get("turns", []):
                self._replay(rec)
        except Exception:
            self.log.exception("대화 기록을 다시 그리지 못했다")
        self.set_state("")
        self.log.info("대화 기록 열기: %s (%d마디)", conv.get("title", ""), len(conv.get("turns", [])))
        self._scroll_bottom()

    #--------------------------------------------------------------
    # 기록 모두 지우기 (⋯ 메뉴 맨 아래) — 한 번 더 묻는다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _clear_history(self):
        if not messagebox.askyesno("대화 기록 지우기", "저장된 대화 기록 {}개를 모두 지울까요?"
                                   .format(len(self.history)), parent=self.win):
            return
        self.history[:] = []
        self._save_history()
        self.new_chat()
        self.log.info("대화 기록을 모두 지웠다")

    #--------------------------------------------------------------
    # 대화 기록 저장
    #=> 지금 대화를 맨 앞으로 올리고(최근 10개만) 파일에 쓴다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (쓰기 실패는 로그만 — 대화는 계속된다)
    #--------------------------------------------------------------
    def _save_history(self):
        if self.conv is not None:
            rsb_history.touch(self.history, self.conv)
        if not rsb_history.save(self.history_path, self.history):
            self.log.warning("대화 기록을 저장하지 못했다: %s", self.history_path)

    #--------------------------------------------------------------
    # 기록의 마디 하나를 다시 그리기 (예전 대화 열기)
    #
    # -in: rec = 마디 dict
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _replay(self, rec):
        t = self._start_turn(rec.get("q", ""), rec.get("scope", ""), rec)
        if rec.get("evidence"):
            self._draw_sources(t, rec["evidence"])
        if rec.get("answer"):
            self._ensure_answer(t)
            t.txt_answer.configure(state="normal")
            t.txt_answer.insert("end", rec["answer"])
            t.txt_answer.configure(state="disabled")
            self._fit_text(t.txt_answer)
        if rec.get("raw"):
            self._draw_raw(t, rec["raw"])
        if rec.get("error"):
            self._draw_error(t, rec["error"])
        if rec.get("meta"):
            self._draw_meta(t, rec["meta"])

    # ── 마디 안 칸 그리기 ─────────────────────────────

    #--------------------------------------------------------------
    # 답 본문 칸 만들기 (처음 글자가 올 때 한 번)
    #=> 기다림 글을 걷고 상자 없는 글 칸을 둔다(Gemini 처럼 답은 바탕에 바로 쓴다).
    #
    # -in: t = 마디
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _ensure_answer(self, t):
        if t.txt_answer is not None:
            return
        self._drop_wait(t)
        # width=1 — 기본값(80글자)이면 칸보다 넓게 요구해 줄바꿈 위치가 어긋난다
        t.txt_answer = tk.Text(t.ans, font=self.f_base, wrap="word", bg=BG, fg=INK,
                               relief="flat", highlightthickness=0, bd=0, height=1, width=1)
        t.txt_answer.pack(fill="x")
        t.txt_answer.bind("<MouseWheel>", self._on_wheel)

    #--------------------------------------------------------------
    # 기다림 글 걷기 ("근거를 찾는 중…")
    #
    # -in: t = 마디
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _drop_wait(self, t):
        if t.lbl_wait is not None:
            try:
                t.lbl_wait.destroy()
            except Exception:
                pass
            t.lbl_wait = None

    #--------------------------------------------------------------
    # 소요 줄 그리기 (답 아래 작은 회색 글)
    #
    # -in: t    = 마디
    # -in: text = "AI 요약 — … · 검색 430ms · 완료 10.62s · …"
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _draw_meta(self, t, text):
        for w in t.meta.winfo_children():
            w.destroy()
        self._wl(tk.Label(t.meta, text=text, font=self.f_small, bg=BG, fg=MUTED, anchor="w",
                          justify="left")).pack(fill="x", pady=(6, 0))
        self._bind_wheel(t.meta)

    #--------------------------------------------------------------
    # 근거 없는 안내 그리기 (관련 내용 없음 등)
    #
    # -in: t    = 마디
    # -in: text = 안내 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _draw_raw(self, t, text):
        self._drop_wait(t)
        self._wl(tk.Label(t.ans, text=text, font=self.f_base, bg=BG, fg=INK2, anchor="w",
                          justify="left")).pack(fill="x")
        self._bind_wheel(t.ans)

    #--------------------------------------------------------------
    # 오류 그리기
    #
    # -in: t   = 마디
    # -in: msg = 오류 글
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _draw_error(self, t, msg):
        self._drop_wait(t)
        self._wl(tk.Label(t.ans, text="● " + msg, font=self.f_small, bg=BG, fg=ERR, anchor="w",
                          justify="left")).pack(fill="x", pady=(4, 0))
        self._bind_wheel(t.ans)

    #--------------------------------------------------------------
    # 출처 그리기 — "▸ 참조 원문 N건" (뉴플로이 AI 처럼 누르면 펼쳐진다)
    #=> 처음에는 접어 둔다. 펼치면 근거마다
    #    1) "번호. 문서 이름" — 경로를 알면 눌러서 그 파일을 연다
    #    2) [...] — 전체 본문을 큰 팝업으로
    #    3) 본문 상자(4줄, 넘치면 상자 안에서 굴림)
    #   머리 오른쪽 "탐색기에서 보기" 는 근거 파일들을 탐색기 탭에 띄운다(§16).
    #
    # -in: t     = 마디
    # -in: items = 근거 목록
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _draw_sources(self, t, items):
        for w in t.src.winfo_children():
            w.destroy()
        t.docs = [it.get("doc", "") for it in items]
        if not items:
            return
        head = tk.Frame(t.src, bg=BG)
        head.pack(fill="x", pady=(10, 0))
        label = "참조 원문 {}건".format(len(items))
        tog = tk.Label(head, text="▸ " + label, font=self.f_small, bg=BG, fg=INK2, cursor="hand2")
        tog.pack(side="left")
        if self.on_show_evidence:
            docs = list(t.docs)
            show = tk.Label(head, text="탐색기에서 보기", font=self.f_link, bg=BG, fg=LINK,
                            cursor="hand2")
            show.pack(side="right")
            show.bind("<Button-1>", lambda _e: self._show_files(docs))
        box = tk.Frame(t.src, bg=BG)          # 펼쳤을 때만 붙인다
        for it in items:
            self._draw_source_item(box, it)
        opened = [False]

        # 접기/펼치기 — 펼친 상자는 출처 머리 바로 아래에 붙는다
        def toggle(_e=None):
            opened[0] = not opened[0]
            if opened[0]:
                box.pack(fill="x")
                tog.config(text="▾ " + label)
            else:
                box.pack_forget()
                tog.config(text="▸ " + label)
            # 마지막 마디를 펼쳤으면 펼친 내용이 보이게 아래로 굴린다(위 마디는 자리를 지킨다)
            if opened[0] and self.turns and self.turns[-1] is t:
                self.win.after_idle(self._scroll_bottom)
            else:
                self.win.after_idle(self._sync_scroll)

        tog.bind("<Button-1>", toggle)
        self._bind_wheel(t.src)

    #--------------------------------------------------------------
    # 출처 한 건 그리기 (펼친 상자 안)
    #
    # -in: parent = 펼친 상자
    # -in: it     = 근거 {no, doc, snippet, full?, path?}
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _draw_source_item(self, parent, it):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(6, 0), padx=(10, 0))
        top = tk.Frame(row, bg=BG)
        top.pack(fill="x")
        more = tk.Label(top, text="[...]", font=self.f_small, bg=BG, fg=LINK, cursor="hand2")
        more.pack(side="right", anchor="n", padx=(6, 0))
        more.bind("<Button-1>", lambda _e, item=dict(it): show_evidence_popup(
            self.win, item, self.f_base, self.f_small))
        path = it.get("path") or ""
        name = self._wl(tk.Label(top, text="{}. {}".format(it.get("no", "?"), it.get("doc", "")),
                                 font=self.f_link if path else self.f_small, bg=BG,
                                 fg=LINK if path else INK, anchor="w", justify="left",
                                 cursor="hand2" if path else ""), 0.8)
        name.pack(side="left", fill="x", expand=True)
        if path:
            name.bind("<Button-1>", lambda _e, p=path: self._open_file(p))

        text = it.get("full") or it.get("snippet") or ""
        box = tk.Frame(row, bg=SRC_BG)
        box.pack(fill="x", pady=(3, 0))
        bar = tk.Scrollbar(box, orient="vertical")
        bar.pack(side="right", fill="y")
        txt = tk.Text(box, font=self.f_small, wrap="word", bg=SRC_BG, fg=INK2, relief="flat",
                      highlightthickness=0, bd=0, padx=8, pady=4, height=SRC_LINES, width=1,
                      yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.config(command=txt.yview)
        txt.insert("1.0", text)
        txt.config(state="disabled")
        # 이 상자는 휠로 상자 안을 굴린다 — 대화 칸 굴리기로 덮어쓰지 않게 표시해 둔다
        txt._rsb_own_scroll = True
        bar._rsb_own_scroll = True

    #--------------------------------------------------------------
    # 출처 문서 열기 (문서 이름을 누름)
    #
    # -in: path = 문서 경로
    #
    # -out: 없음
    # -out: error = 없음 (못 열면 상태 줄에 알린다)
    #--------------------------------------------------------------
    def _open_file(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            self.set_state("파일을 열지 못했습니다: {}".format(e))
            self.log.warning("출처 파일을 열지 못했다: %s (%s)", path, e)

    #--------------------------------------------------------------
    # 줄바꿈 폭을 맞출 글 등록
    #=> 패널 폭이 바뀌면 _refit_all 이 "대화 칸 폭 × 비율" 로 다시 준다.
    #
    # -in: label = Label
    # -in: ratio = 대화 칸 폭에 대한 비율 (기본 1.0)
    #
    # -out: 같은 Label (이어서 pack 하게)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _wl(self, label, ratio=1.0):
        label.configure(wraplength=int(self._wrap() * ratio))
        self._wrap_labels.append((label, ratio))
        return label

    #--------------------------------------------------------------
    # 휠 굴리기 묶기 — 어느 글자 위에서 굴려도 대화 칸이 굴러가게
    #=> 출처 본문 상자처럼 스스로 굴리는 것(_rsb_own_scroll)은 건너뛴다.
    #
    # -in: widget = 묶을 칸(아래 모든 위젯까지)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _bind_wheel(self, widget):
        if getattr(widget, "_rsb_own_scroll", False):
            return
        widget.bind("<MouseWheel>", self._on_wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

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
    #   이미 없어진 이름표(지운 대화)는 목록에서 뺀다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _refit_all(self):
        self._job_refit = None
        wrap = self._wrap()
        alive = []
        for lbl, ratio in self._wrap_labels:
            try:
                lbl.configure(wraplength=int(wrap * ratio))
                alive.append((lbl, ratio))
            except Exception:
                pass                         # 지운 마디의 이름표 — 버린다
        self._wrap_labels = alive
        for t in self.turns:
            if t.txt_answer is not None:
                self._fit_text(t.txt_answer)
        self._sync_scroll()

    #--------------------------------------------------------------
    # 화면의 마디 모두 걷기 (새 대화 · 예전 대화 열기)
    #=> 처음 떴을 때처럼 인사 글만 남긴다. 기록(history)은 건드리지 않는다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _clear_turns(self):
        for t in self.turns:
            try:
                t.frame.destroy()
            except Exception:
                pass
        self.turns = []
        self.frm_hint.pack(fill="x", pady=(12, 0))
        self.canvas.yview_moveto(0.0)
        self._refit_all()
        self._center_hint()

    #--------------------------------------------------------------
    # 처음 인사를 대화 칸 세로 가운데에 두기
    #=> 인사는 굴리는 칸(canvas) 안에 있어 pack 만으로는 세로 가운데가 안 된다.
    #   보이는 칸 높이와 인사 높이의 차이 절반을 위 여백으로 준다(조금 위로 — 눈높이).
    #   칸 크기가 바뀔 때마다(_on_canvas_resize) 다시 맞춘다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (재지 못하면 그대로 둔다)
    #--------------------------------------------------------------
    def _center_hint(self):
        if self.turns:
            return                       # 대화가 있으면 인사는 숨어 있다
        try:
            self.win.update_idletasks()
            room = self.canvas.winfo_height()
            need = self.frm_hint.winfo_reqheight()
            top = max(12, (room - need) // 2 - 30)
            if self.frm_hint.pack_info().get("pady") != (top, 0):
                self.frm_hint.pack_configure(pady=(top, 0))
            self._sync_scroll()
        except Exception:
            pass

    #--------------------------------------------------------------
    # "닫기를 눌렀던 창" 표시 풀기
    #=> 그 창이 범위 밖 폴더로 나갔거나, 트레이에서 "화면 열기" 를 골랐을 때 부른다.
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
    # 패널을 앞으로 가져와 입력 칸에 커서 두기 (트레이 "화면 열기")
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
    #   새 대화면 이 질문을 제목으로 대화를 만들고, 마디를 기록에 더해 바로 저장한다
    #   (답이 오기 전에 꺼져도 질문은 남는다).
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
        self._sync_placeholder()
        if self.conv is None:
            self.conv = rsb_history.new_conv(text, self.folder)
            self.lbl_title.config(text=self.conv["title"])
        record = {"q": text, "scope": self._scope_desc()}
        self.conv["turns"].append(record)
        self._save_history()
        t = self._start_turn(text, record["scope"], record)
        # Gemini 처럼 답 자리에 기다림 글을 먼저 둔다 — 근거가 오면 바뀌고 답이 오면 걷힌다
        t.lbl_wait = tk.Label(t.ans, text="● 근거를 찾는 중…", font=self.f_small, bg=BG,
                              fg=MUTED, anchor="w")
        t.lbl_wait.pack(fill="x")
        self._busy = True
        self.set_state("보내는 중")
        self._scroll_bottom()
        if self.on_ask:
            try:
                self.on_ask(text)
            except Exception:
                self.log.exception("질문 전달에서 예외")

    #--------------------------------------------------------------
    # 새 마디 만들기 (질문 말풍선부터)
    #=> 질문은 오른쪽 회색 말풍선, 그 아래 범위 설명(작게), 그리고 비어 있는 답·소요·출처 칸.
    #
    # -in: question = 물어본 글
    # -in: scope    = 범위 설명("범위: sample_pdf · 문서 30건", 없으면 "")
    # -in: record   = 기록 dict
    #
    # -out: Turn
    # -out: error = 없음
    #--------------------------------------------------------------
    def _start_turn(self, question, scope, record):
        self.frm_hint.pack_forget()
        frame = tk.Frame(self.body, bg=BG)
        frame.pack(fill="x", pady=(4, 16))
        brow = tk.Frame(frame, bg=BG)
        brow.pack(fill="x")
        self._wl(tk.Label(brow, text=question, font=self.f_base, bg=BUBBLE, fg=INK,
                          justify="left", anchor="w", padx=14, pady=9),
                 BUBBLE_RATIO).pack(side="right")
        # 어느 범위에서 찾은 답인지 — 나중에 기록을 열어도 알 수 있게 말풍선 아래 남긴다
        if scope:
            tk.Label(frame, text=scope.replace("범위: ", ""), font=self.f_small, bg=BG,
                     fg=MUTED, anchor="e").pack(fill="x", pady=(2, 0))
        t = Turn(frame, question, record)
        t.ans = tk.Frame(frame, bg=BG)
        t.ans.pack(fill="x", pady=(10, 0))
        t.meta = tk.Frame(frame, bg=BG)
        t.meta.pack(fill="x")
        t.src = tk.Frame(frame, bg=BG)
        t.src.pack(fill="x")
        self._bind_wheel(frame)
        self.turns.append(t)

        while len(self.turns) > MAX_TURNS:
            old = self.turns.pop(0)
            try:
                old.frame.destroy()
            except Exception:
                pass
        return t

    #--------------------------------------------------------------
    # 범위 설명 한 줄 ("범위: 인사 · 문서 7건")
    #
    # -in: 없음
    #
    # -out: 글자 (모르면 "")
    # -out: error = 없음
    #--------------------------------------------------------------
    def _scope_desc(self):
        if not self.scope_info:
            return ""
        try:
            name, n = self.scope_info()
        except Exception:
            return ""
        if not name:
            return ""
        return "범위: {} · 문서 {}건".format(name, "?" if n is None else n)

    #--------------------------------------------------------------
    # 머리의 범위 줄 새로 쓰기 (폴더가 바뀌거나 범위를 바꾸거나 인덱스가 바뀌었을 때)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def refresh_scope(self):
        if not self.win:
            return
        desc = self._scope_desc()
        # 입력 카드의 폴더 칩 — "📁 sample_pdf · 문서 30건"
        self.lbl_scope.config(text=("📁 " + desc.replace("범위: ", "")) if desc else "📁 범위 모름")

    #--------------------------------------------------------------
    # 범위 선택을 바꿨을 때
    #
    # -in: label = 고른 글("이 폴더" 등)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _scope_chosen(self, label):
        self.mb_scope.config(text=label + " ▾")
        mode = next((k for k, v in SCOPE_LABELS.items() if v == label), "folder")
        if mode == self.scope_mode:
            return
        self.scope_mode = mode
        self.log.info("검색 범위: %s", label)
        if self.on_scope_change:
            try:
                self.on_scope_change(mode)
            except Exception:
                self.log.exception("범위 바꾸기에서 예외")
        self.refresh_scope()

    #--------------------------------------------------------------
    # "탐색기에서 보기" (§16, 예전 "근거 파일 보기" 단추)
    #=> 출처 머리의 링크로 옮겼다. 그 마디의 근거 문서들을 탐색기 탭에 띄운다
    #   (예전 대화를 열어서 눌러도 그 대화의 근거가 뜬다).
    #
    # -in: docs = 근거 문서 이름 목록
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _show_files(self, docs=None):
        if self.on_show_evidence:
            try:
                self.on_show_evidence(docs)
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
        # 높이가 바뀌면 처음 인사의 세로 가운데 자리도 달라진다
        if event.height != getattr(self, "_canvas_h", None):
            self._canvas_h = event.height
            if not self.turns:
                self.win.after_idle(self._center_hint)
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
