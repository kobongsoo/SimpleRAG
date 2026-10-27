#------------------------------------------------------------------
# 반복 루프 · 상한 처리 회귀 시험 (generate/outguard.py — Rust 판 generator.rs 시험과 같은 사례)
#=> 1.7B 가 같은 문장을 되풀이하다 200토큰 상한에서 단어 중간에 끊긴 실제 답을 다시 만들어 본다.
#   반복은 반복 문턱(40자)보다 긴 문장으로 시험한다 — 문턱보다 짧으면 판정문이 실행되지 않는다.
#
#   사용: python bench/test_outguard.py
#------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.stdout.reconfigure(encoding="utf-8")

from simplerag.generate.outguard import (NOTE_CAPPED, NOTE_LOOP, OutGuard,  # noqa: E402
                                         repeated_suffix_len, sentence_ends)

fail = []


#------------------------------------------------------------------
# 판정 한 줄 출력 + 실패 모으기
#
# -in: ok = 통과 여부 / name = 시험 이름 / hint = 실패 때 보여 줄 값
#
# -out: 없음
# -out: error = 없음(실패는 fail 목록에 모은다)
#------------------------------------------------------------------
def check(ok, name, hint=""):
    print("%s %s" % ("✅" if ok else "🔴", name))
    if not ok:
        fail.append(name)
        if hint:
            print("     " + repr(hint))


#------------------------------------------------------------------
# 생성 한 번 흉내내기
#=> 글을 3글자씩(토큰 하나쯤) 넣고, 멈추면 마무리한다.
#
# -in: text = 모델이 뱉을 글 / max_new = 상한(토큰) / capped = 끝까지 가면 상한에 닿은 것으로 볼지
#
# -out: (화면에 나간 글, 남긴 답, 표시)
# -out: error = 없음
#------------------------------------------------------------------
def run(text, max_new=200, capped=False):
    g = OutGuard()
    shown = ""
    pieces = [text[i:i + 3] for i in range(0, len(text), 3)]
    for k, p in enumerate(pieces):
        shown += g.push(p, max_new - (k + 1))
        if g.halt:
            break
    emit, kept, note = g.finish(capped=capped)
    return shown + emit, kept, note


# 1) 사용자 보고와 같은 모양 — 같은 문장이 되풀이되다 끊긴다
S = "[1] 번호의 근거에 따르면, 경조사휴가의 금액은 사망 시 6 일이지만, 배우자의 부모 사망 시 5 일이 부여됩니다. "
shown, kept, note = run("경조사휴가는 사망 시 주어집니다. " + S * 3 + "배우")
# 문장 끝 바로 뒤의 "[1]" 은 앞 문장의 인용 번호로 본다(다음 문장 머리인지 가릴 수 없다)
want = "경조사휴가는 사망 시 주어집니다. " + S.rstrip() + " [1]"
check(kept.rstrip() == want, "반복 문장은 한 번만 남긴다", kept)
check(shown.rstrip() == want, "반복분은 화면에도 나가지 않는다", shown)
check(note is None, "문장 경계에서 잘렸으면 표시 없음", note)

