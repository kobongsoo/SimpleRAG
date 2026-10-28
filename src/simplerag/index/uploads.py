#------------------------------------------------------------------
# 업로드 문서 인덱스 — 대화마다 따로 두는 작은 인덱스 (plan/문서업로드_설계서_20261028.html)
#=> RAGSearchBox 입력란의 [+] 로 올린 문서를 그 대화에서만 찾게 한다.
#   인덱싱 폴더의 인덱스(Qdrant · BM25 · index_state.json)와 섞지 않고 대화별 폴더 하나에 둔다 —
#   폴더째 지우면 끝이라 기존 인덱스에 흔적이 남지 않는다(설계서 §3).
#
#   <데이터 폴더>/uploads/<대화 id>/
#     meta.json     기준 파일 — 판 · 임베딩 모델 · 차원 · 청킹 설정 · 올린 파일 목록
#     files/        올린 원본의 사본(<순번>_<원래 이름>)
#     chunks.jsonl  청크 한 줄씩 {id, file, text, doc_name, chunk_idx, section_id}
#     vectors.f32   청크 수 × 384 float32 리틀엔디언, L2 정규화(임베더 출력 그대로)
#   Rust 판 rust/simplerag/src/uploads.rs 와 같은 형식 — 데이터 폴더가 같으면 판을 바꿔도 다시 임베딩하지 않는다.
#
#   검색은 Qdrant 없이 전수 내적(수백 청크라 1ms 안팎) + 메모리 BM25 — UploadCorpus.
#------------------------------------------------------------------

import json
import os
import re
import shutil
import threading
import time

from .. import config

DIR_NAME = "uploads"
META = "meta.json"
CHUNKS = "chunks.jsonl"
VECTORS = "vectors.f32"
FILES = "files"
VERSION = 1
# 대화 하나에 넣을 수 있는 청크 상한(설계서 §10) — 전수 내적·메모리 BM25 가 가벼운 범위
MAX_CHUNKS = 3000
# 워커 메모리에 둘 업로드 코퍼스 수(최근 것부터)
CACHE_CONVS = 3
# 출처 카드의 폴더 칸에 보일 이름
FOLDER_LABEL = "업로드"

_cache = {}            # 대화 id → (meta 수정 시각, UploadCorpus)
_order = []            # 최근에 쓴 대화 id(앞이 오래된 것)
_lock = threading.Lock()


#------------------------------------------------------------------
# 대화 id 를 폴더 이름으로
#=> RAGSearchBox 대화 id 는 "1793171456.529000" 같은 숫자·점이다. 경로를 벗어나는 글자가 오지 않게 거른다.
#
# -in: conv = 대화 id
#
# -out: 폴더 이름에 쓸 글자(비면 None)
# -out: error = 없음
#------------------------------------------------------------------
def safe_id(conv):
    s = re.sub(r"[^0-9A-Za-z._-]", "_", str(conv or "")).strip("._")
    return s or None


#------------------------------------------------------------------
# 업로드 폴더들
#
# -in: conv = 대화 id (None 이면 uploads 폴더 자체)
#
# -out: 절대 경로
# -out: error = 대화 id 가 비면 ValueError
#------------------------------------------------------------------
def upload_dir(conv=None):
    base = os.path.join(config.ROOT, DIR_NAME)
    if conv is None:
        return base
    sid = safe_id(conv)
    if not sid:
        raise ValueError("대화 id 가 없습니다")
    return os.path.join(base, sid)


#------------------------------------------------------------------
# meta.json 읽기
#
# -in: conv = 대화 id
#
# -out: dict 또는 None(없거나 깨짐)
# -out: error = 없음
#------------------------------------------------------------------
def read_meta(conv):
    try:
        with open(os.path.join(upload_dir(conv), META), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) and m.get("files") else None
    except Exception:
        return None


#------------------------------------------------------------------
# 지금 설정으로 만든 인덱스인가
#=> 임베딩 모델 · 차원 · 청킹 설정이 다르면 벡터를 섞어 쓸 수 없다 — 사본으로 다시 만든다(설계서 §5).
#
# -in: meta = read_meta 결과
#
# -out: True | False
# -out: error = 없음
#------------------------------------------------------------------
def compatible(meta):
    from .indexer import chunk_params

    return (meta.get("version") == VERSION and meta.get("dim") == config.EMBED.dim
            and meta.get("embed_model") == os.path.basename(os.path.normpath(config.MODEL_DIR))
            and meta.get("chunk") == chunk_params())


