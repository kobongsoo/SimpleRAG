#------------------------------------------------------------------
# precision 시험 — 설정 창 정밀 모드 체크(config.yaml generation.precision 한 줄 바꾸기)
#=> 켰다 끄면 파일이 글자까지 원래대로인지(주석 칸 정렬·CRLF), 다른 구역의 같은 이름 줄은 건드리지 않는지,
#   줄이 없으면 generation: 아래에 넣는지, 바꾼 파일을 SimpleRAG 설정 읽기가 그대로 받는지 본다.
#   Rust 판 rust/ragsearchbox/src/precision.rs 의 toggle_keeps_file 과 같은 내용.
#------------------------------------------------------------------
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import precision  # noqa: E402

ORIG = ("retrieval:\r\n  precision: x   # 다른 구역\r\ngeneration:\r\n  top_k: 3\r\n"
        "  precision: false              # 주석\r\n")


class TestPrecision(unittest.TestCase):
    #--------------------------------------------------------------
    # 임시 폴더에 config.yaml 을 둔다
    #--------------------------------------------------------------
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.yaml")
        self._write(ORIG)

    #--------------------------------------------------------------
    # 임시 폴더 지우기
    #--------------------------------------------------------------
    def tearDown(self):
        self.tmp.cleanup()

    # 줄 끝을 그대로 쓰고 읽는다
    def _write(self, text):
        with io.open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def _read(self):
        with io.open(self.path, encoding="utf-8", newline="") as f:
            return f.read()

    #--------------------------------------------------------------
    # 켰다 끄면 원래 파일 그대로 — 켠 줄의 주석 칸도 제자리
    #--------------------------------------------------------------
    def test_toggle_keeps_file(self):
        self.assertFalse(precision.read(self.path))
        ok, _ = precision.save(self.path, True)
        self.assertTrue(ok)
        self.assertTrue(precision.read(self.path))
        self.assertIn("  precision: true               # 주석\r\n", self._read())
        self.assertIn("  precision: x   # 다른 구역", self._read())
        precision.save(self.path, False)
        self.assertEqual(self._read(), ORIG)

    #--------------------------------------------------------------
    # 줄이 없으면 generation: 바로 아래에 만든다 · 구역이 없으면 바꾸지 않는다
    #--------------------------------------------------------------
    def test_insert_and_missing(self):
        self._write("generation:\n  top_k: 3\n")
        self.assertTrue(precision.save(self.path, True)[0])
        self.assertEqual(self._read(), "generation:\n  precision: true\n  top_k: 3\n")
        self._write("retrieval:\n  dedup: true\n")
        self.assertFalse(precision.save(self.path, True)[0])
        self.assertFalse(precision.save(None, True)[0])

    #--------------------------------------------------------------
    # 바꾼 파일을 SimpleRAG 설정 읽기(스키마 검사)가 그대로 받는다
    #--------------------------------------------------------------
    def test_simplerag_reads_it(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
        from simplerag.settings import load_settings
        self._write("generation:\n  model: qwen3-1.7b-q4\n  precision: false   # 주석\n")
        precision.save(self.path, True)
        _path, flat = load_settings(self.tmp.name, self.tmp.name)
        self.assertIs(flat.get("generation.precision"), True)

    #--------------------------------------------------------------
    # 안내 줄 — 주의 글은 늘, 모델 파일·config.yaml 이 없으면 그 사유도
    #--------------------------------------------------------------
    def test_note(self):
        self.assertTrue(precision.note(False, True).startswith(precision.WARN))
        self.assertIn("TTFT", precision.WARN)
        self.assertIn(precision.MODEL_FILE, precision.note(False, False))
        self.assertIn("config.yaml", precision.note(False, True, found=False))


class TestLoadPercent(unittest.TestCase):
    #--------------------------------------------------------------
    # 모델 올리기 진행률 — 보통 적재 · 측정 중 · 측정 끝 · 95% 상한 (Rust worker::load_percent 와 같은 값)
    #--------------------------------------------------------------
    def test_load_percent(self):
        from rag_worker import load_percent, LOAD_S, PROBE_S
        self.assertEqual(load_percent(0), 1)
        self.assertEqual(load_percent(LOAD_S / 2), 50)
        self.assertEqual(load_percent(999), 95)
        # 2초에 측정 시작 → 전체 2 + PROBE_S + LOAD_S 로 늘어난다
        self.assertEqual(load_percent(60, 2), int(60 * 100 / (2 + PROBE_S + LOAD_S)))
        # 98초에 측정 끝 → 남은 것은 적재뿐
        self.assertEqual(load_percent(100, 2, 98), int(100 * 100 / (98 + LOAD_S)))

    #--------------------------------------------------------------
    # 진행 막대 아래 글 — % · 초 · 진행 글
    #--------------------------------------------------------------
    def test_loading_note(self):
        from info_window import loading_note
        self.assertEqual(loading_note({"sec": 12, "pct": 40, "detail": "측정 중"}),
                         "모델을 올리는 중 · 40% · 12초 — 측정 중")


if __name__ == "__main__":
    unittest.main()
