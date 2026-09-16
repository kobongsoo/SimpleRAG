#------------------------------------------------------------------
# 골든셋(dataset/golden_dataset.jsonl) RAGAS 4지표 측정
#=> sample 20건 운영 인덱스로 100문항을 실제 파이프라인에 태워 답변·검색 근거를 모으고,
#   RAGAS 0.4.3 으로 Faithfulness / Answer Relevancy / Context Precision / Context Recall 을 잰다.
#
#   단계
#    collect : SimpleRAG 로 문항마다 response(답변)·retrieved_contexts(sLLM 에 들어간 근거 원문)를 모은다
#    score   : 판정 LLM(vLLM qwen3.6-35b-a3b)·임베딩(qwen3-embedding:8b)으로 지표를 채점한다
#
#   판정 방식 (REPORT §36)
#    - F / AR / CR : thinking 끔. 주장·문장 단위 판정이라 엄격성 문제가 없고 빠르다
#    - CP          : thinking 켬. 끄면 "근거 1건이 정답 전체를 담아야 유용" 으로 판정해 과소평가된다(g003)
#    - 판정 LLM 이 가끔 형식을 어겨 NaN 이 나오면 그 문항·지표만 다시 채점한다(--retry)
#
#   실험별 결과 폴더 (계획서 0-2)
#    --exp ID      → results/ragas_golden/<ID>/ 에 답변·점수를 둔다. 생략하면 폴더 최상위(= §36 기준선)
#    --responses P → 다른 폴더의 responses.json 으로 채점만 한다(같은 답변 재채점 = 판정 잡음 측정)
#
#   사용 (collect 는 .venv, score 는 ragas 가 깔린 시스템 python):
#     .venv\Scripts\python bench/ragas_golden.py collect --exp e11_dedup
#     python bench/ragas_golden.py score --exp e11_dedup --no-think --metrics f,ar,cr --resume
#     python bench/ragas_golden.py score --exp e11_dedup --metrics cp
#     python bench/ragas_compare.py <A 폴더> <B 폴더> --noise n01_rep2   # 두 실험 비교
#------------------------------------------------------------------

import argparse
import collections
import io
import json
import math
import os
import sys
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

GOLDEN = os.path.join(_ROOT, "dataset", "golden_dataset.jsonl")
OUT_DIR = os.path.join(_ROOT, "results", "ragas_golden")
RESPONSES = os.path.join(OUT_DIR, "responses.json")

JUDGE_URL = os.environ.get("RAGAS_JUDGE_URL", "http://10.10.19.50:18001/v1")
JUDGE_MODEL = os.environ.get("RAGAS_JUDGE_MODEL", "qwen3.6-35b-a3b")
EMBED_URL = os.environ.get("RAGAS_EMBED_URL", "http://10.10.19.50:18002/v1")
EMBED_MODEL = os.environ.get("RAGAS_EMBED_MODEL", "qwen3-embedding:8b")

# 짧은 키 → (ragas 결과표 열 이름, 보고용 이름)
METRICS = collections.OrderedDict([
    ("f", ("faithfulness", "Faithfulness")),
    ("ar", ("answer_relevancy", "Answer Relevancy")),
    ("cp", ("llm_context_precision_with_reference", "Context Precision")),
    ("cr", ("context_recall", "Context Recall")),
])
METRIC_COLS = list(METRICS.values())


#------------------------------------------------------------------
# 실험 결과 폴더 경로
#=> 실험마다 답변·점수를 따로 둬 서로 덮어쓰지 않게 한다(계획서 0-2).
#    1) ID 가 비어 있으면 results/ragas_golden 최상위 — §36 기준선이 이미 여기에 있다
#    2) ID 는 폴더 이름으로 쓰므로 영문·숫자·_·- 만 허용한다
#
# -in: exp = 실험 ID (예: "e11_dedup", 빈 문자열이면 기준선)
#
# -out: 폴더 절대 경로 (만들지는 않는다)
# -out: error = 허용하지 않는 문자가 있으면 SystemExit
#------------------------------------------------------------------
def exp_dir(exp=""):
    if not exp:
        return OUT_DIR
    if not all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in exp):
        raise SystemExit("--exp 는 영문·숫자·_·- 만: %r" % exp)
    return os.path.join(OUT_DIR, exp)


