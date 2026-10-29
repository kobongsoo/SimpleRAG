//------------------------------------------------------------------
// 색인 연동 (메타데이터 검색 설계서 §11-4 · §17 2단계, Python meta/hook.py)
//=> 인덱서가 문서를 넣고 · 옮기고 · 베끼고 · 빼거나 추출에 실패할 때 meta.sqlite 를 함께 고친다.
//   인덱싱 한 번이면 속성까지 들어간다.
//
//   규칙
//    · 문서마다: 원본 값(raw)만 넣고 그 문서를 임시로 정규화한다(상속 판정은 전체를 봐야 해서 나중에)
//    · 끝날 때(finish): 인덱스에 있는데 메타가 없는 문서 채우기 → 인덱스에서 빠진 문서 메타 지우기
//      → 전체 다시 정규화(템플릿 상속 판정). index 명령 끝 · 워커 /bm25 때 부른다
//    · 사본: 원본의 raw 를 베끼고 경로 · 파일 시스템 정보만 새로 읽는다(설계서 §11-4)
//    · 이동: 행의 경로만 바꾸고 파일 시스템 정보를 새로 읽는다
//    · 추출 실패(스캔 PDF 등)도 메타는 남긴다 — 본문은 못 찾아도 이름 · 날짜 · 사람으로는 찾게
//   메타 처리에서 무슨 일이 나도 인덱싱을 멈추지 않는다(오류를 삼키고 경고 한 줄).
//   SIMPLERAG_META=0 이면 아무것도 하지 않는다.
//------------------------------------------------------------------

use std::collections::HashMap;
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::extract;
use super::normalize as norm;
use super::rules::{load_rules, Rules};
use super::store;
use crate::pyutil;

//------------------------------------------------------------------
// 프로세스 하나에 하나 있는 연동 상태
// -필드: conn  = 열어 둔 meta.sqlite / base = 지금 인덱싱 중인 폴더(폴더 토큰 계산용)
// -필드: cache = (규칙, 별칭 사전) — 문서마다 다시 읽지 않게 / off = 열기에 실패했으면 다시 시도하지 않음
//------------------------------------------------------------------
struct Hook {
    conn: Option<Connection>,
    base: Option<String>,
    cache: Option<(Rules, HashMap<String, String>)>,
    off: bool,
}

static HOOK: Mutex<Hook> = Mutex::new(Hook { conn: None, base: None, cache: None, off: false });

// 잠금 얻기(앞선 패닉으로 독이 들었어도 계속 쓴다)
fn lock() -> std::sync::MutexGuard<'static, Hook> {
    HOOK.lock().unwrap_or_else(|p| p.into_inner())
}

//------------------------------------------------------------------
// 켜져 있나 — SIMPLERAG_META=0 · off · false 면 끔
//------------------------------------------------------------------
pub fn enabled() -> bool {
    let v = std::env::var("SIMPLERAG_META").unwrap_or_else(|_| "1".into());
    !matches!(pyutil::strip(&v), "0" | "off" | "false") && !lock().off
}

// 경고 한 줄(stderr) — 인덱싱 출력 줄 모양을 흐트리지 않게
fn warn(msg: &str) {
    use std::io::Write;
    let mut e = std::io::stderr().lock();
    let _ = writeln!(e, "  [meta] {}", msg);
    let _ = e.flush();
}

//------------------------------------------------------------------
// 메타데이터 폴더 — 상태 파일(index_state.json)이 있는 폴더(보통 데이터 루트)
//=> 시험이 상태 파일만 임시 폴더로 돌릴 때도 메타가 실제 데이터 루트에 섞이지 않게.
//
// -in: 없음
//
// -out: 폴더 경로
// -out: error = 없음
//------------------------------------------------------------------
pub fn data_dir() -> String {
    pyutil::dirname(&pyutil::abspath(&crate::config::cfg().state_path))
}

