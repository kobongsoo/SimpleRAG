#------------------------------------------------------------------
# 따라다니는 패널 자리 계산 시험 (설계서 §17)
#=> 창을 띄우지 않고 계산만 본다. 실제로 붙어 다니는지는 tests 밖의 확인 스크립트로 본다.
#   화면은 1920x1080 에 작업 표시줄을 뺀 (0,0,1920,1040) 을 기본으로 쓴다.
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dock  # noqa: E402
from dock import panel_rect  # noqa: E402

WORK = (0, 0, 1920, 1040)
W = 460


class TestPanelRect(unittest.TestCase):
    #--------------------------------------------------------------
    # 창 오른쪽에 자리가 넉넉하면 바깥에 붙는다 — 목록을 가리지 않는다
    #--------------------------------------------------------------
    def test_outside_right(self):
        x, y, w, h = panel_rect((100, 100, 1000, 900), WORK, W)
        self.assertEqual(x, 1000 + dock.GAP)
        self.assertEqual(y, 100)
        self.assertEqual(w, W)
        self.assertEqual(h, 800)                 # 창 높이에 맞춘다
        self.assertLessEqual(x + w, WORK[2])

    #--------------------------------------------------------------
    # 오른쪽에 자리가 없으면 창 안쪽 가장자리에 붙는다
    #--------------------------------------------------------------
    def test_inside_right(self):
        x, y, w, h = panel_rect((100, 50, 1900, 1000), WORK, W)
        self.assertEqual(x, 1900 - W - dock.EDGE)
        self.assertLessEqual(x + w, WORK[2])
        self.assertEqual(h, 950)

    #--------------------------------------------------------------
    # 창이 최대화되어 있어도 화면 안에 있어야 한다
    #--------------------------------------------------------------
    def test_maximized(self):
        x, y, w, h = panel_rect(WORK, WORK, W)
        self.assertGreaterEqual(x, WORK[0])
        self.assertLessEqual(x + w, WORK[2])
        self.assertEqual(y, 0)
        self.assertEqual(h, 1040)

    #--------------------------------------------------------------
    # 창이 화면 위아래로 삐져나가도 패널은 작업 영역 안에 머문다
    #=> 작업 표시줄을 덮거나 화면 위로 잘려 보이지 않게 한다.
    #--------------------------------------------------------------
    def test_clamped_vertically(self):
        x, y, w, h = panel_rect((100, -200, 1000, 1400), WORK, W)
        self.assertGreaterEqual(y, WORK[1])
        self.assertLessEqual(y + h, WORK[3])

    #--------------------------------------------------------------
    # 아주 낮은 창에서도 최소 높이는 지킨다
    #--------------------------------------------------------------
    def test_min_height(self):
        _, y, _, h = panel_rect((100, 900, 800, 960), WORK, W)
        self.assertGreaterEqual(h, dock.MIN_HEIGHT)
        self.assertLessEqual(y + h, WORK[3])

    #--------------------------------------------------------------
    # 두 번째 모니터(음수 좌표)에서도 그 모니터 안에 붙는다
    #=> 다중 모니터에서 주 화면으로 튀지 않아야 한다.
    #--------------------------------------------------------------
    def test_second_monitor(self):
        work = (-1920, 0, 0, 1040)
        x, y, w, h = panel_rect((-1800, 100, -900, 900), work, W)
        self.assertGreaterEqual(x, work[0])
        self.assertLessEqual(x + w, work[2])
        self.assertEqual(x, -900 + dock.GAP)

    #--------------------------------------------------------------
    # 너비가 터무니없이 작아도 쓸 수 있는 값으로 올린다
    #--------------------------------------------------------------
    def test_min_width(self):
        _, _, w, _ = panel_rect((100, 100, 800, 900), WORK, 10)
        self.assertGreaterEqual(w, 200)

    #--------------------------------------------------------------
    # 같은 입력이면 같은 자리 — app 이 "바뀔 때만 옮기기" 를 할 수 있어야 한다
    #--------------------------------------------------------------
    def test_stable(self):
        a = panel_rect((100, 100, 1000, 900), WORK, W)
        b = panel_rect((100, 100, 1000, 900), WORK, W)
        self.assertEqual(a, b)


class TestTargetState(unittest.TestCase):
    #--------------------------------------------------------------
    # 없는 창은 "죽었다" 로 본다
    #--------------------------------------------------------------
    def test_dead(self):
        alive, minimized, rect = dock.target_state(123456789)
        self.assertFalse(alive)
        self.assertIsNone(rect)

    #--------------------------------------------------------------
    # 실제 창(바탕 화면)은 살아 있고 사각형을 돌려준다
    #--------------------------------------------------------------
    def test_alive(self):
        import win32gui
        alive, minimized, rect = dock.target_state(win32gui.GetDesktopWindow())
        self.assertTrue(alive)
        self.assertFalse(minimized)
        self.assertEqual(len(rect), 4)

    #--------------------------------------------------------------
    # 창 번호가 0 이면 대상이 없는 것이다
    #--------------------------------------------------------------
    def test_none(self):
        self.assertEqual(dock.target_state(None)[0], False)
        self.assertEqual(dock.target_state(0)[0], False)


class TestWorkArea(unittest.TestCase):
    #--------------------------------------------------------------
    # 작업 영역은 항상 쓸 만한 사각형이어야 한다
    #--------------------------------------------------------------
    def test_shape(self):
        import win32gui
        l, t, r, b = dock.work_area(win32gui.GetDesktopWindow())
        self.assertGreater(r, l)
        self.assertGreater(b, t)


if __name__ == "__main__":
    unittest.main()
