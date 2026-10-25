#------------------------------------------------------------------
# history 시험 — 폴더 패널의 대화 기록(⋯ 메뉴)
#=> 최근 10개만 남는지, 다시 물으면 맨 앞으로 오는지, 깨진 파일에도 죽지 않는지 본다.
#------------------------------------------------------------------
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import history  # noqa: E402


class TestHistory(unittest.TestCase):
    #--------------------------------------------------------------
    # 11개를 넣으면 가장 오래된 것이 빠지고 최근 것이 앞에 온다
    #--------------------------------------------------------------
    def test_keep_ten(self):
        convs = []
        for i in range(11):
            history.touch(convs, history.new_conv("질문 {}".format(i), "D:\\문서", now=i), now=i)
        self.assertEqual(len(convs), history.MAX_CONVS)
        self.assertEqual(convs[0]["title"], "질문 10")
        self.assertNotIn("질문 0", [c["title"] for c in convs])

    #--------------------------------------------------------------
    # 예전 대화에 이어 물으면 그 대화가 맨 앞으로 오고 겹치지 않는다
    #--------------------------------------------------------------
    def test_touch_moves_front(self):
        convs = []
        a = history.new_conv("가", "", now=1)
        b = history.new_conv("나", "", now=2)
        history.touch(convs, a, now=1)
        history.touch(convs, b, now=2)
        history.touch(convs, a, now=3)
        self.assertEqual([c["title"] for c in convs], ["가", "나"])
        self.assertEqual(convs[0]["updated"], 3)

    #--------------------------------------------------------------
    # 마디가 너무 많으면 오래된 마디부터 자른다
    #--------------------------------------------------------------
    def test_trim_turns(self):
        convs = []
        c = history.new_conv("q", "")
        c["turns"] = [{"q": str(i)} for i in range(30)]
        history.touch(convs, c)
        self.assertEqual(len(c["turns"]), history.MAX_TURNS)
        self.assertEqual(c["turns"][-1]["q"], "29")

    #--------------------------------------------------------------
    # 긴 질문은 제목을 자르고, 줄바꿈은 한 줄로 편다
    #--------------------------------------------------------------
    def test_title(self):
        c = history.new_conv("출장시\n" + "가" * 80, "")
        self.assertTrue(c["title"].startswith("출장시 가"))
        self.assertLessEqual(len(c["title"]), history.TITLE_CHARS)

    #--------------------------------------------------------------
    # 저장한 것을 그대로 읽고, 깨진 파일·없는 파일은 빈 기록이다
    #--------------------------------------------------------------
    def test_save_load(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "history.json")
        self.assertEqual(history.load(p), [])
        c = history.new_conv("연차 이월", "D:\\문서")
        c["turns"].append({"q": "연차 이월", "answer": "답", "evidence": [{"no": 1, "doc": "a.pdf"}]})
        self.assertTrue(history.save(p, [c]))
        got = history.load(p)
        self.assertEqual(got[0]["turns"][0]["evidence"][0]["doc"], "a.pdf")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{깨진")
        self.assertEqual(history.load(p), [])

    #--------------------------------------------------------------
    # 기록 파일은 로그 폴더 한 단계 위에 둔다
    #--------------------------------------------------------------
    def test_default_path(self):
        p = history.default_path(os.path.join("C:\\x", "RAGSearchBox", "log"))
        self.assertTrue(p.endswith(os.path.join("RAGSearchBox", "history.json")))


if __name__ == "__main__":
    unittest.main()
