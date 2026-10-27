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
                                        soft_clean,
                                        strip_instruction_echo)

HOLD = cli.StreamWriter().hold          # 보류 구간(현재 56자 — 문장 끝까지는 바로 내보낸다)


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
        # render_answer 와 같게 — 정리기를 넘겨 내보내기 전에 되뱉기에 걸리는지 보게 한다
        w = cli.StreamWriter(cleaner=lambda t: strip_instruction_echo(t, n),
                             soft=lambda t: soft_clean(t, n))
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
# 첫 문장은 끝나는 대로 나간다(화면 첫 글자를 앞당긴다) — 되뱉기는 그래도 지워진다
check(emitted > 0, "짧은 답변 — 끝난 첫 문장은 바로 나간다 (emitted=%d)" % emitted)
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
# 예전에는 이미 화면에 나간 [12] 를 지울 수 없었다. 지금은 내보내는 조각마다 가벼운 정리(soft)를
#   하므로 화면에 나가기 전에 괄호를 벗긴다(숫자는 남긴다 — drop_invalid_cites 규칙)
check("[12]" not in out and "12 앞쪽에" in out, "앞쪽의 없는 인용도 화면에 나가기 전에 벗긴다", out[:40])
check("형식으로 표기" not in out, "그래도 꼬리 되뱉기는 제거된다")

# ── 계획서 1-4(REPORT §45) — 실제 수집 답변에서 센 되뱉기 유형 ──
#   지워야 하는 것과 **지우면 안 되는 것**을 짝으로 둔다. §18 의 교훈: 과잉 삭제가 더 나쁘다.
_ECHO_CASES = [
    ("휴가비는 100만원입니다. 근거 번호: [1]", "근거 번호", "100만원", "콜론이 낀 근거 번호 라벨"),
    ("휴가는 5일입니다. [3] 번의 근거 번호는 [3]입니다", "근거 번호는", "5일", "근거 번호는 [n]입니다"),
    ("숙박비는 70,000원입니다. [1] 근거 번호입니다", "근거 번호입니다", "70,000", "근거 번호입니다 꼬리"),
    ("경조금을 받습니다. 근거에 없는 내용은 추측하지 않습니다. [1]", "추측하지", "경조금", "추측 금지 평서문"),
    ("근거에 없는 내용은 추측하지 않아, 2년간 모니터링한다. [1]", "추측하지", "2년간", "추측 금지 + 뒤에 진짜 내용"),
    ("휴가비는 100만원입니다. [1]과 [2]의 정보를 바탕으로 답변합니다", "바탕으로 답변", "100만원", "…바탕으로 답변합니다"),
    ("교통비는 티머니로 지원합니다. [1]번으로 간결하게 답변합니다", "간결하게 답변", "티머니", "…간결하게 답변합니다"),
    ("[근거] 침구류는 25만원 한도입니다. [1]", "[근거]", "25만원", "본문에 옮겨 적은 블록 표식"),
    ("권고 모델은 **DELL P2422H** 입니다. [1]", "**", "DELL P2422H", "마크다운 굵게 — 글자는 남긴다"),
    # 506문항 재수집에서 3건 남아 보탠 변형(REPORT §45.6)
    ("유급휴가는 1일입니다. [1] 번호 [1]로 답변합니다. [1]", "답변합니다", "1일", "번호 [n]로 답변합니다"),
    ("자연어 질의 검색을 제공합니다. [1] 형식으로 근거 번호 표기.", "근거 번호", "자연어", "형식으로 + 근거 번호 (낱말 순서 뒤집힘)"),
    ("설정값을 확인할 수 있습니다. [1] [근거 번호 1]", "근거 번호", "설정값", "인용 자리의 [근거 번호 n]"),
]
print()
for raw, gone, kept, name in _ECHO_CASES:
    body, _ = parse_answer(raw, 3)
    check(gone not in body and kept in body, "되뱉기 제거 — %s" % name, body)

