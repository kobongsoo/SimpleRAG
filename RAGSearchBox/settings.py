#------------------------------------------------------------------
# 설정 파일 읽기 (MDriveSearchBox 의 CIniReader 자리)
#=> exe 옆 RAGSearchBox.ini 를 읽는다. 파일·섹션·키가 없으면 기본값으로 돈다
#   (원본의 "[Monitor] Mode 없으면 0" 규칙과 같다).
#   범위 밖 값은 프로그램을 멈추지 않고 기본값으로 되돌린 뒤 경고를 모아 둔다 —
#   상주 프로그램이라 시작을 못 하면 사용자가 원인을 볼 방법이 없다.
#------------------------------------------------------------------

import configparser
import io
import os
import sys

APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
INI_NAME = "RAGSearchBox.ini"


#------------------------------------------------------------------
# 설정 묶음
#=> INI 에서 읽은 값을 담는다. 읽는 쪽은 s.prefix 처럼 쓴다.
#
# -필드: simplerag_exe    = 워커로 띄울 SimpleRAG 실행 파일(빈 값이면 자동 탐색)
# -필드: no_stream        = chat 에 --no-stream 을 붙일지
# -필드: start_mode       = "boot"(시작 때 예열) | "lazy"(첫 질문 때)
# -필드: idle_unload_min  = 이 분(分) 동안 질문이 없으면 모델을 내린다 (0=계속 유지)
# -필드: start_timeout_s  = 워커 준비를 기다리는 한도(초)
# -필드: answer_timeout_s = 답변 한 건을 기다리는 한도(초)
# -필드: restart_max      = 5분 안 자동 재시작 한도
# -필드: monitor_mode     = 0=WinEvent(기본) | 1=UIA 포커스 핸들러
# -필드: poll_ms          = 감시 주기(ms)
# -필드: input_recent_ms  = "최근 입력"으로 볼 시간(ms)
# -필드: prefix           = 질문으로 볼 접두어(기본 "?")
# -필드: min_chars        = 접두어를 뗀 질문의 최소 길이
# -필드: dedup_sec        = 같은 질문을 무시할 시간(초)
# -필드: scope_folders    = 동작할 폴더 목록(비면 아무 데서도 동작하지 않는다)
# -필드: scope_recheck_min= 없는 폴더를 다시 확인할 주기(분)
# -필드: panel_enabled   = 폴더 패널(§18)을 쓸지 — 지정 폴더를 열면 오른쪽에 대화창이 뜬다
# -필드: shrink_explorer = 패널 자리를 만들려고 탐색기 창을 왼쪽으로 물릴지
# -필드: folder_poll_ms  = 지금 보고 있는 폴더를 얼마나 자주 확인할지
# -필드: searchbox_trigger = 옛 방식(검색창에 ? 입력 → 답변 창). 기본은 끔
# -필드: dock            = "off"(검색창 아래 뜨는 창) | "right"(탐색기 오른쪽에 붙어 따라다님)
# -필드: win_width / win_max_height / font_size = 답변 창 모양
# -필드: log_level        = 로그 단계
# -필드: warnings         = 잘못된 값 안내 목록(트레이·로그로 알린다)
# -필드: ini_path         = 실제로 읽은 INI 경로
#------------------------------------------------------------------
class Settings:
    #--------------------------------------------------------------
    # 기본값으로 초기화
    #=> INI 가 아예 없어도 이 값들로 동작한다(범위 폴더만 비어 있어 실행은 안 된다).
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self):
        self.simplerag_exe = ""
        self.no_stream = False
        self.start_mode = "boot"
        self.idle_unload_min = 60
        self.start_timeout_s = 180
        self.answer_timeout_s = 60
        self.restart_max = 3
        self.monitor_mode = 0
        self.poll_ms = 100
        self.input_recent_ms = 1500
        self.prefix = "?"
        self.min_chars = 2
        self.dedup_sec = 3
        self.scope_folders = []
        self.scope_recheck_min = 5
        self.panel_enabled = True
        self.shrink_explorer = True
        self.folder_poll_ms = 500
        self.searchbox_trigger = False
        self.dock = "off"
        self.win_width = 460
        self.win_max_height = 560
        self.font_size = 10
        self.log_level = "INFO"
        self.warnings = []
        self.ini_path = None


