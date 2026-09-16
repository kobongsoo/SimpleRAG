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
        h = hwnd_of(win)
        if prev_fg and h and user32.GetForegroundWindow() == h:
            user32.SetForegroundWindow(prev_fg)
    except Exception:
        pass


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
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

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
            self.win.deiconify()
            show_no_activate(self.win)
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
    #=> 그 탐색기 창에서는 다시 뜨지 않는다. 다른 폴더에 갔다 오면 다시 뜬다.
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
                self.win.deiconify()
                show_no_activate(self.win)
                give_focus_back(self.win, prev_fg)
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
            t.txt_answer = tk.Text(t.frame, font=self.f_base, wrap="word", bg="white",
                                   relief="flat", highlightthickness=0, bd=0, height=1)
            t.txt_answer.pack(fill="x")
            t.txt_answer.bind("<MouseWheel>", self._on_wheel)
        t.txt_answer.configure(state="normal")
        t.txt_answer.insert("end", text)
        t.answer_chars += len(text)
        # 글자 수에 맞춰 칸 높이를 늘린다(Text 는 스스로 늘지 않는다)
        lines = int(t.txt_answer.index("end-1c").split(".")[0])
        t.txt_answer.configure(height=max(1, lines), state="disabled")
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
        return max(200, self.s.win_width - 50)

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
    # 자리 잡기 — 오른쪽 띠에 두고 탐색기를 왼쪽으로 물린다 (§18)
    #
    # -in: first = 처음 띄울 때인지(이때만 탐색기를 옮긴다)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _place(self, first=False):
        work = rsb_dock.work_area(self.target_hwnd)
        strip = rsb_dock.right_strip(work, self.s.win_width)
        region = rsb_dock.left_region(work, self.s.win_width)

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

        if strip != self._last_rect:
            self._last_rect = strip
            x, y, w, ht = strip
            # ⚠️ tkinter 에게도 알려 줘야 한다. SetWindowPos 로만 크기를 바꾸면
            #    tk 가 "내용에 맞는 크기" 로 곧 되돌려 놓는다(실측: 패널이 짧게 나왔다).
            self.win.geometry("{}x{}+{}+{}".format(w, ht, x, y))
            h = hwnd_of(self.win)
            if h:
                rsb_dock.place_above_target(h, self.target_hwnd, strip)

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
