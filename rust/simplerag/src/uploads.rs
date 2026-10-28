//------------------------------------------------------------------
// 업로드 문서 인덱스 — 대화마다 따로 두는 작은 인덱스 (Python index/uploads.py 와 같다 · plan/문서업로드_설계서_20261028.html)
//=> RAGSearchBox 입력란의 [+] 로 올린 문서를 그 대화에서만 찾게 한다. 인덱싱 폴더의 인덱스(sqlite 벡터 · BM25 ·
//   index_state.json)와 섞지 않고 대화별 폴더 하나에 둔다 — 폴더째 지우면 끝이다(설계서 §3).
//
//   <데이터 폴더>/uploads/<대화 id>/
//     meta.json     기준 파일 — 판 · 임베딩 모델 · 차원 · 청킹 설정 · 올린 파일 목록
//     files/        올린 원본의 사본(<순번>_<원래 이름>)
//     chunks.jsonl  청크 한 줄씩 {id, file, text, doc_name, chunk_idx, section_id}
//     vectors.f32   청크 수 × 384 float32 리틀엔디언, L2 정규화
//   두 판 같은 형식 — 데이터 폴더가 같으면 판을 바꿔도 다시 임베딩하지 않는다.
//   검색은 전수 내적(수백 청크라 1ms 안팎) + 메모리 BM25 — UploadCorpus.
//------------------------------------------------------------------

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use once_cell::sync::Lazy;
use serde_json::{json, Value};

use crate::bm25::Bm25Index;
use crate::config::{cfg, EMBED_DIM};
use crate::embed::Embedder;
use crate::pyutil;
use crate::store::Payload;
use crate::tok::Tok;

pub const DIR_NAME: &str = "uploads";
const META: &str = "meta.json";
const CHUNKS: &str = "chunks.jsonl";
const VECTORS: &str = "vectors.f32";
const FILES: &str = "files";
const VERSION: i64 = 1;
// 대화 하나에 넣을 수 있는 청크 상한(설계서 §10)
pub const MAX_CHUNKS: usize = 3000;
// 워커 메모리에 둘 업로드 코퍼스 수(최근 것부터)
const CACHE_CONVS: usize = 3;
// 출처 카드의 폴더 칸에 보일 이름
pub const FOLDER_LABEL: &str = "업로드";

// 대화 id → (meta 수정 시각, 코퍼스) · 최근에 쓴 순서
static CACHE: Lazy<Mutex<(HashMap<String, (std::time::SystemTime, Arc<UploadCorpus>)>, Vec<String>)>> =
    Lazy::new(|| Mutex::new((HashMap::new(), vec![])));
// 쓰기(올리기·빼기·다시 만들기)를 한 번에 하나만
static WRITE: Lazy<Mutex<()>> = Lazy::new(|| Mutex::new(()));

//------------------------------------------------------------------
// 대화 id → 폴더 이름 (경로를 벗어나는 글자를 거른다 — Python safe_id 와 같다)
//------------------------------------------------------------------
pub fn safe_id(conv: &str) -> Option<String> {
    let s: String = conv.chars().map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' { c } else { '_' }).collect();
    let s = s.trim_matches(|c| c == '.' || c == '_').to_string();
    if s.is_empty() { None } else { Some(s) }
}

// 업로드 폴더(대화 id 가 없으면 uploads 폴더 자체)
pub fn upload_dir(conv: Option<&str>) -> Result<PathBuf, String> {
    let base = Path::new(&cfg().root).join(DIR_NAME);
    match conv {
        None => Ok(base),
        Some(c) => Ok(base.join(safe_id(c).ok_or("대화 id 가 없습니다")?)),
    }
}

// meta.json 읽기 — 없거나 깨졌거나 파일이 없으면 None
pub fn read_meta(conv: &str) -> Option<Value> {
    let p = upload_dir(Some(conv)).ok()?.join(META);
    let m: Value = serde_json::from_slice(&std::fs::read(p).ok()?).ok()?;
    if m.get("files").and_then(|f| f.as_array()).map(|a| !a.is_empty()).unwrap_or(false) { Some(m) } else { None }
}

fn embed_model() -> String {
    pyutil::basename(cfg().model_dir.trim_end_matches(['\\', '/'])).to_string()
}

