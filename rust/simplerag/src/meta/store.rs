//------------------------------------------------------------------
// meta.sqlite 저장 (메타데이터 검색 설계서 §11 · §8-2 · 결정 D1, Python meta/store.py)
//=> 문서 단위 메타데이터를 벡터 저장소와 따로 둔다 — 청크마다 복사하지 않고, 별칭 사전 ·
//   규칙이 바뀌어도 벡터 저장소를 건드리지 않고 다시 계산할 수 있게.
//   Python(sqlite3) · Rust(rusqlite) 두 판이 같은 파일을 읽는다(<데이터 루트>/meta.sqlite).
//
//   흐름: 파일 → extract_raw → raw_json 저장 → (전체를 보고) 상속 판정 → normalize → 정규화 칸 갱신
//   raw_json 에 원본이 있으므로 rebuild 는 파일을 다시 읽지 않는다.
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use rusqlite::types::Value as SqlValue;
use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use super::extract;
use super::normalize::{self as norm, DocMeta, Person};
use super::pyfmt::{self, py_str, truthy};
use super::rules::load_rules;
use crate::pyutil;

pub const SCHEMA_VERSION: i64 = 1;

pub const SCHEMA: &str = "
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
";

pub const META_COLS: &[&str] = &["doc_path", "base", "content_hash", "version_key", "format", "size", "pages",
    "modified_ts", "created_ts", "created_conf", "added_ts", "inherited", "title_display",
    "title_text", "folder_text", "origin_app", "scanned", "recv_kind", "recv_host", "template",
    "doc_type", "doc_type_score", "is_form", "form_score", "status", "ext_mismatch",
    "raw_json", "updated_ts"];

// 템플릿 상속 판정 — 같은 (만든 이 원본값, 생성일 원본값) 쌍이 내용이 다른 문서 이만큼 이상에 있으면 상속
pub const INHERIT_MIN: usize = 3;

//------------------------------------------------------------------
// meta.sqlite 경로 — <데이터 루트>/meta.sqlite
//
// -in: root = 데이터 루트
//
// -out: 파일 경로
// -out: error = 없음
//------------------------------------------------------------------
pub fn default_path(root: &str) -> String {
    pyutil::join(root, "meta.sqlite")
}

//------------------------------------------------------------------
// 열기(없으면 만든다)
//
// -in: path = meta.sqlite 경로
//
// -out: Connection
// -out: error = 폴더를 못 만들거나 파일이 깨졌으면 Err(오류문)
//------------------------------------------------------------------
pub fn connect(path: &str) -> Result<Connection, String> {
    let dir = pyutil::dirname(&pyutil::abspath(path));
    if !dir.is_empty() {
        std::fs::create_dir_all(&dir).map_err(|e| format!("OSError: {}", e))?;
    }
    let conn = Connection::open(path).map_err(|e| format!("OperationalError: {}", e))?;
    // 다른 프로세스(Python 판 · 워커)가 잠깐 쥐고 있어도 기다린다
    let _ = conn.busy_timeout(std::time::Duration::from_secs(5));
    conn.execute_batch(SCHEMA).map_err(|e| format!("OperationalError: {}", e))?;
    conn.execute("INSERT OR REPLACE INTO meta_info VALUES ('schema_version', ?1)", params![SCHEMA_VERSION.to_string()])
        .map_err(|e| format!("OperationalError: {}", e))?;
    Ok(conn)
}

fn now_int() -> i64 {
    pyutil::now_ts() as i64
}

//------------------------------------------------------------------
// 원본 값만 넣기(정규화는 끝에서 한꺼번에)
//=> 상속 판정은 전체 문서를 봐야 하므로, 파일을 읽는 단계에서는 raw 만 넣어 둔다.
//   raw_json 은 파이썬 json.dumps(raw, ensure_ascii=False) 와 같은 글자로 쓴다.
//
// -in: conn / path = 문서 절대경로 / raw = extract_raw() / base = 인덱싱 폴더(모르면 None)
//
// -out: 없음
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn put_raw(conn: &Connection, path: &str, raw: &Value, base: Option<&str>) -> rusqlite::Result<()> {
    let status = raw.get("status").map(json_to_sql).unwrap_or(SqlValue::Null);
    conn.execute("INSERT INTO doc_meta (doc_path, base, raw_json, updated_ts, status) VALUES (?1, ?2, ?3, ?4, ?5) \
                  ON CONFLICT(doc_path) DO UPDATE SET base=COALESCE(excluded.base, doc_meta.base), raw_json=excluded.raw_json, \
                  updated_ts=excluded.updated_ts, status=excluded.status",
                 params![path, base, pyfmt::dumps(raw, None), now_int(), status])?;
    Ok(())
}