# 지우면 안 되는 것 — 문서에 실제로 있는 표현
_KEEP_CASES = [
    ("백업은 zip 형식으로 저장됩니다. [1]", "형식으로 저장", "'형식으로' 가 본문 내용일 때"),
    ("담당자가 고객 질문에 답변합니다. [1]", "질문에 답변합니다", "그냥 '답변합니다' 로 끝나는 문장"),
    ("경로는 D:\\MpowerDB 입니다. [1]", "MpowerDB", "별표 없는 경로"),
    ("숙박비 상한액은 70,000원입니다. [1]", "70,000원입니다", "평범한 답"),
    ("담당자 전화번호 [1]로 답변합니다. [1]", "전화번호", "'번호'가 낱말 일부일 때(전화번호)"),
    ("신청서는 [별지 1] 양식으로 제출합니다. [1]", "[별지 1]", "인용처럼 보이는 본문 대괄호"),
]
for raw, kept, name in _KEEP_CASES:
    body, _ = parse_answer(raw, 3)
    check(kept in body, "보존 — %s" % name, body)

# 스트리밍으로도 같게 — 덜 붙드는 출력기(56자 · 문장 끝 바로 내보내기)가 되뱉기를 놓치지 않는가.
#   앞에 문장을 길게 붙여 보류 구간을 넘긴 상태에서 되뱉기가 오게 한다
_PAD = "규정에 따라 처리합니다. " * 6
print()
for raw, gone, kept, name in _ECHO_CASES:
    for step in (1, 3, 7):
        out, emitted = stream(_PAD + raw, 3, step)
        check(gone not in out and kept in out and emitted > 0,
              "스트리밍 %d자씩 되뱉기 제거 — %s" % (step, name), out[-60:])
for raw, kept, name in _KEEP_CASES:
    out, _ = stream(_PAD + raw, 3, 3)
    check(kept in out, "스트리밍 보존 — %s" % name, out[-60:])

# 생성기(outguard) → 화면 출력기 이어서 — 실제 골든셋 답(2026-10-26 측정)
#   q044: 반복이 앞 문장의 '중간 구절'에서 시작됐다. 출력기가 붙들고 있던 첫머리 "[3]에 따르면, 휴직자로서"
#         는 생성기가 정한 남길 답(final) 기준으로 버린다
#   q096: "근거 [1]에 따르면" 의 '근거' — 뒤에 [1] 이 와야 지우는 되뱉기를 덜 붙든 출력기가 먼저 내보냈다
from simplerag.generate.outguard import OutGuard                        # noqa: E402


#------------------------------------------------------------------
# 생성기 → 출력기 한 번 흉내내기
#=> 글을 step 글자씩 outguard 에 넣고, 나온 조각을 StreamWriter 로 흘린 뒤 남길 답으로 마무리한다.
#
# -in: raw = 모델이 뱉을 글 / n = 근거 개수 / step = 조각 글자 수
#
# -out: 화면에 나간 전체
# -out: error = 없음
#------------------------------------------------------------------
def pipe(raw, n, step):
    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        w = cli.StreamWriter(cleaner=lambda t: strip_instruction_echo(t, n),
                             soft=lambda t: soft_clean(t, n))
        g = OutGuard()
        for i in range(0, len(raw), step):
            e = g.push(raw[i:i + step], 150)
            if e:
                w.write(e)
            if g.halt:
                break
        e, kept, _ = g.finish()
        if e:
            w.write(e)
        body, _ = parse_answer(kept, n)
        w.finish(lambda t: strip_instruction_echo(t, n), final=body)
    finally:
        sys.stdout = real
    return buf.getvalue()


Q044 = ("휴직기간이 끝나는 사원은 휴직기간 만료일 7일 전까지 복직원을 제출해야 한다.  \n"
        "[1]에 따르면, 휴직기간이 만료된 때에는 그 만료일 7일 전까지 복직원을 제출해야 한다.  \n"
        "휴직기간이 끝나지 않으면 해고가 가능하며, 휴직자로서 정당한 사유 없이 휴직기간 만료일 후 7 일이 경과할 때까지 "
        "복직원을 제출하지 않은 경우 [3]에 따라 해고될 수 있다.  \n"
        " [3]에 따르면, 휴직자로서 정당한 사유 없이 휴직기간 만료일 후 7 일이 경과할 때까지 복직원을 제출하지 않은 경우 "
        "해고될 수 있다.")
Q096 = ("2021년 대비 2026년 세계 디지털 휴먼 시장 규모는 약 9.8배로 커질 것으로 전망됩니다.  \n"
        "근거 [1]에 따르면, 2021년 146.8억 달러에서 2026년 987.5억 달러로 9.8배 증가할 예정입니다. [1]")
