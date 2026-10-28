//------------------------------------------------------------------
// 증분 인덱서 (Python index/indexer.py)
//=> 폴더의 문서를 추출 → 청킹 → 임베딩 → 저장소·BM25 에 넣는다.
//    · 변경 감지: (경로, mtime, size) sha1 지문 — 내용 해시는 지문이 바뀐 파일만 구한다
//    · 수정 문서: 새 청크를 먼저 넣고 옛 청크를 지운다(중간에 끊겨도 문서가 사라지지 않게)
//    · 이동(경로만 바뀜) / 복사(같은 파일을 붙여 넣음)는 다시 임베딩하지 않는다
//    · 지워진 문서는 안전 확인(대량 삭제 멈춤) 뒤 뺀다
//    · 바뀐 게 있으면 BM25 를 새로 만들고 고아 점을 청소한다
//   상태 파일(index_state.json)은 Python 판과 같은 모양이다.
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::Instant;

use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

use crate::bm25::Bm25Index;
use crate::chunk::{split_blocks, split_structured, split_text};
use crate::config::cfg;
use crate::embed::Embedder;
use crate::pyutil;
use crate::store::{Payload, VectorStore};

pub const TEXT_EXTS: &[&str] = &[".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".hwp", ".hwpx", ".txt", ".md", ".html", ".htm", ".py", ".ipynb"];

// 블록 추출·청킹 결과가 달라지는 코드 변경마다 올린다(Python 판과 같은 값)
pub const BLOCKS_VERSION: i64 = 5;

pub type State = Map<String, Value>;

//------------------------------------------------------------------
// 저장하는 동안 잠깐 생기는 파일인가 — "~$" · ".~lock." 이름, 숨김·시스템 속성
//------------------------------------------------------------------
pub fn is_transient(path: &str) -> bool {
    let name = pyutil::basename(path);
    if name.starts_with("~$") || name.starts_with(".~lock.") {
        return true;
    }
    use std::os::windows::fs::MetadataExt;
    match std::fs::metadata(path) {
        Ok(m) => m.file_attributes() & 0x6 != 0,
        Err(_) => false,
    }
}

//------------------------------------------------------------------
// 대상 파일 나열 — (정렬된 경로, 제외된 개수)
//------------------------------------------------------------------
pub fn list_files(root: &str, recursive: bool) -> std::io::Result<(Vec<String>, usize)> {
    let mut files = vec![];
    let mut skipped = 0;
    let mut take = |p: String| {
        if TEXT_EXTS.contains(&pyutil::ext_lower(&p).as_str()) && !is_transient(&p) {
            files.push(p);
        } else {
            skipped += 1;
        }
    };
    if recursive {
        // os.walk 순서(폴더 안 이름 순)는 결과 정렬로 상관없어진다
        let mut stack = vec![root.to_string()];
        while let Some(d) = stack.pop() {
            let rd = match std::fs::read_dir(&d) {
                Ok(r) => r,
                Err(e) => {
                    if d == root {
                        return Err(e);
                    }
                    continue;
                }
            };
            for e in rd.flatten() {
                let p = pyutil::join(&d, &e.file_name().to_string_lossy());
                match e.file_type() {
                    Ok(t) if t.is_dir() => stack.push(p),
                    Ok(_) => take(p),
                    Err(_) => {}
                }
            }
        }
    } else {
        for e in std::fs::read_dir(root)?.flatten() {
            let p = pyutil::join(root, &e.file_name().to_string_lossy());
            if std::path::Path::new(&p).is_file() {
                take(p);
            }
        }
    }
    files.sort();
    Ok((files, skipped))
}

//------------------------------------------------------------------
// 파일 지문 — sha1("절대경로|mtime|size")
//------------------------------------------------------------------
pub fn file_fingerprint(path: &str) -> std::io::Result<String> {
    let m = std::fs::metadata(path)?;
    let raw = format!("{}|{}|{}", pyutil::abspath(path), pyutil::mtime_int(path)?, m.len());
    Ok(hex(&Sha1::digest(raw.as_bytes())))
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{:02x}", x)).collect()
}

//------------------------------------------------------------------
// 파일 내용 해시(sha1)
//------------------------------------------------------------------
pub fn content_hash(path: &str) -> std::io::Result<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha1::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex(&h.finalize()))
}

// 경로 비교용 표기
pub fn norm_path(path: &str) -> String {
    pyutil::normcase(&pyutil::abspath(path))
}

// ── 상태 파일 ─────────────────────────────────────────────

pub fn empty_state() -> State {
    let mut s = Map::new();
    s.insert("docs".into(), json!({}));
    s.insert("next_id".into(), json!(0));
    s
}

//------------------------------------------------------------------
// 상태 파일 읽기 — 없거나 깨졌으면 빈 상태
//------------------------------------------------------------------
pub fn load_state() -> State {
    let path = &cfg().state_path;
    match std::fs::read(path) {
        Ok(b) => match serde_json::from_slice::<Value>(&b) {
            Ok(Value::Object(m)) => m,
            _ => empty_state(),
        },
        Err(_) => empty_state(),
    }
}

