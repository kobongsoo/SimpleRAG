#------------------------------------------------------------------
# 평가 문항 후보 채굴 — 하이브리드 방식의 자동 단계
#=> 텍스트 캐시에서 '검증 가능한 사실'을 뽑아 문항 후보를 만든다.
#   최종 문항은 사람이 다듬는다. 여기서 하는 일은 세 가지다.
#
#     ① 사실 채굴  — 숫자/정의/표 셀처럼 정답을 한 문자열로 특정할 수 있는 것
#     ② 유일성 검사 — 같은 사실이 여러 문서에 있으면 정답 출처가 흔들린다
#     ③ 폴더별 정리 — 비례 배분에 맞춰 골라 쓸 수 있게 묶어 둔다
#
#   ⚠️ must 용어는 **원문 문자열을 그대로** 쓴다. 손으로 옮겨 적으면 채점이
#      틀어진다 — 실제로 채점 규칙 오류로 네 번 헛발질했다(설계서 R5).
#
#   사용:
#     python bench/mine_facts.py                    # 전체
#     python bench/mine_facts.py --folder 2.회사규정   # 한 폴더만
#------------------------------------------------------------------

import argparse
import collections
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
CACHE = os.path.join(ROOT, "results", "textcache")
MANIFEST = os.path.join(CACHE, "manifest.json")
OUT = os.path.join(ROOT, "results", "fact_candidates.json")

# 숫자 + 단위. 금액/일수/기간/비율처럼 '얼마/며칠/몇 %' 로 물을 수 있는 것.
UNIT = r"(?:원|만원|억원|천만원|일|개월|년|시간|분|%|퍼센트|회|명|건|배|주|층|GB|MB|TB)"
NUM = r"[0-9][0-9,\.]*"

PATTERNS = [
    # 제5조(지급 금액) 리프레시 휴가비는 100만원으로 한다.
    ("서술", re.compile(
        r"([가-힣A-Za-z][^\n.]{2,40}?)\s*(?:은|는|이|가)\s+"
        r"(" + NUM + r"\s*" + UNIT + r")\s*(?:으로|로)?\s*(?:한다|합니다|이다|입니다|정한다)")),
    # 지원 한도는 10만원 / 한도 : 3,000만원
    ("한도", re.compile(
        r"([가-힣][^\n:|]{2,30}?)\s*(?:한도|금액|비용|요금|기간|일수)\s*[:：]?\s*"
        r"(" + NUM + r"\s*" + UNIT + r")")),
    # "임직원"이라 함은 … 을 말한다
    ("정의", re.compile(
        r"[‘'\"“]?([가-힣A-Za-z][^\n’'\"”]{1,24})[’'\"”]?\s*(?:이|이라|라)\s*함은\s*"
        r"([^\n]{4,80}?)\s*(?:을|를)\s*말한다")),
]

# 표 행 — 첫 칸이 항목명, 나머지가 값
TABLE_ROW = re.compile(r"^\s*\|\s*([^|\n]{2,30}?)\s*\|(.+)\|\s*$", re.M)

# 문항으로 쓰기 곤란한 잡음
NOISE = re.compile(r"(제\s*\d+\s*조|별표|서식|페이지|^\s*\d+\s*$|＿|___)")


#------------------------------------------------------------------
# 캐시 로드
#=> {경로: (메타, 본문)} 형태로 읽어 둔다.
#------------------------------------------------------------------
def load_cache(folder=None):
    man = json.load(open(MANIFEST, encoding="utf-8"))
    out = {}
    for p, v in man.items():
        if folder and v["folder"] != folder:
            continue
        fp = os.path.join(CACHE, v["cache"])
        if not os.path.isfile(fp):
            continue
        out[p] = (v, open(fp, encoding="utf-8").read())
    return out


#------------------------------------------------------------------
# 문서 1건에서 사실 후보 뽑기
#=> 정답이 될 문자열(answer)과 그것을 특정하는 주제어(subject), 그리고
#   원문 근거 문장(context)을 함께 남긴다. 사람이 검수할 때 context 를 본다.
#
# -in: text = 문서 본문
#
# -out: [dict, ...]
#------------------------------------------------------------------
def mine_doc(text):
    facts = []
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            subj = " ".join(m.group(1).split())
            ans = " ".join(m.group(2).split())
            if len(subj) < 2 or NOISE.search(subj):
                continue
            s = max(0, m.start() - 60)
            facts.append({
                "kind": kind, "subject": subj, "answer": ans,
                "context": " ".join(text[s:m.end() + 60].split()),
            })

    for m in TABLE_ROW.finditer(text):
        label = " ".join(m.group(1).split())
        cells = [" ".join(c.split()) for c in m.group(2).split("|")]
        cells = [c for c in cells if c]
        if len(label) < 2 or not cells or NOISE.search(label):
            continue
        # 숫자 셀만 정답 후보로 쓴다 — 문자열 셀은 채점이 애매하다.
        for c in cells:
            if re.fullmatch(NUM + r"(?:\s*" + UNIT + r")?", c):
                s = max(0, m.start() - 120)
                facts.append({
                    "kind": "표", "subject": label, "answer": c,
                    "context": " ".join(text[s:m.end()].split()),
                })
    return facts


def main():
    ap = argparse.ArgumentParser(description="평가 문항 후보 채굴")
    ap.add_argument("--folder")
    ap.add_argument("--max-per-doc", type=int, default=25)
    args = ap.parse_args()

    cache = load_cache(args.folder)
    print("문서 %d건에서 채굴" % len(cache))

    # ① 채굴
    per_doc = {}
    for p, (meta, text) in cache.items():
        f = mine_doc(text)
        if f:
            per_doc[p] = (meta, f[:args.max_per_doc])

    # ② 유일성 — 주제어가 여러 문서에 등장하면 정답 출처가 흔들린다.
    subj_docs = collections.defaultdict(set)
    for p, (meta, facts) in per_doc.items():
        for f in facts:
            subj_docs[f["subject"]].add(p)

    rows = []
    for p, (meta, facts) in per_doc.items():
        for f in facts:
            n = len(subj_docs[f["subject"]])
            f = dict(f)
            f["doc"] = os.path.basename(p)
            f["path"] = p
            f["folder"] = meta["folder"]
            f["subject_docs"] = n          # 1이면 유일
            rows.append(f)

    uniq = [r for r in rows if r["subject_docs"] == 1]
    by_folder = collections.Counter(r["folder"] for r in uniq)

    json.dump(rows, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print("후보 %d개 (유일 주제어 %d개)" % (len(rows), len(uniq)))
    print("\n%-28s %8s %8s" % ("폴더", "유일후보", "문서"))
    docs_by_folder = collections.Counter(
        r["folder"] for r in {r["path"]: r for r in uniq}.values())
    for k, v in by_folder.most_common():
        print("  %-26s %6d %7d" % (k[:26], v, docs_by_folder[k]))
    print("\n저장: " + OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
