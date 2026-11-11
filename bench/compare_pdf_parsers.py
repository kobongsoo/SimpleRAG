#------------------------------------------------------------------
# PDF 파서 비교 — 현재(pypdfium2 구조 파서) · pdfplumber · OpenDataLoader PDF
#=> 같은 PDF 묶음(sample_pdf 30개)을 세 파서로 읽어 아래를 잰다.
#    · 속도 — 쪽당 ms. OpenDataLoader 는 Java 라 파일마다 JVM 을 띄우는 경우(cold)와
#             한 번에 폴더째 넘기는 경우(batch) 둘 다 잰다
#    · 근거 문장 보존 — golden_pdf30 의 reference_contexts(정답 근거 원문)가
#             파서 출력에 그대로 들어 있는가(문서 안 · 맞는 쪽 안), 8글자 조각 재현율
#    · 구조 — 제목 · 표 · 목록 항목을 몇 개 찾았나
#   공백·문장부호는 파서마다 다르게 붙이므로 비교 전에 한글·영문·숫자만 남긴다.
#
#   실행(비교 대상이 설치된 가상환경에서):
#     <venv>\Scripts\python bench\compare_pdf_parsers.py --out <결과 폴더>
#   필요: pip install pdfplumber opendataloader-pdf  + java 11 이상
#------------------------------------------------------------------

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# 청크에 들어가지 않는 블록 — 현재 파서 쪽 글에서 뺀다(OpenDataLoader 도 머리말·꼬리말을 기본 제외)
_SKIP_KINDS = {"header", "footer", "page_number", "toc"}
_KEEP = re.compile(r"[0-9A-Za-z가-힣]+")


#------------------------------------------------------------------
# 비교용 글자 정규화
#=> 파서마다 공백·줄바꿈·표 구분자(|)를 다르게 넣으므로 한글·영문·숫자만 이어 붙인다.
#
# -in: s = 원문 글
#
# -out: str = 소문자, 한글·영문·숫자만 남긴 글
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    # 호환 문자(전각 숫자 등)를 보통 글자로 맞춘 뒤 남길 글자만 모은다
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(_KEEP.findall(s))


#------------------------------------------------------------------
# 현재 파서(pypdfium2 구조 파서 + 공통 후처리)
#=> SimpleRAG 가 인덱싱 때 실제로 부르는 extract_document 를 그대로 부른다.
#
# -in: path = PDF 경로
#
# -out: dict = {pages: {쪽: 글}, n_pages, headings, tables, list_items}
# -out: error = 파서 예외 전파(폴백 없음 — 비교라서 실패를 그대로 보이게)
#------------------------------------------------------------------
def run_current(path):
    from simplerag.docmodel import extract as dx
    doc = dx.extract_document(path)
    pages, cnt = {}, {"heading": 0, "table": 0, "list_item": 0}
    for b in doc.blocks:
        if b.type in cnt:
            cnt[b.type] += 1
        if b.type in _SKIP_KINDS:
            continue
        pages.setdefault(b.page or 0, []).append(b.text)
    return {"pages": {k: "\n".join(v) for k, v in pages.items()}, "n_pages": doc.pages,
            "headings": cnt["heading"], "tables": cnt["table"], "list_items": cnt["list_item"]}


#------------------------------------------------------------------
# pdfplumber (글 + 선택적으로 표)
#=> 쪽마다 extract_text 로 글을 뽑고, with_tables 면 find_tables 로 표도 찾는다.
#   표 글자는 본문 글에 이미 들어 있으므로 개수만 센다(본문과 중복 방지).
#
# -in: path        = PDF 경로
# -in: with_tables = True 면 표 검출까지(시간에 포함)
#
# -out: dict = run_current 와 같은 꼴 (제목·목록은 pdfplumber 가 모르므로 0)
# -out: error = 예외 전파
#------------------------------------------------------------------
def run_plumber(path, with_tables):
    import pdfplumber
    pages, tables = {}, 0
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            pages[i] = page.extract_text() or ""
            if with_tables:
                tables += len(page.find_tables())
            # 쪽 캐시를 비워 큰 PDF 에서 메모리가 쌓이지 않게
            page.flush_cache()
    return {"pages": pages, "n_pages": n, "headings": 0, "tables": tables, "list_items": 0}


