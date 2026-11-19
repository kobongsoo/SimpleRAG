#------------------------------------------------------------------
# 근거 배치(generation.reverse_context_models) 회귀 시험 — 근거배치 설계서 20261119
#=> 1위 근거를 질문 바로 앞에 두는 배치가 설계대로인지 본다. 모델·인덱스는 쓰지 않는다.
#
#   확인하는 것
#    A. 조립 — reverse=False 는 종전과 글자까지 같다 / reverse=True 는 [3] → [2] → [1] 순서, 번호는 순위 그대로
#       기대 문자열은 Rust 판 prompts.rs 시험(ctx_order)과 같은 글이다 → 두 판이 글자까지 같다
#    B. 설정 — 줄 없음 = [qwen3-1.7b-q4] / [] = 모두 끔 / 대문자·공백 정리 / 목록 아님·틀린 별칭은 설정 오류
#    C. 생성기 판단 — 목록에 든 모델 + 지시문 없는 호출(질문 답변)만 뒤집는다, 개요처럼 지시문을 준 호출은 그대로
#
#   실행: .venv/Scripts/python.exe bench/test_ctx_order.py
#------------------------------------------------------------------

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

FAILED = []
TOTAL = [0]
PY = sys.executable

# Rust 판 시험(prompts.rs ctx_order)과 같은 입력·기대 글 — 한쪽을 바꾸면 다른 쪽도 바꾼다
CHUNKS = ["가 근거", "나 근거", "다 근거"]
QUERY = "질문?"
WANT_RANK = "[근거]\n[1] 가 근거\n\n[2] 나 근거\n\n[3] 다 근거\n\n[질문]\n질문?"
WANT_REVERSE = "[근거]\n[3] 다 근거\n\n[2] 나 근거\n\n[1] 가 근거\n\n[질문]\n질문?"


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 돌린다
#
# -in: name = 시험 이름
# -in: cond = 참이면 통과
# -in: note = 실패 때 함께 찍을 값
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def check(name, cond, note=""):
    TOTAL[0] += 1
    print(("  OK   %s" if cond else "  FAIL %s  " + str(note)) % name)
    if not cond:
        FAILED.append(name)


#------------------------------------------------------------------
# 새 프로세스에서 config 를 읽어 근거 배치 목록 받아 오기
#=> config 는 import 할 때 한 번 읽으므로 조건마다 새 프로세스를 띄운다.
#
# -in: yaml_text = 쓸 config.yaml 내용
# -in: tmp       = 임시 폴더
#
# -out: (returncode, 정렬한 별칭 목록 또는 None, stderr)
# -out: error = 없음
#------------------------------------------------------------------
def load_models(yaml_text, tmp):
    e = {k: v for k, v in os.environ.items() if not k.startswith("SIMPLERAG_")}
    # 한글 오류문이 깨지지 않게 UTF-8 고정
    e["PYTHONIOENCODING"] = "utf-8"
    path = os.path.join(tmp, "cfg_%d.yaml" % TOTAL[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    e["SIMPLERAG_CONFIG"] = path
    code = ("import json,sys; sys.path.insert(0,'src'); from simplerag import config as c; "
            "print(json.dumps(sorted(c.REVERSE_CONTEXT_MODELS)))")
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=e)
    vals = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    return r.returncode, vals, r.stderr


#------------------------------------------------------------------
# A. 사용자 메시지 조립
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 모은다)
#------------------------------------------------------------------
def test_build():
    print("\n[A. 조립]")
    from simplerag import config
    from simplerag.generate import prompts
    # 표 펴기가 켜져 있어도 이 근거들엔 표가 없어 글이 그대로다
    got = prompts.build_user_message(QUERY, CHUNKS)
    check("기본(reverse 없음) = 1위부터, 종전 글과 같다", got == WANT_RANK, repr(got))
    got = prompts.build_user_message(QUERY, CHUNKS, False)
    check("reverse=False = 1위부터", got == WANT_RANK, repr(got))
    got = prompts.build_user_message(QUERY, CHUNKS, True)
    check("reverse=True = [3] → [2] → [1] → [질문], 번호는 순위 그대로", got == WANT_REVERSE, repr(got))
    got = prompts.build_user_message(QUERY, ["하나"], True)
    check("근거 1개면 뒤집어도 같다", got == "[근거]\n[1] 하나\n\n[질문]\n질문?", repr(got))
    got = prompts.build_user_message(QUERY, [], True)
    check("근거 0개도 오류 없이 같은 틀", got == prompts.build_user_message(QUERY, []), repr(got))
    check("코드 기본값 = qwen3-1.7b-q4 하나", tuple(config.REVERSE_CONTEXT_DEFAULT) == ("qwen3-1.7b-q4",))


