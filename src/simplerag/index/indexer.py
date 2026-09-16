#------------------------------------------------------------------
# 증분 인덱서 (설계서 결정10 / 결정11 / 결정12)
#=> 폴더의 문서를 추출 → 청킹 → 임베딩 → Qdrant·BM25 에 넣는다.
#
#   왜 증분·체크포인트가 필요한가
#    - 5만 청크 인덱싱에 약 21분이 걸렸고, 그 사이 처리량이 발열로 69→47
#      chunk/s 까지 떨어졌다. 짧은 샘플로 추정한 11분의 두 배다.
#    - 긴 작업은 중간에 끊길 수 있으므로 재개가 가능해야 한다.
#    - 문서가 조금 바뀌었다고 전체를 다시 돌릴 이유도 없다.
#
#   변경 감지 키는 (경로, mtime, size) 해시다. 내용 해시를 쓰면 정확하지만
#   파일을 통째로 읽어야 해서 감지 단계가 인덱싱만큼 느려진다.
#------------------------------------------------------------------

import hashlib
import json
import os
import sys
import time

from .. import config
from ..chunk import split_structured, split_text
from ..extract import build_extractor, extract_text


# 인덱싱 대상 확장자. 여기 없는 것은 조용히 건너뛴다.
#   - 이미지/압축(jpg/png/zip)은 텍스트가 없다.
#   - ipynb 는 JSON 이라 별도 처리기로 소스/마크다운 셀만 뽑는다(extract 모듈).
TEXT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".hwp", ".hwpx", ".txt", ".md", ".html", ".htm", ".py", ".ipynb",
}


#------------------------------------------------------------------
# 대상 파일 나열 (재귀)
#=> 하위 폴더까지 훑되 인덱싱 가능한 확장자만 고른다.
#
# -in: root, recursive
#
# -out: (files, skipped) = 대상 경로 목록, 확장자로 제외된 개수
#------------------------------------------------------------------
def list_files(root, recursive=True):
    files, skipped = [], 0

    def take(path):
        nonlocal skipped
        if os.path.splitext(path)[1].lower() in TEXT_EXTS:
            files.append(path)
        else:
            skipped += 1

    if recursive:
        for dirpath, _, names in os.walk(root):
            for n in names:
                take(os.path.join(dirpath, n))
    else:
        for n in os.listdir(root):
            fp = os.path.join(root, n)
            if os.path.isfile(fp):
                take(fp)
    return sorted(files), skipped


#------------------------------------------------------------------
# 파일 지문
#=> (경로, mtime, size) 로 변경을 감지한다.
#
# -out: hex 문자열
#------------------------------------------------------------------
def file_fingerprint(path):
    st = os.stat(path)
    raw = "{}|{}|{}".format(os.path.abspath(path), int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


#------------------------------------------------------------------
# 상태 파일 읽기/쓰기
#=> {문서경로: {fingerprint, chunks, next_id}} 와 전역 커서를 보관한다.
#------------------------------------------------------------------
def load_state(path=None):
    path = path or config.STATE_PATH
    if not os.path.isfile(path):
        return {"docs": {}, "next_id": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"docs": {}, "next_id": 0}


def save_state(state, path=None):
    path = path or config.STATE_PATH
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)          # 원자적 교체 — 중단 시 상태 파일이 깨지지 않게


#------------------------------------------------------------------
# 지금 설정의 청킹 파라미터
#=> 이 값이 다른 청크가 한 인덱스에 섞이면 검색 결과를 해석할 수 없다. 인덱스에
#   기록해 두고 다음 index 때 비교한다(REPORT §35).
#
# -in: 없음
#
# -out: dict = {tokens, overlap, mode, min_tokens, table_header_max_tokens, chunker_version}
# -out: error = 없음
#------------------------------------------------------------------
def chunk_params():
    return {"tokens": config.CHUNK_TOKENS, "overlap": config.CHUNK_OVERLAP,
            "mode": config.CHUNK_MODE, "min_tokens": config.MIN_CHUNK_TOKENS,
            "table_header_max_tokens": config.TABLE_HEADER_MAX_TOKENS,
            "chunker_version": config.CHUNKER_VERSION}