#------------------------------------------------------------------
# OpenDataLoader JSON → 쪽별 글 · 구조 개수
#=> JSON 은 kids / list items / rows / cells 로 겹겹이 들어 있다. 문서 순서대로 훑으며
#   content 가 있는 마디의 글을 그 'page number' 쪽에 붙인다.
#
# -in: data = OpenDataLoader JSON(dict)
#
# -out: dict = run_current 와 같은 꼴
# -out: error = 없음
#------------------------------------------------------------------
def parse_odl_json(data):
    pages, cnt = {}, {"heading": 0, "table": 0, "list item": 0}

    # 마디 하나를 내려가며 글을 모은다(목록 항목은 자기 글 + 하위 kids 순서)
    def walk(n, page):
        if isinstance(n, dict):
            t = n.get("type")
            if t in cnt:
                cnt[t] += 1
            page = n.get("page number", page)
            c = n.get("content")
            if isinstance(c, str) and c.strip():
                pages.setdefault(page or 0, []).append(c)
            for k, v in n.items():
                if isinstance(v, (list, dict)):
                    walk(v, page)
        elif isinstance(n, list):
            for v in n:
                walk(v, page)

    walk(data.get("kids", []), None)
    return {"pages": {k: "\n".join(v) for k, v in pages.items()},
            "n_pages": data.get("number of pages", 0), "headings": cnt["heading"],
            "tables": cnt["table"], "list_items": cnt["list item"]}


#------------------------------------------------------------------
# OpenDataLoader CLI 실행
#=> java -jar 로 JSON 을 만든다. inputs 가 여러 개면 JVM 하나로 한 번에 처리한다.
#
# -in: jar     = opendataloader-pdf-cli.jar 경로
# -in: inputs  = PDF 경로 목록
# -in: out_dir = JSON 을 쓸 폴더
# -in: extra   = 추가 CLI 인자 목록(예: ["--table-method", "cluster"])
#
# -out: float = 걸린 초
# -out: error = CLI 실패 시 CalledProcessError
#------------------------------------------------------------------
def odl_cli(jar, inputs, out_dir, extra=()):
    cmd = ["java", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8", "-jar", jar,
           *inputs, "-o", out_dir, "-f", "json", "-q", *extra]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return time.perf_counter() - t0


#------------------------------------------------------------------
# 근거 문장 보존 채점
#=> 골든 문항의 reference_contexts 가 파서 글 안에 있는지 본다.
#    1) doc_hit  — 문서 전체 글 안에 그대로 있나
#    2) page_hit — 정답 쪽(쪽 경계를 넘는 발췌를 위해 다음 쪽까지 이은 글) 안에 있나
#    3) gram     — 발췌의 8글자 조각 중 문서 글에 있는 비율(부분 손상 정도)
#
# -in: res    = {파일명: 파서 결과}
# -in: golden = 골든 문항 목록
# -in: docmap = doc_id → 파일명 목록
#
# -out: dict = {n, doc_hit, page_hit, gram, miss: [실패 발췌 일부]}
# -out: error = 없음
#------------------------------------------------------------------
def score(res, golden, docmap):
    n = doc_hit = page_hit = 0
    gram_sum, miss = 0.0, []
    full_cache = {}
    for q in golden:
        for ctx, rp in zip(q["reference_contexts"], q["reference_pages"]):
            files = [f for f in docmap.get(rp["doc_id"], []) if f in res]
            if not files:
                continue
            r = res[files[0]]
            if files[0] not in full_cache:
                full_cache[files[0]] = norm("".join(r["pages"][k] for k in sorted(r["pages"])))
            full = full_cache[files[0]]
            p = rp["page"]
            near = norm(r["pages"].get(p, "") + r["pages"].get(p + 1, ""))
            e = norm(ctx)
            if not e:
                continue
            n += 1
            hit = e in full
            doc_hit += hit
            page_hit += e in near
            grams = [e[i:i + 8] for i in range(0, max(1, len(e) - 7))]
            gram_sum += sum(g in full for g in grams) / len(grams)
            if not hit and len(miss) < 40:
                miss.append({"q": q["id"], "doc": rp["doc_id"], "ctx": ctx[:80]})
    return {"n": n, "doc_hit": doc_hit, "page_hit": page_hit,
            "gram": round(gram_sum / max(n, 1), 4), "miss": miss}


