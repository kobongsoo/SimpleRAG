//------------------------------------------------------------------
// 탐색기 모드 찾기 (탐색기 모드 설계서 §12 · §15)
//=> 답을 짓지 않고 "어느 문서의 몇 쪽에 무엇이 있는지"를 돌려준다.
//    1) 1단계 검색(dense + BM25 → RRF → 사본 거르기)으로 후보 POOL 개
//    2) 리랭커가 있으면 앞 RERANK_N 개를 다시 줄 세운다(나머지는 그 뒤에 RRF 순서)
//    3) 문서별로 묶고, 문서 안에서는 같은 쪽 청크를 한 줄로 합친다
//    4) 줄마다 조항 위치 · 검색어가 몰린 발췌 · 강조 위치를 붙인다
//   RAG 의 관련도 문턱으로 잘라 내지 않는다 — 문턱 아래는 low 로 표시만 한다(흐리게 보이도록).
//   쪽 번호는 payload page(인덱싱 때 블록 지문으로 찾은 시작 쪽)를 쓰고, 없는 옛 청크는
//   PDF 본문을 읽어 청크 첫머리가 나오는 쪽을 찾는다(§13 옛 인덱스 보정).
//------------------------------------------------------------------

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use once_cell::sync::Lazy;
use serde_json::{json, Map, Value};

use crate::indexer;
use crate::pyutil;
use crate::retrieve::{keywords, relevance_min, Retriever};
use crate::store::{pl_str, Payload};

pub const POOL: usize = 60;        // 1단계 후보 수
// 리랭킹할 앞쪽 후보 수 · 한 묶음(길이순) — 리랭킹이 시간 대부분이다.
//   상주 워커 실측(CPU, bge-reranker-base int8): 40개 한 묶음 4~7초 · 20개/4 1.5~1.9초 · 12개/4 0.8~0.95초
pub const RERANK_N: usize = 12;
const RERANK_BATCH: usize = 4;
const LOCATE_TOP: usize = 15;      // 옛 인덱스 쪽 보정은 점수 상위 이만큼만(PDF 를 읽어야 해서 느리다)
pub const MAX_DOCS: usize = 20;    // 내보낼 문서 수 상한
const SNIP_CHARS: usize = 120;     // 발췌 길이(글자)
const SNIP_LEAD: usize = 30;       // 발췌 창에서 첫 검색어 앞에 남길 글자
const PDF_CACHE: usize = 8;        // 쪽 보정용 PDF 본문 캐시(문서 수)
const LOCATE_CHARS: usize = 40;    // 쪽 보정 때 대조할 청크 첫머리 길이(공백 뺀 글자)

//------------------------------------------------------------------
// 찾은 청크 하나(묶기 전)
// -필드: payload = 인덱스 payload / score = 리랭커 점수(없으면 RRF 순위로 만든 값)
//        low = 관련도 문턱 아래 / page·exact = 쪽과 그 쪽이 확실한지
//------------------------------------------------------------------
struct Hit {
    payload: Payload,
    score: f64,
    low: bool,
    page: Option<i64>,
    exact: bool,
}

//------------------------------------------------------------------
// 찾기 (워커 /find · CLI search --json --group)
//=> 결과를 줄 단위 JSON 값 목록으로 만든다 — 문서 줄 다음에 그 문서의 쪽 줄들이 온다.
//
// -in: r = 검색기 / query = 검색어 / folder = 범위 폴더(None = 인덱스 전체) / limit = 1단계 후보 수(0 이면 POOL)
// -out: (줄 목록, 마침 줄) — 줄은 {"doc":n,…} 문서 줄 또는 {"doc":n,"page":…} 쪽 줄
// -out: error = 검색 실패 문자열
//------------------------------------------------------------------
pub fn find(r: &Retriever, query: &str, folder: Option<&str>, limit: usize) -> Result<(Vec<Value>, Value), String> {
    find_with(r, query, folder, limit, &crate::meta::query::PlanOpts::default())
}