#------------------------------------------------------------------
# JSON 저장
#=> 중간에 죽어도 그때까지 결과가 남도록 임시 파일에 쓴 뒤 바꿔치기한다.
#
# -in: path = 저장 경로
# -in: obj  = 저장할 객체
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def save_json(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


#------------------------------------------------------------------
# 골든셋 읽기
#=> jsonl 한 줄 = 문항 1건.
#
# -in: limit = 앞에서 N건만 (0 이면 전부)
#
# -out: rows = 문항 dict 목록
# -out: error = 파일 없으면 FileNotFoundError 전파
#------------------------------------------------------------------
def load_golden(limit=0):
    rows = [json.loads(l) for l in io.open(GOLDEN, encoding="utf-8") if l.strip()]
    return rows[:limit] if limit else rows


#------------------------------------------------------------------
# 1단계 — 파이프라인으로 답변·근거 수집
#=> 운영과 같은 설정(config.yaml: 리랭커 풀 10 → 근거 3건, 기본 모델)으로 문항을 하나씩 돌린다.
#    1) App 을 띄우고 워밍업·리랭커 적재를 기다린다 — 첫 문항 시간이 튀지 않게
#    2) pipeline.answer 이벤트에서 근거 청크와 최종 답변을 받는다
#    3) retrieved_contexts 는 sLLM 에 실제로 들어간 청크 본문 그대로 쓴다(Faithfulness 판정 기준)
#    4) 문항마다 실험 폴더의 responses.json 을 갱신해 중간 결과를 남긴다
#    5) 워밍업 결과(백엔드·단계별 시간)를 warmup.json 으로 남긴다 — 모델 교체 실험(계획서 2-1)에서 기동 비용도 본다
#
# -in: args = argparse 결과 (limit, exp, model)
#
# -out: 0 = 성공
# -out: error = 모델·인덱스 적재 실패 시 예외 전파
#------------------------------------------------------------------
def stage_collect(args):
    from simplerag import config
    from simplerag.warmup import App

    golden = load_golden(args.limit)
    out_dir = exp_dir(args.exp)
    os.makedirs(out_dir, exist_ok=True)
    responses_path = os.path.join(out_dir, "responses.json")
    # 모델을 고르지 않으면 운영 기본 모델 — 기존 실험과 같은 조건
    model = args.model or config.DEFAULT_GEN_MODEL
    if model not in config.GEN_MODELS:
        raise SystemExit("--model 은 %s 중 하나: %r" % (" | ".join(config.GEN_MODELS), model))
    app = App(model)
    out = []
    try:
        t_warm = time.time()
        warm = app.warmup(wait=True)
        app.wait_rerank()
        warm_sec = round(time.time() - t_warm, 2)
        # 결과 dict 에 JSON 으로 못 쓰는 값이 섞일 수 있어 문자열로 바꿔 둔다
        save_json(os.path.join(out_dir, "warmup.json"), {
            "model": model, "wall_sec": warm_sec,
            "backend": getattr(getattr(app, "generator", None), "backend", None),
            "result": json.loads(json.dumps(warm, ensure_ascii=False, default=str)) if warm is not None else None})
        print("[collect] {}문항 / 모델 {} / 근거 {}건 / 리랭커 풀 {} / 워밍업 {}초".format(
            len(golden), model, config.TOP_K, config.RERANK_POOL, warm_sec), flush=True)
        for i, g in enumerate(golden, 1):
            chunks, done = [], None
            for ev in app.pipeline.answer(g["user_input"]):
                if ev[0] == "evidence":
                    chunks = ev[1]
                elif ev[0] == "done":
                    done = ev[1]
            names = [c.get("doc_name", "") for c in chunks]
            # 사본(두 파일) 중 어느 쪽이 검색돼도 정답 문서로 본다
            hit = any(n in g["source_files"] for n in names)
            out.append({
                "id": g["id"], "user_input": g["user_input"], "reference": g["reference"],
                "reference_contexts": g["reference_contexts"],
                "response": done["answer"],
                "retrieved_contexts": [c["text"] for c in chunks],
                "retrieved_docs": names, "doc_hit": hit, "cited": done.get("cited", []),
                "doc_id": g["doc_id"], "question_type": g["question_type"], "difficulty": g["difficulty"],
                "ttft_s": done.get("ttft_s"), "total_s": done.get("total_s"),
                "llm_ttft_s": done.get("llm_ttft_s"),
                "retrieval_ms": done.get("timing", {}).get("total_ms"),
                "model": model,
            })
            save_json(responses_path, out)
            print("  [{:>3}] {} TTFT {:.2f}s {}".format(
                i, "R" if hit else "-", done.get("ttft_s") or 0, g["user_input"][:40]), flush=True)
    finally:
        app.close()
    print("[collect] 저장:", responses_path)
    return 0


#------------------------------------------------------------------
# ragas import 전 vertexai 스텁 주입
#=> ragas 0.4.3 은 import 중 langchain_community.chat_models.vertexai 를 찾는데
#   langchain 1.x 계열 langchain-community 에는 그 모듈이 없다. Vertex 를 안 쓰므로 빈 모듈로 통과시킨다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 예외 없음
#------------------------------------------------------------------
def install_vertexai_shim():
    name = "langchain_community.chat_models.vertexai"
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    m = types.ModuleType(name)
    m.ChatVertexAI = type("ChatVertexAI", (), {})
    sys.modules[name] = m


#------------------------------------------------------------------
# NaN 판정
#=> ragas 는 채점 실패를 float NaN 으로, 우리 저장 형식은 None 으로 남긴다. 둘 다 '값 없음' 으로 본다.
#
# -in: v = 점수 값
#
# -out: bool = 값이 없으면 True
# -out: error = 예외 없음
#------------------------------------------------------------------
def is_missing(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


#------------------------------------------------------------------
# NaN 을 뺀 평균
#=> 판정 LLM 이 형식을 어겨 점수가 없는 문항은 평균에서 빼고, 몇 건 남았는지 따로 센다.
#
# -in: vals = 점수 목록 (None/NaN 섞일 수 있음)
#
# -out: (mean, n_valid) = 평균(유효값 없으면 None), 유효 개수
# -out: error = 예외 없음
#------------------------------------------------------------------
def mean_valid(vals):
    ok = [v for v in vals if not is_missing(v)]
    return (round(sum(ok) / len(ok), 4) if ok else None), len(ok)


#------------------------------------------------------------------
# 층별 요약
#=> 전체·유형별·난이도별·문서별로 지표 평균을 만든다. 채점하지 않은 지표는 None 으로 남는다.
#
# -in: items = 문항별 결과 (scores dict 포함)
# -in: key   = 묶을 필드명 (None 이면 전체)
#
# -out: {그룹: {n, 지표: 평균, 지표_valid: 유효수, mean4, doc_hit}}
# -out: error = 예외 없음
#------------------------------------------------------------------
def summarize(items, key=None):
    groups = collections.OrderedDict()
    for it in sorted(items, key=lambda x: str(x.get(key, "")) if key else ""):
        groups.setdefault(it[key] if key else "all", []).append(it)
    out = {}
    for g, rs in groups.items():
        row = {"n": len(rs)}
        for col, _ in METRIC_COLS:
            row[col], row[col + "_valid"] = mean_valid([r["scores"].get(col) for r in rs])
        vals = [row[c] for c, _ in METRIC_COLS if row[c] is not None]
        row["mean4"] = round(sum(vals) / len(vals), 4) if len(vals) == len(METRIC_COLS) else None
        row["doc_hit"] = round(sum(r["doc_hit"] for r in rs) / len(rs), 4)
        out[g] = row
    return out


#------------------------------------------------------------------
# 판정 LLM·임베딩·지표 객체 만들기
#=> score 단계에서 한 번만 만들어 첫 채점과 재시도에 같이 쓴다.
#    1) 판정 LLM = vLLM(OpenAI 호환), temperature 0, thinking 은 인자대로
#    2) 임베딩은 Answer Relevancy 를 잴 때만 만든다(나머지 3지표는 LLM 전용)
#
# -in: keys  = 채점할 지표 짧은 키 목록 (f/ar/cp/cr)
# -in: think = 판정 LLM thinking 켬 여부
# -in: timeout = 호출 1건 시간 제한(초)
#
# -out: (ragas 모듈 dict, llm, embeddings 또는 None, {키: 지표 객체})
# -out: error = ragas/langchain import 실패 시 ImportError 전파
#------------------------------------------------------------------
def build_judge(keys, think, timeout):
    install_vertexai_shim()
    import warnings
    warnings.filterwarnings("ignore")
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (Faithfulness, LLMContextPrecisionWithReference,
                               LLMContextRecall, ResponseRelevancy)
    from ragas.run_config import RunConfig

    llm = LangchainLLMWrapper(ChatOpenAI(
        model=JUDGE_MODEL, base_url=JUDGE_URL, api_key="EMPTY", temperature=0.0,
        timeout=timeout, max_retries=2,
        # thinking 을 끄면 빠르지만 Context Precision 이 과소평가된다(g003) — 지표별로 골라 쓴다
        extra_body={"chat_template_kwargs": {"enable_thinking": think}}))
    emb = None
    if "ar" in keys:
        emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
            model=EMBED_MODEL, base_url=EMBED_URL, api_key="EMPTY",
            # 토크나이저 기반 길이 분할(tiktoken)을 끈다 — 비OpenAI 모델에 엉뚱한 토큰 id 를 보내지 않게
            check_embedding_ctx_length=False))
    factory = {"f": Faithfulness, "ar": ResponseRelevancy,
               "cp": LLMContextPrecisionWithReference, "cr": LLMContextRecall}
    mods = {"EvaluationDataset": EvaluationDataset, "evaluate": evaluate, "RunConfig": RunConfig}
    return mods, llm, emb, {k: factory[k]() for k in keys}


