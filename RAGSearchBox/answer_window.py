#------------------------------------------------------------------
# 답변 창 (설계서 §7)
#=> 근거를 먼저 보여 주고 답변을 흘린다. SimpleRAG 의 2단계 응답을 화면에 그대로 옮긴 것이다.
#   생성 정답률이 약 72% 라 근거 확인이 안전장치이므로, 답변보다 근거가 먼저·위에 온다.
#
#   포커스를 뺏지 않는 것이 중요하다
#     원본 MDriveSearchBox 는 검색 UI 가 포커스를 가져갔다가 돌아올 때 재실행되는 문제를
#     겪고 폴더 뷰로 포커스를 밀어 넣는 코드를 넣었다(D7-A). 여기서는 아예 활성화되지 않는
#     창(WS_EX_NOACTIVATE)으로 띄워 그 문제가 생기지 않게 한다. 사용자는 탐색기를 계속 쓴다.
#
#   tkinter 는 메인 스레드에서만 다룬다. 워커·감시 스레드는 큐에 넣고, app 이 꺼내
#   여기 메서드를 부른다.
#------------------------------------------------------------------

import tkinter as tk
from tkinter import font as tkfont

import dock as rsb_dock
import log as rsb_log
import settings as rsb_settings

# 화면 가장자리에서 이만큼 띄운다
MARGIN = 8


#------------------------------------------------------------------
# 창이 화면 밖으로 나가지 않게 위치 고르기
#=> 검색창 바로 아래에 붙이되, 오른쪽·아래로 넘치면 안쪽으로 당긴다.
#   순수 계산이라 화면 없이도 시험할 수 있다.
#
# -in: anchor = (left, top, right, bottom) 검색창 화면 좌표. None 이면 오른쪽 위
# -in: w      = 창 너비
# -in: h      = 창 높이
# -in: screen = (화면 너비, 화면 높이)
#
# -out: (x, y) 창 왼쪽 위 좌표
# -out: error = 없음
#------------------------------------------------------------------
def place_near(anchor, w, h, screen):
    sw, sh = screen
    if anchor:
        left, top, right, bottom = anchor
        x = right - w          # 검색창 오른쪽 끝에 맞춘다
        y = bottom + 4
    else:
        x = sw - w - MARGIN
        y = MARGIN

    x = max(MARGIN, min(x, sw - w - MARGIN))
    y = max(MARGIN, min(y, sh - h - MARGIN))
    return int(x), int(y)


#------------------------------------------------------------------
# 창 손잡이(HWND) 얻기
#=> tkinter 위젯 id 는 내부 자식 창이라, 한 단계 위(진짜 창)를 찾아야 한다.
#
# -in: win = tkinter Toplevel
#
# -out: HWND 정수 또는 0
# -out: error = 없음
#------------------------------------------------------------------
def _hwnd_of(win):
    try:
        import ctypes

        user32 = ctypes.windll.user32
        wid = win.winfo_id()
        return user32.GetParent(wid) or wid
    except Exception:
        return 0


#------------------------------------------------------------------
# 활성화되지 않는 창으로 표시해 두기 (Windows 전용)
#=> WS_EX_NOACTIVATE 는 창을 "처음 보여 주기 전"에 걸어야 효과가 있다.
#   보여 준 뒤에 걸면 이미 활성화된 뒤라 포커스를 뺏은 다음이 된다(자체 점검에서 걸렸다).
#   그래서 창을 만들 때 한 번 걸어 둔다.
#
# -in: win = tkinter Toplevel
#
# -out: True = 적용됨
# -out: error = 없음 (실패하면 보통 창으로 뜬다 — 동작 자체는 한다)
#------------------------------------------------------------------
def _mark_no_activate(win):
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = _hwnd_of(win)
        if not hwnd:
            return False
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        return True
    except Exception:
        return False


