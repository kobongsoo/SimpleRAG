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
        if os.path.splitext(path)[1].lower() in TEXT_EXTS and not is_transient(path):
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
# 저장하는 동안 잠깐 생기는 파일인가 (자동 인덱싱 설계서 §4)
#=> Office 는 문서를 열면 "~$보고서.docx" 라는 표시 파일을 만든다. 확장자가 .docx 라
#   확장자 필터로는 걸러지지 않아, 그대로 두면 깨진 문서로 인덱싱을 시도하고 실패한다.
#    1) 이름이 "~$" (Office) 나 ".~lock." (LibreOffice) 으로 시작하면 제외
#    2) 숨김·시스템 속성 파일(Thumbs.db 등)도 제외 — Windows 에서만 속성을 볼 수 있다
#
# -in: path = 파일 경로
#
# -out: True = 인덱싱하지 않을 파일
# -out: error = 없음 (속성을 못 읽으면 이름만으로 판단)
#------------------------------------------------------------------
def is_transient(path):
    name = os.path.basename(path)
    if name.startswith("~$") or name.startswith(".~lock."):
        return True
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
        # FILE_ATTRIBUTE_HIDDEN(0x2) | FILE_ATTRIBUTE_SYSTEM(0x4)
        return bool(attrs & 0x6)
    except OSError:
        return False


#------------------------------------------------------------------
# 파일 내용 해시
#=> 지문(경로·시각·크기)이 바뀌어도 내용이 같으면 다시 임베딩할 까닭이 없다.
#   백업·동기화 도구가 날짜만 바꾸는 경우가 흔하다. 지문이 달라진 파일만 읽으므로
#   평소 비용은 없다.
#
# -in: path = 파일 경로
#
# -out: sha1 hex 문자열
# -out: error = 읽기 실패 시 OSError 전파
#------------------------------------------------------------------
def content_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        # 큰 파일도 메모리에 통째로 올리지 않게 1MB 씩 읽는다
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