//------------------------------------------------------------------
// 상태 파일 쓰기(원자적, 들여쓰기 2, 한글 그대로)
//------------------------------------------------------------------
pub fn save_state(state: &State) -> Result<(), String> {
    let s = serde_json::to_string_pretty(&Value::Object(state.clone())).map_err(|e| e.to_string())?;
    pyutil::write_atomic(&cfg().state_path, s.as_bytes()).map_err(|e| e.to_string())
}

// 상태 안 docs 사전
pub fn docs(state: &State) -> &Map<String, Value> {
    static EMPTY: once_cell::sync::Lazy<Map<String, Value>> = once_cell::sync::Lazy::new(Map::new);
    state.get("docs").and_then(|v| v.as_object()).unwrap_or(&EMPTY)
}
pub fn docs_mut(state: &mut State) -> &mut Map<String, Value> {
    if !state.get("docs").map(|v| v.is_object()).unwrap_or(false) {
        state.insert("docs".into(), json!({}));
    }
    state.get_mut("docs").unwrap().as_object_mut().unwrap()
}
fn failed_mut(state: &mut State) -> &mut Map<String, Value> {
    if !state.get("failed").map(|v| v.is_object()).unwrap_or(false) {
        state.insert("failed".into(), json!({}));
    }
    state.get_mut("failed").unwrap().as_object_mut().unwrap()
}
fn failed_pop(state: &mut State, key: &str) {
    if let Some(f) = state.get_mut("failed").and_then(|v| v.as_object_mut()) {
        f.remove(key);
    }
}
fn next_id(state: &State) -> i64 {
    state.get("next_id").and_then(|v| v.as_i64()).unwrap_or(0)
}
// 문서 항목의 ids 범위 [시작, 끝)
pub fn id_range(meta: &Value) -> Option<(i64, i64)> {
    let a = meta.get("ids")?.as_array()?;
    if a.len() == 2 {
        Some((a[0].as_i64()?, a[1].as_i64()?))
    } else {
        None
    }
}

//------------------------------------------------------------------
// 지금 설정의 청킹 파라미터(인덱스에 기록해 섞임을 막는다)
//------------------------------------------------------------------
pub fn chunk_params() -> Value {
    let c = cfg();
    let mut p = Map::new();
    p.insert("tokens".into(), json!(c.chunk_tokens));
    p.insert("overlap".into(), json!(c.chunk_overlap));
    p.insert("mode".into(), json!(c.chunk_mode));
    p.insert("min_tokens".into(), json!(c.min_chunk_tokens));
    p.insert("table_header_max_tokens".into(), json!(c.table_header_max_tokens));
    p.insert("chunker_version".into(), json!(crate::config::CHUNKER_VERSION));
    if c.extract_engine == "blocks" {
        p.insert("extract_engine".into(), json!("blocks"));
        p.insert("overflow_ratio".into(), pyutil::jf(c.chunk_overflow_ratio));
        p.insert("sentence_split".into(), json!(c.chunk_sentence_split));
        p.insert("hard_max".into(), json!(c.chunk_hard_max));
        p.insert("blocks_version".into(), json!(BLOCKS_VERSION));
        if c.pdf_infer_headings {
            p.insert("pdf_headings".into(), json!(true));
        }
    }
    Value::Object(p)
}

// 파이썬 dict 비교처럼 — 1 과 1.0 은 같다
fn val_eq(a: Option<&Value>, b: Option<&Value>) -> bool {
    match (a, b) {
        (Some(Value::Number(x)), Some(Value::Number(y))) => x.as_f64() == y.as_f64(),
        (x, y) => x == y,
    }
}
pub fn params_eq(a: &Value, b: &Value) -> bool {
    match (a.as_object(), b.as_object()) {
        (Some(x), Some(y)) => x.len() == y.len() && x.iter().all(|(k, v)| val_eq(Some(v), y.get(k))),
        _ => a == b,
    }
}
// 파이썬 str(값) 흉내(오류문용)
fn pyshow(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::Bool(b)) => if *b { "True".into() } else { "False".into() },
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => if n.is_f64() { pyutil::float_repr(n.as_f64().unwrap()) } else { n.to_string() },
        Some(x) => x.to_string(),
    }
}
pub fn show_params(v: &Value) -> String {
    match v.as_object() {
        Some(m) => format!("{{{}}}", m.iter().map(|(k, x)| format!("'{}': {}", k, match x {
            Value::String(s) => format!("'{}'", s),
            other => pyshow(Some(other)),
        })).collect::<Vec<_>>().join(", ")),
        None => pyshow(Some(v)),
    }
}

