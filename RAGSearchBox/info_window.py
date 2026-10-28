#------------------------------------------------------------------
# 설정 창 — 트레이 "설정"
#=> 트레이 메뉴 맨 위 회색 줄에 한 줄로 몰아 넣던 모델·백엔드·리랭킹 정보를
#   칸을 나눠 보여 주는 작은 창이다. 로그 폴더 열기 버튼도 여기로 옮겼다.
#   지금 상태(준비됨·인덱싱 중)는 메뉴 맨 위 줄("RAGSearchBox — 준비됨")에 남겨 두었다.
#
#   값은 app 이 넘겨 준 함수(get_info)로 1초마다 다시 읽는다 — 창을 띄워 둔 사이에
#   리랭커 적재가 끝나거나 워커가 내려가도 바로 바뀐 값이 보인다.
#
#   인덱싱 폴더([Scope] Folders)는 여기서 [추가…] · [빼기] 로 바꾼다. 실제 반영(INI 저장·
#   자동 인덱싱 다시 꾸리기)은 app 이 넘겨 준 함수(set_folders)가 한다.
#
#   tkinter 는 메인 스레드에서만 다룬다. 트레이 스레드는 큐에 넣고 app 이 꺼내 부른다.
#------------------------------------------------------------------

import os
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont
from tkinter import ttk

import dock as rsb_dock
import log as rsb_log
import settings as rsb_settings
import synonym_store

REFRESH_MS = 1000       # 떠 있는 동안 값을 다시 읽는 주기
FOLDER_ROWS = 3         # 인덱싱 폴더 목록 칸의 높이(줄)
WRAP_PX = 420           # 긴 경로·상태 글을 접는 너비
# 설정 창 색 — 회색 바탕에 흰 판, 묶음 상자는 얇은 홈 테두리
BG = "#f3f4f6"          # 창 바탕 · 왼쪽 메뉴
LINE = "#d7dbe3"        # 판·목록 테두리
HOVER = "#e5e7eb"       # 마우스를 올린 메뉴·단추
INK = "#1f2937"         # 본문 글
SUB = "#5b6475"         # 이름·안내 글
BLUE = "#2563eb"        # 진행 글
WARN = "#b45309"        # 주의 글
NAV_BASIC = "\u2699"    # 왼쪽 메뉴 아이콘 — 톱니(기본)
NAV_SYN = "\u21c4"      # 왼쪽 메뉴 아이콘 — 맞바꿈 화살표(동의어)
LOAD_DONE_MS = 1500     # 다 올라온 뒤 100% 를 보여 주는 시간


#------------------------------------------------------------------
# 보여 줄 줄 만들기 (화면과 무관한 순수 부분 — 시험에서 쓴다)
#=> app 이 넘긴 dict 를 (이름, 값) 줄 목록으로 바꾼다. 비어 있는 값에는 안내 글을 넣는다.
#    1) 생성 모델 · 임베딩 모델 — 워커가 안 떴으면 "모델이 올라가 있지 않음"
#    2) 리랭킹 모델 — 모델 이름 뒤에 지금 상태(켬 / 끔 / 적재 중 / 적재 실패)를 붙인다
#    3) 생성 백엔드
#   인덱싱 폴더는 목록·버튼으로 따로 그리므로(folder_note) 여기에는 넣지 않는다.
#   지금 상태(준비됨·인덱싱 중)는 트레이 메뉴 맨 위 줄이 보여 주므로 여기에는 두지 않는다.
#
# -in: info = {"model", "embed_model", "rerank_model", "rerank", "gen",
#              "folders"(list), "docs"(int|None), "log_dir"}
#
# -out: [(이름, 값), ...] — 인덱싱 폴더·로그 폴더 줄은 버튼이 붙으므로 넣지 않는다
# -out: error = 없음 (없는 키는 빈 값으로 본다)
#------------------------------------------------------------------
def info_rows(info):
    # 올리는 중이면 "없음" 대신 그렇다고 적는다(진행 막대가 아래에 따로 돈다)
    up = "올리는 중…" if info.get("starting") else "모델이 올라가 있지 않음"
    # 리랭커는 "이름 · 상태" 로 — 옛 워커라 이름을 모르면 상태만, 둘 다 모르면 안내 글
    rr_name, rr_state = info.get("rerank_model") or "", info.get("rerank") or ""
    rerank_text = " · ".join(x for x in (rr_name, rr_state) if x) or up
    return [
        ("생성 모델", info.get("model") or up),
        ("임베딩 모델", info.get("embed_model") or up),
        ("리랭킹 모델", rerank_text),
        ("생성 백엔드", info.get("gen") or up),
    ]


#------------------------------------------------------------------
# 모델 올리는 중 진행 막대 아래 글 (순수 부분 — Rust info.html 의 같은 줄과 같은 모양)
#=> "모델을 올리는 중 · 42% · 35초 — <워커가 마지막으로 낸 진행 글>"
#
# -in: starting = {"sec": 경과 초, "detail": 마지막 진행 글, "pct": 진행률 1~95}
#
# -out: 안내 글
# -out: error = 없음
#------------------------------------------------------------------
def loading_note(starting):
    text = "모델을 올리는 중 · {}% · {}초".format(int(starting.get("pct") or 0), int(starting.get("sec") or 0))
    detail = (starting.get("detail") or "").strip()
    return text + (" — " + detail[:80] if detail else "")


#------------------------------------------------------------------
# 인덱싱 폴더 목록 아래 안내 줄 (순수 부분 — 시험에서 쓴다)
#=> 폴더가 없으면 추가하라고, 있으면 그 폴더들 아래 인덱싱된 문서 수를 알려 준다.
#
# -in: info = info_rows 와 같은 dict ("folders", "docs" 를 본다)
#
# -out: 안내 글
# -out: error = 없음
#------------------------------------------------------------------
def folder_note(info):
    if not info.get("folders"):
        return "인덱싱할 폴더가 없습니다 — [추가…] 로 고르세요"
    # 문서 수는 상태 파일을 읽을 수 있을 때만 보인다(못 읽으면 숫자 없이 둔다)
    if info.get("docs") is None:
        return ""
    return "인덱싱된 문서 {:,}건".format(info["docs"])


