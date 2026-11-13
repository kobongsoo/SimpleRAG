# -*- coding: utf-8 -*-
"""스캔 PDF OCR — Rust 판 ocr 모듈의 Python 판 (OCR 설계서 §11)

Rust 판과 같은 모델(PP-OCRv6_tiny_det + korean_PP-OCRv5_mobile_rec)을 RapidOCR 로 돌리고,
같은 쪽 판정 규칙 · 같은 줄 묶기 · 같은 캐시 파일 형식(<데이터 루트>/ocr_cache)을 쓴다.
그래서 어느 판이 만든 캐시든 서로 읽는다. Python 판은 대기열 없이 추출할 때 바로 OCR 한다.
"""
import hashlib
import json
import os
import re

import numpy as np

ENGINE = "ppocrv6-tiny-det+ko-ppocrv5-mobile-rec/1"
DET_FILE = "PP-OCRv6_tiny_det.onnx"
REC_FILE = "korean_PP-OCRv5_mobile_rec.onnx"
DICT_FILE = "korean_PP-OCRv5_mobile_rec.dict.txt"
DPI = 200.0
GRID = 40

_engine = None


#------------------------------------------------------------------
# OCR 을 쓸 수 있나
#=> 설정 extract.ocr 가 켜져 있고, 모델 세 파일이 있고, rapidocr 를 불러올 수 있을 때만 True.
#
# -in: 없음
#
# -out: ok = 쓸 수 있으면 True
# -out: error = 예외 없음 (rapidocr 가 없으면 False)
#------------------------------------------------------------------
def enabled():
    from .. import config
    if not config.OCR:
        return False
    if not all(os.path.isfile(os.path.join(config.OCR_DIR, f)) for f in (DET_FILE, REC_FILE, DICT_FILE)):
        return False
    try:
        import rapidocr  # noqa: F401
    except Exception:  # noqa: BLE001 — 설치 안 됨
        return False
    return True


#------------------------------------------------------------------
# RapidOCR 엔진 (처음 한 번만 만든다)
#=> Rust 엔진과 같은 설정: 방향 분류 끔, 스레드 OCR_THREADS, 검출 limit min 736, 큰 변 4000,
#   ImageNet 평균·표준편차. 이 설정으로 Rust 와 글자 F1 이 소수 셋째 자리까지 같았다(2026-11-13).
#
# -in: 없음
#
# -out: RapidOCR 객체
# -out: error = 모델 적재 실패 시 예외 전파
#------------------------------------------------------------------
def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        from .. import config
        _engine = RapidOCR(params={
            "Global.use_cls": False, "Global.log_level": "error", "Global.max_side_len": 4000,
            "EngineConfig.onnxruntime.intra_op_num_threads": config.OCR_THREADS,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.model_path": os.path.join(config.OCR_DIR, DET_FILE),
            "Det.mean": [0.485, 0.456, 0.406], "Det.std": [0.229, 0.224, 0.225],
            "Rec.model_path": os.path.join(config.OCR_DIR, REC_FILE),
            "Rec.rec_keys_path": os.path.join(config.OCR_DIR, DICT_FILE),
        })
    return _engine


#------------------------------------------------------------------
# 파일 내용 해시 (Rust indexer::content_hash 와 같은 sha1)
#
# -in: path = 파일 경로
#
# -out: sha1 16진수
# -out: error = 읽기 실패 시 OSError 전파
#------------------------------------------------------------------
def content_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


#------------------------------------------------------------------
# 캐시 경로
#=> Rust 판과 같은 자리 — <데이터 루트>/ocr_cache/<해시 앞 2자>/<해시>.json
#
# -in: h = 파일 내용 해시
#
# -out: 캐시 파일 경로
# -out: error = 예외 없음
#------------------------------------------------------------------
def cache_path(h):
    from .. import config
    return os.path.join(config.ROOT, "ocr_cache", h[:2], h + ".json")


#------------------------------------------------------------------
# 캐시 읽기
#=> 없거나 깨졌거나 엔진 이름이 다르면 빈 캐시를 돌려준다(쪽 번호 열쇠는 int 로 바꾼다).
#
# -in: h = 파일 내용 해시
#
# -out: {"engine", "dpi", "pages": {쪽 번호: [{t, s, b}]}}
# -out: error = 예외 없음
#------------------------------------------------------------------
def load_cache(h):
    try:
        with open(cache_path(h), encoding="utf-8") as f:
            c = json.load(f)
        if c.get("engine") != ENGINE:
            raise ValueError("다른 엔진")
        c["pages"] = {int(k): v for k, v in c.get("pages", {}).items()}
        return c
    except Exception:  # noqa: BLE001 — 없는 · 깨진 캐시는 새로
        return {"engine": ENGINE, "dpi": DPI, "pages": {}}