//------------------------------------------------------------------
// 인덱스의 청킹 설정과 지금 설정 비교
// -out: error = 기록과 다르면 Err(RuntimeError 문장)
//------------------------------------------------------------------
pub fn check_chunk_params(state: &mut State, rebuild: bool, log: &mut dyn FnMut(&str)) -> Result<Value, String> {
    let params = chunk_params();
    let recorded = state.get("chunk_params").cloned().filter(|v| !v.is_null());
    if rebuild || docs(state).is_empty() {
        state.insert("chunk_params".into(), params.clone());
        return Ok(params);
    }
    let rec = match recorded {
        None => {
            log(&format!("  ⚠️ 이 인덱스에는 청킹 설정 기록이 없습니다(이전 버전에서 만듦). 지금 설정({}토큰/겹침 {}/{}/머리글 상한 {})과 다르게 만들어졌다면 --rebuild 하세요.",
                params["tokens"], params["overlap"], params["mode"].as_str().unwrap_or(""), params["table_header_max_tokens"]));
            return Ok(params);
        }
        Some(r) => r,
    };
    if !params_eq(&rec, &params) {
        let diff: Vec<String> = params.as_object().unwrap().iter()
            .filter(|(k, v)| !val_eq(rec.get(k.as_str()), Some(v)))
            .map(|(k, v)| format!("{} {}→{}", k, pyshow(rec.get(k.as_str())), pyshow(Some(v)))).collect();
        return Err(format!("청킹 설정이 인덱스와 다릅니다({}). 이대로 증분 인덱싱하면 옛 청크와 섞입니다 — index --dir <폴더> --rebuild 로 전체 재구축하세요.", diff.join(", ")));
    }
    Ok(params)
}

// 진행률(최근 20개 표본 속도로 ETA)
struct Progress {
    total: usize,
    samples: std::collections::VecDeque<(Instant, usize)>,
    t0: Instant,
}
impl Progress {
    fn new(total: usize) -> Self {
        Progress { total, samples: Default::default(), t0: Instant::now() }
    }
    fn update(&mut self, done: usize) -> (f64, f64) {
        let now = Instant::now();
        self.samples.push_back((now, done));
        if self.samples.len() > 20 {
            self.samples.pop_front();
        }
        let rate = if self.samples.len() >= 2 {
            let (ta, da) = self.samples[0];
            let (tb, db) = *self.samples.back().unwrap();
            (db - da) as f64 / (tb - ta).as_secs_f64().max(1e-9)
        } else {
            done as f64 / (now - self.t0).as_secs_f64().max(1e-9)
        };
        let eta = if rate > 0.0 { (self.total - done) as f64 / rate } else { 0.0 };
        (rate, eta)
    }
}

//------------------------------------------------------------------
// 문서 한 건 반영 결과
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct SyncResult {
    pub ok: bool,
    pub op: String,
    pub chunks: i64,
    pub why: String,
    pub kept_old: bool,
    pub n_rows: usize,
    pub from: Option<String>,
}

// blocks 엔진으로 한 문서 추출·청킹 → (전체 글자, 표 행 수, 청크 메타)
//------------------------------------------------------------------
// 파일 하나를 청크로 (sync_doc 과 같은 추출·청킹) — 업로드 문서(uploads.rs)가 쓴다
//   돌려줌: [(청크 글, section_id)] — 비면 빈 목록
//------------------------------------------------------------------
pub fn extract_chunks(path: &str, name: &str, tok: &crate::tok::Tok) -> Result<Vec<(String, Option<i64>)>, String> {
    if cfg().extract_engine == "blocks" {
        let (_text, _rows, metas) = extract_blocks(path, name, tok)?;
        return Ok(metas.into_iter().filter(|m| !m.text.is_empty()).map(|m| (m.text, m.section_id)).collect());
    }
    let (text, _rows) = crate::extract::extract_text(path, true)?;
    let chunks = if cfg().chunk_mode == "structured" {
        split_structured(&text, tok, Some(pyutil::splitext(name).0), None, None)
    } else {
        split_text(&text, tok, None, None)
    };
    Ok(chunks.into_iter().filter(|c| !c.is_empty()).map(|c| (c, None)).collect())
}

fn extract_blocks(path: &str, name: &str, tok: &crate::tok::Tok) -> Result<(String, usize, Vec<crate::chunk::ChunkMeta>), String> {
    let title = pyutil::splitext(name).0.to_string();
    let mut doc = crate::docmodel::extract_document(path, Some(&title))?;
    if cfg().extract_dump {
        let _ = crate::docmodel::dump_json(&doc, &pyutil::join(&cfg().root, "blocks_dump"), name);
    }
    let text = doc.full_text();
    let n_rows: usize = doc.blocks.iter().filter(|b| b.kind == crate::docmodel::blocks::TABLE).map(|b| b.rows.as_ref().map(|r| r.len()).unwrap_or(0)).sum();
    let metas = if cfg().chunk_mode == "structured" {
        split_blocks(&mut doc, tok, None)
    } else {
        split_text(&text, tok, None, None).into_iter().map(|t| crate::chunk::ChunkMeta {
            text: t, section_id: None, path: vec![], source: String::new(), page_from: None, page_to: None }).collect()
    };
    Ok((text, n_rows, metas))
}

// 오류 종류 이름을 붙인 사유(파이썬 "{}: {}".format(type(e).__name__, …) 모양)
fn why_of(e: &str) -> String {
    let has_kind = e.split_once(": ").map(|(k, _)| k.ends_with("Error") && !k.contains(' ')).unwrap_or(false);
    let full = if has_kind { e.to_string() } else { format!("ExtractError: {}", e) };
    let (k, m) = full.split_once(": ").unwrap_or(("Error", &full));
    format!("{}: {}", k, pyutil::head(m, 80))
}

