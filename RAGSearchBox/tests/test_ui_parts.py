#------------------------------------------------------------------
# 창 위치·자동 시작 시험 — 화면이나 레지스트리를 건드리지 않는 순수 부분만
#=> 창을 실제로 띄우는 것은 tests/manual_ui.py 로 눈으로 확인한다.
#   여기서는 계산과 값 만들기만 본다(자동 실행에서 안전해야 한다).
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autorun  # noqa: E402
from answer_window import place_near  # noqa: E402


class TestPlaceNear(unittest.TestCase):
    #--------------------------------------------------------------
    # 검색창 바로 아래, 오른쪽 끝에 맞춘다
    #--------------------------------------------------------------
    def test_below_anchor(self):
        x, y = place_near((900, 60, 1200, 90), 460, 200, (1920, 1080))
        self.assertEqual(x, 1200 - 460)
        self.assertEqual(y, 94)

    #--------------------------------------------------------------
    # 화면 오른쪽으로 넘치면 안으로 당긴다
    #--------------------------------------------------------------
    def test_clamp_right(self):
        x, _ = place_near((1700, 60, 1900, 90), 460, 200, (1920, 1080))
        self.assertLessEqual(x + 460, 1920)

    #--------------------------------------------------------------
    # 화면 아래로 넘치면 위로 올린다
    #--------------------------------------------------------------
    def test_clamp_bottom(self):
        _, y = place_near((900, 1000, 1200, 1040), 460, 300, (1920, 1080))
        self.assertLessEqual(y + 300, 1080)

    #--------------------------------------------------------------
    # 검색창 좌표를 모르면 오른쪽 위로 보낸다
    #--------------------------------------------------------------
    def test_no_anchor(self):
        x, y = place_near(None, 460, 200, (1920, 1080))
        self.assertGreater(x, 1000)
        self.assertLess(y, 50)

    #--------------------------------------------------------------
    # 작은 화면에서도 창이 화면 안에 있다
    #--------------------------------------------------------------
    def test_small_screen(self):
        x, y = place_near((700, 40, 900, 70), 460, 400, (1024, 600))
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + 460, 1024)
        self.assertLessEqual(y + 400, 600)


class TestAutorunValue(unittest.TestCase):
    #--------------------------------------------------------------
    # 등록할 명령은 따옴표로 감싼 절대 경로다
    #=> 레지스트리에 쓰지 않고 값만 확인한다(사용자 설정을 건드리면 안 된다).
    #--------------------------------------------------------------
    def test_command_value(self):
        v = autorun.command_value()
        self.assertTrue(v.startswith('"'), v)
        self.assertIn(".exe", v.lower())
        if not getattr(sys, "frozen", False):
            self.assertIn("main.py", v)          # 소스 실행이면 진입점이 붙는다

    #--------------------------------------------------------------
    # 알 수 없는 명령은 사용법을 알리고 2 를 돌려준다
    #--------------------------------------------------------------
    def test_cli_unknown(self):
        self.assertEqual(autorun.run_cli("모름"), 2)
        self.assertEqual(autorun.run_cli(""), 2)


if __name__ == "__main__":
    unittest.main()
