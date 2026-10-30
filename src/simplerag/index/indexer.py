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
#
#   인덱싱 속도 개선 (설계서 2026-10-30)
#    - BM25 는 바뀐 청크만 고친다(덧붙임 · 지움 — §4). 청크별 단어 빈도를 토큰 표에 남긴다
#    - 수정 문서는 글이 그대로인 청크의 벡터를 다시 쓴다(청크 해시 — §4-5)
#    - 다음 문서의 파싱·청킹을 준비 스레드가 임베딩과 겹쳐 한다(§3-1)
#    - 새로 임베딩할 청크가 많으면 벡터를 비워 두고 BM25 로 먼저 검색되게 한다(단계형 — §3-5)
#------------------------------------------------------------------

import hashlib
import json
import os
import queue
import sys
import threading
import time

from .. import config
import numpy as np

from . import bm25 as bm25_mod
from . import store as store_mod
from ..chunk import split_structured, split_text
from ..extract import build_extractor, extract_text
from ..meta import hook as meta_hook      # 문서 메타데이터(meta.sqlite)를 색인과 함께 고친다


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
#       (+ blocks 엔진이면 extract_engine · overflow_ratio · sentence_split · hard_max · blocks_version)
# -out: error = 없음
#------------------------------------------------------------------
def chunk_params():
    params = {"tokens": config.CHUNK_TOKENS, "overlap": config.CHUNK_OVERLAP,
              "mode": config.CHUNK_MODE, "min_tokens": config.MIN_CHUNK_TOKENS,
              "table_header_max_tokens": config.TABLE_HEADER_MAX_TOKENS,
              "chunker_version": config.CHUNKER_VERSION}
    # blocks 엔진일 때만 키를 더한다 — legacy 인덱스의 기록은 예전과 똑같아야
    # 이 변경만으로 기존 인덱스가 '설정이 다르다'며 재구축을 요구하지 않는다
    if config.EXTRACT_ENGINE == "blocks":
        params.update({"extract_engine": "blocks",
                       "overflow_ratio": config.CHUNK_OVERFLOW_RATIO,
                       "sentence_split": config.CHUNK_SENTENCE_SPLIT,
                       "hard_max": config.CHUNK_HARD_MAX,
                       "blocks_version": BLOCKS_VERSION})
        if config.PDF_INFER_HEADINGS:
            params["pdf_headings"] = True
    return params


# 블록 추출·청킹 결과가 달라지는 코드 변경마다 올린다(CHUNKER_VERSION 의 blocks 엔진판).
#   1 = 재설계서 Phase 1 (docx·pptx·xlsx·hwpx·html·md·ipynb 구조 파서)
#   2 = XLSX 옆으로 붙은 표 나누기 + 표 머리글 단위가 조각 끝에 홀로 남지 않게
#   3 = 재설계서 Phase 2 — DOC(바이너리 PAPX·스타일) · PDF(쪽 경계·머리말·쪽번호) 블록 경로
#   4 = DOC 표의 눕힌 행 펼치기(기존 normalize_tables 규칙)
#   5 = D:\분류함 전수 점검 반영 — HTML <head>·<svg> 건너뛰기 제거(<title> 살림), XLSX 행 상한 해제
#   (P-2 글자 크기 제목 추정은 extract.pdf_headings 로 켤 때만 청크가 바뀐다 — 켜면 설정 기록에
#    pdf_headings 키가 들어가 재인덱싱을 요구한다. 꺼 두면 청크가 5 와 같아 버전을 올리지 않는다)
BLOCKS_VERSION = 5


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
# blocks 엔진으로 문서 하나 추출·청킹 (재설계서 §13)
#=> 구조화 추출기(docmodel)로 블록을 만들고 split_blocks 로 청킹한다. 구조 파서가 없는
#   포맷·파서 실패는 docmodel 안에서 기존 추출기(extract_text)로 폴백하고, 그 경우 청킹도
#   split_structured 를 그대로 타서 legacy 엔진과 같은 청크가 나온다.
#
# -in: path     = 문서 경로
# -in: name     = 파일 이름
# -in: ext      = build_extractor() 결과(legacy 폴백용)
# -in: embedder = OnnxEmbedder (토크나이저)
#
# -out: (text, n_rows, metas) — 문서 전체 글자, 표 행 수, split_blocks 결과 목록
# -out: error = 폴백 추출까지 실패하면 예외 전파(상위가 실패로 기록)
#------------------------------------------------------------------
def _extract_blocks(path, name, ext, embedder):
    from ..chunk import split_blocks
    from ..docmodel import dump_json, extract_document

    # 파서가 없거나 실패하면 기존 추출기(extract_text)로 폴백한다
    doc = extract_document(path, legacy=lambda p: extract_text(ext, p),
                           title=os.path.splitext(name)[0])
    if config.EXTRACT_DUMP:
        try:
            dump_json(doc, os.path.join(config.ROOT, "blocks_dump"), name)
        except OSError:
            pass          # 진단 파일을 못 써도 인덱싱은 계속한다
    text = doc.full_text()
    n_rows = sum(len(b.rows or []) for b in doc.blocks if b.type == "table")
    if config.CHUNK_MODE == "structured":
        metas = split_blocks(doc, embedder.tokenizer)
    else:
        # 고정 크기 모드 — 블록 경계는 쓰지 않고 전체 글자를 자른다(되돌리기용)
        metas = [{"text": c} for c in split_text(text, embedder.tokenizer)]
    return text, n_rows, metas


