# -*- coding: utf-8 -*-
"""Rust OCR 엔진 vs RapidOCR — 같은 그림으로 품질·속도·일치도 비교 (OCR 설계서 §13-1~13-3)

사용:
  python bench/bench_ocr.py --docs D:/분류함 --exe rust/target/release/simplerag.exe --models models/ocr
"""
import argparse
import glob
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter


#------------------------------------------------------------------
# 비교용 글자 정리
#=> 공백·기호를 빼고 한글·영문·숫자만 남긴다. 읽는 순서 차이는 글자 F1 에서 따로 다룬다.
#
# -in: s = 문자열
#
# -out: s = 정리된 문자열(소문자)
# -out: error = 예외 없음
#------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^0-9A-Za-z\uac00-\ud7a3]", "", s).lower()


#------------------------------------------------------------------
# 글자 F1 · 단어 재현율
#=> 정답(텍스트층)과 OCR 결과를 비교한다.
#    1) 글자 F1: 순서와 상관없이 글자 다중집합의 교집합으로 정밀도·재현율을 낸다
#    2) 단어 재현율: 정답의 2자 이상 단어가 OCR 결과(공백 없앤 글)에 들어 있는 비율 — 검색에 가장 가깝다
#
# -in: gt  = 정답 글
# -in: hyp = OCR 결과 글
#
# -out: (글자 F1, 단어 재현율)
# -out: error = 정답이 30자 미만이면 None
#------------------------------------------------------------------
def score(gt, hyp):
    g, h = norm(gt), norm(hyp)
    if len(g) < 30:
        return None
    inter = sum((Counter(g) & Counter(h)).values())
    p, r = inter / max(1, len(h)), inter / len(g)
    f1 = 2 * p * r / max(1e-9, p + r)
    words = re.findall(r"[0-9A-Za-z\uac00-\ud7a3]{2,}", gt.lower())
    wr = sum(1 for w in words if w in h) / max(1, len(words))
    return f1, wr


#------------------------------------------------------------------
# 두 OCR 결과의 글자 일치도 (Rust 와 RapidOCR 가 같은 글을 읽었나)
#
# -in: a, b = OCR 결과 글 두 개
#
# -out: 글자 F1 (둘 다 비면 1.0)
# -out: error = 예외 없음
#------------------------------------------------------------------
def agree(a, b):
    a, b = norm(a), norm(b)
    if not a and not b:
        return 1.0
    inter = sum((Counter(a) & Counter(b)).values())
    return 2 * inter / max(1, len(a) + len(b))