//------------------------------------------------------------------
// 문서 한 건 반영 — 추가·수정 공통 (자동 인덱싱 설계서 §5)
//------------------------------------------------------------------
pub fn sync_doc(path: &str, fp: &str, state: &mut State, embedder: &Embedder, store: &VectorStore,
                log: &mut dyn FnMut(&str)) -> Result<SyncResult, String> {
    let key = pyutil::abspath(path);
    let name = pyutil::basename(path).to_string();
    let prev = docs(state).get(&key).cloned();
    let op = if prev.is_some() { "modified" } else { "added" };
    // ── 1) 내용이 같은가
    let chash = match content_hash(path) {
        Ok(h) => h,
        Err(e) => {
            return Ok(SyncResult { ok: false, op: op.into(), kept_old: prev.is_some(),
                why: format!("읽기 실패 {}: {}", "OSError", pyutil::head(&e.to_string(), 70)), ..Default::default() });
        }
    };
    if let Some(p) = &prev {
        if p.get("content_hash").and_then(|v| v.as_str()) == Some(chash.as_str()) {
            let m = docs_mut(state).get_mut(&key).unwrap().as_object_mut().unwrap();
            m.insert("fingerprint".into(), json!(fp));
            m.insert("mtime".into(), json!(pyutil::mtime_int(path).unwrap_or(0)));
            failed_pop(state, &key);
            save_state(state)?;
            return Ok(SyncResult { ok: true, op: "same".into(), chunks: p.get("chunks").and_then(|v| v.as_i64()).unwrap_or(0), ..Default::default() });
        }
    }
    if prev.is_none() {
        if let Some(old) = moved_from(state, path, &chash) {
            log(&format!("  [이동] {} → {}", old, key));
            return move_doc(&old, path, fp, state, store);
        }
        if let Some(src) = copied_from(state, path, &chash) {
            if let Some(r) = copy_doc(&src, path, fp, state, store)? {
                log(&format!("  [복사] {} → {}", src, key));
                return Ok(r);
            }
        }
    }
    // ── 2) 새 버전 만들기(인덱스는 아직 그대로)
    let tok = embedder.tokenizer()?;
    let made: Result<(String, usize, Vec<String>, Option<Vec<crate::chunk::ChunkMeta>>, Vec<Vec<f32>>), String> = (|| {
        let (text, n_rows, chunks, metas) = if cfg().extract_engine == "blocks" {
            let (text, n_rows, metas) = extract_blocks(path, &name, &tok)?;
            let chunks: Vec<String> = metas.iter().map(|m| m.text.clone()).collect();
            (text, n_rows, chunks, Some(metas))
        } else {
            let (text, n_rows) = crate::extract::extract_text(path, true)?;
            let chunks = if cfg().chunk_mode == "structured" {
                split_structured(&text, &tok, Some(pyutil::splitext(&name).0), None, None)
            } else {
                split_text(&text, &tok, None, None)
            };
            (text, n_rows, chunks, None)
        };
        if chunks.is_empty() {
            return Err("ValueError: 추출 텍스트 없음".into());
        }
        let vecs = embedder.embed_passages(&chunks)?;
        Ok((text, n_rows, chunks, metas, vecs))
    })();
    let (text, n_rows, chunks, metas, vecs) = match made {
        Ok(x) => x,
        Err(e) => {
            let why = why_of(&e);
            failed_mut(state).insert(key.clone(), json!({"fingerprint": fp, "why": why, "name": name}));
            save_state(state)?;
            return Ok(SyncResult { ok: false, op: op.into(), why, kept_old: prev.is_some(), ..Default::default() });
        }
    };
    // 회귀 감지 — 글자 수 대비 청크가 너무 적으면 경고
    let expect = std::cmp::max(1, pyutil::clen(&text) as i64 / (cfg().chunk_tokens * 3));
    if (chunks.len() as i64) < expect / 2 {
        log(&format!("  ⚠️ {} — 청크 {}개는 문자수({}) 대비 비정상적으로 적습니다. 토크나이저 절단 여부를 확인하세요.",
            name, chunks.len(), pyutil::comma(pyutil::clen(&text) as i64)));
    }
    // ── 3) id 예약 — 상태 파일에 먼저
    let start = next_id(state);
    let ids: Vec<i64> = (start..start + chunks.len() as i64).collect();
    state.insert("next_id".into(), json!(start + chunks.len() as i64));
    save_state(state)?;
    // ── 4) 새 청크 넣기
    let mtime = pyutil::mtime_int(path).unwrap_or(0);
    let folder = pyutil::basename(&pyutil::dirname(path)).to_string();
    let mut payloads: Vec<Payload> = vec![];
    for (i, c) in chunks.iter().enumerate() {
        let mut p = Map::new();
        p.insert("text".into(), json!(c));
        p.insert("doc_path".into(), json!(key));
        p.insert("doc_name".into(), json!(name));
        p.insert("folder".into(), json!(folder));
        p.insert("chunk_idx".into(), json!(i));
        p.insert("mtime".into(), json!(mtime));
        p.insert("gen".into(), json!(fp));
        p.insert("content_hash".into(), json!(chash));
        if let Some(ms) = &metas {
            let m = &ms[i];
            if let Some(s) = m.section_id {
                p.insert("section_id".into(), json!(s));
            }
            if !m.path.is_empty() {
                p.insert("path".into(), json!(m.path));
            }
            if !m.source.is_empty() {
                p.insert("source".into(), json!(m.source));
            }
            if let Some(x) = m.page_from {
                p.insert("page_from".into(), json!(x));
            }
            if let Some(x) = m.page_to {
                p.insert("page_to".into(), json!(x));
            }
        }
        payloads.push(p);
    }
    store.upsert(&ids, &vecs, &payloads)?;
    // ── 5) 방금 넣은 것 말고 이 문서의 청크를 모두 지운다
    store.delete_doc_except(&key, &ids)?;
    // ── 6) 상태 파일을 새 버전으로
    docs_mut(state).insert(key.clone(), json!({
        "fingerprint": fp, "content_hash": chash, "chunks": chunks.len(),
        "ids": [start, start + chunks.len() as i64], "name": name, "folder": folder, "mtime": mtime,
    }));
    failed_pop(state, &key);
    mark_changed(state);
    save_state(state)?;
    Ok(SyncResult { ok: true, op: op.into(), chunks: chunks.len() as i64, n_rows, ..Default::default() })
}

