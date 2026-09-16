#------------------------------------------------------------------
# 평가 문항 v3 자동 확장 — 리랭킹 채택 판정을 위한 표본 확대
#=> 204문항(McNemar p=0.150)으로는 리랭킹 +9문항 효과를 확정할 수 없었다.
#   판정력을 올리려면 표본을 키워야 한다.
#
#   ⚠️ v1/v2 는 사람이 지문을 읽고 문항을 썼다(하이브리드: 자동 선별 →
#   수동 작성). 여기서는 시간상 **완전 자동 생성**을 시도한다 — 대신
#   품질 하락을 그대로 감수하지 않고 세 겹으로 막는다.
#     ① 표 파싱을 엄격하게 — 실제로 훑어 보니 표의 상당수가 Word 변환
#        잔재(TOC/HYPERLINK/PAGEREF)라 머리글·셀 모양을 깐깐하게 걸렀다.
#     ② bench/validate_cases.py 기계 검증을 그대로 통과해야 한다.
#     ③ 무작위 표본을 사람이(에이전트가) 눈으로 훑어 명백한 오류를 뺀다.
#   그래도 v1/v2 보다 신뢰도가 낮으므로 tag="v3" 로 구분해 둔다 — 나중에
#   이상 결과가 나오면 v3 만 의심할 수 있게.
#
# -출력: bench/eval_cases_v3.py (CASES = v2 전체 + 신규)
#------------------------------------------------------------------

import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, _HERE)
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import structure                          # noqa: E402

CACHE = os.path.join(ROOT, "results", "textcache")
MANIFEST = os.path.join(CACHE, "manifest.json")

# ── 값에서 단위를 보고 자연스러운 질문 형식을 고른다 ──────────
_TEMPLATES = [
    (re.compile(r"(원|만원|억원|천원)$"), "{label} {header}는 얼마인가요?"),
    (re.compile(r"(일|박)$"), "{label} {header}는 며칠인가요?"),
    (re.compile(r"(%|퍼센트)$"), "{label} {header}는 몇 퍼센트인가요?"),
    (re.compile(r"(명|인)$"), "{label} {header}는 몇 명인가요?"),
    (re.compile(r"(회|건|개)$"), "{label} {header}는 몇 {unit}인가요?"),
    (re.compile(r"(시간|분)$"), "{label} {header}는 얼마나 되나요?"),
]

# 잡음으로 판단해 버리는 라벨/헤더
_STOP_LABEL = {"합계", "계", "비고", "순번", "번호", "no", "소계", "총계",
               "-", "구분", "구 분", "내역", "내 역"}
_GARBAGE = re.compile(r"HYPERLINK|PAGEREF|TOC\s|SHAPE|MERGEFORMAT|_Toc\d|\.{4,}")
_NUM_VAL = re.compile(r"^[0-9][0-9,.]*\s*(?:원|만원|억원|천원|일|박|%|퍼센트|명|인|회|건|개|시간|분|km|kg|GB|MB)?$")


#------------------------------------------------------------------
# 셀 목록 뽑기
#=> "| a | b | c |" -> ["a","b","c"] (양끝 빈 칸 제거)
#
# -in: line = 표 한 줄
#
# -out: 셀 문자열 리스트
#------------------------------------------------------------------
def cells_of(line):
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


#------------------------------------------------------------------
# 값에 맞는 질문 문형 고르기
#
# -in: label, header, value
#
# -out: 질문 문자열 또는 None(단위를 못 알아보면 포기 — 억지 문장 방지)
#------------------------------------------------------------------
def make_question(label, header, value):
    for pat, tmpl in _TEMPLATES:
        m = pat.search(value)
        if m:
            return tmpl.format(label=label, header=header, unit=m.group(1))
    if re.fullmatch(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 ]{0,14}", value):
        return "{} {}는 무엇인가요?".format(label, header)
    return None


#------------------------------------------------------------------
# 헤더 한 줄이 쓸 만한지 판정
#=> Word 변환 잔재(TOC/HYPERLINK)와 지나치게 긴 셀을 걸러낸다.
#
# -in: cells = 헤더 셀 리스트
#
# -out: bool
#------------------------------------------------------------------
def good_header(cells):
    if not (2 <= len(cells) <= 8):
        return False
    body = cells[1:]
    if not body or any(not c or len(c) > 12 for c in body):
        return False
    if any(_GARBAGE.search(c) for c in cells):
        return False
    return True


#------------------------------------------------------------------
# 문서 하나에서 표 기반 문항 후보 뽑기
#
# -in: text, doc_stem, folder
#
# -out: [dict, ...]
#------------------------------------------------------------------
def mine_table_questions(text, doc_stem, folder):
    lines = text.split("\n")
    heads = structure.table_headers(lines)
    out, seen_headers = [], {}

    for i, hline in heads.items():
        hcells = cells_of(hline)
        if hline not in seen_headers:
            seen_headers[hline] = good_header(hcells)
        if not seen_headers[hline]:
            continue

        rcells = cells_of(lines[i])
        if len(rcells) < 2:
            continue
        label = rcells[0]
        if (not label or len(label) > 14 or label.lower() in _STOP_LABEL
                or not re.search(r"[가-힣]", label)):
            continue

        for j in range(1, min(len(hcells), len(rcells))):
            header, value = hcells[j], rcells[j]
            if not header or not value or len(value) > 20:
                continue
            if header.lower() in _STOP_LABEL or _GARBAGE.search(value):
                continue
            if not (_NUM_VAL.match(value) or re.fullmatch(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 ]{0,14}", value)):
                continue
            q = make_question(label, header, value)
            if not q:
                continue
            out.append({"cat": folder, "q": q, "src": [doc_stem],
                       "must": [value], "must_not": []})
    return out


def main():
    man = json.load(io.open(MANIFEST, encoding="utf-8"))
    candidates = []
    for p, v in man.items():
        fp = os.path.join(CACHE, v["cache"])
        if not os.path.isfile(fp):
            continue
        text = io.open(fp, encoding="utf-8").read()
        stem = os.path.splitext(os.path.basename(p))[0]
        candidates += mine_table_questions(text, stem, v["folder"])

    print("표 기반 원시 후보: %d건" % len(candidates))

    # 질문 텍스트 기준 중복 제거(사본 문서가 같은 질문을 또 만든다)
    seen, dedup = set(), []
    for c in candidates:
        if c["q"] in seen:
            continue
        seen.add(c["q"])
        dedup.append(c)
    print("질문 중복 제거 후: %d건" % len(dedup))

    json.dump(dedup, io.open(os.path.join(_HERE, "v3_candidates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("저장: bench/v3_candidates.json")


if __name__ == "__main__":
    main()