#------------------------------------------------------------------
# 파일 하나를 청크로 (인덱서와 같은 추출·청킹)
#=> 인덱싱 폴더 문서와 같은 규칙으로 잘라야 검색 품질이 같다(indexer.sync_doc 의 추출 부분과 같다).
#
# -in: path = 파일 경로 / name = 문서 이름 / ext = build_extractor() 결과 / embedder = 토크나이저를 빌린다
#
# -out: [{text, section_id}] — 비면 빈 목록
# -out: error = 추출 예외는 전파
#------------------------------------------------------------------
def extract_chunks(path, name, ext, embedder):
    from ..chunk import split_structured, split_text
    from ..extract import extract_text
    from .indexer import _extract_blocks

    if config.EXTRACT_ENGINE == "blocks":
        _text, _rows, metas = _extract_blocks(path, name, ext, embedder)
        return [{"text": m["text"], "section_id": m.get("section_id")} for m in metas if m.get("text")]
    text, _rows = extract_text(ext, path)
    if config.CHUNK_MODE == "structured":
        chunks = split_structured(text, embedder.tokenizer, title=os.path.splitext(name)[0])
    else:
        chunks = split_text(text, embedder.tokenizer)
    return [{"text": c, "section_id": None} for c in chunks if c]


#------------------------------------------------------------------
# 청크·벡터 읽기
#
# -in: d = 대화 폴더
#
# -out: (청크 목록, (N, dim) float32 배열)
# -out: error = 파일이 깨졌으면 예외 전파
#------------------------------------------------------------------
def _read_rows(d):
    import numpy as np

    rows = []
    p = os.path.join(d, CHUNKS)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    vp = os.path.join(d, VECTORS)
    vecs = (np.fromfile(vp, dtype="<f4").reshape(-1, config.EMBED.dim) if os.path.isfile(vp)
            else np.zeros((0, config.EMBED.dim), dtype=np.float32))
    if len(rows) != len(vecs):
        raise ValueError("청크 {}개와 벡터 {}개가 맞지 않습니다".format(len(rows), len(vecs)))
    return rows, vecs


