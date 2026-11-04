#------------------------------------------------------------------
# Claude 판정 RAGAS 4지표 — 배치 만들기 · 점수 계산
#=> ragas_golden.py collect 로 모은 responses.json 들을 Claude(하위 에이전트)가 판정하도록
#   배치 파일로 나누고, 판정 결과를 RAGAS 와 같은 공식으로 점수화한다.
#   판정 서버(qwen3.6-35b) 대신 Claude 가 판정하므로 기존 RAGAS 점수와는 섞어 비교하지 않는다.
#
#   지표 공식 (RAGAS 0.4 와 같은 정의)
#    F  = 답변 주장 중 근거로 뒷받침되는 비율 (주장 0개면 값 없음)
#    AR = 답변에서 역생성한 질문 3개와 원 질문의 임베딩 코사인 평균, 회피성 답이면 0
#    CP = 근거 순위별 유용 판정의 평균 정밀도(정답 기준)
#    CR = 정답 문장 중 근거로 귀속되는 비율
#
#   눈가림 — 같은 문항의 여러 모델 답을 한 묶음에 넣되 모델 이름 대신 무작위 글자(A~D)를 붙이고
#   순서도 섞는다. 글자 → 모델 대응표(key.json)는 판정 에이전트에게 주지 않는다.
#
#   사용:
#     python bench/ragas_claude.py prep  --runs pdf100_q3_17,... --per-batch 10
#     (에이전트가 judge/batch_XX.json 을 읽고 judge/out_XX.json 을 쓴다)
#     .venv\Scripts\python bench/ragas_claude.py score
#------------------------------------------------------------------

import argparse
import collections
import io
import json
import math
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

RUN_DIR = os.path.join(_ROOT, "results", "ragas_golden")
JUDGE_DIR = os.path.join(RUN_DIR, "claude_judge")


#------------------------------------------------------------------
# JSON 읽기·쓰기
#=> 판정 결과·배치 파일을 utf-8 로 다룬다.
#
# -in: path = 파일 경로 / obj = 저장할 객체
#
# -out: 읽은 객체 (load_json) / 없음 (save_json)
# -out: error = 파일 없음·형식 오류는 예외 전파
#------------------------------------------------------------------
def load_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


#------------------------------------------------------------------
# 실험 ID → (골든셋 이름, 모델 태그)
#=> "pdf100_q35_4b" 처럼 '<세트>_<모델태그>' 로 이름 붙인 실험 폴더를 나눈다.
#
# -in: run = 실험 ID
#
# -out: (set_name, model_tag)
# -out: error = 밑줄이 없으면 ValueError
#------------------------------------------------------------------
def split_run(run):
    s, tag = run.split("_", 1)
    return s, tag


#------------------------------------------------------------------
# 1단계 — 판정 배치 만들기
#=> 실험들의 답을 문항별로 묶어 배치 파일로 쓴다.
#    1) 같은 세트·같은 문항에서 근거(retrieved_contexts)가 같은 답들은 한 묶음 — CP·CR 은 근거와
#       정답만 보므로 묶음당 한 번만 판정하면 된다
#    2) 묶음 안 답에는 무작위 글자를 붙이고 순서를 섞는다(모델 눈가림)
#    3) 묶음을 per_batch 개씩 batch_XX.json 으로 나누고, 글자 대응은 key.json 에 둔다
#
# -in: args = argparse 결과 (runs, per_batch, seed)
#
# -out: 0 = 성공
# -out: error = responses.json 없으면 FileNotFoundError 전파
#------------------------------------------------------------------
def stage_prep(args):
    rng = random.Random(args.seed)
    runs = [r for r in args.runs.split(",") if r]
    groups = collections.OrderedDict()
    for run in runs:
        set_name, tag = split_run(run)
        for r in load_json(os.path.join(RUN_DIR, run, "responses.json")):
            # 근거 본문이 글자까지 같아야 같은 묶음 — 다르면 CP·CR 도 따로 판정한다
            key = (set_name, r["id"], json.dumps(r["retrieved_contexts"], ensure_ascii=False))
            g = groups.setdefault(key, {"question": r["user_input"], "reference": r["reference"],
                                        "contexts": r["retrieved_contexts"], "answers": []})
            g["answers"].append((tag, r["response"]))
    os.makedirs(JUDGE_DIR, exist_ok=True)
    items, keymap = [], {}
    for n, ((set_name, qid, _), g) in enumerate(groups.items()):
        gid = "%s:%s:%d" % (set_name, qid, n)
        answers = list(g["answers"])
        rng.shuffle(answers)
        out_answers = []
        for letter, (tag, text) in zip("ABCDEFGH", answers):
            keymap["%s|%s" % (gid, letter)] = {"set": set_name, "id": qid, "model": tag}
            out_answers.append({"aid": letter, "answer": text})
        items.append({"gid": gid, "question": g["question"], "reference": g["reference"],
                      "contexts": g["contexts"], "answers": out_answers})
    # 배치 순서도 섞어 세트·문서가 한 에이전트에 몰리지 않게 한다
    rng.shuffle(items)
    nb = 0
    for i in range(0, len(items), args.per_batch):
        save_json(os.path.join(JUDGE_DIR, "batch_%02d.json" % nb), items[i:i + args.per_batch])
        nb += 1
    save_json(os.path.join(JUDGE_DIR, "key.json"), keymap)
    print("[prep] 묶음 %d개 / 답 %d건 / 배치 %d개 → %s" % (
        len(items), len(keymap), nb, JUDGE_DIR))
    return 0


