#------------------------------------------------------------------
# 근거 목록 질의 만들기 시험 (설계서 §16)
#=> 검색창에 써 넣을 글자를 만드는 부분만 본다(탐색기는 건드리지 않는다).
#   설계서에 "미확인" 으로 남겨 두었던 따옴표·특수문자 파일 이름을 여기서 정한다.
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evidence_list  # noqa: E402
from evidence_list import build_query, quote_name  # noqa: E402


class TestQuoteName(unittest.TestCase):
    #--------------------------------------------------------------
    # 보통 이름은 따옴표로 감싼다
    #--------------------------------------------------------------
    def test_plain(self):
        self.assertEqual(quote_name("취업규칙.doc"), '"취업규칙.doc"')

    #--------------------------------------------------------------
    # 경로가 들어와도 파일 이름만 쓴다
    #=> 근거에 경로가 함께 오게 되더라도 그대로 동작해야 한다.
    #--------------------------------------------------------------
    def test_path(self):
        self.assertEqual(quote_name(r"D:\문서\인사\취업규칙.doc"), '"취업규칙.doc"')

    #--------------------------------------------------------------
    # 공백·괄호·&·+ 같은 글자는 따옴표 안이라 그대로 둔다
    #--------------------------------------------------------------
    def test_special_chars(self):
        for name in ("12.원격지 근무자 비품 구매 관련 지침_22.01.25.docx",
                     "규정(개정)+별표 & 부록.doc",
                     "2024~2025 인사-규정 [최종].hwp"):
            self.assertEqual(quote_name(name), '"{}"'.format(name))

    #--------------------------------------------------------------
    # 이름 안에 큰따옴표가 있으면 감쌀 수가 없다 → 가장 긴 조각만 쓴다
    #=> 정확하진 않아도 "아무것도 안 나오는 것" 보다 낫다. 어차피 그 폴더 안에서만 찾는다.
    #--------------------------------------------------------------
    def test_embedded_quote(self):
        # '사규 / 별표3 / 개정안.docx' 로 갈리고 가장 긴 조각이 남는다.
        # 탐색기 검색은 이름의 일부만으로도 찾으므로(실측) 이 조각으로도 걸린다.
        self.assertEqual(quote_name('사규 "별표3" 개정안.docx'), '"개정안.docx"')

    #--------------------------------------------------------------
    # 쓸 수 없는 이름은 None
    #--------------------------------------------------------------
    def test_empty(self):
        self.assertIsNone(quote_name(""))
        self.assertIsNone(quote_name(None))
        self.assertIsNone(quote_name('"'))
        self.assertIsNone(quote_name("   "))


class TestBuildQuery(unittest.TestCase):
    #--------------------------------------------------------------
    # 여러 개를 OR 로 잇는다 (실측으로 이 문법만 먹었다)
    #--------------------------------------------------------------
    def test_or(self):
        q = build_query(["25.출장여비규정_16.04.01.doc", "취업규칙.doc"])
        self.assertEqual(q, '"25.출장여비규정_16.04.01.doc" OR "취업규칙.doc"')

    #--------------------------------------------------------------
    # 같은 문서가 여러 번 인용돼도 한 번만 넣는다
    #=> 근거 3건이 같은 파일의 다른 대목인 경우가 흔하다(실제 화면이 그랬다).
    #--------------------------------------------------------------
    def test_dedup(self):
        q = build_query(["취업규칙.doc", "취업규칙.doc", "취업규칙.doc"])
        self.assertEqual(q, '"취업규칙.doc"')

    #--------------------------------------------------------------
    # 너무 많으면 앞에서부터 잘라 쓴다(질의가 한없이 길어지지 않게)
    #--------------------------------------------------------------
    def test_limit(self):
        names = ["문서{}.doc".format(i) for i in range(30)]
        q = build_query(names, limit=3)
        self.assertEqual(q.count(" OR "), 2)
        self.assertIn('"문서0.doc"', q)
        self.assertNotIn('"문서3.doc"', q)

    #--------------------------------------------------------------
    # 쓸 이름이 하나도 없으면 None — 부르는 쪽이 "알 수 없습니다" 로 처리한다
    #--------------------------------------------------------------
    def test_nothing(self):
        self.assertIsNone(build_query([]))
        self.assertIsNone(build_query(None))
        self.assertIsNone(build_query(["", None, "   "]))

    #--------------------------------------------------------------
    # 순서는 근거 순서를 지킨다
    #--------------------------------------------------------------
    def test_order(self):
        q = build_query(["나.doc", "가.doc"])
        self.assertTrue(q.index("나.doc") < q.index("가.doc"))