//------------------------------------------------------------------
// 문서 빼기(색인에서 지운 문서)
//------------------------------------------------------------------
pub fn delete(conn: &Connection, path: &str) -> rusqlite::Result<()> {
    conn.execute("DELETE FROM doc_meta WHERE doc_path = ?1", params![path])?;
    conn.execute("DELETE FROM doc_people WHERE doc_path = ?1", params![path])?;
    Ok(())
}

// JSON 값 → sqlite 값 (파이썬 sqlite3 가 넣는 형식과 같게: bool 은 정수)
pub fn json_to_sql(v: &Value) -> SqlValue {
    match v {
        Value::Null => SqlValue::Null,
        Value::Bool(b) => SqlValue::Integer(*b as i64),
        Value::Number(n) => match n.as_i64() {
            Some(i) => SqlValue::Integer(i),
            None => SqlValue::Real(n.as_f64().unwrap_or(0.0)),
        },
        Value::String(s) => SqlValue::Text(s.clone()),
        other => SqlValue::Text(other.to_string()),
    }
}

fn opt_i(v: Option<i64>) -> SqlValue {
    v.map(SqlValue::Integer).unwrap_or(SqlValue::Null)
}
fn opt_s(v: &Option<String>) -> SqlValue {
    v.clone().map(SqlValue::Text).unwrap_or(SqlValue::Null)
}

// 정규화 칸 이름(갱신 순서) — META_COLS 에서 doc_path · base · raw_json · updated_ts 를 뺀 것
const NORM_COLS: &[&str] = &["content_hash", "version_key", "format", "size", "pages", "modified_ts", "created_ts",
    "created_conf", "added_ts", "inherited", "title_display", "title_text", "folder_text", "origin_app", "scanned",
    "recv_kind", "recv_host", "template", "doc_type", "doc_type_score", "is_form", "form_score", "status", "ext_mismatch"];

// DocMeta → NORM_COLS 순서의 sqlite 값
fn meta_values(m: &DocMeta) -> Vec<SqlValue> {
    vec![
        json_to_sql(&m.content_hash), opt_s(&m.version_key), SqlValue::Text(m.format.clone()), json_to_sql(&m.size),
        opt_i(m.pages), opt_i(m.modified_ts), opt_i(m.created_ts),
        m.created_conf.map(SqlValue::Real).unwrap_or(SqlValue::Null), opt_i(m.added_ts), SqlValue::Integer(m.inherited),
        SqlValue::Text(m.title_display.clone()), SqlValue::Text(m.title_text.clone()), SqlValue::Text(m.folder_text.clone()),
        opt_s(&m.origin_app), opt_i(m.scanned), opt_s(&m.recv_kind), json_to_sql(&m.recv_host), opt_s(&m.template),
        opt_s(&m.doc_type), SqlValue::Real(m.doc_type_score), SqlValue::Integer(m.is_form), SqlValue::Real(m.form_score),
        json_to_sql(&m.status), SqlValue::Integer(m.ext_mismatch),
    ]
}

//------------------------------------------------------------------
// 정규화 결과 한 문서 쓰기 — doc_meta 정규화 칸 갱신 + doc_people 새로
//
// -in: conn / path = 문서 경로 / meta · people = normalize() 결과
//
// -out: 넣은 관련인 수
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn write_norm(conn: &Connection, path: &str, meta: &DocMeta, people: &[Person]) -> rusqlite::Result<usize> {
    let sql = format!("UPDATE doc_meta SET {} WHERE doc_path = ?",
                      NORM_COLS.iter().map(|c| format!("{} = ?", c)).collect::<Vec<_>>().join(", "));
    let mut vals = meta_values(meta);
    vals.push(SqlValue::Text(path.to_string()));
    conn.prepare_cached(&sql)?.execute(rusqlite::params_from_iter(vals.iter()))?;
    conn.prepare_cached("DELETE FROM doc_people WHERE doc_path = ?1")?.execute(params![path])?;
    let mut ins = conn.prepare_cached("INSERT INTO doc_people VALUES (?1, ?2, ?3, ?4, ?5, ?6)")?;
    for p in people {
        ins.execute(params![path, p.name, p.role, p.raw, p.source, p.conf])?;
    }
    Ok(people.len())
}