#------------------------------------------------------------------
# 정수 값 읽기 (범위 확인 포함)
#=> 비었거나 숫자가 아니거나 범위를 벗어나면 기본값을 쓰고 경고를 남긴다.
#
# -in: cp      = configparser 객체
# -in: sec     = 섹션 이름
# -in: key     = 키 이름
# -in: default = 기본값
# -in: lo      = 허용 최솟값(None 이면 제한 없음)
# -in: hi      = 허용 최댓값(None 이면 제한 없음)
# -in: warns   = 경고를 모을 목록
#
# -out: 정수
# -out: error = 없음 (잘못된 값은 기본값 + 경고)
#------------------------------------------------------------------
def _int(cp, sec, key, default, lo, hi, warns):
    raw = cp.get(sec, key, fallback="").strip()
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        warns.append("[{}] {} = {} 는 숫자가 아니라 기본값 {} 을 씁니다".format(sec, key, raw, default))
        return default
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        warns.append("[{}] {} = {} 는 허용 범위({}~{}) 밖이라 기본값 {} 을 씁니다".format(
            sec, key, v, lo, hi, default))
        return default
    return v


#------------------------------------------------------------------
# 예/아니오 값 읽기
#=> 1/true/yes/on 을 참으로 본다. 모르는 값은 기본값 + 경고.
#
# -in: cp      = configparser 객체
# -in: sec     = 섹션 이름
# -in: key     = 키 이름
# -in: default = 기본값
# -in: warns   = 경고를 모을 목록
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _bool(cp, sec, key, default, warns):
    raw = cp.get(sec, key, fallback="").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    warns.append("[{}] {} = {} 를 알 수 없어 기본값 {} 을 씁니다".format(sec, key, raw, default))
    return default


