#------------------------------------------------------------------
# 사본 제거가 파일명 접두를 무시하는지 회귀 테스트 (REPORT §36.6 · 계획서 1-1)
#=> 같은 규정이 두 파일로 있으면 청크 본문·절 제목은 같고 `[파일명 > 절]` 접두만 다르다.
#   예전 dedup_key 는 접두까지 비교해 사본을 못 걸렀다. 모델·인덱스 없이 문자열과
#   가짜 payload 로만 확인한다.
#
#   실행: .venv/Scripts/python.exe bench/test_dedup_title.py
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                        # noqa: E402
from simplerag.retrieve.hybrid import HybridRetriever, dedup_key, pick_unique   # noqa: E402
from simplerag.structure import make_prefix                         # noqa: E402

FAILED = []
TOTAL = [0]

NAME_A = "14.유형자산관리지침_19.03.29.doc"
NAME_B = "58_유형자산 관리 지침-2019.03.29-변경안.doc"
BODY = "나) 회사에서 폐기를 결정한 “개인 사용 자산” 중 데스크탑(모니터 포함)의 경우, 무상으로 제공한다."


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 돌린다
#
# -in: name = 테스트 이름
# -in: cond = 참이어야 하는 조건
# -in: note = 실패 시 함께 출력할 설명
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 쌓는다)
#------------------------------------------------------------------
def check(name, cond, note=""):
    TOTAL[0] += 1
    print(("  OK   %s" if cond else "  FAIL %s  " + str(note)) % name)
    if not cond:
        FAILED.append(name)


#------------------------------------------------------------------
# 인덱서와 같은 모양의 청크 만들기
#=> 인덱서는 파일명에서 확장자를 뗀 제목으로 make_prefix 를 부른다(indexer.index_folder).
#
# -in: doc_name = 파일명, section = 절 제목(None 가능), body = 본문
#
# -out: 청크 문자열
# -out: error = 예외 없음
#------------------------------------------------------------------
def chunk(doc_name, section, body):
    title = doc_name.rsplit(".", 1)[0]
    return make_prefix(title, section) + body


#------------------------------------------------------------------
# dedup_key 단위 검사
#=> 접두만 다른 사본은 같은 키, 절이 다르거나 본문이 다르면 다른 키인지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_key():
    print("\n[dedup_key — 파일명 접두 무시]")
    a = chunk(NAME_A, "9. 폐기", BODY)
    b = chunk(NAME_B, "9. 폐기", BODY)
    check("사본(파일명만 다름, 같은 절) → 같은 키", dedup_key(a, NAME_A) == dedup_key(b, NAME_B))
    check("doc_name 없이 부르면 종전처럼 다른 키(하위 호환)", dedup_key(a) != dedup_key(b))

    c = chunk(NAME_B, "8. 배정", BODY)
    check("본문이 같아도 절 제목이 다르면 다른 키", dedup_key(a, NAME_A) != dedup_key(c, NAME_B))

    d = chunk(NAME_B, "9. 폐기", BODY.replace("무상으로", "유상으로"))
    check("본문이 한 글자라도 다르면 다른 키", dedup_key(a, NAME_A) != dedup_key(d, NAME_B))

    e = chunk(NAME_B, "9. 폐기", BODY.replace(" ", "   ").replace(",", ",\n"))
    check("공백·줄바꿈 차이는 무시", dedup_key(a, NAME_A) == dedup_key(e, NAME_B))

    # 작은 절을 합친 청크 — 머리가 여러 개(§36 에서 300청크 중 53개)
    merged_a = chunk(NAME_A, "3. 신청서류", "서류 1부\n\n") + chunk(NAME_A, "4. 휴가일수", "공휴일 제외")
    merged_b = chunk(NAME_B, "3. 신청서류", "서류 1부\n\n") + chunk(NAME_B, "4. 휴가일수", "공휴일 제외")
    check("머리가 여러 개인 청크도 모든 접두를 무시", dedup_key(merged_a, NAME_A) == dedup_key(merged_b, NAME_B))

    only_a = chunk(NAME_A, None, BODY)          # 절 제목이 없으면 접두가 "[제목]"
    only_b = chunk(NAME_B, None, BODY)
    check("절 없는 접두 [제목] 도 무시", dedup_key(only_a, NAME_A) == dedup_key(only_b, NAME_B))
    check("절 없는 접두와 절 있는 접두는 구분", dedup_key(only_a, NAME_A) != dedup_key(a, NAME_A))

    dotted = "25.출장여비규정_16.04.01.doc"      # 파일명에 점이 여러 개 — 확장자만 뗀다
    f1 = chunk(dotted, "6.1.2.", "일비는 여행일수에 따라 지급")
    f2 = chunk("25_출장여비규정_사본.doc", "6.1.2.", "일비는 여행일수에 따라 지급")
    check("점이 많은 파일명도 제목 추출이 인덱서와 같다", dedup_key(f1, dotted) == dedup_key(f2, "25_출장여비규정_사본.doc"))

    plain = "접두 없는 고정 청킹 본문"
    check("접두 없는 청크는 그대로", dedup_key(plain, NAME_A) == dedup_key(plain))
    check("빈 청크는 None(중복 판정 제외)", dedup_key("", NAME_A) is None and dedup_key(None, NAME_A) is None)

    other_title = chunk(NAME_B, "9. 폐기", BODY)
    check("다른 파일명으로 부르면 접두가 남는다(엉뚱한 제목은 안 지움)",
          dedup_key(other_title, NAME_A) != dedup_key(a, NAME_A))