//------------------------------------------------------------------
// 템플릿 상속 판정 (§8-2)
//=> 같은 (만든 이 원본, 생성일 원본) 쌍이 내용 해시가 다른 문서 INHERIT_MIN 개 이상에 붙어 있으면
//   그 문서들은 회사 양식에서 물려받은 값이다(실측: jungcw / 2011-02-07 이 PPT 75개).
//
// -in: rows = [(doc_path, raw)]
//
// -out: 상속 문서 경로 집합
// -out: error = 없음
//------------------------------------------------------------------
pub fn inherited_paths(rows: &[(String, Value)]) -> HashSet<String> {
    let mut groups: HashMap<(String, String), Vec<(String, Value)>> = HashMap::new();
    for (path, raw) in rows {
        let d = norm::obj(raw.get("doc"));
        let (a, c) = (d.get("author"), d.get("created"));
        if !truthy(a) || !truthy(c) {
            continue;
        }
        let k = (pyutil::strip(&py_str(a.unwrap())).to_string(), pyutil::strip(&py_str(c.unwrap())).to_string());
        let h = norm::obj(raw.get("fs")).get("sha1").cloned().unwrap_or(Value::Null);
        groups.entry(k).or_default().push((path.clone(), h));
    }
    let mut out = HashSet::new();
    for members in groups.values() {
        let distinct: HashSet<String> = members.iter().map(|(_, h)| h.to_string()).collect();
        if distinct.len() >= INHERIT_MIN {
            out.extend(members.iter().map(|(p, _)| p.clone()));
        }
    }
    out
}

//------------------------------------------------------------------
// 다시 정규화 결과 개수
// -필드: docs = 전체 문서 / inherited = 상속 문서 / people = 넣은 관련인
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, Default)]
pub struct Renorm {
    pub docs: usize,
    pub inherited: usize,
    pub people: usize,
}

//------------------------------------------------------------------
// 다시 정규화 (finish · meta rebuild)
//=> raw_json 전체로 상속을 판정하고, 문서마다 normalize 해서 정규화 칸 · doc_people 을 새로 쓴다.
//   파일을 다시 읽지 않는다(별칭 사전 · 규칙이 바뀌었을 때 이것만 돌리면 된다).
//
// -in: conn / root = 데이터 루트(규칙 · 별칭 사전 위치) / paths = 이 문서들만(None 이면 전체)
//
// -out: Renorm
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn renormalize(conn: &Connection, root: &str, paths: Option<&HashSet<String>>) -> rusqlite::Result<Renorm> {
    let rules = load_rules(root);
    let aliases = norm::load_aliases(root);
    let rows: Vec<(String, Value, Option<String>)> = {
        let mut st = conn.prepare("SELECT doc_path, raw_json, base FROM doc_meta")?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<String>>(2)?)))?;
        let mut v = vec![];
        for x in it {
            let (p, j, b) = x?;
            let raw = j.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
            v.push((p, raw, b));
        }
        v
    };
    let pairs: Vec<(String, Value)> = rows.iter().map(|(p, r, _)| (p.clone(), r.clone())).collect();
    let inh = inherited_paths(&pairs);
    let tx = conn.unchecked_transaction()?;
    let mut n_people = 0;
    for (path, raw, base) in &rows {
        // 상속 판정은 전체를 보고 하므로, 일부만 다시 할 때도 상속이 바뀐 문서는 함께 고친다
        if let Some(want) = paths {
            if !want.contains(path) && !inh.contains(path) {
                continue;
            }
        }
        let (meta, people) = norm::normalize(path, raw, &rules, Some(&aliases), inh.contains(path), base.as_deref());
        n_people += write_norm(&tx, path, &meta, &people)?;
    }
    tx.commit()?;
    Ok(Renorm { docs: rows.len(), inherited: inh.len(), people: n_people })
}