for step in (1, 2, 3):
    out = pipe(Q044, 3, step)
    check(out.rstrip().endswith("해고될 수 있다.") and "[3]에 따르면" not in out,
          "q044 — 구절 반복의 첫머리가 화면에 남지 않는다 (%d자씩)" % step, out[-50:])
    out = pipe(Q096, 3, step)
    check("근거 [" not in out and "근거\n" not in out and "[1]에 따르면" in out,
          "q096 — '근거 [1]' 되뱉기를 덜 붙들어도 지운다 (%d자씩)" % step, out[-90:])

# 굵게 별표가 "*" 두 토큰으로 갈라져 와도 별표가 하나도 남지 않는다(골든셋 q025 — 여는 "*" 하나가 먼저 나가
#   짝이 어긋났다). 1자씩 넣으면 보류 경계가 별표 둘 사이에 걸리는 때가 반드시 생긴다
Q025 = ("자녀가 다른 기관에서 장학금을 받는 경우, 학자금 지급은 **차액이 아닌 전체 금액을 지급**하며, "
        "**장학금을 받은 금액에서 차액을 차감한 후** 지급됩니다.  \n자녀 학자금 신청은 **납부 후 1개월 이내**에 "
        "신청해야 합니다.  \n학자금 신청서류는 해당 부서장 결재 후 경영지원본부에 신청해야 합니다. [1]")
for step in (1, 2, 3):
    out, _ = stream(Q025, 3, step)
    check("*" not in out and "차액이 아닌 전체 금액을 지급하며" in out,
          "굵게 별표가 갈라져 와도 남지 않는다 (%d자씩)" % step, out[:80])

# 되뱉기만 남은 답은 원문을 그대로 둔다(과잉 삭제 방지, 기존 규칙)
only_echo, _ = parse_answer("[1] [2] [3] 근거 번호 표기.", 3)
check(only_echo.strip() != "", "내용이 없으면 원문 보존", only_echo)

# 지운 자리에 마침표가 겹치지 않는다
body, _ = parse_answer("경조금을 받습니다. 근거에 없는 내용은 추측하지 않습니다. [1]", 3)
check(".." not in body, "지운 자리에 마침표가 겹치지 않는다", body)
check(parse_answer(early, 3)[1] == [], "cited 목록은 언제나 정확하다")

# ── 되뱉기 거르기 끔(기본, generation.echo_filter: false) ──
#   render_answer 는 꺼져 있으면 StreamWriter(hold=0, soft=…) 로 붙들지 않고 내보낸다.
#   첫 문장이 끝나기 전, 첫 조각부터 화면에 나가야 한다(켜 둔 쪽은 첫 문장 끝까지 붙든다).
print()
print("── 되뱉기 거르기 끔 ──")
for step in (1, 3, 7):
    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        w = cli.StreamWriter(hold=0, soft=lambda t: soft_clean(t, 3))
        w.interval = 0.0                # 시간 조건으로 바로 내보내게(실제는 0.06초마다)
        first = None
        for i in range(0, len(Q025), step):
            w.write(Q025[i:i + step])
            if first is None and w._emitted:
                first = i + step        # 화면에 처음 나간 시점까지 넣은 글자 수
        w.finish(lambda t: cli.clean_answer(t, 3, echo=False))
    finally:
        sys.stdout = real
    out = buf.getvalue()
    check(first is not None and first <= 20, "끔 — 첫 조각이 바로 나간다 (%d자씩, 첫 출력 %s자째)" % (step, first))
    check("*" not in out and "차액이 아닌 전체 금액을 지급하며" in out,
          "끔 — 굵게 별표는 여전히 걷어 낸다 (%d자씩)" % step, out[:80])
body, _ = parse_answer("휴가는 5일입니다. [1] 지시문에 따라 간결하게 답변합니다.", 3, echo=False)
check("간결하게 답변합니다" in body, "끔 — 긴 되뱉기는 남긴다(켰을 때만 지움)", body)
body, _ = parse_answer(SHORT, 3, echo=False)
check("형식으로 표기" not in body and "휴가비를 지급합니다." in body, "끔 — 짧은 되뱉기는 지운다(설계서 ②)", body)
body, _ = parse_answer("**답**은 50만원입니다. [1] [12]", 3, echo=False)
check(body == "답은 50만원입니다. [1] 12", "끔 — 별표·없는 인용 번호 정리는 그대로", body)

