#------------------------------------------------------------------
# 근거 파일 목록 (설계서 §16)
#=> 답변에 나온 근거 파일들을 "질문을 친 바로 그 탭" 에 평소 검색 결과처럼 띄운다.
#
#   왜 이렇게 하나 (실측으로 고른 방식)
#    - IShellWindows 의 Navigate2 는 주소를 줘도, PIDL 을 줘도 제자리가 아니라 새 창 을 열었다
#    - 그런데 Win11 탐색기 검색창은 값이 바뀌면 스스로 다시 검색한다. Enter 도 단추도 필요 없다
#    - 그래서 그 탭의 검색창에 "근거 파일 이름들" 을 써 넣기만 하면 목록이 바뀐다 (1.0~1.3초)
#
#   ⚠️ 쓰는 방법이 중요하다
#      ValuePattern.SetValue 는 탐색기 창을 앞으로 끌어와 포커스를 뺏는다(실측).
#      답은 질문 몇 초 뒤에 오므로 그 사이 사용자가 다른 일을 하고 있을 수 있다.
#      LegacyIAccessible 쪽 SetValue 는 전경을 건드리지 않는다 — 그쪽을 쓴다.
#
#   ⚠️ 질의 문법
#      filename:(…) · System.FileName:(…) 은 0건이었다(색인 안 된 폴더로 보인다).
#      속성 없이 이름을 따옴표로 묶어 OR 로 잇는 것만 먹었다.
#
#   COM 주의: 여기 함수들은 UIA 를 쓰는 감시 스레드에서만 부른다(monitor 가 대신 불러 준다).
#------------------------------------------------------------------

import os

import log as rsb_log

MAX_NAMES = 10          # 질의가 너무 길어지지 않게. 근거는 보통 3건이다
RESTORE_LIMIT = 20      # 되돌릴 원본 글자를 창별로 이만큼만 기억한다


#------------------------------------------------------------------
# 파일 이름 하나를 질의에 넣을 꼴로 바꾸기
#=> 큰따옴표로 감싸는데, 이름 안에 큰따옴표가 있으면 감쌀 수가 없다.
#   그럴 때는 따옴표를 뺀 조각 중 가장 긴 것을 쓴다 — 정확하진 않아도
#   "아무것도 안 나오는 것" 보다는 낫고, 어차피 폴더 안에서만 찾는다.
#
# -in: name = 파일 이름(경로여도 된다 — 이름만 쓴다)
#
# -out: 질의에 넣을 조각. 쓸 수 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def quote_name(name):
    base = os.path.basename((name or "").strip().strip('"'))
    if not base:
        return None
    if '"' in base:
        parts = [p for p in base.split('"') if p.strip()]
        if not parts:
            return None
        base = max(parts, key=len).strip()
        if not base:
            return None
    return '"{}"'.format(base)


#------------------------------------------------------------------
# 근거 이름들 → 검색창에 넣을 질의 (핵심, 순수 함수라 단독 시험 가능)
#=> 같은 문서가 여러 번 인용되는 일이 흔하므로 중복을 없앤다(순서는 유지).
#
# -in: names = 근거 문서 이름 목록
# -in: limit = 최대 몇 개까지 넣을지
#
# -out: 질의 문자열. 쓸 이름이 하나도 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def build_query(names, limit=MAX_NAMES):
    seen, out = set(), []
    for n in names or []:
        q = quote_name(n)
        if not q or q in seen:
            continue
        seen.add(q)
        out.append(q)
        if len(out) >= limit:
            break
    return " OR ".join(out) if out else None


#------------------------------------------------------------------
# 근거 목록 띄우기·되돌리기
#=> 창(HWND)별로 "원래 검색창에 있던 글자" 를 기억해 두었다가 되돌릴 때 쓴다.
#
# -필드: original = {HWND: 원래 검색어}
#------------------------------------------------------------------
class EvidenceList:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: box = searchbox.SearchBox (UIA 조회기). 감시 스레드 것을 그대로 쓴다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, box):
        self.box = box
        self.log = rsb_log.get("evidence")
        self.original = {}

    #--------------------------------------------------------------
    # 원래 검색어 기억해 두기
    #=> 질문이 확정될 때 불러 둔다. 나중에 "원래대로" 로 되돌릴 거리다.
    #
    # -in: hwnd = 탐색기 창
    # -in: text = 그때 검색창에 있던 글자
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def remember(self, hwnd, text):
        if not text:
            return
        self.original[hwnd] = text
        # 오래된 것은 버린다(창을 많이 열고 닫아도 계속 쌓이지 않게)
        if len(self.original) > RESTORE_LIMIT:
            for k in list(self.original)[:-RESTORE_LIMIT]:
                self.original.pop(k, None)

    #--------------------------------------------------------------
    # 그 탭의 검색창에 글자 써 넣기 (공통 부분)
    #=> 창이 죽었거나 최소화됐으면 검색창 요소 자체가 없다 — 그때는 조용히 포기한다.
    #   오작동보다 무반응이 낫다는 설계 원칙 그대로다.
    #
    # -in: hwnd = 탐색기 창
    # -in: text = 써 넣을 글자
    #
    # -out: (True, "") 또는 (False, 사유)
    # -out: error = 없음 (실패는 사유 문자열)
    #--------------------------------------------------------------
    def _write(self, hwnd, text):
        try:
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return False, "창이 닫혔습니다"
            if win32gui.IsIconic(hwnd):
                # 최소화되면 UIA 요소가 사라진다(실측). 복원 전에는 할 수 있는 일이 없다
                return False, "창이 최소화되어 있습니다"
        except Exception:
            pass

        edit = self.box.find_edit(hwnd)
        if edit is None:
            return False, "검색창을 찾지 못했습니다"
        if not self.box.set_value_no_focus(edit, text):
            return False, "검색창에 쓰지 못했습니다"
        return True, ""

    #--------------------------------------------------------------
    # 근거 파일 목록 띄우기
    #
    # -in: hwnd  = 탐색기 창
    # -in: names = 근거 문서 이름 목록
    #
    # -out: (True, 넣은 질의) 또는 (False, 사유)
    # -out: error = 없음
    #--------------------------------------------------------------
    def show(self, hwnd, names):
        query = build_query(names)
        if not query:
            return False, "근거 파일 이름이 없습니다"
        ok, why = self._write(hwnd, query)
        if ok:
            self.log.info("근거 목록 표시: %s", query[:80])
        else:
            rsb_log.diag(self.log, "근거 목록 표시 못 함: %s", why)
        return ok, (query if ok else why)

    #--------------------------------------------------------------
    # 원래 검색어로 되돌리기
    #
    # -in: hwnd = 탐색기 창
    #
    # -out: (True, 되돌린 글자) 또는 (False, 사유)
    # -out: error = 없음
    #--------------------------------------------------------------
    def restore(self, hwnd):
        text = self.original.get(hwnd)
        if not text:
            return False, "되돌릴 검색어를 모릅니다"
        ok, why = self._write(hwnd, text)
        if ok:
            self.log.info("검색어 되돌림: %s", text[:40])
        else:
            rsb_log.diag(self.log, "되돌리지 못함: %s", why)
        return ok, (text if ok else why)