#------------------------------------------------------------------
# 캐시 쓰기 — 임시 파일에 쓴 뒤 바꿔 넣는다(쓰다 꺼져도 반쪽 파일이 남지 않게)
#
# -in: h = 파일 내용 해시 / c = 캐시
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def save_cache(h, c):
    p = cache_path(h)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    out = dict(c, pages={str(k): v for k, v in sorted(c["pages"].items())})
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


#------------------------------------------------------------------
# 그림 상자들이 쪽을 덮는 비율 (Rust ocr::coverage)
#=> 40×40 격자에 칠해 겹친 곳을 한 번만 센다.
#
# -in: boxes = [(왼, 아래, 오른, 위)] / pw, ph = 쪽 크기
#
# -out: 0~1 비율
# -out: error = 예외 없음
#------------------------------------------------------------------
def coverage(boxes, pw, ph):
    if not boxes or pw <= 0 or ph <= 0:
        return 0.0
    m = np.zeros((GRID, GRID), bool)
    for l, b, r, t in boxes:
        x0, x1 = int(max(l, 0) / pw * GRID), min(GRID, int(np.ceil(min(r, pw) / pw * GRID)))
        y0, y1 = int(max(b, 0) / ph * GRID), min(GRID, int(np.ceil(min(t, ph) / ph * GRID)))
        m[y0:y1, x0:x1] = True
    return float(m.mean())


#------------------------------------------------------------------
# OCR 할 쪽 고르기 (Rust ocr::pdf_ocr_pages 와 같은 규칙 — 설계서 §4-2)
#=> 1) 반복 그림: 문서 쪽의 절반 이상(3쪽 이상)에 같은 자리·같은 픽셀 크기로 나오는 그림
#   2) 글자 < 10 이고 그림 ≥ 30% → OCR / 글자 < 10 → 건너뜀
#      반복 아니고 쪽의 2% 이상인 그림 ≥ 70% 이고 글자 < 200 → OCR
#   3) 앞에서부터 OCR_MAX_PAGES 쪽까지
#
# -in: infos = 쪽마다 (글자 수, 쪽 가로, 쪽 세로, [(상자, 픽셀 크기)])
#
# -out: (OCR 할 쪽 번호(0부터) 목록, 상한으로 뺀 쪽 수)
# -out: error = 예외 없음
#------------------------------------------------------------------
def pick_pages(infos):
    from .. import config
    np_ = len(infos)
    key = lambda o: (tuple(int(round(v)) for v in o[0]), tuple(o[1]))  # noqa: E731 — 반복 그림 열쇠
    rep = {}
    for _, _, _, imgs in infos:
        for k in {key(o) for o in imgs}:
            rep[k] = rep.get(k, 0) + 1
    need = max(3.0, np_ * 0.5)
    out = []
    for i, (n, pw, ph, imgs) in enumerate(infos):
        if not imgs:
            continue
        area = pw * ph
        real = [o[0] for o in imgs
                if not (np_ >= 3 and rep.get(key(o), 0) >= need)
                and (o[0][2] - o[0][0]) * (o[0][3] - o[0][1]) >= 0.02 * area]
        cov_all = coverage([o[0] for o in imgs], pw, ph)
        cov_real = coverage(real, pw, ph)
        if (cov_all >= 0.3) if n < 10 else (cov_real >= 0.7 and n < 200):
            out.append(i)
    cap = max(1, config.OCR_MAX_PAGES)
    return out[:cap], max(0, len(out) - cap)


#------------------------------------------------------------------
# 쪽의 그림 객체 모으기 (pypdfium2) — pick_pages 의 입력
#
# -in: page = pypdfium2 쪽
#
# -out: (쪽 가로, 쪽 세로(회전 전), [((왼, 아래, 오른, 위), (픽셀 가로, 세로))])
# -out: error = 예외 없음 (읽기 실패한 객체는 건너뜀)
#------------------------------------------------------------------
def page_images(page):
    import pypdfium2.raw as R
    w, h = page.get_size()
    # 그림 위치는 회전 전 좌표라 쪽 크기도 회전 전으로 맞춘다
    if page.get_rotation() % 180 == 90:
        w, h = h, w
    out = []
    try:
        for o in page.get_objects(filter=[R.FPDF_PAGEOBJ_IMAGE], max_depth=5):
            try:
                out.append((tuple(o.get_bounds()), tuple(o.get_px_size())))
            except Exception:  # noqa: BLE001 — 깨진 그림 객체
                continue
    except Exception:  # noqa: BLE001 — 객체 훑기 실패는 판정만 못 할 뿐
        pass
    return w, h, out


