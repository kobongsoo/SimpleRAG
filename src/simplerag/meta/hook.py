#------------------------------------------------------------------
# 색인 연동 (메타데이터 검색 설계서 §11-4 · §17 2단계)
#=> 인덱서가 문서를 넣고 · 옮기고 · 베끼고 · 빼거나 추출에 실패할 때 meta.sqlite 를 함께 고친다.
#   인덱싱 한 번이면 속성까지 들어간다.
#
#   규칙
#    · 문서마다: 원본 값(raw)만 넣고 그 문서를 임시로 정규화한다(상속 판정은 전체를 봐야 해서 나중에)
#    · 끝날 때(finish): 인덱스에 있는데 메타가 없는 문서 채우기 → 인덱스에서 빠진 문서 메타 지우기
#      → 전체 다시 정규화(템플릿 상속 판정). index 명령 끝 · 워커 /bm25 때 부른다
#    · 사본: 원본의 raw 를 베끼고 경로 · 파일 시스템 정보만 새로 읽는다(설계서 §11-4)
#    · 이동: 행의 경로만 바꾸고 파일 시스템 정보를 새로 읽는다
#    · 추출 실패(스캔 PDF 등)도 메타는 남긴다 — 본문은 못 찾아도 이름 · 날짜 · 사람으로는 찾게
#   메타 처리에서 무슨 일이 나도 인덱싱을 멈추지 않는다(예외를 삼키고 경고 한 줄).
#   SIMPLERAG_META=0 이면 아무것도 하지 않는다.
#------------------------------------------------------------------

import json
import os
import sys

from . import extract as X
from . import normalize as N
from . import store as S
from .rules import load_rules

_conn = None          # 열어 둔 meta.sqlite (인덱서 프로세스에 하나)
_base = None          # 지금 인덱싱 중인 폴더(폴더 토큰 계산용) — index_folder 가 정한다
_cache = None         # (rules, aliases) — 문서마다 다시 읽지 않게
_off = False          # 열기에 실패했으면 이번 프로세스에서는 다시 시도하지 않는다


#------------------------------------------------------------------
# 켜져 있나 — SIMPLERAG_META=0 이면 끔
#------------------------------------------------------------------
def enabled():
    return os.environ.get("SIMPLERAG_META", "1").strip() not in ("0", "off", "false") and not _off