#------------------------------------------------------------------
# _dedup 절차 검사
#=> 융합 순위에서 사본을 건너뛰고 다음 후보로 채우는지, 모자라면 사본으로라도 채우는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_dedup_order():
    print("\n[HybridRetriever._dedup — 사본이 근거 칸을 차지하지 않음]")
    r = object.__new__(HybridRetriever)          # 모델·저장소 없이 메서드만 쓴다
    payloads = {
        1: {"text": chunk(NAME_A, "9. 폐기", BODY), "doc_name": NAME_A},
        2: {"text": chunk(NAME_B, "9. 폐기", BODY), "doc_name": NAME_B},       # 1 의 사본
        3: {"text": chunk(NAME_A, "3. 노트북의 구매", "구매한도 2,000,000원"), "doc_name": NAME_A},
        4: {"text": chunk(NAME_B, "3. 노트북의 구매", "구매한도 2,000,000원"), "doc_name": NAME_B},  # 3 의 사본
        5: {"text": chunk(NAME_A, "7. 반납", "퇴사 시 팀장이 회수하여 반납"), "doc_name": NAME_A},
    }
    got = r._dedup([1, 2, 3, 4, 5], payloads, 3)
    check("사본 2·4 를 건너뛰고 1·3·5", got == [1, 3, 5], got)

    got = r._dedup([2, 1, 5], payloads, 2)
    check("순위는 보존 — 먼저 온 사본(2)이 남는다", got == [2, 5], got)

    got = r._dedup([1, 2, 3, 4], payloads, 3)
    check("고유 후보가 모자라면 사본으로 채운다(근거 3건 유지)", got == [1, 3, 2], got)

    got = r._dedup([1, 2, 3], {1: payloads[1], 2: {"text": payloads[2]["text"]}, 3: payloads[3]}, 3)
    check("doc_name 이 빠진 payload 는 종전 규칙(접두 포함 비교)", got == [1, 2, 3], got)