#------------------------------------------------------------------
# 문항 묶음 1회 채점
#=> 주어진 문항·지표만 ragas evaluate 로 채점해 문항별 {열: 점수} 를 돌려준다.
#
# -in: rows    = 수집 결과 행 목록
# -in: keys    = 채점할 지표 짧은 키 목록
# -in: judge   = build_judge 결과
# -in: args    = argparse 결과 (workers, timeout)
#
# -out: [{열 이름: 점수 또는 None}, ...] (rows 와 같은 순서)
# -out: error = 개별 호출 실패는 None 으로 남는다(raise_exceptions=False)
#------------------------------------------------------------------
def score_once(rows, keys, judge, args):
    mods, llm, emb, metric_objs = judge
    ds = mods["EvaluationDataset"].from_list([{
        "user_input": r["user_input"], "response": r["response"],
        "retrieved_contexts": r["retrieved_contexts"], "reference": r["reference"],
    } for r in rows])
    res = mods["evaluate"](
        ds, metrics=[metric_objs[k] for k in keys], llm=llm, embeddings=emb,
        run_config=mods["RunConfig"](timeout=args.timeout, max_workers=args.workers, max_retries=3),
        raise_exceptions=False, show_progress=True)
    out = []
    for _, rec in res.to_pandas().iterrows():
        sc = {}
        for k in keys:
            col = METRICS[k][0]
            v = rec.get(col)
            sc[col] = None if is_missing(v) else round(float(v), 4)
        out.append(sc)
    return out


