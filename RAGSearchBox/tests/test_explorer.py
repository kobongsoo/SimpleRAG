#------------------------------------------------------------------
# 탐색기 위치 판정 시험 (설계서 §5-1)
#=> 실제 탐색기를 띄우지 않고 순수 판정 함수만 본다. 넣는 값은 P0-9 에서 실제로 읽은 모양 그대로다.
#   - 폴더 탭        : url=file:///…, name=폴더이름
#   - 검색 결과 탭   : url="",        name=검색어 자체
#   - 가상 폴더(내 PC): url="",        name="내 PC"
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import explorer  # noqa: E402


#------------------------------------------------------------------
# 시험용 항목 만들기
#=> 실제 ExplorerLocator.entries() 가 만드는 모양과 같게 맞춘다.
#
# -in: url  = LocationURL
# -in: name = LocationName
#
# -out: 항목 사전
# -out: error = 없음
#------------------------------------------------------------------
def entry(url, name):
    return {"url": url, "name": name, "path": explorer.url_to_path(url)}


class TestUrlToPath(unittest.TestCase):
    #--------------------------------------------------------------
    # 보통 드라이브 경로
    #--------------------------------------------------------------
    def test_drive(self):
        self.assertEqual(explorer.url_to_path("file:///D:/문서/인사"), "D:\\문서\\인사")

    #--------------------------------------------------------------
    # 퍼센트 인코딩된 한글·공백이 풀린다
    #--------------------------------------------------------------
    def test_encoded(self):
        self.assertEqual(explorer.url_to_path("file:///C:/%EA%B7%9C%EC%A0%95/a%20b"),
                         "C:\\규정\\a b")

    #--------------------------------------------------------------
    # 네트워크 경로는 \\서버\공유 가 된다
    #--------------------------------------------------------------
    def test_unc(self):
        self.assertEqual(explorer.url_to_path("file://server/share/규정"),
                         "\\\\server\\share\\규정")

    #--------------------------------------------------------------
    # 검색 결과·가상 폴더는 주소가 비어 있어 경로가 없다
    #--------------------------------------------------------------
    def test_empty(self):
        self.assertIsNone(explorer.url_to_path(""))
        self.assertIsNone(explorer.url_to_path(None))
        self.assertIsNone(explorer.url_to_path("search-ms:query=x"))


class TestClassify(unittest.TestCase):
    #--------------------------------------------------------------
    # 주소가 있으면 폴더
    #--------------------------------------------------------------
    def test_folder(self):
        self.assertEqual(explorer.classify(entry("file:///D:/a", "a"), "?질문"),
                         explorer.KIND_FOLDER)

    #--------------------------------------------------------------
    # 주소가 비고 이름이 검색어와 같으면 검색 결과 화면 (P0-9 실측)
    #--------------------------------------------------------------
    def test_search(self):
        self.assertEqual(explorer.classify(entry("", "?범위"), "?범위"),
                         explorer.KIND_SEARCH)

    #--------------------------------------------------------------
    # 주소가 비었는데 검색어와 다르면 가상 폴더 — 내 PC 가 여기 해당
    #--------------------------------------------------------------
    def test_virtual(self):
        self.assertEqual(explorer.classify(entry("", "내 PC"), "?범위"),
                         explorer.KIND_VIRTUAL)
        # 검색어를 모르면 검색 결과인지 알 수 없으므로 가상 폴더로 본다(보수적)
        self.assertEqual(explorer.classify(entry("", "?범위"), None),
                         explorer.KIND_VIRTUAL)


