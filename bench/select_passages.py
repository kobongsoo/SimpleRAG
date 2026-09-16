#------------------------------------------------------------------
# 문항 작성용 지문 선별 — 하이브리드 방식의 자동 단계 (2차 설계)
#=> 1차 설계(mine_facts.py)는 정규식으로 '사실'을 직접 뽑으려 했는데
#   367건 중 68건(18.5%)에서만 작동했다. 한국어 규정 문체 전용이라
#   영문 매뉴얼·슬라이드·양식에서는 아무것도 나오지 않았다.
#
#   그래서 역할을 바꾼다.
#     자동 — 어느 문서의 어느 대목을 읽을지 고른다(층화 + 사실밀도 + 유일성)
#     수동 — 그 대목을 읽고 문항과 정답을 사람이 쓴다
#
#   이 방식은 문서 종류를 가리지 않는다.
#
#   사용:
#     python bench/select_passages.py --total 200
#     python bench/select_passages.py --folder 해군 --total 16
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
OUT_JSON = os.path.join(ROOT, "results", "passages.json")
OUT_TXT = os.path.join(ROOT, "results", "passages_review.txt")

MIN_PER_FOLDER = 3

# 정답을 특정할 수 있는 신호 — 숫자, 날짜, 고유명사, 표 셀, 정의문
SIGNAL = [
    (re.compile(r"\d"), 1.0),
    (re.compile(r"\d{4}[.\-/]\d{1,2}"), 3.0),          # 날짜
    (re.compile(r"\d[\d,]*\s*(?:원|만원|억|일|년|개월|%)"), 4.0),
    (re.compile(r"[가-힣]{2,}(?:이|이라|라)\s*함은"), 5.0),   # 정의
    (re.compile(r"\|"), 0.5),                          # 표
    (re.compile(r"[:：]"), 0.5),
]

# 문항으로 쓸 수 없는 대목.
#   실제로 1차 선별 결과를 읽어 보고 걸러낸 것들이다. 목차 덤프가 특히 많았다
#   (.doc 를 추출하면 PAGEREF/HYPERLINK 필드코드가 본문처럼 딸려 나온다).
JUNK = re.compile(
    r"(?:copyright|all rights reserved|목\s*차|table of contents"
    r"|무단\s*전재|국립중앙도서관|^\s*[-=_.]{6,}\s*$"
    r"|PAGEREF|HYPERLINK|_Toc\d|\\h\s+\d"     # 워드 목차 필드코드
    r"|\.{5,}\s*\d+)", re.I | re.M)           # 점선 목차

# 개인정보가 본체인 대목 — 보도자료 말미의 담당자 연락처 명단 등.
#   "이 사람 전화번호는?" 류 문항은 만들지 않는다.
PII = re.compile(r"(?:\(0\d{1,2}\)\s*\d{3,4}-\d{4}|☎|\d{2,3}-\d{3,4}-\d{4})")


#------------------------------------------------------------------
# 지문 후보 점수
#=> 사실 신호가 많고 잡음이 적을수록 높다. 길이로 나눠 밀도로 본다.
#
# -in: seg = 지문 문자열
#
# -out: float 점수 (높을수록 문항화하기 좋다)
#------------------------------------------------------------------
def score(seg):
    if len(seg) < 120 or JUNK.search(seg):
        return 0.0
    # 연락처가 3개 이상이면 명단으로 보고 버린다(1~2개는 본문에 섞인 것).
    if len(PII.findall(seg)) >= 3:
        return 0.0
    s = 0.0
    for pat, w in SIGNAL:
        s += w * len(pat.findall(seg))
    # 한글이 거의 없는 영문 매뉴얼도 대상이지만, 기호만 잔뜩인 덤프는 거른다.
    letters = len(re.findall(r"[가-힣A-Za-z]", seg))
    if letters < len(seg) * 0.35:
        return 0.0

    # base64 덤프 방어. .ipynb 는 이미지가 base64 로 박혀 있는데, 글자 비율
    # 검사를 그대로 통과해 버린다(전부 영숫자라서). 공백 없이 길게 이어지는
    # 덩어리가 있으면 사람이 읽는 글이 아니다.
    if max((len(w) for w in seg.split()), default=0) > 60:
        return 0.0
    if seg.count(" ") < len(seg) / 25:          # 공백이 지나치게 드물다
        return 0.0

    return s / (len(seg) ** 0.5)


