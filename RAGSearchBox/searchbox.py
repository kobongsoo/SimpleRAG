#------------------------------------------------------------------
# 검색창 판정과 검색어 읽기 (설계서 §4, 원본 IsInsideFileExplorerSearchBox)
#=> "지금 포커스가 탐색기 검색창 안에 있는가"를 UI Automation 으로 가린다.
#   판정 규칙은 원본 C++ 과 같고, 호출 수단만 comtypes 로 바뀐다.
#
#   규칙 (P0-1 실측으로 확인)
#    1) 포커스 요소의 ControlType 이 Edit 또는 Button 이 아니면 검색창이 아니다
#    2) AutomationId 가 SearchEditBox / SearchBoxSearchButton 이면 검색창 (Win7/10)
#    3) 아니면 부모를 최대 4단계 올라가며 FileExplorerSearchBox 를 찾는다 (Win11)
#    4) 올라가다 CabinetWClass(탐색기 창 자체)에 닿으면 멈춘다
#
#   왜 4)가 필요한가: 주소창도 똑같은 AutoSuggestBox 이고 그 안의 Edit 도
#   ClassName 이 TextBox 라 겉모습이 같다. 주소창의 부모는 PART_AutoSuggestBox 이므로
#   FileExplorerSearchBox 를 못 찾고 창 경계까지 올라가 "아니다"로 끝난다(실측 확인).
#   이름 바꾸기 칸(UIRenameTextElement)도 같은 이유로 걸러진다.
#
#   COM 주의: UIA 객체와 여기서 얻은 요소는 만든 스레드에서만 쓴다.
#------------------------------------------------------------------

import log as rsb_log

SELF_IDS = ("SearchEditBox", "SearchBoxSearchButton")   # Win7/10 은 요소 자신이 이 이름이다
PARENT_ID = "FileExplorerSearchBox"                     # Win11 은 부모가 이 이름이다
BOUNDARY_CLASS = "CabinetWClass"                        # 여기까지 오면 검색창이 아니다
MAX_WALK = 4

UIA_EDIT = 50004
UIA_BUTTON = 50000
UIA_VALUE_PATTERN = 10002
UIA_LEGACY_PATTERN = 10018

UIA_AUTOMATION_ID_PROPERTY = 30011
UIA_CONTROL_TYPE_PROPERTY = 30003
TREE_SCOPE_DESCENDANTS = 4


#------------------------------------------------------------------
# UIA 요소 한 줄 요약
#=> 로그에 남길 때 쓴다. 속성을 읽다 죽는 요소가 있어 통째로 감싼다.
#
# -in: e = UIA 요소(없으면 None)
#
# -out: {"type":…, "cls":…, "aid":…, "name":…} 또는 {"err":…}
# -out: error = 없음
#------------------------------------------------------------------
def describe(e):
    if e is None:
        return {"err": "none"}
    try:
        return {"type": e.CurrentControlType, "cls": e.CurrentClassName,
                "aid": e.CurrentAutomationId, "name": e.CurrentName}
    except Exception as ex:
        return {"err": str(ex)}


