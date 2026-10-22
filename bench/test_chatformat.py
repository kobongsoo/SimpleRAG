#------------------------------------------------------------------
# 모델별 채팅 형식 회귀 테스트 (계획서 2-3 · REPORT §47)
#=> 모델마다 채팅 형식이 다르다. 형식이 한 글자만 틀려도 모델은 학습 때 본 적 없는
#   입력을 받아 엉뚱하게 답한다 — 그러면 품질이 낮게 나와도 **모델 탓인지 형식 탓인지
#   구분할 수 없다.** 그래서 형식을 문자열 단위로 못 박아 둔다.
#
#   가장 중요한 것은 **Qwen3 형식이 종전과 글자까지 같은지**다. 지금까지의 모든
#   기준선(§36~§46)이 그 형식으로 측정됐으므로, 여기가 바뀌면 비교가 전부 깨진다.
#
#   실행: .venv/Scripts/python.exe bench/test_chatformat.py
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                    # noqa: E402
from simplerag.generate import chatformat                       # noqa: E402

FAILED = []
TOTAL = [0]

SYS = "지시문입니다."
USER = "[근거]\n[1] 본문\n\n[질문]\n질문?"


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
# Qwen3 형식이 종전과 글자까지 같은가 — 가장 중요한 검사
#=> 기대 문자열을 여기에 통째로 적어 둔다. 누가 chatformat 을 고쳐 Qwen 경로가
#   달라지면 이 검사가 먼저 깨진다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_qwen_unchanged():
    print("\n── 1. Qwen3 형식은 종전 그대로 (기준선 보호) ".ljust(60, "─"))
    want = (
        "<|im_start|>system\n" + SYS + "<|im_end|>\n"
        "<|im_start|>user\n" + USER + "<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )
    for alias in ("qwen3-0.6b-q4", "qwen3-1.7b-q4"):
        got = chatformat.build_chat_prompt(SYS, USER, alias)
        check("%s 프롬프트가 글자까지 같다" % alias, got == want, got)
        check("%s 중단 문자열" % alias,
              chatformat.stop_strings(alias) == ["<|im_end|>", "<|endoftext|>"],
              chatformat.stop_strings(alias))
    check("<think> 를 미리 닫아 사고토큰을 막는다", want.endswith("<think>\n\n</think>\n\n"))
    check("모르는 별칭은 qwen3 로 본다", chatformat.format_name("없는모델") == "qwen3")


#------------------------------------------------------------------
# Llama-3 헤더 형식(Mi:dm · Kanana)
#=> 두 모델 모두 <|start_header_id|>역할<|end_header_id|> … <|eot_id|> 를 쓴다.
#   BOS 를 문자열에 넣지 않는 것이 중요하다 — llama.cpp 가 토크나이징할 때
#   스스로 붙이므로 여기서 또 쓰면 두 번 들어간다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_llama3_header():
    print("\n── 2. Llama-3 헤더 형식 ".ljust(60, "─"))
    for alias in ("midm-2.0-mini-q4", "kanana-1.5-2.1b-q4"):
        p = chatformat.build_chat_prompt(SYS, USER, alias)
        check("%s BOS 를 문자열에 넣지 않는다" % alias, "<|begin_of_text|>" not in p)
        check("%s 지시문이 시스템 블록에 들어간다" % alias,
              "<|start_header_id|>system<|end_header_id|>" in p and SYS in p)
        check("%s 사용자 블록" % alias,
              "<|start_header_id|>user<|end_header_id|>\n\n" + USER + "<|eot_id|>" in p)
        check("%s 답변 차례로 끝난다" % alias,
              p.endswith("<|start_header_id|>assistant<|end_header_id|>\n\n"), p[-60:])
        check("%s 중단 문자열" % alias,
              chatformat.stop_strings(alias) == ["<|eot_id|>", "<|end_of_text|>"],
              chatformat.stop_strings(alias))
        check("%s Qwen 표식이 섞이지 않는다" % alias, "<|im_start|>" not in p)


#------------------------------------------------------------------
# Mi:dm 의 내장 지시문 — 빼지 않고 그대로 싣는다
#=> KT 공식 템플릿이 시스템 블록 앞에 한국어 지시문을 통째로 붙인다. 모델이
#   그렇게 학습됐으므로 임의로 빼면 공정한 비교가 아니다. 대신 **프롬프트가
#   그만큼 길어진다**는 사실을 여기에 남겨 둔다(TTFT 해석에 필요).
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_midm_preamble():
    print("\n── 3. Mi:dm 내장 지시문 ".ljust(60, "─"))
    p = chatformat.build_chat_prompt(SYS, USER, "midm-2.0-mini-q4")
    check("KT 내장 지시문이 실린다", "Mi:dm(믿:음)은 KT에서 개발한" in p)
    check("내장 지시문이 우리 지시문보다 앞에 온다",
          p.index("Mi:dm(믿:음)") < p.index(SYS))
    check("Kanana 에는 내장 지시문이 없다",
          "Mi:dm(믿:음)" not in chatformat.build_chat_prompt(SYS, USER, "kanana-1.5-2.1b-q4"))
    # 길이는 TTFT 해석의 근거라 눈에 보이게 찍는다.
    base = len(chatformat.build_chat_prompt(SYS, USER, "kanana-1.5-2.1b-q4"))
    print("     (참고) Mi:dm 프롬프트가 Kanana 보다 %d자 길다" % (len(p) - base))
    check("내장 지시문 때문에 1000자 이상 길다", len(p) - base > 1000)


#------------------------------------------------------------------
# 설정과 아귀가 맞는가
#=> GEN_MODELS 에 있는 별칭은 모두 형식이 정해져 있어야 하고, 형식 이름은
#   FORMATS 에 실제로 있어야 한다. 오타를 배포 전에 잡는다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_registry():
    print("\n── 4. 설정과 아귀 맞추기 ".ljust(60, "─"))
    for alias in config.GEN_MODELS:
        check("%s 형식이 정해져 있다" % alias, alias in config.GEN_MODEL_FORMATS)
    for alias, name in config.GEN_MODEL_FORMATS.items():
        check("%s → %s 가 실제로 있다" % (alias, name), name in chatformat.FORMATS)
        check("%s 는 GEN_MODELS 에 있는 별칭이다" % alias, alias in config.GEN_MODELS)


#------------------------------------------------------------------
# 전체 실행
#
# -in: 없음
#
# -out: int = 실패가 있으면 1, 없으면 0
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_qwen_unchanged()
    test_llama3_header()
    test_midm_preamble()
    test_registry()
    print("\n결과: %d 항목 중 실패 %d" % (TOTAL[0], len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