# ── 짧은 되뱉기 · 인용 덩어리 중복 (설계서 ②③) — 거르기 끔에서도 지연 없이 지운다 ──
#   골든셋 100문항(1.7B, 거르기 끔)에서 실제로 남았던 잡음을 그대로 넣는다.
print()
print("── 짧은 되뱉기 (끔) ──")
SHORT_CASES = [
    ("q011", "지원금은 80만원의 50%인 40만원이다.  \n[1] 근거 [1]에 따르면, 50만원 초과분의 50%를 지급한다.",
     "근거", "[1]에 따르면, 50만원 초과분"),
    ("q016", "대출 한도는 3천만원입니다.  \n[2] 근거 번호: [2]", "근거 번호", "3천만원입니다."),
    ("q020", "대표이사에 대한 조치는 이사회가 의결을 맡는다. [1][3][1][3]", "[1][3][1][3]", "[1][3]"),
    ("q029", "정액의 10분의 1에 상당한 금액을 지급받는다.  \n근거 [1]에 따르면, 15일 초과 시 10분의 1을 뺀다.",
     "근거", "[1]에 따르면, 15일 초과"),
    ("q056", "데이터 처리 전에 검증 작업이 수행된다.  \n\n[1] 형식으로 표기:", "형식으로", "검증 작업이 수행된다."),
    ("q075", "형사처벌 대상으로 간주됩니다.  \n[1]의 근거 번호는 [1]입니다.", "근거 번호는", "간주됩니다."),
    # 2차 측정(짧은 거르기 1판)에서 남은 2건
    ("q011b", "40만원이다.  \n[1] 근거 [1]에 따르면, 50만원 초과분의 50%를 지급한다.", "[1] [1]", "[1]에 따르면"),
    ("q070", "[2] 제공자가 알 수 없는 경우  \n\n[1] 형식으로 근거 번호를 표기하면 다음과 같다.", "형식으로",
     "알 수 없는 경우"),
]


#------------------------------------------------------------------
# 거르기 끔 스트리밍 흉내 (render_answer 의 끔 경로와 같게)
#=> StreamWriter(hold=0, short=True) 에 흘려 넣고 clean_answer(echo=False) 로 마무리한다.
#
# -in: raw = 원문 / n = 근거 개수 / step = 한 번에 넣을 글자 수
#
# -out: (화면 출력 전체, 첫 출력까지 넣은 글자 수)
# -out: error = 없음
#------------------------------------------------------------------
def stream_off(raw, n, step):
    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        w = cli.StreamWriter(hold=0, soft=lambda t: soft_clean(t, n), short=True)
        w.interval = 0.0
        first = None
        for i in range(0, len(raw), step):
            w.write(raw[i:i + step])
            if first is None and w._emitted:
                first = i + step
        w.finish(lambda t: cli.clean_answer(t, n, echo=False))
    finally:
        sys.stdout = real
    return buf.getvalue(), first


for qid, raw, gone, kept in SHORT_CASES:
    for step in (1, 3, 7):
        out, first = stream_off(raw, 3, step)
        check(gone not in out and kept in out, "%s 짧은 되뱉기 지움 (%d자씩)" % (qid, step), out[-70:])
        check(first is not None and first <= 12, "%s 첫 조각이 바로 나간다 (%d자씩, %s자째)" % (qid, step, first))
    body, _ = parse_answer(raw, 3, echo=False)
    check(gone not in body and kept.split()[0] in body, "%s 비스트리밍(parse_answer)도 지움" % qid, body[-70:])

# 지우면 안 되는 것 — 본문 낱말 '근거', 인용 번호 뒤 본문, 다른 번호가 이어진 인용
for raw, kept, name in [
        ("지급 근거 규정은 제5조입니다. [1]", "근거 규정은", "인용이 뒤따르지 않는 '근거'"),
        ("[1]에 따르면 휴가는 5일이고, [2]에 따르면 3일이다.", "[2]에 따르면", "문장 중간 인용 번호"),
        ("휴가는 5일이다. [1][2]", "[1][2]", "서로 다른 번호 덩어리(중복 아님)"),
        ("근무 형태는 [1] 교대제로 정한다.", "근무 형태는", "'근' 으로 시작하는 다른 낱말")]:
    for step in (1, 3):
        out, _ = stream_off(raw, 3, step)
        check(kept in out, "남겨야 함 — %s (%d자씩)" % (name, step), out)

print()
print("=" * 60)
print("실패 %d건" % len(fail))
for f in fail:
    print("   " + f)
raise SystemExit(1 if fail else 0)
