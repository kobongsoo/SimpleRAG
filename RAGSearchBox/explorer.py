#------------------------------------------------------------------
# 탐색기 위치 알아내기 (설계서 §5-1 R1·R2, 원본 OnMDriveSearchBoxFocused 자리)
#=> "지금 이 탐색기 창이 어느 폴더를 보고 있는가"를 구한다. 이것이 범위 판정의 입력이다.
#
#   원본(C++)은 IShellWindows 로 창 목록을 돌며 LocationURL 을 읽고 DOS 경로로 바꿨다.
#   여기서도 똑같이 하되, Win11 에서 새로 생긴 두 가지를 더 다룬다.
#
#    1) 탭 — 한 창(HWND)에 탭이 여러 개면 IShellWindows 항목도 여러 개가 같은 HWND 를 갖는다.
#       어느 것이 지금 보고 있는 탭인지 가려야 한다. 실측 결과 창의 첫 번째
#       ShellTabWindowClass 자식이 항상 활성 탭이었다(Ctrl+Tab 으로 바꿔도 그랬다, P0-9).
#    2) 검색 결과 화면 — 검색을 한 번 실행하면 그 탭의 LocationURL 이 빈 문자열 이 되고
#       LocationName 이 검색어 자체가 된다(P0-9 실측: url="" / name="?범위").
#       즉 폴더를 직접 읽을 수 없다. 그래서 그 탭에서 마지막으로 봤던 진짜 폴더를 기억해 둔다.
#
#   가려내지 못하면 "범위 밖"으로 본다. 엉뚱한 폴더에서 답변 창이 뜨는 것보다
#   안 뜨는 편이 낫다는 것이 설계서의 보수 규칙이다.
#
#   COM 주의: Shell.Application 객체는 만든 스레드에서만 쓴다(감시 스레드 전용).
#------------------------------------------------------------------

import os
import urllib.parse

import log as rsb_log
import scope as rsb_scope

KIND_FOLDER = "folder"        # 진짜 폴더를 보고 있다
KIND_SEARCH = "search"        # 검색 결과 화면이다
KIND_VIRTUAL = "virtual"      # 내 PC · 라이브러리 같은 가상 폴더다


#------------------------------------------------------------------
# 위치 URL → DOS 경로 (원본 ConvertURLPathToDosPath)
#=> 탐색기가 주는 주소는 file:///D:/문서/인사 같은 URL 이다. 이것을 D:\문서\인사 로 바꾼다.
#    1) %ED%95% 처럼 인코딩된 한글을 되돌린다
#    2) / 를 \ 로 바꾼다
#    3) file://서버/공유 형태(UNC)는 앞에 \\ 를 붙인다
#
# -in: url = LocationURL 문자열(빈 값 가능)
#
# -out: DOS 경로 문자열. file: 주소가 아니면 None (검색 결과·가상 폴더가 여기 해당)
# -out: error = 없음 (디코딩 실패도 None)
#------------------------------------------------------------------
def url_to_path(url):
    if not url:
        return None
    low = url.lower()
    try:
        if low.startswith("file:///"):
            return urllib.parse.unquote(url[8:]).replace("/", "\\")
        if low.startswith("file://"):
            # file://서버/공유 → \\서버\공유
            return "\\\\" + urllib.parse.unquote(url[7:]).replace("/", "\\")
    except Exception:
        return None
    return None


#------------------------------------------------------------------
# 이 항목은 무엇인가 — 폴더 · 검색 결과 · 가상 폴더
#=> 셋을 가르는 이유: 폴더면 그대로 쓰고, 검색 결과면 기억해 둔 폴더를 쓰고,
#   가상 폴더면 아예 범위 밖이기 때문이다.
#   검색 결과인지는 "주소가 비었는데 이름이 지금 검색창에 적힌 글자와 같다"로 가린다(P0-9 실측).
#
# -in: entry       = {"url":…, "name":…, "path":…} 형태의 탐색기 항목
# -in: search_text = 지금 검색창에 들어 있는 글자(모르면 None)
#
# -out: KIND_FOLDER / KIND_SEARCH / KIND_VIRTUAL
# -out: error = 없음
#------------------------------------------------------------------
def classify(entry, search_text):
    if entry.get("path"):
        return KIND_FOLDER
    name = (entry.get("name") or "").strip()
    text = (search_text or "").strip()
    if text and name and name == text:
        return KIND_SEARCH
    return KIND_VIRTUAL