//------------------------------------------------------------------
// 문서 한 건 빼기 — 지운 청크 수(상태 파일 기준)
//------------------------------------------------------------------
pub fn remove_doc(key: &str, state: &mut State, store: &VectorStore) -> Result<i64, String> {
    let n = docs(state).get(key).and_then(|m| m.get("chunks")).and_then(|v| v.as_i64()).unwrap_or(0);
    store.delete_doc(key)?;
    docs_mut(state).remove(key);
    failed_pop(state, key);
    mark_changed(state);
    save_state(state)?;
    Ok(n)
}

pub fn mark_changed(state: &mut State) {
    state.insert("index_changed_at".into(), pyutil::jf(pyutil::now_ts()));
}

//------------------------------------------------------------------
// BM25 가 인덱스보다 오래됐나
//------------------------------------------------------------------
pub fn bm25_stale(state: &State) -> bool {
    let changed = state.get("index_changed_at").and_then(|v| v.as_f64()).unwrap_or(0.0);
    let built = state.get("bm25_built_at").and_then(|v| v.as_f64());
    changed != 0.0 && built.map(|b| b < changed).unwrap_or(true)
}

//------------------------------------------------------------------
// 사라진 문서 찾기 — 이 폴더 아래만, 실제로도 없는 것만
//------------------------------------------------------------------
pub fn find_removed(state: &State, root: &str, present: &HashSet<String>, recursive: bool) -> Vec<String> {
    let base = norm_path(root);
    let prefix = format!("{}\\", pyutil::rstrip_chars(&base, "\\/"));
    let mut out = vec![];
    for key in docs(state).keys() {
        let k = norm_path(key);
        if recursive {
            if !k.starts_with(&prefix) {
                continue;
            }
        } else if pyutil::dirname(&k) != base {
            continue;
        }
        if present.contains(&k) || std::path::Path::new(key).exists() {
            continue;
        }
        out.push(key.clone());
    }
    out.sort();
    out
}

//------------------------------------------------------------------
// 지워도 안전한가 — (ok, 사유)
//------------------------------------------------------------------
pub fn delete_guard(n_removed: usize, n_known: usize) -> (bool, String) {
    let (limit_pct, limit_n) = (30usize, 50usize);
    if n_removed == 0 {
        return (true, String::new());
    }
    if n_removed >= limit_n {
        return (false, format!("{}건이 한꺼번에 사라졌습니다(한도 {}건)", n_removed, limit_n));
    }
    if n_removed >= 5 && n_known > 0 && n_removed * 100 >= limit_pct * n_known {
        return (false, format!("문서 {}건 중 {}건({:.0}%)이 한꺼번에 사라졌습니다(한도 {}%)",
            n_known, n_removed, n_removed as f64 * 100.0 / n_known as f64, limit_pct));
    }
    (true, String::new())
}

//------------------------------------------------------------------
// 할 일 목록 (index 명령과 워커 /index-plan 공용)
//------------------------------------------------------------------
pub struct Plan {
    pub files: Vec<String>,
    pub skipped: usize,
    pub todo: Vec<(String, String)>,
    pub removed: Vec<String>,
    pub moved: Vec<(String, String)>,
    pub delete_blocked: String,
    pub retry_skip: usize,
}

pub fn plan_changes(doc_dir: &str, state: &State, recursive: bool, allow_delete: bool, rebuild: bool) -> Result<Plan, String> {
    let (files, skipped) = list_files(doc_dir, recursive).map_err(|e| e.to_string())?;
    let failed_before = state.get("failed").and_then(|v| v.as_object()).cloned().unwrap_or_default();
    let mut todo = vec![];
    let mut n_retry = 0;
    for path in &files {
        let fp = match file_fingerprint(path) {
            Ok(f) => f,
            Err(_) => continue,
        };
        let key = pyutil::abspath(path);
        if docs(state).get(&key).and_then(|m| m.get("fingerprint")).and_then(|v| v.as_str()) == Some(fp.as_str()) {
            continue;
        }
        if failed_before.get(&key).and_then(|m| m.get("fingerprint")).and_then(|v| v.as_str()) == Some(fp.as_str()) {
            n_retry += 1;
            continue;
        }
        todo.push((path.clone(), fp));
    }
    let present: HashSet<String> = files.iter().map(|p| norm_path(p)).collect();
    let missing = if rebuild { vec![] } else { find_removed(state, doc_dir, &present, recursive) };
    let (moved, dup) = find_moves(state, &missing, &files);
    let mut removed: Vec<String> = missing.iter().filter(|k| !moved.iter().any(|(o, _)| o == *k) && !dup.contains(k)).cloned().collect();
    let base = format!("{}\\", pyutil::rstrip_chars(&norm_path(doc_dir), "\\/"));
    let n_known = docs(state).keys().filter(|k| norm_path(k).starts_with(&base)).count();
    let mut delete_blocked = String::new();
    if !removed.is_empty() && !allow_delete {
        let (ok, why) = delete_guard(removed.len(), n_known);
        if !ok {
            delete_blocked = why;
            removed.clear();
        }
    }
    removed.extend(dup);
    removed.sort();
    Ok(Plan { files, skipped, todo, removed, moved, delete_blocked, retry_skip: n_retry })
}

