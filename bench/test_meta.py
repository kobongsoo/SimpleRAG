#------------------------------------------------------------------
# 메타데이터 정규화 단위 시험 (메타데이터 검색 설계서 §7~§10, 1단계)
#=> 실측(D:\분류함)에서 본 지저분한 값들이 규칙대로 바뀌는지 확인한다. 파일을 읽지 않는다.
#   사용: .venv\Scripts\python.exe bench\test_meta.py
#------------------------------------------------------------------

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from simplerag.meta import normalize as N  # noqa: E402
from simplerag.meta import store as S  # noqa: E402
from simplerag.meta.rules import load_rules  # noqa: E402

R = load_rules(None)


#------------------------------------------------------------------
# 사람 이름 정규화 (§7-1)
#=> 순서 바뀐 한글 이름 · 직함 · 소속 · 이메일 · hex · 모지바케 · 쓰레기 값을 확인한다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 기대와 다르면 AssertionError
#------------------------------------------------------------------
def test_person():
    ok = {
        "봉수 고": "고봉수", "고 봉수": "고봉수", "고봉수 과장": "고봉수", "고봉수님": "고봉수",
        "김은지(Eline)": "김은지", "이경준 매니저 정보화전략팀": "이경준", "<C0AFBFACC1A4>": "유연정",
        "Han, Hyun Bae": "han hyun bae", "sdchoi@mocomsys.com": "sdchoi", "jungcw": "jungcw",
        "Windows 사용자;juyeong.choi@dncompany.com": "juyeong.choi",
    }
    for raw, want in ok.items():
        got = N.norm_person(raw, R)
        assert got and got[0] == want, (raw, got)
    for junk in ["User", "Microsoft 계정", "m", "PC", "Administrator", "7963", "ê³€ë´›ì‹Ÿ",
                 "Keysight Technologies", "INSUNG DATA INC.", "PC301", "MND-PC", "PptxGenJS", "(주)한글과컴퓨터"]:
        assert N.norm_person(junk, R) is None, junk
    # 별칭 사전 — 계정 · 영문 이름(낱말 순서 무시) → 대표 이름
    al = {"bong9": "고봉수", "cho kang lae": "조강래"}     # load_aliases 는 낱말을 정렬한 키도 넣는다
    assert N.norm_person("bong9", R, al) == ("고봉수", "alias")
    assert N.norm_person("Kang Lae Cho", R, al) == ("조강래", "alias")


#------------------------------------------------------------------
# 날짜 해석 (§8-1)
#=> 실측 형식 다섯 가지와 이상값을 확인한다(UTC epoch).
#------------------------------------------------------------------
def test_dates():
    from datetime import datetime, timezone

    def utc(*a):
        return int(datetime(*a, tzinfo=timezone.utc).timestamp())
    assert N.parse_date("D:20100823131638+09'00'") == utc(2010, 8, 23, 4, 16, 38)
    assert N.parse_date("D:20100823131638") == utc(2010, 8, 23, 4, 16, 38)     # 오프셋 없음 = KST
    assert N.parse_date("2023-11-10T02:00:23Z") == utc(2023, 11, 10, 2, 0, 23)
    assert N.parse_date("2012-03-22T07:05:00") == utc(2012, 3, 22, 7, 5, 0)   # OLE FILETIME = UTC
    assert N.parse_date("2005년 10월 7일 금요일 오후 10:28:32") == utc(2005, 10, 7, 13, 28, 32)
    assert N.parse_date("Mon Jan 12 08:51:48 2004") == utc(2004, 1, 11, 23, 51, 48)
    assert N.parse_date("March 15, 2024 14:09") == utc(2024, 3, 15, 5, 9, 0)
    for bad in ["1900-12-31T15:00:00", "1601-01-01 09:00:00", "2999-01-01T00:00:00Z", "", None]:
        assert N.parse_date(bad) is None, bad