//------------------------------------------------------------------
// meta.sqlite 열기(처음 한 번) — 꺼져 있거나 실패하면 false
//
// -in: h = 연동 상태
//
// -out: 쓸 수 있으면 true
// -out: error = 없음 (실패하면 이번 프로세스에서 메타를 끈다)
//------------------------------------------------------------------
fn open(h: &mut Hook) -> bool {
    let v = std::env::var("SIMPLERAG_META").unwrap_or_else(|_| "1".into());
    if matches!(pyutil::strip(&v), "0" | "off" | "false") || h.off {
        return false;
    }
    if h.conn.is_none() {
        let home = data_dir();
        match store::connect(&store::default_path(&home)) {
            Ok(c) => {
                h.conn = Some(c);
                h.cache = Some((load_rules(&home), norm::load_aliases(&home)));
            }
            Err(e) => {
                h.off = true;
                warn(&format!("meta.sqlite 를 열지 못해 메타데이터를 건너뜁니다: {}", pyutil::head(&e, 100)));
                return false;
            }
        }
    }
    true
}

//------------------------------------------------------------------
// 지금 인덱싱하는 폴더 정하기 (index_folder 시작 때)
//
// -in: folder = 인덱싱 폴더(None 이면 지움)
//
// -out: 없음
// -out: error = 없음
//------------------------------------------------------------------
pub fn set_base(folder: Option<&str>) {
    lock().base = folder.filter(|f| !f.is_empty()).map(pyutil::abspath);
}

// raw_json 한 줄 읽기
fn raw_of(conn: &Connection, path: &str) -> rusqlite::Result<Option<Value>> {
    let j: Option<Option<String>> = conn.query_row("SELECT raw_json FROM doc_meta WHERE doc_path = ?1", params![path], |r| r.get(0)).optional()?;
    Ok(j.flatten().and_then(|s| serde_json::from_str(&s).ok()))
}