#------------------------------------------------------------------
# 이 항목이 활성 탭 제목과 맞는가
#=> 탭이 여러 개일 때 어느 항목이 지금 보고 있는 탭인지 가리는 비교다.
#   "전체 경로 표시"가 켜진 PC 는 탭 제목이 전체 경로이고, 꺼진 PC 는 폴더 이름뿐이라
#   둘 다 인정한다. 검색 결과 탭은 제목이 "?범위 - C의 검색 결과" 처럼
#   검색어로 시작하므로 그것으로 맞춘다.
#
# -in: entry = 탐색기 항목
# -in: kind  = classify() 결과
# -in: title = 활성 탭 제목
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def title_matches(entry, kind, title):
    title = (title or "").strip()
    if not title:
        return False
    if kind == KIND_FOLDER:
        path = entry.get("path") or ""
        # 전체 경로 표시가 켜진 경우
        if rsb_scope.norm(path) == rsb_scope.norm(title):
            return True
        # 꺼진 경우 — 폴더 이름만 제목에 나온다
        return os.path.basename(path.rstrip("\\/")).lower() == title.lower()
    name = (entry.get("name") or "").strip()
    if not name:
        return False
    if kind == KIND_SEARCH:
        return title == name or title.startswith(name + " ")
    return title == name


#------------------------------------------------------------------
# 항목 하나를 실제 폴더 경로로 바꾸기
#=> 검색 결과 화면이면 기억해 둔 폴더를 쓴다. 다만 그 기억이 정말 이 탭 것인지
#   확인해야 한다 — 탭이 여러 개면 다른 탭의 폴더를 기억하고 있을 수 있다.
#   검색 결과 탭 제목에는 원래 폴더 이름이 들어가므로("… - C의 검색 결과")
#   기억한 폴더의 이름이 제목 안에 있을 때만 인정한다. 아니면 모른다고 답한다.
#
# -in: entry      = 탐색기 항목
# -in: kind       = classify() 결과
# -in: title      = 활성 탭 제목(검증용)
# -in: remembered = 이 창에서 마지막으로 봤던 진짜 폴더(없으면 None)
#
# -out: 폴더 경로 또는 None(알 수 없음 = 범위 밖으로 처리된다)
# -out: error = 없음
#------------------------------------------------------------------
def resolve(entry, kind, title, remembered):
    if kind == KIND_FOLDER:
        return entry.get("path")
    if kind == KIND_SEARCH and remembered:
        base = os.path.basename(rsb_scope.norm(remembered))
        if base and base in (title or "").lower():
            return remembered
        return None
    return None


#------------------------------------------------------------------
# 활성 탭 가리기 (핵심 판정)
#=> 같은 창의 항목들 중 지금 보고 있는 것을 고른다.
#    1) 항목이 하나면 그것이다(탭이 하나인 흔한 경우)
#    2) 여럿이면 활성 탭 제목과 맞는 항목을 고른다
#    3) 딱 하나로 가려지지 않으면 후보를 전부 돌려준다 — 부르는 쪽이
#       "후보가 전부 범위 안일 때만 실행"이라는 보수 규칙을 적용한다
#
# -in: entries     = 같은 HWND 의 탐색기 항목 목록
# -in: titles      = 탭 제목 목록(Z순서, 첫 번째가 활성 탭)
# -in: search_text = 지금 검색창 글자
# -in: remembered  = 이 창에서 마지막으로 봤던 진짜 폴더
#
# -out: (경로 목록, 사유). 목록에 None 이 섞이면 "그 후보는 알 수 없음"이라는 뜻이다
# -out: error = 없음 (항목이 없으면 ([], 사유))
#------------------------------------------------------------------
def pick_active(entries, titles, search_text, remembered=None):
    if not entries:
        return [], "탐색기 목록에 없음"

    title = (titles[0] if titles else "") or ""

    if len(entries) == 1:
        e = entries[0]
        k = classify(e, search_text)
        return [resolve(e, k, title, remembered)], "탭 1개 ({})".format(k)

    matched = []
    for e in entries:
        k = classify(e, search_text)
        if title_matches(e, k, title):
            matched.append((e, k))

    if len(matched) == 1:
        e, k = matched[0]
        return [resolve(e, k, title, remembered)], "활성 탭 일치 ({})".format(k)

    # 못 가렸다 — 후보를 전부 넘겨 보수 규칙에 맡긴다
    pool = matched if matched else [(e, classify(e, search_text)) for e in entries]
    paths = [resolve(e, k, title, remembered) for e, k in pool]
    return paths, "활성 탭을 가리지 못함 — 후보 {}개".format(len(paths))


