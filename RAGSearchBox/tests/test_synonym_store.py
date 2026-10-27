#------------------------------------------------------------------
# synonym_store 시험 — 설정 창 동의어 칸(동의어 설계서 4단계)
#=> 후보 걸러 보이기, [맞음]·[아님]·[직접 추가] 가 synonyms.yaml 에 맞게 적히는지,
#   적힌 파일을 SimpleRAG(워커)가 그대로 읽는지, 깨진 사전을 덮어쓰지 않는지 본다.
#------------------------------------------------------------------
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import synonym_store as ss  # noqa: E402
from info_window import synonym_note  # noqa: E402

CANDS = {"candidates": [
    {"word": "할아버지", "count": 2, "examples": ["할아버지 돌아가시면 휴가 며칠?"],
     "terms": [{"term": "조부모", "score": 0.6, "llm": True, "docs": ["취업규칙.pdf"]},
               {"term": "부모", "score": 0.5, "llm": False, "docs": []}]},
    {"word": "와이프", "count": 1, "examples": ["와이프 출산 휴가"],
     "terms": [{"term": "여비", "score": 0.55, "llm": False, "docs": []},
               {"term": "배우자", "score": 0.43, "llm": True, "docs": ["취업규칙.pdf"]}]},
]}


class TestSynonymStore(unittest.TestCase):
    #--------------------------------------------------------------
    # 임시 데이터 폴더에 후보 파일을 둔다
    #--------------------------------------------------------------
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        with io.open(os.path.join(self.home, ss.CANDIDATES_NAME), "w", encoding="utf-8") as f:
            json.dump(CANDS, f, ensure_ascii=False)

    #--------------------------------------------------------------
    # 임시 폴더 지우기
    #--------------------------------------------------------------
    def tearDown(self):
        self.tmp.cleanup()

    #--------------------------------------------------------------
    # 승인하면 그 낱말 줄이 사라지고, [아님] 한 용어는 그 줄에서만 빠진다
    #--------------------------------------------------------------
    def test_approve_and_reject_filter_pending(self):
        rows, n = ss.pending(self.home)
        self.assertEqual((n, [r["word"] for r in rows]), (2, ["할아버지", "와이프"]))
        self.assertEqual(rows[0]["docs"]["조부모"], ["취업규칙.pdf"])
        ok, _ = ss.approve(self.home, "할아버지", "조부모")
        self.assertTrue(ok)
        ok, _ = ss.reject(self.home, "와이프", "여비")
        self.assertTrue(ok)
        rows, n = ss.pending(self.home)
        self.assertEqual(n, 1)
        self.assertEqual((rows[0]["word"], rows[0]["terms"]), ("와이프", ["배우자"]))
        self.assertEqual(ss.counts(self.home), (0, 1))

    #--------------------------------------------------------------
    # 적은 파일 모양 — 두 판이 같은 글을 쓰고, YAML 로 다시 읽힌다(워커가 읽는 그대로)
    #--------------------------------------------------------------
    def test_file_shape_and_roundtrip(self):
        ss.approve(self.home, "할아버지", "조부모")
        ss.approve(self.home, "외할머니", "조부모")
        ss.reject(self.home, "와이프", "여비")
        text = io.open(os.path.join(self.home, ss.USER_NAME), encoding="utf-8").read()
        self.assertIn('synonyms:\n  "조부모": ["할아버지", "외할머니"]\nrejected:\n  - ["와이프", "여비"]\n', text)
        syn, rej = ss.load_user(self.home)
        self.assertEqual((syn, rej), ({"조부모": ["할아버지", "외할머니"]}, [["와이프", "여비"]]))
        # 고치기 전 파일은 .bak 으로 남는다
        self.assertTrue(os.path.isfile(os.path.join(self.home, ss.USER_NAME + ".bak")))

    #--------------------------------------------------------------
    # 승인했던 짝을 [아님] 하면 사전에서 빠지고, 다시 승인하면 [아님] 에서 빠진다
    #--------------------------------------------------------------
    def test_change_of_mind(self):
        ss.approve(self.home, "할아버지", "조부모")
        ss.reject(self.home, "할아버지", "조부모")
        self.assertEqual(ss.load_user(self.home), ({}, [["할아버지", "조부모"]]))
        ss.approve(self.home, "할아버지", "조부모")
        self.assertEqual(ss.load_user(self.home), ({"조부모": ["할아버지"]}, []))

    #--------------------------------------------------------------
    # 승인한 목록과 [빼기] — 사전에서만 빠지고 [아님] 목록은 건드리지 않으며, 그 낱말은 다시 후보로 보인다
    #--------------------------------------------------------------
    def test_approved_and_remove(self):
        ss.approve(self.home, "할아버지", "조부모")
        ss.reject(self.home, "와이프", "여비")
        self.assertEqual(ss.approved_pairs(self.home), [("할아버지", "조부모")])
        self.assertTrue(ss.remove(self.home, "할아버지", "조부모")[0])
        self.assertFalse(ss.remove(self.home, "할아버지", "조부모")[0])
        self.assertEqual(ss.load_user(self.home), ({}, [["와이프", "여비"]]))
        self.assertIn("할아버지", [r["word"] for r in ss.pending(self.home)[0]])

    #--------------------------------------------------------------
    # 직접 추가 확인 — 빈 칸 · 한 글자 · 같은 말은 받지 않는다
    #--------------------------------------------------------------
    def test_check_pair(self):
        for w, t in (("", "조부모"), ("애", "자녀"), ("휴 가", "휴가")):
            ok, msg = ss.approve(self.home, w, t)
            self.assertFalse(ok, msg)
        self.assertFalse(os.path.isfile(os.path.join(self.home, ss.USER_NAME)))

    #--------------------------------------------------------------
    # 손으로 고치다 깨진 사전은 덮어쓰지 않는다
    #--------------------------------------------------------------
    def test_broken_user_file_is_kept(self):
        path = os.path.join(self.home, ss.USER_NAME)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("synonyms: [조부모: \n")
        ok, msg = ss.approve(self.home, "할아버지", "조부모")
        self.assertFalse(ok)
        self.assertIn("읽지 못했습니다", msg)
        self.assertEqual(io.open(path, encoding="utf-8").read(), "synonyms: [조부모: \n")

    #--------------------------------------------------------------
    # 적은 사전을 SimpleRAG 의 동의어 넓히기가 그대로 쓴다
    #--------------------------------------------------------------
    def test_worker_reads_it(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
        from simplerag.retrieve.synonyms import _load, expand_with

        ss.approve(self.home, "할아버지", "조부모")
        ss.reject(self.home, "와이프", "배우자")
        rules = _load([os.path.join(self.home, ss.USER_NAME)])
        self.assertEqual(expand_with(rules, "할아버지 돌아가시면")[0], "할아버지 돌아가시면 조부모")

    #--------------------------------------------------------------
    # 안내 줄 — 후보가 남았을 때 · 놓친 질문만 있을 때 · 아무것도 없을 때
    #--------------------------------------------------------------
    def test_note(self):
        self.assertTrue(synonym_note(3, 2, 1).startswith("후보 2개 남음"))
        self.assertIn("[후보 만들기]", synonym_note(4, 0, 0))
        self.assertTrue(synonym_note(0, 0, 0, "넣었습니다").endswith("\n넣었습니다"))


if __name__ == "__main__":
    unittest.main()
