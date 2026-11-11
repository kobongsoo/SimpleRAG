#------------------------------------------------------------------
# PDF 표 검출 켬/끔 검색 비교 (golden_pdf30)
#=> 같은 sample_pdf 30건을 표 검출 끔(현재)·켬으로 각각 빈 데이터 폴더에 인덱싱하고,
#   골든 100문항을 검색만 해서(LLM 없음) 근거를 얼마나 잘 가져오는지 비교한다.
#    1) index  — SIMPLERAG_HOME=<폴더>, SIMPLERAG_PDF_TABLES=0/1 로 cli index --rebuild (자식 프로세스)
#    2) search — 같은 환경으로 검색기를 올려 문항마다 top_k=3(sLLM 이 보는 수) · 10 을 가져온다
#    3) score  — 문서 적중 · 근거 발췌 포함(정규화 일치 / 8글자 조각 재현율) ·
#                낱말 재현율(순서 무관) · 핵심어(must) 적중을 낸다
#   표 관련 문항 = 근거 쪽에 표 블록이 있는 문항(표 검출을 켠 추출 결과 기준)으로 따로 집계한다.
#
#   사용(.venv):
#     .venv\Scripts\python bench\eval_pdf_tables.py run --tables 0 --home <빈 폴더A>
#     .venv\Scripts\python bench\eval_pdf_tables.py run --tables 1 --home <빈 폴더B>
#     .venv\Scripts\python bench\eval_pdf_tables.py compare results\pdf_tables_eval\off.json results\pdf_tables_eval\on.json
#------------------------------------------------------------------

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "dataset", "golden_pdf30.jsonl")
DOCS = os.path.join(ROOT, "dataset", "golden_pdf30_docs.json")
OUT_DIR = os.path.join(ROOT, "results", "pdf_tables_eval")
_KEEP = re.compile(r"[0-9A-Za-z가-힣]+")


#------------------------------------------------------------------
# 비교용 글자 정규화
#=> 표 구분자(|)·공백·줄바꿈이 켬/끔에서 다르게 들어가므로 한글·영문·숫자만 남긴다.
#
# -in: s = 원문
#
# -out: str = 소문자, 한글·영문·숫자만 이은 글
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(_KEEP.findall(s))


#------------------------------------------------------------------
# 발췌 하나가 글 안에 얼마나 들어 있나
#=> 통째로 들어 있으면 1.0. 아니면 발췌의 8글자 조각 중 글에 있는 비율.
#   (표를 행 단위로 펴면 pdfium 줄 순서로 만든 발췌와 글자 순서가 달라질 수 있어 조각으로도 본다)
#
# -in: ex  = 정규화한 발췌
# -in: hay = 정규화한 검색 결과 글
#
# -out: float 0~1
# -out: error = 없음
#------------------------------------------------------------------
def coverage(ex, hay):
    if not ex:
        return 0.0
    if ex in hay:
        return 1.0
    grams = [ex[i:i + 8] for i in range(0, max(1, len(ex) - 7))]
    return sum(g in hay for g in grams) / len(grams)


#------------------------------------------------------------------
# 낱말 재현율 (순서 무관)
#=> 골든 발췌는 pdfium 이 표를 읽은 순서(셀이 뒤섞인 줄) 그대로라, 표를 행 단위로 펴면
#   내용이 다 있어도 글자 순서가 달라 포함·조각 점수가 깎인다. 그래서 발췌를 공백으로
#   나눈 낱말(정규화 후 2글자 이상)이 검색 결과 글에 몇 개나 있는지도 본다.
#
# -in: ctx = 발췌 원문
# -in: hay = 정규화한 검색 결과 글
#
# -out: float 0~1 (낱말이 없으면 0)
# -out: error = 없음
#------------------------------------------------------------------
def token_recall(ctx, hay):
    toks = [t for t in (norm(w) for w in (ctx or "").split()) if len(t) >= 2]
    if not toks:
        return 0.0
    return sum(t in hay for t in toks) / len(toks)