#------------------------------------------------------------------
# 탐색기 위치 조회기
#=> IShellWindows 와 탭 제목을 읽고, 창별로 마지막 폴더를 기억한다.
#   반드시 한 스레드(감시 스레드)에서만 쓴다 — COM 객체가 스레드에 묶이기 때문이다.
#
# -필드: last_folder = HWND 별로 마지막에 본 진짜 폴더
#------------------------------------------------------------------
class ExplorerLocator:
    #--------------------------------------------------------------
    # 생성자
    #=> COM 객체는 여기서 만들지 않는다. 쓰는 스레드가 CoInitialize 를 한 뒤
    #   처음 쓸 때 만들어야 하기 때문이다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self):
        self.log = rsb_log.get("explorer")
        self.last_folder = {}
        self._shell = None

    #--------------------------------------------------------------
    # Shell.Application 얻기 (처음 쓸 때 만든다)
    #
    # -in: 없음
    #
    # -out: COM 객체 또는 None(만들지 못함)
    # -out: error = 없음 (실패는 로그 1회)
    #--------------------------------------------------------------
    def _shell_app(self):
        if self._shell is None:
            try:
                import win32com.client
                self._shell = win32com.client.Dispatch("Shell.Application")
            except Exception:
                self.log.exception("Shell.Application 을 만들지 못했다")
                return None
        return self._shell

    #--------------------------------------------------------------
    # 이 창의 탐색기 항목들 읽기
    #=> 탭마다 항목이 하나씩 나온다. 주소를 DOS 경로로 바꿔 함께 담는다.
    #
    # -in: hwnd = 탐색기 창 핸들
    #
    # -out: [{"url":…, "name":…, "path":… 또는 None}] 목록
    # -out: error = 없음 (읽기 실패는 빈 목록)
    #--------------------------------------------------------------
    def entries(self, hwnd):
        app = self._shell_app()
        if app is None:
            return []
        out = []
        try:
            for w in app.Windows():
                try:
                    if int(w.HWND) != int(hwnd):
                        continue
                    url = str(w.LocationURL or "")
                    out.append({"url": url, "name": str(w.LocationName or ""),
                                "path": url_to_path(url)})
                except Exception:
                    # 창이 방금 닫히면 항목 접근에서 예외가 난다 — 그 항목만 건너뛴다
                    continue
        except Exception:
            self.log.exception("탐색기 창 목록을 읽지 못했다")
        return out

    #--------------------------------------------------------------
    # 탭 제목 읽기 (Z순서)
    #=> 첫 번째가 활성 탭이다(P0-9 실측). 탭이 없는 Windows 10 에서는 빈 목록이 된다.
    #
    # -in: hwnd = 탐색기 창 핸들
    #
    # -out: 제목 문자열 목록
    # -out: error = 없음 (실패는 빈 목록)
    #--------------------------------------------------------------
    def tab_titles(self, hwnd):
        try:
            import win32gui
            titles = []
            h = win32gui.FindWindowEx(hwnd, 0, "ShellTabWindowClass", None)
            while h:
                titles.append(win32gui.GetWindowText(h))
                h = win32gui.FindWindowEx(hwnd, h, "ShellTabWindowClass", None)
            return titles
        except Exception:
            return []

    #--------------------------------------------------------------
    # 지금 이 창이 보고 있는 폴더 (부르는 쪽의 유일한 입구)
    #=> 항목·탭 제목을 읽어 활성 탭을 가리고, 진짜 폴더를 봤으면 기억해 둔다.
    #   기억은 다음에 그 탭이 검색 결과 화면이 되었을 때 쓰인다.
    #
    # -in: hwnd        = 탐색기 창 핸들
    # -in: search_text = 지금 검색창 글자(검색 결과 화면을 가리는 데 쓴다)
    #
    # -out: (경로 목록, 사유). None 이 섞이면 그 후보는 알 수 없다는 뜻
    # -out: error = 없음
    #--------------------------------------------------------------
    def folder_of(self, hwnd, search_text=None):
        ents = self.entries(hwnd)
        titles = self.tab_titles(hwnd)
        paths, why = pick_active(ents, titles, search_text, self.last_folder.get(hwnd))
        # 진짜 폴더를 하나로 가려낸 경우에만 기억을 갱신한다
        if len(paths) == 1 and paths[0] and any(e.get("path") for e in ents):
            self.last_folder[hwnd] = paths[0]
        return paths, why

    #--------------------------------------------------------------
    # 닫힌 창의 기억 버리기
    #=> HWND 는 재사용되므로, 창이 사라지면 그 기억도 지워야 엉뚱한 폴더를 쓰지 않는다.
    #
    # -in: 없음
    #
    # -out: 지운 개수
    # -out: error = 없음
    #--------------------------------------------------------------
    def forget_closed(self):
        try:
            import win32gui
        except Exception:
            return 0
        dead = [h for h in self.last_folder if not win32gui.IsWindow(h)]
        for h in dead:
            self.last_folder.pop(h, None)
        return len(dead)