# 경고 한 줄(stderr) — 인덱싱 출력 줄 모양을 흐트리지 않게
def _warn(msg):
    try:
        print("  [meta] " + msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — 경고조차 못 써도 인덱싱은 계속
        pass


#------------------------------------------------------------------
# meta.sqlite 열기(처음 한 번)
#
# -in: 없음
#
# -out: sqlite3.Connection / 꺼져 있거나 열기 실패면 None
# -out: error = 없음 (실패하면 이번 프로세스에서 메타를 끈다)
#------------------------------------------------------------------
def _db():
    global _conn, _off, _cache
    if not enabled():
        return None
    if _conn is None:
        try:
            # 상태 파일(index_state.json) 옆에 둔다 — 보통은 데이터 루트이고, 시험이 상태 파일만
            # 임시 폴더로 돌릴 때도 메타가 실제 데이터 루트에 섞이지 않는다
            home = data_dir()
            _conn = S.connect(S.default_path(home))
            _cache = (load_rules(home), N.load_aliases(home))
        except Exception as e:  # noqa: BLE001
            _off = True
            _warn("meta.sqlite 를 열지 못해 메타데이터를 건너뜁니다: {}: {}".format(type(e).__name__, str(e)[:80]))
            return None
    return _conn


#------------------------------------------------------------------
# 메타데이터 폴더 — 상태 파일(index_state.json)이 있는 폴더(보통 데이터 루트)
#
# -in: 없음
#
# -out: 폴더 경로
# -out: error = 없음
#------------------------------------------------------------------
def data_dir():
    from .. import config
    return os.path.dirname(os.path.abspath(config.STATE_PATH))


#------------------------------------------------------------------
# 지금 인덱싱하는 폴더 정하기 (index_folder 시작 때)
#
# -in: folder = 인덱싱 폴더(None 이면 지움)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def set_base(folder):
    global _base
    _base = os.path.abspath(folder) if folder else None


# 한 문서만 임시 정규화(상속은 아직 모름 — finish 가 전체로 다시 한다)
def _normalize_one(conn, path):
    row = conn.execute("SELECT raw_json, base FROM doc_meta WHERE doc_path = ?", (path,)).fetchone()
    if row is None:
        return
    raw = json.loads(row[0])
    # 본문 첫머리(색인이 채움)는 정규화 때만 붙인다
    head = conn.execute("SELECT head FROM doc_body WHERE doc_path = ?", (path,)).fetchone()
    if head and head[0]:
        raw["body_head"] = head[0]
    # 이미 상속으로 판정된 문서는 그 판정을 유지한다(다음 finish 까지)
    inh = conn.execute("SELECT inherited FROM doc_meta WHERE doc_path = ?", (path,)).fetchone()
    rules, aliases = _cache
    meta, people = N.normalize(path, raw, rules, aliases, bool(inh and inh[0]), row[1])
    meta["ext_mismatch"] = int(bool(raw.get("ext_mismatch")))
    cols = [c for c in S.META_COLS if c not in ("doc_path", "base", "raw_json", "updated_ts")]
    conn.execute("UPDATE doc_meta SET " + ", ".join(c + " = ?" for c in cols) + " WHERE doc_path = ?",
                 [meta.get(c) for c in cols] + [path])
    conn.execute("DELETE FROM doc_people WHERE doc_path = ?", (path,))
    for pp in people:
        conn.execute("INSERT INTO doc_people VALUES (?, ?, ?, ?, ?, ?)",
                     (path, pp["name"], pp["role"], str(pp["raw"]), pp["source"], pp["conf"]))


#------------------------------------------------------------------
# 문서를 넣었다 · 고쳤다 · 내용 같음 · 추출 실패 — 원본 값을 다시 읽어 넣는다
#=> 색인이 이미 센 내용 해시를 넘기면 다시 세지 않는다. 문서 하나에 수십 ms.
#
# -in: path  = 문서 경로(상태 파일 키와 같은 절대경로)
# -in: chash = 내용 해시(SHA1, 모르면 None)
#
# -out: 없음
# -out: error = 없음 (실패는 경고만)
#------------------------------------------------------------------
def synced(path, chash=None):
    conn = _db()
    if conn is None:
        return
    try:
        key = os.path.abspath(path)
        raw = X.extract_raw(key, sha1=chash)
        S.put_raw(conn, key, raw, _base)
        _normalize_one(conn, key)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        _warn("{} — {}: {}".format(os.path.basename(path), type(e).__name__, str(e)[:80]))


#------------------------------------------------------------------
# 사본을 넣었다 — 원본 raw 를 베끼고 경로 · 파일 시스템 정보만 새로
#=> 원본 메타가 없으면(옛 인덱스) 그냥 새로 읽는다.
#
# -in: src = 원본 경로 / path = 사본 경로 / chash = 내용 해시
#------------------------------------------------------------------
def copied(src, path, chash=None):
    conn = _db()
    if conn is None:
        return
    try:
        key = os.path.abspath(path)
        row = conn.execute("SELECT raw_json FROM doc_meta WHERE doc_path = ?", (os.path.abspath(src),)).fetchone()
        if row is None:
            synced(key, chash)
            return
        raw = json.loads(row[0])
        raw["name"] = os.path.basename(key)
        raw["fs"] = X.fs_info(key, want_hash=False)
        raw["fs"]["sha1"] = chash or (json.loads(row[0]).get("fs") or {}).get("sha1")
        S.put_raw(conn, key, raw, _base)
        _normalize_one(conn, key)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        _warn("사본 {} — {}: {}".format(os.path.basename(path), type(e).__name__, str(e)[:80]))


#------------------------------------------------------------------
# 문서를 옮겼다 — 행의 경로만 바꾸고 파일 시스템 정보를 새로 읽는다
#
# -in: old = 옛 경로 / path = 새 경로
#------------------------------------------------------------------
def moved(old, path):
    conn = _db()
    if conn is None:
        return
    try:
        old, key = os.path.abspath(old), os.path.abspath(path)
        row = conn.execute("SELECT raw_json FROM doc_meta WHERE doc_path = ?", (old,)).fetchone()
        if row is None:
            synced(key)
            return
        raw = json.loads(row[0])
        sha1 = (raw.get("fs") or {}).get("sha1")
        raw["name"] = os.path.basename(key)
        raw["fs"] = X.fs_info(key, want_hash=False)
        raw["fs"]["sha1"] = sha1
        S.delete(conn, key)          # 새 경로에 찌꺼기 행이 있으면 먼저 치운다
        conn.execute("UPDATE doc_meta SET doc_path = ? WHERE doc_path = ?", (key, old))
        conn.execute("UPDATE doc_people SET doc_path = ? WHERE doc_path = ?", (key, old))
        conn.execute("UPDATE doc_body SET doc_path = ? WHERE doc_path = ?", (key, old))
        S.put_raw(conn, key, raw, _base)
        _normalize_one(conn, key)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        _warn("이동 {} — {}: {}".format(os.path.basename(path), type(e).__name__, str(e)[:80]))


#------------------------------------------------------------------
# 문서를 뺐다 — 메타도 지운다
#
# -in: key = 상태 파일 키
#------------------------------------------------------------------
def removed(key):
    conn = _db()
    if conn is None:
        return
    try:
        S.delete(conn, os.path.abspath(key))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        _warn("삭제 {} — {}: {}".format(os.path.basename(key), type(e).__name__, str(e)[:80]))


#------------------------------------------------------------------
# 마무리 — 채우기 · 정리 · 다시 정규화 (index 끝 · 워커 /bm25)
#=> 1) 상태 파일의 문서 · 실패 문서 중 메타가 없는 것을 읽어 넣는다(옛 인덱스 · 중간에 끊긴 실행)
#   2) 상태 파일에 없는 문서의 메타를 지운다(meta.sqlite 는 인덱스를 그대로 따른다)
#   3) 전체를 다시 정규화한다 — 템플릿 상속은 모든 문서를 봐야 판정할 수 있다
#
# -in: state = 인덱서 상태 dict / log = 진행 출력 함수
# -in: fetch = 청크 id 목록 → {id: payload} 함수(주면 본문 첫머리도 채운다, 기본 None)
#
# -out: {filled, pruned, docs, inherited} / 꺼져 있으면 None
# -out: error = 없음 (실패는 경고만)
#------------------------------------------------------------------
def finish(state, log=None, fetch=None):
    log = log or (lambda m: None)
    conn = _db()
    if conn is None:
        return None
    try:
        want = {os.path.abspath(k): (v or {}).get("content_hash") for k, v in state.get("docs", {}).items()}
        for k in state.get("failed", {}):
            want.setdefault(os.path.abspath(k), None)
        have = {p for (p,) in conn.execute("SELECT doc_path FROM doc_meta")}
        missing = [p for p in want if p not in have]
        filled = 0
        if missing:
            log("메타데이터 채우기: {}건".format(len(missing)))
        for i, p in enumerate(missing, 1):
            if not os.path.isfile(p):
                continue
            try:
                S.put_raw(conn, p, X.extract_raw(p, sha1=want[p]), _base_of(p))
                filled += 1
            except Exception as e:  # noqa: BLE001
                _warn("{} — {}: {}".format(os.path.basename(p), type(e).__name__, str(e)[:80]))
            if i % 200 == 0:
                conn.commit()
                log("  메타데이터 {}/{}".format(i, len(missing)))
        pruned = [p for p in have if p not in want]
        for p in pruned:
            S.delete(conn, p)
        # 본문 첫머리 — 색인이 청크를 읽을 함수를 넘겼을 때만(본문 첫 제목 · 양식 빈칸 판정)
        if fetch is not None:
            try:
                nb = fill_bodies(conn, state, fetch)
                if nb:
                    log("메타데이터 본문 첫머리: {}건".format(nb))
            except Exception as e:  # noqa: BLE001 — 첫머리를 못 채워도 나머지 정규화는 한다
                _warn("본문 첫머리 실패 — {}: {}".format(type(e).__name__, str(e)[:80]))
        conn.commit()
        res = S.renormalize(conn, data_dir())
        res.update(filled=filled, pruned=len(pruned))
        log("메타데이터: 문서 {docs} · 채움 {filled} · 정리 {pruned} · 상속 {inherited}".format(**res))
        return res
    except Exception as e:  # noqa: BLE001
        _warn("마무리 실패 — {}: {}".format(type(e).__name__, str(e)[:120]))
        return None


#------------------------------------------------------------------
# 본문 첫머리 채우기 (7단계) — 인덱스의 첫 청크 본문을 doc_body 에 넣는다
#=> 문서를 다시 읽지 않고 색인이 이미 만든 청크를 쓴다. 내용 해시가 같으면 건너뛴다.
#    1) 상태 파일 문서마다 doc_body 의 chash 와 비교
#    2) 바뀐 문서는 앞 청크 몇 개의 payload 를 읽어 그 문서의 가장 앞 청크(chunk_idx 최소)를 고른다
#    3) "[문서 > 절]" 머리 줄을 떼고 앞 600자를 넣는다
#
# -in: conn = meta.sqlite 연결 / state = 인덱서 상태 / fetch = id 목록 → {id: payload} 함수
#
# -out: 채운 문서 수
# -out: error = fetch 예외는 전파(부르는 쪽이 삼킨다)
#------------------------------------------------------------------
def fill_bodies(conn, state, fetch):
    have = {p: h for p, h in conn.execute("SELECT doc_path, chash FROM doc_body")}
    n = 0
    for k, v in state.get("docs", {}).items():
        key = os.path.abspath(k)
        ch = (v or {}).get("content_hash")
        if not ch or have.get(key) == ch:
            continue
        ids = list((v or {}).get("ids") or [])[:8]
        if not ids:
            continue
        pls = [pl for pl in fetch(ids).values() if pl and os.path.normcase(pl.get("doc_path") or "") == os.path.normcase(key)]
        if not pls:
            continue
        first = min(pls, key=lambda pl: pl.get("chunk_idx") or 0)
        head = body_text(first.get("text") or "")[:600]
        S.put_body(conn, key, ch, head)
        n += 1
    return n


# 청크 글에서 "[문서 > 절]" 머리 줄을 뗀 본문
def body_text(text):
    if text.startswith("[") and "]\n" in text[:300]:
        return text.split("]\n", 1)[1]
    return text


# 채우기 때 폴더 토큰 기준 — 지금 인덱싱 폴더 아래면 그 폴더, 아니면 없음(부모 폴더 이름만 쓰게)
def _base_of(path):
    if _base and os.path.normcase(path).startswith(os.path.normcase(_base) + os.sep):
        return _base
    return None
