#------------------------------------------------------------------
# 시스템 지시문 선택 회귀 테스트 (계획서 1-3 · REPORT §40)
#=> generation.prompt 로 지시문을 고를 때
#    1) 기본(v0)은 종전 SYSTEM_PROMPT 와 글자까지 같다 — 과거 측정이 그대로 유효
#    2) v4 는 "3~5문장" 한 곳만 다르고 나머지 문구는 v0 그대로다
#    3) v4 지시문을 답에 되뱉어도 기존 되뱉기 제거가 걸린다
#    4) 생성기가 system 인자 없이 부르면 설정의 지시문이 프롬프트에 들어간다
#   모델 없이 문자열과 가짜 llama 객체로만 확인한다.
#
#   실행: .venv/Scripts/python.exe bench/test_prompt_variant.py
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                                           # noqa: E402
from simplerag.generate import prompts                                 # noqa: E402
from simplerag.generate.gguf_generator import GgufGenerator            # noqa: E402

FAILED = []
TOTAL = [0]

V0_EXACT = (
    "당신은 사내 문서 검색 도우미입니다. 아래 [근거] 안의 내용만 사용해 한국어로 "
    "간결하게 답변하세요. 근거에 없는 내용은 추측하지 마세요. "
    "답변은 3~5문장으로 요약하고 마지막에 근거 번호를 [1] 형식으로 표기하세요."
)


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
# 가짜 llama — 받은 프롬프트만 기록한다
# -필드: prompts = create_completion 에 들어온 프롬프트 목록
#------------------------------------------------------------------
class FakeLlama:
    #------------------------------------------------------------------
    # 초기화
    #=> 기록 목록을 만든다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self):
        self.prompts = []

    #------------------------------------------------------------------
    # 생성 흉내
    #=> 프롬프트를 기록하고, 스트리밍이면 조각 하나를, 아니면 완성 dict 를 돌려준다.
    #
    # -in: prompt = 프롬프트, kwargs = 생성 옵션(stream 여부만 본다)
    #
    # -out: 스트림 iterator 또는 완성 dict
    # -out: error = 없음
    #------------------------------------------------------------------
    def create_completion(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if kwargs.get("stream"):
            return iter([{"choices": [{"text": "답"}]}])
        return {"choices": [{"text": "답"}]}


#------------------------------------------------------------------
# 지시문 문구 검사
#=> v0 가 종전과 같은지, v4 가 한 곳만 다른지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_text():
    print("\n[지시문 문구]")
    check("v0 = 종전 SYSTEM_PROMPT(글자까지 동일)", prompts.PROMPTS["v0"] == V0_EXACT == prompts.SYSTEM_PROMPT)
    v4 = prompts.PROMPTS["v4"]
    check("v4 에 '3~5문장' 이 없다", "3~5문장" not in v4)
    check("v4 에 '첫 문장에 바로' · '1~2문장' 이 있다", "첫 문장에 바로" in v4 and "1~2문장" in v4)
    for keep in ("아래 [근거] 안의 내용만 사용해 한국어로 간결하게 답변하세요.",
                 "근거에 없는 내용은 추측하지 마세요.",
                 "마지막에 근거 번호를 [1] 형식으로 표기하세요."):
        check("v4 가 v0 문구를 그대로 둔다: %s" % keep[:20], keep in v4 and keep in V0_EXACT)
    saved = config.PROMPT
    try:
        config.PROMPT = "v4"
        check("config.PROMPT=v4 → active_system_prompt() 가 v4", prompts.active_system_prompt() == v4)
        config.PROMPT = "v0"
        check("config.PROMPT=v0 → v0", prompts.active_system_prompt() == V0_EXACT)
    finally:
        config.PROMPT = saved
    try:
        prompts.active_system_prompt("v9")
        check("모르는 이름은 ValueError", False)
    except ValueError:
        check("모르는 이름은 ValueError", True)


#------------------------------------------------------------------
# 되뱉기 제거 검사
#=> v4 지시문을 답에 옮겨 적은 경우에도 parse_answer 가 지시문 조각을 걷어내는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_echo():
    print("\n[v4 되뱉기 제거]")
    body, _ = prompts.parse_answer("리프레시 휴가비는 100만원입니다. 답변은 1~2문장으로 요약합니다. [1]", 3)
    check("'답변은 1~2문장으로 요약합니다' 를 지운다", "1~2문장" not in body and "100만원" in body, body)
    body, _ = prompts.parse_answer("근속 2년 이상입니다. 근거 번호를 [1] 형식으로 표기합니다. [1]", 3)
    check("인용 표기 되뱉기도 종전처럼 지운다", "형식으로" not in body and "2년" in body, body)


#------------------------------------------------------------------
# 생성기 경로 검사
#=> 모델을 올리지 않고 GgufGenerator 에 가짜 llama 를 꽂아, 설정한 지시문이 프롬프트에 들어가는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_generator():
    print("\n[생성기 — system 인자 없이 부르면 설정의 지시문]")
    gen = object.__new__(GgufGenerator)
    import threading
    gen._llm, gen.n_ctx, gen._infer_lock = FakeLlama(), 2048, threading.Lock()
    gen.ensure_loaded = lambda: None
    saved = config.PROMPT
    try:
        config.PROMPT = "v4"
        list(gen.stream("질문", ["근거1", "근거2", "근거3"]))
        gen.generate("질문", ["근거1"])
        check("stream·generate 모두 v4 지시문", all(prompts.SYSTEM_PROMPT_V4 in p for p in gen._llm.prompts))
        config.PROMPT = "v0"
        list(gen.stream("질문", ["근거1"]))
        check("v0 로 돌리면 v0 지시문", V0_EXACT in gen._llm.prompts[-1] and "1~2문장" not in gen._llm.prompts[-1])
        list(gen.stream("질문", ["근거1"], system="직접 준 지시문"))
        check("system 인자를 주면 그것이 우선", "직접 준 지시문" in gen._llm.prompts[-1])
    finally:
        config.PROMPT = saved


#------------------------------------------------------------------
# 테스트 진입점
#=> 세 묶음을 돌리고 실패 수를 종료 코드로 돌려준다.
#
# -in: 없음
#
# -out: 0 = 전부 통과, 1 = 실패 있음
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_text()
    test_echo()
    test_generator()
    print("\n결과: %d 항목 중 실패 %d" % (TOTAL[0], len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