// 지금 설정으로 만든 인덱스인가(임베딩 모델 · 차원 · 청킹 설정)
pub fn compatible(meta: &Value) -> bool {
    meta.get("version").and_then(|v| v.as_i64()) == Some(VERSION)
        && meta.get("dim").and_then(|v| v.as_u64()) == Some(EMBED_DIM as u64)
        && meta.get("embed_model").and_then(|v| v.as_str()) == Some(embed_model().as_str())
        && meta.get("chunk").map(|c| crate::indexer::params_eq(c, &crate::indexer::chunk_params())).unwrap_or(false)
}

fn new_meta(conv: &str) -> Value {
    json!({"version": VERSION, "conv": conv, "dim": EMBED_DIM, "embed_model": embed_model(),
           "chunk": crate::indexer::chunk_params(), "files": []})
}

// 청크·벡터 읽기
fn read_rows(d: &Path) -> Result<(Vec<Value>, Vec<Vec<f32>>), String> {
    let mut rows = vec![];
    if let Ok(t) = std::fs::read_to_string(d.join(CHUNKS)) {
        for l in t.lines().filter(|l| !l.trim().is_empty()) {
            rows.push(serde_json::from_str::<Value>(l).map_err(|e| format!("청크 줄이 깨졌습니다: {}", e))?);
        }
    }
    let mut vecs = vec![];
    if let Ok(b) = std::fs::read(d.join(VECTORS)) {
        for row in b.chunks_exact(EMBED_DIM * 4) {
            vecs.push(row.chunks_exact(4).map(|x| f32::from_le_bytes([x[0], x[1], x[2], x[3]])).collect());
        }
    }
    if rows.len() != vecs.len() {
        return Err(format!("청크 {}개와 벡터 {}개가 맞지 않습니다", rows.len(), vecs.len()));
    }
    Ok((rows, vecs))
}

// 청크·벡터·meta 쓰기 — *.tmp 에 쓰고 이름 바꾸기, meta 를 맨 마지막에(meta 가 기준)
fn write_all(d: &Path, rows: &[Value], vecs: &[Vec<f32>], meta: &Value) -> Result<(), String> {
    let mut t = String::new();
    for r in rows {
        t.push_str(&serde_json::to_string(r).unwrap());
        t.push('\n');
    }
    let mut b = Vec::with_capacity(vecs.len() * EMBED_DIM * 4);
    for v in vecs {
        for x in v {
            b.extend_from_slice(&x.to_le_bytes());
        }
    }
    let w = |name: &str, data: &[u8]| std::fs::write(d.join(format!("{}.tmp", name)), data).map_err(|e| format!("쓰지 못했습니다: {}", e));
    w(CHUNKS, t.as_bytes())?;
    w(VECTORS, &b)?;
    w(META, serde_json::to_string_pretty(meta).unwrap().as_bytes())?;
    for name in [CHUNKS, VECTORS, META] {
        std::fs::rename(d.join(format!("{}.tmp", name)), d.join(name)).map_err(|e| format!("바꾸지 못했습니다: {}", e))?;
    }
    Ok(())
}

// 진행 글을 stderr 로(RAGSearchBox 진행 막대용)
fn say(msg: &str) {
    eprintln!("{}", msg);
}

// 임베딩(배치마다 진행 글)
fn embed_with_progress(embedder: &Embedder, texts: &[String]) -> Result<Vec<Vec<f32>>, String> {
    let mut out = Vec::with_capacity(texts.len());
    say(&format!("[upload] 임베딩 0/{}", texts.len()));
    for part in texts.chunks(crate::config::EMBED_BATCH) {
        out.extend(embedder.embed_passages(part)?);
        say(&format!("[upload] 임베딩 {}/{}", out.len(), texts.len()));
    }
    Ok(out)
}