#------------------------------------------------------------------
# 문서에서 지문 뽑기
#=> 문단 단위로 훑어 점수 상위 구간을 고른다. 같은 문서 안에서 서로
#   멀리 떨어진 대목을 고르도록 이미 고른 위치 주변은 피한다.
#
# -in: text = 본문
# -in: k    = 뽑을 개수
# -in: width= 지문 길이(문자)
#
# -out: [(위치, 지문), ...]
#------------------------------------------------------------------
def pick(text, k=2, width=420):
    cands = []
    step = max(width // 2, 200)
    for i in range(0, max(1, len(text) - width), step):
        seg = text[i:i + width]
        sc = score(seg)
        if sc > 0:
            cands.append((sc, i, seg))
    cands.sort(reverse=True)

    out, used = [], []
    for sc, i, seg in cands:
        if any(abs(i - j) < width * 2 for j in used):
            continue
        used.append(i)
        out.append((i, " ".join(seg.split())))
        if len(out) >= k:
            break
    return out


#------------------------------------------------------------------
# 폴더별 문항 배정 — 비례 + 최소 보장
#
# -in: counts = {폴더: 문서수}
# -in: total  = 목표 문항 수
#
# -out: {폴더: 배정 문항 수}
#------------------------------------------------------------------
def allocate(counts, total):
    folders = sorted(counts, key=lambda k: -counts[k])
    rest = total - MIN_PER_FOLDER * len(folders)
    tot = sum(counts.values())
    if rest < 0:
        return {k: max(1, total // len(folders)) for k in folders}
    return {k: MIN_PER_FOLDER + max(0, round(rest * counts[k] / tot))
            for k in folders}


#------------------------------------------------------------------
# 사본 그룹 만들기 (핵심 — 코퍼스의 50%가 중복이다)
#=> 완전 동일한 본문을 가진 문서들을 한 그룹으로 묶는다. 문항을 만들 때
#   그룹당 하나만 쓰고, 채점할 때도 그룹 전체를 정답 출처로 인정해야 한다.
#   그렇지 않으면 검색이 사본 B를 가져왔다는 이유로 오답 처리된다.
#
# -in: man = manifest
#
# -out: (대표문서 집합, {경로: 그룹id})
#------------------------------------------------------------------
def dup_groups(man):
    import hashlib
    buckets = collections.defaultdict(list)
    for p, v in man.items():
        fp = os.path.join(CACHE, v["cache"])
        if not os.path.isfile(fp):
            continue
        flat = re.sub(r"\s+", "", open(fp, encoding="utf-8").read())
        buckets[hashlib.md5(flat.encode()).hexdigest()].append(p)

    rep, gid = set(), {}
    for i, (h, ps) in enumerate(sorted(buckets.items())):
        # 대표는 이름이 가장 긴 것 — 보통 날짜·번호가 붙은 정식 파일명이다.
        ps.sort(key=lambda x: (-len(os.path.basename(x)), x))
        rep.add(ps[0])
        for p in ps:
            gid[p] = i
    return rep, gid


def main():
    ap = argparse.ArgumentParser(description="문항 작성용 지문 선별")
    ap.add_argument("--total", type=int, default=200)
    ap.add_argument("--folder")
    ap.add_argument("--per-passage", type=float, default=1.4,
                    help="지문 1개당 기대 문항 수")
    args = ap.parse_args()

    man = json.load(open(MANIFEST, encoding="utf-8"))
    if args.folder:
        man = {p: v for p, v in man.items() if v["folder"] == args.folder}

    rep, gid = dup_groups(man)
    n_before = len(man)
    man = {p: v for p, v in man.items() if p in rep}
    print("사본 정리: %d건 → %d건 (중복 %d건 제외)" % (
        n_before, len(man), n_before - len(man)))

    counts = collections.Counter(v["folder"] for v in man.values())
    alloc = allocate(counts, args.total)

    by_folder = collections.defaultdict(list)
    for p, v in man.items():
        by_folder[v["folder"]].append((p, v))

    selected = []
    for folder, docs in by_folder.items():
        want_q = alloc[folder]
        want_p = max(1, round(want_q / args.per_passage))
        # 문서를 고루 쓰기 위해 글자 수 순으로 늘어놓고 라운드로빈으로 돈다.
        docs.sort(key=lambda d: -d[1]["chars"])
        picked, di, guard = [], 0, 0
        per_doc = collections.Counter()
        while len(picked) < want_p and guard < want_p * 12:
            guard += 1
            p, v = docs[di % len(docs)]
            di += 1
            if per_doc[p] >= 2:
                continue
            fp = os.path.join(CACHE, v["cache"])
            if not os.path.isfile(fp):
                continue
            text = open(fp, encoding="utf-8").read()
            got = pick(text, k=per_doc[p] + 1)
            if len(got) <= per_doc[p]:
                continue
            pos, seg = got[per_doc[p]]
            per_doc[p] += 1
            picked.append({
                "folder": folder, "doc": os.path.basename(p), "path": p,
                "ext": v["ext"], "pos": pos, "passage": seg,
                "quota": want_q,
                # 채점 시 정답 출처로 함께 인정해야 할 사본들
                "dup_group": sorted(os.path.basename(q) for q, g in gid.items()
                                    if g == gid.get(p)),
            })
        selected += picked

    json.dump(selected, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    with open(OUT_TXT, "w", encoding="utf-8") as f:
        cur = None
        for i, s in enumerate(selected, 1):
            if s["folder"] != cur:
                cur = s["folder"]
                f.write("\n{}\n[{}]  배정 {}문항\n{}\n".format(
                    "=" * 74, cur, s["quota"], "=" * 74))
            f.write("\n#{:03d} {} ({})\n{}\n".format(
                i, s["doc"][:60], s["ext"], s["passage"]))

    print("지문 %d개 / 폴더 %d개 / 문서 %d건" % (
        len(selected), len(by_folder), len({s["path"] for s in selected})))
    print("배정 합계 %d문항" % sum(alloc.values()))
    print("\n검수용: " + OUT_TXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
