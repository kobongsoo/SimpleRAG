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
from info_window import folder_note, info_rows  # noqa: E402


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


class TestInfoRows(unittest.TestCase):
    #--------------------------------------------------------------
    # 워커가 준 값을 칸마다 그대로 보여 준다 — 상태 칸은 없다(메뉴 맨 위 줄이 맡는다)
    #--------------------------------------------------------------
    def test_values(self):
        rows = info_rows({"model": "Qwen3-1.7B", "embed_model": "e5-small-ko",
                          "rerank_model": "bge-reranker-base-int8", "rerank": "켬",
                          "gen": "iGPU(Vulkan)", "folders": [r"D:\문서"], "docs": 1234})
        # 인덱싱 폴더는 목록 칸으로 따로 그리므로 줄 목록에는 없다
        self.assertEqual([n for n, _ in rows],
                         ["생성 모델", "임베딩 모델", "리랭킹 모델", "생성 백엔드"])
        rows = dict(rows)
        self.assertEqual(rows["생성 모델"], "Qwen3-1.7B")
        self.assertEqual(rows["임베딩 모델"], "e5-small-ko")
        self.assertEqual(rows["리랭킹 모델"], "bge-reranker-base-int8 · 켬")
        self.assertEqual(rows["생성 백엔드"], "iGPU(Vulkan)")

    #--------------------------------------------------------------
    # 인덱싱 폴더 아래 안내 줄 — 문서 수 / 폴더 없음 / 수를 모름
    #--------------------------------------------------------------
    def test_folder_note(self):
        self.assertEqual(folder_note({"folders": [r"D:\문서"], "docs": 1234}), "인덱싱된 문서 1,234건")
        self.assertIn("[추가…]", folder_note({}))
        self.assertEqual(folder_note({"folders": [r"D:\문서"], "docs": None}), "")

    #--------------------------------------------------------------
    # 옛 워커라 리랭커 이름을 모르면 상태만 보인다
    #--------------------------------------------------------------
    def test_rerank_state_only(self):
        rows = dict(info_rows({"rerank": "켬(백그라운드 적재 중)"}))
        self.assertEqual(rows["리랭킹 모델"], "켬(백그라운드 적재 중)")

    #--------------------------------------------------------------
    # 워커가 안 떴거나 폴더가 없어도 빈 칸 대신 안내 글이 나온다
    #--------------------------------------------------------------
    def test_empty(self):
        rows = dict(info_rows({}))
        self.assertIn("올라가 있지 않음", rows["생성 모델"])
        self.assertIn("올라가 있지 않음", rows["임베딩 모델"])
        self.assertIn("올라가 있지 않음", rows["리랭킹 모델"])


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