//------------------------------------------------------------------
// 찾기 — 화면 조건 칩에서 끈 조건 · 넓힌 기간을 반영(opts)
//------------------------------------------------------------------
pub fn find_with(r: &Retriever, query: &str, folder: Option<&str>, limit: usize,
                 opts: &crate::meta::query::PlanOpts) -> Result<(Vec<Value>, Value), String> {
    let t0 = Instant::now();
    let pool = if limit == 0 { POOL } else { limit };
    // 메타데이터 조건(메타데이터 검색 설계서 §13) — 조건이 없는 질의는 mp 가 None 이라 예전과 똑같다
    let mp = crate::retrieve::meta_plan_opts(query, folder, opts);
    let meta_rows = mp.as_ref().and_then(|_| {
        let home = crate::meta::hook::data_dir();
        crate::meta::query::rows(&crate::meta::store::default_path(&home)).ok()
    });
    if let Some(p) = mp.as_ref().filter(|p| p.meta_only) {
        return Ok(find_meta_only(p, meta_rows.as_deref(), folder, t0));
    }
    let query: &str = mp.as_ref().map(|p| p.query.as_str()).unwrap_or(query);
    let docs = mp.as_ref().and_then(|p| p.docs.as_ref());
    // dense · BM25 후보도 pool 만큼 넓힌다(설정의 답용 후보 수로는 한 문서에 몰린다)
    let (chunks, timing) = r.search_rrf_docs(query, pool, folder, None, pool, docs)?;
    let ids: Vec<i64> = chunks.iter().map(|c| c.id).collect();
    let payloads = r.store.payloads(&ids)?;

    // ── 리랭킹 — 앞 RERANK_N 개만, 나머지는 그 뒤에 RRF 순서로
    let rr = r.reranker.lock().unwrap().clone();
    // 실측 조정용 — SIMPLERAG_FIND_RERANK · SIMPLERAG_FIND_BATCH 로 바꿔 볼 수 있다
    let env_n = |k: &str, d: usize| std::env::var(k).ok().and_then(|v| v.trim().parse::<usize>().ok()).unwrap_or(d);
    let rerank_n = env_n("SIMPLERAG_FIND_RERANK", RERANK_N);
    let batch = env_n("SIMPLERAG_FIND_BATCH", RERANK_BATCH).max(1);
    let n_rr = if rr.is_some() { chunks.len().min(rerank_n) } else { 0 };
    let mut scores: Vec<Option<f64>> = vec![None; chunks.len()];
    let mut rerank_ms = 0.0;
    if let Some(rr) = &rr {
        if n_rr > 0 {
            let t = Instant::now();
            let texts: Vec<String> = chunks[..n_rr].iter().map(|c| c.text.clone()).collect();
            if let Ok(s) = rr.score_batched(query, &texts, batch) {
                for (i, v) in s.into_iter().enumerate() {
                    scores[i] = Some(v as f64);
                }
            }
            rerank_ms = t.elapsed().as_secs_f64() * 1000.0;
        }
    }
    let gate = if rr.is_some() { relevance_min() } else { None };
    let mut hits: Vec<Hit> = vec![];
    for (i, c) in chunks.iter().enumerate() {
        let pl = payloads.get(&c.id).cloned().unwrap_or_default();
        if pl.is_empty() {
            continue;
        }
        // 리랭커 점수가 없으면 RRF 순위로 줄 세울 값을 만든다(리랭킹한 것보다 항상 아래)
        let mut score = scores[i].unwrap_or(-100.0 - i as f64);
        // 메타데이터 가산(작성 역할 · 유형 · 양식 · 기간 안) — 문턱(low) 판정은 리랭커 점수 그대로로
        if let Some(p) = &mp {
            score += p.boosts.get(pl_str(&pl, "doc_path")).copied().unwrap_or(0.0);
        }
        let low = match (scores[i], gate) {
            (Some(s), Some(g)) => s < g,
            // 리랭킹하지 않은 후보는 낮다고 단정하지 않는다(점수 순으로 아래에 놓일 뿐)
            (None, _) => false,
            _ => false,
        };
        hits.push(Hit { payload: pl, score, low, page: None, exact: false });
    }
    hits.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    // 쪽 정하기 — 옛 인덱스 보정(PDF 읽기)은 점수 상위 LOCATE_TOP 개만
    for (i, h) in hits.iter_mut().enumerate() {
        let (page, exact) = chunk_page(&h.payload, i < LOCATE_TOP);
        h.page = page;
        h.exact = exact;
    }

    // ── 문서별로 묶기(처음 나온 순서 = 최고 점수 순)
    let mut order: Vec<String> = vec![];
    let mut groups: HashMap<String, Vec<Hit>> = HashMap::new();
    for h in hits {
        let key = pl_str(&h.payload, "doc_path").to_string();
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(h);
    }
    // 문서 점수 — 최고 청크 + 0.15 × 둘째 청크(여러 곳이 고루 맞은 문서를 함께 위로)
    let doc_score = |v: &Vec<Hit>| v[0].score + v.get(1).map(|h| 0.15 * h.score.max(0.0)).unwrap_or(0.0);
    order.sort_by(|a, b| doc_score(&groups[b]).partial_cmp(&doc_score(&groups[a])).unwrap_or(std::cmp::Ordering::Equal));

    let kws = keywords(query);
    let state = indexer::load_state();
    let docs_state = indexer::docs(&state);
    let mut lines: Vec<Value> = vec![];
    let mut n_places = 0usize;
    let mut n_low = 0usize;
    let mut seen: Vec<String> = vec![];
    for (di, key) in order.iter().take(MAX_DOCS).enumerate() {
        let v = &groups[key];
        let no = di + 1;
        let name = pl_str(&v[0].payload, "doc_name").to_string();
        let all_low = v.iter().all(|h| h.low);
        if all_low {
            n_low += 1;
        }
        let mut dl = doc_line(no, key, &name, &kws, query, docs_state.get(key), Some(doc_score(v)), all_low, v.len());
        add_meta(&mut dl, key, meta_rows.as_deref());
        lines.push(dl);
        seen.push(key.clone());
        // 같은 쪽 청크는 한 줄로 — 가장 점수 높은 청크의 발췌를 쓰고 나머지는 more 로 센다
        let mut page_order: Vec<Option<i64>> = vec![];
        let mut by_page: HashMap<Option<i64>, Vec<&Hit>> = HashMap::new();
        for h in v {
            if !by_page.contains_key(&h.page) {
                page_order.push(h.page);
            }
            by_page.entry(h.page).or_default().push(h);
        }
        for pg in page_order {
            let hs = &by_page[&pg];
            let best = hs[0];
            let text = pl_str(&best.payload, "text");
            let (body, prefix_sec) = split_prefix(text);
            let body = clean_body(&body);
            let section = match best.payload.get("path").and_then(|p| p.as_array()) {
                Some(a) if !a.is_empty() => a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(" › "),
                _ => prefix_sec,
            };
            let (snippet, marks) = snippet(&body, &kws);
            n_places += 1;
            lines.push(json!({
                "doc": no, "page": pg, "page_to": best.payload.get("page_to").cloned().unwrap_or(Value::Null),
                "exact": best.exact, "section": section, "score": round2(best.score), "low": best.low,
                "snippet": snippet, "marks": marks, "more": hs.len() - 1,
                // 글 미리 보기(PDF 아닌 문서 · 설계서 §14-③ C)에 쓰는 청크 본문 — 이 쪽의 청크들을 이어서
                "text": hs.iter().take(3).map(|h| clean_body(&split_prefix(pl_str(&h.payload, "text")).0)).collect::<Vec<_>>().join("\n…\n"),
                "chunk_idx": best.payload.get("chunk_idx").cloned().unwrap_or(Value::Null),
            }));
        }
    }
    // ── 이름만 맞은 문서 — 내용 후보에 없어도 "파일 이름 일치" 묶음에 보이게(이름 일치 상위 5개)
    let mut name_only = 0;
    for (key, meta) in docs_state.iter() {
        if name_only >= 5 || seen.contains(key) {
            continue;
        }
        // 메타데이터 조건이 문서를 좁혔으면 이름 일치도 그 안에서만
        if docs.map_or(false, |d| !d.contains(key)) {
            continue;
        }
        if let Some(f) = folder {
            if !crate::commands::under_any(key, &[f.to_string()]) {
                continue;
            }
        }
        let name = meta.get("name").and_then(|v| v.as_str()).map(|s| s.to_string())
            .unwrap_or_else(|| pyutil::basename(key).to_string());
        if name_hit(&name, &kws, query) {
            name_only += 1;
            let no = lines.iter().filter(|l| l.get("page").is_none()).count() + 1;
            lines.push(doc_line(no, key, &name, &kws, query, Some(meta), None, false, 0));
        }
    }
    let n_docs = lines.iter().filter(|l| l.get("page").is_none()).count();
    let mut done = json!({
        "ms": (t0.elapsed().as_secs_f64() * 1000.0).round(), "docs": n_docs, "places": n_places, "low": n_low,
        "rerank_ms": rerank_ms.round(), "reranked": n_rr,
    });
    if let Some(n) = timing.get("scope_docs") {
        done["scope_docs"] = n.clone();
    }
    if let Some(s) = timing.get("synonyms") {
        done["synonyms"] = s.clone();
    }
    if let Some(p) = &mp {
        done["meta"] = p.to_json();
    }
    Ok((lines, done))
}

