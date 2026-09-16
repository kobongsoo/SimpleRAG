#------------------------------------------------------------------
# sLLM(GGUF/llama.cpp) CPU 속도 실측 벤치
#=> "로컬 RAG 가 실용 속도로 성립하는가" 를 판정하기 위한 최소 벤치.
#   RAG 실제 형태(고정 시스템프롬프트 + 근거청크 N개 + 질문)로 프롬프트를 만들고,
#   1) 모델 로딩 2) prefill(프롬프트 처리) 3) TTFT(첫 글자까지) 4) decode(생성) 를 잰다.
#
#   측정 원칙
#    - 매 측정 전 llm.reset() 으로 KV 캐시를 비워 prefill 을 항상 '차갑게' 잰다.
#      (그렇지 않으면 llama.cpp 의 프리픽스 재사용 때문에 2회차부터 prefill 이 사라짐)
#    - stream=True 로 첫 토큰 도착 시각을 직접 재서 TTFT 를 구한다.
#    - Qwen3 는 thinking 모드가 켜지면 사고토큰 수백개를 더 뱉어 3배 느려지므로,
#      채팅 템플릿을 직접 만들어 <think></think> 를 비운 상태로 강제한다.
#    - 반복 측정 후 '중앙값' 을 쓴다(노트북은 부스트/발열로 편차가 큼).
#
#   사용:
#     python bench/bench_llm.py --stage threads   # 1단계: 최적 스레드 수 탐색
#     python bench/bench_llm.py --stage full      # 2단계: 본 측정
#------------------------------------------------------------------

import argparse
import inspect
import json
import os
import statistics
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.abspath(os.path.join(_HERE, "..", "models"))
RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))

MODELS = {
    "qwen3-0.6b-q4": "Qwen3-0.6B-Q4_K_M.gguf",
    "qwen3-1.7b-q4": "Qwen3-1.7B-Q4_K_M.gguf",
}

# RAG 시스템 프롬프트 — 실제 운영과 같은 길이/성격으로 둔다(프리픽스 캐시 대상).
SYSTEM_PROMPT = (
    "당신은 사내 문서 검색 도우미입니다. 아래에 제공된 [근거] 안의 내용만 사용해 "
    "한국어로 간결하게 답변하세요. 근거에 없는 내용은 추측하지 말고 "
    "제공된 문서에서 확인할 수 없다고 답하세요. "
    "답변은 3~5문장으로 요약하고, 마지막에 사용한 근거 번호를 [1], [2] 형식으로 표기하세요."
)

QUESTION = "연차휴가 미사용분에 대한 수당 지급 기준과 소멸 시점을 알려줘."

# 근거 청크 생성용 한국어 원문 — 실제 사내규정 문서와 비슷한 밀도의 문장들.
_KO_SENTENCES = [
    "제25조(연차유급휴가) 회사는 1년간 80퍼센트 이상 출근한 직원에게 15일의 유급휴가를 부여한다.",
    "계속 근로기간이 1년 미만인 직원에게는 1개월 개근 시 1일의 유급휴가를 부여한다.",
    "3년 이상 계속 근로한 직원에게는 최초 1년을 초과하는 매 2년마다 1일을 가산한 휴가를 준다.",
    "가산휴가를 포함한 총 휴가일수는 25일을 한도로 한다.",
    "연차유급휴가는 1년간 행사하지 아니하면 소멸한다. 다만 사용자의 귀책사유로 사용하지 못한 경우에는 그러하지 아니하다.",
    "제26조(연차수당) 미사용 연차유급휴가에 대하여는 통상임금을 기준으로 수당을 지급한다.",
    "연차수당은 휴가청구권이 소멸한 날이 속하는 달의 다음 급여지급일에 지급함을 원칙으로 한다.",
    "회사가 근로기준법 제61조에 따른 사용촉진 조치를 적법하게 이행한 경우 미사용 휴가에 대한 보상 의무를 면한다.",
    "사용촉진은 휴가 소멸 6개월 전을 기준으로 직원별 미사용 일수를 서면으로 통보하는 절차를 포함한다.",
    "직원이 통보를 받고도 사용 시기를 지정하지 아니한 경우 회사가 사용 시기를 지정하여 서면 통보할 수 있다.",
    "제27조(휴가의 대체) 회사는 직원대표와 서면 합의에 따라 특정 근로일에 휴가를 갈음할 수 있다.",
    "출산전후휴가 및 육아휴직 기간은 연차휴가 산정 시 출근한 것으로 본다.",
    "업무상 부상 또는 질병으로 휴업한 기간 역시 출근한 것으로 간주하여 산정한다.",
    "퇴직 시에는 미사용 연차일수 전부에 대하여 수당을 정산하여 최종 급여와 함께 지급한다.",
    "연차휴가의 산정 기준일은 매년 1월 1일이며 회계연도 기준으로 일괄 관리한다.",
    "부서장은 업무에 중대한 지장이 있는 경우에 한하여 휴가 시기를 변경할 것을 요청할 수 있다.",
]