#------------------------------------------------------------------
# 가짜 검색창 — 실제 탐색기 없이 EvidenceList 의 판단만 본다
#
# -필드: written = 써 넣은 글자들
#------------------------------------------------------------------
class FakeBox:
    def __init__(self, edit=object(), can_write=True):
        self.edit = edit
        self.can_write = can_write
        self.written = []

    #--------------------------------------------------------------
    # 검색창 찾기 (가짜)
    #--------------------------------------------------------------
    def find_edit(self, hwnd):
        return self.edit

    #--------------------------------------------------------------
    # 값 쓰기 (가짜)
    #--------------------------------------------------------------
    def set_value_no_focus(self, e, text):
        if not self.can_write:
            return False
        self.written.append(text)
        return True


class TestEvidenceList(unittest.TestCase):
    #--------------------------------------------------------------
    # 시험용 창 번호
    #=> EvidenceList 는 "창이 살아 있나 · 최소화됐나" 를 진짜로 확인한다.
    #   그래서 아무 숫자가 아니라 실제로 존재하는 창(바탕 화면)을 쓴다.
    #--------------------------------------------------------------
    def setUp(self):
        import win32gui
        self.hwnd = win32gui.GetDesktopWindow()

    #--------------------------------------------------------------
    # 근거를 넣으면 질의가 검색창으로 간다
    #--------------------------------------------------------------
    def test_show(self):
        box = FakeBox()
        ev = evidence_list.EvidenceList(box)
        ok, detail = ev.show(self.hwnd, ["취업규칙.doc", "취업규칙.doc"])
        self.assertTrue(ok, detail)
        self.assertEqual(box.written, ['"취업규칙.doc"'])

    #--------------------------------------------------------------
    # 기억해 둔 원래 검색어로 되돌린다
    #--------------------------------------------------------------
    def test_restore(self):
        box = FakeBox()
        ev = evidence_list.EvidenceList(box)
        ev.remember(self.hwnd, "?연차 이월 기준")
        ev.show(self.hwnd, ["취업규칙.doc"])
        ok, detail = ev.restore(self.hwnd)
        self.assertTrue(ok, detail)
        self.assertEqual(box.written[-1], "?연차 이월 기준")

    #--------------------------------------------------------------
    # 기억이 없으면 되돌리지 않는다(엉뚱한 글자를 넣지 않는다)
    #--------------------------------------------------------------
    def test_restore_unknown(self):
        box = FakeBox()
        ev = evidence_list.EvidenceList(box)
        ok, why = ev.restore(99)
        self.assertFalse(ok)
        self.assertEqual(box.written, [])
        self.assertIn("모릅니다", why)

    #--------------------------------------------------------------
    # 검색창에 못 쓰면 사유를 돌려준다
    #--------------------------------------------------------------
    def test_write_fail(self):
        box = FakeBox(can_write=False)
        ev = evidence_list.EvidenceList(box)
        ok, why = ev.show(self.hwnd, ["취업규칙.doc"])
        self.assertFalse(ok)
        self.assertIn("쓰지 못했습니다", why)

    #--------------------------------------------------------------
    # 기억은 창 수만큼만 — 창을 많이 열고 닫아도 쌓이지 않는다
    #--------------------------------------------------------------
    def test_remember_capped(self):
        ev = evidence_list.EvidenceList(FakeBox())
        for i in range(evidence_list.RESTORE_LIMIT + 10):
            ev.remember(i, "?질문{}".format(i))
        self.assertLessEqual(len(ev.original), evidence_list.RESTORE_LIMIT)


if __name__ == "__main__":
    unittest.main()
