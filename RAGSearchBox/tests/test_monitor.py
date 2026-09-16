#------------------------------------------------------------------
# 감시 로직 시험 (설계서 §5 T2·T4, §5-1)
#=> 실제 탐색기·UIA 없이 판정 부분만 본다. UIA 와 IShellWindows 자리는 가짜로 끼운다.
#   확인하는 것
#    1) ? 선검사 — 일반 파일 검색은 Enter 를 봐도 하는 일이 없어야 한다
#    2) 확정 때 글자를 "붙잡아 둔 요소"에서 읽는가 (포커스 요소가 아니라)
#    3) 범위 밖 · 폴더 불명 · 후보 여럿이면 실행하지 않는가 (보수 규칙)
#------------------------------------------------------------------
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log as rsb_log  # noqa: E402
import monitor as rsb_monitor  # noqa: E402
import settings as rsb_settings  # noqa: E402
from scope import Scope  # noqa: E402

rsb_log.setup("INFO")


#------------------------------------------------------------------
# 가짜 검색창 판정기
#=> 붙잡아 둔 요소에서 읽는지 확인하려고, 요소마다 다른 글자를 돌려준다.
#
# -필드: values = {요소: 그 요소가 돌려줄 글자}
#------------------------------------------------------------------
class FakeBox:
    def __init__(self, values):
        self.ok = True
        self.values = values
        self.read_calls = []

    #--------------------------------------------------------------
    # 요소의 글자 읽기 (누구를 읽었는지 기록한다)
    #--------------------------------------------------------------
    def read_value(self, e):
        self.read_calls.append(e)
        return self.values.get(e)

    #--------------------------------------------------------------
    # 요소가 살아 있는가 (가짜는 항상 살아 있다)
    #--------------------------------------------------------------
    def alive(self, e):
        return e in self.values

    #--------------------------------------------------------------
    # 아직 포커스를 쥐고 있는가 (가짜는 살아 있으면 쥐고 있다고 본다)
    #--------------------------------------------------------------
    def has_focus(self, e):
        return e in self.values

    #--------------------------------------------------------------
    # 화면 위치 (가짜는 없다)
    #--------------------------------------------------------------
    def rect_of(self, e):
        return None


#------------------------------------------------------------------
# 가짜 탐색기 위치 조회기
#=> 미리 정해 둔 폴더 후보를 그대로 돌려준다.
#
# -필드: result = (경로 목록, 사유)
#------------------------------------------------------------------
class FakeLocator:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    #--------------------------------------------------------------
    # 폴더 후보 돌려주기
    #--------------------------------------------------------------
    def folder_of(self, hwnd, search_text=None):
        self.calls += 1
        return self.result

    #--------------------------------------------------------------
    # 닫힌 창 정리 (가짜는 할 일 없음)
    #--------------------------------------------------------------
    def forget_closed(self):
        return 0


#------------------------------------------------------------------
# 시험용 Monitor 만들기
#=> 스레드를 돌리지 않고 내부 함수만 부른다(실제 훅·COM 은 건드리지 않는다).
#
# -in: folders = 가짜 폴더 후보 목록
# -in: scope_roots = 지정 폴더 목록
# -in: values  = 요소별 글자
#
# -out: (monitor, 받은 질문 목록)
# -out: error = 없음
#------------------------------------------------------------------
def make_monitor(folders, scope_roots, values):
    s = rsb_settings.Settings()
    got = []
    sc = Scope(scope_roots, recheck_min=60)
    # 존재하지 않는 시험용 경로도 범위로 인정하도록 검사 결과를 직접 채운다
    sc.active = list(scope_roots)
    sc.refresh = lambda force=False, now=None: False
    m = rsb_monitor.Monitor(s, sc, lambda t, f, a, h: got.append((t, f)))
    m._box = FakeBox(values)
    m._locator = FakeLocator((folders, "가짜"))
    return m, got


class TestLooksLikeQuestion(unittest.TestCase):
    #--------------------------------------------------------------
    # ? 로 시작할 때만 Enter 를 본다
    #--------------------------------------------------------------
    def test_prefix(self):
        m, _ = make_monitor(["D:\\문서"], ["D:\\문서"], {})
        self.assertTrue(m._looks_like_question("?연차"))
        self.assertTrue(m._looks_like_question("  ?연차"))
        self.assertTrue(m._looks_like_question("？연차"))     # 전각 물음표도 인정
        self.assertFalse(m._looks_like_question("연차.xlsx"))
        self.assertFalse(m._looks_like_question(""))
        self.assertFalse(m._looks_like_question(None))


