"""골든셋 jsonl 기계 검증 — 발췌문이 원문(쪽)에 글자 그대로 있는지, 필수 필드·유형·must 가 맞는지 본다.

사용: python -I validate.py <파일.jsonl> [<파일2.jsonl> ...]
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = {d["doc_id"]: d for d in json.load(open(os.path.join(HERE, "docs.json"), encoding="utf-8"))}
TYPES = {"single_hop", "multi_hop", "reasoning"}
DIFF = {"easy", "medium", "hard"}
BANNED = ("이 문서", "본 문서", "해당 문서", "위 문서", "이 규정에서", "이 보고서")


#------------------------------------------------------------------
# 공백 정규화
#=> PDF 텍스트는 줄바꿈·공백이 들쭉날쭉하므로 모든 공백을 없애고 비교한다.
#
# -in: s = 문자열
#
# -out: 공백 없는 문자열
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    return re.sub(r"\s+", "", s)


#------------------------------------------------------------------
# 문서 쪽 텍스트 읽기
#=> pages/<doc_id>.txt 의 "=== p.N ===" 표지로 쪽을 나눠 {쪽번호: 정규화 텍스트} 로 돌려준다.
#
# -in: did = 문서 id (p01 …)
#
# -out: {int: str}
# -out: error = 파일 없으면 예외 전파
#------------------------------------------------------------------
def load_pages(did, _cache={}):
    if did not in _cache:
        raw = open(os.path.join(HERE, "pages", did + ".txt"), encoding="utf-8").read()
        parts = re.split(r"=== p\.(\d+) ===\n", raw)
        _cache[did] = {int(parts[i]): norm(parts[i + 1]) for i in range(1, len(parts), 2)}
    return _cache[did]


#------------------------------------------------------------------
# 한 문항 검사
#=> 문제를 문자열 목록으로 돌려준다(비면 통과).
#
# -in: r = 문항 dict
#
# -out: errs = 오류 문구 목록
# -out: error = 없음
#------------------------------------------------------------------
def check(r):
    errs = []
    for k in ("id", "user_input", "reference", "reference_contexts", "source_files", "doc_ids",
              "pages", "question_type", "difficulty", "must"):
        if k not in r:
            errs.append("필드 없음: " + k)
    if errs:
        return errs
    if r["question_type"] not in TYPES:
        errs.append("question_type 값")
    if r["difficulty"] not in DIFF:
        errs.append("difficulty 값")
    if any(b in r["user_input"] for b in BANNED):
        errs.append("질문에 지칭어(이 문서 등)")
    if not (1 <= len(r["reference_contexts"]) <= 4):
        errs.append("reference_contexts 1~4개")
    if len(r["pages"]) != len(r["reference_contexts"]):
        errs.append("pages 와 reference_contexts 개수가 다름 (발췌마다 [doc_id, 쪽] 하나)")
        return errs
    for did in r["doc_ids"]:
        if did not in DOCS:
            errs.append("모르는 doc_id " + did)
    for ctx, pg in zip(r["reference_contexts"], r["pages"]):
        did, p = pg
        if did not in DOCS:
            continue
        pages = load_pages(did)
        if len(norm(ctx)) < 15:
            errs.append("발췌가 너무 짧음: " + ctx[:30])
        if norm(ctx) not in pages.get(p, ""):
            where = [q for q, t in pages.items() if norm(ctx) in t]
            errs.append("발췌가 {} p.{} 에 없음{}: {}".format(
                did, p, " (실제 p.{})".format(where) if where else "", ctx[:40]))
    allctx = norm(" ".join(r["reference_contexts"]))
    for m in r["must"]:
        if norm(m) not in norm(r["reference"]):
            errs.append("must 가 reference 에 없음: " + m)
        if r["question_type"] != "reasoning" and norm(m) not in allctx:
            errs.append("must 가 발췌에 없음: " + m)
    if r["question_type"] == "multi_hop" and len(r["reference_contexts"]) < 2:
        errs.append("multi_hop 은 발췌 2개 이상")
    files = {f for did in r["doc_ids"] if did in DOCS for f in DOCS[did]["files"]}
    if set(r["source_files"]) != files:
        errs.append("source_files 는 doc_ids 의 파일 전부(사본 포함)여야 함: {}".format(sorted(files)))
    return errs


#------------------------------------------------------------------
# 진입점
#=> 파일들을 읽어 문항별 오류와 유형 분포, 질문 중복을 출력한다.
#
# -in: 없음 (argv = jsonl 경로들)
#
# -out: 없음 (종료 코드 0 = 모두 통과, 1 = 오류 있음)
# -out: error = JSON 형식 오류는 줄 번호와 함께 보고
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    bad, seen, n = 0, set(), 0
    dist = {}
    for path in sys.argv[1:]:
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                print("{}:{} JSON 오류 {}".format(path, i, e)); bad += 1; continue
            n += 1
            errs = check(r)
            q = norm(r.get("user_input", ""))
            if q in seen:
                errs.append("질문 중복")
            seen.add(q)
            dist[r.get("question_type")] = dist.get(r.get("question_type"), 0) + 1
            if errs:
                bad += 1
                print("[{}] {}".format(r.get("id"), " / ".join(errs)))
    print("문항 {} · 오류 {} · 유형 {}".format(n, bad, dist))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