#------------------------------------------------------------------
# 검색창 판정기
#=> UIA 객체를 한 번 만들어 두고 계속 쓴다. 감시 스레드 전용이다.
#
# -필드: ok = UIA 를 만들었는가(실패하면 감시를 중단해야 한다 — 원본과 같은 동작)
#------------------------------------------------------------------
class SearchBox:
    #--------------------------------------------------------------
    # 생성자 — UIA 객체를 만든다
    #=> 부르는 스레드가 먼저 CoInitialize 를 해 두어야 한다.
    #   실패해도 예외를 던지지 않는다. 상주 프로그램이 시작조차 못 하면 안 되기 때문에
    #   ok=False 로 두고 트레이가 사용자에게 알리게 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 실패하면 self.ok=False, self.error 에 사유
    #--------------------------------------------------------------
    def __init__(self):
        self.log = rsb_log.get("searchbox")
        self.ok = False
        self.error = None
        self.uia = None
        self.walker = None
        try:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            self._UIA = UIA
            self.uia = comtypes.client.CreateObject(UIA.CUIAutomation,
                                                    interface=UIA.IUIAutomation)
            # ControlView 로 올라간다 — Raw 는 중간 컨테이너가 많아 4단계 안에 못 닿는다
            self.walker = self.uia.ControlViewWalker
            self.ok = True
        except Exception as ex:
            self.error = "UI Automation 을 시작하지 못했습니다: {}".format(ex)
            self.log.exception("UIA 생성 실패")

    #--------------------------------------------------------------
    # 지금 포커스를 가진 요소
    #
    # -in: 없음
    #
    # -out: UIA 요소 또는 None
    # -out: error = 없음 (조회 실패는 None — 창이 막 닫히는 중이면 흔하다)
    #--------------------------------------------------------------
    def focused(self):
        if not self.ok:
            return None
        try:
            return self.uia.GetFocusedElement()
        except Exception:
            return None

    #--------------------------------------------------------------
    # 이 요소가 검색창 안인가 (핵심 판정, 원본과 같은 규칙)
    #
    # -in: e = UIA 요소
    #
    # -out: (True/False, 사유 문자열) — 사유는 DIAG 로그로 남긴다
    # -out: error = 없음 (속성 읽기 실패는 (False, 사유))
    #--------------------------------------------------------------
    def is_inside(self, e):
        if not self.ok or e is None:
            return False, "요소 없음"
        try:
            if e.CurrentControlType not in (UIA_EDIT, UIA_BUTTON):
                return False, "Edit/Button 아님"
            if e.CurrentAutomationId in SELF_IDS:
                return True, "자신이 검색창(Win7/10)"
        except Exception as ex:
            return False, "속성 읽기 실패: {}".format(ex)

        cur = e
        for level in range(MAX_WALK):
            try:
                cur = self.walker.GetParentElement(cur)
            except Exception as ex:
                return False, "부모 조회 실패: {}".format(ex)
            if cur is None:
                return False, "부모 없음({}단계)".format(level)
            info = describe(cur)
            if info.get("aid") == PARENT_ID:
                return True, "부모가 검색창({}단계)".format(level)
            # 탐색기 창 자체까지 올라왔다 = 검색창 밖이다(주소창이 여기로 온다)
            if info.get("cls") == BOUNDARY_CLASS:
                return False, "창 경계까지 올라옴"
        return False, "{}단계 안에 못 찾음".format(MAX_WALK)

    #--------------------------------------------------------------
    # 검색창 글자 읽기
    #=> ValuePattern 을 먼저 보고, 없으면 LegacyIAccessible 을 본다(원본과 같은 순서).
    #   ⚠️ 반드시 armed 때 붙잡아 둔 요소 로 불러야 한다. Enter 뒤에 "포커스 요소"를 읽으면
    #      Win11 은 이미 포커스를 파일 목록으로 옮겨 놓아 파일 이름이 읽힌다(P0-1 실측).
    #
    # -in: e = 검색창 요소(붙잡아 둔 것)
    #
    # -out: 글자 문자열 또는 None(둘 다 실패)
    # -out: error = 없음 (실패는 None + DIAG 로그 — 오작동보다 무반응이 낫다)
    #--------------------------------------------------------------
    def read_value(self, e):
        if not self.ok or e is None:
            return None
        for pid, iface_name in ((UIA_VALUE_PATTERN, "IUIAutomationValuePattern"),
                                (UIA_LEGACY_PATTERN, "IUIAutomationLegacyIAccessiblePattern")):
            try:
                p = e.GetCurrentPattern(pid)
                if not p:
                    continue
                v = p.QueryInterface(getattr(self._UIA, iface_name)).CurrentValue
                if v is not None:
                    return v
            except Exception:
                continue
        rsb_log.diag(self.log, "검색어를 읽지 못했다 (Value·Legacy 모두 실패)")
        return None

    #--------------------------------------------------------------
    # 요소의 화면 위치
    #=> 답변 창을 검색창 바로 아래에 놓기 위해 쓴다.
    #
    # -in: e = UIA 요소
    #
    # -out: (left, top, right, bottom) 또는 None
    # -out: error = 없음
    #--------------------------------------------------------------
    def rect_of(self, e):
        if not self.ok or e is None:
            return None
        try:
            r = e.CurrentBoundingRectangle
            if r.right <= r.left or r.bottom <= r.top:
                return None
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            return None

    #--------------------------------------------------------------
    # 창 번호로 검색창 입력칸 찾기 (§16 근거 목록용)
    #=> 포커스와 상관없이, 그 탐색기 창의 검색창을 직접 찾는다.
    #   armed 때 붙잡아 둔 요소는 확정하면서 놓아 주므로 여기서 다시 찾는다.
    #   창이 최소화되면 이 요소 자체가 사라진다(실측) — 그때는 None 이다.
    #
    # -in: hwnd = 탐색기 창 핸들
    #
    # -out: 입력칸 UIA 요소 또는 None
    # -out: error = 없음 (못 찾으면 None)
    #--------------------------------------------------------------
    def find_edit(self, hwnd):
        if not self.ok:
            return None
        try:
            root = self.uia.ElementFromHandle(hwnd)
            box = root.FindFirst(
                TREE_SCOPE_DESCENDANTS,
                self.uia.CreatePropertyCondition(UIA_AUTOMATION_ID_PROPERTY, PARENT_ID))
            if not box:
                return None
            return box.FindFirst(
                TREE_SCOPE_DESCENDANTS,
                self.uia.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY, UIA_EDIT))
        except Exception:
            return None

    #--------------------------------------------------------------
    # 검색창에 글자 써 넣기 (포커스를 뺏지 않는 방법으로)
    #=> ⚠️ ValuePattern.SetValue 를 쓰면 탐색기 창이 앞으로 튀어나온다(실측).
    #   답변은 질문 몇 초 뒤에 오므로 그 사이 사용자가 다른 창을 보고 있을 수 있고,
    #   그때 창을 뺏으면 §7 의 "포커스를 뺏지 않는다" 원칙이 깨진다.
    #   LegacyIAccessible 쪽 SetValue 는 같은 일을 하면서 전경을 건드리지 않는다.
    #
    # -in: e    = find_edit() 로 얻은 입력칸
    # -in: text = 써 넣을 글자
    #
    # -out: True = 썼다
    # -out: error = 없음 (실패는 False + DIAG 로그)
    #--------------------------------------------------------------
    def set_value_no_focus(self, e, text):
        if not self.ok or e is None:
            return False
        try:
            p = e.GetCurrentPattern(UIA_LEGACY_PATTERN)
            if not p:
                rsb_log.diag(self.log, "LegacyIAccessible 패턴이 없다")
                return False
            p.QueryInterface(self._UIA.IUIAutomationLegacyIAccessiblePattern).SetValue(text)
            return True
        except Exception as ex:
            rsb_log.diag(self.log, "검색창에 쓰지 못했다: %s", ex)
            return False

    #--------------------------------------------------------------
    # 이 요소가 지금 키보드 포커스를 쥐고 있는가
    #=> 글자를 치는 동안 탐색기가 제안 목록을 열면서 "포커스 요소"가 잠깐 딴 것으로
    #   바뀌는 일이 있다(실측). 그때 무장을 풀어 버리면 Enter 를 통째로 놓치므로,
    #   붙잡아 둔 검색창 요소에게 직접 "네가 아직 포커스냐"고 물어 확인한다.
    #
    # -in: e = UIA 요소
    #
    # -out: bool
    # -out: error = 없음 (조회 실패는 False)
    #--------------------------------------------------------------
    def has_focus(self, e):
        if not self.ok or e is None:
            return False
        try:
            return bool(e.CurrentHasKeyboardFocus)
        except Exception:
            return False

    #--------------------------------------------------------------
    # 이 요소가 아직 살아 있는가
    #=> 탐색기가 검색 UI 를 닫으면 요소가 사라진다. 죽은 요소를 계속 들고 있으면
    #   armed 가 풀리지 않으므로 주기적으로 확인한다.
    #
    # -in: e = UIA 요소
    #
    # -out: bool
    # -out: error = 없음
    #--------------------------------------------------------------
    def alive(self, e):
        if not self.ok or e is None:
            return False
        try:
            _ = e.CurrentControlType
            return True
        except Exception:
            return False