//------------------------------------------------------------------
// 옮긴 문서 찾기 — (이동 [(옛 키, 새 경로)], 중복 [옛 키])
//=> 사라진 문서의 (내용 해시, 파일 이름) 과 같은 파일이 지금 폴더에 있으면 이동.
//------------------------------------------------------------------
pub fn find_moves(state: &State, missing: &[String], files: &[String]) -> (Vec<(String, String)>, Vec<String>) {
    let mut want: HashMap<(String, String), Vec<String>> = HashMap::new();
    for key in missing {
        if let Some(h) = docs(state).get(key).and_then(|m| m.get("content_hash")).and_then(|v| v.as_str()) {
            want.entry((h.to_string(), pyutil::basename(key).to_lowercase())).or_default().push(key.clone());
        }
    }
    if want.is_empty() {
        return (vec![], vec![]);
    }
    let names: HashSet<String> = want.keys().map(|(_, n)| n.clone()).collect();
    let indexed: HashSet<String> = docs(state).keys().map(|k| norm_path(k)).collect();
    let mut moved = vec![];
    let mut dup = vec![];
    for path in files {
        let name = pyutil::basename(path).to_lowercase();
        if !names.contains(&name) {
            continue;
        }
        let h = match content_hash(path) {
            Ok(h) => h,
            Err(_) => continue,
        };
        let olds = match want.get_mut(&(h, name)) {
            Some(o) if !o.is_empty() => o,
            _ => continue,
        };
        let old = olds.remove(0);
        if indexed.contains(&norm_path(path)) {
            dup.push(old);
        } else {
            moved.push((old, path.clone()));
        }
    }
    (moved, dup)
}

// 이 파일이 어디서 옮겨 왔는가(옛 자리는 이제 없음)
pub fn moved_from(state: &State, path: &str, chash: &str) -> Option<String> {
    let name = pyutil::basename(path).to_lowercase();
    let me = norm_path(path);
    docs(state).iter().find(|(k, d)| d.get("content_hash").and_then(|v| v.as_str()) == Some(chash)
        && pyutil::basename(k).to_lowercase() == name && norm_path(k) != me && !std::path::Path::new(k.as_str()).exists())
        .map(|(k, _)| k.clone())
}

// 이 파일이 어느 문서의 복사본인가(원본이 그대로 있음)
pub fn copied_from(state: &State, path: &str, chash: &str) -> Option<String> {
    let name = pyutil::basename(path).to_lowercase();
    let me = norm_path(path);
    docs(state).iter().find(|(k, d)| d.get("content_hash").and_then(|v| v.as_str()) == Some(chash)
        && pyutil::basename(k).to_lowercase() == name && norm_path(k) != me
        && std::path::Path::new(k.as_str()).exists() && d.get("ids").map(|v| !v.is_null()).unwrap_or(false))
        .map(|(k, _)| k.clone())
}

//------------------------------------------------------------------
// 복사본 넣기 — 원본 벡터·청크를 새 id 로 베낀다
//------------------------------------------------------------------
pub fn copy_doc(src_key: &str, path: &str, fp: &str, state: &mut State, store: &VectorStore) -> Result<Option<SyncResult>, String> {
    let src = docs(state).get(src_key).cloned().unwrap_or(Value::Null);
    let (a, b) = id_range(&src).unwrap_or((0, 0));
    let ids_src: Vec<i64> = (a..b).collect();
    let (vecs, mut payloads) = match store.points_of(&ids_src)? {
        Some(x) if !x.1.is_empty() => x,
        _ => return Ok(None),
    };
    let key = pyutil::abspath(path);
    let name = pyutil::basename(path).to_string();
    let folder = pyutil::basename(&pyutil::dirname(path)).to_string();
    let mtime = pyutil::mtime_int(path).unwrap_or(0);
    let start = next_id(state);
    let n = payloads.len() as i64;
    let ids: Vec<i64> = (start..start + n).collect();
    state.insert("next_id".into(), json!(start + n));
    save_state(state)?;
    for p in payloads.iter_mut() {
        p.insert("doc_path".into(), json!(key));
        p.insert("doc_name".into(), json!(name));
        p.insert("folder".into(), json!(folder));
        p.insert("mtime".into(), json!(mtime));
        p.insert("gen".into(), json!(fp));
    }
    store.upsert(&ids, &vecs, &payloads)?;
    store.delete_doc_except(&key, &ids)?;
    let mut entry = src.as_object().cloned().unwrap_or_default();
    entry.insert("fingerprint".into(), json!(fp));
    entry.insert("ids".into(), json!([start, start + n]));
    entry.insert("name".into(), json!(name));
    entry.insert("folder".into(), json!(folder));
    entry.insert("mtime".into(), json!(mtime));
    docs_mut(state).insert(key.clone(), Value::Object(entry));
    failed_pop(state, &key);
    mark_changed(state);
    save_state(state)?;
    Ok(Some(SyncResult { ok: true, op: "copied".into(), chunks: n, from: Some(src_key.to_string()), ..Default::default() }))
}