#------------------------------------------------------------------
# 청크·벡터·meta 쓰기 (원자적)
#=> *.tmp 에 쓰고 이름을 바꾼다. meta.json 을 맨 마지막에 바꿔 meta 가 늘 기준이 되게 한다.
#
# -in: d = 대화 폴더 / rows = 청크 목록 / vecs = (N, dim) 배열 / meta = meta dict
#
# -out: 없음
# -out: error = 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def _write_all(d, rows, vecs, meta):
    import numpy as np

    with open(os.path.join(d, CHUNKS + ".tmp"), "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    np.ascontiguousarray(vecs, dtype="<f4").tofile(os.path.join(d, VECTORS + ".tmp"))
    with open(os.path.join(d, META + ".tmp"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    for name in (CHUNKS, VECTORS, META):
        os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))


#------------------------------------------------------------------
# 빈 meta (지금 설정으로)
#
# -in: conv = 대화 id
#
# -out: meta dict
# -out: error = 없음
#------------------------------------------------------------------
def _new_meta(conv):
    from .indexer import chunk_params

    return {"version": VERSION, "conv": str(conv), "dim": config.EMBED.dim,
            "embed_model": os.path.basename(os.path.normpath(config.MODEL_DIR)),
            "chunk": chunk_params(), "files": []}


#------------------------------------------------------------------
# 문서 하나 올리기 (/upload)
#=> 1) 사본을 files/ 에 둔다(원본이 옮겨져도 출처를 열 수 있게)
#   2) 추출 → 청킹 → 임베딩(진행은 on_progress 로)
#   3) 기존 청크 뒤에 붙여 원자적으로 다시 쓴다
#   대화 청크 합이 MAX_CHUNKS 를 넘으면 그 파일은 넣지 않는다.
#
# -in: conv = 대화 id / path = 올릴 파일 / embedder = 임베더 / ext = 추출기(None 이면 만든다)
# -in: on_progress = fn(단계 글) — 워커가 stderr 로 내보내 진행 막대에 쓴다(None 이면 조용히)
#
# -out: {"ok", "conv", "name", "chunks", "files", "ms"} 또는 {"ok": False, "why"}
# -out: error = 없음 (실패는 why 로)
#------------------------------------------------------------------
def add(conv, path, embedder, ext=None, on_progress=None):
    from .indexer import TEXT_EXTS, build_extractor, is_transient

    say = on_progress or (lambda m: None)
    t0 = time.perf_counter()
    try:
        d = upload_dir(conv)
    except ValueError as e:
        return {"ok": False, "why": str(e)}
    path = os.path.abspath(path or "")
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return {"ok": False, "conv": str(conv), "name": name, "why": "파일이 없습니다"}
    if os.path.splitext(name)[1].lower() not in TEXT_EXTS or is_transient(path):
        return {"ok": False, "conv": str(conv), "name": name, "why": "지원하지 않는 형식입니다"}
    with _lock:
        meta = read_meta(conv)
        if meta is not None and not compatible(meta):
            # 설정이 바뀐 옛 인덱스 — 사본으로 다시 만든 뒤 이어 붙인다
            r = rebuild(conv, embedder, ext, on_progress, locked=True)
            if not r["ok"]:
                return dict(r, name=name)
            meta = read_meta(conv)
        meta = meta or _new_meta(conv)
        os.makedirs(os.path.join(d, FILES), exist_ok=True)
        rows, vecs = _read_rows(d) if meta["files"] else ([], None)
        # 같은 이름을 두 번 올려도 겹치지 않게 사본 앞에 순번을 붙인다
        seq = max([f.get("seq", 0) for f in meta["files"]] + [0]) + 1
        stored = os.path.join(FILES, "{}_{}".format(seq, name))
        try:
            shutil.copyfile(path, os.path.join(d, stored))
            say("[upload] 글자 뽑는 중 — {}".format(name))
            ext = ext or build_extractor()
            chunks = extract_chunks(os.path.join(d, stored), name, ext, embedder)
            if not chunks:
                raise ValueError("글자를 뽑지 못했습니다(스캔 문서는 아직 지원하지 않습니다)")
            if len(rows) + len(chunks) > MAX_CHUNKS:
                raise ValueError("대화 하나에 넣을 수 있는 분량(청크 {}개)을 넘습니다".format(MAX_CHUNKS))
            say("[upload] 임베딩 0/{}".format(len(chunks)))
            new = embedder.embed_passages([c["text"] for c in chunks],
                                          on_batch=lambda done, total: say("[upload] 임베딩 {}/{}".format(done, total)))
        except Exception as e:
            _remove_quiet(os.path.join(d, stored))
            if not meta["files"]:
                shutil.rmtree(d, ignore_errors=True)
            why = str(e) if isinstance(e, ValueError) else "{}: {}".format(type(e).__name__, str(e)[:80])
            return {"ok": False, "conv": str(conv), "name": name, "why": why}
        import numpy as np

        start = len(rows)
        fi = len(meta["files"])
        for i, c in enumerate(chunks):
            rows.append({"id": start + i, "file": fi, "text": c["text"], "doc_name": name,
                         "chunk_idx": i, "section_id": c.get("section_id")})
        vecs = new if vecs is None or not len(vecs) else np.vstack([vecs, new])
        meta["files"].append({"name": name, "stored": stored.replace(os.sep, "/"), "seq": seq,
                              "size": os.path.getsize(path), "chunks": [start, start + len(chunks)],
                              "added": time.strftime("%Y-%m-%dT%H:%M:%S")})
        _write_all(d, rows, vecs, meta)
        _forget(conv)
    return {"ok": True, "conv": str(conv), "name": name, "chunks": len(chunks),
            "files": len(meta["files"]), "ms": int((time.perf_counter() - t0) * 1000)}


#------------------------------------------------------------------
# 문서 하나 빼기 (/upload-remove — 첨부 칩 [×])
#=> 그 파일의 청크·벡터를 빼고 남은 것의 id 를 0부터 다시 매긴다(임베딩 불필요).
#   남은 파일이 없으면 폴더째 지운다.
#
# -in: conv = 대화 id / name = 뺄 파일 이름(같은 이름이 여럿이면 마지막에 올린 것)
#
# -out: {"ok", "conv", "name", "files"} 또는 {"ok": False, "why"}
# -out: error = 없음
#------------------------------------------------------------------
def remove(conv, name):
    with _lock:
        meta = read_meta(conv)
        if meta is None:
            return {"ok": False, "conv": str(conv), "name": name, "why": "올린 문서가 없습니다"}
        idx = [i for i, f in enumerate(meta["files"]) if f.get("name") == name]
        if not idx:
            return {"ok": False, "conv": str(conv), "name": name, "why": "그 이름의 문서가 없습니다"}
        fi = idx[-1]
        d = upload_dir(conv)
        if len(meta["files"]) == 1:
            delete(conv, locked=True)
            return {"ok": True, "conv": str(conv), "name": name, "files": 0}
        rows, vecs = _read_rows(d)
        keep = [i for i, r in enumerate(rows) if r.get("file") != fi]
        gone = meta["files"].pop(fi)
        new_rows = []
        for r in (rows[i] for i in keep):
            r = dict(r, id=len(new_rows), file=r["file"] - (1 if r["file"] > fi else 0))
            new_rows.append(r)
        # 파일별 청크 범위를 다시 센다
        for k, f in enumerate(meta["files"]):
            ids = [r["id"] for r in new_rows if r["file"] == k]
            f["chunks"] = [ids[0], ids[-1] + 1] if ids else [0, 0]
        _write_all(d, new_rows, vecs[keep], meta)
        _remove_quiet(os.path.join(d, gone.get("stored", "")))
        _forget(conv)
    return {"ok": True, "conv": str(conv), "name": name, "files": len(meta["files"])}


#------------------------------------------------------------------
# 설정이 바뀐 업로드 인덱스를 사본으로 다시 만들기
#
# -in: conv, embedder, ext, on_progress = add 와 같음 / locked = 이미 _lock 을 쥐었나
#
# -out: {"ok": True} 또는 {"ok": False, "why"}
# -out: error = 없음
#------------------------------------------------------------------
def rebuild(conv, embedder, ext=None, on_progress=None, locked=False):
    if not locked:
        with _lock:
            return rebuild(conv, embedder, ext, on_progress, locked=True)
    from .indexer import build_extractor
    import numpy as np

    say = on_progress or (lambda m: None)
    meta = read_meta(conv)
    if meta is None:
        return {"ok": False, "why": "올린 문서가 없습니다"}
    d = upload_dir(conv)
    ext = ext or build_extractor()
    new_meta = _new_meta(conv)
    rows, parts = [], []
    for f in meta["files"]:
        src = os.path.join(d, f.get("stored", ""))
        say("[upload] 다시 만드는 중 — {}".format(f.get("name")))
        try:
            chunks = extract_chunks(src, f["name"], ext, embedder)
            vec = embedder.embed_passages([c["text"] for c in chunks]) if chunks else None
        except Exception as e:
            return {"ok": False, "why": "다시 만들지 못했습니다 — {}: {}".format(type(e).__name__, str(e)[:60])}
        if not chunks:
            continue
        start, fi = len(rows), len(new_meta["files"])
        for i, c in enumerate(chunks):
            rows.append({"id": start + i, "file": fi, "text": c["text"], "doc_name": f["name"],
                         "chunk_idx": i, "section_id": c.get("section_id")})
        parts.append(vec)
        new_meta["files"].append(dict(f, chunks=[start, start + len(chunks)]))
    if not new_meta["files"]:
        delete(conv, locked=True)
        return {"ok": False, "why": "다시 만들 문서가 없습니다"}
    _write_all(d, rows, np.vstack(parts), new_meta)
    _forget(conv)
    return {"ok": True}


#------------------------------------------------------------------
# 대화 폴더 지우기
#=> 보통은 RAGSearchBox 가 폴더를 직접 지운다(모델이 내려가 있어도 지워져야 하므로).
#   워커 쪽에서는 메모리 캐시만 비우고 남은 폴더를 지운다.
#
# -in: conv = 대화 id / locked = 이미 _lock 을 쥐었나
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def delete(conv, locked=False):
    if not locked:
        with _lock:
            return delete(conv, locked=True)
    try:
        shutil.rmtree(upload_dir(conv), ignore_errors=True)
    except ValueError:
        pass
    _forget(conv)


#------------------------------------------------------------------
# 메모리 캐시 비우기 (/upload-forget)
#
# -in: conv = 대화 id (None 이면 전부)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _forget(conv=None):
    if conv is None:
        _cache.clear()
        del _order[:]
        return
    sid = safe_id(conv)
    _cache.pop(sid, None)
    if sid in _order:
        _order.remove(sid)


forget = _forget


#------------------------------------------------------------------
# 지우기 — 실패해도 조용히(파일이 열려 있으면 다음 정리 때 다시 지운다)
#------------------------------------------------------------------
def _remove_quiet(p):
    try:
        if p and os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


class UploadCorpus:
    #------------------------------------------------------------------
    # 업로드 코퍼스 — 한 대화의 청크·벡터·BM25 (검색 1차 후보용)
    #=> HybridRetriever._search 가 Qdrant·BM25 캐시 대신 이것으로 후보를 뽑는다.
    #
    # -필드: conv     = 대화 id
    # -필드: n_files  = 올린 문서 수
    # -필드: rows     = 청크 목록(id 순)
    # -필드: vecs     = (N, dim) float32, L2 정규화
    # -필드: payloads = id → 근거 payload(text · doc_name · doc_path(사본) · folder · chunk_idx · section_id)
    # -필드: bm25     = 메모리 BM25(Bm25Index — 디스크에 쓰지 않는다)
    #------------------------------------------------------------------
    def __init__(self, conv, meta, rows, vecs, tok):
        from .bm25 import Bm25Index

        d = upload_dir(conv)
        self.conv = str(conv)
        self.n_files = len(meta["files"])
        self.files = [f.get("name", "") for f in meta["files"]]
        self.rows = rows
        self.vecs = vecs
        self.payloads = {}
        for r in rows:
            f = meta["files"][r["file"]]
            self.payloads[r["id"]] = {"text": r["text"], "doc_name": r.get("doc_name") or f.get("name", ""),
                                      "doc_path": os.path.normpath(os.path.join(d, f.get("stored", ""))),
                                      "folder": FOLDER_LABEL, "chunk_idx": r.get("chunk_idx", 0),
                                      "section_id": r.get("section_id")}
        self.bm25 = Bm25Index(path=os.path.join(d, "bm25.cache"))
        self.bm25.build([r["text"] for r in rows], [r["id"] for r in rows], tok)

    #------------------------------------------------------------------
    # 의미 검색 — 전수 내적
    #
    # -in: qv = 질의 벡터(L2 정규화) / k = 후보 수
    #
    # -out: [(id, 점수, payload)] 점수 순
    # -out: error = 없음
    #------------------------------------------------------------------
    def search(self, qv, k):
        import numpy as np

        if not len(self.vecs):
            return []
        scores = self.vecs @ np.asarray(qv, dtype=np.float32)
        k = min(k, len(scores))
        # 같은 점수는 앞 청크가 먼저(안정 정렬 — Rust 판과 같은 순서)
        order = np.argsort(-scores, kind="stable")[:k]
        return [(int(i), float(scores[i]), self.payloads[int(i)]) for i in order]


#------------------------------------------------------------------
# 대화의 업로드 코퍼스 (캐시)
#=> meta.json 수정 시각이 같으면 앞서 읽은 것을 쓴다. 설정이 바뀐 옛 인덱스면 사본으로 다시 만든다.
#
# -in: conv = 대화 id / embedder = 임베더(토크나이저 · 다시 만들기용)
#
# -out: UploadCorpus 또는 None(그 대화에 올린 문서가 없음)
# -out: error = 없음 (깨진 폴더는 None)
#------------------------------------------------------------------
def corpus(conv, embedder):
    try:
        d = upload_dir(conv)
    except ValueError:
        return None
    meta = read_meta(conv)
    if meta is None:
        _forget(conv)
        return None
    if not compatible(meta):
        if not rebuild(conv, embedder)["ok"]:
            return None
        meta = read_meta(conv)
        if meta is None:
            return None
    sid = safe_id(conv)
    mtime = os.path.getmtime(os.path.join(d, META))
    with _lock:
        hit = _cache.get(sid)
        if hit and hit[0] == mtime:
            if sid in _order:
                _order.remove(sid)
            _order.append(sid)
            return hit[1]
        try:
            rows, vecs = _read_rows(d)
            c = UploadCorpus(conv, meta, rows, vecs, embedder.tokenizer)
        except Exception:
            return None
        _cache[sid] = (mtime, c)
        if sid in _order:
            _order.remove(sid)
        _order.append(sid)
        while len(_order) > CACHE_CONVS:
            _cache.pop(_order.pop(0), None)
        return c