#------------------------------------------------------------------
# 가짜 리랭커
#=> 본문에 든 숫자 태그로 점수를 정한다. 같은 본문(사본)은 반드시 같은 점수 — 실제 크로스인코더와 같다.
# -필드: calls = 받은 근거 목록 기록
#------------------------------------------------------------------
class FakeReranker:
    #------------------------------------------------------------------
    # 초기화
    #=> 호출 기록 목록을 만든다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self):
        self.calls = []

    #------------------------------------------------------------------
    # 점수 매기기
    #=> "점수=0.9" 같은 태그를 읽어 점수로 쓴다. 파일명 접두는 점수에 영향이 없다.
    #
    # -in: query = 질의(무시), texts = 근거 본문 목록
    #
    # -out: 점수 목록
    # -out: error = 없음
    #------------------------------------------------------------------
    def score(self, query, texts):
        self.calls.append(list(texts))
        import re
        return [float(re.search(r"점수=([\d.]+)", t).group(1)) for t in texts]


#------------------------------------------------------------------
# 리랭킹 뒤 사본 제거 검사 (§37 e11 — g014·g018·g019)
#=> 풀이 사본으로 채워져도 최종 근거 3칸에 같은 본문이 두 번 들어가지 않는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_after_rerank():
    print("\n[search — 리랭킹 뒤에도 사본을 거른다]")

    def c(pid, name, sec, body):
        return {"id": pid, "text": chunk(name, sec, body), "doc_name": name}

    # _dedup 이 고유 후보 부족으로 사본(2)을 풀 뒤쪽에 채운 상황을 그대로 만든다
    pool = [c(1, NAME_A, "3. 노트북의 구매", "파손 수리 점수=0.9"),
            c(3, NAME_A, "6. 워크스테이션", "구매한도 점수=0.5"),
            c(4, NAME_A, "10. 기타", "전산소모품 점수=0.4"),
            c(2, NAME_B, "3. 노트북의 구매", "파손 수리 점수=0.9")]     # 1 의 사본
    saved = (config.RERANK_POOL, config.DEDUP_EVIDENCE)
    try:
        config.RERANK_POOL, config.DEDUP_EVIDENCE = 4, True
        r = HybridRetriever(None, None, None, reranker=FakeReranker())
        r._search = lambda q, k, fs=None: (pool, {"total_ms": 1.0})
        got, _ = r.search("q", top_k=3)
        check("사본(2)이 원본(1)과 같은 점수여도 근거에 한 번만", [x["id"] for x in got] == [1, 3, 4],
              [x["id"] for x in got])

        # 고유 2개(1·3) + 사본 2개(2·5) — 풀이 top_k 보다 커야 리랭킹 경로를 탄다
        short = [pool[0], pool[1], pool[3], c(5, NAME_B, "6. 워크스테이션", "구매한도 점수=0.5")]
        r._search = lambda q, k, fs=None: (short, {"total_ms": 1.0})
        got, _ = r.search("q", top_k=3)
        check("고유 후보가 top_k 보다 적으면 사본으로 채운다", [x["id"] for x in got] == [1, 3, 2],
              [x["id"] for x in got])

        config.DEDUP_EVIDENCE = False
        r._search = lambda q, k, fs=None: (pool, {"total_ms": 1.0})
        got, _ = r.search("q", top_k=3)
        check("사본 제거를 끄면(SIMPLERAG_DEDUP=0) 점수 순 그대로", [x["id"] for x in got] == [1, 2, 3],
              [x["id"] for x in got])
    finally:
        config.RERANK_POOL, config.DEDUP_EVIDENCE = saved

    ranked = [{"text": chunk(NAME_A, "s", "x"), "doc_name": NAME_A},
              {"text": chunk(NAME_B, "s", "x"), "doc_name": NAME_B},
              {"text": chunk(NAME_B, "t", "y"), "doc_name": NAME_B}]
    check("pick_unique — 파일명만 다른 사본은 건너뛴다", pick_unique(ranked, 2) == [ranked[0], ranked[2]])


#------------------------------------------------------------------
# 테스트 진입점
#=> 키 단위 검사와 _dedup 절차 검사를 돌리고 실패 수를 종료 코드로 돌려준다.
#
# -in: 없음
#
# -out: 0 = 전부 통과, 1 = 실패 있음
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_key()
    test_dedup_order()
    test_after_rerank()
    print("\n결과: %d 항목 중 실패 %d" % (TOTAL[0], len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