//------------------------------------------------------------------
// 옮긴 문서 — 경로 정보만 바꾼다
//------------------------------------------------------------------
pub fn move_doc(old_key: &str, path: &str, fp: &str, state: &mut State, store: &VectorStore) -> Result<SyncResult, String> {
    let key = pyutil::abspath(path);
    let name = pyutil::basename(path).to_string();
    let folder = pyutil::basename(&pyutil::dirname(path)).to_string();
    let mtime = pyutil::mtime_int(path).unwrap_or(0);
    let mut entry = docs(state).get(old_key).and_then(|v| v.as_object()).cloned().unwrap_or_default();
    let (a, b) = id_range(&Value::Object(entry.clone())).unwrap_or((0, 0));
    let mut pl = Map::new();
    pl.insert("doc_path".into(), json!(key));
    pl.insert("doc_name".into(), json!(name));
    pl.insert("folder".into(), json!(folder));
    pl.insert("mtime".into(), json!(mtime));
    pl.insert("gen".into(), json!(fp));
    store.set_payload_ids(&(a..b).collect::<Vec<_>>(), &pl)?;
    entry.insert("fingerprint".into(), json!(fp));
    entry.insert("name".into(), json!(name));
    entry.insert("folder".into(), json!(folder));
    entry.insert("mtime".into(), json!(mtime));
    let chunks = entry.get("chunks").and_then(|v| v.as_i64()).unwrap_or(0);
    docs_mut(state).remove(old_key);
    docs_mut(state).insert(key.clone(), Value::Object(entry));
    failed_pop(state, &key);
    save_state(state)?;
    Ok(SyncResult { ok: true, op: "moved".into(), chunks, from: Some(old_key.to_string()), ..Default::default() })
}

//------------------------------------------------------------------
// 폴더 인덱싱 요약
//------------------------------------------------------------------
#[derive(Debug, Default)]
pub struct Summary {
    pub files: usize,
    pub changed: usize,
    pub counts: BTreeMap<String, usize>,
    pub removed: usize,
    pub delete_blocked: String,
    pub held_new: usize,
    pub chunks: i64,
    pub failed: Vec<(String, String)>,
    pub sec: f64,
}

impl Summary {
    pub fn c(&self, k: &str) -> usize {
        *self.counts.get(k).unwrap_or(&0)
    }
}

