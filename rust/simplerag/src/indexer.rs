//------------------------------------------------------------------
// 증분 인덱서 (Python index/indexer.py)
//=> 폴더의 문서를 추출 → 청킹 → 임베딩 → 저장소·BM25 에 넣는다.
//    · 변경 감지: (경로, mtime, size) sha1 지문 — 내용 해시는 지문이 바뀐 파일만 구한다
//    · 수정 문서: 새 청크를 먼저 넣고 옛 청크를 지운다(중간에 끊겨도 문서가 사라지지 않게)
//    · 이동(경로만 바뀜) / 복사(같은 파일을 붙여 넣음)는 다시 임베딩하지 않는다
//    · 지워진 문서는 안전 확인(대량 삭제 멈춤) 뒤 뺀다
//    · 바뀐 게 있으면 BM25 를 고치고(바뀐 청크만 — 인덱싱 속도 개선 설계서 §4) 고아 점을 청소한다
//    · 수정 문서는 글이 그대로인 청크의 벡터를 다시 쓴다(청크 해시 비교 — §4-5)
//    · 다음 문서의 파싱·청킹을 준비 스레드가 임베딩과 겹쳐 한다(§3-1)
//    · 새로 임베딩할 청크가 많으면 벡터를 비워 두고 BM25 로 먼저 검색되게 한 뒤 나중에 채운다(§3-5)
//   상태 파일(index_state.json)은 Python 판과 같은 모양이다.
//------------------------------------------------------------------

use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::Instant;

use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

use crate::bm25::{self, Bm25Index, Tf};
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
    // SIMPLERAG_INDEX_EXCLUDE — ; 로 나눈 제외 목록(대소문자 무시). 말뭉치처럼 문서가 아닌 큰 파일 · 폴더를 뺄 때
    //   \ 나 / 가 든 항목은 폴더(그 아래 전부), 아니면 파일 이름
    let exclude: Vec<String> = std::env::var("SIMPLERAG_INDEX_EXCLUDE").unwrap_or_default()
        .split(';').map(|s| s.trim().replace('/', "\\").trim_end_matches('\\').to_lowercase()).filter(|s| !s.is_empty()).collect();
    let excluded = |p: &str| {
        let low = p.replace('/', "\\").to_lowercase();
        let name = pyutil::basename(&low).to_string();
        exclude.iter().any(|e| if e.contains('\\') { low.starts_with(&format!("{}\\", e)) } else { name == *e })
    };
    let mut take = |p: String| {
        if TEXT_EXTS.contains(&pyutil::ext_lower(&p).as_str()) && !is_transient(&p) && !excluded(&p) {
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
// 청크 해시 — 청크 글(접두 포함, 임베딩에 넣는 글 그대로)의 sha1 16진수
//=> 수정 문서에서 글이 그대로인 청크를 찾아 벡터를 다시 쓴다(설계서 §4-5). 청크당 약 1μs.
//
// -in: text = 청크 글
//
// -out: 40글자 16진수
// -out: error = 없음
//------------------------------------------------------------------
pub fn chunk_hash(text: &str) -> String {
    hex(&Sha1::digest(text.as_bytes()))
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
// -필드: embedded = 이번에 새로 임베딩한 청크 수 / reused = 옛 벡터를 다시 쓴 청크 수 / deferred = 벡터 대기로 둔 청크 수
#[derive(Debug, Clone, Default)]
pub struct SyncResult {
    pub ok: bool,
    pub op: String,
    pub chunks: i64,
    pub why: String,
    pub kept_old: bool,
    pub n_rows: usize,
    pub from: Option<String>,
    pub embedded: usize,
    pub reused: usize,
    pub deferred: usize,
}

// 문서 하나를 추출·청킹한 결과 — (전체 글자, 표 행 수, 청크 글, 청크 메타)
type Parsed = (String, usize, Vec<String>, Option<Vec<crate::chunk::ChunkMeta>>);

//------------------------------------------------------------------
// 준비 스레드가 미리 만든 것 (설계서 §3-1)
// -필드: chash  = 내용 해시(읽기 실패면 Err 사유)
// -필드: parsed = 추출·청킹 결과. 내용이 같거나 옮긴·복사한 문서로 보이면 None(본 스레드가 판단)
//------------------------------------------------------------------
pub struct Prepared {
    pub chash: Result<String, String>,
    pub parsed: Option<Result<Parsed, String>>,
}

//------------------------------------------------------------------
// sync_doc 의 선택 사항
// -필드: defer    = true 면 새 청크의 벡터를 비워 둔다(단계형 인덱싱 1단계)
// -필드: prepared = 준비 스레드 결과(없으면 그 자리에서 추출·청킹)
//------------------------------------------------------------------
#[derive(Default)]
pub struct SyncOpts {
    pub defer: bool,
    pub prepared: Option<Prepared>,
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
            text: t, section_id: None, path: vec![], source: String::new(), page_from: None, page_to: None, page: None }).collect()
    };
    Ok((text, n_rows, metas))
}