#------------------------------------------------------------------
# 목표 토큰 수에 맞춘 근거 청크 생성 (핵심)
#=> 실제 토크나이저로 세면서 문장을 채워 target_tokens 에 가장 가깝게 만든다.
#   문자열 길이로 어림잡으면 한국어는 오차가 커서 prefill 측정이 흐려진다.
#
# -in: llm           = 토크나이저로 쓸 Llama 인스턴스
# -in: target_tokens = 청크 1개의 목표 토큰 수
# -in: seed_idx      = 청크마다 시작 문장을 어긋나게 해 내용이 겹치지 않게 함
#
# -out: text = 목표 토큰 수에 도달한 한국어 청크 본문
#------------------------------------------------------------------
def make_chunk(llm, target_tokens, seed_idx):
    parts = []
    i = seed_idx
    while True:
        parts.append(_KO_SENTENCES[i % len(_KO_SENTENCES)])
        i += 1
        text = " ".join(parts)
        if len(llm.tokenize(text.encode("utf-8"), add_bos=False)) >= target_tokens:
            return text


#------------------------------------------------------------------
# Qwen3 채팅 프롬프트 조립 (thinking 비활성)
#=> Qwen3 템플릿에서 enable_thinking=False 일 때와 동일한 형태를 직접 만든다.
#   <think> 블록을 미리 닫아두면 모델이 사고토큰을 생성하지 않고 바로 답한다.
#
# -in: system, user = 시스템/사용자 메시지 본문
#
# -out: prompt = 모델에 그대로 넣을 완성 프롬프트 문자열
#------------------------------------------------------------------
def build_prompt(system, user):
    return (
        "<|im_start|>system\n" + system + "<|im_end|>\n"
        "<|im_start|>user\n" + user + "<|im_end|>\n"
        "<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


#------------------------------------------------------------------
# RAG 사용자 메시지 구성
#=> [근거] N개 + 질문 형태로, 실제 파이프라인이 만드는 프롬프트를 모사한다.
#
# -in: llm, n_chunks, chunk_tokens
#
# -out: user_msg = 사용자 메시지 본문
#------------------------------------------------------------------
def build_rag_user_msg(llm, n_chunks, chunk_tokens):
    blocks = []
    for k in range(n_chunks):
        body = make_chunk(llm, chunk_tokens, seed_idx=k * 5)
        blocks.append("[" + str(k + 1) + "] " + body)
    return "[근거]\n" + "\n\n".join(blocks) + "\n\n[질문]\n" + QUESTION


#------------------------------------------------------------------
# Llama 인스턴스 생성 (버전별 인자 차이 흡수)
#=> llama-cpp-python 버전에 따라 flash_attn 등 인자명이 바뀌므로,
#   실제 시그니처에 있는 것만 골라 넘긴다(없는 인자로 죽지 않게).
#
# -in: model_path, n_threads, n_ctx
#
# -out: (llm, load_sec) = 인스턴스와 로딩 소요 초
#------------------------------------------------------------------
def load_llm(model_path, n_threads, n_ctx, flash_attn=True, n_batch=512):
    from llama_cpp import Llama

    wanted = {
        "model_path": model_path,
        "n_ctx": n_ctx,
        "n_threads": n_threads,
        "n_threads_batch": n_threads,
        "n_batch": n_batch,
        "logits_all": False,
        "verbose": False,
        "flash_attn": flash_attn,
    }
    sig = set(inspect.signature(Llama.__init__).parameters)
    kwargs = {k: v for k, v in wanted.items() if k in sig}

    t0 = time.perf_counter()
    llm = Llama(**kwargs)
    return llm, time.perf_counter() - t0


#------------------------------------------------------------------
# 1회 측정 (핵심)
#=> KV 캐시를 비운 뒤 스트리밍으로 생성하며 TTFT/decode 를 직접 잰다.
#    1) llm.reset() 으로 prefill 을 차갑게 강제
#    2) 첫 청크 도착 시각 = TTFT (프롬프트 처리 + 첫 토큰)
#    3) 이후 토큰들의 평균 속도 = decode tok/s
#
# -in: llm, prompt, max_tokens
#
# -out: dict = prompt_tokens/ttft_s/gen_tokens/decode_tps/prefill_tps/total_s/text
#------------------------------------------------------------------
def measure_once(llm, prompt, max_tokens):
    n_prompt = len(llm.tokenize(prompt.encode("utf-8"), add_bos=True))

    llm.reset()  # 프리픽스 캐시 무효화. 없으면 2회차부터 prefill 이 사라진다.

    t0 = time.perf_counter()
    t_first = None
    n_gen = 0
    out = []

    for chunk in llm.create_completion(
        prompt, max_tokens=max_tokens, temperature=0.0,
        stop=["<|im_end|>", "<|endoftext|>"], stream=True,
    ):
        piece = chunk["choices"][0]["text"]
        if t_first is None:
            t_first = time.perf_counter()
        n_gen += 1
        out.append(piece)

    t_end = time.perf_counter()
    if t_first is None:          # 아무것도 생성 못 한 경우 방어
        t_first = t_end

    # 스트리밍 청크 수는 실제 토큰 수와 다를 수 있다 — 한글처럼 여러 바이트를
    # 쓰는 글자는 UTF-8 경계가 맞을 때까지 청크를 비워두고 모으기 때문이다.
    # llm.n_tokens(=프롬프트+생성 누적)를 쓰면 정확한 생성 토큰 수가 나온다.
    n_exact = getattr(llm, "n_tokens", 0) - n_prompt
    n_tok = n_exact if n_exact > 0 else n_gen

    ttft = t_first - t0
    decode_s = max(t_end - t_first, 1e-9)
    return {
        "prompt_tokens": n_prompt,
        "ttft_s": ttft,
        "gen_tokens": n_tok,
        "stream_chunks": n_gen,
        "decode_tps": (n_tok - 1) / decode_s if n_tok > 1 else 0.0,
        "prefill_tps": n_prompt / ttft if ttft > 0 else 0.0,
        "total_s": t_end - t0,
        "text": "".join(out),
    }


#------------------------------------------------------------------
# 한 조건(모델×스레드×시나리오)을 반복 측정해 중앙값으로 요약
#=> 노트북 CPU 는 부스트/발열 때문에 1회 측정이 못 미덥다. warmup 1회 후 repeat 회 측정.
#
# -in: llm, prompt, max_tokens, repeat
#
# -out: dict = 중앙값 요약 + 마지막 생성문 일부(품질 눈검사용)
#------------------------------------------------------------------
def measure(llm, prompt, max_tokens, repeat):
    measure_once(llm, prompt, 8)          # warmup(짧게) — 커널/캐시 예열
    runs = [measure_once(llm, prompt, max_tokens) for _ in range(repeat)]

    def med(key):
        return statistics.median(r[key] for r in runs)

    return {
        "prompt_tokens": runs[-1]["prompt_tokens"],
        "gen_tokens": int(med("gen_tokens")),
        "ttft_s": round(med("ttft_s"), 2),
        "prefill_tps": round(med("prefill_tps"), 1),
        "decode_tps": round(med("decode_tps"), 1),
        "total_s": round(med("total_s"), 2),
        "sample": runs[-1]["text"][:200].replace("\n", " "),
    }


#------------------------------------------------------------------
# 별칭 -> 모델 파일 경로 (없으면 안내 후 종료)
#------------------------------------------------------------------
def model_path(alias):
    p = os.path.join(MODELS_DIR, MODELS[alias])
    if not os.path.isfile(p):
        print("[error] 모델 없음: " + p, file=sys.stderr)
        print("        먼저 'python bench/download_models.py' 를 실행하세요.", file=sys.stderr)
        raise SystemExit(2)
    return p


#------------------------------------------------------------------
# 1단계: 최적 스레드 수 탐색
#=> i5-1340P 같은 P/E 하이브리드 CPU 는 스레드를 많이 준다고 빨라지지 않는다.
#   동기화 배리어가 가장 느린 E-core 를 기다리기 때문에 실측으로 골라야 한다.
#------------------------------------------------------------------
def stage_threads(args):
    results = []
    for alias in args.models:
        for nt in args.threads:
            llm, load_s = load_llm(model_path(alias), nt, args.n_ctx)
            user = build_rag_user_msg(llm, args.chunks, args.chunk_tokens)
            prompt = build_prompt(SYSTEM_PROMPT, user)
            r = measure(llm, prompt, max_tokens=64, repeat=args.repeat)
            r.update(model=alias, n_threads=nt, load_s=round(load_s, 2))
            results.append(r)
            print("  {:<14} threads={:<3} prefill={:>7.1f} t/s  decode={:>6.1f} t/s  TTFT={:>5.2f}s".format(
                alias, nt, r["prefill_tps"], r["decode_tps"], r["ttft_s"]))
            llm.close() if hasattr(llm, "close") else None
            del llm
    return results


#------------------------------------------------------------------
# 시나리오 문자열 파싱
#=> "top3x256:3:256,top3x128:3:128" 형태를 (이름, 청크수, 청크토큰) 목록으로.
#   청크 구성이 TTFT 를 지배하므로, 스윗스팟 탐색을 인자로 열어 둔다.
#
# -in: spec = 콤마로 구분한 "이름:청크수:청크토큰" 문자열
#
# -out: [(name, n_chunks, chunk_tokens), ...]
#------------------------------------------------------------------
def parse_scenarios(spec):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, n, t = item.split(":")
        out.append((name, int(n), int(t)))
    return out


#------------------------------------------------------------------
# 2단계: 본 측정 (시나리오별)
#=> 청크 크기 256 vs 512 를 비교해 'prefill 은 컨텍스트 길이에 정비례' 를 확인한다.
#   답변 길이는 실제 운영값(256 토큰)으로 둔다.
#------------------------------------------------------------------
def stage_full(args):
    scenarios = parse_scenarios(args.scenarios)
    results = []
    for alias in args.models:
        nt = args.threads[0]
        llm, load_s = load_llm(model_path(alias), nt, args.n_ctx)
        for name, n_chunks, ctok in scenarios:
            user = build_rag_user_msg(llm, n_chunks, ctok)
            prompt = build_prompt(SYSTEM_PROMPT, user)
            r = measure(llm, prompt, max_tokens=args.max_tokens, repeat=args.repeat)
            r.update(model=alias, n_threads=nt, scenario=name, load_s=round(load_s, 2))
            results.append(r)
            print("  {:<14} {:<10} prompt={:>5}tok  TTFT={:>5.2f}s  decode={:>6.1f} t/s  total={:>6.2f}s".format(
                alias, name, r["prompt_tokens"], r["ttft_s"], r["decode_tps"], r["total_s"]))
        llm.close() if hasattr(llm, "close") else None
        del llm
    return results


def main():
    p = argparse.ArgumentParser(description="GGUF sLLM CPU 속도 실측")
    p.add_argument("--stage", choices=["threads", "full"], default="threads")
    p.add_argument("--models", nargs="+", default=list(MODELS))
    p.add_argument("--threads", nargs="+", type=int, default=[4, 6, 8, 12])
    p.add_argument("--chunks", type=int, default=3)
    p.add_argument("--chunk-tokens", type=int, default=256)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--n-ctx", type=int, default=4096)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--scenarios", default="top3x256:3:256,top3x512:3:512,top5x256:5:256",
                   help="이름:청크수:청크토큰 을 콤마로 나열")
    p.add_argument("--tag", default="", help="결과 파일명 접미사(덮어쓰기 방지)")
    args = p.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("[bench] stage={} models={} repeat={}\n".format(args.stage, args.models, args.repeat))

    results = stage_threads(args) if args.stage == "threads" else stage_full(args)

    out = os.path.join(RESULTS_DIR, "bench_" + args.stage + args.tag + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n[bench] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