#------------------------------------------------------------------
# 표가 있는 쪽 목록 (표 관련 문항 고르기용)
#=> 표 검출을 켠 추출기로 30건을 읽어 표 블록이 있는 (파일명, 쪽) 을 모은다.
#
# -in: 없음 (SIMPLERAG_PDF_TABLES=1 인 프로세스에서 부른다)
#
# -out: set[(파일명, 쪽)]
# -out: error = 추출 예외 전파
#------------------------------------------------------------------
def table_pages():
    import glob
    from simplerag.docmodel import extract as dx
    out = set()
    for f in glob.glob(os.path.join(ROOT, "sample_pdf", "**", "*.pdf"), recursive=True):
        d = dx.extract_document(f)
        for b in d.blocks:
            if b.type == "table":
                out.add((os.path.basename(f), b.page))
    return out


#------------------------------------------------------------------
# 인덱싱 + 검색 + 채점 한 번
#=> 1) 자식 프로세스로 cli index --rebuild (환경변수로 데이터 폴더·표 검출 지정)
#    2) 이 프로세스에서 검색기를 올려 문항마다 top 3 · top 10 을 모은다
#    3) 문항별 결과와 요약을 JSON 으로 저장한다
#
# -in: args.tables = 0 | 1
# -in: args.home   = 인덱스를 만들 빈 폴더
# -in: args.skip_index = True 면 이미 만든 인덱스로 검색만
# -in: args.whole = 작은 표 통째 기준 토큰(0 = 끔)
# -in: args.tag   = 결과 파일 이름(빈 값이면 on/off)
#
# -out: 0 = 성공
# -out: error = 인덱싱 실패 시 SystemExit
#------------------------------------------------------------------
def run(args):
    # 벡터 지연을 끈다 — 기본(500 청크 초과 시 미룸)이면 dense 없이 측정될 수 있다
    env = dict(os.environ, SIMPLERAG_HOME=os.path.abspath(args.home),
               SIMPLERAG_PDF_TABLES=str(args.tables), SIMPLERAG_CHUNK_TABLE_WHOLE=str(args.whole),
               SIMPLERAG_DEFER_VECTORS_OVER="0", PYTHONIOENCODING="utf-8")
    os.makedirs(args.home, exist_ok=True)
    t_index = None
    if not args.skip_index:
        t0 = time.time()
        r = subprocess.run([sys.executable, os.path.join(ROOT, "src", "simplerag", "cli.py"),
                            "index", "--dir", os.path.join(ROOT, "sample_pdf"), "--rebuild"],
                           env=env, cwd=ROOT)
        if r.returncode != 0:
            raise SystemExit("인덱싱 실패 rc={}".format(r.returncode))
        t_index = round(time.time() - t0, 1)
    # 검색기는 이 프로세스에서 — 설정 모듈이 import 때 환경변수를 읽으므로 먼저 넣는다
    os.environ.update(env)
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from simplerag.warmup import App

    golden = [json.loads(l) for l in open(GOLDEN, encoding="utf-8")]
    tpages = table_pages() if args.tables else None
    app = App()
    rows = []
    try:
        app.warmup(wait=True, skip_llm=True)
        app.wait_rerank()
        for g in golden:
            res = {}
            for k in (3, 10):
                chunks, _ = app.retriever.search(g["user_input"], top_k=k)
                hay = norm(" ".join(c["text"] for c in chunks))
                names = [c.get("doc_name", "") for c in chunks]
                covs = [coverage(norm(c), hay) for c in g["reference_contexts"]]
                toks = [token_recall(c, hay) for c in g["reference_contexts"]]
                must = [norm(m) for m in g.get("must") or [] if norm(m)]
                res[k] = {"doc_hit": any(n in g["source_files"] for n in names),
                          "ev_full": sum(1 for c in covs if c >= 1.0) / len(covs),
                          "ev_gram": sum(covs) / len(covs),
                          "ev_tok": sum(toks) / len(toks),
                          # 핵심어(must)가 근거 안에 있으면 sLLM 이 답할 재료가 있는 것
                          "must": sum(m in hay for m in must) / len(must) if must else 1.0,
                          "docs": names,
                          "texts": [c["text"][:400] for c in chunks] if k == 3 else None}
            rows.append({"id": g["id"], "type": g["question_type"], "res": res,
                         "pages": g["reference_pages"], "files": g["source_files"]})
    finally:
        app.close()
    out = {"tables": args.tables, "whole": args.whole, "index_sec": t_index, "rows": rows,
           "table_pages": sorted([list(p) for p in tpages]) if tpages else None}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "{}.json".format(args.tag or ("on" if args.tables else "off")))
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장:", path, "/ 인덱싱", t_index, "초")
    return 0