# 1-2) 골든셋 q027·q044 모양 — 반복이 시작된 문장의 첫머리가 화면에 조각으로 남으면 안 된다
_FRAG_CASES = [
    # q027 실제 답 — 예전에는 "… 지급된다. [1] 기 (같은 내용이 반복되어 이하 생략)" 로 남았다
    ("차장 이하 직원이 기내박 없이 도쿄로 2박 3일 해외출장(셋째 날 귀국)을 가면, 식비는 별도 지급하지 않고 "
     "일비만 정액으로 지급받는다.  \n",
     "[1] 기내박 없이 식비를 지급하지  않는 경우, 일비만 정액으로 지급된다. [2] 일비는 정액으로 지급받는다. "
     "[3] 식비는 별도로 지급하지 않고, 일비만 정액으로 지급된다. " * 3),
    ("무단결근이 이어지면 해고될 수 있다. [3]에 따르면, 휴직자로서 복직 기한을 넘기면 퇴직 처리된다. ",
     "[3]에 따르면, 휴직자로서 복직 기한을 넘기면 퇴직 처리된다. [3]에 따르면, 휴직자로서 복직"),
]
#   q066 — 반복 문장이 머리의 인용 번호만 바뀌어 온다("[1] 청탁금지법…" → "[3] 청탁금지법")
_FRAG_CASES.append((
    "청탁금지법 위반 행위의 공소시효는 5년이며, 질서위반행위규제법에 따릅니다.  \n"
    "[1] 청탁금지법은 공소시효가 없는 경우 형사소송법과 질서위반행위규제법에 따라 적용됩니다.  \n"
    "[2] 질서위반행위규제법은 공소시효가 5년으로 규정되어 있습니다.  \n",
    " [3] 청탁금지법은 공소시효가 없는 경우 형사소송법과 질서위반행위규제법에 따라 적용됩니다.  \n" * 2))
#   q080 — 앞에 없던 번호로 새 문장이 시작된다(머리 "[2" 가 먼저 나가 조각으로 남았다)
_FRAG_CASES.append((
    "비즈니스웨어는 통합 플랫폼이다.  \n[1] 비즈니스웨어는 통합을 위한 플랫폼 역할을 수행한다.  \n",
    " [2] 비즈니스웨어는 통합을 위한 플랫폼 역할을 수행한다.  \n" * 3))
for head, tail in _FRAG_CASES:
    shown, kept, note = run(head + tail)
    ends = sentence_ends(kept.rstrip())
    check(bool(ends) and ends[-1] == len(kept.rstrip()), "반복 생략 자리에 조각 없음 — " + head[:12], kept)
    # 생성기는 남길 답보다 조금 더 흘려보낼 수 있다(그 꼬리는 화면 출력기가 버린다 — test_output_cleanup 의 q044)
    check(shown.startswith(kept.rstrip()) and note in (None, NOTE_LOOP), "남길 답은 흘려보낸 글의 앞부분 — " + head[:12], shown)
# 1-3) 골든셋 q096 실제 원문 — 반복이 앞의 다른 곳으로 갈아타며 이어진다.
#   "근거 [1]에 따르면, 2021년 "(둘째 문장 머리) → " 대비 2026년 …"(첫 문장 중간). 갈아타는 순간 문장 머리가
#   화면에 나가, 되뱉기 거르기 끔(붙들기 없음)에서 "… [1]에 따르면, 2021년 대 (같은 내용이…)" 로 남았다.
#   생성기가 내보낸 글(shown)에 그 머리가 없어야 한다 — 화면 출력기가 붙들어 줄 것을 기대하지 않는다.
Q096 = ("2021년 대비 2026년 세계 디지털 휴먼 시장 규모는 약 9.8배로 커질 것으로 전망됩니다.  \n"
        "근거 [1]에 따르면, 2021년 146.8억 달러에서 2026년 987.5억 달러로 9.8배 증가할 예정입니다.  \n"
        "근거 [1]에 따르면, 2021년 대비 2026년 세계 디지털 휴먼 시장 규모는 약 9.8배로 커질 것으로")
# 모델이 실제로 낸 조각 경계 그대로(1.7B, 2026-10-27 기록) — 고정 길이로 자르면 경계에 따라 문제가 안 드러난다
#   (수정 전 코드로 1·3자씩은 통과하고 실제 경계·2자씩만 실패했다)
Q096_TOKENS = ("예|2|0|2|1|년| 대|비| |2|0|2|6|년| 세계| 디|지|털| 휴|먼| 시|장| 규|모|는| 약| |9|.|8|배|로| 커|질| 것으로"
               "| 전|망|됩니다|.|  \n|근|거| [|1|]|에| 따|르|면|,| |2|0|2|1|년| |1|4|6|.|8|억| 달|러|에서| |2|0|2|6|년"
               "| |9|8|7|.|5|억| 달|러|로| |9|.|8|배| 증|가|할| 예|정|입니다|.|  \n|근|거| [|1|]|에| 따|르|면|,| |2|0|2|1"
               "|년| 대|비| |2|0|2|6|년| 세계| 디|지|털| 휴|먼| 시|장| 규|모|는| 약| |9|.|8|배|로| 커").split("|")