class TestConfirm(unittest.TestCase):
    BOX = "검색창요소"
    LIST = "파일목록요소"

    #--------------------------------------------------------------
    # 정상 — 붙잡아 둔 요소에서 읽어 질문을 넘긴다
    #=> 포커스가 파일 목록으로 옮겨 가 있어도 검색창 요소에서 읽어야 한다(P0-1).
    #--------------------------------------------------------------
    def test_ok_reads_saved_element(self):
        m, got = make_monitor(["D:\\문서\\인사"], ["D:\\문서"],
                              {self.BOX: "?연차 이월 기준", self.LIST: "연차규정.txt"})
        m.armed = rsb_monitor.Armed(1, self.BOX, ["D:\\문서\\인사"], "가짜", None)
        m._confirm()
        self.assertEqual(got, [("?연차 이월 기준", ["D:\\문서\\인사"])])
        self.assertEqual(m._box.read_calls, [self.BOX])     # 파일 목록은 읽지 않았다
        self.assertIsNone(m.armed)                          # 두 번 안 나가게 무장이 풀린다

    #--------------------------------------------------------------
    # 범위 밖 폴더면 조용히 무시한다
    #--------------------------------------------------------------
    def test_outside_scope(self):
        m, got = make_monitor(["D:\\다른곳"], ["D:\\문서"], {self.BOX: "?연차"})
        m.armed = rsb_monitor.Armed(1, self.BOX, ["D:\\다른곳"], "가짜", None)
        m._confirm()
        self.assertEqual(got, [])

    #--------------------------------------------------------------
    # 후보가 여럿이면 전부 범위 안일 때만 실행한다 (보수 규칙)
    #--------------------------------------------------------------
    def test_multi_candidates(self):
        m, got = make_monitor(None, ["D:\\문서"], {self.BOX: "?연차"})
        m.armed = rsb_monitor.Armed(1, self.BOX, ["D:\\문서\\A", "D:\\문서\\B"], "가짜", None)
        m._confirm()
        self.assertEqual(len(got), 1)

        m2, got2 = make_monitor(None, ["D:\\문서"], {self.BOX: "?연차"})
        m2.armed = rsb_monitor.Armed(1, self.BOX, ["D:\\문서\\A", "D:\\밖"], "가짜", None)
        m2._confirm()
        self.assertEqual(got2, [])

    #--------------------------------------------------------------
    # 폴더를 못 가렸으면(가상 폴더·검색 결과인데 기억 없음) 실행하지 않는다
    #=> 무장 때 못 구했으면 확정 때 한 번 더 구해 보고, 그래도 모르면 무시한다.
    #--------------------------------------------------------------
    def test_unknown_folder(self):
        m, got = make_monitor([None], ["D:\\문서"], {self.BOX: "?연차"})
        m.armed = rsb_monitor.Armed(1, self.BOX, [None], "가짜", None)
        m._confirm()
        self.assertEqual(got, [])
        self.assertEqual(m._locator.calls, 1)               # 확정 때 다시 구해 봤다

    #--------------------------------------------------------------
    # 확정 때 다시 구한 폴더가 범위 안이면 실행한다
    #--------------------------------------------------------------
    def test_retry_folder_ok(self):
        m, got = make_monitor(["D:\\문서\\인사"], ["D:\\문서"], {self.BOX: "?연차"})
        m.armed = rsb_monitor.Armed(1, self.BOX, [None], "가짜", None)
        m._confirm()
        self.assertEqual(got, [("?연차", ["D:\\문서\\인사"])])

    #--------------------------------------------------------------
    # Enter 뒤에 읽은 값이 ? 로 시작하지 않으면 취소한다
    #=> 사용자가 ? 를 지우고 Enter 를 친 경우다. 읽은 값을 믿는다.
    #--------------------------------------------------------------
    def test_prefix_gone(self):
        m, got = make_monitor(["D:\\문서"], ["D:\\문서"], {self.BOX: "연차.xlsx"})
        m.armed = rsb_monitor.Armed(1, self.BOX, ["D:\\문서"], "가짜", None)
        m._confirm()
        self.assertEqual(got, [])

    #--------------------------------------------------------------
    # 요소가 죽어 못 읽으면 마지막으로 읽어 둔 값을 쓴다
    #--------------------------------------------------------------
    def test_fallback_to_cached_value(self):
        m, got = make_monitor(["D:\\문서"], ["D:\\문서"], {})
        a = rsb_monitor.Armed(1, "죽은요소", ["D:\\문서"], "가짜", None)
        a.value = "?연차"
        m.armed = a
        m._confirm()
        self.assertEqual(got, [("?연차", ["D:\\문서"])])


class TestTickPrecheck(unittest.TestCase):
    #--------------------------------------------------------------
    # 무장하지 않았으면 한 주기에 아무 일도 하지 않는다
    #=> 탐색기를 쓰지 않는 대부분의 시간에 비용이 없어야 한다.
    #--------------------------------------------------------------
    def test_idle_tick(self):
        m, got = make_monitor(["D:\\문서"], ["D:\\문서"], {})
        m._focus_dirty = False
        m.hook_ok = True
        m.armed = None
        # 훅이 있을 때의 1초 안전 재확인이 이 주기에 걸리지 않게 막는다
        # (마침 탐색기가 앞에 있으면 결과가 달라져 시험이 들쭉날쭉해진다)
        m._last_focus_scan = time.monotonic()
        m._tick()
        self.assertEqual(got, [])
        self.assertEqual(m._box.read_calls, [])


if __name__ == "__main__":
    unittest.main()