//------------------------------------------------------------------
// 문서 하나 추출·청킹 (sync_doc 2단계의 앞부분 — 준비 스레드도 이것을 부른다)
//
// -in: path = 문서 경로 / name = 파일 이름 / tok = e5 토크나이저
//
// -out: (전체 글자, 표 행 수, 청크 글, 청크 메타)
// -out: error = 추출 실패 · 청크 없음이면 Err(사유)
//------------------------------------------------------------------
pub fn parse_doc(path: &str, name: &str, tok: &crate::tok::Tok) -> Result<Parsed, String> {
    let (text, n_rows, chunks, metas) = if cfg().extract_engine == "blocks" {
        let (text, n_rows, metas) = extract_blocks(path, name, tok)?;
        let chunks: Vec<String> = metas.iter().map(|m| m.text.clone()).collect();
        (text, n_rows, chunks, Some(metas))
    } else {
        let (text, n_rows) = crate::extract::extract_text(path, true)?;
        let chunks = if cfg().chunk_mode == "structured" {
            split_structured(&text, tok, Some(pyutil::splitext(name).0), None, None)
        } else {
            split_text(&text, tok, None, None)
        };
        (text, n_rows, chunks, None)
    };
    if chunks.is_empty() {
        return Err("ValueError: 추출 텍스트 없음".into());
    }
    Ok((text, n_rows, chunks, metas))
}

//------------------------------------------------------------------
// 수정 문서의 벡터 재사용 (설계서 §4-5)
//=> 다시 자른 청크 가운데 옛 버전과 글이 똑같은 청크는 옛 벡터를 그대로 쓴다.
//    1) 옛 청크마다 해시(payload chunk_hash, 없으면 본문으로 계산) → 점 id 목록
//    2) 새 청크 해시가 같으면 그 id 하나를 짝짓는다(같은 글이 여러 번이면 개수만큼만)
//    3) 짝지은 id 의 벡터를 저장소에서 꺼낸다(대기 중이던 청크는 빈 벡터 그대로 — 계속 대기)
//
// -in: store = 저장소 / old_ids = 옛 버전 점 id / hashes = 새 청크 해시
//
// -out: 새 청크마다 Some(옛 벡터) 또는 None(새로 임베딩)
// -out: error = 저장소 읽기 실패면 Err
//------------------------------------------------------------------
fn reuse_vectors(store: &VectorStore, old_ids: &[i64], hashes: &[String]) -> Result<Vec<Option<Vec<f32>>>, String> {
    let mut out: Vec<Option<Vec<f32>>> = vec![None; hashes.len()];
    if old_ids.is_empty() {
        return Ok(out);
    }
    let old = store.payloads(old_ids)?;
    let mut by_hash: HashMap<String, Vec<i64>> = HashMap::new();
    for id in old_ids {
        if let Some(p) = old.get(id) {
            // chunk_hash 가 없는 옛 인덱스는 남아 있는 본문으로 그 자리에서 계산한다
            let h = p.get("chunk_hash").and_then(|v| v.as_str()).map(|s| s.to_string())
                .unwrap_or_else(|| chunk_hash(crate::store::pl_str(p, "text")));
            by_hash.entry(h).or_default().push(*id);
        }
    }
    let mut pick: Vec<(usize, i64)> = vec![];
    for (i, h) in hashes.iter().enumerate() {
        if let Some(ids) = by_hash.get_mut(h) {
            if !ids.is_empty() {
                pick.push((i, ids.remove(0)));
            }
        }
    }
    if pick.is_empty() {
        return Ok(out);
    }
    let ids: Vec<i64> = pick.iter().map(|x| x.1).collect();
    if let Some((vecs, _)) = store.points_of(&ids)? {
        for ((i, _), v) in pick.iter().zip(vecs) {
            out[*i] = Some(v);
        }
    }
    Ok(out)
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
    sync_doc_with(path, fp, state, embedder, store, log, SyncOpts::default())
}

