#------------------------------------------------------------------
# 컨텍스트 초과 방어 회귀 테스트
#=> 2026-08-25 리랭킹 평가(506문항) 도중 331번째 문항에서 실제로 죽었다.
#     ValueError: Requested tokens (4061) exceed context window of 2048
#   원인은 리랭킹이 아니라 인덱스에 있던 초장문 청크였다 — 구조 청킹이
#   표/절을 통째로 유지하는 탓에 59,747청크 중 17개가 3,000자를 넘고
#   최대 5,526자다. 이런 청크가 근거로 뽑히면 그것 하나로 2048 컨텍스트를
#   넘겨 파이프라인 전체가 예외로 중단된다(운영 CLI/exe 도 마찬가지).
#
#   GgufGenerator._fit_prompt 로 막았고, 이 파일은 그 방어가 계속
#   살아있는지 확인한다. 두 가지를 본다.
#     ① 초장문 근거가 와도 예외 없이 답이 나온다
#     ② 정상 길이 근거는 프롬프트가 **한 글자도 달라지지 않는다**
#        (②가 깨지면 과거 벤치 수치와 비교가 불가능해진다)
#
#   실행: .venv/Scripts/python.exe bench/test_context_guard.py
#         (모델을 실제로 로드하므로 수십 초 걸린다)
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                    # noqa: E402
from simplerag.generate.gguf_generator import GgufGenerator     # noqa: E402
from simplerag.generate.prompts import (                        # noqa: E402
    SYSTEM_PROMPT, build_prompt, build_user_message)

FAILED = []


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 다 돌린다
#
# -in: name = 테스트 이름
# -in: cond = 참이어야 하는 조건
# -in: note = 실패 시 함께 출력할 설명(기본 "")
#
# -out: 없음
# -out: error = 예외 없음. 실패는 전역 FAILED 에 쌓인다
#------------------------------------------------------------------
def check(name, cond, note=""):
    if cond:
        print("  OK   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, note))
        FAILED.append(name)


#------------------------------------------------------------------
# 테스트 본체
#=> 실제 모델을 띄운다. _fit_prompt 는 토크나이저가 있어야 동작하므로
#   모킹으로는 진짜 회귀를 잡을 수 없다(터진 것도 실제 토큰 수였다).
#
# -in: 없음
#
# -out: 없음 (결과는 표준출력 + 종료코드)
# -out: error = 모델 파일이 없으면 GenerateError 가 그대로 전파된다
#------------------------------------------------------------------
def main():
    gen = GgufGenerator(config.DEFAULT_GEN_MODEL)
    gen.ensure_loaded()

    # ① 실제 인덱스 최대치(5,526자)를 훨씬 넘기는 근거로 시험한다.
    #    여유 있게 잡아야 "우연히 통과"를 배제할 수 있다.
    huge = "가나다라마바사아자차카타파하 규정 조항이 길게 이어진다. " * 400
    chunks = [huge, "짧은 근거입니다.", "또 다른 짧은 근거입니다."]
    try:
        out = "".join(gen.stream("이 규정은 언제 시행되나요?", chunks))
        ok = True
    except Exception as e:                              # noqa: BLE001
        out, ok = "", "%s: %s" % (type(e).__name__, e)
    check("초장문 근거(%d자)로도 예외 없이 생성" % len(huge), ok is True, ok)
    check("생성 결과가 비어있지 않음", len(out.strip()) > 0)

    # 근거 3개가 그대로 유지돼야 인용 번호가 어긋나지 않는다
    fitted = gen._fit_prompt(SYSTEM_PROMPT, "질문", chunks, config.GEN_MAX_TOKENS)
    check("잘라낸 뒤에도 근거 3개 표식이 남음",
          all(("[%d]" % i) in fitted for i in (1, 2, 3)))

    n_tok = len(gen._llm.tokenize(fitted.encode("utf-8"), special=True))
    limit = gen.n_ctx - config.GEN_MAX_TOKENS
    check("프롬프트 토큰(%d) <= 예산(%d)" % (n_tok, limit), n_tok <= limit)

    # ② 정상 길이는 손대지 않는다 — 이게 깨지면 과거 측정과 비교 불가
    normal = ["짧은 근거 A 입니다.", "짧은 근거 B 입니다.", "짧은 근거 C 입니다."]
    same = gen._fit_prompt(SYSTEM_PROMPT, "질문", normal, config.GEN_MAX_TOKENS)
    expect = build_prompt(SYSTEM_PROMPT, build_user_message("질문", normal))
    check("정상 근거는 프롬프트가 완전히 동일", same == expect)

    # 실제 인덱스 중앙값(223자) x 3 도 무손실이어야 한다
    mid = ["나" * 223] * 3
    same2 = gen._fit_prompt(SYSTEM_PROMPT, "질문", mid, config.GEN_MAX_TOKENS)
    check("중앙값 길이(223자x3)도 무손실",
          same2 == build_prompt(SYSTEM_PROMPT, build_user_message("질문", mid)))

    print("\n%d개 중 %d개 실패" % (6, len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