#------------------------------------------------------------------
# 스캔본 흉내
#=> 깨끗한 그림을 0.7° 기울이고, 흐리게 하고, 잡음을 넣고, JPEG 55 로 뭉갠다(2026-11-13 실측과 같은 조건).
#
# -in: img = PIL 그림
#
# -out: 열화된 PIL 그림(RGB)
# -out: error = 예외 없음
#------------------------------------------------------------------
def degrade(img):
    g = img.convert("L").rotate(0.7, expand=True, fillcolor=255, resample=Image.BICUBIC)
    g = g.filter(ImageFilter.GaussianBlur(0.8))
    arr = np.asarray(g).astype(np.float32)
    arr = np.clip(arr + np.random.default_rng(0).normal(0, 8, arr.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).convert("RGB").save(buf, "JPEG", quality=55)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


#------------------------------------------------------------------
# 정답 쪽 고르기
#=> 텍스트층이 있는 한국어 PDF 를 씨앗 7 로 섞어, 문서마다 첫 쪽·가운데 쪽 중 한글 150자 이상인 쪽을
#   200dpi 로 그린다. 2026-11-13 실측과 같은 40쪽이 나온다.
#
# -in: root   = 문서 폴더
# -in: n_docs = 문서 수(기본 20 → 최대 40쪽)
# -in: seed   = 섞기 씨앗(기본 7)
#
# -out: [(이름, PIL 그림, 정답 글)]
# -out: error = 열리지 않는 PDF 는 건너뜀
#------------------------------------------------------------------
def pick_pages(root, n_docs=20, seed=7):
    pdfs = sorted(glob.glob(os.path.join(root, "**", "*.pdf"), recursive=True))
    random.Random(seed).shuffle(pdfs)
    out = []
    for p in pdfs:
        if len(out) >= n_docs * 2:
            break
        try:
            doc = pdfium.PdfDocument(p)
            n = len(doc)
            for i in sorted({0, n // 2}):
                t = doc[i].get_textpage().get_text_range()
                # 한글이 충분한 쪽만 — 정답이 있어야 비교할 수 있다
                if len(re.findall(r"[\uac00-\ud7a3]", t)) < 150:
                    continue
                img = doc[i].render(scale=200 / 72).to_pil().convert("RGB")
                out.append((f"{os.path.basename(p)}#p{i+1}", img, t))
            doc.close()
        except Exception:
            continue
    return out[: n_docs * 2]


#------------------------------------------------------------------
# RapidOCR 엔진 (정답 역할)
#=> Rust 엔진과 같은 설정: 방향 분류 끔, 스레드 4, 검출 limit min 736, 큰 변 4000, ImageNet 평균·표준편차.
#
# -in: models = 모델 폴더(PP-OCRv6_tiny_det.onnx · korean_PP-OCRv5_mobile_rec.onnx · .dict.txt)
#
# -out: RapidOCR 객체
# -out: error = rapidocr 가 없거나 모델 적재 실패 시 예외 전파
#------------------------------------------------------------------
def rapid_engine(models):
    from rapidocr import RapidOCR
    return RapidOCR(params={
        "Global.use_cls": False, "Global.log_level": "error", "Global.max_side_len": 4000,
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Det.model_path": os.path.join(models, "PP-OCRv6_tiny_det.onnx"),
        "Det.mean": [0.485, 0.456, 0.406], "Det.std": [0.229, 0.224, 0.225],
        "Rec.model_path": os.path.join(models, "korean_PP-OCRv5_mobile_rec.onnx"),
        "Rec.rec_keys_path": os.path.join(models, "korean_PP-OCRv5_mobile_rec.dict.txt"),
    })


#------------------------------------------------------------------
# 측정 실행
#=> 1) 쪽 40개를 깨끗한 판·스캔 흉내 판 PNG 로 임시 폴더에 쓴다
#   2) Rust 엔진(simplerag ocr <폴더> --out)과 RapidOCR 로 같은 파일을 OCR 한다
#   3) 정답 대비 글자 F1·단어 재현율, 두 엔진 일치도, 쪽당 시간을 낸다
#
# -in: 없음 (명령줄 인자)
#
# -out: 없음 (표준 출력 + --json 이 있으면 결과 파일)
# -out: error = exe·모델이 없으면 subprocess/적재 예외 전파
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True)
    ap.add_argument("--exe", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    pages = pick_pages(a.docs)
    print("쪽", len(pages), flush=True)
    rapid = rapid_engine(a.models)
    result = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind in ("clean", "scanlike"):
            d = os.path.join(tmp, kind)
            os.makedirs(d)
            # 파일 이름 순서 = 쪽 순서 (Rust 는 이름 순으로 읽는다)
            for i, (_, img, _) in enumerate(pages):
                (img if kind == "clean" else degrade(img)).save(os.path.join(d, f"p{i:03d}.png"))
            out_json = os.path.join(tmp, f"{kind}.json")
            env = dict(os.environ, SIMPLERAG_OCR_DIR=os.path.abspath(a.models))
            t0 = time.time()
            subprocess.run([a.exe, "ocr", d, "--out", out_json, "--chars", "0"], check=True, env=env,
                           stdout=subprocess.DEVNULL)
            rust_wall = time.time() - t0
            rj = json.load(open(out_json, encoding="utf-8"))["pages"]
            files = sorted(os.listdir(d))
            rapid(np.asarray(Image.open(os.path.join(d, files[0])).convert("RGB"))[:, :, ::-1].copy())  # 예열
            rs, ps, ag, rt, pt = [], [], [], [], []
            for i, (name, _, gt) in enumerate(pages):
                rtxt = "\n".join(l["t"] for l in rj[str(i + 1)]["lines"])
                rt.append(rj[str(i + 1)]["ocr_ms"] / 1000)
                im = np.asarray(Image.open(os.path.join(d, files[i])).convert("RGB"))[:, :, ::-1].copy()
                t0 = time.perf_counter()
                r = rapid(im)
                pt.append(time.perf_counter() - t0)
                ptxt = "\n".join(r.txts or [])
                s1, s2 = score(gt, rtxt), score(gt, ptxt)
                if s1 and s2:
                    rs.append(s1)
                    ps.append(s2)
                ag.append(agree(rtxt, ptxt))
            rs, ps = np.array(rs), np.array(ps)
            result[kind] = {
                "n": len(rs),
                "rust_char_f1": float(rs[:, 0].mean()), "rust_word_recall": float(rs[:, 1].mean()),
                "rapid_char_f1": float(ps[:, 0].mean()), "rapid_word_recall": float(ps[:, 1].mean()),
                "agree_mean": float(np.mean(ag)), "agree_min": float(np.min(ag)),
                "rust_sec_page": float(np.mean(rt)), "rapid_sec_page": float(np.mean(pt)),
                "rust_wall_sec": rust_wall,
            }
            print(kind, {k: round(v, 3) for k, v in result[kind].items()}, flush=True)
    if a.json:
        json.dump(result, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