//------------------------------------------------------------------
// 조건만 있는 질의 — 본문 검색 없이 조건에 맞는 문서 줄만 (§13-2)
//=> 계획의 순서(가산 · 수정일)로 MAX_DOCS 개. 쪽 줄은 없다(어느 쪽이 맞았는지 알 수 없으므로).
//
// -in: mp = 계획 / rows = 읽어 둔 meta.sqlite / folder = 범위 폴더 / t0 = 시작 시각
// -out: (문서 줄들, 마침 줄 — meta.meta_only = true)
//------------------------------------------------------------------
fn find_meta_only(mp: &crate::meta::query::Plan, rows: Option<&crate::meta::query::Cached>, folder: Option<&str>,
                  t0: Instant) -> (Vec<Value>, Value) {
    let state = indexer::load_state();
    let docs_state = indexer::docs(&state);
    let mut lines = vec![];
    for key in mp.order.iter() {
        if lines.len() >= MAX_DOCS {
            break;
        }
        // 폴더 한정은 계획에서 이미 걸렀지만, 인덱스에 없는 문서(메타만 남은 것)는 상태 파일 이름으로 보인다
        if let Some(f) = folder {
            if !crate::commands::under_any(key, &[f.to_string()]) {
                continue;
            }
        }
        let meta = docs_state.get(key);
        let name = meta.and_then(|m| m.get("name")).and_then(|v| v.as_str()).map(|s| s.to_string())
            .unwrap_or_else(|| pyutil::basename(key).to_string());
        let mut dl = doc_line(lines.len() + 1, key, &name, &[], "", meta, None, false, 0);
        dl["meta_hit"] = json!(true);
        if dl["mtime"].is_null() {
            // 인덱스 상태에 없는 문서(추출 실패 등) — 메타의 수정일로
            if let Some(m) = rows.and_then(|c| c.rows.get(key)).and_then(|r| r.modified) {
                dl["mtime"] = json!(m);
            }
        }
        add_meta(&mut dl, key, rows);
        lines.push(dl);
    }
    let n = lines.len();
    let mut m = mp.to_json();
    m["meta_only"] = json!(true);
    let done = json!({
        "ms": (t0.elapsed().as_secs_f64() * 1000.0).round(), "docs": n, "places": 0, "low": 0,
        "rerank_ms": 0, "reranked": 0, "meta": m,
    });
    (lines, done)
}