#------------------------------------------------------------------
# 청크 해시 — 청크 글(접두 포함, 임베딩에 넣는 글 그대로)의 sha1 16진수
#=> 수정 문서에서 글이 그대로인 청크를 찾아 벡터를 다시 쓴다(인덱싱 속도 개선 설계서 §4-5).
#   운영 18.7만 청크 전체가 0.16초(청크당 약 0.9μs)라 인덱싱 시간에 보태지는 것이 사실상 없다.
#
# -in: text = 청크 글
#
# -out: 40글자 16진수
# -out: error = 없음
#------------------------------------------------------------------
def chunk_hash(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


# 추출·청킹을 한 번에 하나만 — 준비 스레드와 본 스레드가 같은 추출기를 동시에 쓰지 않게
_PARSE_LOCK = threading.Lock()


#------------------------------------------------------------------
# 문서 하나 추출·청킹 (sync_doc 2단계의 앞부분 — 준비 스레드도 이것을 부른다)
#=> blocks 엔진이면 블록 추출 → split_blocks, 아니면 글자 추출 → 구조/고정 청킹.
#
# -in: path     = 문서 경로
# -in: name     = 파일 이름
# -in: ext      = build_extractor() 결과
# -in: embedder = OnnxEmbedder (토크나이저)
#
# -out: (text, n_rows, chunks, metas) — 전체 글자, 표 행 수, 청크 글 목록, 청크 메타(없으면 None)
# -out: error = 추출 실패는 예외 전파, 청크가 없으면 ValueError
#------------------------------------------------------------------
def parse_doc(path, name, ext, embedder):
    with _PARSE_LOCK:
        n_rows, metas = 0, None
        if config.EXTRACT_ENGINE == "blocks":
            text, n_rows, metas = _extract_blocks(path, name, ext, embedder)
            chunks = [m["text"] for m in metas]
        else:
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
    return text, n_rows, chunks, metas


#------------------------------------------------------------------
# 수정 문서의 벡터 재사용 (인덱싱 속도 개선 설계서 §4-5)
#=> 다시 자른 청크 가운데 옛 버전과 글이 똑같은 청크는 옛 벡터를 그대로 쓴다.
#    1) 옛 청크마다 해시(payload chunk_hash, 없으면 본문으로 계산) → 점 id 목록
#    2) 새 청크 해시가 같으면 그 id 하나를 짝짓는다(같은 글이 여러 번이면 개수만큼만)
#    3) 짝지은 점의 벡터와 대기 표시를 꺼낸다(대기 중이던 청크는 계속 대기)
#
# -in: store   = VectorStore
# -in: old_ids = 옛 버전 점 id 목록
# -in: hashes  = 새 청크 해시 목록
#
# -out: 새 청크마다 None(새로 임베딩) 또는 (옛 벡터, 대기였나)
# -out: error = 저장소 읽기 실패는 예외 전파
#------------------------------------------------------------------
def _reuse_vectors(store, old_ids, hashes):
    out = [None] * len(hashes)
    if not old_ids:
        return out
    vecs, payloads = store.points_of(old_ids)
    if vecs is None:
        return out                       # 옛 점이 빠져 있다 — 모두 새로 임베딩
    by_hash = {}
    for i, p in enumerate(payloads):
        # chunk_hash 가 없는 옛 인덱스는 남아 있는 본문으로 그 자리에서 계산한다
        h = p.get("chunk_hash") or chunk_hash(p.get("text", ""))
        by_hash.setdefault(h, []).append(i)
    for j, h in enumerate(hashes):
        rows = by_hash.get(h)
        if rows:
            i = rows.pop(0)
            out[j] = (vecs[i], bool(payloads[i].get("vec_pending")))
    return out


#------------------------------------------------------------------
# 문서 한 건을 인덱스에 반영 — 추가·수정 공통 (자동 인덱싱 설계서 §5)
#=> 수정 문서는 "수정 전 청크를 지우고 수정 후 청크를 넣는다". 다만 그 순서대로 하면
#   사이에 문서가 비고, 넣기가 실패하면 문서가 사라진다. 그래서 순서를 뒤집는다.
#    1) 내용 해시가 전과 같으면 지문만 새로 적고 끝(다시 임베딩하지 않는다)
#       처음 보는 경로인데 같은 파일(내용·이름)이 다른 자리에서 사라졌으면 옮긴 것 —
#       move_doc 으로 경로만 바꾸고 끝(다시 임베딩하지 않는다).
#       원본이 그대로 있으면 붙여 넣은 복사본 — copy_doc 으로 원본 벡터를 베낀다(다시 임베딩 없음)
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
# -in: defer    = True 면 새로 임베딩할 청크의 벡터를 비워 둔다(단계형 인덱싱 1단계 — 자리 표시 벡터 +
#                 vec_pending 표시, 나중에 fill_pending 이 채운다). 기본 False
# -in: prepared = 준비 스레드 결과 {chash, parsed?}(None 이면 그 자리에서 해시 · 추출 · 청킹)
#
# -out: dict = {ok, op("added"|"modified"|"same"|"moved"|"copied"), chunks, why, kept_old, n_rows, from,
#               reused(옛 벡터를 다시 쓴 청크 수), embedded(새로 임베딩한 수), deferred(벡터 대기로 둔 수)}
# -out: error = 추출·임베딩 실패는 ok=False 로 돌려준다. 저장소 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def sync_doc(path, fp, state, embedder, store, ext, log=None, defer=False, prepared=None):
    log = log or (lambda m: None)
    key = os.path.abspath(path)
    name = os.path.basename(path)
    prev = state["docs"].get(key)
    op = "modified" if prev else "added"
    prepared = prepared or {}

    # ── 1) 내용이 같은가 ─────────────────────────────
    # 준비 스레드가 이미 구했으면 그것을 쓴다(읽기 실패도 그대로 넘어온다)
    try:
        chash = prepared["chash"] if "chash" in prepared else content_hash(path)
        if isinstance(chash, Exception):
            raise chash
    except OSError as e:
        return {"ok": False, "op": op, "chunks": 0, "kept_old": bool(prev),
                "why": "읽기 실패 {}: {}".format(type(e).__name__, str(e)[:70])}
    if prev and prev.get("content_hash") == chash:
        prev["fingerprint"] = fp
        prev["mtime"] = int(os.stat(path).st_mtime)
        state.get("failed", {}).pop(key, None)
        save_state(state)
        # 내용은 같아도 파일 시스템 날짜는 바뀌었다 — 메타는 다시 읽는다(수십 ms)
        meta_hook.synced(key, chash)
        return {"ok": True, "op": "same", "chunks": prev.get("chunks", 0)}
    # 처음 보는 경로인데 같은 파일이 다른 자리에서 사라졌다 — 옮긴 것이다(경로만 바꾼다)
    if not prev:
        old = moved_from(state, path, chash)
        if old:
            log("  [이동] {} → {}".format(old, key))
            return move_doc(old, path, fp, state, store)
        # 원본이 그대로 있는 같은 파일 — 붙여 넣은 복사본이다(원본 벡터를 베낀다)
        src = copied_from(state, path, chash)
        if src:
            r = copy_doc(src, path, fp, state, store)
            if r:
                log("  [복사] {} → {}".format(src, key))
                return r

    # ── 2) 새 버전 만들기 (인덱스는 아직 그대로) ──────
    # 문서 1건의 실패가 전체 실행을 죽이면 안 된다. 추출뿐 아니라 청킹·임베딩까지
    # 통째로 감싼다(토크나이저가 서로게이트 문자에서 죽어 전체가 멈춘 적이 있다).
    # 옛 버전의 점 id — 글이 같은 청크의 벡터를 다시 쓴다(설계서 §4-5)
    rng = (prev or {}).get("ids")
    old_ids = list(range(rng[0], rng[1])) if rng else []
    try:
        # 준비 스레드가 이미 만들었으면 그것을 쓴다(결과는 그 자리에서 만든 것과 같다)
        parsed = prepared.get("parsed")
        if parsed is None:
            parsed = parse_doc(path, name, ext, embedder)
        if isinstance(parsed, Exception):
            raise parsed
        text, n_rows, chunks, metas = parsed
        hashes = [chunk_hash(c) for c in chunks]
        reused = _reuse_vectors(store, old_ids, hashes)      # [None | (옛 벡터, 대기였나)]
        # 다시 쓸 벡터가 없는 청크만 임베딩한다 — 대기면 자리 표시 벡터로 둔다
        need = [c for c, r in zip(chunks, reused) if r is None]
        if defer:
            fresh = [store_mod.pending_vector(store.dim)] * len(need)
        else:
            fresh = list(embedder.embed_passages(need)) if need else []
        it = iter(fresh)
        vecs = np.asarray([r[0] if r is not None else next(it) for r in reused], dtype="float32")
        n_reused = len(chunks) - len(need)
    except Exception as e:
        why = "{}: {}".format(type(e).__name__, str(e)[:80])
        # 같은 지문으로는 다시 시도하지 않게 적어 둔다(자동 인덱싱이 저장할 때마다
        # 같은 실패를 되풀이하지 않도록). 파일이 다시 바뀌면 지문이 달라져 다시 시도한다.
        state.setdefault("failed", {})[key] = {"fingerprint": fp, "why": why, "name": name}
        save_state(state)
        # 본문을 못 뽑은 문서(스캔 PDF 등)도 이름 · 날짜 · 사람으로는 찾을 수 있게 메타는 남긴다
        meta_hook.synced(key, chash)
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
                 "gen": fp, "content_hash": chash,
                 # 청크 글의 해시 — 다음에 이 문서가 바뀌면 같은 글인 청크를 찾는 데 쓴다
                 "chunk_hash": h}
                for i, (c, h) in enumerate(zip(chunks, hashes))]
    if metas:
        # blocks 엔진 — 절 번호(Parent-Child 실험 A 에서 같은 절 청크를 모을 때 쓴다)·
        # 제목 경로·출처·쪽 범위를 함께 남긴다. 값이 없는 키는 넣지 않는다.
        for p, m in zip(payloads, metas):
            for k in ("section_id", "path", "source", "page_from", "page_to", "page"):
                if m.get(k) not in (None, []):
                    p[k] = m[k]
    # 벡터 대기 표시 — 새로 임베딩할 청크인데 미뤘으면(재사용 청크는 옛 표시를 그대로 이어받는다)
    for p, r in zip(payloads, reused):
        if (r is None and defer) or (r is not None and r[1]):
            p["vec_pending"] = True
    store.upsert(ids, vecs, payloads)
    # 청크별 단어 빈도도 함께 남긴다 — BM25 를 고칠 때 토큰화하지 않게(설계서 §4-3)
    bm25_mod.TfStore().put(ids, bm25_mod.term_freqs(chunks, embedder.tokenizer))

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
    meta_hook.synced(key, chash)
    n_new = len(chunks) - n_reused
    return {"ok": True, "op": op, "chunks": len(chunks), "n_rows": n_rows, "reused": n_reused,
            "embedded": 0 if defer else n_new, "deferred": n_new if defer else 0}


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
    meta_hook.removed(key)
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
    missing = [] if rebuild else find_removed(state, doc_dir, present, recursive)
    # 사라진 것 가운데 "같은 파일이 다른 자리에 있는 것"(하위 폴더로 옮김)은 지운 게 아니다.
    # 이동은 sync_doc 이 경로만 바꾸고, 이미 새 자리가 인덱스에 있으면 옛 자리는 중복이라 뺀다.
    # 둘 다 대량 삭제 안전 확인에 세지 않는다 — 폴더 정리로 많이 옮겨도 멈추지 않게.
    moved, dup = find_moves(state, missing, files)
    removed = [k for k in missing if k not in moved and k not in dup]
    base = norm_path(doc_dir).rstrip("\\/") + os.sep
    n_known = sum(1 for k in state["docs"] if norm_path(k).startswith(base))
    delete_blocked = ""
    if removed and not allow_delete:
        ok, why = delete_guard(len(removed), n_known)
        if not ok:
            delete_blocked = why
            removed = []
    removed = sorted(removed + dup)

    return {"files": files, "skipped": skipped, "todo": todo, "removed": removed,
            "moved": moved, "delete_blocked": delete_blocked, "retry_skip": n_retry_skip}