//------------------------------------------------------------------
// 문서 하나 올리기 (/upload — Python uploads.add 와 같다)
//   돌려줌: {"ok", "conv", "name", "chunks", "files", "ms"} 또는 {"ok": false, "why"}
//------------------------------------------------------------------
pub fn add(conv: &str, path: &str, embedder: &Embedder) -> Value {
    let t0 = Instant::now();
    let d = match upload_dir(Some(conv)) {
        Ok(d) => d,
        Err(e) => return json!({"ok": false, "why": e}),
    };
    let path = pyutil::abspath(path);
    let name = pyutil::basename(&path).to_string();
    let fail = |why: String| json!({"ok": false, "conv": conv, "name": name, "why": why});
    if !Path::new(&path).is_file() {
        return fail("파일이 없습니다".into());
    }
    let ext = pyutil::splitext(&name).1.to_lowercase();
    if !crate::indexer::TEXT_EXTS.contains(&ext.as_str()) || crate::indexer::is_transient(&path) {
        return fail("지원하지 않는 형식입니다".into());
    }
    let _g = WRITE.lock().unwrap();
    let mut meta = read_meta(conv);
    if let Some(m) = &meta {
        if !compatible(m) {
            // 설정이 바뀐 옛 인덱스 — 사본으로 다시 만든 뒤 이어 붙인다
            if let Err(e) = rebuild_locked(conv, embedder) {
                return fail(e);
            }
            meta = read_meta(conv);
        }
    }
    let mut meta = meta.unwrap_or_else(|| new_meta(conv));
    let had = meta["files"].as_array().map(|a| a.len()).unwrap_or(0);
    let _ = std::fs::create_dir_all(d.join(FILES));
    let (mut rows, mut vecs) = if had > 0 {
        match read_rows(&d) {
            Ok(x) => x,
            Err(e) => return fail(e),
        }
    } else {
        (vec![], vec![])
    };
    // 같은 이름을 두 번 올려도 겹치지 않게 사본 앞에 순번을 붙인다
    let seq = meta["files"].as_array().unwrap().iter().filter_map(|f| f.get("seq").and_then(|v| v.as_i64())).max().unwrap_or(0) + 1;
    let stored = format!("{}/{}_{}", FILES, seq, name);
    let stored_path = d.join(&stored);
    let made: Result<(Vec<(String, Option<i64>)>, Vec<Vec<f32>>), String> = (|| {
        std::fs::copy(&path, &stored_path).map_err(|e| format!("사본을 만들지 못했습니다: {}", e))?;
        say(&format!("[upload] 글자 뽑는 중 — {}", name));
        let tok = embedder.tokenizer()?;
        let chunks = crate::indexer::extract_chunks(&stored_path.to_string_lossy(), &name, &tok)?;
        if chunks.is_empty() {
            return Err("글자를 뽑지 못했습니다(스캔 문서는 아직 지원하지 않습니다)".into());
        }
        if rows.len() + chunks.len() > MAX_CHUNKS {
            return Err(format!("대화 하나에 넣을 수 있는 분량(청크 {}개)을 넘습니다", MAX_CHUNKS));
        }
        let texts: Vec<String> = chunks.iter().map(|c| c.0.clone()).collect();
        let v = embed_with_progress(embedder, &texts)?;
        Ok((chunks, v))
    })();
    let (chunks, new) = match made {
        Ok(x) => x,
        Err(e) => {
            let _ = std::fs::remove_file(&stored_path);
            if had == 0 {
                let _ = std::fs::remove_dir_all(&d);
            }
            return fail(e);
        }
    };
    let start = rows.len();
    for (i, (text, sid)) in chunks.iter().enumerate() {
        rows.push(json!({"id": start + i, "file": had, "text": text, "doc_name": name, "chunk_idx": i, "section_id": sid}));
    }
    vecs.extend(new);
    let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    meta["files"].as_array_mut().unwrap().push(json!({"name": name, "stored": stored, "seq": seq, "size": size,
        "chunks": [start, start + chunks.len()], "added": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()}));
    if let Err(e) = write_all(&d, &rows, &vecs, &meta) {
        return fail(e);
    }
    forget(Some(conv));
    json!({"ok": true, "conv": conv, "name": name, "chunks": chunks.len(), "files": had + 1, "ms": t0.elapsed().as_millis() as u64})
}

