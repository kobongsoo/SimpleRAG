#------------------------------------------------------------------
# scope 시험 — 폴더 범위 판정 (설계서 §5-1 R3)
#=> 경계·대소문자·하위 폴더·UNC 를 확인한다. 실제 폴더는 임시 폴더로 만들어 쓴다.
#------------------------------------------------------------------
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scope import Scope, is_under, norm  # noqa: E402


class TestIsUnder(unittest.TestCase):
    #--------------------------------------------------------------
    # 같은 폴더와 하위 폴더는 안으로 본다
    #--------------------------------------------------------------
    def test_same_and_child(self):
        self.assertTrue(is_under(r"D:\문서", r"D:\문서"))
        self.assertTrue(is_under(r"D:\문서\인사\2025", r"D:\문서"))
        self.assertTrue(is_under("D:\\문서\\", "D:\\문서"))

    #--------------------------------------------------------------
    # 이름이 겹치는 다른 폴더는 밖이다 (핵심 경계)
    #--------------------------------------------------------------
    def test_boundary(self):
        self.assertFalse(is_under(r"D:\문서함", r"D:\문서"))
        self.assertFalse(is_under(r"D:\문서함\인사", r"D:\문서"))

    #--------------------------------------------------------------
    # 대소문자는 구분하지 않는다
    #--------------------------------------------------------------
    def test_case(self):
        self.assertTrue(is_under(r"d:\Docs\HR", r"D:\docs"))

    #--------------------------------------------------------------
    # UNC 경로
    #--------------------------------------------------------------
    def test_unc(self):
        self.assertTrue(is_under(r"\\srv\share\규정\인사", r"\\srv\share\규정"))
        self.assertFalse(is_under(r"\\srv\share2\규정", r"\\srv\share\규정"))

    #--------------------------------------------------------------
    # 빈 값은 판정 불가로 본다
    #--------------------------------------------------------------
    def test_empty(self):
        self.assertFalse(is_under("", r"D:\문서"))
        self.assertFalse(is_under(r"D:\문서", ""))
        self.assertEqual(norm(""), "")


class TestScope(unittest.TestCase):
    #--------------------------------------------------------------
    # 실제 폴더로 포함 여부 확인
    #--------------------------------------------------------------
    def test_contains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "규정")
            inside = os.path.join(root, "인사")
            os.makedirs(inside)
            sc = Scope([root])
            self.assertTrue(sc.usable())
            self.assertTrue(sc.contains(inside)[0])
            ok, why = sc.contains(tmp)
            self.assertFalse(ok)
            self.assertEqual(why, "범위 밖")

    #--------------------------------------------------------------
    # 설정이 비었거나 폴더가 없으면 동작하지 않는다
    #--------------------------------------------------------------
    def test_unusable(self):
        sc = Scope([])
        self.assertFalse(sc.usable())
        self.assertEqual(sc.contains(r"D:\어디든")[1], "범위 폴더 미설정")

        sc2 = Scope([r"Z:\없는폴더_" + os.urandom(4).hex()])
        self.assertFalse(sc2.usable())

    #--------------------------------------------------------------
    # 폴더를 모르면(가상 폴더 등) 밖으로 본다
    #--------------------------------------------------------------
    def test_unknown_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = Scope([tmp])
            self.assertFalse(sc.contains(None)[0])
            self.assertFalse(sc.contains("")[0])

    #--------------------------------------------------------------
    # 없던 폴더가 생기면 다시 확인할 때 잡힌다 (네트워크 드라이브 대비)
    #--------------------------------------------------------------
    def test_refresh_picks_up_new_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            later = os.path.join(tmp, "나중에")
            sc = Scope([later], recheck_min=1)
            self.assertFalse(sc.usable())
            os.makedirs(later)
            self.assertTrue(sc.refresh(force=True))
            self.assertTrue(sc.usable())


if __name__ == "__main__":
    unittest.main()