#------------------------------------------------------------------
# 옮긴 문서 찾기 (사라진 경로 ↔ 같은 파일이 있는 새 경로)
#=> 문서를 하위 폴더로 옮기면 대조 결과가 "옛 경로 사라짐 + 새 경로 추가" 로 나온다.
#   그대로 처리하면 같은 문서가 다시 임베딩되고, 사라진 것이 많으면 대량 삭제 안전 확인에
#   걸려 옛 경로는 지워지지 않아 같은 문서가 두 벌 남았다(사용자 화면에서 23건 확인).
#    1) 사라진 문서의 (내용 해시, 파일 이름) 을 모은다 — 내용 해시는 파일 바이트의 sha1
#    2) 지금 폴더의 파일 가운데 이름이 같은 것만 내용 해시를 구해 견준다(읽는 양을 줄인다)
#    3) 맞으면 이동. 새 경로가 이미 인덱스에 있으면(예전 버그로 두 벌이 됨) 옛 경로는 중복
#   이름까지 같아야 이동으로 본다 — 이름을 바꾸면 청크 앞에 붙은 제목이 달라져 다시 만든다.
#
# -in: state   = 상태 dict
# -in: missing = 사라진 문서 키 목록(find_removed 결과)
# -in: files   = 지금 폴더의 파일 경로 목록
#
# -out: (moved {옛 키: 새 경로}, dup [옛 키 — 새 자리가 이미 인덱스에 있음])
# -out: error = 없음 (읽지 못하는 파일은 이동으로 보지 않는다)
#------------------------------------------------------------------
def find_moves(state, missing, files):
    want = {}
    for key in missing:
        h = (state["docs"].get(key) or {}).get("content_hash")
        if h:
            want.setdefault((h, os.path.basename(key).lower()), []).append(key)
    if not want:
        return {}, []
    names = {name for _, name in want}
    indexed = {norm_path(k) for k in state["docs"]}
    moved, dup = {}, []
    for path in files:
        name = os.path.basename(path).lower()
        if name not in names:
            continue                       # 이름이 다르면 해시를 구할 필요도 없다
        try:
            h = content_hash(path)
        except OSError:
            continue
        olds = want.get((h, name))
        if not olds:
            continue
        old = olds.pop(0)
        if norm_path(path) in indexed:
            dup.append(old)
        else:
            moved[old] = path
    return moved, dup