#------------------------------------------------------------------
# 2단계 — RAGAS 채점 (+ NaN 재채점)
#=> 수집 결과를 지표별로 채점하고, 판정 LLM 이 형식을 어겨 비어 버린 칸만 골라 다시 채점한다.
#    1) --resume 이면 같은 태그의 기존 결과를 읽어 비어 있는 칸만 채점 대상으로 삼는다
#    2) 첫 채점 후 NaN 칸이 남으면 그 문항·지표만 --retry 회까지 다시 채점한다
#       (재현해 보니 같은 문항도 단독으로는 정상 채점됐다 — 동시 부하 때 간헐적 파싱 실패)
#    3) 문항별 점수와 전체/유형/난이도/문서별 요약을 실험 폴더에 태그 파일로 저장한다
#
# -in: args = argparse 결과 (limit, workers, timeout, think, metrics, retry, resume, exp, responses)
#
# -out: 0 = 성공
# -out: error = 수집 파일 없으면 FileNotFoundError, 지표 키가 틀리면 SystemExit
#------------------------------------------------------------------
def stage_score(args):
    keys = [k.strip() for k in args.metrics.split(",") if k.strip()]
    bad = [k for k in keys if k not in METRICS]
    if bad or not keys:
        raise SystemExit("--metrics 는 f,ar,cp,cr 중에서: %s" % bad)
    keys = [k for k in METRICS if k in keys]           # 순서 고정

    out_dir = exp_dir(args.exp)
    os.makedirs(out_dir, exist_ok=True)
    # --responses 를 주면 다른 실험의 답변을 그대로 채점한다 — 같은 답변을 두 번 재 판정 잡음만 떼어 본다
    responses_path = os.path.abspath(args.responses) if args.responses else os.path.join(out_dir, "responses.json")
    rows = json.load(io.open(responses_path, encoding="utf-8"))
    if args.limit:
        rows = rows[:args.limit]

    # 판정 방식(thinking)·지표 조합마다 파일을 따로 둔다. 4지표 전부면 예전 이름(think/nothink)을 유지
    tag = ("think" if args.think else "nothink")
    if keys != list(METRICS):
        tag += "_" + "-".join(keys)
    if args.limit:
        tag += "_limit%d" % args.limit
    items_path = os.path.join(out_dir, "scores_items_%s.json" % tag)
    summary_path = os.path.join(out_dir, "summary_%s.json" % tag)

    scores = [{METRICS[k][0]: None for k in keys} for _ in rows]
    prev_sec = 0.0
    if args.resume and os.path.exists(items_path):
        old = {r["id"]: r["scores"] for r in json.load(io.open(items_path, encoding="utf-8"))}
        for sc, r in zip(scores, rows):
            for col in sc:
                sc[col] = old.get(r["id"], {}).get(col)
        if os.path.exists(summary_path):
            prev_sec = json.load(io.open(summary_path, encoding="utf-8")).get("score_sec", 0.0)

    judge = build_judge(keys, args.think, args.timeout)
    print("[score] {}문항 / 지표 {} / 판정 {} (thinking {}) / 임베딩 {} / workers {}".format(
        len(rows), ",".join(keys), JUDGE_MODEL, "on" if args.think else "off",
        EMBED_MODEL if "ar" in keys else "-", args.workers), flush=True)

    t0 = time.time()
    retries = 0
    for attempt in range(1 + args.retry):
        # 지표마다 비어 있는 문항만 모아 채점한다 — 재시도 때 이미 채운 칸은 다시 부르지 않는다
        todo = collections.OrderedDict((k, [i for i, sc in enumerate(scores) if sc[METRICS[k][0]] is None])
                                       for k in keys)
        todo = collections.OrderedDict((k, v) for k, v in todo.items() if v)
        if not todo:
            break
        print("[score] 시도 {} — 빈 칸 {}".format(
            attempt + 1, ", ".join("%s %d" % (k, len(v)) for k, v in todo.items())), flush=True)
        if attempt:
            retries += 1
        for k, idx in todo.items():
            got = score_once([rows[i] for i in idx], [k], judge, args)
            for i, sc in zip(idx, got):
                scores[i].update(sc)
    sec = prev_sec + time.time() - t0

    items = []
    for r, sc in zip(rows, scores):
        full = {col: None for col, _ in METRIC_COLS}
        full.update(sc)
        items.append(dict(r, scores=full))

    summary = {
        "judge": {"model": JUDGE_MODEL, "url": JUDGE_URL, "thinking": args.think, "temperature": 0.0},
        "embeddings": {"model": EMBED_MODEL, "url": EMBED_URL} if "ar" in keys else None,
        "exp": args.exp or "(기준선)", "responses": responses_path,
        "metrics": keys, "ragas_version": __import__("ragas").__version__,
        "n": len(items), "score_sec": round(sec, 1), "retry_passes": retries,
        "overall": summarize(items)["all"],
        "by_type": summarize(items, "question_type"),
        "by_difficulty": summarize(items, "difficulty"),
        "by_doc": summarize(items, "doc_id"),
    }
    save_json(items_path, items)
    save_json(summary_path, summary)

    o = summary["overall"]
    print("\n[score] 누적 {:.0f}초 / 재시도 {}회 → {}".format(sec, retries, summary_path))
    for k in keys:
        col, label = METRICS[k]
        print("  {:<18} {}  (유효 {}/{})".format(label, o[col], o[col + "_valid"], o["n"]))
    return 0


