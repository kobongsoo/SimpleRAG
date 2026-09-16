#------------------------------------------------------------------
# query_filter 시험 — 탐색기 없이 돌아가는 순수 로직 확인
#=> 설계서 §5 T4 의 규칙이 그대로 지켜지는지 본다.
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from query_filter import Dedup, to_question  # noqa: E402


class TestToQuestion(unittest.TestCase):
    #--------------------------------------------------------------
    # 접두어가 있는 정상 입력
    #--------------------------------------------------------------
    def test_prefix(self):
        self.assertEqual(to_question("?연차 이월 기준")[0], "연차 이월 기준")
        self.assertEqual(to_question("？전각 물음표")[0], "전각 물음표")
        self.assertEqual(to_question("?  공백 뒤 질문")[0], "공백 뒤 질문")
        self.assertEqual(to_question("?? 두 번 친 경우")[0], "두 번 친 경우")

    #--------------------------------------------------------------
    # 접두어가 없으면 파일 검색이므로 건드리지 않는다
    #--------------------------------------------------------------
    def test_no_prefix(self):
        q, why = to_question("보고서")
        self.assertIsNone(q)
        self.assertEqual(why, "접두어 없음")
        self.assertIsNone(to_question("")[0])
        self.assertIsNone(to_question(None)[0])
        self.assertIsNone(to_question("   ")[0])

    #--------------------------------------------------------------
    # 너무 짧은 질문
    #--------------------------------------------------------------
    def test_min_chars(self):
        self.assertIsNone(to_question("?")[0])
        self.assertIsNone(to_question("?가", min_chars=3)[0])
        self.assertIsNone(to_question("?가나", min_chars=3)[0])       # 접두어를 떼면 2글자
        self.assertEqual(to_question("?가나다", min_chars=3)[0], "가나다")

    #--------------------------------------------------------------
    # 워커 조작으로 읽히지 않게 하기 (chat 슬래시 명령·종료어)
    #--------------------------------------------------------------
    def test_worker_command_guard(self):
        self.assertEqual(to_question("?/topk 5")[0], "topk 5")
        self.assertEqual(to_question("?종료")[0], "종료 ?")
        self.assertEqual(to_question("?exit")[0], "exit ?")
        self.assertEqual(to_question("?EXIT")[0], "EXIT ?")

    #--------------------------------------------------------------
    # 워커는 한 줄만 읽는다
    #--------------------------------------------------------------
    def test_single_line(self):
        self.assertEqual(to_question("?연차\n이월\t기준")[0], "연차 이월 기준")

    #--------------------------------------------------------------
    # 접두어를 바꾼 경우 (전각은 기본 접두어일 때만 인정한다)
    #--------------------------------------------------------------
    def test_custom_prefix(self):
        self.assertEqual(to_question("!질문 내용", prefix="!")[0], "질문 내용")
        self.assertIsNone(to_question("?질문 내용", prefix="!")[0])


class TestDedup(unittest.TestCase):
    #--------------------------------------------------------------
    # 같은 질문은 정해진 시간 안에서만 무시
    #--------------------------------------------------------------
    def test_window(self):
        d = Dedup(window_sec=3)
        self.assertFalse(d.is_duplicate("연차", now=100.0))
        self.assertTrue(d.is_duplicate("연차", now=101.0))
        self.assertFalse(d.is_duplicate("연차", now=104.0))

    #--------------------------------------------------------------
    # 다른 질문은 곧바로 통과
    #--------------------------------------------------------------
    def test_other_question(self):
        d = Dedup(window_sec=3)
        self.assertFalse(d.is_duplicate("연차", now=100.0))
        self.assertFalse(d.is_duplicate("경조금", now=100.1))

    #--------------------------------------------------------------
    # 0 이면 거르지 않는다
    #--------------------------------------------------------------
    def test_disabled(self):
        d = Dedup(window_sec=0)
        self.assertFalse(d.is_duplicate("연차", now=100.0))
        self.assertFalse(d.is_duplicate("연차", now=100.0))


if __name__ == "__main__":
    unittest.main()
