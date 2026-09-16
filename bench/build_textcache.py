#------------------------------------------------------------------
# 코퍼스 텍스트 캐시 구축 — 평가셋 확장의 준비 단계
#=> 인덱싱된 문서 전부를 한 번만 추출해 텍스트로 떠 둔다.
#   문항을 채굴하고 다듬는 과정에서 같은 문서를 여러 번 읽어야 하는데,
#   추출은 문서당 수 초가 걸려 매번 다시 하면 작업이 불가능하다.
#
#   인덱싱과 같은 추출기를 쓴다 — 평가 문항의 정답 근거가 **실제로 색인된
#   텍스트와 일치**해야 하기 때문이다. 다른 경로로 뽑으면 원문에는 있는데
#   색인에는 없는 사실로 문항을 만드는 사고가 난다.
#
#   사용:
#     python bench/build_textcache.py
#     python bench/build_textcache.py --limit 20     # 시험 삼아 20건만
#------------------------------------------------------------------

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

import simplerag.extract as EX                       # noqa: E402

CACHE = os.path.join(ROOT, "results", "textcache")
MANIFEST = os.path.join(CACHE, "manifest.json")
STATE = os.path.join(ROOT, "index_state.json")
BASE = r"D:\분류함"


#------------------------------------------------------------------
# 캐시 파일명 만들기
#=> 경로가 중복되지 않게 폴더 구조를 파일명에 녹인다. 원본 경로는 manifest 에
#   따로 남기므로 여기서는 사람이 알아볼 수 있으면 충분하다.
#
# -in: path = 원본 문서 절대경로
#
# -out: 캐시 파일명(확장자 .txt)
#------------------------------------------------------------------
def cache_name(path):
    rel = os.path.relpath(path, BASE) if path.lower().startswith(BASE.lower()) else path
    flat = rel.replace(os.sep, "__").replace(":", "")
    return flat + ".txt"


def main():
    ap = argparse.ArgumentParser(description="코퍼스 텍스트 캐시")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 추출")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    st = json.load(open(STATE, encoding="utf-8"))
    docs = st.get("docs", st)
    paths = sorted(docs.keys())
    if args.limit:
        paths = paths[:args.limit]

    ext_obj = EX.build_extractor()
    manifest = {}
    if os.path.isfile(MANIFEST) and not args.force:
        manifest = json.load(open(MANIFEST, encoding="utf-8"))

    t0 = time.perf_counter()
    ok = skip = fail = 0
    failures = []
    for i, p in enumerate(paths, 1):
        name = cache_name(p)
        out = os.path.join(CACHE, name)
        if not args.force and os.path.isfile(out) and p in manifest:
            skip += 1
            continue
        try:
            text, n_rows = EX.extract_text(ext_obj, p)
        except Exception as e:
            fail += 1
            failures.append((os.path.basename(p), "{}: {}".format(
                type(e).__name__, str(e)[:70])))
            continue
        if not text or not text.strip():
            fail += 1
            failures.append((os.path.basename(p), "텍스트 없음"))
            continue

        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        rel = os.path.relpath(p, BASE) if p.lower().startswith(BASE.lower()) else p
        manifest[p] = {
            "cache": name,
            "folder": rel.split(os.sep)[0] if os.sep in rel else "(루트)",
            "ext": os.path.splitext(p)[1].lower(),
            "chars": len(text),
            "table_rows": n_rows,
        }
        ok += 1
        if i % 25 == 0:
            print("  {}/{} … {:.0f}s".format(i, len(paths), time.perf_counter() - t0))

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print("\n추출 {}건 / 건너뜀 {}건 / 실패 {}건 / {:.0f}s".format(
        ok, skip, fail, time.perf_counter() - t0))
    if failures:
        print("\n실패 목록(평가 문항을 만들 수 없는 문서):")
        for n, why in failures[:40]:
            print("  - {:<52} {}".format(n[:52], why))
    print("\n캐시: " + CACHE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
