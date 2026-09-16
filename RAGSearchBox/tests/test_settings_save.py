#------------------------------------------------------------------
# 설정 되쓰기 시험 (설계서 §18 — 패널 너비 저장)
#=> 이 INI 는 설명 주석이 본문만큼 중요하다. 값을 바꾸면서도 주석·순서·다른 항목이
#   그대로 남는지 본다. 진짜 설정 파일은 건드리지 않고 임시 파일로만 시험한다.
#------------------------------------------------------------------
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings as rsb_settings  # noqa: E402

SAMPLE = """; 맨 위 설명
[SimpleRAG]
SimpleRagExe     =

[Window]
; off = 검색창 아래 / right = 오른쪽에 붙음
Dock             = right
Width            = 460
MaxHeight        = 560
FontSize         = 10

[Log]
Level            = INFO
"""


#------------------------------------------------------------------
# 임시 INI 만들기
#
# -in: text = 파일 내용
#
# -out: 경로
# -out: error = 없음
#------------------------------------------------------------------
def make_ini(text=SAMPLE):
    d = tempfile.mkdtemp(prefix="rsb_ini_")
    p = os.path.join(d, "RAGSearchBox.ini")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


#------------------------------------------------------------------
# 파일 내용 읽기 (손잡이를 흘리지 않게)
#
# -in: p = 경로
#
# -out: 글자
# -out: error = 없음
#------------------------------------------------------------------
def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


class TestSaveWindowWidth(unittest.TestCase):
    #--------------------------------------------------------------
    # 값만 바뀌고 주석·다른 항목은 그대로 남는다
    #--------------------------------------------------------------
    def test_keeps_comments(self):
        p = make_ini()
        ok, detail = rsb_settings.save_window_width(p, 640)
        self.assertTrue(ok, detail)
        after = read(p)
        self.assertIn("Width            = 640", after)
        self.assertIn("; 맨 위 설명", after)
        self.assertIn("; off = 검색창 아래 / right = 오른쪽에 붙음", after)
        self.assertIn("Dock             = right", after)
        self.assertIn("MaxHeight        = 560", after)
        self.assertIn("[Log]", after)

    #--------------------------------------------------------------
    # 저장한 값을 다시 읽으면 그대로 나온다
    #--------------------------------------------------------------
    def test_roundtrip(self):
        p = make_ini()
        rsb_settings.save_window_width(p, 700)
        s = rsb_settings.load(p)
        self.assertEqual(s.win_width, 700)

    #--------------------------------------------------------------
    # [Window] 는 있는데 Width 가 없으면 만들어 넣는다
    #--------------------------------------------------------------
    def test_missing_key(self):
        p = make_ini("[Window]\nDock             = right\n")
        ok, _ = rsb_settings.save_window_width(p, 520)
        self.assertTrue(ok)
        self.assertEqual(rsb_settings.load(p).win_width, 520)

    #--------------------------------------------------------------
    # [Window] 자체가 없으면 구역째 만들어 붙인다
    #--------------------------------------------------------------
    def test_missing_section(self):
        p = make_ini("[Log]\nLevel            = INFO\n")
        ok, _ = rsb_settings.save_window_width(p, 520)
        self.assertTrue(ok)
        after = read(p)
        self.assertIn("[Window]", after)
        self.assertIn("[Log]", after)
        self.assertEqual(rsb_settings.load(p).win_width, 520)

    #--------------------------------------------------------------
    # 다른 구역의 Width 는 건드리지 않는다
    #=> 앞으로 [Panel] 같은 데 같은 이름이 생겨도 헷갈리지 않아야 한다.
    #--------------------------------------------------------------
    def test_other_section_untouched(self):
        p = make_ini("[Panel]\nWidth            = 111\n\n[Window]\nWidth            = 460\n")
        rsb_settings.save_window_width(p, 800)
        after = read(p)
        self.assertIn("[Panel]\nWidth            = 111", after)
        self.assertIn("[Window]\nWidth            = 800", after)

    #--------------------------------------------------------------
    # 범위 밖 값·이상한 값·없는 파일은 쓰지 않는다
    #=> 잘못된 값을 적어 두면 다음 실행 때 경고와 함께 기본값으로 되돌아간다.
    #   애초에 쓰지 않는 편이 낫다.
    #--------------------------------------------------------------
    def test_rejects_bad(self):
        p = make_ini()
        for bad in (10, 5000, "넓게", None):
            ok, why = rsb_settings.save_window_width(p, bad)
            self.assertFalse(ok, bad)
            self.assertTrue(why)
        self.assertIn("Width            = 460", read(p))

        ok, why = rsb_settings.save_window_width(r"D:\없는폴더\RAGSearchBox.ini", 500)
        self.assertFalse(ok)
        self.assertIn("없어", why)

    #--------------------------------------------------------------
    # 같은 값을 여러 번 써도 파일이 망가지지 않는다
    #--------------------------------------------------------------
    def test_repeat(self):
        p = make_ini()
        for w in (500, 600, 600, 700):
            self.assertTrue(rsb_settings.save_window_width(p, w)[0])
        after = read(p)
        self.assertEqual(after.count("Width"), 1)
        self.assertEqual(rsb_settings.load(p).win_width, 700)


if __name__ == "__main__":
    unittest.main()