#------------------------------------------------------------------
# 진입점
#=> 파일 목록을 모아 파서마다 돌리고, 시간·채점·구조 개수를 JSON 과 표로 낸다.
#    1) current · plumber_text · plumber_tables · odl_cold 를 파일마다 잰다
#    2) odl_batch 는 PDF 를 임시 폴더에 모아 JVM 한 번으로 잰다(쪽당 순수 처리 시간)
#    3) 골든 근거 문장 보존을 채점한다
#
# -in: 없음 (명령줄: --out 결과 폴더, --engines 쉼표 목록, --odl-extra 추가 인자)
#
# -out: 없음 (out/summary.json · out/<engine>/*.txt 작성)
# -out: error = 파서 예외는 파일 단위로 잡아 errors 에 기록
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--engines", default="current,plumber_text,plumber_tables,odl_cold,odl_batch")
    ap.add_argument("--odl-extra", default="")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    engines = a.engines.split(",")
    extra = a.odl_extra.split() if a.odl_extra else []

    import opendataloader_pdf
    jar = os.path.join(os.path.dirname(opendataloader_pdf.__file__), "jar",
                       "opendataloader-pdf-cli.jar")
    files = sorted(glob.glob(os.path.join(ROOT, "sample_pdf", "**", "*.pdf"), recursive=True))
    golden = [json.loads(l) for l in open(os.path.join(ROOT, "dataset", "golden_pdf30.jsonl"),
                                           encoding="utf-8")]
    docmap = {d["doc_id"]: d["files"] for d in
              json.load(open(os.path.join(ROOT, "dataset", "golden_pdf30_docs.json"), encoding="utf-8"))}

    summary = {}
    for eng in engines:
        if eng == "odl_batch":
            continue
        res, times, errors = {}, {}, []
        tmp = tempfile.mkdtemp(prefix="odl_")
        for f in files:
            name = os.path.basename(f)
            try:
                t0 = time.perf_counter()
                if eng == "current":
                    r = run_current(f)
                elif eng == "plumber_text":
                    r = run_plumber(f, False)
                elif eng == "plumber_tables":
                    r = run_plumber(f, True)
                elif eng == "odl_cold":
                    # 파일마다 JVM 기동 포함 — 인덱서가 파일 하나씩 부를 때의 실제 비용
                    odl_cli(jar, [f], tmp, extra)
                    jp = os.path.join(tmp, os.path.splitext(name)[0] + ".json")
                    r = parse_odl_json(json.load(open(jp, encoding="utf-8")))
                else:
                    raise ValueError(eng)
                times[name] = time.perf_counter() - t0
                res[name] = r
            except Exception as e:      # noqa: BLE001 — 한 파일 실패로 비교 전체가 멈추지 않게
                errors.append("{}: {}: {}".format(name, type(e).__name__, str(e)[:150]))
        shutil.rmtree(tmp, ignore_errors=True)
        # 사람이 눈으로 비교할 수 있게 쪽 구분을 넣은 글을 저장
        od = os.path.join(a.out, eng)
        os.makedirs(od, exist_ok=True)
        for name, r in res.items():
            with open(os.path.join(od, name + ".txt"), "w", encoding="utf-8") as w:
                for k in sorted(r["pages"]):
                    w.write("\n===== p{} =====\n{}\n".format(k, r["pages"][k]))
        pages = sum(r["n_pages"] for r in res.values())
        total = sum(times.values())
        summary[eng] = {
            "files": len(res), "pages": pages, "sec": round(total, 2),
            "ms_per_page": round(1000 * total / max(pages, 1), 2),
            "headings": sum(r["headings"] for r in res.values()),
            "tables": sum(r["tables"] for r in res.values()),
            "list_items": sum(r["list_items"] for r in res.values()),
            "chars": sum(len(norm("".join(r["pages"].values()))) for r in res.values()),
            "score": score(res, golden, docmap), "errors": errors,
            "per_file": {n: {"sec": round(times[n], 3), "pages": res[n]["n_pages"],
                             "tables": res[n]["tables"], "headings": res[n]["headings"]}
                         for n in res},
        }
        s = summary[eng]
        print("{:15s} {:4d}쪽 {:7.2f}s {:7.2f}ms/쪽  근거 {}/{} 쪽맞음 {} 8gram {:.3f}  제목 {} 표 {} 목록 {}  오류 {}".format(
            eng, pages, total, s["ms_per_page"], s["score"]["doc_hit"], s["score"]["n"],
            s["score"]["page_hit"], s["score"]["gram"], s["headings"], s["tables"],
            s["list_items"], len(errors)), flush=True)

    if "odl_batch" in engines:
        # 경로에 한글·공백이 있어도 되지만 파일명 충돌을 피해 임시 폴더에 모은다
        src = tempfile.mkdtemp(prefix="odl_in_")
        dst = tempfile.mkdtemp(prefix="odl_out_")
        for f in files:
            shutil.copy(f, src)
        sec = odl_cli(jar, [src], dst, extra)
        pages = sum(json.load(open(j, encoding="utf-8")).get("number of pages", 0)
                    for j in glob.glob(os.path.join(dst, "**", "*.json"), recursive=True))
        summary["odl_batch"] = {"pages": pages, "sec": round(sec, 2),
                                "ms_per_page": round(1000 * sec / max(pages, 1), 2)}
        print("odl_batch       {:4d}쪽 {:7.2f}s {:7.2f}ms/쪽".format(pages, sec, 1000 * sec / max(pages, 1)))
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)

    out = os.path.join(a.out, "summary{}.json".format("_" + a.tag if a.tag else ""))
    json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장:", out)


if __name__ == "__main__":
    main()