#------------------------------------------------------------------
# 활성화 없이 보여 주고, 혹시 전경을 뺏었으면 되돌리기
#=> 스타일만으로 막히지 않는 경우(창 관리자 설정 등)를 위한 이중 장치다.
#   사용자가 검색창에 계속 글을 쓸 수 있어야 하므로, 뺏었으면 곧바로 돌려준다.
#
# -in: win     = tkinter Toplevel
# -in: prev_fg = 띄우기 직전의 전경 창 HWND
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _show_without_focus(win, prev_fg):
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = _hwnd_of(win)
        if not hwnd:
            return
        SW_SHOWNOACTIVATE = 4
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        if prev_fg and user32.GetForegroundWindow() == hwnd:
            user32.SetForegroundWindow(prev_fg)
    except Exception:
        pass


#------------------------------------------------------------------
# 지금 전경 창 얻기
#=> 창을 띄우기 직전에 기억해 두었다가, 뺏었으면 돌려주는 데 쓴다.
#
# -in: 없음
#
# -out: HWND 정수(실패하면 0)
# -out: error = 없음
#------------------------------------------------------------------
def _foreground():
    try:
        import ctypes

        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return 0


#------------------------------------------------------------------
# 답변 창
#=> 창 하나를 만들어 두고 질문마다 내용을 갈아 끼운다(매번 새로 만들지 않는다).
#
# -필드: visible  = 지금 떠 있는지
# -필드: question = 지금 보여 주는 질문
#------------------------------------------------------------------
class AnswerWindow:
    #--------------------------------------------------------------
    # 생성자
    #=> 실제 위젯은 처음 show() 할 때 만든다. 시작만 하고 질문이 없으면 창을 만들 이유가 없다.
    #
    # -in: root     = 숨겨 둔 tk.Tk (메인 스레드)
    # -in: settings = Settings (창 크기·글자 크기)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, settings):
        self.root = root
        self.s = settings
        self.log = rsb_log.get("window")
        self.win = None
        self.visible = False
        self.question = ""
        self._answer_chars = 0
        self._tick_job = None
        self._t0 = None
        self._status = "준비 중"
        self._docs = []
        # §16 근거 목록 — app 이 채워 넣는다. 눌렀을 때 부를 함수 fn()
        self.on_show_evidence = None
        self.on_restore = None
        # §17 따라다니는 패널 — 붙어 다닐 탐색기 창과 마지막으로 맞춘 자리
        self.target_hwnd = None
        self._last_rect = None
        # 오류를 보여 준 뒤에는 상태 글을 덮어쓰지 않는다(아래 set_status 설명 참고)
        self._errored = False

    #--------------------------------------------------------------
    # 위젯 만들기 (한 번만)
    #=> 제목 · 상태 · 근거 · 답변 · 아래쪽 단추로 나눈다.
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
        # 제목 표시줄 아이콘 — 없으면 tkinter 기본(깃털) 이 나온다
        ico = rsb_settings.icon_path()
        if ico:
            try:
                win.iconbitmap(ico)
            except Exception:
                pass
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())

        base = tkfont.Font(family="Malgun Gothic", size=s.font_size)
        bold = tkfont.Font(family="Malgun Gothic", size=s.font_size, weight="bold")
        small = tkfont.Font(family="Malgun Gothic", size=max(7, s.font_size - 2))

        outer = tk.Frame(win, bg="white", padx=12, pady=10)
        outer.pack(fill="both", expand=True)

        self.lbl_question = tk.Label(outer, text="", font=bold, bg="white", anchor="w",
                                     justify="left", wraplength=s.win_width - 40)
        self.lbl_question.pack(fill="x")

        self.lbl_status = tk.Label(outer, text="", font=small, fg="#647083", bg="white", anchor="w")
        self.lbl_status.pack(fill="x", pady=(2, 6))

        # 근거와 답변은 스크롤되는 칸 안에 넣는다.
        # 떠다니는 창에서는 MaxHeight 를, 붙어 있는 패널에서는 탐색기 창 높이를 넘길 수 없는데,
        # 그때 내용이 길면 그냥 잘려서 읽을 방법이 없었다(도킹 확인에서 답변이 통째로 잘렸다).
        # 캔버스 안에 담아 두면 넘칠 때 굴려서 볼 수 있다.
        mid = tk.Frame(outer, bg="white")
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, bg="white", highlightthickness=0, bd=0, height=1)
        self.vbar = tk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.body = tk.Frame(self.canvas, bg="white")
        self.canvas.create_window((0, 0), window=self.body, anchor="nw", tags="body")
        # 내용이 바뀌면 굴릴 범위를 다시 잡고, 칸 너비에 맞춰 내용 너비를 맞춘다
        self.body.bind("<Configure>", lambda e: self._sync_scroll())
        # 칸 크기가 바뀌면(도킹 중 탐색기 창 크기 변경 포함) 내용 너비와 굴릴 범위를 다시 잡는다.
        # 여기서 _sync_scroll 을 부르지 않으면 패널이 작아져도 스크롤 막대가 안 나온다(실제로 겪었다).
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        self.frm_evidence = tk.Frame(self.body, bg="white")
        self.frm_evidence.pack(fill="x")

        self.lbl_answer_head = tk.Label(
            self.body, text="AI 요약 — 위 근거로 확인하세요", font=small, fg="#a8701a",
            bg="white", anchor="w")

        self.txt_answer = tk.Text(self.body, height=6, font=base, wrap="word", bg="white",
                                  relief="flat", highlightthickness=0, bd=0)
        self.txt_answer.configure(state="disabled")

        # 창이 포커스를 갖지 않으므로, 마우스가 올라간 것만으로 굴러가야 한다
        for w in (self.canvas, self.body, self.frm_evidence, self.txt_answer):
            w.bind("<MouseWheel>", self._on_wheel)

        foot = tk.Frame(outer, bg="white")
        foot.pack(fill="x", side="bottom", pady=(8, 0))
        self.lbl_timing = tk.Label(foot, text="", font=small, fg="#647083", bg="white")
        self.lbl_timing.pack(side="left")
        tk.Button(foot, text="닫기", command=self.close, relief="groove",
                  font=small, takefocus=0).pack(side="right", padx=(6, 0))
        tk.Button(foot, text="복사", command=self._copy, relief="groove",
                  font=small, takefocus=0).pack(side="right")
        # §16 — 근거 파일들을 질문을 친 그 탭의 검색 결과로 띄운다.
        # 자동으로 하지 않는 이유: 사용자가 자기 검색 결과를 보고 있을 수 있어서다.
        self.btn_restore = tk.Button(foot, text="원래대로", command=self._restore,
                                     relief="groove", font=small, takefocus=0,
                                     state="disabled")
        self.btn_restore.pack(side="right", padx=(6, 6))
        self.btn_files = tk.Button(foot, text="근거 파일 보기", command=self._show_files,
                                   relief="groove", font=small, takefocus=0,
                                   state="disabled")
        self.btn_files.pack(side="right")

        # 창을 처음 보여 주기 전에 "활성화 안 함" 을 걸어 둔다 (보여 준 뒤에 걸면 늦다)
        win.update_idletasks()
        _mark_no_activate(win)
        self.win = win

    #--------------------------------------------------------------
    # 창 띄우기 (질문 1건 시작)
    #=> 이전 내용을 지우고 검색창 옆에 놓는다. 포커스는 뺏지 않는다.
    #
    # -in: question = 보여 줄 질문(접두어를 뗀 것)
    # -in: anchor   = 검색창 화면 좌표 (left, top, right, bottom). 없으면 오른쪽 위
    # -in: status   = 처음 보여 줄 상태 글
    # -in: target   = 질문이 나온 탐색기 창(§17 에서 따라다닐 대상). 없으면 안 따라간다
    #
    # -out: 없음
    # -out: error = 없음 (창 만들기 실패는 로그만 남긴다)
    #--------------------------------------------------------------
    def show(self, question, anchor=None, status="검색 중", target=None):
        try:
            self._build()
            self.question = question
            self._answer_chars = 0
            self._docs = []
            self._errored = False          # 새 질문이니 지난 오류는 잊는다
            self._status = status
            self.target_hwnd = target
            self._last_rect = None
            # 근거가 오기 전에는 보여 줄 파일이 없다
            self.btn_files.config(state="disabled")
            self.btn_restore.config(state="disabled")

            self.lbl_question.config(text=question)
            self.lbl_timing.config(text="")
            for child in self.frm_evidence.winfo_children():
                child.destroy()
            self.lbl_answer_head.pack_forget()
            self.txt_answer.pack_forget()
            self.txt_answer.configure(state="normal")
            self.txt_answer.delete("1.0", "end")
            self.txt_answer.configure(state="disabled")

            if self._docking():
                # 탐색기 창에 붙는다. 높이는 창에 맞추므로 여기서 정하지 않는다(§17)
                self.win.attributes("-topmost", False)
                self.win.geometry("{}x{}".format(self.s.win_width, 400))
            else:
                self.win.attributes("-topmost", True)
                w, h = self.s.win_width, 180
                x, y = place_near(anchor, w, h,
                                  (self.win.winfo_screenwidth(), self.win.winfo_screenheight()))
                self.win.geometry("{}x{}+{}+{}".format(w, h, x, y))
            prev_fg = _foreground()          # 지금 사용자가 쓰던 창(보통 탐색기)
            self.win.deiconify()
            _show_without_focus(self.win, prev_fg)
            self.visible = True
            if self._docking():
                self.follow(force=True)      # 뜨자마자 제자리에 붙인다

            import time
            self._t0 = time.monotonic()
            self._start_tick()
        except Exception:
            self.log.exception("답변 창을 띄우지 못했다")

    #--------------------------------------------------------------
    # 경과 시간 표시 시작
    #=> 답변이 오기 전까지 "몇 초째"를 보여 준다. 준비 중(모델 적재)일 때 특히 필요하다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _start_tick(self):
        self._stop_tick()

        def tick():
            import time
            if not self.visible:
                return
            el = time.monotonic() - (self._t0 or time.monotonic())
            self.lbl_status.config(text="● {} · {:.1f}초".format(self._status, el))
            self._tick_job = self.root.after(200, tick)

        tick()

    #--------------------------------------------------------------
    # 경과 시간 표시 멈추기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _stop_tick(self):
        if self._tick_job:
            try:
                self.root.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

    #--------------------------------------------------------------
    # 상태 글 바꾸기
    #
    # -in: text = "모델 준비 중 (첫 질문)" 처럼 지금 하는 일
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_status(self, text):
        # 오류를 보여 준 뒤라면 덮어쓰지 않는다.
        # 워커가 죽으면 곧바로 다시 뜨면서 "모델 준비 중" 상태가 이어 오는데,
        # 그대로 두면 사용자가 정작 봐야 할 "SimpleRAG 가 종료되었습니다" 가
        # 1초도 안 되어 사라진다(오류 재현 확인에서 실제로 그랬다).
        if self._errored:
            return
        self._status = text
        if self.visible and self.win:
            self.lbl_status.config(text="● {}".format(text))

    #--------------------------------------------------------------
    # 근거 보여 주기 (답변보다 먼저 온다)
    #
    # -in: items = [{no, doc, snippet}, ...]
    # -in: ms    = 검색에 걸린 시간(ms)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def set_evidence(self, items, ms):
        if not self.visible or not self.win:
            return
        self._docs = [it.get("doc", "") for it in items]
        # tkinter 위젯은 바깥에서 들여다볼 방법이 없다(UIA 로도 안 읽힌다).
        # 무엇을 그렸는지 확인할 길은 이 로그뿐이라 DIAG 로 남긴다.
        rsb_log.diag(self.log, "근거 %d건 그림 (검색 %sms)", len(items), ms)
        if self._docs and self.on_show_evidence:
            self.btn_files.config(state="normal")
        small = tkfont.Font(family="Malgun Gothic", size=max(7, self.s.font_size - 2))
        base = tkfont.Font(family="Malgun Gothic", size=self.s.font_size)

        for child in self.frm_evidence.winfo_children():
            child.destroy()
        head = tk.Label(self.frm_evidence, text="근거 {}건 ({}ms)".format(len(items), ms),
                        font=small, fg="#647083", bg="white", anchor="w")
        head.pack(fill="x", pady=(4, 2))
        for it in items:
            tk.Label(self.frm_evidence, text="[{}] {}".format(it["no"], it["doc"]),
                     font=base, bg="white", anchor="w", justify="left",
                     wraplength=self.s.win_width - 40).pack(fill="x")
            if it.get("snippet"):
                tk.Label(self.frm_evidence, text=it["snippet"][:160], font=small,
                         fg="#647083", bg="white", anchor="w", justify="left",
                         wraplength=self.s.win_width - 48).pack(fill="x", pady=(0, 3))

        self.lbl_answer_head.pack(fill="x", pady=(8, 2))
        self.txt_answer.pack(fill="both", expand=True)
        self.set_status("답변 생성 중")
        self._fit_height()

    #--------------------------------------------------------------
    # 답변 글자 붙이기 (스트리밍)
    #
    # -in: text = 새로 온 글자 조각
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def append_token(self, text):
        if not self.visible or not self.win or not text:
            return
        first = (self._answer_chars == 0)
        self.txt_answer.configure(state="normal")
        self.txt_answer.insert("end", text)
        self.txt_answer.see("end")
        self.txt_answer.configure(state="disabled")
        self._answer_chars += len(text)
        if first:
            # 근거가 길면 정작 기다리던 답변이 접힌 아래에 깔린다(도킹 확인에서 실제로 그랬다).
            # 답변이 시작되는 순간 한 번만 그 자리로 내려 준다. 근거는 바로 위에 그대로 있다.
            self._scroll_to_answer()

    #--------------------------------------------------------------
    # 답변 마무리
    #=> 소요 시간을 아래에 적고 경과 시간 표시를 멈춘다.
    #
    # -in: info = chat_parser 의 done 정보
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def finish(self, info):
        if not self.visible or not self.win:
            return
        self._stop_tick()
        bits = []
        if info.get("search_ms") is not None:
            bits.append("검색 {}ms".format(info["search_ms"]))
        if info.get("ttft_s") is not None:
            bits.append("첫 글자 {:.2f}s".format(info["ttft_s"]))
        if info.get("total_s") is not None:
            bits.append("완료 {:.2f}s".format(info["total_s"]))
        self.lbl_timing.config(text=" · ".join(bits))
        self.lbl_status.config(text="● 완료")
        rsb_log.diag(self.log, "답변 완료: 근거 %d건 · 답변 %d자 · %s",
                     len(self._docs), self._answer_chars, " · ".join(bits) or "-")
        for warn in info.get("warnings", []):
            self.append_token("\n" + warn)
        self._fit_height()

    #--------------------------------------------------------------
    # 해석하지 못한 출력 그대로 보여 주기
    #=> 구분선 문구가 바뀌어 해석이 안 될 때, 빈 창을 보여 주는 대신 원문을 띄운다(설계서 §6).
    #
    # -in: text = 워커가 낸 원문
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def show_raw(self, text):
        if not self.visible or not self.win:
            return
        self.lbl_answer_head.pack(fill="x", pady=(8, 2))
        self.txt_answer.pack(fill="both", expand=True)
        self.append_token(text)
        self._fit_height()

    #--------------------------------------------------------------
    # 오류 보여 주기
    #
    # -in: msg = 사용자에게 보여 줄 한 줄
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def show_error(self, msg):
        if not self.visible or not self.win:
            return
        self._stop_tick()
        self.lbl_status.config(text="● " + msg)
        # 이 뒤에 오는 상태 갱신이 이 문구를 덮지 않게 한다(set_status 참고)
        self._errored = True
        self.log.warning("창에 오류 표시: %s", msg)

    #--------------------------------------------------------------
    # 지금 도킹 모드인가 (§17)
    #=> 설정이 right 이고, 따라다닐 탐색기 창을 알고 있을 때만 붙는다.
    #
    # -in: 없음
    #
    # -out: bool
    # -out: error = 없음
    #--------------------------------------------------------------
    def _docking(self):
        return self.s.dock == "right" and bool(self.target_hwnd)

    #--------------------------------------------------------------
    # 탐색기 창을 따라가기 (§17) — app 이 50ms 마다 불러 준다
    #=> 창을 끌거나 크기를 바꾸거나 최대화·스냅하면 그에 맞춰 붙는다.
    #    1) 대상 창이 사라졌으면 패널도 닫는다
    #    2) 최소화됐으면 숨긴다. 복원되면 다시 보인다
    #    3) 자리가 바뀌었을 때만 옮긴다 — 매번 옮기면 깜빡이고 CPU 만 먹는다
    #
    #   창을 옮기는 일은 tkinter 스레드(메인)에서만 해야 하므로, 감시 스레드가 아니라
    #   app 의 큐 처리 주기에 얹었다. 그 주기가 곧 따라오는 속도(50ms)다.
    #
    # -in: force = True 면 자리가 같아도 한 번 맞춘다(띄운 직후)
    #
    # -out: 없음
    # -out: error = 없음 (실패는 로그만 — 따라가지 못해도 창은 살아 있어야 한다)
    #--------------------------------------------------------------
    def follow(self, force=False):
        if not self.visible or not self.win or not self._docking():
            return
        try:
            alive, minimized, rect = rsb_dock.target_state(self.target_hwnd)
            if not alive:
                # 탐색기 창이 닫혔다 — 붙어 있을 자리가 없어졌다
                self.close()
                return
            if minimized:
                if self.win.winfo_viewable():
                    self.win.withdraw()      # visible 은 그대로 둔다(복원되면 다시 보여 준다)
                return
            if not self.win.winfo_viewable():
                prev_fg = _foreground()
                self.win.deiconify()
                _show_without_focus(self.win, prev_fg)

            target = rsb_dock.panel_rect(rect, rsb_dock.work_area(self.target_hwnd),
                                         self.s.win_width)
            if force or target != self._last_rect:
                self._last_rect = target
                hwnd = _hwnd_of(self.win)
                if hwnd:
                    rsb_dock.place_above_target(hwnd, self.target_hwnd, target)
                    self._sync_scroll()      # 높이가 바뀌었으니 굴릴 범위도 다시
        except Exception:
            self.log.exception("패널이 탐색기를 따라가지 못했다")

    #--------------------------------------------------------------
    # 짧은 알림 한 줄 (§16 단추 결과)    #--------------------------------------------------------------
    # 짧은 알림 한 줄 (§16 단추 결과)
    #=> 오류 표시(show_error)와 달리 "이번 일만" 알리는 것이라 _errored 를 세우지 않는다.
    #   예: 창을 최소화해 둔 채 "근거 파일 보기" 를 누른 경우.
    #
    # -in: msg = 보여 줄 한 줄
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def notice(self, msg):
        if not self.visible or not self.win:
            return
        self.lbl_status.config(text="● " + msg)

    #--------------------------------------------------------------
    # "근거 파일 보기" 눌렀을 때 (§16)
    #=> 실제 조작은 감시 스레드가 한다(UIA 요소가 그 스레드 것이라서).
    #   여기서는 부탁만 하고, 되돌리기 단추를 열어 둔다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (콜백 예외는 로그만 — 창이 죽으면 안 된다)
    #--------------------------------------------------------------
    def _show_files(self):
        if not self.on_show_evidence:
            return
        try:
            self.on_show_evidence()
            self.btn_restore.config(state="normal")
        except Exception:
            self.log.exception("근거 파일 보기에서 예외")

    #--------------------------------------------------------------
    # "원래대로" 눌렀을 때 (§16)
    #=> 질문을 쳤을 때의 검색어로 탐색기를 되돌린다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _restore(self):
        if not self.on_restore:
            return
        try:
            self.on_restore()
        except Exception:
            self.log.exception("원래대로에서 예외")

    #--------------------------------------------------------------
    # 내용에 맞춰 창 높이 맞추기
    #=> 설정한 최대 높이를 넘지 않게 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _fit_height(self):
        # 도킹 중에는 높이를 탐색기 창에 맞춘다 — 내용에 따라 늘이지 않고 굴려서 본다(§17)
        if self._docking():
            self._sync_scroll()
            return
        try:
            self.win.update_idletasks()
            need_body = self.body.winfo_reqheight()
            # 캔버스는 스스로 크기를 주장하지 않으므로 내용 높이를 직접 준다
            self.canvas.configure(height=max(1, need_body))
            self.win.update_idletasks()
            need = self.win.winfo_reqheight()
            h = max(150, min(need, self.s.win_max_height))
            if need > h:
                # 최대 높이에 걸렸다 — 캔버스를 그만큼 줄이고 나머지는 굴려서 본다
                self.canvas.configure(height=max(60, need_body - (need - h)))
            geo = self.win.geometry().split("+")
            self.win.geometry("{}x{}+{}+{}".format(self.s.win_width, h, geo[1], geo[2]))
            self._sync_scroll()
        except Exception:
            pass

    #--------------------------------------------------------------
    # 굴릴 범위 맞추기
    #=> 내용이 칸보다 길 때만 스크롤 막대를 보여 준다. 짧으면 막대가 없어야 깔끔하다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _sync_scroll(self):
        if not self.win:
            return
        try:
            # 창 크기 변경이 아직 반영되지 않았을 수 있다 — 재어 보기 전에 한 번 정리한다
            self.win.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            need = self.body.winfo_reqheight()
            room = self.canvas.winfo_height()
            if need > room + 2:
                if not self.vbar.winfo_ismapped():
                    self.vbar.pack(side="right", fill="y")
            else:
                if self.vbar.winfo_ismapped():
                    self.vbar.pack_forget()
                self.canvas.yview_moveto(0)
        except Exception:
            pass

    #--------------------------------------------------------------
    # 답변이 보이는 자리로 한 번 내리기
    #=> 내용이 칸보다 길 때만 움직인다. 다 보이면 그대로 둔다(근거가 먼저라는 §7 원칙 유지).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _scroll_to_answer(self):
        try:
            self.win.update_idletasks()
            total = self.body.winfo_reqheight()
            if total <= self.canvas.winfo_height() + 2:
                return                       # 다 보인다 — 굳이 움직이지 않는다
            y = self.lbl_answer_head.winfo_y()
            if total > 0 and y > 0:
                self.canvas.yview_moveto(max(0.0, min(1.0, float(y) / total)))
        except Exception:
            pass

    #--------------------------------------------------------------
    # 칸 크기가 바뀌었을 때
    #=> 내용 너비를 칸에 맞추고, 굴릴 범위·막대를 다시 잡는다.
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
    # 마우스 휠로 굴리기
    #=> 이 창은 포커스를 갖지 않으므로, 마우스가 올라간 것만으로 굴러가야 한다.
    #   (Windows 10 이후 기본값인 "비활성 창 스크롤" 설정이 이 이벤트를 보내 준다.)
    #
    # -in: event = 휠 이벤트
    #
    # -out: "break" (부모로 전달하지 않는다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _on_wheel(self, event):
        try:
            if self.body.winfo_reqheight() > self.canvas.winfo_height():
                self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        except Exception:
            pass
        return "break"

    #--------------------------------------------------------------
    # 복사 단추
    #=> 질문 · 답변 · 근거 문서명을 클립보드에 넣는다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _copy(self):
        try:
            answer = self.txt_answer.get("1.0", "end").strip()
            lines = ["[질문] " + self.question, "", answer]
            if self._docs:
                lines += ["", "[근거]"] + ["- " + d for d in self._docs]
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
        except Exception:
            self.log.exception("복사 실패")

    #--------------------------------------------------------------
    # 창 닫기
    #=> 화면만 닫는다. 워커는 그대로 두고 남은 출력은 버린다(설계서 §6).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def close(self):
        self._stop_tick()
        self.visible = False
        if self.win:
            try:
                self.win.withdraw()
            except Exception:
                pass