//------------------------------------------------------------------
// 폴더 인덱싱 (핵심)
// -out: error = 폴더 없음·청킹 설정 불일치는 Err(사유)
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
pub fn index_folder(doc_dir: &str, embedder: &Embedder, store: &VectorStore, bm25: &Bm25Index, rebuild: bool,
                    log: &mut dyn FnMut(&str), recursive: bool, allow_delete: bool, max_add: Option<usize>) -> Result<Summary, String> {
    if !std::path::Path::new(doc_dir).is_dir() {
        return Err(format!("폴더 없음: {}", doc_dir));
    }
    embedder.ensure_loaded()?;
    let mut state = if rebuild { empty_state() } else { load_state() };
    check_chunk_params(&mut state, rebuild, log)?;
    if rebuild {
        store.recreate()?;
    } else {
        store.ensure_loaded()?;
    }
    let plan = plan_changes(doc_dir, &state, recursive, allow_delete, rebuild)?;
    let mut todo = plan.todo.clone();
    if plan.skipped > 0 {
        log(&format!("제외 {}건(확장자·임시 파일 등)", plan.skipped));
    }
    if !plan.delete_blocked.is_empty() {
        log(&format!("  ⚠️ {} — 지우지 않았습니다. 정말 지운 것이면 --allow-delete 로 다시 실행하세요.", plan.delete_blocked));
    }
    let mut held_new = 0;
    let moved_to: HashSet<String> = plan.moved.iter().map(|(_, p)| norm_path(p)).collect();
    if let Some(max) = max_add {
        let is_old = |p: &str, st: &State| docs(st).contains_key(&pyutil::abspath(p)) || moved_to.contains(&norm_path(p));
        let adds = todo.iter().filter(|t| !is_old(&t.0, &state)).count();
        if adds > max {
            held_new = adds;
            todo.retain(|t| is_old(&t.0, &state));
            log(&format!("  새 문서 {}건은 한도({}건)를 넘어 이번에는 넣지 않았습니다", held_new, max));
        }
    }
    log(&format!("문서 {}건 중 {}건 처리 대상, 사라진 문서 {}건", plan.files.len(), todo.len(), plan.removed.len()));
    if plan.retry_skip > 0 {
        log(&format!("  지난번 실패 뒤 바뀌지 않아 건너뛴 문서 {}건", plan.retry_skip));
    }
    let t_start = Instant::now();
    let mut prog = Progress::new(todo.len().max(1));
    let mut sum = Summary { files: plan.files.len(), ..Default::default() };
    let mut n_removed = 0;
    for key in &plan.removed {
        let n = remove_doc(key, &mut state, store)?;
        n_removed += 1;
        log(&format!("  [삭제] {:<48} {:>4}청크", pyutil::head(pyutil::basename(key), 46), n));
    }
    let total = todo.len();
    for (i, (path, fp)) in todo.iter().enumerate() {
        let n = i + 1;
        let name = pyutil::basename(path).to_string();
        let r = sync_doc(path, fp, &mut state, embedder, store, log)?;
        if !r.ok {
            sum.failed.push((name, format!("{}{}", r.why, if r.kept_old { " (옛 버전 유지)" } else { "" })));
            continue;
        }
        *sum.counts.entry(r.op.clone()).or_default() += 1;
        if r.op == "same" {
            log(&format!("  [{}/{}] {:<48} 내용 같음 — 다시 임베딩하지 않음", n, total, pyutil::head(&name, 46)));
            continue;
        }
        if r.op == "moved" {
            log(&format!("  [{}/{}] [이동] {:<44} 경로만 바꿈 — 다시 임베딩하지 않음", n, total, pyutil::head(&name, 42)));
            continue;
        }
        sum.chunks += r.chunks;
        let (rate, eta) = prog.update(n);
        let label = match r.op.as_str() { "added" => "추가", "copied" => "복사", _ => "수정" };
        let table_note = if r.n_rows > 0 { format!("  표{}행", r.n_rows) } else { String::new() };
        log(&format!("  [{}/{}] [{}] {:<44} {:>4}청크  ({:.1} 문서/s, 남은시간 {:.0}s){}",
            n, total, label, pyutil::head(&name, 42), r.chunks, rate, eta, table_note));
    }
    if sum.c("added") > 0 || sum.c("modified") > 0 || sum.c("copied") > 0 || n_removed > 0 || rebuild || bm25_stale(&state) {
        log("BM25 인덱스 구축 중...");
        let (texts, ids, n_orphans) = collect_for_bm25(store, Some(&state), log)?;
        if n_orphans > 0 {
            log(&format!("  고아 청크 {}개를 지웠습니다(중간에 끊겼던 작업의 찌꺼기)", n_orphans));
        }
        let tok = embedder.tokenizer()?;
        let build_s = if texts.is_empty() { 0.0 } else { bm25.build(&texts, &ids, &tok)? };
        state.insert("bm25_built_at".into(), pyutil::jf(pyutil::now_ts()));
        save_state(&state)?;
        log(&format!("BM25 인덱스 {}건 구축 {:.1}s", pyutil::comma(texts.len() as i64), build_s));
    }
    for (name, why) in &sum.failed {
        log(&format!("  ❌ {} — {}", name, why));
    }
    sum.changed = todo.len() + n_removed;
    sum.removed = n_removed;
    sum.delete_blocked = plan.delete_blocked;
    sum.held_new = held_new;
    sum.sec = pyutil::round_to(t_start.elapsed().as_secs_f64(), 1);
    Ok(sum)
}

//------------------------------------------------------------------
// 저장소 전체 청크 회수(BM25 재구축) + 고아 청소 — (본문, id, 지운 고아 수)
//=> 고아 = 상태 파일에 없는 문서의 점, 또는 기록된 id 범위 밖의 점.
//   고아가 전체의 30% 를 넘으면 상태 파일이 깨졌을 수 있어 지우지 않는다.
//------------------------------------------------------------------
pub fn collect_for_bm25(store: &VectorStore, state: Option<&State>, log: &mut dyn FnMut(&str)) -> Result<(Vec<String>, Vec<i64>, usize), String> {
    let points = store.scroll_all()?;
    let mut orphans: HashSet<i64> = HashSet::new();
    if let Some(st) = state {
        let d = docs(st);
        if !d.is_empty() {
            for (id, _, doc_path) in &points {
                match d.get(doc_path) {
                    None => {
                        orphans.insert(*id);
                    }
                    Some(meta) => {
                        if let Some((a, b)) = id_range(meta) {
                            if !(a <= *id && *id < b) {
                                orphans.insert(*id);
                            }
                        }
                    }
                }
            }
            if !orphans.is_empty() && orphans.len() * 100 > 30 * points.len() {
                log(&format!("  ⚠️ 상태 파일과 맞지 않는 청크가 {}/{}개입니다. 상태 파일이 깨졌을 수 있어 지우지 않았습니다 — `status` 로 확인하세요.", orphans.len(), points.len()));
                orphans.clear();
            } else if !orphans.is_empty() {
                let mut v: Vec<i64> = orphans.iter().copied().collect();
                v.sort();
                store.delete_ids(&v)?;
            }
        }
    }
    let mut texts = vec![];
    let mut ids = vec![];
    for (id, text, _) in points {
        if orphans.contains(&id) {
            continue;
        }
        texts.push(text);
        ids.push(id);
    }
    Ok((texts, ids, orphans.len()))
}