#------------------------------------------------------------------
# 인덱스의 청킹 설정과 지금 설정 비교
#=> 증분 인덱싱은 **바뀐 문서만** 새 설정으로 자르고 나머지는 옛 청크를 그대로 둔다.
#   청크 크기·겹침·방식이 다르면 한 인덱스에 두 종류가 섞이므로 증분을 거부한다.
#    1) --rebuild 거나 빈 인덱스면 지금 설정을 기록한다
#    2) 기록이 없는 옛 인덱스(§35 이전)면 경고만 하고 기록하지 않는다 — 어떤 설정으로
#       만들었는지 모르므로 "지금 설정으로 만들었다"고 적으면 거짓이 된다
#    3) 기록이 있고 다르면 RuntimeError — 무엇이 다른지와 --rebuild 를 알려 준다
#
# -in: state   = load_state() 결과(수정됨: chunk_params 기록)
# -in: rebuild = 전체 재구축 여부
# -in: log     = 안내 출력 함수
#
# -out: 지금 설정의 chunk_params dict
# -out: error = 기록된 설정과 다르면 RuntimeError
#------------------------------------------------------------------
def check_chunk_params(state, rebuild, log):
    params = chunk_params()
    recorded = state.get("chunk_params")
    if rebuild or not state.get("docs"):
        state["chunk_params"] = params
        return params
    if recorded is None:
        log("  ⚠️ 이 인덱스에는 청킹 설정 기록이 없습니다(이전 버전에서 만듦). 지금 설정"
            "({}토큰/겹침 {}/{}/머리글 상한 {})과 다르게 만들어졌다면 --rebuild 하세요.".format(
                params["tokens"], params["overlap"], params["mode"],
                params["table_header_max_tokens"]))
        return params
    if recorded != params:
        diff = ", ".join("{} {}→{}".format(k, recorded.get(k), v)
                         for k, v in params.items() if recorded.get(k) != v)
        raise RuntimeError(
            "청킹 설정이 인덱스와 다릅니다({}). 이대로 증분 인덱싱하면 옛 청크와 섞입니다 — "
            "index --dir <폴더> --rebuild 로 전체 재구축하세요.".format(diff))
    return params


#------------------------------------------------------------------
# 진행률 표시 (이동평균 ETA)
#=> 처리량이 발열로 계속 떨어지므로 전체 평균으로 ETA 를 내면 실제보다
#   낙관적인 값이 나온다. 최근 구간 속도로 계산해야 실제와 맞는다.
#------------------------------------------------------------------
class Progress:
    def __init__(self, total, window=20):
        self.total = total
        self.window = window
        self.samples = []          # (시각, 누적개수)
        self.t0 = time.perf_counter()

    def update(self, done):
        now = time.perf_counter()
        self.samples.append((now, done))
        if len(self.samples) > self.window:
            self.samples.pop(0)

        # 최근 구간 속도 — 표본이 하나뿐이면 전체 평균으로 대체한다.
        if len(self.samples) >= 2:
            (t_a, d_a), (t_b, d_b) = self.samples[0], self.samples[-1]
            rate = (d_b - d_a) / max(t_b - t_a, 1e-9)
        else:
            rate = done / max(now - self.t0, 1e-9)

        eta = (self.total - done) / rate if rate > 0 else 0
        return rate, eta