// 문서 줄에 메타 표시 칸 — 제목(파일 이름과 다를 때) · 관련인(작성 → 수정 순 최대 3명)
fn add_meta(dl: &mut Value, key: &str, rows: Option<&crate::meta::query::Cached>) {
    let c = match rows {
        Some(c) => c,
        None => return,
    };
    if let Some(t) = c.rows.get(key).and_then(|r| r.title.clone()) {
        dl["title"] = json!(t);
    }
    if let Some(ps) = c.by_path.get(key) {
        let rank = |r: &str| match r { "author" => 0, "editor" => 1, _ => 2 };
        let mut v: Vec<&(String, String)> = ps.iter().collect();
        v.sort_by_key(|(_, r)| rank(r));
        let mut names: Vec<String> = vec![];
        for (n, _) in v {
            if !names.contains(n) && names.len() < 3 {
                names.push(n.clone());
            }
        }
        if !names.is_empty() {
            dl["people"] = json!(names);
        }
    }
}

// 소수 둘째 자리
fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

//------------------------------------------------------------------
// 문서 줄 만들기
// -in: no = 문서 번호 / key = 문서 경로 / name = 문서 이름 / meta = 상태 파일의 그 문서 항목
//      score = 문서 점수(이름만 맞은 문서는 None) / low = 모든 청크가 문턱 아래 / hits = 내용이 맞은 청크 수
//------------------------------------------------------------------
#[allow(clippy::too_many_arguments)]
fn doc_line(no: usize, key: &str, name: &str, kws: &[String], query: &str, meta: Option<&Value>,
            score: Option<f64>, low: bool, hits: usize) -> Value {
    let mtime = meta.and_then(|m| m.get("mtime")).cloned().unwrap_or(Value::Null);
    json!({
        "doc": no, "doc_path": key, "doc_name": name, "folder": meta.and_then(|m| m.get("folder")).cloned().unwrap_or(Value::Null),
        "name_hit": name_hit(name, kws, query), "score": score.map(round2), "low": low, "hits": hits, "mtime": mtime,
    })
}