//------------------------------------------------------------------
// 문서 한 건 반영 — 선택 사항(준비 결과 · 벡터 대기)을 받는 본체
//=> sync_doc 과 같은 안전 순서(id 예약 → 넣기 → 옛 청크 지우기 → 상태 갱신)를 그대로 지킨다.
//   바뀐 점: 준비 스레드가 만든 추출·청킹을 쓰고, 옛 벡터를 다시 쓰고, 청크별 토큰 빈도를 함께 저장하고,
//   opts.defer 면 새 청크의 벡터를 비워 둔다(나중에 fill_pending 이 채운다).
//
// -in: path · fp · state · embedder · store · log = sync_doc 과 같음
// -in: opts = 준비 결과 · 벡터 대기 여부
//
// -out: SyncResult (embedded · reused · deferred 포함)
// -out: error = 추출·임베딩 실패는 ok=false, 저장소 · 상태 파일 쓰기 실패는 Err
//------------------------------------------------------------------
pub fn sync_doc_with(path: &str, fp: &str, state: &mut State, embedder: &Embedder, store: &VectorStore,
                     log: &mut dyn FnMut(&str), opts: SyncOpts) -> Result<SyncResult, String> {
    let key = pyutil::abspath(path);
    let name = pyutil::basename(path).to_string();
    let prev = docs(state).get(&key).cloned();
    let op = if prev.is_some() { "modified" } else { "added" };
    let (pre_hash, mut pre_parsed) = match opts.prepared {
        Some(p) => (Some(p.chash), p.parsed),
        None => (None, None),
    };
    // ── 1) 내용이 같은가
    let chash = match pre_hash.unwrap_or_else(|| content_hash(path).map_err(|e| e.to_string())) {
        Ok(h) => h,
        Err(e) => {
            return Ok(SyncResult { ok: false, op: op.into(), kept_old: prev.is_some(),
                why: format!("읽기 실패 {}: {}", "OSError", pyutil::head(&e, 70)), ..Default::default() });
        }
    };
    if let Some(p) = &prev {
        if p.get("content_hash").and_then(|v| v.as_str()) == Some(chash.as_str()) {
            let m = docs_mut(state).get_mut(&key).unwrap().as_object_mut().unwrap();
            m.insert("fingerprint".into(), json!(fp));
            m.insert("mtime".into(), json!(pyutil::mtime_int(path).unwrap_or(0)));
            failed_pop(state, &key);
            save_state(state)?;
            // 내용은 같아도 파일 시스템 날짜는 바뀌었다 — 메타는 다시 읽는다(수십 ms)
            crate::meta::hook::synced(&key, Some(&chash));
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
    // 옛 버전의 점 id — 글이 같은 청크의 벡터를 다시 쓴다(설계서 §4-5)
    let old_ids: Vec<i64> = prev.as_ref().and_then(id_range).map(|(a, b)| (a..b).collect()).unwrap_or_default();
    let made: Result<(String, usize, Vec<String>, Option<Vec<crate::chunk::ChunkMeta>>, Vec<Vec<f32>>, Vec<String>, usize, usize), String> = (|| {
        // 준비 스레드가 이미 만들었으면 그것을 쓴다(결과는 그 자리에서 만든 것과 같다)
        let (text, n_rows, chunks, metas) = match pre_parsed.take() {
            Some(p) => p?,
            None => parse_doc(path, &name, &tok)?,
        };
        let hashes: Vec<String> = chunks.iter().map(|c| chunk_hash(c)).collect();
        let reused = reuse_vectors(store, &old_ids, &hashes)?;
        let n_reused = reused.iter().filter(|v| v.is_some()).count();
        // 다시 쓸 벡터가 없는 청크만 임베딩한다 — 대기면 빈 벡터로 둔다
        let need: Vec<String> = chunks.iter().zip(&reused).filter(|(_, r)| r.is_none()).map(|(c, _)| c.clone()).collect();
        let fresh = if opts.defer { vec![vec![]; need.len()] } else { embedder.embed_passages(&need)? };
        let mut fresh = fresh.into_iter();
        let vecs: Vec<Vec<f32>> = reused.into_iter().map(|r| r.unwrap_or_else(|| fresh.next().unwrap_or_default())).collect();
        Ok((text, n_rows, chunks, metas, vecs, hashes, n_reused, need.len()))
    })();
    let (text, n_rows, chunks, metas, vecs, hashes, n_reused, n_new) = match made {
        Ok(x) => x,
        Err(e) => {
            let why = why_of(&e);
            failed_mut(state).insert(key.clone(), json!({"fingerprint": fp, "why": why, "name": name}));
            save_state(state)?;
            // 본문을 못 뽑은 문서(스캔 PDF 등)도 이름 · 날짜 · 사람으로는 찾을 수 있게 메타는 남긴다
            crate::meta::hook::synced(&key, Some(&chash));
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
        // 청크 글의 해시 — 다음에 이 문서가 바뀌면 같은 글인 청크를 찾는 데 쓴다
        p.insert("chunk_hash".into(), json!(hashes[i]));
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
            // 청크가 실제로 시작하는 쪽 — 탐색기 모드가 "그 쪽에서 열기"에 쓴다
            if let Some(x) = m.page {
                p.insert("page".into(), json!(x));
            }
        }
        payloads.push(p);
    }
    store.upsert(&ids, &vecs, &payloads)?;
    // 청크별 단어 빈도도 함께 남긴다 — BM25 를 고칠 때 토큰화하지 않게(설계서 §4-3, 100청크에 수십 ms)
    let texts: Vec<&str> = chunks.iter().map(|c| c.as_str()).collect();
    let blobs: Vec<Vec<u8>> = tok.term_freqs_par(&texts, 1).iter().map(bm25::tf_to_blob).collect();
    store.put_tf(&ids, &blobs)?;
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
    crate::meta::hook::synced(&key, Some(&chash));
    let (embedded, deferred) = if opts.defer { (0, n_new) } else { (n_new, 0) };
    Ok(SyncResult { ok: true, op: op.into(), chunks: chunks.len() as i64, n_rows, embedded, reused: n_reused, deferred, ..Default::default() })
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
    crate::meta::hook::removed(key);
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
    // 원본의 토큰 표도 새 id 로 베낀다(원본이 벡터 대기면 사본도 빈 벡터 그대로 — 나중에 함께 채워진다)
    let tfs = store.tf_of(Some(&ids_src))?;
    let (dst, blobs): (Vec<i64>, Vec<Vec<u8>>) = ids_src.iter().zip(&ids).filter_map(|(s, d)| tfs.get(s).map(|b| (*d, b.clone()))).unzip();
    store.put_tf(&dst, &blobs)?;
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
    // 메타도 원본을 베끼고 경로 · 파일 시스템 정보만 새로 읽는다
    crate::meta::hook::copied(src_key, &key, src.get("content_hash").and_then(|v| v.as_str()));
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
    crate::meta::hook::moved(old_key, &key);
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
    // 단계형 인덱싱 — 벡터 대기로 남은 청크 수(0 이면 모두 벡터가 있다)
    pub pending: usize,
    // 수정 문서에서 옛 벡터를 다시 쓴 청크 수 · 새로 임베딩한 청크 수
    pub reused: usize,
    pub embedded: usize,
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
    // 메타데이터의 폴더 토큰은 이 인덱싱 폴더 아래 경로로 잡는다
    crate::meta::hook::set_base(Some(doc_dir));
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
    let tok = embedder.tokenizer()?;
    // 단계형 인덱싱 문턱(§3-6) — 이번 실행에서 새로 임베딩할 청크가 이만큼 쌓인 뒤로는 벡터를 비워 두고
    //   BM25 로 먼저 검색되게 한다. 문서 몇 건(자동 인덱싱)은 문턱에 닿지 않아 지금처럼 바로 임베딩한다.
    let defer_over = if cfg().defer_vectors_over > 0 { Some(cfg().defer_vectors_over as usize) } else { None };
    let mut fresh_chunks = 0usize;
    // 준비 스레드(§3-1) — 문서가 두 건 이상일 때만. 내용이 같거나 옮긴·복사한 문서는 파싱하지 않게
    //   지금 상태의 내용 해시를 넘겨 둔다(이번 실행 중 생긴 복사본은 파싱이 헛되어도 결과는 같다)
    let use_prefetch = cfg().embed_prefetch && total >= 2;
    let known: HashMap<String, String> = docs(&state).iter()
        .filter_map(|(k, m)| m.get("content_hash").and_then(|v| v.as_str()).map(|h| (k.clone(), h.to_string()))).collect();
    let known_hashes: HashSet<String> = known.values().cloned().collect();
    std::thread::scope(|sc| -> Result<(), String> {
        let rx = if use_prefetch {
            // 크기 0 통로 — 준비 스레드는 한 건만 앞서 간다(메모리 상한)
            let (tx, rx) = std::sync::mpsc::sync_channel::<Prepared>(0);
            let (todo_r, known_r, hashes_r, tok_r) = (&todo, &known, &known_hashes, tok.clone());
            sc.spawn(move || {
                for (path, _) in todo_r {
                    // 본 스레드가 멈췄으면(오류) 통로가 닫혀 여기서 끝난다
                    if tx.send(prepare_doc(path, &tok_r, known_r, hashes_r)).is_err() {
                        break;
                    }
                }
            });
            Some(rx)
        } else {
            None
        };
        for (i, (path, fp)) in todo.iter().enumerate() {
            let n = i + 1;
            let name = pyutil::basename(path).to_string();
            let prepared = rx.as_ref().and_then(|r| r.recv().ok());
            let defer = defer_over.map(|o| fresh_chunks >= o).unwrap_or(false);
            let r = sync_doc_with(path, fp, &mut state, embedder, store, log, SyncOpts { defer, prepared })?;
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
            sum.reused += r.reused;
            sum.embedded += r.embedded;
            fresh_chunks += r.embedded + r.deferred;
            let (rate, eta) = prog.update(n);
            let label = match r.op.as_str() { "added" => "추가", "copied" => "복사", _ => "수정" };
            let mut note = if r.n_rows > 0 { format!("  표{}행", r.n_rows) } else { String::new() };
            if r.reused > 0 {
                note += &format!("  벡터 재사용 {}/{}", r.reused, r.chunks);
            }
            if r.deferred > 0 {
                note += "  벡터 대기";
            }
            log(&format!("  [{}/{}] [{}] {:<44} {:>4}청크  ({:.1} 문서/s, 남은시간 {:.0}s){}",
                n, total, label, pyutil::head(&name, 42), r.chunks, rate, eta, note));
        }
        Ok(())
    })?;
    // 옛 형식(v2) · 깨진 BM25 파일은 바뀐 게 없어도 한 번 새로 만든다
    let bm25_missing = !docs(&state).is_empty() && bm25.ensure_loaded() < 0.0;
    if sum.c("added") > 0 || sum.c("modified") > 0 || sum.c("copied") > 0 || n_removed > 0 || rebuild || bm25_stale(&state) || bm25_missing {
        log("BM25 인덱스 반영 중...");
        let r = refresh_bm25(store, bm25, Some(&state), &tok, rebuild, log)?;
        if r.orphans > 0 {
            log(&format!("  고아 청크 {}개를 지웠습니다(중간에 끊겼던 작업의 찌꺼기)", r.orphans));
        }
        state.insert("bm25_built_at".into(), pyutil::jf(pyutil::now_ts()));
        save_state(&state)?;
        log(&r.line());
    }
    // ── 메타데이터 마무리 — 빠진 문서 채우기 · 정리 · 템플릿 상속 판정(전체를 봐야 한다) ──
    crate::meta::hook::finish(&state, log);
    for (name, why) in &sum.failed {
        log(&format!("  ❌ {} — {}", name, why));
    }
    sum.pending = store.pending_count()?;
    if sum.pending > 0 {
        log(&format!("검색 가능 — 벡터 채우는 중 0 / {} (그동안은 BM25 · 동의어로 찾습니다)", pyutil::comma(sum.pending as i64)));
    }
    sum.changed = todo.len() + n_removed;
    sum.removed = n_removed;
    sum.delete_blocked = plan.delete_blocked;
    sum.held_new = held_new;
    sum.sec = pyutil::round_to(t_start.elapsed().as_secs_f64(), 1);
    Ok(sum)
}

//------------------------------------------------------------------
// 준비 스레드의 문서 한 건 (설계서 §3-1)
//=> 다음 문서의 내용 해시 · 추출 · 청킹을 임베딩과 겹쳐 미리 해 둔다.
//    1) 내용 해시를 구한다
//    2) 상태 파일에 같은 경로 · 같은 내용이 있으면(내용 같음), 처음 보는 경로인데 같은 내용이
//       다른 자리에 있으면(이동 · 복사 후보) 파싱하지 않는다 — 본 스레드가 판단한다
//    3) 그 밖에는 추출 · 청킹까지 한다(실패도 결과로 넘긴다 — 본 스레드가 실패로 기록)
//
// -in: path = 문서 경로 / tok = e5 토크나이저 / known = {문서 키: 내용 해시} / known_hashes = 내용 해시 모음
//
// -out: Prepared
// -out: error = 없음 (실패는 Prepared 안의 Err)
//------------------------------------------------------------------
fn prepare_doc(path: &str, tok: &crate::tok::Tok, known: &HashMap<String, String>, known_hashes: &HashSet<String>) -> Prepared {
    let key = pyutil::abspath(path);
    let chash = content_hash(path).map_err(|e| e.to_string());
    let parsed = match &chash {
        Err(_) => None,
        Ok(h) => {
            let same = known.get(&key) == Some(h);
            let move_or_copy = !known.contains_key(&key) && known_hashes.contains(h);
            if same || move_or_copy {
                None
            } else {
                Some(parse_doc(path, pyutil::basename(path), tok))
            }
        }
    };
    Prepared { chash, parsed }
}

//------------------------------------------------------------------
// BM25 반영 결과
// -필드: mode = "full"(전체 다시 만듦 · 합치기) | "update"(덧붙임 · 지움만) | "same"(바뀐 것 없음)
//------------------------------------------------------------------
#[derive(Debug, Default)]
pub struct Bm25Report {
    pub chunks: usize,
    pub sec: f64,
    pub orphans: usize,
    pub mode: &'static str,
    pub added: usize,
    pub removed: usize,
    pub tokenized: usize,
}

impl Bm25Report {
    // 진행 출력 한 줄
    pub fn line(&self) -> String {
        match self.mode {
            "update" => format!("BM25 인덱스 {}건 — 바뀐 청크만 반영(추가 {} · 삭제 {}) {:.2}s",
                pyutil::comma(self.chunks as i64), self.added, self.removed, self.sec),
            "same" => format!("BM25 인덱스 {}건 — 바뀐 청크 없음", pyutil::comma(self.chunks as i64)),
            _ => format!("BM25 인덱스 {}건 구축 {:.1}s{}", pyutil::comma(self.chunks as i64), self.sec,
                if self.tokenized > 0 { format!(" (토큰화 {}건)", pyutil::comma(self.tokenized as i64)) } else { String::new() }),
        }
    }
}

//------------------------------------------------------------------
// 점들의 단어 빈도 모으기 — 토큰 표에서 읽고, 없는 것만 토큰화해 채운다(설계서 §4-3)
//=> 옛 인덱스(표가 없음)는 처음 한 번 빠진 청크만 토큰화한다 — 스레드 여러 개로(단계 A).
//
// -in: store = 저장소 / ids = 점 id 목록 / tok = e5 토크나이저 / all = true 면 표 전체를 한 번에 읽는다
//
// -out: (ids 순서의 단어 빈도, 새로 토큰화한 수)
// -out: error = 저장소 읽기 · 쓰기 실패면 Err
//------------------------------------------------------------------
fn tfs_for(store: &VectorStore, ids: &[i64], tok: &crate::tok::Tok, all: bool) -> Result<(Vec<Tf>, usize), String> {
    let mut have = store.tf_of(if all { None } else { Some(ids) })?;
    let missing: Vec<i64> = ids.iter().copied().filter(|i| have.get(i).and_then(|b| bm25::tf_from_blob(b)).is_none()).collect();
    if !missing.is_empty() {
        let texts = store.texts_of(&missing)?;
        let refs: Vec<&str> = texts.iter().map(|s| s.as_str()).collect();
        let tfs = tok.term_freqs_par(&refs, crate::config::bm25_threads());
        let blobs: Vec<Vec<u8>> = tfs.iter().map(bm25::tf_to_blob).collect();
        store.put_tf(&missing, &blobs)?;
        for (id, b) in missing.iter().zip(blobs) {
            have.insert(*id, b);
        }
    }
    let out = ids.iter().map(|i| have.get(i).and_then(|b| bm25::tf_from_blob(b)).unwrap_or_default()).collect();
    Ok((out, missing.len()))
}

//------------------------------------------------------------------
// BM25 반영 (핵심 — 설계서 §4 단계 A~C)
//=> 저장소와 색인을 맞대어 바뀐 청크만 고친다. 많이 바뀌었거나 색인이 없으면 전체를 다시 만든다.
//    1) 고아 점 청소(상태 파일에 없는 문서 · id 범위 밖)
//    2) 색인이 있고 full 이 아니면: 넣을 id · 뺄 id 를 구해, 합칠 때가 아니면 덧붙임 · 지움만 고친다
//    3) 아니면 모든 점의 단어 빈도(토큰 표)로 본 세그먼트를 새로 만든다 — 토큰화 없이
//
// -in: store · bm25 = 저장소 · 색인 / state = 상태(None 이면 고아 청소 안 함) / tok = e5 토크나이저
// -in: full = true 면 전체 다시 만들기(--rebuild) / log = 안내 출력
//
// -out: Bm25Report
// -out: error = 저장소 · 파일 쓰기 실패면 Err
//------------------------------------------------------------------
pub fn refresh_bm25(store: &VectorStore, bm25: &Bm25Index, state: Option<&State>, tok: &crate::tok::Tok, full: bool,
                    log: &mut dyn FnMut(&str)) -> Result<Bm25Report, String> {
    let t0 = Instant::now();
    let orphans = clean_orphans(store, state, log)?;
    let live = store.ids()?;
    let mut rep = Bm25Report { chunks: live.len(), orphans, ..Default::default() };
    if !full && !live.is_empty() {
        if let Some((add, rem)) = bm25.plan_update(&live) {
            if add.is_empty() && rem.is_empty() {
                rep.mode = "same";
                rep.sec = t0.elapsed().as_secs_f64();
                return Ok(rep);
            }
            if !bm25.should_merge(add.len(), rem.len()) {
                let (tfs, n_tok) = tfs_for(store, &add, tok, false)?;
                bm25.update(add.iter().copied().zip(tfs).collect(), &rem)?;
                rep.mode = "update";
                rep.added = add.len();
                rep.removed = rem.len();
                rep.tokenized = n_tok;
                rep.sec = t0.elapsed().as_secs_f64();
                return Ok(rep);
            }
        }
    }
    // 전체 — 처음 · --rebuild · 합치기 · 옛 형식
    let (tfs, n_tok) = tfs_for(store, &live, tok, true)?;
    bm25.build_tf(&live, &tfs)?;
    // 지운 청크의 토큰 표 찌꺼기 정리(옛 인덱스 · 끊긴 작업)
    store.prune_tf()?;
    rep.mode = "full";
    rep.tokenized = n_tok;
    rep.sec = t0.elapsed().as_secs_f64();
    Ok(rep)
}

//------------------------------------------------------------------
// 벡터 대기 청크 채우기 (단계형 인덱싱 2단계 — 설계서 §3-5-5)
//=> 먼저 넣은 문서부터(id 순) 64개씩 임베딩해 벡터를 채운다. 끊겨도 대기 청크는 저장소에 남으므로
//   다음 index 나 워커가 이어 채운다.
//   배치(8개)는 문서 경계를 넘지 않고 문서의 첫 대기 청크부터 8개씩 묶는다 — 바로 임베딩할 때와
//   묶음이 같아 벡터가 바이트까지 같다(int8 모델은 함께 묶인 글에 따라 벡터가 미세하게 달라진다).
//    1) 대기 청크를 64개 꺼내 문서별로 나눈다
//    2) 꺼낸 묶음이 꽉 찼으면 마지막 문서는 다음 차례로 이어지므로 8의 배수까지만 채운다
//
// -in: embedder · store = 임베더 · 저장소
// -in: max = Some(n) 이면 최대 n 개만(워커 /embed-pending), None 이면 다 채울 때까지
// -in: on_progress = 64개마다 (이번에 채운 수, 남은 수)
//
// -out: (채운 수, 남은 수)
// -out: error = 임베딩 · 저장 실패면 Err
//------------------------------------------------------------------
pub fn fill_pending(embedder: &Embedder, store: &VectorStore, max: Option<usize>,
                    on_progress: &mut dyn FnMut(usize, usize)) -> Result<(usize, usize), String> {
    const STEP: usize = 64;
    let mut done = 0;
    loop {
        let want = match max {
            Some(m) => m.saturating_sub(done).min(STEP),
            None => STEP,
        };
        if want == 0 {
            break;
        }
        let batch = store.pending_batch(want)?;
        if batch.is_empty() {
            break;
        }
        let full = batch.len() == want;
        // 문서별로 나눈다(id 순이라 같은 문서는 붙어 있다)
        let mut groups: Vec<Vec<(i64, String)>> = vec![];
        let mut last_doc: Option<String> = None;
        for (id, text, doc) in batch {
            if last_doc.as_deref() != Some(doc.as_str()) {
                groups.push(vec![]);
                last_doc = Some(doc);
            }
            groups.last_mut().unwrap().push((id, text));
        }
        // 마지막 문서는 다음 차례로 이어질 수 있다 — 8 의 배수까지만(남은 것은 다음에 그 자리부터 8개씩)
        if full && groups.len() > 1 {
            let g = groups.last_mut().unwrap();
            let keep = g.len() / crate::config::EMBED_BATCH * crate::config::EMBED_BATCH;
            g.truncate(keep);
        }
        for g in groups.into_iter().filter(|g| !g.is_empty()) {
            let (ids, texts): (Vec<i64>, Vec<String>) = g.into_iter().unzip();
            let vecs = embedder.embed_passages(&texts)?;
            store.set_vectors(&ids, &vecs)?;
            done += ids.len();
        }
        on_progress(done, store.pending_count()?);
    }
    Ok((done, store.pending_count()?))
}

//------------------------------------------------------------------
// 고아 점 청소 — 지운 수
//=> 고아 = 상태 파일에 없는 문서의 점, 또는 기록된 id 범위 밖의 점(sync_doc 이 넣고 지우기 사이에서 끊김).
//   고아가 전체의 30% 를 넘으면 상태 파일이 깨졌을 수 있어 지우지 않는다.
//
// -in: store = 저장소 / state = 상태(None 이면 아무것도 안 함) / log = 안내 출력
//
// -out: 지운 고아 수
// -out: error = 저장소 쓰기 실패면 Err
//------------------------------------------------------------------
pub fn clean_orphans(store: &VectorStore, state: Option<&State>, log: &mut dyn FnMut(&str)) -> Result<usize, String> {
    let st = match state {
        Some(s) if !docs(s).is_empty() => s,
        _ => return Ok(0),
    };
    let d = docs(st);
    let points = store.doc_paths()?;
    let mut orphans: Vec<i64> = vec![];
    for (id, doc_path) in &points {
        match d.get(doc_path) {
            None => orphans.push(*id),
            Some(meta) => {
                if let Some((a, b)) = id_range(meta) {
                    if !(a <= *id && *id < b) {
                        orphans.push(*id);
                    }
                }
            }
        }
    }
    if !orphans.is_empty() && orphans.len() * 100 > 30 * points.len() {
        log(&format!("  ⚠️ 상태 파일과 맞지 않는 청크가 {}/{}개입니다. 상태 파일이 깨졌을 수 있어 지우지 않았습니다 — `status` 로 확인하세요.", orphans.len(), points.len()));
        return Ok(0);
    }
    orphans.sort();
    store.delete_ids(&orphans)?;
    Ok(orphans.len())
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
