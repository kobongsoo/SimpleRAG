#------------------------------------------------------------------
# 정밀 모드 회귀 테스트 (계획서 D8 ③ · REPORT §44)
#=> 기본은 빠른 0.6B, `--precise` 를 붙인 실행만 1.7B 로 답하는 구조를 지킨다.
#    1) CLI 가 --precise / --model 을 어떻게 모델 별칭으로 바꾸는지
#    2) 모델 별칭 → 화면 표시("빠름/정밀")
#    3) 생성기가 별칭대로 GGUF 경로를 잡는지(모델 적재는 하지 않는다)
#    4) 백엔드 측정 캐시가 모델마다 따로 남는지 — 모드를 오갈 때 재측정하지 않게
#
#   실행: .venv/Scripts/python.exe bench/test_precise_mode.py
#------------------------------------------------------------------

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import cli, config                        # noqa: E402
from simplerag.generate import backend as gen_backend    # noqa: E402
from simplerag.generate.gguf_generator import GgufGenerator  # noqa: E402

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


#------------------------------------------------------------------
# argparse 결과 흉내
#
# -in: precise = --precise 여부, model = --model 값
#
# -out: precise·model 속성을 가진 객체
# -out: error = 없음
#------------------------------------------------------------------
def args(precise=False, model=None):
    return type("A", (), {"precise": precise, "model": model})()


#------------------------------------------------------------------
# 모델 고르기·표시 검사
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_pick():
    print("\n[CLI — 어떤 모델로 답할지]")
    check("아무 것도 안 주면 설정 기본값(None → App 이 결정)", cli._pick_model(args()) is None)
    check("--precise → 1.7B", cli._pick_model(args(precise=True)) == config.PRECISE_GEN_MODEL)
    check("--model 로 직접 지정하면 그대로",
          cli._pick_model(args(model=config.PRECISE_GEN_MODEL)) == config.PRECISE_GEN_MODEL)
    check("--precise 와 같은 모델을 함께 줘도 통과",
          cli._pick_model(args(precise=True, model=config.PRECISE_GEN_MODEL)) == config.PRECISE_GEN_MODEL)
    try:
        cli._pick_model(args(precise=True, model=config.FAST_GEN_MODEL))
        check("--precise 와 다른 --model 은 오류", False)
    except SystemExit as e:
        check("--precise 와 다른 --model 은 오류", "함께 쓸 수 없습니다" in str(e), e)
    check("표시 이름 빠름/정밀",
          (cli._model_mode(config.FAST_GEN_MODEL), cli._model_mode(config.PRECISE_GEN_MODEL)) == ("빠름", "정밀"))
    check("모르는 별칭은 그대로 보여 준다", cli._model_mode("mystery-7b") == "mystery-7b")
    check("기본값은 빠름(0.6B)", config.DEFAULT_GEN_MODEL == config.FAST_GEN_MODEL == "qwen3-0.6b-q4")
    check("정밀 모델이 목록에 있다", config.PRECISE_GEN_MODEL in config.GEN_MODELS)


#------------------------------------------------------------------
# 생성기 경로 검사 — 모델을 올리지 않고 별칭만 확인한다
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_generator_alias():
    print("\n[생성기 — 별칭대로 GGUF 를 찾는다]")
    for alias, name in ((config.FAST_GEN_MODEL, "Qwen3-0.6B"), (config.PRECISE_GEN_MODEL, "Qwen3-1.7B")):
        gen = GgufGenerator(alias)
        check("%s → %s" % (alias, name), gen.alias == alias and name in config.gen_model_path(alias))
    check("정밀 모델 파일이 실제로 있다(없으면 --precise 는 안내 후 실패)",
          os.path.isfile(config.gen_model_path(config.PRECISE_GEN_MODEL)),
          config.gen_model_path(config.PRECISE_GEN_MODEL))