//------------------------------------------------------------------
// 파일 이름 일치 판정
//=> 탐색기 사용자는 이름으로도 찾는다. 공백 뺀 검색어가 이름에 통째로 있거나,
//   핵심 낱말이 2개 이상(낱말이 하나뿐이면 그 하나) 이름에 들어 있으면 일치로 본다.
//------------------------------------------------------------------
fn name_hit(name: &str, kws: &[String], query: &str) -> bool {
    let n = squash(name).to_lowercase();
    let q = squash(query).to_lowercase();
    if q.chars().count() >= 2 && n.contains(&q) {
        return true;
    }
    if kws.is_empty() {
        return false;
    }
    let got = kws.iter().filter(|k| n.contains(&k.to_lowercase())).count();
    got >= kws.len().min(2)
}

// 공백을 모두 뺀 글자
fn squash(s: &str) -> String {
    s.chars().filter(|c| !pyutil::is_space(*c)).collect()
}

//------------------------------------------------------------------
// 청크 앞 접두 "[문서 > 절]\n" 떼기 → (본문, 접두 속 절 경로)
//------------------------------------------------------------------
fn split_prefix(text: &str) -> (String, String) {
    if text.starts_with('[') {
        if let Some(i) = text.find("]\n") {
            let inner = &text[1..i];
            // 맨 앞은 문서 이름이라 위치에서 뺀다
            let sec = inner.split(" > ").skip(1).collect::<Vec<_>>().join(" › ");
            return (text[i + 2..].to_string(), sec);
        }
    }
    (text.to_string(), String::new())
}

//------------------------------------------------------------------
// 본문 속 접두 줄 빼기 — 작은 절 여럿을 묶은 청크는 절마다 "[문서 > 절]" 줄이 중간에 들어 있다
//   ("[별표 1]" 같은 진짜 본문은 " > " 가 없어 남는다)
//------------------------------------------------------------------
fn clean_body(body: &str) -> String {
    body.lines().filter(|l| {
        let t = l.trim();
        !(t.starts_with('[') && t.ends_with(']') && t.contains(" > "))
    }).collect::<Vec<_>>().join("\n")
}