#------------------------------------------------------------------
# 폴더 인덱싱 (핵심)
#=> 변경된 문서만 골라 추출·청킹·임베딩하고 Qdrant 에 넣는다. 문서 단위로
#   체크포인트를 남겨 중단 시 이어서 할 수 있다.
#    1) 대상 파일 나열 → 지문 비교로 변경분만 선별
#    2) 문서마다: 추출 → 청킹 → 임베딩 → 기존 청크 삭제 → upsert → 상태 저장
#    3) 전부 끝나면 BM25 인덱스를 새로 만든다(전체 코퍼스가 필요하므로 마지막에)
#
# -in: doc_dir  = 대상 폴더
# -in: embedder = OnnxEmbedder
# -in: store    = VectorStore
# -in: bm25     = Bm25Index
# -in: rebuild  = True 면 상태를 무시하고 전부 다시
# -in: on_log   = 진행 메시지 콜백 fn(str)
#
# -out: dict = 처리 요약
#------------------------------------------------------------------
def index_folder(doc_dir, embedder, store, bm25, rebuild=False, on_log=None,
                 recursive=True):
    log = on_log or (lambda m: None)

    if not os.path.isdir(doc_dir):
        raise RuntimeError("폴더 없음: " + doc_dir)

    ext = build_extractor()
    embedder.ensure_loaded()
    tok = embedder.tokenizer          # 절단 해제된 토크나이저(결정12)

    state = {"docs": {}, "next_id": 0} if rebuild else load_state()
    # 청킹 설정이 이 인덱스를 만들 때와 다르면 증분으로 섞지 않는다(§35) — 저장소를 건드리기 전에
    check_chunk_params(state, rebuild, log)
    store.ensure_collection(recreate=rebuild)

    files, skipped = list_files(doc_dir, recursive=recursive)
    if skipped:
        log("확장자 제외 {}건(이미지/압축 등)".format(skipped))

    todo = []
    for path in files:
        fp = file_fingerprint(path)
        prev = state["docs"].get(os.path.abspath(path))
        if prev and prev.get("fingerprint") == fp:
            continue                  # 안 바뀐 문서는 건너뛴다
        todo.append((path, fp))

    log("문서 {}건 중 {}건 처리 대상".format(len(files), len(todo)))
    if not todo:
        return {"files": len(files), "changed": 0, "chunks": 0, "sec": 0.0}

    t_start = time.perf_counter()
    prog = Progress(len(todo))
    total_chunks = 0
    failed = []

    for n, (path, fp) in enumerate(todo, 1):
        name = os.path.basename(path)

        # 문서 1건의 실패가 전체 실행을 죽이면 안 된다. 371건 인덱싱에 19분이
        # 걸리는데, 중간의 깨진 파일 하나로 처음부터 다시 하게 되기 때문이다.
        # 추출뿐 아니라 청킹·임베딩까지 통째로 감싼다(실제로 토크나이저가
        # 서로게이트 문자에서 죽어 50번째 문서에서 전체가 중단된 적이 있다).
        try:
            text, n_rows = extract_text(ext, path)
            if config.CHUNK_MODE == "structured":
                # 파일명을 문서 제목으로 쓴다 — 청크 접두에 들어가 검색과
                # 생성 양쪽에 문맥을 준다(설계서 구조인식 §4.5).
                title = os.path.splitext(name)[0]
                chunks = split_structured(text, tok, title=title)
            else:
                chunks = split_text(text, tok)
        except Exception as e:
            failed.append((name, "{}: {}".format(type(e).__name__, str(e)[:80])))
            continue

        table_note = "  표{}행".format(n_rows) if n_rows else ""
        if not chunks:
            failed.append((name, "추출 텍스트 없음"))
            continue

        # 회귀 감지(결정12): 문자 수 대비 청크가 비정상적으로 적으면 경고한다.
        # 토크나이저 절단이 되살아나면 여기서 잡힌다.
        expect = max(1, len(text) // (config.CHUNK_TOKENS * 3))
        if len(chunks) < expect // 2:
            log("  ⚠️ {} — 청크 {}개는 문자수({:,}) 대비 비정상적으로 적습니다. "
                "토크나이저 절단 여부를 확인하세요.".format(name, len(chunks), len(text)))

        try:
            vecs = embedder.embed_passages(chunks)
        except Exception as e:
            failed.append((name, "임베딩 {}: {}".format(type(e).__name__, str(e)[:70])))
            continue

        key = os.path.abspath(path)
        if key in state["docs"]:
            store.delete_doc(key)      # 바뀐 문서의 옛 청크 제거

        start_id = state["next_id"]
        ids = list(range(start_id, start_id + len(chunks)))
        mtime = int(os.stat(path).st_mtime)
        folder = os.path.basename(os.path.dirname(path))
        payloads = [{"text": c, "doc_path": key, "doc_name": name,
                     "folder": folder,
                     "chunk_idx": i, "mtime": mtime}
                    for i, c in enumerate(chunks)]
        store.upsert(ids, vecs, payloads)

        state["next_id"] = start_id + len(chunks)
        state["docs"][key] = {"fingerprint": fp, "chunks": len(chunks),
                              "name": name, "folder": folder, "mtime": mtime}
        total_chunks += len(chunks)

        rate, eta = prog.update(n)
        log("  [{}/{}] {:<48} {:>4}청크  ({:.1f} 문서/s, 남은시간 {:.0f}s)".format(
            n, len(todo), name[:46], len(chunks), rate, eta) + table_note)

        save_state(state)              # 문서 단위 체크포인트

    # BM25 는 전체 코퍼스가 있어야 만들 수 있어 마지막에 한 번 구축한다.
    log("BM25 인덱스 구축 중...")
    all_chunks, all_ids = _collect_all(store)
    build_s = bm25.build(all_chunks, all_ids, tok) if all_chunks else 0.0
    log("BM25 인덱스 {:,}건 구축 {:.1f}s".format(len(all_chunks), build_s))

    for name, why in failed:
        log("  ❌ {} — {}".format(name, why))

    return {"files": len(files), "changed": len(todo), "chunks": total_chunks,
            "failed": failed, "sec": round(time.perf_counter() - t_start, 1)}


#------------------------------------------------------------------
# 저장소에서 전체 청크 회수 (BM25 재구축용)
#=> Qdrant local 의 scroll 로 payload 를 모두 읽어 온다.
#
# -out: (texts, ids)
#------------------------------------------------------------------
def _collect_all(store):
    from .store import COLLECTION

    store.ensure_collection()
    texts, ids = [], []
    offset = None
    while True:
        points, offset = store._client.scroll(
            collection_name=COLLECTION, limit=2000,
            offset=offset, with_payload=True, with_vectors=False)
        for p in points:
            texts.append(p.payload.get("text", ""))
            ids.append(p.id)
        if offset is None:
            break
    return texts, ids