#------------------------------------------------------------------
# 이 파일이 어디서 옮겨 왔는가 (sync_doc 이 새 문서를 넣기 전에 본다)
#=> 상태 파일에 있으나 지금은 없는 문서 가운데 내용 해시와 파일 이름이 같은 것.
#
# -in: state = 상태 dict
# -in: path  = 새 경로
# -in: chash = 새 파일의 내용 해시
#
# -out: 옛 키 또는 None
# -out: error = 없음
#------------------------------------------------------------------
def moved_from(state, path, chash):
    name = os.path.basename(path).lower()
    me = norm_path(path)
    for key, d in state["docs"].items():
        if (d.get("content_hash") == chash and os.path.basename(key).lower() == name
                and norm_path(key) != me and not os.path.exists(key)):
            return key
    return None


#------------------------------------------------------------------
# 이 파일이 어느 문서의 복사본인가 (sync_doc 이 새 문서를 넣기 전에 본다)
#=> 상태 파일에 있고 지금도 파일이 있는 문서 가운데 내용 해시와 파일 이름이 같은 것.
#   (원본이 사라졌으면 복사가 아니라 이동이다 — moved_from 이 먼저 본다)
#
# -in: state = 상태 dict
# -in: path  = 새 경로
# -in: chash = 새 파일의 내용 해시
#
# -out: 원본 키 또는 None
# -out: error = 없음
#------------------------------------------------------------------
def copied_from(state, path, chash):
    name = os.path.basename(path).lower()
    me = norm_path(path)
    for key, d in state["docs"].items():
        if (d.get("content_hash") == chash and os.path.basename(key).lower() == name
                and norm_path(key) != me and os.path.exists(key) and d.get("ids")):
            return key
    return None