#------------------------------------------------------------------
# 설정 읽기 (핵심)
#=> exe(또는 이 소스) 옆의 RAGSearchBox.ini 를 읽어 Settings 를 만든다.
#    1) 파일이 없으면 전부 기본값 — 경고 1건만 남긴다
#    2) 값마다 범위를 확인하고, 이상한 값은 기본값으로 되돌린다
#    3) Scope Folders 는 ';' 로 가른다. 실제로 있는 폴더인지는 여기서 보지 않는다
#       (네트워크 드라이브가 늦게 붙을 수 있어 scope.py 가 주기적으로 다시 본다)
#
# -in: path = INI 경로(없으면 exe 옆 RAGSearchBox.ini)
#
# -out: Settings
# -out: error = 없음 (읽기 실패도 경고로만 남기고 기본값을 돌려준다)
#------------------------------------------------------------------
def load(path=None):
    s = Settings()
    s.ini_path = path or os.path.join(APP_DIR, INI_NAME)

    cp = configparser.ConfigParser(interpolation=None)
    if not os.path.isfile(s.ini_path):
        s.warnings.append("설정 파일이 없어 기본값으로 동작합니다: {}".format(s.ini_path))
        return s
    try:
        cp.read(s.ini_path, encoding="utf-8")
    except Exception as e:
        s.warnings.append("설정 파일을 읽지 못해 기본값으로 동작합니다: {}".format(e))
        return s

    w = s.warnings
    s.simplerag_exe = cp.get("SimpleRAG", "SimpleRagExe", fallback="").strip()
    s.no_stream = _bool(cp, "SimpleRAG", "NoStream", False, w)

    mode = cp.get("Worker", "StartMode", fallback="").strip().lower()
    if mode in ("boot", "lazy"):
        s.start_mode = mode
    elif mode:
        w.append("[Worker] StartMode = {} 를 알 수 없어 boot 를 씁니다".format(mode))
    s.idle_unload_min = _int(cp, "Worker", "IdleUnloadMin", 60, 0, 1440, w)
    s.start_timeout_s = _int(cp, "Worker", "StartTimeoutSec", 180, 10, 1800, w)
    s.answer_timeout_s = _int(cp, "Worker", "AnswerTimeoutSec", 60, 5, 600, w)
    s.restart_max = _int(cp, "Worker", "RestartMax", 3, 0, 20, w)

    s.monitor_mode = _int(cp, "Monitor", "Mode", 0, 0, 1, w)
    s.poll_ms = _int(cp, "Monitor", "PollMs", 100, 20, 1000, w)
    s.input_recent_ms = _int(cp, "Monitor", "InputRecentMs", 1500, 100, 10000, w)

    s.panel_enabled = _bool(cp, "Panel", "Enabled", True, w)
    s.shrink_explorer = _bool(cp, "Panel", "ShrinkExplorer", True, w)
    s.folder_poll_ms = _int(cp, "Panel", "FolderPollMs", 500, 100, 5000, w)

    # 옛 방식(검색창 ? 감지)은 기본으로 끈다 — 켜면 검색창 찾기까지 함께 돈다
    s.searchbox_trigger = _bool(cp, "Trigger", "SearchBox", False, w)
    raw_prefix = cp.get("Trigger", "Prefix", fallback=None)
    if raw_prefix is not None:
        s.prefix = raw_prefix.strip() or "?"
    s.min_chars = _int(cp, "Trigger", "MinChars", 2, 1, 100, w)
    s.dedup_sec = _int(cp, "Trigger", "DedupSec", 3, 0, 60, w)

    folders = cp.get("Scope", "Folders", fallback="")
    s.scope_folders = [p.strip() for p in folders.split(";") if p.strip()]
    s.scope_recheck_min = _int(cp, "Scope", "RecheckMin", 5, 1, 120, w)

    dock = cp.get("Window", "Dock", fallback="").strip().lower()
    if dock in ("off", "right"):
        s.dock = dock
    elif dock:
        w.append("[Window] Dock = {} 를 알 수 없어 off 를 씁니다".format(dock))
    s.win_width = _int(cp, "Window", "Width", 460, 300, 1200, w)
    s.win_max_height = _int(cp, "Window", "MaxHeight", 560, 200, 2000, w)
    s.font_size = _int(cp, "Window", "FontSize", 10, 7, 20, w)

    s.log_level = cp.get("Log", "Level", fallback="INFO").strip() or "INFO"
    return s


#------------------------------------------------------------------
# 아이콘 파일 찾기
#=> 트레이와 답변 창이 함께 쓴다. exe 로 묶으면 PyInstaller 가 임시 폴더에
#   풀어 놓으므로(sys._MEIPASS) 거기를 먼저 보고, 소스 실행이면 이 파일 옆을 본다.
#
# -in: 없음
#
# -out: .ico 경로 또는 None(없으면 기본 아이콘을 쓴다)
# -out: error = 없음
#------------------------------------------------------------------
def icon_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, "RAGSearchBox.ico")
    return p if os.path.isfile(p) else None


