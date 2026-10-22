#------------------------------------------------------------------
# 표 행 평문화 회귀 테스트 (계획서 3-3 · REPORT §46)
#=> 근거 표에서 모델이 옆 칸 값을 집는 실패(g048: 대리 단독부임 15,000 을 25,000 으로)를
#   없애려고, 표를 "머리글: 값" 행 평문으로 편다. 모델·인덱스 없이 문자열만으로 확인한다.
#
#   확인하는 것은 세 가지다.
#     1) 병합 셀 때문에 한 칸 밀린 행을 제대로 되돌리는가(g048 그 자체)
#     2) 밀리지 않은 행·이어지는 빈 칸을 **잘못 밀지 않는가**(과잉 교정이 더 나쁘다)
#     3) 해석할 수 없는 표는 원문 그대로 두는가(무손실)
#
#   실행: .venv/Scripts/python.exe bench/test_tableflat.py
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag.tableflat import flatten_tables          # noqa: E402

FAILED = []
TOTAL = [0]


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


# g048 이 실제로 받은 근거. 첫 칸이 비어 값이 한 칸씩 밀려 있다.
SHIFTED = (
    "[25.출장여비규정_16.04.01]\n"
    "| 구 분 | 가족부임시 | 단독부임시 | 비 고 |\n"
    "|  | 대리(선임) | 25,000 | 15,000 |\n"
    "|  | 사원 | 20,000 | 10,000 |"
)

# 같은 표의 다른 조각. 임원 행만 밀리지 않았다(병합 셀이 시작되는 행).
MIXED = (
    "별표3 국내 부임수당\n"
    "(단위 : 원)\n"
    "| 구 분 | 가족부임시 | 단독부임시 | 비 고 |\n"
    "| 임원 | 50,000 | 40,000 |\n"
    "|  | 부장(수석) | 40,000 | 30,000 |"
)


#------------------------------------------------------------------
# 밀린 행 되돌리기 — 3-3 의 핵심
#=> "대리(선임)" 이 이름표 열로 돌아와야 단독부임시가 15,000 으로 읽힌다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_shift():
    print("\n── 1. 병합 셀로 밀린 행 되돌리기 ".ljust(60, "─"))
    out = flatten_tables(SHIFTED)
    check("대리(선임)이 이름표 열로 온다", "구 분: 대리(선임)" in out, out)
    check("단독부임시가 15,000 으로 읽힌다", "단독부임시: 15,000" in out, out)
    check("가족부임시는 25,000 그대로", "가족부임시: 25,000" in out, out)
    check("사원 행도 같이 고쳐진다", "구 분: 사원 / 가족부임시: 20,000 / 단독부임시: 10,000" in out, out)
    check("표 앞의 출처 접두는 그대로", out.startswith("[25.출장여비규정_16.04.01]"), out)
    check("모든 행을 폈으면 머리글 줄은 뺀다", "| 구 분 |" not in out, out)

    out2 = flatten_tables(MIXED)
    check("밀리지 않은 임원 행은 그대로 해석", "구 분: 임원 / 가족부임시: 50,000 / 단독부임시: 40,000" in out2, out2)
    check("밀린 부장 행은 고쳐서 해석",
          "구 분: 부장(수석) / 가족부임시: 40,000 / 단독부임시: 30,000" in out2, out2)
    check("표 앞뒤 본문은 살아 있다", "별표3 국내 부임수당" in out2 and "(단위 : 원)" in out2, out2)
    check("비어 있는 비고 열은 만들지 않는다", "비 고:" not in out2, out2)


#------------------------------------------------------------------
# 잘못 밀지 않기 — 과잉 교정 방지
#=> 빈 첫 칸이 늘 밀림은 아니다. 위 행에서 이름표가 이어지는 경우가 있다.
#   그때 왼쪽으로 밀면 값이 통째로 한 칸씩 어긋난다(고치려다 더 나빠진다).
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_no_overshift():
    print("\n── 2. 잘못 밀지 않는다 ".ljust(60, "─"))
    cont = "| 구분 | A | B |\n| 임원 | 1 | 2 |\n|  | 3 | 4 |"
    out = flatten_tables(cont)
    check("이어지는 빈 칸은 밀지 않는다(3 은 A 값)", "A: 3 / B: 4" in out, out)
    check("이름표를 훔쳐 오지 않는다", "구분: 3" not in out, out)

    # 첫 열이 숫자 열인 표 — 이름표 열 규칙을 적용하면 안 된다.
    numeric_first = "| 1 | 2 | 3 |\n| 4 | 5 | 6 |"
    check("머리글이 없으면 손대지 않는다", flatten_tables(numeric_first) == numeric_first)