//------------------------------------------------------------------
// 폴더 훑기 결과
// -필드: files · read · skipped · removed · failed = 파일 수 / renorm = 다시 정규화 결과 / sec = 걸린 초
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct ScanResult {
    pub files: usize,
    pub read: usize,
    pub skipped: usize,
    pub removed: usize,
    pub failed: usize,
    pub renorm: Renorm,
    pub sec: f64,
}

// os.walk(folder) 의 모든 파일 경로(정렬 전) — 못 여는 폴더는 조용히 건너뛴다
fn walk(dir: &str, out: &mut Vec<String>) {
    let rd = match std::fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    let mut subdirs = vec![];
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let p = pyutil::join(dir, &name);
        let ft = match e.file_type() {
            Ok(t) => t,
            Err(_) => continue,
        };
        // 파이썬 os.walk: 디렉터리 판정은 링크를 따라가지만, 링크 폴더 안으로는 내려가지 않는다
        let is_dir = if ft.is_symlink() { std::path::Path::new(&p).is_dir() } else { ft.is_dir() };
        if is_dir {
            if !ft.is_symlink() {
                subdirs.push(p);
            }
        } else {
            out.push(p);
        }
    }
    for d in subdirs {
        walk(&d, out);
    }
}

//------------------------------------------------------------------
// 폴더 훑기 (meta scan)
//=> 폴더 아래 모든 파일의 원본 값을 읽어 넣고, 사라진 파일은 빼고, 끝에 다시 정규화한다.
//   이미 있고 크기 · 수정 시각이 같은 파일은 다시 읽지 않는다(raw 의 fs 와 비교).
//
// -in: conn / folder = 훑을 폴더 / root = 데이터 루트 / log = 진행 출력 함수
//
// -out: Ok(ScanResult)
// -out: error = 폴더가 없으면 Err(NotFound(폴더)), sqlite 오류는 Err(Sql)
//------------------------------------------------------------------
pub enum ScanError {
    NotFound(String),
    Sql(rusqlite::Error),
}

impl From<rusqlite::Error> for ScanError {
    fn from(e: rusqlite::Error) -> Self {
        ScanError::Sql(e)
    }
}

pub fn scan(conn: &Connection, folder: &str, root: &str, log: &mut dyn FnMut(&str)) -> Result<ScanResult, ScanError> {
    let folder = pyutil::abspath(folder);
    if !std::path::Path::new(&folder).is_dir() {
        return Err(ScanError::NotFound(folder));
    }
    let t0 = Instant::now();
    let known: HashMap<String, Value> = {
        let mut st = conn.prepare("SELECT doc_path, raw_json FROM doc_meta WHERE base = ?1")?;
        let it = st.query_map(params![folder], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)))?;
        let mut m = HashMap::new();
        for x in it {
            let (p, j) = x?;
            m.insert(p, j.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null));
        }
        m
    };
    let mut seen: HashSet<String> = HashSet::new();
    let (mut n_read, mut n_skip, mut n_fail) = (0, 0, 0);
    let mut paths = vec![];
    walk(&folder, &mut paths);
    paths.sort();
    let total = paths.len();
    let mut tx = Some(conn.unchecked_transaction()?);
    for (i, p) in paths.iter().enumerate() {
        let i = i + 1;
        let p = pyutil::abspath(p);
        seen.insert(p.clone());
        let st = match std::fs::metadata(&p) {
            Ok(s) => s,
            Err(_) => continue,
        };
        if let Some(old) = known.get(&p) {
            let fs = norm::obj(old.get("fs"));
            let same_size = fs.get("size").and_then(|v| v.as_u64()) == Some(st.len());
            if truthy(Some(old)) && same_size && norm::parse_date(fs.get("modified"), None).is_some()
                && norm::parse_date(fs.get("modified"), None) == pyfmt::st_mtime_int(&p) {
                n_skip += 1;
                continue;
            }
        }
        let raw = match extract::extract_raw(&p, None) {
            Ok(r) => r,
            Err(_) => {
                n_fail += 1;
                continue;
            }
        };
        if raw.get("status").and_then(|v| v.as_str()) == Some("failed") {
            n_fail += 1;
        }
        put_raw(conn, &p, &raw, Some(&folder))?;
        n_read += 1;
        if i % 200 == 0 {
            log(&format!("  {}/{}  {:.0}s", i, total, t0.elapsed().as_secs_f64()));
            if let Some(t) = tx.take() {
                t.commit()?;
            }
            tx = Some(conn.unchecked_transaction()?);
        }
    }
    let removed: Vec<&String> = known.keys().filter(|p| !seen.contains(*p)).collect();
    for p in &removed {
        delete(conn, p)?;
    }
    if let Some(t) = tx.take() {
        t.commit()?;
    }
    let renorm = renormalize(conn, root, None)?;
    Ok(ScanResult { files: total, read: n_read, skipped: n_skip, removed: removed.len(), failed: n_fail, renorm,
                    sec: pyutil::round_to(t0.elapsed().as_secs_f64(), 1) })
}

