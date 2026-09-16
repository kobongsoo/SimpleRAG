#------------------------------------------------------------------
# 진단: '느린 원인'이 모델인가, 파이썬 래퍼 오버헤드인가
#=> 1차 실측에서 Qwen3-0.6B 가 22 tok/s 로 이론상한(259)의 8% 밖에 안 나왔다.
#   decode 는 메모리 대역폭에 묶인 작업이라 이 정도로 낮을 이유가 없으므로,
#   llama.cpp 내부(C) 시간과 파이썬 왕복 시간을 분리해 원인을 특정한다.
#
#   비교 항목
#     A. streaming  = 토큰마다 파이썬 제너레이터를 왕복 (현재 bench 방식)
#     B. non-stream = 한 번에 받기 (파이썬 왕복 최소)
#     C. raw eval   = llm.eval + argmax 직접 루프 (샘플러/후처리 배제)
#     D. llama.cpp 내부 타이밍 = C 구간의 순수 eval 속도
#
#   C 가 A/B 보다 크게 빠르면 병목은 래퍼(샘플링/후처리)에 있다.
#
#   사용:  python bench/diag_overhead.py
#------------------------------------------------------------------

import ctypes
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from bench_llm import (MODELS, MODELS_DIR, SYSTEM_PROMPT, build_prompt,   # noqa: E402
                       build_rag_user_msg, load_llm)

N_GEN = 64
N_THREADS = 4


#------------------------------------------------------------------
# A. 스트리밍 생성 속도
#=> 현재 bench_llm 이 쓰는 경로. 토큰마다 파이썬으로 값이 올라온다.
#------------------------------------------------------------------
def run_streaming(llm, prompt):
    llm.reset()
    t0 = time.perf_counter()
    t_first = None
    for _ in llm.create_completion(prompt, max_tokens=N_GEN, temperature=0.0,
                                   stop=["<|im_end|>"], stream=True):
        if t_first is None:
            t_first = time.perf_counter()
    t_end = time.perf_counter()
    n = llm.n_tokens - len(llm.tokenize(prompt.encode("utf-8"), add_bos=True))
    return n, t_first - t0, (n - 1) / max(t_end - t_first, 1e-9)


#------------------------------------------------------------------
# B. 논스트리밍 생성 속도
#=> 결과를 한 번에 받는다. 파이썬 왕복이 1회뿐이라 래퍼 오버헤드가 줄어든다.
#   A 와 B 가 비슷하면 제너레이터 왕복은 범인이 아니다.
#------------------------------------------------------------------
def run_nonstream(llm, prompt):
    llm.reset()
    t0 = time.perf_counter()
    r = llm.create_completion(prompt, max_tokens=N_GEN, temperature=0.0,
                              stop=["<|im_end|>"], stream=False)
    t_end = time.perf_counter()
    n = r["usage"]["completion_tokens"]
    return n, t_end - t0


#------------------------------------------------------------------
# C. 저수준 eval 루프 (핵심 대조군)
#=> llm.eval(tokens) 로 직접 돌리고 argmax 로만 다음 토큰을 고른다.
#   샘플러 체인/페널티/디토크나이즈 등 래퍼가 하는 일을 전부 뺀 '맨몸' 속도.
#   여기서 빨라지면 병목은 모델이 아니라 래퍼다.
#------------------------------------------------------------------
def run_raw(llm, prompt):
    toks = llm.tokenize(prompt.encode("utf-8"), add_bos=True)

    llm.reset()
    t0 = time.perf_counter()
    llm.eval(toks)                      # prefill
    t_prefill = time.perf_counter()

    # 무엇을 샘플링하는지는 '속도' 측정과 무관하므로, 고정 토큰을 반복 투입해
    # 순수 forward 비용만 잰다. (scores 버퍼는 logits_all=False 일 때 레이아웃이
    # 버전마다 달라 argmax 로 읽으면 깨지기 쉽다 — 측정 목적상 불필요하다.)
    fixed = toks[-1]
    for _ in range(N_GEN - 1):
        llm.eval([fixed])
    t_end = time.perf_counter()

    return (len(toks) / max(t_prefill - t0, 1e-9),            # prefill tok/s
            (N_GEN - 1) / max(t_end - t_prefill, 1e-9))       # decode tok/s


#------------------------------------------------------------------
# D. llama.cpp 내부 타이밍 출력
#=> C 구간이 실제로 몇 ms 를 썼는지 llama.cpp 자신이 집계한 값을 찍는다.
#   (llama_perf_context_print 는 stderr 로 나간다)
#------------------------------------------------------------------
def print_internal_timings(llm):
    try:
        import llama_cpp
        fn = llama_cpp.llama_perf_context_print
        fn.argtypes = [ctypes.c_void_p]
        ctx = llm._ctx.ctx if hasattr(llm, "_ctx") else llm.ctx
        fn(ctx)
    except Exception as e:
        print("  (내부 타이밍 사용 불가: {}: {})".format(type(e).__name__, e))


def main():
    alias = sys.argv[1] if len(sys.argv) > 1 else "qwen3-0.6b-q4"
    path = os.path.join(MODELS_DIR, MODELS[alias])
    if not os.path.isfile(path):
        print("모델 없음: " + path, file=sys.stderr)
        return 2

    print("[diag] {}  threads={}  gen={}\n".format(alias, N_THREADS, N_GEN))
    llm, load_s = load_llm(path, N_THREADS, 4096)
    print("  로딩: {:.2f}s".format(load_s))

    user = build_rag_user_msg(llm, 3, 256)
    prompt = build_prompt(SYSTEM_PROMPT, user)
    n_prompt = len(llm.tokenize(prompt.encode("utf-8"), add_bos=True))
    print("  프롬프트: {} tok\n".format(n_prompt))

    # 예열 — 첫 실행은 커널/페이지 폴트 때문에 항상 느리다.
    llm.reset()
    llm.create_completion(prompt, max_tokens=8, temperature=0.0)

    n, ttft, dtps = run_streaming(llm, prompt)
    print("  A. streaming     decode={:>7.1f} tok/s   TTFT={:.2f}s  (gen {} tok)".format(dtps, ttft, n))

    n, total = run_nonstream(llm, prompt)
    print("  B. non-stream    전체={:>9.2f}s          (gen {} tok)".format(total, n))

    ptps, dtps_raw = run_raw(llm, prompt)
    print("  C. raw eval      decode={:>7.1f} tok/s   prefill={:.1f} tok/s".format(dtps_raw, ptps))

    print("\n  D. llama.cpp 내부 타이밍:")
    print_internal_timings(llm)

    # E. 설정 변형 — flash_attn / 스레드 수가 CPU 백엔드에서 실제로 이득인지 확인.
    print("\n  E. 설정 변형 (raw eval 기준):")
    del llm
    for fa in (True, False):
        for nt in (2, 4, 8):
            l2, _ = load_llm(path, nt, 4096, flash_attn=fa)
            p2, d2 = run_raw(l2, prompt)
            print("     flash_attn={:<5} threads={:<3} prefill={:>7.1f}  decode={:>6.1f} tok/s".format(
                str(fa), nt, p2, d2))
            del l2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