// 한 문서만 임시 정규화(상속은 아직 모름 — finish 가 전체로 다시 한다)
fn normalize_one(conn: &Connection, cache: &(Rules, HashMap<String, String>), path: &str) -> rusqlite::Result<()> {
    let row: Option<(Option<String>, Option<String>, Option<i64>)> = conn.query_row(
        "SELECT raw_json, base, inherited FROM doc_meta WHERE doc_path = ?1", params![path],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional()?;
    let (j, base, inh) = match row {
        Some(r) => r,
        None => return Ok(()),
    };
    let raw: Value = j.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
    // 본문 첫머리(색인이 채움)는 정규화 때만 붙인다
    let head: Option<Option<String>> = conn.query_row("SELECT head FROM doc_body WHERE doc_path = ?1", params![path],
                                                      |r| r.get(0)).optional()?;
    let raw = store::with_body_head(&raw, head.flatten().as_deref());
    let (rules, aliases) = cache;
    // 이미 상속으로 판정된 문서는 그 판정을 유지한다(다음 finish 까지)
    let (meta, people) = norm::normalize(path, &raw, rules, Some(aliases), inh.unwrap_or(0) != 0, base.as_deref());
    store::write_norm(conn, path, &meta, &people)?;
    Ok(())
}

// 문서 넣기 본체(잠금 안에서) — synced · copied · moved 가 함께 쓴다
fn synced_in(h: &mut Hook, path: &str, chash: Option<&str>) -> Result<(), String> {
    let key = pyutil::abspath(path);
    let raw = extract::extract_raw(&key, chash).map_err(|e| format!("OSError: {}", e))?;
    let conn = h.conn.as_ref().unwrap();
    // 한 문서의 쓰기 몇 줄을 한 번에(문서마다 디스크 동기화가 여러 번 일어나지 않게)
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    store::put_raw(&tx, &key, &raw, h.base.as_deref()).map_err(|e| e.to_string())?;
    normalize_one(&tx, h.cache.as_ref().unwrap(), &key).map_err(|e| e.to_string())?;
    tx.commit().map_err(|e| e.to_string())?;
    Ok(())
}

//------------------------------------------------------------------
// 문서를 넣었다 · 고쳤다 · 내용 같음 · 추출 실패 — 원본 값을 다시 읽어 넣는다
//=> 색인이 이미 센 내용 해시를 넘기면 다시 세지 않는다. 문서 하나에 수십 ms.
//
// -in: path  = 문서 경로(상태 파일 키와 같은 절대경로)
// -in: chash = 내용 해시(SHA1, 모르면 None)
//
// -out: 없음
// -out: error = 없음 (실패는 경고만)
//------------------------------------------------------------------
pub fn synced(path: &str, chash: Option<&str>) {
    let mut h = lock();
    if !open(&mut h) {
        return;
    }
    if let Err(e) = synced_in(&mut h, path, chash) {
        warn(&format!("{} — {}", pyutil::basename(path), pyutil::head(&e, 100)));
    }
}

//------------------------------------------------------------------
// 사본을 넣었다 — 원본 raw 를 베끼고 경로 · 파일 시스템 정보만 새로
//=> 원본 메타가 없으면(옛 인덱스) 그냥 새로 읽는다.
//
// -in: src = 원본 경로 / path = 사본 경로 / chash = 내용 해시
//
// -out: 없음
// -out: error = 없음 (실패는 경고만)
//------------------------------------------------------------------
pub fn copied(src: &str, path: &str, chash: Option<&str>) {
    let mut h = lock();
    if !open(&mut h) {
        return;
    }
    let r = (|| -> Result<(), String> {
        let key = pyutil::abspath(path);
        let old = raw_of(h.conn.as_ref().unwrap(), &pyutil::abspath(src)).map_err(|e| e.to_string())?;
        let mut raw = match old {
            Some(Value::Object(m)) => m,
            _ => return synced_in(&mut h, &key, chash),
        };
        let old_sha = norm::obj(raw.get("fs")).get("sha1").cloned().unwrap_or(Value::Null);
        raw.insert("name".into(), json!(pyutil::basename(&key)));
        let mut fs: Map<String, Value> = extract::fs_info(&key, false).map_err(|e| format!("OSError: {}", e))?;
        fs.insert("sha1".into(), match chash.filter(|c| !c.is_empty()) {
            Some(c) => json!(c),
            None => old_sha,
        });
        raw.insert("fs".into(), Value::Object(fs));
        let conn = h.conn.as_ref().unwrap();
        store::put_raw(conn, &key, &Value::Object(raw), h.base.as_deref()).map_err(|e| e.to_string())?;
        normalize_one(conn, h.cache.as_ref().unwrap(), &key).map_err(|e| e.to_string())?;
        Ok(())
    })();
    if let Err(e) = r {
        warn(&format!("사본 {} — {}", pyutil::basename(path), pyutil::head(&e, 100)));
    }
}

//------------------------------------------------------------------
// 문서를 옮겼다 — 행의 경로만 바꾸고 파일 시스템 정보를 새로 읽는다
//
// -in: old = 옛 경로 / path = 새 경로
//
// -out: 없음
// -out: error = 없음 (실패는 경고만)
//------------------------------------------------------------------
pub fn moved(old: &str, path: &str) {
    let mut h = lock();
    if !open(&mut h) {
        return;
    }
    let r = (|| -> Result<(), String> {
        let (old, key) = (pyutil::abspath(old), pyutil::abspath(path));
        let prev = raw_of(h.conn.as_ref().unwrap(), &old).map_err(|e| e.to_string())?;
        let mut raw = match prev {
            Some(Value::Object(m)) => m,
            _ => return synced_in(&mut h, &key, None),
        };
        let sha1 = norm::obj(raw.get("fs")).get("sha1").cloned().unwrap_or(Value::Null);
        raw.insert("name".into(), json!(pyutil::basename(&key)));
        let mut fs = extract::fs_info(&key, false).map_err(|e| format!("OSError: {}", e))?;
        fs.insert("sha1".into(), sha1);
        raw.insert("fs".into(), Value::Object(fs));
        let conn = h.conn.as_ref().unwrap();
        let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
        // 새 경로에 찌꺼기 행이 있으면 먼저 치운다
        store::delete(&tx, &key).map_err(|e| e.to_string())?;
        tx.execute("UPDATE doc_meta SET doc_path = ?1 WHERE doc_path = ?2", params![key, old]).map_err(|e| e.to_string())?;
        tx.execute("UPDATE doc_people SET doc_path = ?1 WHERE doc_path = ?2", params![key, old]).map_err(|e| e.to_string())?;
        tx.execute("UPDATE doc_body SET doc_path = ?1 WHERE doc_path = ?2", params![key, old]).map_err(|e| e.to_string())?;
        store::put_raw(&tx, &key, &Value::Object(raw), h.base.as_deref()).map_err(|e| e.to_string())?;
        normalize_one(&tx, h.cache.as_ref().unwrap(), &key).map_err(|e| e.to_string())?;
        tx.commit().map_err(|e| e.to_string())?;
        Ok(())
    })();
    if let Err(e) = r {
        warn(&format!("이동 {} — {}", pyutil::basename(path), pyutil::head(&e, 100)));
    }
}

//------------------------------------------------------------------
// 문서를 뺐다 — 메타도 지운다
//
// -in: key = 상태 파일 키
//
// -out: 없음
// -out: error = 없음 (실패는 경고만)
//------------------------------------------------------------------
pub fn removed(key: &str) {
    let mut h = lock();
    if !open(&mut h) {
        return;
    }
    if let Err(e) = store::delete(h.conn.as_ref().unwrap(), &pyutil::abspath(key)) {
        warn(&format!("삭제 {} — {}", pyutil::basename(key), pyutil::head(&e.to_string(), 100)));
    }
}

// 채우기 때 폴더 토큰 기준 — 지금 인덱싱 폴더 아래면 그 폴더, 아니면 없음(부모 폴더 이름만 쓰게)
fn base_of(base: &Option<String>, path: &str) -> Option<String> {
    match base {
        Some(b) if pyutil::normcase(path).starts_with(&format!("{}\\", pyutil::normcase(b))) => Some(b.clone()),
        _ => None,
    }
}

//------------------------------------------------------------------
// 마무리 결과
// -필드: filled = 채운 문서 / pruned = 지운 문서 / renorm = 다시 정규화 결과
//------------------------------------------------------------------
#[derive(Debug, Clone, Copy, Default)]
pub struct Finish {
    pub filled: usize,
    pub pruned: usize,
    pub renorm: store::Renorm,
}

//------------------------------------------------------------------
// 마무리 — 채우기 · 정리 · 다시 정규화 (index 끝 · 워커 /bm25)
//=> 1) 상태 파일의 문서 · 실패 문서 중 메타가 없는 것을 읽어 넣는다(옛 인덱스 · 중간에 끊긴 실행)
//   2) 상태 파일에 없는 문서의 메타를 지운다(meta.sqlite 는 인덱스를 그대로 따른다)
//   3) 전체를 다시 정규화한다 — 템플릿 상속은 모든 문서를 봐야 판정할 수 있다
//
// -in: state = 인덱서 상태(index_state.json) / log = 진행 출력 함수
//   (본문 첫머리는 <index_dir>/points.sqlite 에서 읽는다 — 파이썬 fetch=store.payloads_of 자리)
//
// -out: Some(Finish) / 꺼져 있거나 실패하면 None
// -out: error = 없음 (실패는 경고만)
//------------------------------------------------------------------
pub fn finish(state: &Map<String, Value>, log: &mut dyn FnMut(&str)) -> Option<Finish> {
    let mut h = lock();
    if !open(&mut h) {
        return None;
    }
    let r = (|| -> Result<Finish, String> {
        let conn = h.conn.as_ref().unwrap();
        // 원하는 문서(상태 파일 순서) → 내용 해시
        let mut want: Vec<(String, Option<String>)> = vec![];
        let mut want_set: std::collections::HashSet<String> = std::collections::HashSet::new();
        if let Some(d) = state.get("docs").and_then(|v| v.as_object()) {
            for (k, v) in d {
                let p = pyutil::abspath(k);
                let ch = v.get("content_hash").and_then(|x| x.as_str()).map(|s| s.to_string());
                match want.iter_mut().find(|(a, _)| *a == p) {
                    Some(slot) => slot.1 = ch,
                    None => {
                        want_set.insert(p.clone());
                        want.push((p, ch));
                    }
                }
            }
        }
        if let Some(f) = state.get("failed").and_then(|v| v.as_object()) {
            for k in f.keys() {
                let p = pyutil::abspath(k);
                if want_set.insert(p.clone()) {
                    want.push((p, None));
                }
            }
        }
        let have: std::collections::HashSet<String> = {
            let mut st = conn.prepare("SELECT doc_path FROM doc_meta").map_err(|e| e.to_string())?;
            let it = st.query_map([], |r| r.get::<_, String>(0)).map_err(|e| e.to_string())?;
            it.filter_map(|x| x.ok()).collect()
        };
        let missing: Vec<&(String, Option<String>)> = want.iter().filter(|(p, _)| !have.contains(p)).collect();
        let mut filled = 0;
        if !missing.is_empty() {
            log(&format!("메타데이터 채우기: {}건", missing.len()));
        }
        let mut tx = Some(conn.unchecked_transaction().map_err(|e| e.to_string())?);
        for (i, (p, ch)) in missing.iter().enumerate() {
            let i = i + 1;
            if std::path::Path::new(p).is_file() {
                let r = extract::extract_raw(p, ch.as_deref()).map_err(|e| format!("OSError: {}", e))
                    .and_then(|raw| store::put_raw(conn, p, &raw, base_of(&h.base, p).as_deref()).map_err(|e| e.to_string()));
                match r {
                    Ok(()) => filled += 1,
                    Err(e) => warn(&format!("{} — {}", pyutil::basename(p), pyutil::head(&e, 100))),
                }
            }
            if i % 200 == 0 {
                if let Some(t) = tx.take() {
                    t.commit().map_err(|e| e.to_string())?;
                }
                tx = Some(conn.unchecked_transaction().map_err(|e| e.to_string())?);
                log(&format!("  메타데이터 {}/{}", i, missing.len()));
            }
        }
        let pruned: Vec<&String> = have.iter().filter(|p| !want_set.contains(*p)).collect();
        for p in &pruned {
            store::delete(conn, p).map_err(|e| e.to_string())?;
        }
        // 본문 첫머리 — 인덱스(points.sqlite)의 청크를 바로 읽는다(본문 첫 제목 · 양식 빈칸 판정).
        // 인덱스가 아직 없으면 건너뛰고, 실패해도 나머지 정규화는 한다(경고만)
        let pts = pyutil::join(&crate::config::cfg().index_dir, "points.sqlite");
        if std::path::Path::new(&pts).is_file() {
            match fill_bodies_from(conn, state, &pts) {
                Ok(nb) if nb > 0 => log(&format!("메타데이터 본문 첫머리: {}건", nb)),
                Ok(_) => {}
                Err(e) => warn(&format!("본문 첫머리 실패 — {}", pyutil::head(&e, 100))),
            }
        }
        if let Some(t) = tx.take() {
            t.commit().map_err(|e| e.to_string())?;
        }
        let renorm = store::renormalize(conn, &data_dir(), None).map_err(|e| e.to_string())?;
        let res = Finish { filled, pruned: pruned.len(), renorm };
        log(&format!("메타데이터: 문서 {} · 채움 {} · 정리 {} · 상속 {}", renorm.docs, filled, res.pruned, renorm.inherited));
        Ok(res)
    })();
    match r {
        Ok(f) => Some(f),
        Err(e) => {
            warn(&format!("마무리 실패 — {}", pyutil::head(&e, 120)));
            None
        }
    }
}

//------------------------------------------------------------------
// 청크 읽개 — 인덱스의 points.sqlite 를 읽기 전용으로 연다
//=> 색인 저장소(store.rs)를 통째로 올리지 않고 id 로 payload 만 꺼낸다(워커가 열어 둔 중이어도 된다 — WAL).
//
// -in: path = points.sqlite 경로
//
// -out: Ok(Connection)
// -out: error = 열기 실패는 Err(오류문)
//------------------------------------------------------------------
pub fn open_points(path: &str) -> Result<Connection, String> {
    use rusqlite::OpenFlags;
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .map_err(|e| format!("OperationalError: {}", e))?;
    let _ = c.busy_timeout(std::time::Duration::from_secs(5));
    Ok(c)
}

//------------------------------------------------------------------
// id 목록 → payload 목록 (파이썬 fetch(ids) 의 {id: payload} 를 넣은 순서대로)
//=> 같은 id 가 두 번 나오면 한 번만(파이썬 dict 키처럼). 없는 id · 깨진 JSON 은 뺀다.
//
// -in: pc = points.sqlite 연결 / ids = 상태 파일의 청크 id 들(JSON 값)
//
// -out: Ok(payload 들)
// -out: error = sqlite 오류는 Err(오류문)
//------------------------------------------------------------------
pub fn fetch_payloads(pc: &Connection, ids: &[Value]) -> Result<Vec<Value>, String> {
    let mut st = pc.prepare_cached("SELECT payload FROM points WHERE id = ?1").map_err(|e| e.to_string())?;
    let mut seen: Vec<String> = vec![];
    let mut out = vec![];
    for id in ids {
        let k = id.to_string();
        if seen.contains(&k) {
            continue;
        }
        let pl: Option<String> = st.query_row(params![store::json_to_sql(id)], |r| r.get(0)).optional()
            .map_err(|e| e.to_string())?;
        if let Some(pl) = pl {
            seen.push(k);
            if let Ok(v) = serde_json::from_str::<Value>(&pl) {
                out.push(v);
            }
        }
    }
    Ok(out)
}

//------------------------------------------------------------------
// 본문 첫머리 채우기 (7단계, 파이썬 fill_bodies) — 인덱스의 첫 청크 본문을 doc_body 에 넣는다
//=> 문서를 다시 읽지 않고 색인이 이미 만든 청크를 쓴다. 내용 해시가 같으면 건너뛴다.
//    1) 상태 파일 문서마다 doc_body 의 chash 와 비교
//    2) 바뀐 문서는 앞 청크 id 8개의 payload 를 읽어 그 문서의 가장 앞 청크(chunk_idx 최소, 같으면 먼저 것)를 고른다
//    3) "[문서 > 절]" 머리 줄을 떼고 앞 600자(바이트가 아니라 글자)를 넣는다
//
// -in: conn  = meta.sqlite 연결 / state = 인덱서 상태(index_state.json)
// -in: fetch = id 목록 → payload 목록 함수
//
// -out: Ok(채운 문서 수)
// -out: error = fetch · sqlite 오류는 Err(부르는 쪽이 경고로 삼킨다)
//------------------------------------------------------------------
pub fn fill_bodies(conn: &Connection, state: &Map<String, Value>,
                   fetch: &mut dyn FnMut(&[Value]) -> Result<Vec<Value>, String>) -> Result<usize, String> {
    let have: HashMap<String, Option<String>> = {
        let mut st = conn.prepare("SELECT doc_path, chash FROM doc_body").map_err(|e| e.to_string())?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))).map_err(|e| e.to_string())?;
        it.collect::<rusqlite::Result<_>>().map_err(|e| e.to_string())?
    };
    let docs = match state.get("docs").and_then(|v| v.as_object()) {
        Some(d) => d,
        None => return Ok(0),
    };
    let mut n = 0;
    for (k, v) in docs {
        let key = pyutil::abspath(k);
        // 내용 해시가 없거나 이미 같은 해시로 채웠으면 건너뛴다
        let ch = match v.get("content_hash").and_then(|x| x.as_str()).filter(|c| !c.is_empty()) {
            Some(c) => c,
            None => continue,
        };
        if have.get(&key).map(|h| h.as_deref() == Some(ch)).unwrap_or(false) {
            continue;
        }
        let ids: Vec<Value> = v.get("ids").and_then(|x| x.as_array())
            .map(|a| a.iter().take(8).cloned().collect()).unwrap_or_default();
        if ids.is_empty() {
            continue;
        }
        // 이 문서의 청크만(앞 id 가 이웃 문서 것일 수도 있다)
        let nk = pyutil::normcase(&key);
        let pls: Vec<Value> = fetch(&ids)?.into_iter().filter(|pl| {
            pl.as_object().map(|m| !m.is_empty()).unwrap_or(false)
                && pyutil::normcase(pl.get("doc_path").and_then(|x| x.as_str()).unwrap_or("")) == nk
        }).collect();
        // chunk_idx 가 가장 작은 청크(파이썬 min — 같으면 먼저 것, 없으면 0)
        let idx = |pl: &Value| pl.get("chunk_idx").and_then(|x| x.as_f64()).unwrap_or(0.0);
        let mut first: Option<&Value> = None;
        for pl in &pls {
            if first.map(|f| idx(pl) < idx(f)).unwrap_or(true) {
                first = Some(pl);
            }
        }
        let first = match first {
            Some(f) => f,
            None => continue,
        };
        let text = first.get("text").and_then(|x| x.as_str()).unwrap_or("");
        let head = pyutil::head(body_text(text), 600);
        store::put_body(conn, &key, ch, head).map_err(|e| e.to_string())?;
        n += 1;
    }
    Ok(n)
}