#------------------------------------------------------------------
# PDF 쪽 하나 OCR (Rust 대기열의 한 쪽과 같음)
#=> 1) 200dpi 로 그려 BGR 로 바꾼다  2) RapidOCR  3) 글자층 글자와 절반 이상 겹친 줄은 버린다
#
# -in: page = pypdfium2 쪽 / tp = 그 쪽의 텍스트 페이지
#
# -out: [{"t": 글, "s": 점수, "b": [x0, y0, x1, y1]}] (그림 픽셀 좌표)
# -out: error = 엔진 · 그리기 실패 시 예외 전파
#------------------------------------------------------------------
def ocr_page(page, tp):
    scale = DPI / 72.0
    img = np.asarray(page.render(scale=scale).to_pil().convert("RGB"))[:, :, ::-1].copy()
    r = _get_engine()(img)
    lines = []
    for q, t, s in zip(r.boxes if r.boxes is not None else [], r.txts or [], r.scores or []):
        xs, ys = [p[0] for p in q], [p[1] for p in q]
        lines.append({"t": t, "s": round(float(s), 3), "b": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))]})
    # 글자층 글자 상자(PDF 좌표, 아래가 0) → 그림 좌표
    ph = page.get_height()
    chars = []
    for i in range(tp.count_chars()):
        ch = tp.get_text_range(i, 1)
        if not ch.strip():
            continue
        l, b, rr, t = tp.get_charbox(i)
        chars.append((l * scale, (ph - t) * scale, rr * scale, (ph - b) * scale))
    return drop_overlapping(lines, chars)


#------------------------------------------------------------------
# 글자층과 겹친 OCR 줄 빼기 (Rust ocr::drop_overlapping)
#=> 줄 상자 넓이의 절반 이상이 글자층 글자 상자와 겹치면 버린다(쪽 번호·도장 글자를 두 번 넣지 않게).
#
# -in: lines = OCR 줄 / chars = 글자 상자 [(x0, y0, x1, y1)]
#
# -out: 남긴 줄
# -out: error = 예외 없음
#------------------------------------------------------------------
def drop_overlapping(lines, chars):
    if not chars:
        return lines
    out = []
    for ln in lines:
        b = ln["b"]
        area = max(1.0, (b[2] - b[0]) * (b[3] - b[1]))
        ov = sum(max(0.0, min(b[2], c[2]) - max(b[0], c[0])) * max(0.0, min(b[3], c[3]) - max(b[1], c[1])) for c in chars)
        if ov < 0.5 * area:
            out.append(ln)
    return out


#------------------------------------------------------------------
# 줄 묶기 — OCR 상자들 → 화면의 한 줄씩 (Rust ocr::group_rows)
#=> 1) 세로 가운데로 정렬하고, 세로로 절반 이상 겹치면 같은 줄
#   2) 줄 안에서는 왼쪽부터, 상자 사이 틈이 글자 높이 0.5배보다 넓으면 띄어쓰기
#
# -in: lines = [{"t", "b"}]
#
# -out: 줄 글 목록(위에서 아래)
# -out: error = 예외 없음
#------------------------------------------------------------------
def group_rows(lines):
    order = sorted(range(len(lines)), key=lambda i: (lines[i]["b"][1] + lines[i]["b"][3]) / 2)
    rows = []  # [ids, top, bottom]
    for i in order:
        b = lines[i]["b"]
        h = max(1.0, b[3] - b[1])
        if rows:
            r = rows[-1]
            rh = max(1.0, r[2] - r[1])
            if min(b[3], r[2]) - max(b[1], r[1]) >= 0.5 * min(h, rh):
                r[0].append(i)
                r[1], r[2] = min(r[1], b[1]), max(r[2], b[3])
                continue
        rows.append([[i], b[1], b[3]])
    out = []
    for ids, _, _ in rows:
        ids.sort(key=lambda i: lines[i]["b"][0])
        avg_h = sum(lines[i]["b"][3] - lines[i]["b"][1] for i in ids) / len(ids)
        text, prev = "", None
        for i in ids:
            b = lines[i]["b"]
            # 상자 사이 넓은 틈은 띄어쓰기 — 한국어 인식이 상자 안 띄어쓰기를 자주 빠뜨린다
            if prev is not None and b[0] - prev > 0.5 * avg_h:
                text += " "
            text += lines[i]["t"].strip()
            prev = b[2] if prev is None else max(prev, b[2])
        out.append(text)
    return out


#------------------------------------------------------------------
# 문서의 OCR 줄 구하기 — 캐시에 없으면 지금 OCR 해 캐시에 넣는다 (Python 판은 대기열 없이)
#
# -in: path  = PDF 경로
# -in: pdf   = 열린 pypdfium2 문서
# -in: want  = OCR 할 쪽 번호(0부터)
#
# -out: {쪽 번호(0부터): [줄 글]}
# -out: error = 해시 · 엔진 실패 시 예외 전파 (상위가 잡아 경고로 남긴다)
#------------------------------------------------------------------
def rows_for(path, pdf, want):
    h = content_hash(path)
    c = load_cache(h)
    changed = False
    for i in want:
        if i + 1 in c["pages"]:
            continue
        page = pdf[i]
        try:
            tp = page.get_textpage()
            c["pages"][i + 1] = ocr_page(page, tp)
            changed = True
        finally:
            page.close()
    if changed:
        save_cache(h, c)
    return {i: [t for t in group_rows(c["pages"][i + 1]) if re.sub(r"\s", "", t)] for i in want}