#------------------------------------------------------------------
# 해석할 수 없으면 원문 그대로 — 무손실
#=> 표처럼 보이지만 표가 아닌 것들이 섞여 들어온다. 그럴 때 손대면
#   내용이 사라지거나 엉뚱한 이름표가 붙는다. 원문 보존이 언제나 안전하다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_untouched():
    print("\n── 3. 해석할 수 없으면 원문 그대로 ".ljust(60, "─"))
    plain = "표가 없는 평범한 본문입니다. 숙박비는 70,000원입니다."
    check("표가 없으면 그대로", flatten_tables(plain) == plain)

    no_header = "| 본인 결혼 | 5 | 500,000 |\n| 자녀 결혼 | 1 | 300,000 |"
    check("머리글이 없으면 그대로", flatten_tables(no_header) == no_header)

    # 표 뒤 설명 문단이 마지막 셀에 통째로 딸려 오는 실제 사례.
    long_cell = (
        "| 구 분 | 가족부임시 | 단독부임시 | 비 고 |\n"
        "|  | 대리(선임) | 25,000 | 15,000 |\n"
        "|  | 연수목적 해외 출장도 일반 해외 출장과 동일하게 처리해도 됨. "
        "단, 외부로부터 지원받는 비용항목은 중복 지원하지 않는다. | 항공편 | 비고 |"
    )
    out = flatten_tables(long_cell)
    check("긴 칸이 든 행은 손대지 않는다", "연수목적 해외 출장도" in out and "| 구 분 |" in out, out)
    check("같은 표의 정상 행은 편다", "구 분: 대리(선임)" in out, out)
    check("원문 행이 남으면 머리글도 남긴다", "| 구 분 |" in out, out)

    check("빈 문자열도 안전", flatten_tables("") == "")
    check("None 도 안전", flatten_tables(None) is None)

    # 무손실 — 값 행의 칸이 하나도 사라지지 않는다.
    #   머리글 칸은 예외다. 값이 하나도 없는 열("비 고")은 이름표만 있으므로
    #   행을 다 펴고 나면 사라진다 — 지워진 내용이 아니라 빈 열의 이름이다.
    for src in (SHIFTED, MIXED, long_cell):
        flat = flatten_tables(src)
        rows = [ln.strip() for ln in src.split("\n") if ln.strip().startswith("|")]
        values = [c.strip() for ln in rows[1:]
                  for c in ln.strip("|").split("|") if c.strip()]
        missing = [v for v in values if v not in flat]
        check("값이 하나도 사라지지 않는다 (%s…)" % src[:12], not missing, missing)


#------------------------------------------------------------------
# 설정 연결 — 기본은 꺼져 있고 환경변수로 켠다
#=> 측정 중이라 기본값은 꺼 둔다. build_user_message 가 설정을 실제로 읽는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_switch():
    print("\n── 4. 설정 연결 ".ljust(60, "─"))
    from simplerag import config
    from simplerag.generate.prompts import build_user_message

    was = config.FLATTEN_TABLES
    try:
        config.FLATTEN_TABLES = False
        off = build_user_message("대리 단독부임 수당?", [SHIFTED])
        check("꺼져 있으면 표 원문 그대로", "| 구 분 |" in off, off[:120])

        config.FLATTEN_TABLES = True
        on = build_user_message("대리 단독부임 수당?", [SHIFTED])
        check("켜면 평문으로 들어간다", "구 분: 대리(선임)" in on, on[:120])
        check("근거 번호 매김은 그대로", on.startswith("[근거]\n[1] "), on[:60])
        check("질문 블록도 그대로", on.rstrip().endswith("대리 단독부임 수당?"), on[-60:])
    finally:
        config.FLATTEN_TABLES = was


#------------------------------------------------------------------
# 전체 실행
#
# -in: 없음
#
# -out: int = 실패가 있으면 1, 없으면 0
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_shift()
    test_no_overshift()
    test_untouched()
    test_switch()
    print("\n결과: %d 항목 중 실패 %d" % (TOTAL[0], len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
