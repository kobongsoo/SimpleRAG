#------------------------------------------------------------------
# meta.sqlite 저장 (메타데이터 검색 설계서 §11 · §8-2 · 결정 D1)
#=> 문서 단위 메타데이터를 벡터 저장소와 따로 둔다 — 청크마다 복사하지 않고, 별칭 사전 ·
#   규칙이 바뀌어도 벡터 저장소를 건드리지 않고 다시 계산할 수 있게.
#   Python(sqlite3) · Rust(rusqlite) 두 판이 같은 파일을 읽는다(<데이터 루트>/meta.sqlite).
#
#   흐름: 파일 → extract_raw → raw_json 저장 → (전체를 보고) 상속 판정 → normalize → 정규화 칸 갱신
#   raw_json 에 원본이 있으므로 rebuild 는 파일을 다시 읽지 않는다.
#------------------------------------------------------------------

import json
import os
import sqlite3
import time

from . import extract as X
from . import normalize as N
from .rules import load_rules

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_meta (
    doc_path TEXT PRIMARY KEY,      -- index_state.json 의 키와 같은 절대경로
    base TEXT,                      -- 이 문서를 넣은 인덱싱 폴더(폴더 토큰 계산용)
    content_hash TEXT,              -- 중복 그룹 키(SHA1)
    version_key TEXT,               -- 파일명 정리본 → 버전 그룹
    format TEXT,                    -- word|excel|ppt|hwp|pdf|text|image|other
    size INTEGER,
    pages INTEGER,
    modified_ts INTEGER,            -- #6 대표 수정일(UTC epoch)
    created_ts INTEGER,             -- #7 작성일
    created_conf REAL,              -- 상속이면 0.3
    added_ts INTEGER,               -- #8 PC 에 들어온 날
    inherited INTEGER,              -- 템플릿 상속 문서
    title_display TEXT,             -- #11 대표 제목
    title_text TEXT,                -- #10 후보 3개(가산용)
    folder_text TEXT,               -- #1 인덱싱 폴더 아래 폴더 이름들
    origin_app TEXT,                -- #14
    scanned INTEGER,                -- #15
    recv_kind TEXT,                 -- #16 mail|drive|intranet|ai|web
    recv_host TEXT,
    template TEXT,                  -- #17
    doc_type TEXT, doc_type_score REAL,   -- #18
    is_form INTEGER, form_score REAL,
    status TEXT,                    -- ok|encrypted|empty|failed
    ext_mismatch INTEGER,
    raw_json TEXT,                  -- 원본 값(수집 금지 항목은 처음부터 없음) — 다시 정규화용
    updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS doc_people (
    doc_path TEXT, name TEXT, role TEXT,   -- role: author|editor|template
    raw TEXT, source TEXT, conf REAL
);
CREATE INDEX IF NOT EXISTS ix_people_name ON doc_people(name);
CREATE INDEX IF NOT EXISTS ix_people_path ON doc_people(doc_path);
CREATE INDEX IF NOT EXISTS ix_meta_mod ON doc_meta(modified_ts);
CREATE INDEX IF NOT EXISTS ix_meta_hash ON doc_meta(content_hash);
CREATE INDEX IF NOT EXISTS ix_meta_ver ON doc_meta(version_key);
CREATE TABLE IF NOT EXISTS meta_info (key TEXT PRIMARY KEY, value TEXT);
"""

META_COLS = ["doc_path", "base", "content_hash", "version_key", "format", "size", "pages",
             "modified_ts", "created_ts", "created_conf", "added_ts", "inherited", "title_display",
             "title_text", "folder_text", "origin_app", "scanned", "recv_kind", "recv_host", "template",
             "doc_type", "doc_type_score", "is_form", "form_score", "status", "ext_mismatch",
             "raw_json", "updated_ts"]

# 템플릿 상속 판정 — 같은 (만든 이 원본값, 생성일 원본값) 쌍이 내용이 다른 문서 이만큼 이상에 있으면 상속
INHERIT_MIN = 3


#------------------------------------------------------------------
# meta.sqlite 경로 — <데이터 루트>/meta.sqlite
#
# -in: root = 데이터 루트(None 이면 config.ROOT)
#
# -out: 파일 경로
# -out: error = 없음
#------------------------------------------------------------------
def default_path(root=None):
    if root is None:
        from .. import config
        root = config.ROOT
    return os.path.join(root, "meta.sqlite")


#------------------------------------------------------------------
# 열기(없으면 만든다)
#
# -in: path = meta.sqlite 경로
#
# -out: sqlite3.Connection
# -out: error = 폴더를 못 만들거나 파일이 깨졌으면 예외 전파
#------------------------------------------------------------------
def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta_info VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return conn


#------------------------------------------------------------------
# 원본 값만 넣기(정규화는 finish 에서 한꺼번에)
#=> 상속 판정은 전체 문서를 봐야 하므로, 파일을 읽는 단계에서는 raw 만 넣어 둔다.
#
# -in: conn / path = 문서 절대경로 / raw = extract_raw() / base = 인덱싱 폴더
#
# -out: 없음
# -out: error = sqlite 예외 전파
#------------------------------------------------------------------
def put_raw(conn, path, raw, base):
    conn.execute("INSERT INTO doc_meta (doc_path, base, raw_json, updated_ts, status) VALUES (?, ?, ?, ?, ?) "
                 # 워커의 문서 한 건 명령은 인덱싱 폴더를 모른다(None) — 그때는 있던 값을 지킨다
                 "ON CONFLICT(doc_path) DO UPDATE SET base=COALESCE(excluded.base, doc_meta.base), raw_json=excluded.raw_json, "
                 "updated_ts=excluded.updated_ts, status=excluded.status",
                 (path, base, json.dumps(raw, ensure_ascii=False), int(time.time()), raw.get("status")))


#------------------------------------------------------------------
# 문서 빼기(색인에서 지운 문서)
#------------------------------------------------------------------
def delete(conn, path):
    conn.execute("DELETE FROM doc_meta WHERE doc_path = ?", (path,))
    conn.execute("DELETE FROM doc_people WHERE doc_path = ?", (path,))


#------------------------------------------------------------------
# 템플릿 상속 판정 (§8-2)
#=> 같은 (만든 이 원본, 생성일 원본) 쌍이 내용 해시가 다른 문서 INHERIT_MIN 개 이상에 붙어 있으면
#   그 문서들은 회사 양식에서 물려받은 값이다(실측: jungcw / 2011-02-07 이 PPT 75개).
#
# -in: rows = [(doc_path, raw dict)]
#
# -out: 상속 문서 경로 집합
# -out: error = 없음
#------------------------------------------------------------------
def inherited_paths(rows):
    groups = {}
    for path, raw in rows:
        d = raw.get("doc") or {}
        a, c = d.get("author"), d.get("created")
        if not a or not c:
            continue
        groups.setdefault((str(a).strip(), str(c).strip()), []).append((path, (raw.get("fs") or {}).get("sha1")))
    out = set()
    for members in groups.values():
        if len({h for _, h in members}) >= INHERIT_MIN:
            out.update(p for p, _ in members)
    return out


#------------------------------------------------------------------
# 다시 정규화 (finish · meta rebuild)
#=> raw_json 전체로 상속을 판정하고, 문서마다 normalize 해서 정규화 칸 · doc_people 을 새로 쓴다.
#   파일을 다시 읽지 않는다(별칭 사전 · 규칙이 바뀌었을 때 이것만 돌리면 된다).
#
# -in: conn / root = 데이터 루트(규칙 · 별칭 사전 위치) / paths = 이 문서들만(None 이면 전체)
#
# -out: {docs, inherited, people} 개수
# -out: error = sqlite 예외 전파
#------------------------------------------------------------------
def renormalize(conn, root, paths=None):
    rules = load_rules(root)
    aliases = N.load_aliases(root)
    rows = [(p, json.loads(r), b) for p, r, b in conn.execute("SELECT doc_path, raw_json, base FROM doc_meta")]
    inh = inherited_paths([(p, r) for p, r, _ in rows])
    want = set(paths) if paths is not None else None
    n_people = 0
    cols = [c for c in META_COLS if c not in ("doc_path", "base", "raw_json", "updated_ts")]
    for path, raw, base in rows:
        # 상속 판정은 전체를 보고 하므로, 일부만 다시 할 때도 상속이 바뀐 문서는 함께 고친다
        if want is not None and path not in want and path not in inh:
            continue
        meta, people = N.normalize(path, raw, rules, aliases, path in inh, base)
        meta["ext_mismatch"] = int(bool(raw.get("ext_mismatch")))
        conn.execute("UPDATE doc_meta SET " + ", ".join(c + " = ?" for c in cols) + " WHERE doc_path = ?",
                     [meta.get(c) for c in cols] + [path])
        conn.execute("DELETE FROM doc_people WHERE doc_path = ?", (path,))
        for pp in people:
            conn.execute("INSERT INTO doc_people VALUES (?, ?, ?, ?, ?, ?)",
                         (path, pp["name"], pp["role"], str(pp["raw"]), pp["source"], pp["conf"]))
            n_people += 1
    conn.commit()
    return {"docs": len(rows), "inherited": len(inh), "people": n_people}


#------------------------------------------------------------------
# 폴더 훑기 (meta scan) — 1단계의 진입점(색인 연동은 2단계)
#=> 폴더 아래 모든 파일의 원본 값을 읽어 넣고, 사라진 파일은 빼고, 끝에 다시 정규화한다.
#   이미 있고 크기 · 수정 시각이 같은 파일은 다시 읽지 않는다(raw 의 fs 와 비교).
#
# -in: conn / folder = 훑을 폴더 / root = 데이터 루트 / log = 진행 출력 함수
#
# -out: {files, read, skipped, removed, failed, sec, ...renormalize 결과}
# -out: error = 폴더가 없으면 FileNotFoundError
#------------------------------------------------------------------
def scan(conn, folder, root, log=print):
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise FileNotFoundError(folder)
    t0 = time.time()
    known = {p: json.loads(r) for p, r in conn.execute(
        "SELECT doc_path, raw_json FROM doc_meta WHERE base = ?", (folder,))}
    seen = set()
    n_read = n_skip = n_fail = 0
    paths = sorted(os.path.join(d, f) for d, _, fs in os.walk(folder) for f in fs)
    for i, p in enumerate(paths, 1):
        p = os.path.abspath(p)
        seen.add(p)
        try:
            st = os.stat(p)
        except OSError:
            continue
        old = known.get(p)
        if old and (old.get("fs") or {}).get("size") == st.st_size and \
                N.parse_date((old.get("fs") or {}).get("modified")) == int(st.st_mtime):
            n_skip += 1
            continue
        try:
            raw = X.extract_raw(p)
        except OSError:
            n_fail += 1
            continue
        if raw.get("status") == "failed":
            n_fail += 1
        put_raw(conn, p, raw, folder)
        n_read += 1
        if i % 200 == 0:
            log("  {}/{}  {:.0f}s".format(i, len(paths), time.time() - t0))
            conn.commit()
    removed = [p for p in known if p not in seen]
    for p in removed:
        delete(conn, p)
    conn.commit()
    res = {"files": len(paths), "read": n_read, "skipped": n_skip, "removed": len(removed), "failed": n_fail}
    res.update(renormalize(conn, root))
    res["sec"] = round(time.time() - t0, 1)
    return res


#------------------------------------------------------------------
# 요약 통계 (meta stats) — 채움률 · 상태 · 형식 · 사람 상위
#
# -in: conn
#
# -out: dict
# -out: error = 없음
#------------------------------------------------------------------
def stats(conn):
    q = lambda sql, *a: conn.execute(sql, a).fetchall()   # noqa: E731 — 짧은 질의 도우미
    n = q("SELECT COUNT(*) FROM doc_meta")[0][0]
    out = {"docs": n}
    if not n:
        return out
    for col in ("created_ts", "modified_ts", "title_display", "origin_app", "recv_kind", "template", "doc_type"):
        out["filled." + col] = q("SELECT COUNT(*) FROM doc_meta WHERE {} IS NOT NULL".format(col))[0][0]
    out["inherited"] = q("SELECT COUNT(*) FROM doc_meta WHERE inherited = 1")[0][0]
    out["forms"] = q("SELECT COUNT(*) FROM doc_meta WHERE is_form = 1")[0][0]
    out["scanned"] = q("SELECT COUNT(*) FROM doc_meta WHERE scanned = 1")[0][0]
    out["status"] = dict(q("SELECT status, COUNT(*) FROM doc_meta GROUP BY status"))
    out["format"] = dict(q("SELECT format, COUNT(*) FROM doc_meta GROUP BY format ORDER BY 2 DESC"))
    out["docs_with_people"] = q("SELECT COUNT(DISTINCT doc_path) FROM doc_people")[0][0]
    out["top_people"] = q("SELECT name, COUNT(DISTINCT doc_path) FROM doc_people GROUP BY name ORDER BY 2 DESC LIMIT 15")
    out["dup_groups"] = q("SELECT COUNT(*) FROM (SELECT content_hash FROM doc_meta WHERE content_hash IS NOT NULL "
                          "GROUP BY content_hash HAVING COUNT(*) > 1)")[0][0]
    out["version_groups"] = q("SELECT COUNT(*) FROM (SELECT version_key FROM doc_meta WHERE version_key IS NOT NULL "
                              "GROUP BY version_key HAVING COUNT(DISTINCT content_hash) > 1)")[0][0]
    return out


#------------------------------------------------------------------
# 문서 하나 보기 (meta show)
#
# -in: conn / path = 문서 경로(일부만 줘도 됨 — 끝이 같은 첫 문서)
#
# -out: {meta, people} / 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def show(conn, path):
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM doc_meta WHERE doc_path = ?", (os.path.abspath(path),)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM doc_meta WHERE doc_path LIKE ? LIMIT 1", ("%" + path,)).fetchone()
        if row is None:
            return None
        meta = {k: row[k] for k in row.keys() if k != "raw_json"}
        people = [dict(r) for r in conn.execute(
            "SELECT name, role, raw, source, conf FROM doc_people WHERE doc_path = ?", (row["doc_path"],))]
        return {"meta": meta, "people": people, "raw": json.loads(row["raw_json"] or "{}")}
    finally:
        conn.row_factory = None
