#------------------------------------------------------------------
# 문서 업로드 시험 (RAGSearchBox 쪽) — 문서 업로드 설계서 §7·§9
#=> 1) 워커가 /ask 에 대화 id · uploads 를 싣는지(upload 를 아는 워커에게만)
#   2) 업로드 진행 글 → % (Rust main.rs upload_progress 와 같은 값)
#   3) 대화 id → 업로드 폴더 이름(SimpleRAG uploads.safe_id 와 같은 규칙)
#------------------------------------------------------------------
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import upload_progress, upload_safe_id  # noqa: E402
from rag_worker import STDIN_ENCODING, RagWorker  # noqa: E402


class FakeStdin:
    # 쓴 바이트를 모은다
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class FakeProc:
    # stdin 만 있는 가짜 워커 프로세스
    def __init__(self):
        self.stdin = FakeStdin()


class TestUploadAsk(unittest.TestCase):
    #--------------------------------------------------------------
    # 질문을 맡기고 보낸 한 줄
    #
    # -in: features = 워커가 알린 기능 / kw = ask 인자
    #
    # -out: 보낸 줄
    #--------------------------------------------------------------
    def _sent(self, features, **kw):
        w = RagWorker(["x"], lambda ev: None)
        w.features = set(features)
        w._proc = FakeProc()
        w.ask("침구류는?", **kw)
        self.assertTrue(w._send_pending())
        return w._proc.stdin.data.decode(STDIN_ENCODING, "replace").rstrip("\r\n")

    #--------------------------------------------------------------
    # upload 를 아는 워커 — 폴더가 없어도 /ask 로 conv · uploads 를 싣는다
    #--------------------------------------------------------------
    def test_conv_sent(self):
        line = self._sent({"ask-folder", "ask-prev", "upload"}, conv="1793.5", uploads=True)
        req = json.loads(line[5:])
        self.assertEqual((req["conv"], req["uploads"]), ("1793.5", True))

    #--------------------------------------------------------------
    # 옛 워커(upload 없음) — 대화 id 를 싣지 않는다
    #--------------------------------------------------------------
    def test_old_worker(self):
        self.assertEqual(self._sent({"ask-folder", "ask-prev"}, conv="1793.5", uploads=True), "침구류는?")


class TestUploadHelpers(unittest.TestCase):
    #--------------------------------------------------------------
    # 진행 글 → (%, 글) — 글자 뽑기 5% · 임베딩 10~99%
    #--------------------------------------------------------------
    def test_progress(self):
        self.assertEqual(upload_progress("[upload] 임베딩 64/128"), (54, "임베딩 64/128"))
        self.assertEqual(upload_progress("[upload] 임베딩 0/6"), (10, "임베딩 0/6"))
        self.assertEqual(upload_progress("[upload] 글자 뽑는 중 — 계약서.pdf"), (5, "글자 뽑는 중"))
        self.assertIsNone(upload_progress("리랭커 적재 완료 (1200ms)"))

    #--------------------------------------------------------------
    # 대화 id → 폴더 이름 — 경로를 벗어나는 글자를 거른다
    #--------------------------------------------------------------
    def test_safe_id(self):
        self.assertEqual(upload_safe_id("1793171456.529000"), "1793171456.529000")
        self.assertEqual(upload_safe_id("..\\..\\x"), "x")
        self.assertIsNone(upload_safe_id(""))


if __name__ == "__main__":
    unittest.main()