//------------------------------------------------------------------
// 발췌 만들기 — 검색어가 가장 많이 몰린 SNIP_CHARS 글자 창
//=> 청크 머리 160자보다 "맞은 자리"를 보여 주는 편이 쓸모 있다.
//    1) 줄바꿈을 공백으로 펴고 낱말마다 나오는 자리를 모은다(세 글자 이상은 앞 두 글자로도)
//    2) 자리마다 창을 대 보고 낱말 수가 가장 많은 창을 고른다
//    3) 창 안의 낱말 자리를 [시작, 끝) 글자 위치로 돌려준다(화면이 <mark> 로 감싼다)
//
// -in: body = 접두 뗀 청크 본문 / kws = 핵심 낱말
// -out: (발췌, [[시작, 끝], …])
//------------------------------------------------------------------
fn snippet(body: &str, kws: &[String]) -> (String, Vec<[usize; 2]>) {
    let flat: Vec<char> = body.split_whitespace().collect::<Vec<_>>().join(" ").chars().collect();
    let lower: Vec<char> = flat.iter().flat_map(|c| c.to_lowercase()).collect();
    // 소문자로 바꿔 길이가 달라지는 글자(드묾)가 있으면 원문으로 찾는다
    let hay = if lower.len() == flat.len() { &lower } else { &flat };
    let mut spots: Vec<[usize; 2]> = vec![];
    for k in kws {
        let kc: Vec<char> = k.to_lowercase().chars().collect();
        let mut found = find_all(hay, &kc);
        if found.is_empty() && kc.len() >= 3 {
            // "정산하여" 같은 활용형 — 앞 두 글자로도 찾는다
            found = find_all(hay, &kc[..2]);
            spots.extend(found.into_iter().map(|p| [p, p + 2]));
        } else {
            spots.extend(found.into_iter().map(|p| [p, p + kc.len()]));
        }
    }
    spots.sort();
    let n = flat.len();
    let mut start = 0;
    if !spots.is_empty() && n > SNIP_CHARS {
        let mut best = (0usize, 0usize);
        for s in &spots {
            let st = s[0].saturating_sub(SNIP_LEAD).min(n.saturating_sub(SNIP_CHARS));
            let cnt = spots.iter().filter(|x| x[0] >= st && x[1] <= st + SNIP_CHARS).count();
            if cnt > best.0 {
                best = (cnt, st);
            }
        }
        start = best.1;
    }
    let end = (start + SNIP_CHARS).min(n);
    let lead = if start > 0 { "…" } else { "" };
    let tail = if end < n { "…" } else { "" };
    let text: String = format!("{}{}{}", lead, flat[start..end].iter().collect::<String>(), tail);
    let off = lead.chars().count();
    let marks = spots.into_iter().filter(|s| s[0] >= start && s[1] <= end)
        .map(|s| [s[0] - start + off, s[1] - start + off]).collect();
    (text, marks)
}

// 글자 배열에서 needle 이 나오는 모든 시작 위치(겹치지 않게)
fn find_all(hay: &[char], needle: &[char]) -> Vec<usize> {
    let mut out = vec![];
    if needle.is_empty() || needle.len() > hay.len() {
        return out;
    }
    let mut i = 0;
    while i + needle.len() <= hay.len() {
        if &hay[i..i + needle.len()] == needle {
            out.push(i);
            i += needle.len();
        } else {
            i += 1;
        }
    }
    out
}

//------------------------------------------------------------------
// 청크의 쪽 정하기 → (쪽, 확실한가)
//=> 1) payload page(새 인덱스) → 확실
//   2) page_from == page_to → 확실
//   3) PDF 면 본문을 읽어 청크 첫머리가 나오는 쪽(옛 인덱스 보정) → 찾으면 확실
//   4) 그래도 모르면 page_from(근사)
//------------------------------------------------------------------
fn chunk_page(pl: &Payload, may_locate: bool) -> (Option<i64>, bool) {
    if let Some(p) = pl.get("page").and_then(|v| v.as_i64()) {
        return (Some(p), true);
    }
    let from = pl.get("page_from").and_then(|v| v.as_i64());
    let to = pl.get("page_to").and_then(|v| v.as_i64());
    match (from, to) {
        (Some(f), Some(t)) if f == t => (Some(f), true),
        (Some(f), Some(t)) => {
            let path = pl_str(pl, "doc_path");
            if may_locate && path.to_lowercase().ends_with(".pdf") {
                let (body, _) = split_prefix(pl_str(pl, "text"));
                if let Some(p) = locate_page(path, f, t, &body) {
                    return (Some(p), true);
                }
            }
            (Some(f), false)
        }
        (f, _) => (f, false),
    }
}