#------------------------------------------------------------------
# 제목 (§9) — 쓰레기 속성 제목 · 파일명 정리본 · 대표 제목 고르기
#------------------------------------------------------------------
def test_titles():
    for junk in ["슬라이드 1", "PowerPoint 프레젠테이션", "ARIA2.dvi", "3", "GabrickWeiss.book", "과정명"]:
        assert N.clean_prop_title(junk, R, False) is None, junk
    assert N.clean_prop_title("Microsoft Word - 경조사지원규정.doc", R, False) is None
    assert N.clean_prop_title("취  업  규  칙", R, False) == "취업규칙"
    assert N.clean_prop_title("유형자산 관리 규정", R, True) is None          # 상속 문서의 제목은 버린다
    assert N.clean_filename("25.출장여비규정_16.04.01.pdf") == "출장여비규정"
    assert N.clean_filename("11_M드라이브 V9 사용자 매뉴얼_20190915.pptx", versions=False) == "M드라이브 V9 사용자 매뉴얼"
    assert N.clean_filename("문서분류체계_v1.1.pptx") == "문서분류체계"
    assert N.clean_filename("09_설치확인서_2020525.pdf.pdf") == "설치확인서"
    # 비슷한 두 후보는 긴 쪽
    assert N.pick_title(None, "M드라이브 V9", "M드라이브 V9 사용자 매뉴얼", "x")[0] == "M드라이브 V9 사용자 매뉴얼"


#------------------------------------------------------------------
# 출처 · 유형 (§10)
#------------------------------------------------------------------
def test_origin_recv_type():
    assert N.origin_app({"producer": "Hancom PDF 1.3.0.538"}, "pdf", R) == "hwp"
    assert N.origin_app({"producer": "ApeosPort-VI C3371"}, "pdf", R) == "scanner"
    assert N.recv_kind("ezwebmail.bizmeka.com", R) == "mail"
    assert N.recv_kind("10.10.4.10", R) == "intranet"
    assert N.recv_kind("claude.ai", R) == "ai"
    assert N.recv_kind("example.com", R) == "web"
    dtype, _, form, _ = N.type_scores(["업무 처리 절차서 양식"], ["13_샘플_템플릿"], None, [], "docx", R)
    assert dtype == "절차서" and form


#------------------------------------------------------------------
# 템플릿 상속 판정 (§8-2) — 같은 (만든 이, 생성일) 쌍이 내용이 다른 문서 3개 이상
#------------------------------------------------------------------
def test_inherit():
    def raw(h):
        return {"doc": {"author": "jungcw", "created": "2011-02-07T02:01:21Z"}, "fs": {"sha1": h}}
    rows = [("a", raw("1")), ("b", raw("2")), ("c", raw("3")), ("d", {"doc": {"author": "x", "created": "y"}, "fs": {"sha1": "9"}})]
    assert S.inherited_paths(rows) == {"a", "b", "c"}
    # 같은 내용의 사본 셋은 상속이 아니다
    assert S.inherited_paths([("a", raw("1")), ("b", raw("1")), ("c", raw("1"))]) == set()


#------------------------------------------------------------------
# 사람 별칭 사전 (§7-2 · 3단계) — 이름 모양 근거 · 승인 · 무시 · 나
#=> 분류함 실측에서 본 짝으로 로마자 · 이름 일부 · 이니셜 판정을 보고,
#   임시 폴더에 people.yaml 을 써서 승인 → 별칭 적용 → 무시 → 빼기 를 확인한다.
#------------------------------------------------------------------
def test_people():
    import tempfile
    from simplerag.meta import people as P
    assert P.romanized_match("조강래", "cho kang lae")
    assert P.romanized_match("조강래", "kang-lae cho")
    assert P.romanized_match("김성수", "kim-sung-su")
    assert not P.romanized_match("김영민", "youngho yoon")       # 다른 사람(윤영호)
    assert P.partial_match("고봉수", "bong")
    assert P.partial_match("최성근", "skchoi")                    # 이름 이니셜 + 성
    assert not P.partial_match("이윤석", "yoon")                  # 흔한 성 표기만으로는 받지 않는다
    assert not P.partial_match("허성", "sung")
    assert P.initials_match("조강래", "ckl")
    assert P.initials_match("박규영", "pky")
    with tempfile.TemporaryDirectory() as root:
        P.approve(root, "bong9", "고봉수")
        al = N.load_aliases(root)
        assert N.norm_person("bong9", R, al) == ("고봉수", "alias")
        P.reject(root, "sung", "허성")
        d = P.load_user(root)
        assert d["people"] == {"고봉수": ["bong9"]} and ["sung", "허성"] in d["rejected"]
        P.remove(root, "bong9")
        assert N.norm_person("bong9", R, N.load_aliases(root)) == ("bong9", "account")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("통과:", name)