//------------------------------------------------------------------
// points.sqlite 로 본문 첫머리 채우기 — finish · meta bodies 가 함께 쓴다
//
// -in: conn = meta.sqlite 연결 / state = 인덱서 상태 / points = points.sqlite 경로
//
// -out: Ok(채운 문서 수)
// -out: error = 열기 · 읽기 실패는 Err(오류문)
//------------------------------------------------------------------
pub fn fill_bodies_from(conn: &Connection, state: &Map<String, Value>, points: &str) -> Result<usize, String> {
    let pc = open_points(points)?;
    let mut fetch = |ids: &[Value]| fetch_payloads(&pc, ids);
    fill_bodies(conn, state, &mut fetch)
}

//------------------------------------------------------------------
// 청크 글에서 "[문서 > 절]" 머리 줄을 뗀 본문 (파이썬 body_text)
//=> "[" 로 시작하고 앞 300자 안에 "]\n" 이 있으면 첫 "]\n" 뒤부터.
//
// -in: text = 청크 글
//
// -out: 본문 조각
// -out: error = 없음
//------------------------------------------------------------------
pub fn body_text(text: &str) -> &str {
    if text.starts_with('[') && pyutil::head(text, 300).contains("]\n") {
        if let Some(i) = text.find("]\n") {
            return &text[i + 2..];
        }
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    // 머리 줄 떼기 — "[문서 > 절]\n" 이 앞 300자 안에 있을 때만
    #[test]
    fn body_text_strip() {
        assert_eq!(body_text("[a > b]\n본문"), "본문");
        assert_eq!(body_text("본문 [x]\n"), "본문 [x]\n");
        let long = format!("[{}]\n본문", "가".repeat(400));
        assert_eq!(body_text(&long), long.as_str());
    }

    // 첫 청크 고르기 — 이웃 문서 청크는 빼고 chunk_idx 가 가장 작은 것, 600자로 자름
    #[test]
    fn fill_pick_first() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(store::SCHEMA).unwrap();
        let state: Map<String, Value> = serde_json::from_value(json!({"docs": {
            "C:\\d\\a.docx": {"content_hash": "h1", "ids": [5, 6, 7]},
            "C:\\d\\b.docx": {"content_hash": "", "ids": [1]},
        }})).unwrap();
        let mut fetch = |ids: &[Value]| -> Result<Vec<Value>, String> {
            Ok(ids.iter().map(|i| match i.as_i64().unwrap() {
                5 => json!({"doc_path": "c:\\D\\A.docx", "chunk_idx": 2, "text": "둘"}),
                6 => json!({"doc_path": "C:\\d\\a.docx", "chunk_idx": 1, "text": format!("[a]\n{}", "가".repeat(700))}),
                _ => json!({"doc_path": "C:\\d\\other.docx", "chunk_idx": 0, "text": "남"}),
            }).collect())
        };
        assert_eq!(fill_bodies(&conn, &state, &mut fetch).unwrap(), 1);
        let h: String = conn.query_row("SELECT head FROM doc_body", [], |r| r.get(0)).unwrap();
        assert_eq!(h, "가".repeat(600));
        // 같은 해시면 다시 채우지 않는다
        assert_eq!(fill_bodies(&conn, &state, &mut fetch).unwrap(), 0);
    }
}