//------------------------------------------------------------------
// 문서 하나 빼기 (/upload-remove — 첨부 칩 [×]). 남은 파일이 없으면 폴더째 지운다
//------------------------------------------------------------------
pub fn remove(conv: &str, name: &str) -> Value {
    let _g = WRITE.lock().unwrap();
    let mut meta = match read_meta(conv) {
        Some(m) => m,
        None => return json!({"ok": false, "conv": conv, "name": name, "why": "올린 문서가 없습니다"}),
    };
    let files = meta["files"].as_array().unwrap().clone();
    let fi = match files.iter().rposition(|f| f.get("name").and_then(|v| v.as_str()) == Some(name)) {
        Some(i) => i,
        None => return json!({"ok": false, "conv": conv, "name": name, "why": "그 이름의 문서가 없습니다"}),
    };
    let d = match upload_dir(Some(conv)) {
        Ok(d) => d,
        Err(e) => return json!({"ok": false, "why": e}),
    };
    if files.len() == 1 {
        delete(conv);
        return json!({"ok": true, "conv": conv, "name": name, "files": 0});
    }
    let (rows, vecs) = match read_rows(&d) {
        Ok(x) => x,
        Err(e) => return json!({"ok": false, "conv": conv, "name": name, "why": e}),
    };
    let mut new_rows = vec![];
    let mut new_vecs = vec![];
    for (r, v) in rows.into_iter().zip(vecs) {
        let f = r["file"].as_u64().unwrap_or(0) as usize;
        if f == fi {
            continue;
        }
        let mut r = r;
        r["id"] = json!(new_rows.len());
        r["file"] = json!(if f > fi { f - 1 } else { f });
        new_rows.push(r);
        new_vecs.push(v);
    }
    let gone = meta["files"].as_array_mut().unwrap().remove(fi);
    // 파일별 청크 범위를 다시 센다
    for (k, f) in meta["files"].as_array_mut().unwrap().iter_mut().enumerate() {
        let ids: Vec<u64> = new_rows.iter().filter(|r| r["file"].as_u64() == Some(k as u64)).filter_map(|r| r["id"].as_u64()).collect();
        f["chunks"] = if ids.is_empty() { json!([0, 0]) } else { json!([ids[0], ids[ids.len() - 1] + 1]) };
    }
    if let Err(e) = write_all(&d, &new_rows, &new_vecs, &meta) {
        return json!({"ok": false, "conv": conv, "name": name, "why": e});
    }
    if let Some(s) = gone.get("stored").and_then(|v| v.as_str()) {
        let _ = std::fs::remove_file(d.join(s));
    }
    forget(Some(conv));
    json!({"ok": true, "conv": conv, "name": name, "files": meta["files"].as_array().unwrap().len()})
}

// 설정이 바뀐 업로드 인덱스를 사본으로 다시 만들기(WRITE 를 쥔 채로 부른다)
fn rebuild_locked(conv: &str, embedder: &Embedder) -> Result<(), String> {
    let meta = read_meta(conv).ok_or("올린 문서가 없습니다")?;
    let d = upload_dir(Some(conv))?;
    let tok = embedder.tokenizer()?;
    let mut new_meta = new_meta(conv);
    let (mut rows, mut vecs) = (vec![], vec![]);
    for f in meta["files"].as_array().unwrap() {
        let name = f["name"].as_str().unwrap_or("");
        let src = d.join(f["stored"].as_str().unwrap_or(""));
        say(&format!("[upload] 다시 만드는 중 — {}", name));
        let chunks = crate::indexer::extract_chunks(&src.to_string_lossy(), name, &tok)
            .map_err(|e| format!("다시 만들지 못했습니다 — {}", e))?;
        if chunks.is_empty() {
            continue;
        }
        let texts: Vec<String> = chunks.iter().map(|c| c.0.clone()).collect();
        let v = embedder.embed_passages(&texts)?;
        let (start, fi) = (rows.len(), new_meta["files"].as_array().unwrap().len());
        for (i, (text, sid)) in chunks.iter().enumerate() {
            rows.push(json!({"id": start + i, "file": fi, "text": text, "doc_name": name, "chunk_idx": i, "section_id": sid}));
        }
        vecs.extend(v);
        let mut nf = f.clone();
        nf["chunks"] = json!([start, start + chunks.len()]);
        new_meta["files"].as_array_mut().unwrap().push(nf);
    }
    if new_meta["files"].as_array().unwrap().is_empty() {
        let _ = std::fs::remove_dir_all(&d);
        forget(Some(conv));
        return Err("다시 만들 문서가 없습니다".into());
    }
    write_all(&d, &rows, &vecs, &new_meta)?;
    forget(Some(conv));
    Ok(())
}

// 대화 폴더 지우기(보통은 RAGSearchBox 가 직접 지운다)
pub fn delete(conv: &str) {
    if let Ok(d) = upload_dir(Some(conv)) {
        let _ = std::fs::remove_dir_all(d);
    }
    forget(Some(conv));
}

// 메모리 캐시 비우기 (/upload-forget) — None 이면 전부
pub fn forget(conv: Option<&str>) {
    let mut g = CACHE.lock().unwrap();
    match conv.and_then(safe_id) {
        None => {
            g.0.clear();
            g.1.clear();
        }
        Some(sid) => {
            g.0.remove(&sid);
            g.1.retain(|x| x != &sid);
        }
    }
}