// sqlite 값 → JSON (stats · show 출력용)
fn sql_to_json(v: SqlValue) -> Value {
    match v {
        SqlValue::Null => Value::Null,
        SqlValue::Integer(i) => json!(i),
        SqlValue::Real(f) => pyutil::jf(f),
        SqlValue::Text(s) => json!(s),
        SqlValue::Blob(b) => json!(String::from_utf8_lossy(&b)),
    }
}

// 질의 결과 첫 칸 정수
fn count(conn: &Connection, sql: &str) -> rusqlite::Result<i64> {
    conn.query_row(sql, [], |r| r.get(0))
}

// 두 칸 질의 → [(키, 값)] (키는 파이썬 json 의 dict 키처럼 글자로)
fn pairs(conn: &Connection, sql: &str) -> rusqlite::Result<Vec<(SqlValue, SqlValue)>> {
    let mut st = conn.prepare(sql)?;
    let it = st.query_map([], |r| Ok((r.get::<_, SqlValue>(0)?, r.get::<_, SqlValue>(1)?)))?;
    it.collect()
}

// dict 키 글자 — 파이썬 json.dumps 는 None 키를 "null", 정수 키를 숫자 글자로 쓴다
fn key_str(v: &SqlValue) -> String {
    match v {
        SqlValue::Null => "null".into(),
        SqlValue::Integer(i) => i.to_string(),
        SqlValue::Real(f) => pyutil::float_repr(*f),
        SqlValue::Text(s) => s.clone(),
        SqlValue::Blob(b) => String::from_utf8_lossy(b).into_owned(),
    }
}

//------------------------------------------------------------------
// 요약 통계 (meta stats) — 채움률 · 상태 · 형식 · 사람 상위
//
// -in: conn
//
// -out: JSON 객체(파이썬 stats() 와 같은 키 순서)
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn stats(conn: &Connection) -> rusqlite::Result<Value> {
    let n = count(conn, "SELECT COUNT(*) FROM doc_meta")?;
    let mut out = Map::new();
    out.insert("docs".into(), json!(n));
    if n == 0 {
        return Ok(Value::Object(out));
    }
    for col in ["created_ts", "modified_ts", "title_display", "origin_app", "recv_kind", "template", "doc_type"] {
        out.insert(format!("filled.{}", col), json!(count(conn, &format!("SELECT COUNT(*) FROM doc_meta WHERE {} IS NOT NULL", col))?));
    }
    out.insert("inherited".into(), json!(count(conn, "SELECT COUNT(*) FROM doc_meta WHERE inherited = 1")?));
    out.insert("forms".into(), json!(count(conn, "SELECT COUNT(*) FROM doc_meta WHERE is_form = 1")?));
    out.insert("scanned".into(), json!(count(conn, "SELECT COUNT(*) FROM doc_meta WHERE scanned = 1")?));
    let to_obj = |ps: Vec<(SqlValue, SqlValue)>| {
        let mut m = Map::new();
        for (k, v) in ps {
            m.insert(key_str(&k), sql_to_json(v));
        }
        Value::Object(m)
    };
    out.insert("status".into(), to_obj(pairs(conn, "SELECT status, COUNT(*) FROM doc_meta GROUP BY status")?));
    out.insert("format".into(), to_obj(pairs(conn, "SELECT format, COUNT(*) FROM doc_meta GROUP BY format ORDER BY 2 DESC")?));
    out.insert("docs_with_people".into(), json!(count(conn, "SELECT COUNT(DISTINCT doc_path) FROM doc_people")?));
    let top = pairs(conn, "SELECT name, COUNT(DISTINCT doc_path) FROM doc_people GROUP BY name ORDER BY 2 DESC LIMIT 15")?;
    out.insert("top_people".into(), Value::Array(top.into_iter().map(|(a, b)| json!([sql_to_json(a), sql_to_json(b)])).collect()));
    out.insert("dup_groups".into(), json!(count(conn, "SELECT COUNT(*) FROM (SELECT content_hash FROM doc_meta WHERE content_hash IS NOT NULL \
        GROUP BY content_hash HAVING COUNT(*) > 1)")?));
    out.insert("version_groups".into(), json!(count(conn, "SELECT COUNT(*) FROM (SELECT version_key FROM doc_meta WHERE version_key IS NOT NULL \
        GROUP BY version_key HAVING COUNT(DISTINCT content_hash) > 1)")?));
    Ok(Value::Object(out))
}