#------------------------------------------------------------------
# 복사본 넣기 — 원본의 청크·벡터를 그대로 베낀다 (다시 임베딩하지 않는다)
#=> 같은 문서를 다른 폴더에 붙여 넣은 경우. 복사본도 인덱스에 따로 둔다:
#    - 그 폴더에서 폴더 한정 검색을 하면 찾아야 하고
#    - 원본을 지워도 복사본은 남아야 한다
#   검색 결과에서는 본문이 같은 청크를 하나로 합치므로(hybrid.dedup_key) 근거 자리를 두 번 쓰지 않는다.
#    1) 원본 점들의 벡터·payload 를 읽는다(빠졌으면 None — 부르는 쪽이 새로 만든다)
#    2) 새 id 를 예약해 상태 파일에 먼저 적는다(sync_doc 과 같은 순서)
#    3) 위치 정보(doc_path·folder·mtime·gen)만 바꿔 새 id 로 넣는다
#    4) 상태 파일에 복사본을 적는다. 새 점이 생겼으므로 BM25 는 다시 만든다(mark_changed)
#
# -in: src_key = 원본 키
# -in: path    = 복사본 경로
# -in: fp      = 복사본의 file_fingerprint
# -in: state   = 상태 dict (수정됨)
# -in: store   = VectorStore
#
# -out: {ok, op: "copied", chunks, from} 또는 None(원본 점을 못 읽음)
# -out: error = 저장소 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def copy_doc(src_key, path, fp, state, store):
    src = state["docs"][src_key]
    rng = src.get("ids") or [0, 0]
    vecs, payloads = store.points_of(list(range(rng[0], rng[1])))
    if vecs is None or not len(payloads):
        return None
    key = os.path.abspath(path)
    name = os.path.basename(path)
    folder = os.path.basename(os.path.dirname(path))
    mtime = int(os.stat(path).st_mtime)

    start_id = state["next_id"]
    ids = list(range(start_id, start_id + len(payloads)))
    state["next_id"] = start_id + len(payloads)
    save_state(state)

    for p in payloads:
        p.update(doc_path=key, doc_name=name, folder=folder, mtime=mtime, gen=fp)
    store.upsert(ids, vecs, payloads)
    # 원본의 토큰 표도 새 id 로 베낀다(원본이 벡터 대기면 사본도 대기 표시 그대로 — 나중에 함께 채워진다)
    tfs = bm25_mod.TfStore()
    src_ids = list(range(rng[0], rng[1]))
    got = tfs.get(src_ids)
    pairs = [(d, got[s]) for s, d in zip(src_ids, ids) if s in got]
    tfs.put([d for d, _ in pairs], [t for _, t in pairs])
    # 예전에 넣다 끊긴 찌꺼기가 있을 수 있다 — 방금 넣은 것만 남긴다(sync_doc 과 같다)
    store.delete_doc_except(key, ids)

    entry = dict(src)
    entry.update(fingerprint=fp, ids=[start_id, start_id + len(payloads)], name=name,
                 folder=folder, mtime=mtime)
    state["docs"][key] = entry
    state.get("failed", {}).pop(key, None)
    mark_changed(state)
    save_state(state)
    # 메타도 원본을 베끼고 경로 · 파일 시스템 정보만 새로 읽는다
    meta_hook.copied(src_key, key, entry.get("content_hash"))
    return {"ok": True, "op": "copied", "chunks": len(payloads), "from": src_key}


