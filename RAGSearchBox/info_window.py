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
    up = "모델이 올라가 있지 않음"
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
        self._tabs = None
        self._syn_job_seen = None  # 안내 줄에 이미 보인 후보 만들기 소식 번호
        self._fonts = None
        self._btn_unload = None
        self._btn_reload = None
        self._autorun_var = None
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
    #=> 왼쪽에 이름, 오른쪽에 값을 두는 두 칸 표.
    #    1) 모델 정보 네 줄과 [모델 내리기]·[모델 다시 올리기]
    #    2) 인덱싱 폴더 목록과 [추가…]·[빼기]·[다시 확인]
    #    3) 로그인 시 자동 시작(체크), 로그 폴더 줄과 [열기]
    #    4) 맨 아래 [화면 열기] · [닫기]
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
        win.title("RAGSearchBox 설정")
        win.resizable(False, False)
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

        # 탭 두 개 — 기본(모델·폴더·자동 시작·로그) · 동의어(후보 승인·직접 추가·승인한 목록)
        self._tabs = ttk.Notebook(win)
        self._tabs.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        body = tk.Frame(self._tabs, bg="white", padx=14, pady=12)
        syn_tab = tk.Frame(self._tabs, bg="white", padx=14, pady=12)
        self._tabs.add(body, text="기본")
        self._tabs.add(syn_tab, text="동의어")

        # 이름 칸을 먼저 만들어 두고 값만 갱신한다(줄 수는 info_rows 가 정한다)
        rows = info_rows({})
        for r, (name, _value) in enumerate(rows):
            tk.Label(body, text=name, font=f_bold, bg="white", fg="#475063",
                     anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=3)
            lbl = tk.Label(body, text="", font=f_base, bg="white", anchor="w",
                           justify="left", wraplength=WRAP_PX)
            lbl.grid(row=r, column=1, columnspan=2, sticky="w", pady=3)
            self._values[name] = lbl

        # 모델 내리기/올리기 — 예전 트레이 메뉴. 지금 상태에 맞는 쪽만 누를 수 있게 한다
        r = len(rows)
        mrow = tk.Frame(body, bg="white")
        mrow.grid(row=r, column=1, columnspan=2, sticky="w", pady=(2, 3))
        self._btn_unload = tk.Button(mrow, text="모델 내리기 (인덱싱 전에)", relief="groove",
                                     font=f_base, padx=10,
                                     command=lambda: self._do("unload"))
        self._btn_unload.pack(side="left")
        self._btn_reload = tk.Button(mrow, text="모델 다시 올리기", relief="groove", font=f_base,
                                     padx=10, command=lambda: self._do("reload"))
        self._btn_reload.pack(side="left", padx=(6, 0))
        self._action_note = tk.Label(body, text="", font=f_base, bg="white", fg="#647083",
                                     anchor="w", justify="left", wraplength=WRAP_PX)
        self._action_note.grid(row=r + 1, column=1, columnspan=2, sticky="w")

        # 인덱싱 폴더 — 목록 + 오른쪽 버튼 세 개 + 아래 안내 줄
        r += 2
        tk.Label(body, text="인덱싱 폴더", font=f_bold, bg="white", fg="#475063",
                 anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=3)
        box = tk.Frame(body, bg="#d7dbe3", padx=1, pady=1)
        box.grid(row=r, column=1, sticky="we", pady=3)
        self._folder_box = tk.Listbox(box, height=FOLDER_ROWS, width=46, font=f_base,
                                      relief="flat", highlightthickness=0, bd=0,
                                      activestyle="none", exportselection=False)
        self._folder_box.pack(fill="both", expand=True)
        btns = tk.Frame(body, bg="white")
        btns.grid(row=r, column=2, sticky="ne", padx=(8, 0), pady=3)
        state = "normal" if self.set_folders else "disabled"
        tk.Button(btns, text="추가…", command=self._add_folder, relief="groove", font=f_base,
                  padx=10, state=state).pack(fill="x")
        tk.Button(btns, text="빼기", command=self._remove_folder, relief="groove", font=f_base,
                  padx=10, state=state).pack(fill="x", pady=(4, 0))
        # 예전 트레이 "전체 다시 확인" — 폴더와 함께 있어야 무엇을 확인하는지 분명하다
        tk.Button(btns, text="다시 확인", command=self._rescan, relief="groove", font=f_base,
                  padx=10, state="normal" if self.rescan else "disabled").pack(fill="x", pady=(4, 0))
        self._folder_note = tk.Label(body, text="", font=f_base, bg="white", fg="#647083",
                                     anchor="w", justify="left", wraplength=WRAP_PX)
        self._folder_note.grid(row=r + 1, column=1, columnspan=2, sticky="w")

        # 로그인 시 자동 시작 — 예전 트레이 체크 메뉴. 값은 레지스트리에서 1초마다 다시 읽는다
        r += 2
        tk.Label(body, text="자동 시작", font=f_bold, bg="white", fg="#475063",
                 anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=3)
        self._autorun_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="로그인할 때 자동으로 시작", variable=self._autorun_var,
                       command=lambda: self._do("autorun"), font=f_base, bg="white",
                       activebackground="white", anchor="w",
                       state="normal" if "autorun" in self.actions else "disabled"
                       ).grid(row=r, column=1, columnspan=2, sticky="w", pady=3)

        r += 1
        tk.Label(body, text="로그 폴더", font=f_bold, bg="white", fg="#475063",
                 anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 12), pady=3)
        self._log_label = tk.Label(body, text="", font=f_base, bg="white", anchor="w",
                                   justify="left", wraplength=WRAP_PX - 70)
        self._log_label.grid(row=r, column=1, sticky="w", pady=3)
        tk.Button(body, text="열기", command=self._open_logs, relief="groove",
                  font=f_base, padx=10).grid(row=r, column=2, sticky="e", padx=(8, 0), pady=3)

        self._build_syn(syn_tab, f_base, f_bold)

        foot = tk.Frame(win, bg="white", padx=14, pady=0)
        foot.pack(fill="x", pady=(10, 0))
        tk.Button(foot, text="닫기", command=self.hide, relief="groove",
                  font=f_base, padx=14).pack(side="right", pady=(0, 12))
        # 화면 열기(트레이에도 있다) — 닫아 둔 폴더 패널을 다시 띄운다
        tk.Button(foot, text="화면 열기", command=lambda: self._do("open"), relief="groove",
                  font=f_base, padx=14,
                  state="normal" if "open" in self.actions else "disabled"
                  ).pack(side="left", pady=(0, 12))
        self.win = win

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
        tk.Label(tab, text="답을 못 찾은 질문에서 뽑은 후보예요. 맞으면 승인하세요 — 다음 질문부터 바로 적용됩니다.",
                 font=f_base, bg="white", fg="#475063", anchor="w", justify="left",
                 wraplength=WRAP_PX + 120).pack(fill="x")
        self._syn_box = tk.Frame(tab, bg="white")
        self._syn_box.pack(fill="x", pady=(6, 0))
        add = tk.Frame(tab, bg="white")
        add.pack(fill="x", pady=(8, 0))
        self._syn_word = tk.Entry(add, width=12, font=f_base, relief="solid", bd=1)
        self._syn_word.pack(side="left")
        tk.Label(add, text="→", font=f_base, bg="white").pack(side="left", padx=4)
        self._syn_term = tk.Entry(add, width=14, font=f_base, relief="solid", bd=1)
        self._syn_term.pack(side="left")
        tk.Button(add, text="직접 추가", command=self._syn_add, relief="groove", font=f_base,
                  padx=8).pack(side="left", padx=(6, 0))
        tk.Button(add, text="후보 만들기", command=self._syn_suggest, relief="groove", font=f_base,
                  padx=8, state="normal" if self.suggest else "disabled").pack(side="right")
        self._syn_note = tk.Label(tab, text="", font=f_base, bg="white", fg="#647083",
                                  anchor="w", justify="left", wraplength=WRAP_PX + 120)
        self._syn_note.pack(fill="x", pady=(4, 0))
        # 승인한 동의어 — 무엇을 넣었는지 보고 잘못 넣은 것은 뺀다
        tk.Label(tab, text="승인한 동의어", font=f_bold, bg="white", fg="#475063",
                 anchor="w").pack(fill="x", pady=(10, 2))
        lst = tk.Frame(tab, bg="white")
        lst.pack(fill="x")
        box = tk.Frame(lst, bg="#d7dbe3", padx=1, pady=1)
        box.pack(side="left", fill="x", expand=True)
        self._syn_list = tk.Listbox(box, height=4, font=f_base, relief="flat", highlightthickness=0,
                                    bd=0, activestyle="none", exportselection=False)
        self._syn_list.pack(fill="both", expand=True)
        tk.Button(lst, text="빼기", command=self._syn_remove, relief="groove", font=f_base,
                  padx=10).pack(side="left", anchor="n", padx=(8, 0))

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
            if self._action_note.cget("text") != self._action_msg:
                self._action_note.config(text=self._action_msg)
                changed = True
            # 자동 시작 체크는 밖(명령줄 --autorun)에서 바뀔 수도 있어 매번 맞춘다
            if "autorun" in info and bool(self._autorun_var.get()) != bool(info["autorun"]):
                self._autorun_var.set(bool(info["autorun"]))
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
            self._tabs.tab(1, text="동의어 ({})".format(n_left) if n_left else "동의어")
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
                           justify="left", wraplength=WRAP_PX)

            # 고른 문서 용어가 나오는 문서를 회색 줄에 보인다
            def show_why(_e=None, row=row, combo=combo, why=why):
                docs = row["docs"].get(combo.get()) or []
                text = "놓친 질문: \"{}\"".format(row["example"])
                if docs:
                    text += " · '{}' 이 나오는 문서: {}".format(combo.get(), ", ".join(docs))
                why.config(text=text)
            combo.bind("<<ComboboxSelected>>", show_why)
            show_why()
            tk.Button(top, text="✕ 아님", relief="groove", font=f_base, padx=6,
                      command=lambda w=row["word"], c=combo: self._syn_do(synonym_store.reject, w, c.get())
                      ).pack(side="right")
            tk.Button(top, text="✓ 맞음", relief="groove", font=f_base, padx=6,
                      command=lambda w=row["word"], c=combo: self._syn_do(synonym_store.approve, w, c.get())
                      ).pack(side="right", padx=(0, 4))
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
