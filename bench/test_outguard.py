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

from simplerag.generate.outguard import (NOTE_CAPPED, OutGuard,      # noqa: E402
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