#------------------------------------------------------------------
# 옮긴 문서의 경로만 바꾸기 (다시 임베딩하지 않는다)
#=> 내용이 같으므로 청크·벡터는 그대로 두고 위치 정보만 새로 적는다.
#    1) 저장소의 그 문서 점들(id 범위)에 새 doc_path·folder 등을 덮어쓴다
#    2) 상태 파일의 키를 새 경로로 옮긴다
#   1 을 먼저 한다 — 도중에 끊겨도 상태 파일에는 옛 경로가 남아 다음 대조가 다시 이동으로
#   찾아 같은 일을 되풀이한다(id 로 고르므로 두 번 해도 같다).
#   청크·id 가 그대로라 BM25 는 다시 만들 필요가 없다.
#
# -in: old_key = 상태 파일의 옛 키
# -in: path    = 새 경로
# -in: fp      = 새 경로의 file_fingerprint
# -in: state   = 상태 dict (수정됨)
# -in: store   = VectorStore
#
# -out: {ok, op: "moved", chunks, from}
# -out: error = 저장소 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def move_doc(old_key, path, fp, state, store):
    key = os.path.abspath(path)
    name = os.path.basename(path)
    folder = os.path.basename(os.path.dirname(path))
    mtime = int(os.stat(path).st_mtime)
    entry = dict(state["docs"][old_key])
    rng = entry.get("ids") or [0, 0]
    store.set_payload_ids(list(range(rng[0], rng[1])),
                          {"doc_path": key, "doc_name": name, "folder": folder,
                           "mtime": mtime, "gen": fp})
    entry.update(fingerprint=fp, name=name, folder=folder, mtime=mtime)
    del state["docs"][old_key]
    state["docs"][key] = entry
    state.get("failed", {}).pop(key, None)
    save_state(state)
    meta_hook.moved(old_key, key)
    return {"ok": True, "op": "moved", "chunks": entry.get("chunks", 0), "from": old_key}


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
# -out: dict = 처리 요약 {files, changed, added, modified, same, moved, copied, removed,
#                         delete_blocked, held_new, chunks, failed, sec,
#                         pending(벡터 대기로 남은 청크 수), reused, embedded}
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
    if rebuild:
        # id 를 0 부터 다시 쓰므로 옛 토큰 표도 비운다
        bm25_mod.TfStore().clear()

    plan = plan_changes(doc_dir, state, recursive=recursive, allow_delete=allow_delete,
                        rebuild=rebuild)
    # 메타데이터의 폴더 토큰은 이 인덱싱 폴더 아래 경로로 잡는다
    meta_hook.set_base(doc_dir)
    files, todo, removed = plan["files"], plan["todo"], plan["removed"]
    delete_blocked = plan["delete_blocked"]
    if plan["skipped"]:
        log("제외 {}건(확장자·임시 파일 등)".format(plan["skipped"]))
    if delete_blocked:
        log("  ⚠️ {} — 지우지 않았습니다. 정말 지운 것이면 --allow-delete 로 다시 실행하세요."
            .format(delete_blocked))

    # 새 문서가 너무 많으면 이번에는 넣지 않는다 — 사람이 "지금 인덱싱" 으로 허락해야 한다
    held_new = 0
    # 옮긴 문서는 새 문서가 아니다(경로만 바꾼다) — 한도에 세지 않는다
    moved_to = {norm_path(p) for p in plan["moved"].values()}
    if max_add is not None:
        adds = [t for t in todo if os.path.abspath(t[0]) not in state["docs"]
                and norm_path(t[0]) not in moved_to]
        if len(adds) > max_add:
            held_new = len(adds)
            todo = [t for t in todo if os.path.abspath(t[0]) in state["docs"]
                    or norm_path(t[0]) in moved_to]
            log("  새 문서 {}건은 한도({}건)를 넘어 이번에는 넣지 않았습니다".format(held_new, max_add))

    log("문서 {}건 중 {}건 처리 대상, 사라진 문서 {}건".format(len(files), len(todo), len(removed)))
    if plan["retry_skip"]:
        log("  지난번 실패 뒤 바뀌지 않아 건너뛴 문서 {}건".format(plan["retry_skip"]))

    t_start = time.perf_counter()
    prog = Progress(max(1, len(todo)))
    counts = {"added": 0, "modified": 0, "same": 0, "moved": 0, "copied": 0}
    total_chunks = 0
    n_reused = n_embedded = 0
    failed = []

    # ── 삭제 ─────────────────────────────────────────
    n_removed = 0
    for key in removed:
        n = remove_doc(key, state, store)
        n_removed += 1
        log("  [삭제] {:<48} {:>4}청크".format(os.path.basename(key)[:46], n))

    # ── 추가·수정 ────────────────────────────────────
    # 단계형 인덱싱 문턱(§3-5-6) — 이번 실행에서 새로 임베딩할 청크가 이만큼 쌓인 뒤로는 벡터를 비워 두고
    #   BM25 로 먼저 검색되게 한다. 문서 몇 건(자동 인덱싱)은 문턱에 닿지 않아 지금처럼 바로 임베딩한다.
    defer_over = config.DEFER_VECTORS_OVER if config.DEFER_VECTORS_OVER > 0 else None
    fresh_chunks = 0
    # 준비 스레드(§3-1) — 문서가 두 건 이상일 때만
    prep = _Prefetcher(todo, ext, embedder, state) if config.EMBED_PREFETCH and len(todo) >= 2 else None
    try:
        for n, (path, fp) in enumerate(todo, 1):
            name = os.path.basename(path)
            prepared = prep.next() if prep else None
            defer = defer_over is not None and fresh_chunks >= defer_over
            r = sync_doc(path, fp, state, embedder, store, ext, log, defer=defer, prepared=prepared)
            if not r["ok"]:
                failed.append((name, r["why"] + (" (옛 버전 유지)" if r.get("kept_old") else "")))
                continue
            counts[r["op"]] += 1
            if r["op"] == "same":
                log("  [{}/{}] {:<48} 내용 같음 — 다시 임베딩하지 않음".format(n, len(todo), name[:46]))
                continue
            if r["op"] == "moved":
                log("  [{}/{}] [이동] {:<44} 경로만 바꿈 — 다시 임베딩하지 않음".format(
                    n, len(todo), name[:42]))
                continue
            total_chunks += r["chunks"]
            n_reused += r.get("reused", 0)
            n_embedded += r.get("embedded", 0)
            fresh_chunks += r.get("embedded", 0) + r.get("deferred", 0)
            rate, eta = prog.update(n)
            label = {"added": "추가", "copied": "복사"}.get(r["op"], "수정")
            note = "  표{}행".format(r["n_rows"]) if r.get("n_rows") else ""
            if r.get("reused"):
                note += "  벡터 재사용 {}/{}".format(r["reused"], r["chunks"])
            if r.get("deferred"):
                note += "  벡터 대기"
            log("  [{}/{}] [{}] {:<44} {:>4}청크  ({:.1f} 문서/s, 남은시간 {:.0f}s)".format(
                n, len(todo), label, name[:42], r["chunks"], rate, eta) + note)
    finally:
        if prep:
            prep.stop()

    # ── BM25 ─────────────────────────────────────────
    # 바뀐 청크만 고친다(덧붙임 · 지움 — §4). 지난번에 벡터만 바꾸고 BM25 를 못 만든 채 끝났으면
    # (bm25_stale), 또는 옛 형식(v2) · 깨진 캐시면 이번에 바뀐 게 없어도 반영한다.
    bm25_missing = bool(state["docs"]) and bm25.ensure_loaded() < 0
    if (counts["added"] or counts["modified"] or counts["copied"] or n_removed or rebuild
            or bm25_stale(state) or bm25_missing):
        log("BM25 인덱스 반영 중...")
        rep = refresh_bm25(store, bm25, state, embedder.tokenizer, full=rebuild, log=log)
        if rep["orphans"]:
            log("  고아 청크 {}개를 지웠습니다(중간에 끊겼던 작업의 찌꺼기)".format(rep["orphans"]))
        state["bm25_built_at"] = time.time()
        save_state(state)
        log(bm25_line(rep))

    # ── 메타데이터 마무리 — 빠진 문서 채우기 · 정리 · 템플릿 상속 판정(전체를 봐야 한다) ──
    # 본문 첫머리(본문 첫 제목 · 양식 빈칸 판정)는 인덱스의 첫 청크에서 읽는다
    meta_hook.finish(state, log, fetch=store.payloads_of)

    for name, why in failed:
        log("  ❌ {} — {}".format(name, why))

    pending = store.pending_count()
    if pending:
        log("검색 가능 — 벡터 채우는 중 0 / {:,} (그동안은 BM25 · 동의어로 찾습니다)".format(pending))

    return {"files": len(files), "changed": len(todo) + n_removed,
            "added": counts["added"], "modified": counts["modified"], "same": counts["same"],
            "moved": counts["moved"], "copied": counts["copied"],
            "removed": n_removed, "delete_blocked": delete_blocked, "held_new": held_new,
            "chunks": total_chunks, "failed": failed,
            "pending": pending, "reused": n_reused, "embedded": n_embedded,
            "sec": round(time.perf_counter() - t_start, 1)}