//------------------------------------------------------------------
// 문서 하나 보기 (meta show)
//
// -in: conn / path = 문서 경로(일부만 줘도 됨 — 끝이 같은 첫 문서)
//
// -out: Some({meta, people, raw}) / 없으면 None
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn show(conn: &Connection, path: &str) -> rusqlite::Result<Option<Value>> {
    use rusqlite::OptionalExtension;
    let cols = META_COLS.join(", ");
    let read_row = |r: &rusqlite::Row| -> rusqlite::Result<Vec<SqlValue>> {
        (0..META_COLS.len()).map(|i| r.get::<_, SqlValue>(i)).collect()
    };
    let mut row = conn.query_row(&format!("SELECT {} FROM doc_meta WHERE doc_path = ?1", cols),
                                 params![pyutil::abspath(path)], read_row).optional()?;
    if row.is_none() {
        row = conn.query_row(&format!("SELECT {} FROM doc_meta WHERE doc_path LIKE ?1 LIMIT 1", cols),
                             params![format!("%{}", path)], read_row).optional()?;
    }
    let row = match row {
        Some(r) => r,
        None => return Ok(None),
    };
    let mut meta = Map::new();
    let mut raw_json = String::new();
    let mut doc_path = String::new();
    for (c, v) in META_COLS.iter().zip(row) {
        if *c == "raw_json" {
            if let SqlValue::Text(s) = &v {
                raw_json = s.clone();
            }
            continue;
        }
        if *c == "doc_path" {
            if let SqlValue::Text(s) = &v {
                doc_path = s.clone();
            }
        }
        meta.insert(c.to_string(), sql_to_json(v));
    }
    let mut st = conn.prepare("SELECT name, role, raw, source, conf FROM doc_people WHERE doc_path = ?1")?;
    let people: Vec<Value> = st.query_map(params![doc_path], |r| {
        let mut m = Map::new();
        for (i, k) in ["name", "role", "raw", "source", "conf"].iter().enumerate() {
            m.insert(k.to_string(), sql_to_json(r.get::<_, SqlValue>(i)?));
        }
        Ok(Value::Object(m))
    })?.collect::<rusqlite::Result<_>>()?;
    let raw: Value = serde_json::from_str(if raw_json.is_empty() { "{}" } else { &raw_json }).unwrap_or(json!({}));
    Ok(Some(json!({"meta": Value::Object(meta), "people": people, "raw": raw})))
}

#[cfg(test)]
mod tests {
    use super::*;

    // 템플릿 상속 판정 (§8-2) — 같은 (만든 이, 생성일) 쌍이 내용이 다른 문서 3개 이상
    #[test]
    fn inherit() {
        let raw = |h: &str| json!({"doc": {"author": "jungcw", "created": "2011-02-07T02:01:21Z"}, "fs": {"sha1": h}});
        let rows = vec![("a".to_string(), raw("1")), ("b".to_string(), raw("2")), ("c".to_string(), raw("3")),
                        ("d".to_string(), json!({"doc": {"author": "x", "created": "y"}, "fs": {"sha1": "9"}}))];
        let got = inherited_paths(&rows);
        let want: HashSet<String> = ["a", "b", "c"].iter().map(|s| s.to_string()).collect();
        assert_eq!(got, want);
        // 같은 내용의 사본 셋은 상속이 아니다
        let rows = vec![("a".to_string(), raw("1")), ("b".to_string(), raw("1")), ("c".to_string(), raw("1"))];
        assert!(inherited_paths(&rows).is_empty());
    }
}