#------------------------------------------------------------------
# SimpleRAG 실행 파일 찾기 (설계서 §6)
#=> INI 지정 → RAGSearchBox.exe 옆 → ..\dist\simplerag → 개발용 소스 순서로 본다.
#   개발 중에는 파이썬으로 cli.py 를 돌리는 형태라 명령이 두 토막이 된다.
#
#   ⚠️ 워커로 쓰는 simplerag.exe 는 --python-option u 로 빌드된 것이어야 한다(설계서 D8).
#      그 옵션이 없으면 근거가 답변과 함께 늦게 도착한다. 여기서는 확인할 방법이 없어
#      배포 가이드와 설계서에만 적어 둔다.
#
# -in: s = Settings
#
# -out: (명령 목록, 설명) 예: (["D:\\...\\simplerag.exe"], "INI 지정")
# -out: error = 하나도 못 찾으면 (None, 사유 문자열)
#------------------------------------------------------------------
def find_worker_cmd(s):
    if s.simplerag_exe:
        if os.path.isfile(s.simplerag_exe):
            return [s.simplerag_exe], "INI 지정"
        return None, "INI 의 SimpleRagExe 경로에 파일이 없습니다: {}".format(s.simplerag_exe)

    near = os.path.join(APP_DIR, "simplerag.exe")
    if os.path.isfile(near):
        return [near], "RAGSearchBox 옆"

    dist = os.path.abspath(os.path.join(APP_DIR, "..", "dist", "simplerag", "simplerag.exe"))
    if os.path.isfile(dist):
        return [dist], "dist 폴더"

    # 개발용: 프로젝트 소스를 그대로 돌린다
    root = os.path.abspath(os.path.join(APP_DIR, ".."))
    py = os.path.join(root, ".venv", "Scripts", "python.exe")
    cli = os.path.join(root, "src", "simplerag", "cli.py")
    if os.path.isfile(py) and os.path.isfile(cli):
        return [py, cli], "개발용 소스(.venv)"

    return None, "SimpleRAG 실행 파일을 찾지 못했습니다 — INI 의 SimpleRagExe 를 지정하세요"


#------------------------------------------------------------------
# 창 너비를 INI 에 되쓰기 (§18 — 사용자가 패널 너비를 바꿨을 때)
#=> configparser 로 다시 쓰면 파일의 주석이 모두 날아간다. 이 INI 는 설명 주석이
#   본문만큼 중요하므로, [Window] 의 Width 줄만 찾아 값을 바꾼다.
#    1) [Window] 구역을 찾는다
#    2) 그 안의 Width 줄을 찾아 값만 갈아 끼운다(앞뒤 여백·주석은 그대로 둔다)
#    3) 구역이나 키가 없으면 만들어 붙인다
#
# -in: path  = INI 경로(없으면 아무것도 하지 않는다)
# -in: width = 저장할 너비(픽셀)
#
# -out: (True, 저장한 값) 또는 (False, 사유)
# -out: error = 없음 (쓰기 실패도 사유 문자열로 돌려준다)
#------------------------------------------------------------------
def save_window_width(path, width):
    if not path or not os.path.isfile(path):
        return False, "설정 파일이 없어 저장하지 않습니다: {}".format(path)
    try:
        width = int(width)
    except (TypeError, ValueError):
        return False, "너비가 숫자가 아닙니다"
    if not (300 <= width <= 1200):
        return False, "허용 범위(300~1200) 밖이라 저장하지 않습니다: {}".format(width)

    try:
        with io.open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as e:
        return False, "설정 파일을 읽지 못했습니다: {}".format(e)

    in_window = False
    done = False
    window_at = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            if in_window and not done:
                break                       # [Window] 가 끝났는데 Width 가 없었다
            in_window = stripped.lower() == "[window]"
            if in_window:
                window_at = i
            continue
        if not in_window or stripped.startswith(";") or stripped.startswith("#"):
            continue
        if "=" in stripped and stripped.split("=", 1)[0].strip().lower() == "width":
            key = line.split("=", 1)[0]     # 원래 들여쓰기·정렬을 그대로 쓴다
            lines[i] = "{}= {}".format(key, width)
            done = True
            break

    if not done:
        if window_at >= 0:
            lines.insert(window_at + 1, "Width            = {}".format(width))
        else:
            lines += ["", "[Window]", "Width            = {}".format(width)]

    try:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        return False, "설정 파일에 쓰지 못했습니다: {}".format(e)
    return True, width