#------------------------------------------------------------------
# B. 설정 읽기
#
# -in: tmp = 임시 폴더
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 모은다)
#------------------------------------------------------------------
def test_settings(tmp):
    print("\n[B. 설정]")
    rc, v, err = load_models("generation:\n  top_k: 3\n", tmp)
    check("줄 없음 → [qwen3-1.7b-q4]", rc == 0 and v == ["qwen3-1.7b-q4"], (rc, v, err))
    rc, v, err = load_models("generation:\n  reverse_context_models: []\n", tmp)
    check("[] → 모두 끔", rc == 0 and v == [], (rc, v, err))
    rc, v, err = load_models("generation:\n  reverse_context_models:\n", tmp)
    check("값을 비우면(null) 코드 기본값", rc == 0 and v == ["qwen3-1.7b-q4"], (rc, v, err))
    rc, v, err = load_models("generation:\n  reverse_context_models: [' Qwen3-1.7B-Q4 ', midm-2.0-mini-q4]\n", tmp)
    check("대문자·공백 정리, 여러 개", rc == 0 and v == ["midm-2.0-mini-q4", "qwen3-1.7b-q4"], (rc, v, err))
    rc, v, err = load_models("generation:\n  reverse_context_models: qwen3-1.7b-q4\n", tmp)
    check("목록 아님 → 설정 오류", rc != 0 and "모델 별칭 목록" in err, (rc, v, err[-300:]))
    rc, v, err = load_models("generation:\n  reverse_context_models: [qwen3 1.7b]\n", tmp)
    check("틀린 별칭 → 설정 오류(항목을 밝힘)", rc != 0 and "qwen3 1.7b" in err, (rc, v, err[-300:]))


#------------------------------------------------------------------
# C. 생성기의 뒤집기 판단 — 모델을 올리지 않고 _fit_prompt 로 넘기는 값만 본다
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 모은다)
#------------------------------------------------------------------
def test_generator():
    print("\n[C. 생성기 판단]")
    from simplerag import config
    from simplerag.generate import gguf_generator

    seen = []

    # 가짜 생성기 — stream 이 _fit_prompt 에 넘기는 reverse 만 기록하고 생성 직전에 멈춘다
    class Stop(Exception):
        pass

    # 모델을 올리지 않으려고 적재·조립만 가로챈다
    class Fake(gguf_generator.GgufGenerator):
        def __init__(self, alias):
            self.alias = alias

        def ensure_loaded(self):
            pass

        def _fit_prompt(self, system, query, chunks, max_tokens, reverse=False):
            seen.append(reverse)
            raise Stop()

    # 한 번 부르고 기록된 reverse 를 돌려준다
    def run(alias, system=None):
        try:
            next(iter(Fake(alias).stream(QUERY, CHUNKS, system=system)))
        except Stop:
            pass
        return seen.pop()

    old = config.REVERSE_CONTEXT_MODELS
    try:
        config.REVERSE_CONTEXT_MODELS = frozenset(["qwen3-1.7b-q4"])
        check("목록에 든 1.7B · 질문 답변 → 뒤집음", run("qwen3-1.7b-q4") is True)
        check("목록에 든 1.7B · 지시문을 준 호출(개요) → 그대로", run("qwen3-1.7b-q4", system="개요 지시문") is False)
        check("목록에 없는 Mi:dm → 그대로", run("midm-2.0-mini-q4") is False)
        config.REVERSE_CONTEXT_MODELS = frozenset()
        check("빈 목록이면 1.7B 도 그대로", run("qwen3-1.7b-q4") is False)
    finally:
        config.REVERSE_CONTEXT_MODELS = old


#------------------------------------------------------------------
# 전체 실행
#
# -in: 없음
#
# -out: 0 = 모두 통과, 1 = 실패 있음
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_build()
    with tempfile.TemporaryDirectory() as tmp:
        test_settings(tmp)
    test_generator()
    print("\n%d/%d 통과" % (TOTAL[0] - len(FAILED), TOTAL[0]))
    if FAILED:
        print("실패:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
