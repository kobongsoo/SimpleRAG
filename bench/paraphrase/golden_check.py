"""골든셋 100문항(문서 용어 질문)에서 동의어 넓히기가 검색을 해치지 않는지 — 켬·끔 근거 적중 비교.

사용: SIMPLERAG_HOME=<sample_pdf 인덱스 폴더> .venv\\Scripts\\python bench/paraphrase/golden_check.py
"""
import io
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from measure import ctx_hit  # noqa: E402


#------------------------------------------------------------------
# 진입점
#=> 같은 앱으로 문항마다 넓힘 끔·켬 두 번 검색해, 정답 근거 적중과 넓힘이 일어난 문항을 센다.
#
# -in: 없음
#
# -out: 없음 (표준출력)
# -out: error = 적재 실패 시 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from simplerag import config
    from simplerag.warmup import App

    rows = [json.loads(l) for l in io.open(os.path.join(ROOT, "dataset", "golden_pdf30.jsonl"), encoding="utf-8")]
    app = App(config.DEFAULT_GEN_MODEL)
    app.warmup(wait=True, skip_llm=True)
    app.wait_rerank()
    hit = {False: 0, True: 0}
    changed, expanded = [], []
    for r in rows:
        res = {}
        for on in (False, True):
            config.SYNONYMS = on
            chunks, timing = app.pipeline.retriever.search(r["user_input"], top_k=config.TOP_K)
            res[on] = ctx_hit(r["reference_contexts"], [c["text"] for c in chunks])
            hit[on] += res[on]
            if on and timing.get("synonyms"):
                expanded.append((r["id"], timing["synonyms"]))
        if res[False] != res[True]:
            changed.append((r["id"], res[False], res[True]))
    app.close()
    print("골든셋 {}문항 근거 적중 — 넓힘 끔 {} · 켬 {}".format(len(rows), hit[False], hit[True]))
    print("넓힘이 일어난 문항 {}개: {}".format(len(expanded), expanded))
    print("적중이 바뀐 문항: {}".format(changed))


if __name__ == "__main__":
    main()