// 쪽 보정용 PDF 본문 캐시 — (경로, 쪽마다 공백 뺀 글자). 맨 뒤가 가장 최근
static PAGES: Lazy<Mutex<Vec<(String, Arc<Vec<String>>)>>> = Lazy::new(|| Mutex::new(vec![]));

//------------------------------------------------------------------
// 옛 인덱스 보정 — PDF 에서 청크 첫머리가 나오는 쪽 찾기
// -in: path = PDF 경로 / from·to = 찾을 쪽 범위(1부터) / body = 접두 뗀 청크 본문
// -out: 찾은 쪽 / 못 찾거나 읽기 실패면 None
//------------------------------------------------------------------
fn locate_page(path: &str, from: i64, to: i64, body: &str) -> Option<i64> {
    let head: String = squash(body).chars().take(LOCATE_CHARS).collect();
    if head.chars().count() < 8 {
        return None;
    }
    let pages = pdf_pages(path)?;
    // 앞 40자가 쪽 경계에 걸쳐 있으면 못 찾으므로 앞 16자로 한 번 더
    let short: String = head.chars().take(16).collect();
    for needle in [&head, &short] {
        for p in from.max(1)..=to.min(pages.len() as i64) {
            if pages[(p - 1) as usize].contains(needle.as_str()) {
                return Some(p);
            }
        }
    }
    None
}

// PDF 쪽 글 읽기(캐시)
fn pdf_pages(path: &str) -> Option<Arc<Vec<String>>> {
    {
        let mut c = PAGES.lock().unwrap();
        if let Some(i) = c.iter().position(|(p, _)| p == path) {
            let hit = c.remove(i);
            let v = hit.1.clone();
            c.push(hit);
            return Some(v);
        }
    }
    let (raw, _) = crate::pdfium::read_pages(path, false, |_, _| true).ok()?;
    let v = Arc::new(raw.into_iter().map(|pt| squash(&pt.text)).collect::<Vec<_>>());
    let mut c = PAGES.lock().unwrap();
    c.push((path.to_string(), v.clone()));
    if c.len() > PDF_CACHE {
        c.remove(0);
    }
    Some(v)
}

//------------------------------------------------------------------
// 워커 출력 — "@find {json}" 줄들 + "@find_done {json}" (ASCII JSON, @ev 와 같은 규칙)
//------------------------------------------------------------------
pub fn print_lines(lines: &[Value], done: &Value, out: &mut dyn FnMut(&str)) {
    for l in lines {
        out(&format!("@find {}", pyutil::json_ascii(l, true)));
    }
    out(&format!("@find_done {}", pyutil::json_ascii(done, true)));
}

// 오류 한 줄
pub fn error_line(e: &str) -> String {
    let mut m = Map::new();
    m.insert("error".into(), json!(e));
    format!("@find_done {}", pyutil::json_ascii(&Value::Object(m), true))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snippet_marks_keyword() {
        let (s, m) = snippet("여비는 출장 전 지급하고 귀임 후 7일 이내에 정산하여야 한다.", &["정산".to_string()]);
        assert!(s.contains("정산"));
        let chars: Vec<char> = s.chars().collect();
        let got: String = chars[m[0][0]..m[0][1]].iter().collect();
        assert_eq!(got, "정산");
    }

    #[test]
    fn prefix_split() {
        let (b, sec) = split_prefix("[규정 > 10. 여비지급 > 10.1]\n여비는");
        assert_eq!(b, "여비는");
        assert_eq!(sec, "10. 여비지급 › 10.1");
    }

    #[test]
    fn name_match() {
        let k = vec!["출장".to_string(), "여비".to_string()];
        assert!(name_hit("25.출장여비규정_16.04.01.pdf", &k, "출장 여비"));
        assert!(!name_hit("16.이사회규정.pdf", &k, "출장 여비"));
    }
}