#------------------------------------------------------------------
# 명령행 진입점
#=> 인자를 읽어 collect 또는 score 단계를 실행한다.
#
# -in: 없음 (sys.argv)
#
# -out: 종료 코드 (0 성공)
# -out: error = 인자 오류는 argparse 가 SystemExit, 단계 내부 예외는 전파
#------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="골든셋 RAGAS 4지표")
    p.add_argument("stage", choices=["collect", "score"])
    p.add_argument("--limit", type=int, default=0, help="앞에서 N문항만(스모크 테스트)")
    p.add_argument("--workers", type=int, default=8, help="판정 동시 요청 수")
    p.add_argument("--timeout", type=int, default=600, help="판정 호출 1건 시간 제한(초) — thinking 켜면 길어진다")
    p.add_argument("--no-think", dest="think", action="store_false",
                   help="판정 LLM thinking 끔(빠르지만 Context Precision 이 과소평가됨)")
    p.add_argument("--metrics", default="f,ar,cp,cr", help="채점할 지표 (f,ar,cp,cr 중 쉼표 구분)")
    p.add_argument("--retry", type=int, default=2, help="NaN 칸 재채점 횟수")
    p.add_argument("--resume", action="store_true", help="같은 태그 기존 결과에서 빈 칸만 채점")
    p.add_argument("--exp", default="", help="실험 ID — results/ragas_golden/<ID>/ 에 저장(생략 시 기준선 폴더)")
    p.add_argument("--responses", default="", help="score 전용: 채점할 responses.json 경로(다른 실험 답변 재채점)")
    p.add_argument("--model", default="", help="collect 전용: 생성 모델 별칭(생략 시 config.DEFAULT_GEN_MODEL)")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    return stage_collect(args) if args.stage == "collect" else stage_score(args)


if __name__ == "__main__":
    raise SystemExit(main())