#------------------------------------------------------------------
# 경로 비교용 표기
#=> Windows 경로는 대소문자를 가리지 않는다. 상태 파일의 키는 인덱싱 당시 표기
#   그대로라, 비교할 때만 한 가지 표기로 맞춘다(키 자체는 바꾸지 않는다).
#
# -in: path = 경로
#
# -out: 비교용 문자열
# -out: error = 없음
#------------------------------------------------------------------
def norm_path(path):
    return os.path.normcase(os.path.abspath(path))


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
# 문서 한 건을 인덱스에 반영 — 추가·수정 공통 (자동 인덱싱 설계서 §5)
#=> 수정 문서는 "수정 전 청크를 지우고 수정 후 청크를 넣는다". 다만 그 순서대로 하면
#   사이에 문서가 비고, 넣기가 실패하면 문서가 사라진다. 그래서 순서를 뒤집는다.
#    1) 내용 해시가 전과 같으면 지문만 새로 적고 끝(다시 임베딩하지 않는다)
#    2) 새 버전을 추출·청킹·임베딩한다 — 인덱스는 아직 건드리지 않는다.
#       여기서 실패하면 옛 버전을 그대로 두고, 같은 지문으로는 다시 시도하지 않게 적어 둔다
#    3) 새 점 id 를 예약하고 상태 파일에 먼저 적는다(도중에 죽어도 id 가 겹치지 않게)
#    4) 새 청크를 넣는다
#    5) 이 문서의 청크 가운데 "방금 넣은 것 말고" 전부 지운다 — 옛 버전과
#       예전에 넣다 만 찌꺼기가 한 번에 정리된다
#    6) 상태 파일을 새 버전으로 바꾼다
#   어느 단계에서 끊겨도 상태 파일의 지문이 옛것이므로 다음 실행이 다시 처리해 바로잡는다.
#
# -in: path     = 문서 경로
# -in: fp       = file_fingerprint(path) 결과
# -in: state    = load_state() 결과(수정됨)
# -in: embedder = OnnxEmbedder (ensure_loaded 된 것)
# -in: store    = VectorStore
# -in: ext      = build_extractor() 결과
# -in: log      = 안내 출력 함수 fn(str)
#
# -out: dict = {ok, op("added"|"modified"|"same"), chunks, why, kept_old, n_rows}
# -out: error = 추출·임베딩 실패는 ok=False 로 돌려준다. 저장소 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def sync_doc(path, fp, state, embedder, store, ext, log=None):
    log = log or (lambda m: None)
    key = os.path.abspath(path)
    name = os.path.basename(path)
    prev = state["docs"].get(key)
    op = "modified" if prev else "added"

    # ── 1) 내용이 같은가 ─────────────────────────────
    try:
        chash = content_hash(path)
    except OSError as e:
        return {"ok": False, "op": op, "chunks": 0, "kept_old": bool(prev),
                "why": "읽기 실패 {}: {}".format(type(e).__name__, str(e)[:70])}
    if prev and prev.get("content_hash") == chash:
        prev["fingerprint"] = fp
        prev["mtime"] = int(os.stat(path).st_mtime)
        state.get("failed", {}).pop(key, None)
        save_state(state)
        return {"ok": True, "op": "same", "chunks": prev.get("chunks", 0)}

    # ── 2) 새 버전 만들기 (인덱스는 아직 그대로) ──────
    # 문서 1건의 실패가 전체 실행을 죽이면 안 된다. 추출뿐 아니라 청킹·임베딩까지
    # 통째로 감싼다(토크나이저가 서로게이트 문자에서 죽어 전체가 멈춘 적이 있다).
    n_rows = 0
    try:
        text, n_rows = extract_text(ext, path)
        if config.CHUNK_MODE == "structured":
            # 파일명을 문서 제목으로 쓴다 — 청크 접두에 들어가 검색과
            # 생성 양쪽에 문맥을 준다(설계서 구조인식 §4.5).
            chunks = split_structured(text, embedder.tokenizer,
                                      title=os.path.splitext(name)[0])
        else:
            chunks = split_text(text, embedder.tokenizer)
        if not chunks:
            raise ValueError("추출 텍스트 없음")
        vecs = embedder.embed_passages(chunks)
    except Exception as e:
        why = "{}: {}".format(type(e).__name__, str(e)[:80])
        # 같은 지문으로는 다시 시도하지 않게 적어 둔다(자동 인덱싱이 저장할 때마다
        # 같은 실패를 되풀이하지 않도록). 파일이 다시 바뀌면 지문이 달라져 다시 시도한다.
        state.setdefault("failed", {})[key] = {"fingerprint": fp, "why": why, "name": name}
        save_state(state)
        return {"ok": False, "op": op, "chunks": 0, "why": why, "kept_old": bool(prev)}

    # 회귀 감지(결정12): 문자 수 대비 청크가 비정상적으로 적으면 경고한다.
    # 토크나이저 절단이 되살아나면 여기서 잡힌다.
    expect = max(1, len(text) // (config.CHUNK_TOKENS * 3))
    if len(chunks) < expect // 2:
        log("  ⚠️ {} — 청크 {}개는 문자수({:,}) 대비 비정상적으로 적습니다. "
            "토크나이저 절단 여부를 확인하세요.".format(name, len(chunks), len(text)))

    # ── 3) id 예약 — 상태 파일에 먼저 적는다 ──────────
    start_id = state["next_id"]
    ids = list(range(start_id, start_id + len(chunks)))
    state["next_id"] = start_id + len(chunks)
    save_state(state)

    # ── 4) 새 청크 넣기 ──────────────────────────────
    mtime = int(os.stat(path).st_mtime)
    folder = os.path.basename(os.path.dirname(path))
    payloads = [{"text": c, "doc_path": key, "doc_name": name, "folder": folder,
                 "chunk_idx": i, "mtime": mtime,
                 # 어느 버전 문서에서 나온 청크인지(진단·고아 청소용)
                 "gen": fp, "content_hash": chash}
                for i, c in enumerate(chunks)]
    store.upsert(ids, vecs, payloads)

    # ── 5) 방금 넣은 것 말고 이 문서의 청크를 모두 지운다 ──
    # 새 문서여도 부른다 — 예전에 넣다가 끊겨 남은 찌꺼기가 있을 수 있다.
    store.delete_doc_except(key, ids)

    # ── 6) 상태 파일을 새 버전으로 ────────────────────
    state["docs"][key] = {"fingerprint": fp, "content_hash": chash,
                          "chunks": len(chunks), "ids": [start_id, start_id + len(chunks)],
                          "name": name, "folder": folder, "mtime": mtime}
    state.get("failed", {}).pop(key, None)
    mark_changed(state)
    save_state(state)
    return {"ok": True, "op": op, "chunks": len(chunks), "n_rows": n_rows}


#------------------------------------------------------------------
# 문서 한 건을 인덱스에서 빼기 (파일이 지워졌을 때)
#=> 그 문서의 점을 모두 지우고 상태 파일에서도 뺀다. BM25 는 나중에 한꺼번에 다시 만든다.
#   ⚠️ 지워도 되는지(폴더가 보이는지, 한꺼번에 너무 많지 않은지)는 부르는 쪽이 먼저 확인한다
#      — delete_guard().
#
# -in: key   = 상태 파일의 문서 키(인덱싱 당시 절대경로)
# -in: state = load_state() 결과(수정됨)
# -in: store = VectorStore
#
# -out: 지운 문서의 청크 수(상태 파일 기준)
# -out: error = 저장소 쓰기 실패 시 예외 전파
#------------------------------------------------------------------
def remove_doc(key, state, store):
    meta = state["docs"].get(key) or {}
    store.delete_doc(key)
    state["docs"].pop(key, None)
    state.get("failed", {}).pop(key, None)
    mark_changed(state)
    save_state(state)
    return meta.get("chunks", 0)


#------------------------------------------------------------------
# 인덱스가 바뀌었다고 적기
#=> BM25 를 다시 만들어야 하는지 판단하는 데 쓴다. 벡터는 바꿨는데 BM25 를 만들기 전에
#   끊기면, 다음 실행에서 "BM25 가 인덱스보다 오래됐다" 를 알아채 다시 만든다.
#
# -in: state = 상태 dict(수정됨)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def mark_changed(state):
    state["index_changed_at"] = time.time()


#------------------------------------------------------------------
# BM25 를 다시 만들어야 하는가
#
# -in: state = 상태 dict
#
# -out: True = 인덱스가 BM25 보다 나중에 바뀌었다
# -out: error = 없음
#------------------------------------------------------------------
def bm25_stale(state):
    changed = state.get("index_changed_at")
    built = state.get("bm25_built_at")
    return bool(changed) and (not built or built < changed)


#------------------------------------------------------------------
# 디스크에서 사라진 문서 찾기 (자동 인덱싱 설계서 §6)
#=> 상태 파일에는 있는데 폴더에는 없는 문서를 고른다. 지금까지는 이 단계가 없어서
#   지운 문서가 인덱스에 남아 계속 근거로 나왔다.
#    1) 이 폴더 아래 문서만 본다 — 다른 폴더에서 인덱싱한 문서는 절대 건드리지 않는다
#    2) 하위 폴더를 안 보는 실행(--no-recursive)이면 바로 아래 문서만 본다
#    3) 이번에 나열한 파일에 없고, 실제로도 없는 것만 고른다(나열 뒤 새로 생긴 경우 제외)
#
# -in: state     = 상태 dict
# -in: root      = 이번에 인덱싱하는 폴더
# -in: present   = 이번에 나열한 파일들의 norm_path 집합
# -in: recursive = 하위 폴더까지 보았는지
#
# -out: 사라진 문서 키 목록
# -out: error = 없음
#------------------------------------------------------------------
def find_removed(state, root, present, recursive=True):
    base = norm_path(root)
    prefix = base.rstrip("\\/") + os.sep
    out = []
    for key in state["docs"]:
        k = norm_path(key)
        if recursive:
            if not k.startswith(prefix):
                continue
        elif os.path.dirname(k) != base:
            continue
        if k in present or os.path.exists(key):
            continue
        out.append(key)
    return sorted(out)


#------------------------------------------------------------------
# 지워도 안전한가 (자동 인덱싱 설계서 §6)
#=> 드라이브가 빠지거나 네트워크가 끊기면 "파일이 전부 사라졌다" 로 보인다.
#   그대로 믿고 지우면 인덱스가 통째로 날아간다.
#    1) 폴더 자체가 안 보이면 지우지 않는다(index_folder 가 먼저 막는다)
#    2) 한꺼번에 많이 사라졌으면 지우지 않고 멈춘다 — 사람이 --allow-delete 로 확인해야 한다
#       · limit_n 건 이상 사라졌거나
#       · 그 폴더 문서의 limit_pct % 이상이 사라졌을 때(단, 5건 미만이면 비율은 보지 않는다
#         — 문서 3건짜리 폴더에서 1건 지웠다고 매번 막으면 쓸 수가 없다)
#
# -in: n_removed = 사라진 문서 수
# -in: n_known   = 그 폴더에서 상태 파일이 알던 문서 수
# -in: limit_pct = 비율 한도(%)
# -in: limit_n   = 건수 한도
#
# -out: (ok, why) — ok=False 면 지우지 않는다
# -out: error = 없음
#------------------------------------------------------------------
def delete_guard(n_removed, n_known, limit_pct=30, limit_n=50):
    if n_removed <= 0:
        return True, ""
    if n_removed >= limit_n:
        return False, "{}건이 한꺼번에 사라졌습니다(한도 {}건)".format(n_removed, limit_n)
    if n_removed >= 5 and n_known and n_removed * 100 >= limit_pct * n_known:
        return False, "문서 {}건 중 {}건({:.0f}%)이 한꺼번에 사라졌습니다(한도 {}%)".format(
            n_known, n_removed, n_removed * 100.0 / n_known, limit_pct)
    return True, ""


#------------------------------------------------------------------
# 폴더와 상태 파일 대조 — 무엇을 추가·수정·삭제할지 (자동 인덱싱 설계서 §4·§6)
#=> 인덱스는 건드리지 않고 "할 일 목록" 만 만든다. index 명령과 워커의 /index-plan
#   명령이 같은 판단을 쓰도록 한곳에 둔다(두 벌이면 언젠가 어긋난다).
#    1) 폴더를 훑어 지문이 상태 파일과 다른 문서 → 추가·수정 후보
#       (지난번에 실패했고 그 뒤로 안 바뀐 문서는 뺀다)
#    2) 상태 파일에는 있는데 폴더에 없는 문서 → 삭제 후보
#    3) 삭제 후보가 너무 많으면 비우고 이유를 적는다(allow_delete 면 그대로)
#
# -in: doc_dir      = 대상 폴더 (있는지는 부르는 쪽이 먼저 확인한다)
# -in: state        = 상태 dict
# -in: recursive    = 하위 폴더까지
# -in: allow_delete = True 면 대량 삭제 안전 확인을 건너뛴다
# -in: rebuild      = True 면 삭제 후보를 만들지 않는다(어차피 전부 새로 만든다)
#
# -out: dict = {files, skipped, todo:[(path, fp)], removed:[key], delete_blocked, retry_skip}
# -out: error = 폴더를 못 읽으면 OSError 전파
#------------------------------------------------------------------
def plan_changes(doc_dir, state, recursive=True, allow_delete=False, rebuild=False):
    files, skipped = list_files(doc_dir, recursive=recursive)

    # ── 추가·수정 후보 ───────────────────────────────
    failed_before = state.get("failed", {})
    todo, n_retry_skip = [], 0
    for path in files:
        fp = file_fingerprint(path)
        key = os.path.abspath(path)
        prev = state["docs"].get(key)
        if prev and prev.get("fingerprint") == fp:
            continue                  # 안 바뀐 문서는 건너뛴다
        f = failed_before.get(key)
        if f and f.get("fingerprint") == fp:
            n_retry_skip += 1         # 지난번에 실패했고 그 뒤로 안 바뀌었다
            continue
        todo.append((path, fp))

    # ── 삭제 후보 ────────────────────────────────────
    present = {norm_path(p) for p in files}
    removed = [] if rebuild else find_removed(state, doc_dir, present, recursive)
    base = norm_path(doc_dir).rstrip("\\/") + os.sep
    n_known = sum(1 for k in state["docs"] if norm_path(k).startswith(base))
    delete_blocked = ""
    if removed and not allow_delete:
        ok, why = delete_guard(len(removed), n_known)
        if not ok:
            delete_blocked = why
            removed = []

    return {"files": files, "skipped": skipped, "todo": todo, "removed": removed,
            "delete_blocked": delete_blocked, "retry_skip": n_retry_skip}


#------------------------------------------------------------------
# 폴더 인덱싱 (핵심)
#=> 폴더와 상태 파일을 대조해 추가·수정·삭제를 반영한다. 문서 단위로
#   체크포인트를 남겨 중단 시 이어서 할 수 있다.
#    1) 대상 파일 나열 → 지문 비교로 추가·수정 후보, 상태 파일 대조로 삭제 후보
#    2) 삭제 후보가 너무 많으면 지우지 않는다(allow_delete 로만 풀린다)
#    3) 추가·수정: sync_doc (새것 넣고 옛것 지우기) / 삭제: remove_doc
#    4) 바뀐 게 있으면(또는 지난번에 BM25 를 못 만들었으면) BM25 를 새로 만든다.
#       이때 상태 파일에 없는 고아 점도 청소한다
#
# -in: doc_dir      = 대상 폴더
# -in: embedder     = OnnxEmbedder
# -in: store        = VectorStore
# -in: bm25         = Bm25Index
# -in: rebuild      = True 면 상태를 무시하고 전부 다시
# -in: on_log       = 진행 메시지 콜백 fn(str)
# -in: recursive    = 하위 폴더까지 (기본 True)
# -in: allow_delete = True 면 대량 삭제 안전 확인을 건너뛴다(사람이 확인했을 때만)
# -in: max_add      = 새 문서가 이보다 많으면 새 문서는 넣지 않고 미룬다(수정·삭제는 한다).
#                     None 이면 제한 없음. 자동 인덱싱이 처음 켠 폴더를 묻지 않고 통째로
#                     인덱싱하지 않게 한다(설계서 §8 — 371건에 19분 걸렸다)
#
# -out: dict = 처리 요약 {files, changed, added, modified, same, removed,
#                         delete_blocked, held_new, chunks, failed, sec}
# -out: error = 폴더 없음·청킹 설정 불일치는 RuntimeError
#------------------------------------------------------------------
def index_folder(doc_dir, embedder, store, bm25, rebuild=False, on_log=None,
                 recursive=True, allow_delete=False, max_add=None):
    log = on_log or (lambda m: None)

    # ⚠️ 폴더가 안 보이면 여기서 멈춘다 — 계속하면 "전부 사라졌다" 로 보여 다 지운다
    if not os.path.isdir(doc_dir):
        raise RuntimeError("폴더 없음: " + doc_dir)

    ext = build_extractor()
    embedder.ensure_loaded()

    state = {"docs": {}, "next_id": 0} if rebuild else load_state()
    # 청킹 설정이 이 인덱스를 만들 때와 다르면 증분으로 섞지 않는다(§35) — 저장소를 건드리기 전에
    check_chunk_params(state, rebuild, log)
    store.ensure_collection(recreate=rebuild)

    plan = plan_changes(doc_dir, state, recursive=recursive, allow_delete=allow_delete,
                        rebuild=rebuild)
    files, todo, removed = plan["files"], plan["todo"], plan["removed"]
    delete_blocked = plan["delete_blocked"]
    if plan["skipped"]:
        log("제외 {}건(확장자·임시 파일 등)".format(plan["skipped"]))
    if delete_blocked:
        log("  ⚠️ {} — 지우지 않았습니다. 정말 지운 것이면 --allow-delete 로 다시 실행하세요."
            .format(delete_blocked))

    # 새 문서가 너무 많으면 이번에는 넣지 않는다 — 사람이 "지금 인덱싱" 으로 허락해야 한다
    held_new = 0
    if max_add is not None:
        adds = [t for t in todo if os.path.abspath(t[0]) not in state["docs"]]
        if len(adds) > max_add:
            held_new = len(adds)
            todo = [t for t in todo if os.path.abspath(t[0]) in state["docs"]]
            log("  새 문서 {}건은 한도({}건)를 넘어 이번에는 넣지 않았습니다".format(held_new, max_add))

    log("문서 {}건 중 {}건 처리 대상, 사라진 문서 {}건".format(len(files), len(todo), len(removed)))
    if plan["retry_skip"]:
        log("  지난번 실패 뒤 바뀌지 않아 건너뛴 문서 {}건".format(plan["retry_skip"]))

    t_start = time.perf_counter()
    prog = Progress(max(1, len(todo)))
    counts = {"added": 0, "modified": 0, "same": 0}
    total_chunks = 0
    failed = []

    # ── 삭제 ─────────────────────────────────────────
    n_removed = 0
    for key in removed:
        n = remove_doc(key, state, store)
        n_removed += 1
        log("  [삭제] {:<48} {:>4}청크".format(os.path.basename(key)[:46], n))

    # ── 추가·수정 ────────────────────────────────────
    for n, (path, fp) in enumerate(todo, 1):
        name = os.path.basename(path)
        r = sync_doc(path, fp, state, embedder, store, ext, log)
        if not r["ok"]:
            failed.append((name, r["why"] + (" (옛 버전 유지)" if r.get("kept_old") else "")))
            continue
        counts[r["op"]] += 1
        if r["op"] == "same":
            log("  [{}/{}] {:<48} 내용 같음 — 다시 임베딩하지 않음".format(n, len(todo), name[:46]))
            continue
        total_chunks += r["chunks"]
        rate, eta = prog.update(n)
        label = "추가" if r["op"] == "added" else "수정"
        table_note = "  표{}행".format(r["n_rows"]) if r.get("n_rows") else ""
        log("  [{}/{}] [{}] {:<44} {:>4}청크  ({:.1f} 문서/s, 남은시간 {:.0f}s)".format(
            n, len(todo), label, name[:42], r["chunks"], rate, eta) + table_note)

    # ── BM25 ─────────────────────────────────────────
    # 전체 코퍼스가 있어야 만들 수 있어 마지막에 한 번 구축한다. 지난번에 벡터만 바꾸고
    # BM25 를 못 만든 채 끝났으면(bm25_stale) 이번에 바뀐 게 없어도 만든다.
    if counts["added"] or counts["modified"] or n_removed or rebuild or bm25_stale(state):
        log("BM25 인덱스 구축 중...")
        all_chunks, all_ids, n_orphans = collect_for_bm25(store, state, log)
        if n_orphans:
            log("  고아 청크 {}개를 지웠습니다(중간에 끊겼던 작업의 찌꺼기)".format(n_orphans))
        build_s = bm25.build(all_chunks, all_ids, embedder.tokenizer) if all_chunks else 0.0
        state["bm25_built_at"] = time.time()
        save_state(state)
        log("BM25 인덱스 {:,}건 구축 {:.1f}s".format(len(all_chunks), build_s))

    for name, why in failed:
        log("  ❌ {} — {}".format(name, why))

    return {"files": len(files), "changed": len(todo) + n_removed,
            "added": counts["added"], "modified": counts["modified"], "same": counts["same"],
            "removed": n_removed, "delete_blocked": delete_blocked, "held_new": held_new,
            "chunks": total_chunks, "failed": failed,
            "sec": round(time.perf_counter() - t_start, 1)}


#------------------------------------------------------------------
# 저장소에서 전체 청크 회수 (BM25 재구축용) + 고아 청소
#=> Qdrant local 의 scroll 로 payload 를 모두 읽어 온다. 읽는 김에 상태 파일과 맞지 않는
#   점(고아)을 골라 지우고 BM25 에서도 뺀다.
#    - 고아 = 상태 파일에 없는 문서의 점, 또는 상태 파일이 적어 둔 id 범위 밖의 점
#      (sync_doc 이 넣고 지우기 사이에서 끊겼을 때 남는다)
#    ⚠️ 상태 파일이 깨졌거나 비었으면 모든 점이 고아로 보인다. 그럴 때 지우면 인덱스가
#       통째로 날아가므로, 고아가 전체의 30% 를 넘으면 지우지 않고 경고만 한다.
#
# -in: store = VectorStore
# -in: state = 상태 dict (None 이면 청소하지 않는다)
# -in: log   = 안내 출력 함수
#
# -out: (texts, ids, 지운 고아 수)
# -out: error = qdrant 예외 전파
#------------------------------------------------------------------
def collect_for_bm25(store, state=None, log=None):
    from .store import COLLECTION

    log = log or (lambda m: None)
    store.ensure_collection()
    points = []
    offset = None
    while True:
        batch, offset = store._client.scroll(
            collection_name=COLLECTION, limit=2000,
            offset=offset, with_payload=True, with_vectors=False)
        points.extend(batch)
        if offset is None:
            break

    orphans = set()
    if state is not None and state.get("docs"):
        docs = state["docs"]
        for p in points:
            meta = docs.get(p.payload.get("doc_path"))
            if meta is None:
                orphans.add(p.id)
            else:
                rng = meta.get("ids")          # 옛 상태 파일에는 없다 — 그러면 범위는 안 본다
                if rng and not (rng[0] <= p.id < rng[1]):
                    orphans.add(p.id)
        if orphans and len(orphans) * 100 > 30 * len(points):
            log("  ⚠️ 상태 파일과 맞지 않는 청크가 {}/{}개입니다. 상태 파일이 깨졌을 수 있어 "
                "지우지 않았습니다 — `status` 로 확인하세요.".format(len(orphans), len(points)))
            orphans = set()
        elif orphans:
            store.delete_ids(sorted(orphans))

    texts, ids = [], []
    for p in points:
        if p.id in orphans:
            continue
        texts.append(p.payload.get("text", ""))
        ids.append(p.id)
    return texts, ids, len(orphans)


#------------------------------------------------------------------
# 저장소에서 전체 청크 회수 (옛 이름 — 실험 스크립트 호환)
#=> exp6_kiwi 등이 `texts, ids = _collect_all(store)` 로 부른다. 청소는 하지 않는다.
#
# -in: store = VectorStore
#
# -out: (texts, ids)
# -out: error = qdrant 예외 전파
#------------------------------------------------------------------
def _collect_all(store):
    texts, ids, _ = collect_for_bm25(store)
    return texts, ids