#------------------------------------------------------------------
# 평균 정밀도(Context Precision)
#=> RAGAS 와 같은 식: 유용 판정 v_k 마다 precision@k 를 곱해 더하고 유용 개수로 나눈다.
#   유용한 근거가 하나도 없으면 0.
#
# -in: verdicts = 근거 순위대로 0/1 목록
#
# -out: CP 값(0~1), 판정이 비면 None
# -out: error = 예외 없음
#------------------------------------------------------------------
def avg_precision(verdicts):
    if not verdicts:
        return None
    hit, total = 0, 0.0
    for k, v in enumerate(verdicts, 1):
        if v:
            hit += 1
            total += hit / k
    return total / hit if hit else 0.0


#------------------------------------------------------------------
# 비율 (분모 0 이면 값 없음)
#
# -in: flags = 0/1 목록
#
# -out: 1 의 비율, 목록이 비면 None
# -out: error = 예외 없음
#------------------------------------------------------------------
def ratio(flags):
    return (sum(1 for f in flags if f) / len(flags)) if flags else None


#------------------------------------------------------------------
# 코사인 유사도
#
# -in: a, b = 같은 길이의 벡터
#
# -out: 코사인 값
# -out: error = 예외 없음 (영벡터면 0)
#------------------------------------------------------------------
def cosine(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return (sum(x * y for x, y in zip(a, b)) / (na * nb)) if na and nb else 0.0


#------------------------------------------------------------------
# 평균 (None 제외) · 유효 개수
#
# -in: vals = 값 목록
#
# -out: (평균 또는 None, 유효 개수)
# -out: error = 예외 없음
#------------------------------------------------------------------
def mean_valid(vals):
    ok = [v for v in vals if v is not None]
    return (round(sum(ok) / len(ok), 4) if ok else None), len(ok)


#------------------------------------------------------------------
# 2단계 — 판정 결과를 점수로
#=> out_XX.json 을 모아 답 단위 4지표를 계산하고, 세트×모델 요약을 낸다.
#    1) key.json 으로 글자를 모델로 되돌린다
#    2) AR 은 역생성 질문과 원 질문을 SimpleRAG 임베더로 임베딩해 코사인 평균(회피성이면 0)
#    3) items.json(답 단위) · summary.json(세트×모델 평균) 저장
#
# -in: args = argparse 결과 (없음)
#
# -out: 0 = 성공
# -out: error = 판정이 빠진 답이 있으면 목록을 출력하고 1 반환
#------------------------------------------------------------------
def stage_score(args):
    from simplerag.embed.onnx_embedder import OnnxEmbedder

    keymap = load_json(os.path.join(JUDGE_DIR, "key.json"))
    batches = sorted(f for f in os.listdir(JUDGE_DIR) if f.startswith("batch_"))
    src = {}
    for b in batches:
        for it in load_json(os.path.join(JUDGE_DIR, b)):
            src[it["gid"]] = it
    judged = {}
    for b in batches:
        out = os.path.join(JUDGE_DIR, b.replace("batch_", "out_"))
        if os.path.isfile(out):
            for j in load_json(out):
                judged[j["gid"]] = j
    missing = [k for k in keymap if k.split("|")[0] not in judged]
    if missing:
        print("[score] 판정 없는 답 %d건 (예: %s)" % (len(missing), missing[:5]))
        return 1

    emb = OnnxEmbedder()
    emb.ensure_loaded()
    rows = []
    for key, meta in keymap.items():
        gid, aid = key.split("|")
        j, s = judged[gid], src[gid]
        a = next((x for x in j["answers"] if x["aid"] == aid), None)
        if a is None:
            print("[score] 답 판정 누락:", key)
            return 1
        f = ratio([c.get("supported") for c in a.get("claims", [])])
        if a.get("noncommittal"):
            # RAGAS: 회피성(모르겠다·찾을 수 없다) 답은 관련성 0
            ar = 0.0
        else:
            qs = [q for q in a.get("questions", []) if q.strip()]
            if qs:
                vecs = emb.embed([s["question"]] + qs)
                # 임베더가 numpy float32 를 돌려주므로 JSON 저장용 float 로 바꾼다
                ar = float(sum(cosine(vecs[0], v) for v in vecs[1:]) / len(qs))
            else:
                ar = None
        rows.append({
            "set": meta["set"], "id": meta["id"], "model": meta["model"],
            "faithfulness": f, "answer_relevancy": ar,
            "context_precision": avg_precision(j.get("cp", [])),
            "context_recall": ratio([c.get("attributed") for c in j.get("cr", [])]),
            "n_claims": len(a.get("claims", [])), "noncommittal": bool(a.get("noncommittal")),
        })
    save_json(os.path.join(JUDGE_DIR, "items.json"), rows)

    cols = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    summary = collections.OrderedDict()
    for r in sorted(rows, key=lambda x: (x["set"], x["model"])):
        summary.setdefault("%s|%s" % (r["set"], r["model"]), []).append(r)
    out = {}
    for k, rs in summary.items():
        row = {"n": len(rs), "noncommittal": sum(r["noncommittal"] for r in rs)}
        for c in cols:
            row[c], row[c + "_valid"] = mean_valid([r[c] for r in rs])
        vals = [row[c] for c in cols if row[c] is not None]
        row["mean4"] = round(sum(vals) / len(vals), 4) if len(vals) == 4 else None
        out[k] = row
        print("%-22s n=%3d F=%s AR=%s CP=%s CR=%s mean=%s" % (
            k, row["n"], row["faithfulness"], row["answer_relevancy"],
            row["context_precision"], row["context_recall"], row["mean4"]))
    save_json(os.path.join(JUDGE_DIR, "summary.json"), out)
    return 0


#------------------------------------------------------------------
# 진입점
#=> prep / score 단계를 고른다.
#
# -in: 없음 (명령행 인자)
#
# -out: 종료 코드
# -out: error = 인자 오류는 argparse 가 SystemExit
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Claude 판정 RAGAS 4지표")
    p.add_argument("stage", choices=["prep", "score"])
    p.add_argument("--runs", default="", help="prep: 실험 ID 쉼표 목록 (<세트>_<모델태그>)")
    p.add_argument("--per-batch", type=int, default=10, help="prep: 배치당 묶음 수")
    p.add_argument("--seed", type=int, default=20261029, help="prep: 눈가림 섞기 시드")
    p.add_argument("--judge-dir", default="", help="판정 폴더 이름(results/ragas_golden/ 아래, 생략 시 claude_judge)")
    args = p.parse_args()
    # 판정 회차마다 폴더를 나눠 앞선 배치·결과를 덮어쓰지 않게 한다
    if args.judge_dir:
        global JUDGE_DIR
        JUDGE_DIR = os.path.join(RUN_DIR, args.judge_dir)
    return stage_prep(args) if args.stage == "prep" else stage_score(args)


if __name__ == "__main__":
    sys.exit(main())
