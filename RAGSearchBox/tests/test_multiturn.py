#------------------------------------------------------------------
# 멀티턴(이어서 묻기) 시험 — 워커가 /ask 에 앞 질문(prev)을 싣는지, 설정 창 체크가 config.yaml 을 고치는지
#=> 프로세스는 띄우지 않는다. 가짜 stdin 으로 _send_pending 이 쓴 줄을 받아 본다.
#    1) ask-prev 를 아는 워커면 /ask JSON 에 prev 가 실린다(폴더가 없어도 /ask 로)
#    2) 옛 워커(ask-prev 없음)면 prev 를 싣지 않는다
#    3) multiturn 줄이 없으면 기본 켬, 끄면 generation: 아래에 줄이 생긴다
#------------------------------------------------------------------
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import precision  # noqa: E402
from rag_worker import STDIN_ENCODING, RagWorker  # noqa: E402


class FakeStdin:
    #--------------------------------------------------------------
    # 쓴 바이트를 모은다
    #--------------------------------------------------------------
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class FakeProc:
    #--------------------------------------------------------------
    # stdin 만 있는 가짜 워커 프로세스
    #--------------------------------------------------------------
    def __init__(self):
        self.stdin = FakeStdin()


class TestMultiturnAsk(unittest.TestCase):
    #--------------------------------------------------------------
    # 질문을 맡기고 보낸 한 줄을 돌려준다
    #
    # -in: features = 워커가 알린 기능 / folder, prev = 질문의 범위·앞 질문
    #
    # -out: 보낸 줄(끝 줄바꿈 뗌) — 워커 stdin 글자판(STDIN_ENCODING)으로 읽는다
    #--------------------------------------------------------------
    def _sent(self, features, folder=None, prev=None):
        w = RagWorker(["x"], lambda ev: None)
        w.features = set(features)
        w._proc = FakeProc()
        w.ask("침구류는?", folder=folder, prev=prev)
        self.assertTrue(w._send_pending())
        return w._proc.stdin.data.decode(STDIN_ENCODING, "replace").rstrip("\r\n")

    #--------------------------------------------------------------
    # ask-prev 를 아는 워커 — 폴더가 없어도 /ask 로 앞 질문을 싣는다
    #--------------------------------------------------------------
    def test_prev_sent(self):
        line = self._sent({"ask-folder", "ask-prev"}, prev="원격지 근무자 책상은 얼마까지?")
        self.assertTrue(line.startswith("/ask "))
        req = json.loads(line[5:])
        self.assertEqual(req["q"], "침구류는?")
        self.assertEqual(req["prev"], "원격지 근무자 책상은 얼마까지?")
        self.assertIsNone(req["folder"])

    #--------------------------------------------------------------
    # 옛 워커(ask-prev 없음) — 앞 질문을 싣지 않는다. 폴더도 없으면 질문 글 그대로
    #--------------------------------------------------------------
    def test_old_worker(self):
        self.assertEqual(self._sent({"ask-folder"}, prev="앞 질문"), "침구류는?")
        req = json.loads(self._sent({"ask-folder"}, folder="D:\\docs", prev="앞 질문")[5:])
        self.assertNotIn("prev", req)

    #--------------------------------------------------------------
    # 앞 질문이 없으면(새 대화) prev 키를 넣지 않는다
    #--------------------------------------------------------------
    def test_no_prev(self):
        req = json.loads(self._sent({"ask-folder", "ask-prev"}, folder="D:\\docs")[5:])
        self.assertNotIn("prev", req)


class TestMultiturnFlag(unittest.TestCase):
    #--------------------------------------------------------------
    # 줄이 없으면 기본 켬 · 끄면 generation: 바로 아래에 줄이 생기고, 정밀 모드 줄은 그대로
    #--------------------------------------------------------------
    def test_flag(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.yaml")
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write("generation:\n  top_k: 3\n  precision: false   # 주석\n")
            self.assertTrue(precision.read(p, "multiturn"))
            ok, msg = precision.save(p, False, key="multiturn", label="이어서 묻기")
            self.assertTrue(ok)
            self.assertEqual(msg, "이어서 묻기를 껐습니다")
            self.assertFalse(precision.read(p, "multiturn"))
            self.assertFalse(precision.read(p, "precision"))
            with open(p, encoding="utf-8", newline="") as f:
                self.assertEqual(f.read(), "generation:\n  multiturn: false\n  top_k: 3\n  precision: false   # 주석\n")


if __name__ == "__main__":
    unittest.main()