#------------------------------------------------------------------
# 표 관련 문항 번호 고르기
#=> 표 검출을 켠 결과에 든 "표가 있는 쪽" 목록과 문항의 근거 쪽을 맞대어,
#   근거 쪽 중 하나라도 표가 있으면 표 관련 문항으로 본다.
#
# -in: b = 표 켬 검색 결과 dict (table_pages, rows 포함)
#
# -out: set = 표 관련 문항 id
# -out: error = DOCS 파일 없으면 예외 전파
#------------------------------------------------------------------
def table_question_ids(b):
    docs = {d["doc_id"]: d["files"] for d in json.load(open(DOCS, encoding="utf-8"))}
    tp = {tuple(p) for p in b["table_pages"]}
    out = set()
    for r in b["rows"]:
        for p in r["pages"]:
            # 사본 파일 이름 중 어느 쪽이든 그 쪽에 표가 있으면 표 관련
            if any((f, p["page"]) in tp for f in docs.get(p["doc_id"], [])):
                out.add(r["id"])
    return out


#------------------------------------------------------------------
# sLLM 답변 수집 + 핵심어 채점
#=> 이미 만든 인덱스(--home)로 운영과 같은 답변 파이프라인을 돌려 문항마다 답을 받는다.
#    1) 환경변수로 데이터 폴더·표 설정을 넣고 App 을 기본 생성 모델로 띄운다
#    2) pipeline.answer 이벤트에서 근거(evidence)와 답(done)을 받는다
#    3) 답 안에 핵심어(must)가 몇 개 들었는지 정규화 글자로 센다 — 판정 서버 없이 규칙 채점
#    4) 문항마다 결과 파일을 갱신해 중간에 끊겨도 남긴다
#
# -in: args.tables = 0 | 1 (인덱스를 만든 설정과 같게)
# -in: args.home   = 인덱스가 있는 폴더
# -in: args.whole  = 작은 표 통째 기준 토큰(인덱스와 같게)
# -in: args.tag    = 결과 파일 이름 ans_<tag>.json
#
# -out: 0 = 성공
# -out: error = 모델·인덱스 적재 실패 시 예외 전파
#------------------------------------------------------------------
def answer(args):
    env = dict(os.environ, SIMPLERAG_HOME=os.path.abspath(args.home),
               SIMPLERAG_PDF_TABLES=str(args.tables), SIMPLERAG_CHUNK_TABLE_WHOLE=str(args.whole),
               SIMPLERAG_DEFER_VECTORS_OVER="0", PYTHONIOENCODING="utf-8",
               # 모델 폴더는 기본이 데이터 폴더 아래라 임시 폴더엔 없다 — 프로젝트 models/ 를 쓴다
               SIMPLERAG_MODELS_DIR=os.path.join(ROOT, "models"))
    # 설정 모듈이 import 때 환경변수를 읽으므로 import 전에 넣는다
    os.environ.update(env)
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from simplerag import config
    from simplerag.warmup import App

    golden = [json.loads(l) for l in open(GOLDEN, encoding="utf-8")]
    if args.ids:
        keep = set(args.ids.split(","))
        golden = [g for g in golden if g["id"] in keep]
    path = os.path.join(OUT_DIR, "ans_{}.json".format(args.tag))
    app = App(config.DEFAULT_GEN_MODEL)
    rows = []
    t_all = time.time()
    try:
        app.warmup(wait=True)
        app.wait_rerank()
        for i, g in enumerate(golden, 1):
            chunks, done = [], None
            for ev in app.pipeline.answer(g["user_input"]):
                if ev[0] == "evidence":
                    chunks = ev[1]
                elif ev[0] == "done":
                    done = ev[1]
            ans = (done or {}).get("answer", "")
            hay, ctx = norm(ans), norm(" ".join(c["text"] for c in chunks))
            must = [norm(m) for m in g.get("must") or [] if norm(m)]
            rows.append({
                "id": g["id"], "type": g["question_type"], "q": g["user_input"],
                "reference": g["reference"], "must_raw": g.get("must") or [],
                "answer": ans,
                # 답에 든 핵심어 비율 — 답 정답률의 규칙 근사
                "must_ans": sum(m in hay for m in must) / len(must) if must else 1.0,
                # 근거에 든 핵심어 비율 — 재료가 있었는데 못 썼는지 가르는 데 쓴다
                "must_ctx": sum(m in ctx for m in must) / len(must) if must else 1.0,
                "docs": [c.get("doc_name", "") for c in chunks],
                "total_s": (done or {}).get("total_s")})
            json.dump({"tables": args.tables, "whole": args.whole, "model": config.DEFAULT_GEN_MODEL,
                       "rows": rows}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            if i % 10 == 0 or i == len(golden):
                print("  [{}/{}] {:.0f}초 경과 · 답 핵심어 평균 {:.3f}".format(
                    i, len(golden), time.time() - t_all,
                    sum(r["must_ans"] for r in rows) / len(rows)), flush=True)
    finally:
        app.close()
    print("저장:", path)
    return 0


#------------------------------------------------------------------
# 두 답변 결과 비교 출력
#=> 전체 / 표 관련 / 표 무관으로 나눠 답 핵심어·근거 핵심어 평균을 보여 주고,
#   답 핵심어가 갈린 문항을 나열한다.
#
# -in: args.a      = 기준(끔) 답변 JSON
# -in: args.b      = 비교(켬) 답변 JSON
# -in: args.search = 표 켬 검색 결과 JSON (표 관련 문항 판정용, 기본 on.json)
#
# -out: 0
# -out: error = 파일 없으면 예외 전파
#------------------------------------------------------------------
def acompare(args):
    a = {r["id"]: r for r in json.load(open(args.a, encoding="utf-8"))["rows"]}
    b = {r["id"]: r for r in json.load(open(args.b, encoding="utf-8"))["rows"]}
    table_q = table_question_ids(json.load(open(args.search, encoding="utf-8")))
    ids = [i for i in a if i in b]
    for label, sel in (("전체", ids), ("표 관련", [i for i in ids if i in table_q]),
                       ("표 무관", [i for i in ids if i not in table_q])):
        n = max(len(sel), 1)
        line = []
        for key, name in (("must_ans", "답 핵심어"), ("must_ctx", "근거 핵심어")):
            x = sum(a[i][key] for i in sel) / n
            y = sum(b[i][key] for i in sel) / n
            line.append("{} {:.3f}→{:.3f}({:+.3f})".format(name, x, y, y - x))
        # 핵심어를 전부 맞힌 문항 수 — 평균보다 체감에 가깝다
        fa = sum(1 for i in sel if a[i]["must_ans"] >= 1.0)
        fb = sum(1 for i in sel if b[i]["must_ans"] >= 1.0)
        print("[{}] {}문항  {} · 전부 맞힘 {}→{}".format(label, len(sel), " · ".join(line), fa, fb))
    print("\n[답 핵심어가 갈린 문항]  끔→켬")
    for i in ids:
        if abs(a[i]["must_ans"] - b[i]["must_ans"]) >= 0.1:
            print("  {} {} 답 {:.2f}→{:.2f} · 근거 {:.2f}→{:.2f}  {}".format(
                i, "표" if i in table_q else "  ", a[i]["must_ans"], b[i]["must_ans"],
                a[i]["must_ctx"], b[i]["must_ctx"], a[i]["type"]))
    return 0


#------------------------------------------------------------------
# 켬/끔 결과 비교 출력
#=> 전체 100문항과 표 관련 문항으로 나눠 문서 적중 · 근거 포함(완전/조각) 평균을 보여 주고,
#   두 쪽에서 근거 포함이 갈린 문항을 나열한다.
#
# -in: args.off = 끔 결과 JSON
# -in: args.on  = 켬 결과 JSON (table_pages 포함)
#
# -out: 0
# -out: error = 파일 없으면 예외 전파
#------------------------------------------------------------------
def compare(args):
    a = json.load(open(args.off, encoding="utf-8"))
    b = json.load(open(args.on, encoding="utf-8"))
    # 표 관련 문항은 표 검출을 켠 쪽의 표 쪽 목록으로 고른다
    if not b.get("table_pages"):
        a, b = b, a
    table_q = table_question_ids(b)
    A = {r["id"]: r for r in a["rows"]}
    B = {r["id"]: r for r in b["rows"]}

    # 문항 묶음 하나의 평균
    def agg(rows, k, key):
        return sum(float(r["res"][str(k)][key]) for r in rows) / max(len(rows), 1)

    print("인덱싱 시간: 끔 {}초 · 켬 {}초".format(a.get("index_sec"), b.get("index_sec")))
    for label, ids in (("전체", list(A)), ("표 관련", sorted(table_q)), ("표 무관", [i for i in A if i not in table_q])):
        print("\n[{}] {}문항".format(label, len(ids)))
        for k in (3, 10):
            line = []
            for key, name in (("doc_hit", "문서"), ("ev_full", "근거완전"), ("ev_gram", "조각"),
                              ("ev_tok", "낱말"), ("must", "핵심어")):
                x, y = agg([A[i] for i in ids], k, key), agg([B[i] for i in ids], k, key)
                line.append("{} {:.3f}→{:.3f}({:+.3f})".format(name, x, y, y - x))
            print("  top{:<2} ".format(k) + " · ".join(line))
    print("\n[top3 낱말 재현율 또는 핵심어가 0.1 이상 갈린 문항]  낱말 끔→켬 · 핵심어 끔→켬")
    for i in A:
        a3, b3 = A[i]["res"]["3"], B[i]["res"]["3"]
        if abs(b3["ev_tok"] - a3["ev_tok"]) >= 0.1 or abs(b3["must"] - a3["must"]) >= 0.1:
            print("  {} {} 낱말 {:.2f}→{:.2f} · 핵심어 {:.2f}→{:.2f}  {}".format(
                i, "표" if i in table_q else "  ", a3["ev_tok"], b3["ev_tok"],
                a3["must"], b3["must"], A[i]["type"]))
    return 0


#------------------------------------------------------------------
# 명령줄 진입점
#
# -in: 없음 (명령줄: run | compare | answer | acompare)
#
# -out: 종료 코드
# -out: error = 하위 명령 예외 전파
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run")
    pr.add_argument("--tables", type=int, choices=(0, 1), required=True)
    pr.add_argument("--home", required=True)
    pr.add_argument("--skip-index", action="store_true")
    pr.add_argument("--whole", type=int, default=0, help="이 토큰 이하 표는 통째로 한 청크(0 = 끔)")
    pr.add_argument("--tag", default="", help="결과 파일 이름(기본 on/off)")
    pc = sub.add_parser("compare")
    pc.add_argument("off")
    pc.add_argument("on")
    pa = sub.add_parser("answer")
    pa.add_argument("--tables", type=int, choices=(0, 1), required=True)
    pa.add_argument("--home", required=True)
    pa.add_argument("--whole", type=int, default=0)
    pa.add_argument("--tag", required=True)
    pa.add_argument("--ids", default="", help="쉼표로 구분한 문항 id(빈 값이면 전체)")
    pq = sub.add_parser("acompare")
    pq.add_argument("a")
    pq.add_argument("b")
    pq.add_argument("--search", default=os.path.join(OUT_DIR, "on.json"))
    a = ap.parse_args()
    return {"run": run, "compare": compare, "answer": answer, "acompare": acompare}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