#------------------------------------------------------------------
# 준비 스레드 (인덱싱 속도 개선 설계서 §3-1)
#=> 다음 문서의 내용 해시 · 추출 · 청킹을 임베딩과 겹쳐 미리 해 둔다. 한 건만 앞서 간다(메모리 상한).
#    1) 내용 해시를 구한다
#    2) 상태 파일에 같은 경로 · 같은 내용이 있으면(내용 같음), 처음 보는 경로인데 같은 내용이
#       다른 자리에 있으면(이동 · 복사 후보) 파싱하지 않는다 — 본 스레드가 판단한다
#    3) 그 밖에는 추출 · 청킹까지 한다(실패도 결과로 넘긴다 — 본 스레드가 실패로 기록)
#   파서 · onnxruntime 이 계산 중에 GIL 을 놓으므로 스레드로 겹친다.
#
# -필드: _q     = 만든 결과를 본 스레드로 넘기는 통로
# -필드: _slots = 앞서 갈 수 있는 건수(1) — 본 스레드가 하나 가져가면 하나 더 만든다
#------------------------------------------------------------------
class _Prefetcher:
    #------------------------------------------------------------------
    # 생성 — 곧바로 준비 스레드를 띄운다
    #
    # -in: todo     = [(경로, 지문)] 처리 순서
    # -in: ext      = build_extractor() 결과
    # -in: embedder = OnnxEmbedder (토크나이저)
    # -in: state    = 상태 dict (지금의 내용 해시를 읽어 둔다)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, todo, ext, embedder, state):
        self._known = {k: m.get("content_hash") for k, m in state["docs"].items()}
        self._hashes = set(h for h in self._known.values() if h)
        self._q = queue.Queue()
        self._slots = threading.Semaphore(1)
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, args=(list(todo), ext, embedder),
                                   name="index-prefetch", daemon=True)
        self._t.start()

    #------------------------------------------------------------------
    # 준비 스레드 본체 — 문서마다 자리를 하나 얻고 만든 뒤 통로에 넣는다
    #
    # -in: todo · ext · embedder = 생성자와 같음
    #
    # -out: 없음
    # -out: error = 없음 (문서 실패는 결과 안의 예외로 넘긴다)
    #------------------------------------------------------------------
    def _run(self, todo, ext, embedder):
        for path, _ in todo:
            self._slots.acquire()
            if self._stop.is_set():
                return
            key = os.path.abspath(path)
            out = {}
            try:
                out["chash"] = content_hash(path)
            except OSError as e:
                out["chash"] = e
            h = out["chash"]
            if not isinstance(h, Exception):
                same = self._known.get(key) == h
                move_or_copy = key not in self._known and h in self._hashes
                if not (same or move_or_copy):
                    try:
                        out["parsed"] = parse_doc(path, os.path.basename(path), ext, embedder)
                    except Exception as e:      # 문서 1건 실패 — 본 스레드가 실패로 기록한다
                        out["parsed"] = e
            self._q.put(out)

    #------------------------------------------------------------------
    # 다음 문서의 준비 결과 받기(처리 순서대로) — 받으면 한 건 더 만들게 자리를 내준다
    #
    # -out: dict {chash, parsed?}
    # -out: error = 없음
    #------------------------------------------------------------------
    def next(self):
        out = self._q.get()
        self._slots.release()
        return out

    #------------------------------------------------------------------
    # 멈추기 — 본 스레드가 끝났거나 오류로 빠져나올 때
    #=> 멈춤 표시를 하고 자리를 하나 내줘, 자리를 기다리던 준비 스레드가 깨어나 끝나게 한다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def stop(self):
        self._stop.set()
        self._slots.release()


#------------------------------------------------------------------
# BM25 반영 결과 한 줄 (진행 출력)
#
# -in: rep = refresh_bm25 결과
#
# -out: 안내 글
# -out: error = 없음
#------------------------------------------------------------------
def bm25_line(rep):
    if rep["mode"] == "update":
        return "BM25 인덱스 {:,}건 — 바뀐 청크만 반영(추가 {} · 삭제 {}) {:.2f}s".format(
            rep["chunks"], rep["added"], rep["removed"], rep["sec"])
    if rep["mode"] == "same":
        return "BM25 인덱스 {:,}건 — 바뀐 청크 없음".format(rep["chunks"])
    return "BM25 인덱스 {:,}건 구축 {:.1f}s{}".format(
        rep["chunks"], rep["sec"], " (토큰화 {:,}건)".format(rep["tokenized"]) if rep["tokenized"] else "")


#------------------------------------------------------------------
# 점들의 단어 빈도 모으기 — 토큰 표에서 읽고, 없는 것만 토큰화해 채운다(설계서 §4-3)
#=> 옛 인덱스(표가 없음)는 처음 한 번 빠진 청크만 토큰화한다(encode_batch 병렬 — 단계 A).
#
# -in: tfs   = TfStore
# -in: ids   = 점 id 목록
# -in: texts = {id: 본문} (토큰화할 때 쓴다)
# -in: tok   = e5 토크나이저
# -in: all_rows = True 면 표 전체를 한 번에 읽는다(전체 구축)
#
# -out: (ids 순서의 단어 빈도 목록, 새로 토큰화한 수)
# -out: error = sqlite 예외 전파
#------------------------------------------------------------------
def _tfs_for(tfs, ids, texts, tok, all_rows=False):
    have = tfs.get(None if all_rows else ids)
    missing = [i for i in ids if i not in have]
    if missing:
        made = bm25_mod.term_freqs([texts.get(i, "") for i in missing], tok)
        tfs.put(missing, made)
        have.update(zip(missing, made))
    return [have[i] for i in ids], len(missing)


