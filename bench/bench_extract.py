#------------------------------------------------------------------
# 실문서 텍스트 추출 검증 (Q1 결정 — CSOClassify extract 모듈 재사용)
#=> 설계서 §13 Q1 에서 "CSOClassify 의 extract 모듈을 Windows 전용으로 재사용"
#   하기로 했다. 실제로 대상 문서(D:\분류함\2.회사규정)에서 동작하는지,
#   특히 레거시 .doc(OLE) 이 제대로 나오는지를 먼저 확인한다.
#
#   확인 항목
#    1) 포맷별 추출 성공/실패
#    2) 추출 문자 수 (빈 텍스트 = 사실상 실패)
#    3) 소요 시간 (인덱싱 예산에 추출 시간을 더해야 함)
#    4) 하이브리드 파서 vs 사이냅 단독 비교
#
#   사용:  python bench/bench_extract.py --dir "D:\분류함\2.회사규정"
#------------------------------------------------------------------

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))

# CSOClassify 의 src 를 import 경로에 얹어 그대로 재사용한다(복사 아님).
CSO_SRC = r"D:\Project\CSOClassify\src"


#------------------------------------------------------------------
# 추출기 준비
#=> CSOClassify 패키지를 경로에 추가하고 팩토리로 추출기를 만든다.
#   hybrid=True 면 포맷별 전용 파서 + 사이냅 폴백, False 면 사이냅 단독.
#
# -in: hybrid = 하이브리드 라우팅 사용 여부
#
# -out: extractor 인스턴스
#------------------------------------------------------------------
def make_extractor(hybrid):
    if CSO_SRC not in sys.path:
        sys.path.insert(0, CSO_SRC)
    from csoclassify.extract import build_extractor
    return build_extractor(hybrid=hybrid)


#------------------------------------------------------------------
# 폴더 내 문서 전부 추출 (핵심)
#=> 파일마다 소요시간·문자수·실패사유를 남긴다. 빈 문자열이 나오면
#   예외가 안 났어도 '실패'로 본다 — 인덱싱에 들어가면 조용히 누락되기 때문.
#
# -in: ext = 추출기, files = 대상 경로 목록
#
# -out: rows = 파일별 결과
#------------------------------------------------------------------
def run(ext, files):
    rows = []
    for path in files:
        name = os.path.basename(path)
        t0 = time.perf_counter()
        try:
            text = ext.extract(path)
            err = None
        except Exception as e:
            text, err = "", "{}: {}".format(type(e).__name__, str(e)[:160])
        dt = time.perf_counter() - t0

        chars = len(text or "")
        rows.append({
            "file": name,
            "ext": os.path.splitext(name)[1].lower().lstrip("."),
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "chars": chars,
            "sec": round(dt, 2),
            "ok": bool(chars > 0 and err is None),
            "error": err,
            "head": (text or "")[:120].replace("\n", " ").strip(),
        })
        mark = "OK " if rows[-1]["ok"] else "FAIL"
        print("  [{}] {:<52} {:>7,}자 {:>6.2f}s {}".format(
            mark, name[:50], chars, dt, err or ""))
    return rows


def main():
    p = argparse.ArgumentParser(description="실문서 추출 검증")
    p.add_argument("--dir", default=r"D:\분류함\2.회사규정")
    p.add_argument("--modes", nargs="+", default=["hybrid", "synap"])
    args = p.parse_args()

    if not os.path.isdir(args.dir):
        print("[error] 폴더 없음: " + args.dir, file=sys.stderr)
        return 2

    files = sorted(os.path.join(args.dir, f) for f in os.listdir(args.dir)
                   if os.path.isfile(os.path.join(args.dir, f)))
    print("[extract] 대상 {}건 — {}\n".format(len(files), args.dir))

    out = {}
    for mode in args.modes:
        print("=== mode={} ===".format(mode))
        try:
            ext = make_extractor(hybrid=(mode == "hybrid"))
        except Exception as e:
            print("  추출기 생성 실패: {}: {}\n".format(type(e).__name__, e))
            continue
        rows = run(ext, files)
        ok = sum(r["ok"] for r in rows)
        tot = sum(r["sec"] for r in rows)
        chars = sum(r["chars"] for r in rows)
        print("  → 성공 {}/{}  총 {:,}자  {:.1f}s\n".format(ok, len(rows), chars, tot))
        out[mode] = rows

    path = os.path.join(RESULTS_DIR, "extract.json")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("[extract] 저장: " + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