_Q096_SPLITS = [("실제 토큰", Q096_TOKENS)] + [("%d자씩" % s, [Q096[i:i + s] for i in range(0, len(Q096), s)])
                                            for s in (1, 2, 3)]
for name, pieces in _Q096_SPLITS:
    g = OutGuard()
    shown = ""
    for k, p in enumerate(pieces):
        shown += g.push(p, 200 - k)  # 실제 측정처럼 상한 200 — 작으면 상한 직전 붙들기가 문제를 가린다
        if g.halt:
            break
    emit, kept, note = g.finish()
    check(g.halt is not None and "2021년 대" not in shown[shown.rfind("\n"):] and note == NOTE_LOOP,
          "q096 — 갈아타며 이어지는 반복의 머리가 나가지 않는다 (%s)" % name, shown[-60:])

check(sentence_ends("해고될 수 있다. [3]에 따르면") == [len("해고될 수 있다.")], "'[3]에' 는 다음 문장의 머리")
check(sentence_ends("끝이다. [1][2] 다음") == [len("끝이다. [1][2]")], "'[1][2] ' 는 앞 문장의 인용")

# 2) 정상적인 짧은 되풀이 — 표 줄 · 같은 머리말 · 숫자 나열
T = ("| 부장 | 가 | 176 |\n| 차장 | 가 | 176 |\n[1] 근거에 따르면, 숙박비는 실비입니다. "
     "[1] 근거에 따르면, 일비는 2만원입니다. 1, 2, 3, 1, 2, 3.")
shown, kept, note = run(T)
check(kept == T and shown == T and note is None, "짧은 되풀이는 끊지 않는다", kept)

# 3) 상한 — 마지막 완결 문장(뒤 인용 번호 포함)까지 + 표시, 덜 끝난 문장은 화면에도 안 나간다
C = "첫째 문장입니다. [1] 둘째 문장은 휴가가 5일 주어진다는 것입니다. [2][3] 셋째 문장은 아직 끝나지"
n_pieces = (len(C) + 2) // 3
shown, kept, note = run(C, max_new=n_pieces, capped=True)
want = "첫째 문장입니다. [1] 둘째 문장은 휴가가 5일 주어진다는 것입니다. [2][3]"
check(kept == want, "상한 — 완결 문장까지만", kept)
check(shown == want, "상한 — 반쪽 문장이 화면에 남지 않는다", shown)
check(note == NOTE_CAPPED, "상한 — 생략 표시", note)

# 4) 완결 문장이 없으면 있는 그대로
_, kept, note = run("끝나지 않은 문장", max_new=3, capped=True)
check(kept == "끝나지 않은 문장" and note == NOTE_CAPPED, "완결 문장이 없으면 그대로 둔다", kept)

# 5) 자연스럽게 끝난 답은 그대로
_, kept, note = run("답은 5일")
check(kept == "답은 5일" and note is None, "자연스럽게 끝난 답은 그대로", kept)

# 6) 문장 경계 규칙 · 반복 꼬리
s = "값은 3.5일입니다. [1] 다음\n끝은 6."
ends = [s[:p] for p in sentence_ends(s)]
check(ends == ["값은 3.5일입니다. [1]", "값은 3.5일입니다. [1] 다음\n"], "문장 경계(소수점·인용·줄바꿈)", ends)
check(repeated_suffix_len("abcXabc") == 3 and repeated_suffix_len("ababab") == 4, "반복 꼬리 길이")

print()
print("=" * 60)
print("실패 %d건" % len(fail))
for f in fail:
    print("   " + f)
raise SystemExit(1 if fail else 0)