#------------------------------------------------------------------
# BM25 반영 (핵심 — 설계서 §4 단계 A~C)
#=> 저장소와 색인을 맞대어 바뀐 청크만 고친다. 많이 바뀌었거나 색인이 없으면 전체를 다시 만든다.
#    1) 저장소를 훑어 고아 점을 청소하고 살아 있는 점의 id · 본문을 모은다
#    2) 색인이 있고 full 이 아니면: 넣을 id · 뺄 id 를 구해, 합칠 때가 아니면 덧붙임 · 지움만 고친다
#    3) 아니면 모든 점의 단어 빈도(토큰 표)로 본 세그먼트를 새로 만든다 — 토큰화 없이
#
# -in: store = VectorStore
# -in: bm25  = Bm25Index
# -in: state = 상태 dict(None 이면 고아 청소 안 함)
# -in: tok   = e5 토크나이저
# -in: full  = True 면 전체 다시 만들기(--rebuild)
# -in: log   = 안내 출력 함수
#
# -out: dict {mode("full"|"update"|"same"), chunks, sec, orphans, added, removed, tokenized}
# -out: error = 저장소 · 파일 예외 전파
#------------------------------------------------------------------
def refresh_bm25(store, bm25, state, tok, full=False, log=None):
    t0 = time.perf_counter()
    texts, ids, n_orphans = collect_for_bm25(store, state, log)
    by_id = dict(zip(ids, texts))
    tfs = bm25_mod.TfStore()
    rep = {"mode": "full", "chunks": len(ids), "orphans": n_orphans, "added": 0, "removed": 0, "tokenized": 0}
    if not full and ids:
        plan = bm25.plan_update(ids)
        if plan is not None:
            add, rem = plan
            if not add and not rem:
                rep.update(mode="same", sec=time.perf_counter() - t0)
                return rep
            if not bm25.should_merge(len(add), len(rem)):
                made, n_tok = _tfs_for(tfs, add, by_id, tok)
                bm25.update(list(zip(add, made)), rem)
                rep.update(mode="update", added=len(add), removed=len(rem), tokenized=n_tok,
                           sec=time.perf_counter() - t0)
                return rep
    # 전체 — 처음 · --rebuild · 합치기 · 옛 형식
    made, n_tok = _tfs_for(tfs, ids, by_id, tok, all_rows=True)
    bm25.build_tf(ids, made)
    # 지운 청크의 토큰 표 찌꺼기 정리(옛 인덱스 · 끊긴 작업)
    tfs.prune(ids)
    rep.update(tokenized=n_tok, sec=time.perf_counter() - t0)
    return rep


#------------------------------------------------------------------
# 벡터 대기 청크 채우기 (단계형 인덱싱 2단계 — 설계서 §3-5-5)
#=> 먼저 넣은 문서부터(id 순) 64개씩 임베딩해 벡터를 채운다. 끊겨도 대기 청크는 저장소에 남으므로
#   다음 index 나 워커가 이어 채운다.
#   배치(8개)는 문서 경계를 넘지 않고 문서의 첫 대기 청크부터 8개씩 묶는다 — 바로 임베딩할 때와
#   묶음이 같아 벡터가 같다(int8 모델은 함께 묶인 글에 따라 벡터가 미세하게 달라진다).
#    1) 대기 청크를 64개 꺼내 문서별로 나눈다
#    2) 꺼낸 묶음이 꽉 찼으면 마지막 문서는 다음 차례로 이어지므로 8의 배수까지만 채운다
#
# -in: embedder    = OnnxEmbedder
# -in: store       = VectorStore
# -in: max_n       = 최대 채울 수(None 이면 다 채울 때까지)
# -in: on_progress = 64개마다 fn(이번에 채운 수, 남은 수)
# -in: on_filled   = 채울 때마다 fn(ids, vecs) — 벡터 한 벌(VectorMatrix) 고치기용
#
# -out: (채운 수, 남은 수)
# -out: error = 임베딩 · 저장 예외 전파
#------------------------------------------------------------------
def fill_pending(embedder, store, max_n=None, on_progress=None, on_filled=None):
    step, done = 64, 0
    while True:
        want = step if max_n is None else min(step, max_n - done)
        if want <= 0:
            break
        batch = store.pending_batch(want)
        if not batch:
            break
        # 문서별로 나눈다(id 순이라 같은 문서는 붙어 있다)
        groups = []
        for i, t, d in batch:
            if not groups or groups[-1][0] != d:
                groups.append((d, []))
            groups[-1][1].append((i, t))
        # 마지막 문서는 다음 차례로 이어질 수 있다 — 8 의 배수까지만(남은 것은 다음에 그 자리부터 8개씩)
        if len(batch) == want and len(groups) > 1:
            last = groups[-1][1]
            del last[len(last) // config.EMBED_BATCH * config.EMBED_BATCH:]
        for _, g in groups:
            if not g:
                continue
            ids = [i for i, _ in g]
            vecs = embedder.embed_passages([t for _, t in g])
            store.set_vectors(ids, vecs)
            if on_filled:
                on_filled(ids, vecs)
            done += len(ids)
        if on_progress:
            on_progress(done, store.pending_count())
    return done, store.pending_count()



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
