#------------------------------------------------------------------
# 출력 정리 회귀 시험 — 인용 번호 파싱 + 스트리밍 정리
#=> 이 시험이 존재하는 이유가 곧 시험 설계다.
#
#   §18 에도 스트리밍 단위 시험이 있었다. 토큰을 1·3·7·40자 단위로 흘려 넣는
#   시험이다. 그런데 **시험 문자열이 67자인데 보류 구간이 90자**라, 어떤 단위로
#   넣든 `finish()` 시점에 화면으로 나간 것이 0자였다. 그래서 문제의 판정문이
#   한 번도 실행되지 않았고, 결함이 그대로 통과했다(REPORT §18.4 정정).
#
#   → **경계값을 가진 코드는 그 경계를 넘는 입력으로 시험한다.**
#      여기서는 보류 구간(90자)보다 긴 입력을 반드시 포함한다.
#
#   사용: python bench/test_output_cleanup.py
#------------------------------------------------------------------

import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.stdout.reconfigure(encoding="utf-8")

import simplerag.cli as cli                                       # noqa: E402
from simplerag.generate.prompts import (drop_invalid_cites,       # noqa: E402
                                        parse_answer,
                                        strip_instruction_echo)

HOLD = cli.StreamWriter().hold          # 보류 구간(현재 90자)


#------------------------------------------------------------------
# 스트리밍 한 번 흉내내기
#=> 토큰을 잘게 흘려 넣고 finish 로 마무리한 뒤, 화면에 나간 전체를 돌려준다.
#
# -in: raw       = 모델이 뱉을 원문
# -in: n         = 근거 개수
# -in: step      = 한 번에 흘려 넣을 글자 수
#
# -out: (화면 출력 전체, finish 직전에 이미 나가 있던 글자 수)
# -out: error = 없음
#------------------------------------------------------------------
def stream(raw, n, step=7):
    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        w = cli.StreamWriter()
        for i in range(0, len(raw), step):
            w.write(raw[i:i + step])
        emitted = len(w._emitted)
        w.finish(lambda t: strip_instruction_echo(t, n))
    finally:
        sys.stdout = real
    return buf.getvalue(), emitted


ECHO = "근거 번호 [1] 형식으로 표기하세요."

# 짧은 답변 — 보류 구간 안에 전부 들어간다(§18 이 시험한 유일한 경우)
SHORT = "리프레시 휴가비는 유급휴가 2일과 휴가비를 지급합니다. [1]  " + ECHO

# 🔴 보류 구간을 넘는 답변 — §18 이 만들지 못했던 상태
LONG = ("[1] 원격지 근무자에게는 숙박비, 운임, 일비, 식비를 지급한다.  \n"
        "[2] 원격지 근무자에게는 숙박비, 운임, 일비, 식비를 지급한다.  \n"
        "[12] 원격지 근무자 비품 구매 관련 지침_22.01.25 > 1. 대상자  \n"
        "원격지 근무자(회사 비용으로 숙박시설을 마련해 주는 경우를 말함)  \n\n"
        + ECHO)

fail = []


#------------------------------------------------------------------
# 단언 한 건
#=> 실패해도 멈추지 않고 모아서 마지막에 한 번에 보여 준다.
#
# -in: ok   = 판정 결과
# -in: name = 시험 이름
# -in: hint = 실패 시 덧붙일 설명(없으면 생략)
#
# -out: 없음 (전역 fail 목록에 쌓는다)
#------------------------------------------------------------------
def check(ok, name, hint=""):
    print("%s %s" % ("✅" if ok else "🔴", name))
    if not ok:
        fail.append(name)
        if hint:
            print("     " + hint)


print("── 1. 인용 번호 파싱 ".ljust(60, "─"))
CASES = [
    ("실제 관측 — 근거 3건에 [12]", LONG, 3, [1, 2]),
    ("개수 미지정 시 종전 동작 유지", LONG, None, [1, 2, 12]),
    ("정상 인용은 보존", "본문입니다. [1][3]", 3, [1, 3]),
    ("경계값 — 마지막 번호 [3] 보존", "본문 [3]", 3, [3]),
    ("경계값 — 바로 위 [4] 제거", "본문 [4] 입니다", 3, []),
    ("0 은 인용이 아니다", "본문 [0] 입니다", 3, []),
]
for name, raw, n, want in CASES:
    body, cited = parse_answer(raw, n)
    check(cited == want, name, "cited=%s (기대 %s)" % (cited, want))

check("휴가는 5일입니다" in drop_invalid_cites("휴가는 5일입니다 [9]", 3),
      "무효 인용을 걷어내도 본문은 남는다")

# 🔴 대괄호 안이 인용이 아니라 **값**인 경우. 지우면 숫자가 사라진다.
#    204문항 실측에서 실제로 나왔다(REPORT §24.8).
check(drop_invalid_cites("사업예산은 [1]에 따르면, [75]억 원입니다.", 3)
      == "사업예산은 [1]에 따르면, 75억 원입니다.",
      "값으로 쓰인 [75]억 → 75억 (숫자를 버리지 않는다)")
check("75" in drop_invalid_cites("예산은 [75]억 원", 3),
      "범위 밖 숫자라도 내용은 절대 잃지 않는다")

print()
print("── 2. 스트리밍 정리 (경계 넘는 입력 포함) ".ljust(60, "─"))

out, emitted = stream(SHORT, 3)
check(emitted == 0, "짧은 답변은 화면에 나간 것이 없다 (emitted=%d)" % emitted)
check("형식으로 표기" not in out, "짧은 답변 — 되뱉기 제거")

for step in (1, 3, 7, 40):
    out, emitted = stream(LONG, 3, step)
    # 🔴 이 단언이 §18 시험에 없었다. 경계를 넘지 않으면 결함이 드러나지 않는다.
    check(emitted > 0,
          "%2d자씩 — 보류 구간(%d)을 넘겨 화면에 이미 나간 상태를 만든다 (emitted=%d)"
          % (step, HOLD, emitted))
    check("형식으로 표기" not in out,
          "%2d자씩 — 여러 줄 답변에서도 되뱉기 제거" % step,
          "꼬리=%r" % out[-40:])

print()
print("── 3. 스트리밍의 한계 (고칠 수 없는 것을 명시한다) ".ljust(60, "─"))
early = "[12] 앞쪽에 찍힌 잘못된 인용. " + "본문이 길게 이어진다. " * 20 + "\n" + ECHO
out, _ = stream(early, 3)
check("[12]" in out, "이미 화면에 나간 인용은 지울 수 없다 (의도된 한계)")
check("형식으로 표기" not in out, "그래도 꼬리 되뱉기는 제거된다")
check(parse_answer(early, 3)[1] == [], "cited 목록은 언제나 정확하다")

print()
print("=" * 60)
print("실패 %d건" % len(fail))
for f in fail:
    print("   " + f)
raise SystemExit(1 if fail else 0)