class TestPickActive(unittest.TestCase):
    A = "C:\\work\\scope\\A\\docs"
    B = "C:\\work\\scope\\B\\docs"
    C = "C:\\work\\scope\\C"

    #--------------------------------------------------------------
    # 탭이 하나면 그것이 답이다
    #--------------------------------------------------------------
    def test_single_tab(self):
        paths, _ = explorer.pick_active([entry("file:///C:/work/scope/A/docs", "docs")],
                                        [self.A], "?질문")
        self.assertEqual(paths, [self.A])

    #--------------------------------------------------------------
    # 전체 경로 제목으로 같은 이름 폴더 두 탭을 가린다 (P0-9 실측 상황)
    #--------------------------------------------------------------
    def test_two_same_name_fullpath_title(self):
        ents = [entry("file:///C:/work/scope/A/docs", "docs"),
                entry("file:///C:/work/scope/B/docs", "docs")]
        paths, why = explorer.pick_active(ents, [self.B, self.A], "?질문")
        self.assertEqual(paths, [self.B], why)

    #--------------------------------------------------------------
    # 전체 경로 표시가 꺼진 PC — 제목이 폴더 이름뿐이라 둘 다 후보로 남는다
    #=> 이때는 "후보가 전부 범위 안일 때만" 이라는 보수 규칙에 맡긴다.
    #--------------------------------------------------------------
    def test_two_same_name_short_title(self):
        ents = [entry("file:///C:/work/scope/A/docs", "docs"),
                entry("file:///C:/work/scope/B/docs", "docs")]
        paths, why = explorer.pick_active(ents, ["docs", "docs"], "?질문")
        self.assertEqual(sorted(paths), sorted([self.A, self.B]))
        self.assertIn("후보", why)

    #--------------------------------------------------------------
    # 이름이 다르면 제목이 짧아도 가려진다
    #--------------------------------------------------------------
    def test_diff_name_short_title(self):
        ents = [entry("file:///C:/work/scope/C", "C"),
                entry("file:///C:/work/scope/B/docs", "docs")]
        paths, _ = explorer.pick_active(ents, ["C", "docs"], "?질문")
        self.assertEqual(paths, [self.C])

    #--------------------------------------------------------------
    # 검색 결과 탭 — 기억해 둔 폴더를 쓴다
    #=> 제목이 "?범위 - C의 검색 결과" 이고 기억한 폴더 이름 C 가 제목에 있으므로 인정한다.
    #--------------------------------------------------------------
    def test_search_tab_uses_memory(self):
        ents = [entry("", "?범위"), entry("file:///C:/work/scope/B/docs", "docs")]
        titles = ["?범위 - C의 검색 결과", self.B]
        paths, _ = explorer.pick_active(ents, titles, "?범위", remembered=self.C)
        self.assertEqual(paths, [self.C])

    #--------------------------------------------------------------
    # 기억한 폴더가 이 탭 것이 아니면 쓰지 않는다
    #=> 다른 탭의 폴더를 기억하고 있을 수 있어, 제목에 그 폴더 이름이 없으면 모른다고 답한다.
    #--------------------------------------------------------------
    def test_search_tab_wrong_memory(self):
        ents = [entry("", "?범위"), entry("file:///C:/work/scope/B/docs", "docs")]
        titles = ["?범위 - C의 검색 결과", self.B]
        paths, _ = explorer.pick_active(ents, titles, "?범위", remembered=self.A)
        self.assertEqual(paths, [None])

    #--------------------------------------------------------------
    # 기억이 아예 없으면 모른다 (범위 밖으로 처리된다)
    #--------------------------------------------------------------
    def test_search_tab_no_memory(self):
        paths, _ = explorer.pick_active([entry("", "?범위")], ["?범위 - C의 검색 결과"],
                                        "?범위")
        self.assertEqual(paths, [None])

    #--------------------------------------------------------------
    # 내 PC 같은 가상 폴더는 언제나 모른다 = 범위 밖
    #--------------------------------------------------------------
    def test_virtual_folder(self):
        paths, _ = explorer.pick_active([entry("", "내 PC")], ["내 PC"], "?질문",
                                        remembered=self.C)
        self.assertEqual(paths, [None])

    #--------------------------------------------------------------
    # 목록이 비면 아무 후보도 없다
    #--------------------------------------------------------------
    def test_no_entries(self):
        paths, why = explorer.pick_active([], [], "?질문")
        self.assertEqual(paths, [])
        self.assertIn("없음", why)

    #--------------------------------------------------------------
    # 제목을 못 읽는 경우(Windows 10 처럼 탭이 없는 창)에도 탭 1개면 답이 나온다
    #--------------------------------------------------------------
    def test_no_titles_single(self):
        paths, _ = explorer.pick_active([entry("file:///C:/work/scope/C", "C")], [], "?질문")
        self.assertEqual(paths, [self.C])


class TestTitleMatches(unittest.TestCase):
    #--------------------------------------------------------------
    # 대소문자와 끝 구분자가 달라도 같은 경로로 본다
    #--------------------------------------------------------------
    def test_case_and_sep(self):
        e = entry("file:///C:/Work/Scope/C", "C")
        self.assertTrue(explorer.title_matches(e, explorer.KIND_FOLDER, "c:\\work\\scope\\c\\"))

    #--------------------------------------------------------------
    # 제목이 비면 맞는 것이 없다
    #--------------------------------------------------------------
    def test_empty_title(self):
        e = entry("file:///C:/work/scope/C", "C")
        self.assertFalse(explorer.title_matches(e, explorer.KIND_FOLDER, ""))


if __name__ == "__main__":
    unittest.main()