#------------------------------------------------------------------
# 동의어 칸 아래 안내 줄 (순수 부분 — 시험에서 쓴다)
#=> 놓친 질문 수 · 남은 후보 수 · 승인한 일상어 수, 그리고 마지막 동작 결과.
#
# -in: n_miss = 놓친 질문 수 / n_left = 남은 후보 수 / n_ok = 승인한 일상어 수 / msg = 마지막 결과 글
#
# -out: 안내 글
# -out: error = 없음
#------------------------------------------------------------------
def synonym_note(n_miss, n_left, n_ok, msg=""):
    if n_left:
        head = "후보 {}개 남음 · 승인한 동의어 {}개".format(n_left, n_ok)
    elif n_miss:
        head = "답을 못 찾은 질문 {}개 — [후보 만들기] 로 사전 후보를 뽑으세요 · 승인한 동의어 {}개".format(n_miss, n_ok)
    else:
        head = "답을 못 찾은 질문이 쌓이면 여기서 동의어 후보를 고를 수 있습니다 · 승인한 동의어 {}개".format(n_ok)
    return head + ("\n" + msg if msg else "")


#------------------------------------------------------------------
# 설정 창
#=> 한 번 만든 창을 숨겼다 다시 보이는 식으로 쓴다(여러 개 뜨지 않게).
#
# -필드: get_info  = 지금 값을 돌려주는 함수 fn() -> dict (info_rows 의 입력과 같은 모양)
# -필드: open_logs = [열기] 버튼을 누르면 부를 함수 fn()
# -필드: set_folders = 인덱싱 폴더를 바꿀 때 부를 함수 fn(목록) -> (ok, 안내 글). 없으면 버튼을 잠근다
# -필드: rescan      = [다시 확인] 을 누르면 부를 함수 fn() -> (ok, 안내 글). 없으면 버튼을 잠근다
# -필드: actions     = 예전 트레이 메뉴 동작 {"open"|"unload"|"reload"|"autorun": fn() -> 안내 글}
# -필드: suggest     = [후보 만들기] 를 누르면 부를 함수 fn() -> 안내 글. 없으면 버튼을 잠근다
# -필드: visible   = 지금 떠 있는가
#   동의어 칸은 get_info 의 "data_home"(워커 데이터 폴더)에서 후보·사전 파일을 직접 읽고 쓴다.
#------------------------------------------------------------------
class InfoWindow:
    #--------------------------------------------------------------
    # 생성자
    #=> 위젯은 처음 show() 할 때 만든다.
    #
    # -in: root      = 숨겨 둔 tk.Tk (메인 스레드)
    # -in: settings  = Settings (글꼴 크기)
    # -in: get_info  = 값을 돌려주는 함수
    # -in: open_logs = 로그 폴더를 여는 함수
    # -in: set_folders = 인덱싱 폴더를 바꾸는 함수 fn(목록) -> (ok, 안내 글) (기본 None)
    # -in: rescan      = 폴더를 곧바로 다시 대조하는 함수 fn() -> (ok, 안내 글) (기본 None)
    # -in: actions     = 화면 열기·모델 내리기/올리기·자동 시작 함수들 {이름: fn() -> 안내 글} (기본 None)
    # -in: suggest     = 동의어 후보를 만드는 함수 fn() -> 안내 글 (기본 None)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, root, settings, get_info, open_logs, set_folders=None, rescan=None,
                 actions=None, suggest=None):
        self.root = root
        self.s = settings
        self.get_info = get_info
        self.open_logs = open_logs
        self.set_folders = set_folders
        self.rescan = rescan
        self.actions = dict(actions or {})
        self.suggest = suggest
        self._home = None          # 워커 데이터 폴더(동의어 파일이 있는 곳)
        self._syn_sig = None       # 동의어 칸을 마지막으로 그린 파일 상태(바뀔 때만 다시 그린다)
        self._syn_box = None       # 후보 줄들을 담는 틀
        self._syn_note = None
        self._syn_msg = ""         # 마지막 동의어 동작 결과
        self._syn_word = None      # 직접 추가 — 일상어 칸
        self._syn_term = None      # 직접 추가 — 문서 용어 칸
        self._syn_counts = (0, 0, 0)   # (놓친 질문, 남은 후보, 승인한 수)
        self._syn_pairs = []       # 승인한 동의어 목록 칸에 그려 둔 짝들 [(일상어, 문서 용어)]
        self._syn_list = None
        self._pages = {}           # 왼쪽 메뉴 이름 → 판(기본·동의어)
        self._nav = {}             # 왼쪽 메뉴 이름 → 메뉴 줄
        self._nav_fonts = None
        self._page = "basic"
        self._load_hide = None     # 100% 를 잠깐 보인 뒤 진행 막대를 숨길 예약
        self._syn_job_seen = None  # 안내 줄에 이미 보인 후보 만들기 소식 번호
        self._fonts = None
        self._btn_unload = None
        self._btn_reload = None
        self._autorun_var = None
        self._load_box = None      # 모델 올리는 중 진행 막대 칸(평소 숨김)
        self._load_bar = None
        self._load_note = None
        self._loading = False      # 진행 막대를 지금 보이고 있나
        self._mt_var = None        # 이어서 묻기(멀티턴) 체크 값
        self._mt_check = None
        self._mt_note = None       # 이어서 묻기 안내·결과 줄
        self._prec_var = None      # 정밀 모드 체크 값
        self._prec_check = None
        self._prec_note = None     # 정밀 모드 주의·결과 줄
        self._action_note = None
        self._action_msg = ""      # 마지막 동작 결과(모델 단추 아래 안내 줄)
        self._folders = None       # 목록 칸에 지금 그려 둔 폴더들(바뀔 때만 다시 그린다)
        self._folder_box = None
        self._folder_note = None
        self._msg = ""             # 마지막 폴더 변경 결과(안내 줄에 덧붙인다)
        self.log = rsb_log.get("info")
        self.win = None
        self.visible = False
        self._values = {}          # 이름 → 값 Label
        self._log_label = None
        self._job = None           # 예약해 둔 다음 갱신(root.after)

    #--------------------------------------------------------------
    # 위젯 만들기 (한 번만)
    #=> 왼쪽 메뉴(기본 · 동의어) + 오른쪽 흰 판. 메뉴를 누르면 그 판이 앞으로 온다.
    #   기본 판은 묶음 상자로 나눈다.
    #    1) 모델 — 네 줄(생성·임베딩·리랭킹·백엔드), [모델 내리기]·[모델 다시 올리기], 올리는 중 진행 막대
    #    2) 인덱싱 폴더 — 폴더 목록과 [추가…]·[빼기]·[다시 확인]
    #    3) 시작 — 로그인 시 자동 시작
    #    4) 대화 — 이어서 묻기(멀티턴) 체크와 안내 글
    #    5) 정밀 모드 — 체크와 느려진다는 주의 글
    #    6) 로그 폴더 — 경로와 [열기]
    #    7) 맨 아래 [화면 열기] · [닫기]
    #   두 판을 같은 칸에 겹쳐 두므로 창 크기는 큰 쪽에 맞춰 고정된다(메뉴를 바꿔도 창이 출렁이지 않는다).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _build(self):
        if self.win:
            return
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("MoAI-S 설정")
        win.resizable(False, False)
        win.configure(bg=BG)
        ico = rsb_settings.icon_path()
        if ico:
            try:
                win.iconbitmap(ico)
            except Exception:
                pass
        # 닫기(X)는 없애지 않고 숨긴다 — 다음에 다시 열 때 그대로 쓴다
        win.protocol("WM_DELETE_WINDOW", self.hide)
        win.bind("<Escape>", lambda _e: self.hide())

        size = getattr(self.s, "font_size", 10)
        f_base = tkfont.Font(family="Malgun Gothic", size=size)
        f_bold = tkfont.Font(family="Malgun Gothic", size=size, weight="bold")
        f_nav = tkfont.Font(family="Malgun Gothic", size=size + 1)
        f_nav_on = tkfont.Font(family="Malgun Gothic", size=size + 1, weight="bold")
        self._nav_fonts = (f_nav, f_nav_on)

        main = tk.Frame(win, bg=BG)
        main.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        # 왼쪽 메뉴 — 고른 것은 흰 바탕·굵게(오른쪽 판과 이어져 보이게)
        nav = tk.Frame(main, bg=BG)
        nav.pack(side="left", fill="y")
        # 오른쪽 판 — 얇은 테두리의 흰 판 안에 두 쪽을 겹쳐 둔다
        panel = tk.Frame(main, bg="white", highlightbackground=LINE, highlightthickness=1)
        panel.pack(side="left", fill="both", expand=True)
        body = tk.Frame(panel, bg="white", padx=16, pady=10)
        syn_tab = tk.Frame(panel, bg="white", padx=16, pady=14)
        for page in (body, syn_tab):
            page.grid(row=0, column=0, sticky="nsew")
        self._pages = {"basic": body, "syn": syn_tab}
        for key, icon, text in (("basic", NAV_BASIC, "기본"), ("syn", NAV_SYN, "동의어")):
            item = tk.Label(nav, text="  {}   {}".format(icon, text), font=f_nav, bg=BG, fg=SUB,
                            anchor="w", padx=14, pady=12, width=11, cursor="hand2")
            item.pack(fill="x")
            item.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            # 마우스를 올리면 옅게 — 고른 메뉴는 흰 바탕 그대로 둔다
            item.bind("<Enter>", lambda _e, w=item, k=key: k != self._page and w.config(bg=HOVER))
            item.bind("<Leave>", lambda _e, w=item, k=key: k != self._page and w.config(bg=BG))
            self._nav[key] = item

        # ── 모델 ──
        g = self._group(body, "모델", f_bold)
        g.columnconfigure(1, weight=1)
        # 이름 칸을 먼저 만들어 두고 값만 갱신한다(줄 수는 info_rows 가 정한다)
        rows = info_rows({})
        for r, (name, _value) in enumerate(rows):
            tk.Label(g, text=name, font=f_base, bg="white", fg=SUB,
                     anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 14), pady=2)
            lbl = tk.Label(g, text="", font=f_base, bg="white", fg=INK, anchor="w",
                           justify="left", wraplength=self._px(WRAP_PX))
            lbl.grid(row=r, column=1, sticky="w", pady=2)
            self._values[name] = lbl
        # 모델 내리기/올리기 — 예전 트레이 메뉴. 지금 상태에 맞는 쪽만 누를 수 있게 한다
        r = len(rows)
        mrow = tk.Frame(g, bg="white")
        mrow.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self._btn_unload = self._button(mrow, "모델 내리기 (인덱싱 전에)", lambda: self._do("unload"), f_base)
        self._btn_unload.pack(side="left")
        self._btn_reload = self._button(mrow, "모델 다시 올리기", lambda: self._do("reload"), f_base)
        self._btn_reload.pack(side="left", padx=(8, 0))
        # 모델을 올리는 동안만 보이는 진행 막대 — 0~100% · 경과 초 · 워커의 마지막 진행 글
        self._load_box = tk.Frame(g, bg="white")
        self._load_box.grid(row=r + 1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        self._load_bar = ttk.Progressbar(self._load_box, mode="determinate", maximum=100,
                                         length=self._px(WRAP_PX + 110))
        self._load_bar.pack(anchor="w", fill="x")
        self._load_note = tk.Label(self._load_box, text="", font=f_base, bg="white", fg=BLUE,
                                   anchor="w", justify="left", wraplength=self._px(WRAP_PX + 110))
        self._load_note.pack(anchor="w")
        self._load_box.grid_remove()
        self._action_note = tk.Label(g, text="", font=f_base, bg="white", fg=SUB,
                                     anchor="w", justify="left", wraplength=self._px(WRAP_PX + 110))
        self._action_note.grid(row=r + 2, column=0, columnspan=2, sticky="w")

        # ── 인덱싱 폴더 ── 목록 + 오른쪽 버튼 세 개 + 아래 안내 줄
        g = self._group(body, "인덱싱 폴더", f_bold)
        g.columnconfigure(0, weight=1)
        box = tk.Frame(g, bg=LINE, padx=1, pady=1)
        box.grid(row=0, column=0, sticky="we")
        self._folder_box = tk.Listbox(box, height=FOLDER_ROWS, width=46, font=f_base,
                                      relief="flat", highlightthickness=0, bd=0,
                                      activestyle="none", exportselection=False)
        self._folder_box.pack(fill="both", expand=True)
        btns = tk.Frame(g, bg="white")
        btns.grid(row=0, column=1, sticky="ne", padx=(10, 0))
        state = "normal" if self.set_folders else "disabled"
        self._button(btns, "추가…", self._add_folder, f_base, state=state).pack(fill="x")
        self._button(btns, "빼기", self._remove_folder, f_base, state=state).pack(fill="x", pady=(5, 0))
        # 예전 트레이 "전체 다시 확인" — 폴더와 함께 있어야 무엇을 확인하는지 분명하다
        self._button(btns, "다시 확인", self._rescan, f_base,
                     state="normal" if self.rescan else "disabled").pack(fill="x", pady=(5, 0))
        self._folder_note = tk.Label(g, text="", font=f_base, bg="white", fg=SUB,
                                     anchor="w", justify="left", wraplength=self._px(WRAP_PX))
        self._folder_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # ── 시작 ── 로그인 시 자동 시작(예전 트레이 체크 메뉴). 값은 레지스트리에서 1초마다 다시 읽는다
        g = self._group(body, "시작", f_bold)
        self._autorun_var = tk.BooleanVar(value=False)
        tk.Checkbutton(g, text="로그인할 때 자동으로 시작", variable=self._autorun_var,
                       command=lambda: self._do("autorun"), font=f_base, bg="white", fg=INK,
                       activebackground="white", anchor="w",
                       state="normal" if "autorun" in self.actions else "disabled"
                       ).pack(anchor="w")

        # ── 대화 ── 이어서 묻기(멀티턴, 기본 켬). 무엇을 하는지 체크 아래에 늘 보인다
        g = self._group(body, "대화", f_bold)
        self._mt_var = tk.BooleanVar(value=True)
        self._mt_check = tk.Checkbutton(
            g, text="이어서 묻기 (멀티턴 — 같은 대화의 앞 질문 이어 받기 · 기본 켬)", variable=self._mt_var,
            command=self._multiturn, font=f_base, bg="white", fg=INK, activebackground="white",
            anchor="w", state="normal" if "multiturn" in self.actions else "disabled")
        self._mt_check.pack(anchor="w")
        self._mt_note = tk.Label(g, text="", font=f_base, bg="white", fg=SUB,
                                 anchor="w", justify="left", wraplength=self._px(WRAP_PX + 90))
        self._mt_note.pack(anchor="w", padx=(22, 0))

        # ── 정밀 모드 ── Qwen3-4B + 이웃 청크. 느려진다는 주의 글을 체크 아래에 늘 보인다
        g = self._group(body, "정밀 모드", f_bold)
        self._prec_var = tk.BooleanVar(value=False)
        self._prec_check = tk.Checkbutton(
            g, text="정밀 모드 켜기 (Qwen3-4B 모델 + 이웃 청크 · 기본 끔)", variable=self._prec_var,
            command=self._precision, font=f_base, bg="white", fg=INK, activebackground="white",
            anchor="w", state="normal" if "precision" in self.actions else "disabled")
        self._prec_check.pack(anchor="w")
        self._prec_note = tk.Label(g, text="", font=f_base, bg="white", fg=WARN,
                                   anchor="w", justify="left", wraplength=self._px(WRAP_PX + 90))
        self._prec_note.pack(anchor="w", padx=(22, 0))

        # ── 로그 폴더 ──
        g = self._group(body, "로그 폴더", f_bold)
        g.columnconfigure(0, weight=1)
        self._log_label = tk.Label(g, text="", font=f_base, bg="white", fg=INK, anchor="w",
                                   justify="left", wraplength=self._px(WRAP_PX))
        self._log_label.grid(row=0, column=0, sticky="w")
        self._button(g, "열기", self._open_logs, f_base).grid(row=0, column=1, sticky="e", padx=(10, 0))

        self._build_syn(syn_tab, f_base, f_bold)
        self._show_page(self._page)

        foot = tk.Frame(win, bg=BG, padx=12)
        foot.pack(fill="x", pady=12)
        self._button(foot, "닫기", self.hide, f_base, width=10).pack(side="right")
        # 화면 열기(트레이에도 있다) — 닫아 둔 폴더 패널을 다시 띄운다
        self._button(foot, "화면 열기", lambda: self._do("open"), f_base, width=10,
                     state="normal" if "open" in self.actions else "disabled").pack(side="left")
        self.win = win

    #--------------------------------------------------------------
    # 화면 배율에 맞춘 픽셀 (글 접힘 너비 등)
    #=> 글꼴은 배율(125%·150%)을 따라 커지는데 wraplength 는 픽셀이라 그대로면 글이 일찍 접힌다.
    #   96dpi 를 1 로 보고 지금 배율만큼 늘린다.
    #
    # -in: px = 96dpi 기준 픽셀
    #
    # -out: 지금 화면 픽셀
    # -out: error = 없음 (재지 못하면 그대로)
    #--------------------------------------------------------------
    def _px(self, px):
        try:
            return int(px * self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            return px

    #--------------------------------------------------------------
    # 묶음 상자 하나 (제목이 테두리에 걸린 상자)
    #
    # -in: parent = 넣을 판 / title = 묶음 제목 / font = 제목 글꼴
    #
    # -out: 안쪽 틀(여기에 줄을 넣는다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _group(self, parent, title, font):
        g = tk.LabelFrame(parent, text=" {} ".format(title), font=font, bg="white", fg=INK,
                          bd=1, relief="groove", padx=12, pady=6)
        g.pack(fill="x", pady=(4, 6))
        return g

    #--------------------------------------------------------------
    # 납작한 단추 (얇은 테두리 · 마우스를 올리면 옅은 회색)
    #
    # -in: parent, text, command, font = 단추 자리·글·누를 때 함수·글꼴
    # -in: kw = tk.Button 에 넘길 나머지(state, width 등)
    #
    # -out: tk.Button
    # -out: error = 없음
    #--------------------------------------------------------------
    def _button(self, parent, text, command, font, **kw):
        b = tk.Button(parent, text=text, command=command, font=font, bg="white", fg=INK,
                      activebackground=HOVER, relief="solid", bd=1, padx=12, pady=2,
                      highlightthickness=0, cursor="hand2", **kw)
        # 잠긴 단추는 색을 바꾸지 않는다
        b.bind("<Enter>", lambda _e: b.cget("state") == "normal" and b.config(bg=HOVER))
        b.bind("<Leave>", lambda _e: b.config(bg="white"))
        return b

    #--------------------------------------------------------------
    # 왼쪽 메뉴로 판 바꾸기
    #
    # -in: key = "basic" | "syn"
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _show_page(self, key):
        self._page = key
        self._pages[key].tkraise()
        f_nav, f_on = self._nav_fonts
        for k, item in self._nav.items():
            on = k == key
            item.config(bg="white" if on else BG, fg=INK if on else SUB, font=f_on if on else f_nav)

    #--------------------------------------------------------------
    # 동의어 탭 만들기 (동의어 설계서 §8)
    #=> 위에서부터
    #    1) 안내 글 · 후보 줄들(바뀔 때만 다시 그림 — _syn_draw)
    #    2) 직접 추가(일상어 → 문서 용어) · [후보 만들기]
    #    3) 안내 줄(놓친 질문 · 남은 후보 · 승인한 수 · 마지막 결과)
    #    4) 승인한 동의어 목록과 [빼기]
    #
    # -in: tab = 동의어 탭 틀 / f_base, f_bold = 글꼴
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _build_syn(self, tab, f_base, f_bold):
        self._fonts = (f_base, f_bold)
        # ── 후보 승인 ── 안내 · 후보 줄들 · 직접 추가 · [후보 만들기] · 안내 줄
        g = self._group(tab, "후보 승인", f_bold)
        tk.Label(g, text="답을 못 찾은 질문에서 뽑은 후보예요. 맞으면 승인하세요 — 다음 질문부터 바로 적용됩니다.",
                 font=f_base, bg="white", fg=SUB, anchor="w", justify="left",
                 wraplength=self._px(WRAP_PX + 100)).pack(fill="x")
        self._syn_box = tk.Frame(g, bg="white")
        self._syn_box.pack(fill="x", pady=(6, 0))
        self._syn_note = tk.Label(g, text="", font=f_base, bg="white", fg=SUB,
                                  anchor="w", justify="left", wraplength=self._px(WRAP_PX + 100))
        self._syn_note.pack(fill="x", pady=(4, 0))
        tools = tk.Frame(g, bg="white")
        tools.pack(fill="x", pady=(6, 2))
        self._button(tools, "후보 만들기", self._syn_suggest, f_base,
                     state="normal" if self.suggest else "disabled").pack(side="left")

        # ── 직접 추가 ── 일상어 → 문서 용어
        g = self._group(tab, "직접 추가", f_bold)
        add = tk.Frame(g, bg="white")
        add.pack(fill="x")
        tk.Label(add, text="일상어", font=f_base, bg="white", fg=SUB).pack(side="left", padx=(0, 6))
        self._syn_word = tk.Entry(add, width=12, font=f_base, relief="solid", bd=1)
        self._syn_word.pack(side="left")
        tk.Label(add, text="→  문서 용어", font=f_base, bg="white", fg=SUB).pack(side="left", padx=6)
        self._syn_term = tk.Entry(add, width=14, font=f_base, relief="solid", bd=1)
        self._syn_term.pack(side="left")
        self._button(add, "추가", self._syn_add, f_base).pack(side="left", padx=(8, 0))

        # ── 승인한 동의어 ── 무엇을 넣었는지 보고 잘못 넣은 것은 뺀다
        g = self._group(tab, "승인한 동의어", f_bold)
        lst = tk.Frame(g, bg="white")
        lst.pack(fill="x")
        box = tk.Frame(lst, bg=LINE, padx=1, pady=1)
        box.pack(side="left", fill="x", expand=True)
        self._syn_list = tk.Listbox(box, height=5, font=f_base, relief="flat", highlightthickness=0,
                                    bd=0, activestyle="none", exportselection=False)
        self._syn_list.pack(fill="both", expand=True)
        self._button(lst, "빼기", self._syn_remove, f_base).pack(side="left", anchor="n", padx=(10, 0))

    #--------------------------------------------------------------
    # 보이기 (트레이 "설정")
    #=> 이미 떠 있으면 앞으로 가져오기만 한다. 값을 곧바로 채우고 주기 갱신을 시작한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (그리기 실패는 로그만)
    #--------------------------------------------------------------
    def show(self):
        try:
            self._build()
            was_visible = self.visible
            self.visible = True
            # 이미 돌던 갱신 예약을 지우고 새로 건다 — 두 번 열어도 갱신 고리가 하나만 돈다
            self._cancel()
            self._refresh()
            if not was_visible:
                # 먼저 보여야 제목 표시줄까지 포함한 실제 크기를 잴 수 있다
                self.win.deiconify()
                self._place(initial=True)
            self.win.lift()
            # 트레이 메뉴에서 열었으니 사용자가 바로 볼 수 있게 앞에 세우고 포커스를 준다
            self.win.attributes("-topmost", True)
            self.win.after(200, lambda: self.win and self.win.attributes("-topmost", False))
            self.win.focus_force()
        except Exception:
            self.log.exception("설정 창을 띄우지 못했다")

    #--------------------------------------------------------------
    # 숨기기 (닫기·Esc)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def hide(self):
        self.visible = False
        self._cancel()
        if self.win:
            try:
                self.win.withdraw()
            except Exception:
                pass

    #--------------------------------------------------------------
    # 없애기 (프로그램 끝낼 때)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def close(self):
        self.visible = False
        self._cancel()
        if self.win:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    # ── 안에서 쓰는 것들 ──────────────────────────────

    #--------------------------------------------------------------
    # 화면 오른쪽 아래(트레이 근처)에 놓기 / 화면 밖으로 넘치면 당기기
    #=> 트레이에서 연 창이라 트레이 가까이에 띄운다. 작업 표시줄에 가리지 않도록
    #   마우스가 있는 모니터의 작업 영역(작업 표시줄을 뺀 곳)을 기준으로 삼는다.
    #    1) 제목 표시줄까지 포함한 바깥 크기를 실제 창에서 잰다(배율 150% 등에서도 맞게)
    #    2) initial 이면 작업 영역 오른쪽 아래 구석에 놓는다
    #    3) 아니면 지금 자리를 두고, 글이 길어져 창이 커졌을 때 넘친 만큼만 안으로 당긴다
    #
    # -in: initial = True 면 처음 띄울 때(구석으로 보낸다), False 면 넘침만 바로잡는다
    #
    # -out: 없음
    # -out: error = 없음 (재기 실패 시 화면 크기로 대신한다)
    #--------------------------------------------------------------
    def _place(self, initial=True):
        # 처음 띄울 때는 창이 실제로 그려져야 제목 표시줄을 포함한 크기가 맞게 잡힌다
        if initial:
            self.win.update()
        else:
            self.win.update_idletasks()
        try:
            import win32api
            import win32gui
            l, t, r, b = rsb_dock.work_area_at(win32api.GetCursorPos()) if initial \
                else rsb_dock.work_area(int(self.win.frame(), 16))
            x0, y0, x1, y1 = win32gui.GetWindowRect(int(self.win.frame(), 16))
            w, h = x1 - x0, y1 - y0
        except Exception:
            l, t = 0, 0
            r, b = self.win.winfo_screenwidth(), self.win.winfo_screenheight() - 80
            x0, y0 = self.win.winfo_rootx(), self.win.winfo_rooty()
            w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight() + 40
        if initial:
            x, y = r - w - 16, b - h - 16
        else:
            x, y = min(x0, r - w), min(y0, b - h)
        x, y = max(l, x), max(t, y)
        # 이미 그 자리면 옮기지 않는다(1초마다 불려도 창이 떨리지 않게)
        if initial or (x, y) != (x0, y0):
            self.win.geometry("+{}+{}".format(x, y))
            # Tk 는 위치 변경을 한가할 때 적용한다 — 곧이어 앞으로 세우기 전에 지금 반영해 둔다
            self.win.update_idletasks()

    #--------------------------------------------------------------
    # 값 다시 읽어 채우기 (떠 있는 동안 1초마다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (값 읽기 실패는 로그만 — 다음 주기에 다시 해 본다)
    #--------------------------------------------------------------
    def _refresh(self):
        self._job = None
        try:
            info = self.get_info() or {}
            changed = False
            for name, value in info_rows(info):
                lbl = self._values.get(name)
                # 같은 값이면 다시 그리지 않는다(깜박임·불필요한 배치 계산을 줄인다)
                if lbl is not None and lbl.cget("text") != value:
                    lbl.config(text=value)
                    changed = True
            # 폴더 목록은 바뀌었을 때만 다시 그린다 — 매초 지우면 고른 줄이 풀린다
            folders = list(info.get("folders") or [])
            if folders != self._folders:
                self._folders = folders
                self._folder_box.delete(0, "end")
                for f in folders:
                    self._folder_box.insert("end", f)
                changed = True
            note = folder_note(info)
            if self._msg:
                note = (note + "\n" if note else "") + self._msg
            if self._folder_note.cget("text") != note:
                self._folder_note.config(text=note)
                changed = True
            # 동의어 칸 — 후보·사전 파일이 바뀌었을 때만 줄을 다시 그린다
            self._home = info.get("data_home")
            # 후보 만들기 진행 소식은 새 소식(번호가 바뀜)일 때만 안내 줄에 — 사용자 동작 결과를 매초 덮지 않게
            job = info.get("syn_job")
            if job and job[0] != self._syn_job_seen:
                self._syn_job_seen = job[0]
                self._syn_msg = job[1]
            changed = self._syn_refresh() or changed
            # 모델 단추 — 올라가 있으면 내리기만, 내려가 있으면 올리기만 누를 수 있다
            up = info.get("model_up")
            for btn, name, want in ((self._btn_unload, "unload", True),
                                    (self._btn_reload, "reload", False)):
                st = "normal" if (name in self.actions and up is not None and up == want) else "disabled"
                if btn.cget("state") != st:
                    btn.config(state=st)
            changed = self._loading_refresh(info.get("starting")) or changed
            if self._action_note.cget("text") != self._action_msg:
                self._action_note.config(text=self._action_msg)
                changed = True
            # 자동 시작 체크는 밖(명령줄 --autorun)에서 바뀔 수도 있어 매번 맞춘다
            if "autorun" in info and bool(self._autorun_var.get()) != bool(info["autorun"]):
                self._autorun_var.set(bool(info["autorun"]))
            # 정밀 모드 체크도 config.yaml 에서 매번 맞춘다 — 파일을 직접 고쳐도 따라간다
            changed = self._precision_refresh(info) or changed
            changed = self._flag_refresh(info, "multiturn", self._mt_var, self._mt_check, self._mt_note) or changed
            path = info.get("log_dir") or ""
            if self._log_label.cget("text") != path:
                self._log_label.config(text=path)
                changed = True
            # 글이 길어져 창이 커졌으면 작업 표시줄 밑으로 내려가지 않게 당긴다
            if changed and self.visible and self.win.winfo_viewable():
                self._place(initial=False)
        except Exception:
            self.log.exception("설정 창 갱신에서 예외")
        if self.visible and self.win:
            self._job = self.win.after(REFRESH_MS, self._refresh)

    #--------------------------------------------------------------
    # 예약해 둔 갱신 취소
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _cancel(self):
        if self._job and self.win:
            try:
                self.win.after_cancel(self._job)
            except Exception:
                pass
        self._job = None

    #--------------------------------------------------------------
    # 인덱싱 폴더 바꾸기 (추가·빼기가 같이 쓴다)
    #=> app 의 set_folders 에 새 목록을 넘기고, 결과 글을 안내 줄에 남긴 뒤 곧바로 다시 그린다.
    #
    # -in: folders = 바꿀 폴더 목록
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그와 안내 줄로)
    #--------------------------------------------------------------
    def _apply_folders(self, folders):
        try:
            _ok, msg = self.set_folders(folders)
        except Exception as e:
            self.log.exception("인덱싱 폴더 바꾸기에서 예외")
            msg = "바꾸지 못했습니다: {}".format(e)
        self._msg = msg or ""
        # 목록을 다시 읽어 그린다 — 실패했으면 원래 목록으로 돌아가 보인다
        self._folders = None
        self._refresh_now()

    #--------------------------------------------------------------
    # [추가…] — 폴더 고르기 창을 띄워 목록에 더한다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _add_folder(self):
        if not self.set_folders:
            return
        path = filedialog.askdirectory(parent=self.win, title="인덱싱할 폴더 고르기", mustexist=True)
        if not path:
            return                                   # 취소
        # 고르기 창은 "D:/문서" 처럼 돌려준다 — INI·인덱스와 같은 모양(D:\문서)으로 맞춘다
        path = os.path.normpath(path)
        current = list(self._folders or [])
        if any(os.path.normcase(path) == os.path.normcase(f) for f in current):
            self._msg = "이미 들어 있는 폴더입니다"
            self._refresh_now()
            return
        self._apply_folders(current + [path])

    #--------------------------------------------------------------
    # [빼기] — 고른 폴더를 목록에서 뺀다 (그 폴더 문서는 인덱스에서도 빠진다)
    #=> 되돌리기 번거로운 일이라 한 번 더 묻는다. 마지막 하나는 뺄 수 없게 app 이 막는다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _remove_folder(self):
        if not self.set_folders:
            return
        sel = self._folder_box.curselection()
        if not sel:
            self._msg = "뺄 폴더를 목록에서 고르세요"
            self._refresh_now()
            return
        current = list(self._folders or [])
        target = current[sel[0]]
        if not messagebox.askyesno(
                "인덱싱 폴더 빼기",
                "{}\n\n이 폴더를 빼면 그 안 문서가 인덱스에서 빠집니다(원본 파일은 그대로).\n"
                "계속할까요?".format(target), parent=self.win):
            return
        self._apply_folders([f for f in current if f != target])

    #--------------------------------------------------------------
    # [다시 확인] — 인덱싱 폴더를 곧바로 인덱스와 대조
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그와 안내 줄로)
    #--------------------------------------------------------------
    def _rescan(self):
        if not self.rescan:
            return
        try:
            _ok, msg = self.rescan()
        except Exception as e:
            self.log.exception("다시 확인에서 예외")
            msg = "확인하지 못했습니다: {}".format(e)
        self._msg = msg or ""
        self._refresh_now()

    #--------------------------------------------------------------
    # 예전 트레이 메뉴 동작 하나 부르기 (화면 열기·모델 내리기/올리기·자동 시작)
    #=> app 이 준 함수를 부르고, 돌려준 안내 글을 모델 단추 아래(화면 열기는 생략)에 보인다.
    #
    # -in: name = "open" | "unload" | "reload" | "autorun"
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그와 안내 줄로)
    #--------------------------------------------------------------
    def _do(self, name):
        fn = self.actions.get(name)
        if not fn:
            return
        try:
            msg = fn()
        except Exception as e:
            self.log.exception("설정 창 동작에서 예외: %s", name)
            msg = "하지 못했습니다: {}".format(e)
        if msg:
            self._action_msg = msg
        self._refresh_now()

    #--------------------------------------------------------------
    # [정밀 모드] 체크를 눌렀을 때
    #=> app 이 config.yaml 을 바꾸고 모델을 다시 올린다. 결과 글은 다음 갱신 때 주의 줄 아래에 보인다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그로, 체크는 다음 갱신 때 파일 값으로 되돌아간다)
    #--------------------------------------------------------------
    def _precision(self):
        fn = self.actions.get("precision")
        if not fn:
            return
        try:
            fn(bool(self._prec_var.get()))
        except Exception:
            self.log.exception("정밀 모드 바꾸기에서 예외")
        self._refresh_now()

    #--------------------------------------------------------------
    # 모델 올리는 중 진행 막대 보이기/숨기기
    #=> 워커가 준비 중이면 막대를 보이고 진행률(%)을 채운다.
    #   다 올라오면 100% · "준비 완료" 를 잠깐(LOAD_DONE_MS) 보인 뒤 숨긴다.
    #
    # -in: starting = loading_note 의 입력 {"sec", "detail", "pct"} 또는 None(준비 중 아님)
    #
    # -out: True = 화면이 바뀜
    # -out: error = 없음
    #--------------------------------------------------------------
    def _loading_refresh(self, starting):
        shown = self._loading
        if not starting:
            if shown:
                # 막 끝났다 — 100% 로 채워 보이고 잠시 뒤 숨긴다
                self._loading = False
                self._load_bar.config(value=100)
                self._load_note.config(text="준비 완료 · 100%")
                self._load_hide = self.win.after(LOAD_DONE_MS, self._loading_hide)
            return shown
        if self._load_hide:
            self.win.after_cancel(self._load_hide)
            self._load_hide = None
        self._load_bar.config(value=int(starting.get("pct") or 0))
        text = loading_note(starting)
        if self._load_note.cget("text") != text:
            self._load_note.config(text=text)
        if not shown:
            self._loading = True
            self._load_box.grid()
        return not shown

    #--------------------------------------------------------------
    # 진행 막대 숨기기 (100% 를 잠깐 보인 뒤)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _loading_hide(self):
        self._load_hide = None
        if not self._loading and self._load_box:
            self._load_box.grid_remove()
            if self.visible:
                self._place(initial=False)

    #--------------------------------------------------------------
    # [이어서 묻기] 체크를 눌렀을 때
    #=> app 이 config.yaml 의 generation.multiturn 을 바꾸고 모델을 다시 올린다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그로, 체크는 다음 갱신 때 파일 값으로 되돌아간다)
    #--------------------------------------------------------------
    def _multiturn(self):
        fn = self.actions.get("multiturn")
        if not fn:
            return
        try:
            fn(bool(self._mt_var.get()))
        except Exception:
            self.log.exception("이어서 묻기 바꾸기에서 예외")
        self._refresh_now()

    #--------------------------------------------------------------
    # 참/거짓 설정 체크·안내 줄 맞추기 (이어서 묻기)
    #
    # -in: info = get_info 결과 / key = "multiturn" (info[key] True|False|None, info[key + "_note"] 글)
    # -in: var, check, note = 그 체크 값·체크·안내 줄
    #
    # -out: True = 화면이 바뀜
    # -out: error = 없음
    #--------------------------------------------------------------
    def _flag_refresh(self, info, key, var, check, note):
        on = info.get(key)
        if on is not None and bool(var.get()) != on:
            var.set(on)
        # config.yaml 을 못 찾으면 눌러도 바꿀 곳이 없다
        st = "normal" if (key in self.actions and on is not None) else "disabled"
        if check.cget("state") != st:
            check.config(state=st)
        text = info.get(key + "_note") or ""
        if note.cget("text") != text:
            note.config(text=text)
            return True
        return False

    #--------------------------------------------------------------
    # 정밀 모드 체크·안내 줄 맞추기
    #
    # -in: info = get_info 결과 (precision: True|False|None, precision_note: 글)
    #
    # -out: True = 화면이 바뀜
    # -out: error = 없음
    #--------------------------------------------------------------
    def _precision_refresh(self, info):
        on = info.get("precision")
        if on is not None and bool(self._prec_var.get()) != on:
            self._prec_var.set(on)
        # config.yaml 을 못 찾으면 눌러도 바꿀 곳이 없다
        st = "normal" if ("precision" in self.actions and on is not None) else "disabled"
        if self._prec_check.cget("state") != st:
            self._prec_check.config(state=st)
        note = info.get("precision_note") or ""
        if self._prec_note.cget("text") != note:
            self._prec_note.config(text=note)
            return True
        return False

    #--------------------------------------------------------------
    # 동의어 칸 다시 그리기 (1초마다 부르지만 파일이 바뀌었을 때만 줄을 새로 만든다)
    #=> 1) 후보·사전·놓친 질문 파일의 수정 시각으로 바뀌었는지 본다
    #   2) 바뀌었으면 후보 줄(최대 5개)을 지우고 다시 만든다 — 줄마다 일상어 → [문서 용어 고르기] [맞음] [아님]
    #      아래 회색 줄에 놓친 질문 예와 그 용어가 나오는 문서
    #   3) 안내 줄(놓친 질문 수 · 남은 후보 · 승인한 수 · 마지막 결과)을 맞춘다
    #
    # -in: 없음
    #
    # -out: True = 화면이 바뀜(창 크기 다시 맞추기용)
    # -out: error = 없음 (파일 못 읽음은 빈 칸으로)
    #--------------------------------------------------------------
    def _syn_refresh(self):
        home = self._home
        if not home or self._syn_box is None:
            return False

        # 파일 수정 시각 — 없으면 None
        def mt(name):
            try:
                return os.path.getmtime(os.path.join(home, name))
            except OSError:
                return None
        sig = (home, mt(synonym_store.CANDIDATES_NAME), mt(synonym_store.USER_NAME),
               mt(synonym_store.MISSES_NAME))
        changed = False
        if sig != self._syn_sig:
            self._syn_sig = sig
            rows, n_left = synonym_store.pending(home)
            n_miss, n_ok = synonym_store.counts(home)
            self._syn_counts = (n_miss, n_left, n_ok)
            self._syn_draw(rows)
            # 승인한 목록 — 다시 그리면 고른 줄이 풀리므로 파일이 바뀐 때만
            self._syn_pairs = synonym_store.approved_pairs(home)
            self._syn_list.delete(0, "end")
            for w, t in self._syn_pairs:
                self._syn_list.insert("end", "{}  →  {}".format(w, t))
            # 탭 이름에 남은 후보 수 — 기본 탭을 보고 있어도 후보가 쌓인 걸 알 수 있게
            self._nav["syn"].config(text="  {}   동의어{}".format(NAV_SYN, " ({})".format(n_left) if n_left else ""))
            changed = True
        note = synonym_note(*self._syn_counts, msg=self._syn_msg)
        if self._syn_note.cget("text") != note:
            self._syn_note.config(text=note)
            changed = True
        return changed

    #--------------------------------------------------------------
    # 후보 줄 만들기
    #
    # -in: rows = synonym_store.pending 의 줄 목록
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _syn_draw(self, rows):
        f_base, f_bold = self._fonts
        for w in self._syn_box.winfo_children():
            w.destroy()
        for row in rows:
            fr = tk.Frame(self._syn_box, bg="white")
            fr.pack(fill="x", pady=(2, 0))
            top = tk.Frame(fr, bg="white")
            top.pack(fill="x")
            tk.Label(top, text=row["word"] + "  →", font=f_bold, bg="white").pack(side="left")
            combo = ttk.Combobox(top, values=row["terms"], state="readonly", width=14, font=f_base)
            combo.current(0)
            combo.pack(side="left", padx=(4, 0))
            why = tk.Label(fr, text="", font=f_base, bg="white", fg="#647083", anchor="w",
                           justify="left", wraplength=self._px(WRAP_PX))

            # 고른 문서 용어가 나오는 문서를 회색 줄에 보인다
            def show_why(_e=None, row=row, combo=combo, why=why):
                docs = row["docs"].get(combo.get()) or []
                text = "놓친 질문: \"{}\"".format(row["example"])
                if docs:
                    text += " · '{}' 이 나오는 문서: {}".format(combo.get(), ", ".join(docs))
                why.config(text=text)
            combo.bind("<<ComboboxSelected>>", show_why)
            show_why()
            self._button(top, "✕ 아님", lambda w=row["word"], c=combo: self._syn_do(synonym_store.reject, w, c.get()),
                         f_base).pack(side="right")
            self._button(top, "✓ 맞음", lambda w=row["word"], c=combo: self._syn_do(synonym_store.approve, w, c.get()),
                         f_base).pack(side="right", padx=(0, 4))
            why.pack(fill="x")

    #--------------------------------------------------------------
    # [맞음] · [아님] · [직접 추가] 공통 — 사전에 적고 칸을 곧바로 다시 그린다
    #
    # -in: fn = synonym_store.approve | reject / word = 일상어 / term = 문서 용어
    #
    # -out: True = 성공
    # -out: error = 없음 (실패는 안내 줄로)
    #--------------------------------------------------------------
    def _syn_do(self, fn, word, term):
        if not self._home:
            self._syn_msg = "SimpleRAG 데이터 폴더를 찾지 못했습니다"
            self._refresh_now()
            return False
        ok, msg = fn(self._home, word, term)
        self._syn_msg = msg
        # 파일이 바뀌었으니 다음 갱신에서 줄을 새로 그리게 한다
        self._syn_sig = None
        self._refresh_now()
        return ok

    #--------------------------------------------------------------
    # [직접 추가] — 두 칸에 적은 짝을 사전에 넣는다. 성공하면 칸을 비운다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _syn_add(self):
        word, term = self._syn_word.get().strip(), self._syn_term.get().strip()
        if self._syn_do(synonym_store.approve, word, term):
            self._syn_word.delete(0, "end")
            self._syn_term.delete(0, "end")

    #--------------------------------------------------------------
    # [빼기] — 승인한 목록에서 고른 짝을 사전에서 뺀다 ([아님] 과 달리 다시 제안될 수 있다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _syn_remove(self):
        sel = self._syn_list.curselection()
        if not sel:
            self._syn_msg = "뺄 동의어를 목록에서 고르세요"
            self._refresh_now()
            return
        w, t = self._syn_pairs[sel[0]]
        self._syn_do(synonym_store.remove, w, t)

    #--------------------------------------------------------------
    # [후보 만들기] — app 이 뒤에서 suggest-synonyms 를 돌리고, 끝나면 후보 파일이 바뀌어 칸이 다시 그려진다
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (app 쪽 예외는 로그와 안내 줄로)
    #--------------------------------------------------------------
    def _syn_suggest(self):
        if not self.suggest:
            return
        try:
            self._syn_msg = self.suggest() or ""
        except Exception as e:
            self.log.exception("동의어 후보 만들기에서 예외")
            self._syn_msg = "후보를 만들지 못했습니다: {}".format(e)
        self._refresh_now()

    #--------------------------------------------------------------
    # 곧바로 다시 그리기 (버튼을 누른 직후 — 1초 주기를 기다리지 않는다)
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def _refresh_now(self):
        self._cancel()
        self._refresh()

    #--------------------------------------------------------------
    # [열기] — 로그 폴더를 탐색기로
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음 (실패는 로그만)
    #--------------------------------------------------------------
    def _open_logs(self):
        try:
            self.open_logs()
        except Exception:
            self.log.exception("로그 폴더 열기에서 예외")