#------------------------------------------------------------------
# 백엔드 측정 캐시 검사 — 모드를 오가도 다시 재지 않는다
#=> 캐시 경로를 임시 폴더로 돌려 운영 캐시를 건드리지 않는다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_cache():
    print("\n[백엔드 캐시 — 모델마다 따로 남는다]")
    saved_path = config.BACKEND_CACHE_PATH
    tmp = tempfile.mkdtemp(prefix="simplerag_cache_")
    try:
        config.BACKEND_CACHE_PATH = os.path.join(tmp, "gen_backend.json")
        fast_key, precise_key = gen_backend.cache_key(config.FAST_GEN_MODEL), gen_backend.cache_key(config.PRECISE_GEN_MODEL)
        check("모델이 다르면 캐시 키도 다르다", fast_key != precise_key)

        # 옛 형식(측정 한 건이 최상위)도 읽어야 한다 — 이전 버전이 만든 파일
        json.dump({"key": fast_key, "backend": "vulkan", "reason": "옛 형식"},
                  io.open(config.BACKEND_CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        check("옛 형식 파일을 읽는다", (gen_backend.peek(config.FAST_GEN_MODEL) or {}).get("reason") == "옛 형식")
        check("다른 모델은 없다고 답한다", gen_backend.peek(config.PRECISE_GEN_MODEL) is None)

        gen_backend._save({"key": precise_key, "backend": "vulkan", "reason": "정밀 측정"})
        check("정밀을 저장해도 빠름 측정이 남는다",
              (gen_backend.peek(config.FAST_GEN_MODEL) or {}).get("reason") == "옛 형식")
        check("정밀 측정도 읽힌다", (gen_backend.peek(config.PRECISE_GEN_MODEL) or {}).get("reason") == "정밀 측정")

        gen_backend._save({"key": precise_key, "backend": "cpu", "reason": "정밀 재측정"})
        entries = json.load(io.open(config.BACKEND_CACHE_PATH, encoding="utf-8"))["entries"]
        check("같은 키를 다시 저장하면 덮어쓴다(중복 없음)", len(entries) == 2, entries)
        check("최신 측정이 읽힌다", (gen_backend.peek(config.PRECISE_GEN_MODEL) or {}).get("reason") == "정밀 재측정")

        for i in range(gen_backend._CACHE_KEEP + 2):
            gen_backend._save({"key": dict(fast_key, host="pc%d" % i), "backend": "cpu"})
        entries = json.load(io.open(config.BACKEND_CACHE_PATH, encoding="utf-8"))["entries"]
        check("보관 개수를 넘으면 오래된 것부터 버린다", len(entries) == gen_backend._CACHE_KEEP, len(entries))

        io.open(config.BACKEND_CACHE_PATH, "w", encoding="utf-8").write("{깨진 파일")
        check("깨진 캐시는 없는 것으로 본다", gen_backend.peek(config.FAST_GEN_MODEL) is None)
    finally:
        config.BACKEND_CACHE_PATH = saved_path


#------------------------------------------------------------------
# 도움말 출력 검사 — argparse 의 %-서식 함정
#=> argparse 는 help 문자열을 %-서식으로 처리한다. "+4.4%p" 처럼 %가 들어가면
#   `--help` 가 ValueError 로 죽는다(실제로 exe 에서 `ask --help` 가 죽었다).
#   모든 하위 명령의 도움말을 실제로 만들어 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def test_help():
    print("\n[도움말 — 모든 하위 명령이 --help 로 죽지 않는다]")
    import contextlib
    for cmd in ("ask", "chat", "warmup", "search", "index", "status", "backend"):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cli.main([cmd, "--help"])
            ok, note = False, "SystemExit 가 없었다"
        except SystemExit as e:
            ok, note = e.code in (0, None), e
        except Exception as e:                      # ValueError: unsupported format character 등
            ok, note = False, "%s: %s" % (type(e).__name__, e)
        check("%s --help" % cmd, ok, note)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(["ask", "--help"])
    except SystemExit:
        pass
    check("ask 도움말에 --precise 안내가 있다", "--precise" in buf.getvalue(), buf.getvalue()[:200])


#------------------------------------------------------------------
# 테스트 진입점
#
# -in: 없음
#
# -out: 0 = 전부 통과, 1 = 실패 있음
# -out: error = 없음
#------------------------------------------------------------------
def main():
    test_pick()
    test_generator_alias()
    test_cache()
    test_help()
    print("\n결과: %d 항목 중 실패 %d" % (TOTAL[0], len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