//------------------------------------------------------------------
// 업로드 코퍼스 — 한 대화의 청크·벡터·BM25 (검색 1차 후보용, Python UploadCorpus 와 같다)
//------------------------------------------------------------------
pub struct UploadCorpus {
    pub conv: String,
    pub n_files: usize,
    pub vecs: Vec<Vec<f32>>,
    pub payloads: HashMap<i64, Payload>,
    pub bm25: Bm25Index,
}

impl UploadCorpus {
    fn new(conv: &str, meta: &Value, rows: Vec<Value>, vecs: Vec<Vec<f32>>, tok: &Tok) -> Result<UploadCorpus, String> {
        let d = upload_dir(Some(conv))?;
        let files = meta["files"].as_array().unwrap();
        let mut payloads = HashMap::new();
        let mut texts = vec![];
        let mut ids = vec![];
        for r in &rows {
            let id = r["id"].as_i64().unwrap_or(0);
            let f = &files[r["file"].as_u64().unwrap_or(0) as usize];
            let doc_path = d.join(f["stored"].as_str().unwrap_or("").replace('/', "\\"));
            let mut p = Payload::new();
            p.insert("text".into(), r["text"].clone());
            p.insert("doc_name".into(), r.get("doc_name").cloned().unwrap_or_else(|| f["name"].clone()));
            p.insert("doc_path".into(), json!(doc_path.to_string_lossy()));
            p.insert("folder".into(), json!(FOLDER_LABEL));
            p.insert("chunk_idx".into(), r.get("chunk_idx").cloned().unwrap_or(json!(0)));
            if let Some(s) = r.get("section_id").filter(|v| !v.is_null()) {
                p.insert("section_id".into(), s.clone());
            }
            texts.push(r["text"].as_str().unwrap_or("").to_string());
            ids.push(id);
            payloads.insert(id, p);
        }
        // 메모리 BM25 — Rust BM25 는 만들 때 캐시 파일을 쓰므로 대화 폴더 안에 둔다(폴더와 함께 지워진다)
        let bm25 = Bm25Index::new(Some(d.join("bm25.cache").to_string_lossy().into_owned()));
        bm25.build(&texts, &ids, tok)?;
        Ok(UploadCorpus { conv: conv.to_string(), n_files: files.len(), vecs, payloads, bm25 })
    }

    //------------------------------------------------------------------
    // 의미 검색 — 전수 내적. 돌려줌: [(id, 점수, payload)] 점수 순(같은 점수는 앞 청크가 먼저)
    //------------------------------------------------------------------
    pub fn search(&self, qv: &[f32], k: usize) -> Vec<(i64, f32, Payload)> {
        let mut scored: Vec<(usize, f32)> = self.vecs.iter().enumerate()
            .map(|(i, v)| (i, v.iter().zip(qv).map(|(a, b)| a * b).sum::<f32>())).collect();
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        scored.into_iter().take(k).map(|(i, s)| (i as i64, s, self.payloads[&(i as i64)].clone())).collect()
    }
}

//------------------------------------------------------------------
// 대화의 업로드 코퍼스(캐시) — 올린 문서가 없으면 None. 설정이 바뀐 옛 인덱스면 사본으로 다시 만든다
//------------------------------------------------------------------
pub fn corpus(conv: &str, embedder: &Embedder) -> Option<Arc<UploadCorpus>> {
    let d = upload_dir(Some(conv)).ok()?;
    let mut meta = match read_meta(conv) {
        Some(m) => m,
        None => {
            forget(Some(conv));
            return None;
        }
    };
    if !compatible(&meta) {
        let _g = WRITE.lock().unwrap();
        rebuild_locked(conv, embedder).ok()?;
        meta = read_meta(conv)?;
    }
    let sid = safe_id(conv)?;
    let mtime = std::fs::metadata(d.join(META)).ok()?.modified().ok()?;
    let mut g = CACHE.lock().unwrap();
    if let Some((t, c)) = g.0.get(&sid) {
        if *t == mtime {
            let c = c.clone();
            g.1.retain(|x| x != &sid);
            g.1.push(sid);
            return Some(c);
        }
    }
    let (rows, vecs) = read_rows(&d).ok()?;
    let tok = embedder.tokenizer().ok()?;
    let c = Arc::new(UploadCorpus::new(conv, &meta, rows, vecs, &tok).ok()?);
    g.0.insert(sid.clone(), (mtime, c.clone()));
    g.1.retain(|x| x != &sid);
    g.1.push(sid);
    while g.1.len() > CACHE_CONVS {
        let old = g.1.remove(0);
        g.0.remove(&old);
    }
    Some(c)
}
