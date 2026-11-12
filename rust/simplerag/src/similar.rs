//------------------------------------------------------------------
// 탐색기 모드 비슷한 문서 찾기 (plan/비슷한문서찾기_설계서_20261112.html)
//=> 문서 하나를 넣으면 그 문서와 비슷한 문서를 찾아 찾기(/find)와 같은 "@find" 줄로 돌려준다.
//    1) 기준 문서의 청크 — 인덱스에 있는 문서면 저장 벡터를 그대로 쓴다(A 경우, 임베딩 0번)
//       인덱스 밖 파일이라도 내용 해시가 같은 문서가 인덱스에 있으면 그 문서로 본다
//       그래도 없는 바깥 문서(B 경우)는 인덱싱과 같은 추출 · 청킹을 하고 고른 K 청크만 임베딩한다(저장하지 않음)
//    2) 문서 전체에서 고른 간격으로 K 청크를 고른다(실측: 전부 쓴 순위와 상위 10 일치 0.93)
//    3) 저장소를 한 번 훑어 문서마다 "고른 청크별 최고 점수"와 "본문이 그대로 같은 청크"를 구한다
//    4) 사본(내용 해시 같음) · 다른 판(본문 VERSION_RATIO 이상 같음)은 점수 경쟁에서 빼고 따로 묶는다
//    5) 나머지는 최고 점수의 평균으로 줄 세워 상위 MAX_DOCS, 문서마다 가장 비슷한 문단 짝 PAIRS 개를 붙인다
//------------------------------------------------------------------

use std::collections::HashSet;
use std::time::Instant;

use serde_json::{json, Value};

use crate::find::{add_meta, chunk_page, clean_body, doc_line, round2, snippet, split_prefix, MAX_DOCS};
use crate::indexer;
use crate::pyutil;
use crate::retrieve::Retriever;
use crate::store::{pl_str, Payload};

pub const K: usize = 32;               // 기준 문서에서 고를 청크 수
pub const VERSION_RATIO: f64 = 0.2;    // 기준 문서 본문의 이 비율 이상이 그대로 든 문서 = 다른 판
const BODY_MIN: usize = 40;            // 본문 비교에 쓰는 최소 글자(공백 뺀) — 별표 · 머리글 오판 방지
const PAIRS: usize = 2;                // 문서마다 보일 비슷한 문단 짝 수
const MAX_VERSIONS: usize = 20;        // 사본 · 다른 판 묶음 상한
const MAX_SCAN_THREADS: usize = 4;     // 훑기 스레드 상한 — 인덱싱 · 화면이 함께 돌므로 코어를 다 쓰지 않는다
// 바깥 PDF 는 쪽이 이보다 많으면 고른 쪽만 읽는다 — 추출 시간이 쪽 수에 비례한다(578쪽 3~5초 · 2,705쪽 24초 실측)
const PDF_ALL_PAGES: usize = 300;
// 고른 쪽 읽기 — 곳 수 × 곳마다 이어 읽는 쪽 수. 64곳 × 1쪽이 32곳 × 2쪽보다 같거나 나았다
//   (2026-11-12 실측, 578쪽 · 2,705쪽: 전부 읽은 순위와 상위 10 일치 0.8 · 0.8 대 0.7 · 0.7, 추출 0.4~1.3초)
const PDF_SPOTS: usize = 64;
const PDF_SPAN: usize = 1;
// AI 답변창 요약에 넘길 기준 문서 글 — 고른 간격 몇 청크를 이 글자 수까지(overview::CAP_ONE 과 같은 값)
const SUMMARY_CHUNKS: usize = 8;
// 고른 쪽만 읽은 PDF 는 청크 경계가 달라 본문 그대로 비교(VERSION_RATIO)가 맞지 않는다 — 유사도가 이 이상이면 다른 판
//   (실측: 원본 87~88% · 그다음 문서 60% 이하)
const SAMPLED_VERSION_SCORE: f64 = 0.80;
const SUMMARY_CHARS: usize = 1400;

//------------------------------------------------------------------
// 본문 열쇠 — 청크 글에서 접두를 떼고 공백을 뺀 본문의 해시
//=> 다른 문서의 청크라도 본문이 그대로 같으면 같은 열쇠가 나온다(접두 "[문서 > 절]" 은 문서 이름이 달라 뺀다).
//   훑기 때 청크 21만 개마다 부르므로 문자열을 새로 만들지 않고 글자를 바로 해시에 넣는다.
//    1) 맨 앞 접두 줄 · 본문 속 접두 줄(find::clean_body 와 같은 규칙)을 건너뛴다
//    2) 공백이 아닌 글자만 해시에 넣고 센다 — BODY_MIN 글자보다 짧으면 None(짧은 글은 관계없는 문서끼리도 같다)
//
// -in: text = 청크 글(접두 포함)
//
// -out: Some(열쇠) / 짧으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn body_key(text: &str) -> Option<u64> {
    // find::split_prefix 와 같은 규칙 — '[' 로 시작하면 첫 "]\n" 까지가 접두
    let body = if text.starts_with('[') {
        match text.find("]\n") {
            Some(i) => &text[i + 2..],
            None => text,
        }
    } else {
        text
    };
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut n = 0usize;
    for line in body.lines() {
        let t = line.trim();
        // 작은 절을 묶은 청크 속 "[문서 > 절]" 줄은 본문이 아니다
        if t.starts_with('[') && t.ends_with(']') && t.contains(" > ") {
            continue;
        }
        for c in line.chars() {
            if !pyutil::is_space(c) {
                // 같은 실행 안에서만 비교하므로 빠른 곱셈 해시로 충분하다
                h = (h.rotate_left(5) ^ c as u64).wrapping_mul(0x517c_c1b7_2722_0a95);
                n += 1;
            }
        }
    }
    if n < BODY_MIN {
        return None;
    }
    Some(h ^ n as u64)
}

//------------------------------------------------------------------
// 고른 간격으로 고르기 — n 개 가운데 k 개의 자리
//=> 앞·중간·뒤(0.89)보다 문서 전체에 고르게 퍼진 쪽(0.93)이 전부 쓴 순위에 더 가까웠다(설계서 §2).
//
// -in: n = 전체 수 / k = 고를 수
//
// -out: 자리 목록(오름차순) — n ≤ k 면 전부
// -out: error = 없음
//------------------------------------------------------------------
pub fn pick_even(n: usize, k: usize) -> Vec<usize> {
    if n <= k {
        return (0..n).collect();
    }
    (0..k).map(|i| i * n / k).collect()
}

// 훑기 스레드 수 — 코어 절반, 1~MAX_SCAN_THREADS
fn scan_threads() -> usize {
    let n = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(2);
    (n / 2).clamp(1, MAX_SCAN_THREADS)
}

//------------------------------------------------------------------
// 기준 문서
// -필드: key = 기준 문서 경로(인덱스 문서면 인덱스 경로) / mode = "indexed" | "same_content" | "external"
// -필드: pts = 기준 문서 점(바깥 문서는 id -1 · 빈 벡터) / parse_ms = 추출 시간
// -필드: pages = 바깥 PDF 를 고른 쪽만 읽었으면 (읽은 쪽 수, 전체 쪽 수)
//------------------------------------------------------------------
struct Source {
    key: String,
    mode: &'static str,
    pts: Vec<(i64, Vec<f32>, Payload)>,
    parse_ms: f64,
    pages: Option<(usize, usize)>,
}

//------------------------------------------------------------------
// 고른 쪽인가 — n 쪽 가운데 spots 곳에서 span 쪽씩
//=> 곳의 시작 쪽 = floor(j × n / spots). i 쪽이 어느 곳의 [시작, 시작+span) 안에 들면 읽는다.
//   n 이 PDF_ALL_PAGES 이하면 전부 읽는다.
//
// -in: i = 쪽 번호(0부터) / n = 전체 쪽 수 / spots · span = 곳 수 · 곳마다 쪽 수
//
// -out: 읽을지
// -out: error = 없음
//------------------------------------------------------------------
pub fn keep_page(i: usize, n: usize, spots: usize, span: usize) -> bool {
    if n <= PDF_ALL_PAGES || spots == 0 {
        return true;
    }
    // i 를 포함할 수 있는 곳은 i 근처의 곳뿐이다 — 시작 쪽을 내림하므로 앞뒤 한 곳씩 함께 본다
    let j = i * spots / n;
    (j.saturating_sub(1)..=(j + 1).min(spots - 1)).any(|j| {
        let st = j * n / spots;
        i >= st && i < st + span
    })
}

// 고른 쪽 설정 — SIMPLERAG_SIMILAR_PDF="곳x쪽"(예 64x1) 로 바꿔 잴 수 있다. "all" 이면 전부 읽기
fn pdf_pick() -> (usize, usize) {
    match std::env::var("SIMPLERAG_SIMILAR_PDF").ok().map(|v| v.trim().to_lowercase()) {
        Some(v) if v == "all" => (0, 0),
        Some(v) => {
            let mut it = v.split('x').map(|x| x.trim().parse::<usize>().ok());
            match (it.next().flatten(), it.next().flatten()) {
                (Some(a), Some(b)) if a > 0 && b > 0 => (a, b),
                _ => (PDF_SPOTS, PDF_SPAN),
            }
        }
        None => (PDF_SPOTS, PDF_SPAN),
    }
}

// 추출 오류를 사용자 말로 — 텍스트층이 없는 PDF(스캔)는 내부 문구 대신 이유를 알린다
fn extract_error(e: &str) -> String {
    let scan = ["텍스트층 없음", "pdf:empty", "전엔진 실패"].iter().any(|w| e.contains(w));
    if scan {
        "글자를 뽑지 못했습니다 — 스캔한 문서(그림으로 된 쪽)는 아직 비교할 수 없습니다".into()
    } else {
        format!("문서를 읽지 못했습니다: {}", pyutil::head(e, 160))
    }
}

//------------------------------------------------------------------
// 기준 문서 정하기 — 인덱스에 있는 경로 · 내용이 같은 문서 · 바깥 문서
//=> 1) 경로가 인덱스에 있으면 그 점들(A)
//   2) 내용 해시가 같은 문서가 인덱스에 있으면 그 점들(A — 메일 첨부가 폴더 문서의 사본인 경우)
//   3) 아니면 바깥 문서(B) — 추출 · 청킹해 벡터 없는 점(id -1)으로 만든다. 고른 청크만 similar() 가 임베딩한다
//      PDF 가 PDF_ALL_PAGES 쪽보다 길면 고른 쪽만 읽는다(크기 상한은 두지 않는다 — 시간은 MB 가 아니라 쪽 수에 비례)
//
// -in: r = 검색기 / path = 넣은 파일 경로
//
// -out: Source
// -out: error = 파일을 읽지 못함 · 글자를 뽑지 못함 · 저장소 오류면 그 문자열
//------------------------------------------------------------------
fn source_doc(r: &Retriever, path: &str) -> Result<Source, String> {
    let pts = r.store.doc_points(path)?;
    if !pts.is_empty() {
        let key = pl_str(&pts[0].2, "doc_path").to_string();
        return Ok(Source { key, mode: "indexed", pts, parse_ms: 0.0, pages: None });
    }
    // 인덱싱 폴더 밖의 사본(메일 첨부 등) — 내용 해시가 같으면 그 문서의 벡터를 쓴다
    let hash = indexer::content_hash(path).map_err(|e| format!("문서를 읽지 못했습니다: {} ({})", path, e))?;
    if let Some(key) = r.store.doc_by_hash(&hash)? {
        let pts = r.store.doc_points(&key)?;
        if !pts.is_empty() {
            return Ok(Source { key, mode: "same_content", pts, parse_ms: 0.0, pages: None });
        }
    }
    // ── 바깥 문서(B) — 인덱싱과 같은 추출 · 청킹(청크 접두의 제목도 같은 규칙이라 인덱스 청크와 비교가 맞다)
    let t = Instant::now();
    let name = pyutil::basename(path).to_string();
    eprintln!("[similar] 문서 읽는 중 — {}", name);
    let tok = r.embedder.tokenizer()?;
    let (spots, span) = pdf_pick();
    // 전체 쪽 수와 읽은 쪽 수를 센다(keep 은 쪽마다 불린다)
    let seen = std::cell::Cell::new((0usize, 0usize));
    let keep = |i: usize, n: usize| {
        let k = keep_page(i, n, spots, span);
        let (read, _) = seen.get();
        seen.set((read + k as usize, n));
        k
    };
    let metas = indexer::extract_metas(path, &name, &tok, Some(&keep)).map_err(|e| extract_error(&e))?;
    if metas.is_empty() {
        return Err(extract_error("텍스트층 없음"));
    }
    let (read, total) = seen.get();
    let pages = if total > 0 && read < total { Some((read, total)) } else { None };
    let pts = metas.into_iter().enumerate().map(|(i, m)| {
        // 문단 짝 표시(chunk_page · 절 경로)에 쓰는 칸만 인덱스 payload 와 같은 이름으로
        let mut p = Payload::new();
        p.insert("text".into(), json!(m.text));
        p.insert("doc_path".into(), json!(path));
        p.insert("doc_name".into(), json!(name));
        p.insert("content_hash".into(), json!(hash));
        p.insert("chunk_idx".into(), json!(i));
        if !m.path.is_empty() {
            p.insert("path".into(), json!(m.path));
        }
        for (k, v) in [("page", m.page), ("page_from", m.page_from), ("page_to", m.page_to)] {
            if let Some(v) = v {
                p.insert(k.into(), json!(v));
            }
        }
        (-1, vec![], p)
    }).collect();
    Ok(Source { key: path.to_string(), mode: "external", pts, parse_ms: t.elapsed().as_secs_f64() * 1000.0, pages })
}

//------------------------------------------------------------------
// AI 답변창 요약에 넘길 기준 문서 글 — 문서 전체에서 고른 간격 SUMMARY_CHUNKS 청크의 본문을 SUMMARY_CHARS 까지
//=> 워커가 기준 문서를 다시 읽지 않게 찾기 결과에 실어 보낸다(바깥 문서는 다시 읽으면 추출을 또 한다).
//   청크마다 몫(SUMMARY_CHARS / 청크 수)만큼 잘라 앞쪽 청크가 글자 수를 다 차지하지 않게 한다.
//
// -in: pts = 기준 문서 점(문서 순서)
//
// -out: 요약용 글
// -out: error = 없음
//------------------------------------------------------------------
fn summary_text(pts: &[(i64, Vec<f32>, Payload)]) -> String {
    let picks = pick_even(pts.len(), SUMMARY_CHUNKS);
    let each = SUMMARY_CHARS / picks.len().max(1);
    picks.iter().map(|&i| {
        let body = clean_body(&split_prefix(pl_str(&pts[i].2, "text")).0);
        let flat = body.split_whitespace().collect::<Vec<_>>().join(" ");
        pyutil::head(&flat, each).to_string()
    }).filter(|t| !t.is_empty()).collect::<Vec<_>>().join("\n")
}

//------------------------------------------------------------------
// 쪽 줄 하나 — 비슷한 문단 짝(상대 문서 쪽 + 기준 문서 쪽)
//
// -in: no = 문서 번호 / their = 상대 청크 payload / mine = 기준 청크 payload / score = 그 짝 내적
//
// -out: @find 쪽 줄(찾기 쪽 줄 모양 + mine)
// -out: error = 없음
//------------------------------------------------------------------
fn pair_line(no: usize, their: &Payload, mine: &Payload, score: f32) -> Value {
    let (page, exact) = chunk_page(their, false);
    let (body, prefix_sec) = split_prefix(pl_str(their, "text"));
    let body = clean_body(&body);
    let section = match their.get("path").and_then(|p| p.as_array()) {
        Some(a) if !a.is_empty() => a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(" › "),
        _ => prefix_sec,
    };
    let (snip, _) = snippet(&body, &[]);
    let (mpage, mexact) = chunk_page(mine, false);
    let (mbody, _) = split_prefix(pl_str(mine, "text"));
    let (msnip, _) = snippet(&clean_body(&mbody), &[]);
    json!({
        "doc": no, "page": page, "page_to": their.get("page_to").cloned().unwrap_or(Value::Null),
        "exact": exact, "section": section, "score": round2(score as f64), "low": false,
        "snippet": snip, "marks": [], "more": 0, "text": body,
        "chunk_idx": their.get("chunk_idx").cloned().unwrap_or(Value::Null),
        "mine": {"page": mpage, "exact": mexact, "snippet": msnip},
    })
}

//------------------------------------------------------------------
// 비슷한 문서 찾기 (핵심)
//=> 모듈 머리 설명의 1)~5). 결과는 찾기와 같은 (줄 목록, 마침 줄).
//   문서 줄에는 group("copy" | "version" | "similar") · pct(유사도 %) · overlap(같은 본문 비율)을 더 싣는다.
//
// -in: r = 검색기 / path = 넣은 파일 경로 / folder = 찾을 범위(None 이면 전체)
//
// -out: (줄 목록, 마침 줄 {kind:"similar", ms, docs, places, groups, source})
// -out: error = 문서를 읽지 못함 · 추출 · 저장소 · 임베딩 오류면 그 문자열
//------------------------------------------------------------------
pub fn similar(r: &Retriever, path: &str, folder: Option<&str>) -> Result<(Vec<Value>, Value), String> {
    let t0 = Instant::now();
    let Source { key: src, mode, pts, parse_ms, pages } = source_doc(r, path)?;
    let src_hash = pl_str(&pts[0].2, "content_hash").to_string();

    // ── 기준 문서 본문 열쇠(사본 · 다른 판 판정) · 고른 청크
    let bodies: HashSet<u64> = pts.iter().filter_map(|(_, _, p)| body_key(pl_str(p, "text"))).collect();
    let picks = pick_even(pts.len(), K);
    let mut qvs: Vec<Vec<f32>> = picks.iter().map(|&i| pts[i].1.clone()).collect();
    // 벡터 대기 청크 · 바깥 문서 청크만 그 글로 바로 임베딩한다 — 많아야 K 개
    let missing: Vec<usize> = (0..qvs.len()).filter(|&i| qvs[i].is_empty()).collect();
    let mut embed_ms = 0.0;
    if !missing.is_empty() {
        let t = Instant::now();
        r.embedder.ensure_loaded()?;
        let texts: Vec<String> = missing.iter().map(|&i| pl_str(&pts[picks[i]].2, "text").to_string()).collect();
        let vs = r.embedder.embed_passages(&texts)?;
        for (k, v) in missing.iter().zip(vs) {
            qvs[*k] = v;
        }
        embed_ms = t.elapsed().as_secs_f64() * 1000.0;
    }

    // ── 범위(폴더 한정)
    let scope = match folder {
        Some(f) => Some(r.store.folder_scope(f)?),
        None => None,
    };
    let allow = scope.as_ref().map(|(_, ids)| ids);

    // ── 훑기
    let t_scan = Instant::now();
    let scan = r.store.doc_max_scan(&qvs, &bodies, body_key, allow, scan_threads())?;
    let scan_ms = t_scan.elapsed().as_secs_f64() * 1000.0;

    // ── 나누기: 자기 자신 · 사본 · 다른 판 · 내용 비슷
    let src_norm = pyutil::normcase(&src);
    let nb = bodies.len().max(1) as f64;
    let mut versions: Vec<(usize, &'static str, f64, Option<f64>)> = vec![];   // (문서 칸, 묶음, 같은 본문 비율, 유사도로 판정했으면 그 점수)
    let mut similar: Vec<(usize, f64)> = vec![];                   // (문서 칸, 점수)
    for j in 0..scan.docs.len() {
        if pyutil::normcase(&scan.docs[j]) == src_norm {
            continue;
        }
        let hash = pl_str(&scan.first[j].1, "content_hash");
        let overlap = scan.same[j].len() as f64 / nb;
        if !src_hash.is_empty() && hash == src_hash {
            versions.push((j, "copy", 1.0, None));
        } else if !bodies.is_empty() && overlap >= VERSION_RATIO {
            versions.push((j, "version", overlap, None));
        } else {
            // 벡터가 하나도 없는 문서(전부 벡터 대기)는 점수를 매기지 않는다
            let got: Vec<f32> = scan.best[j].iter().filter(|b| b.1 >= 0).map(|b| b.0).collect();
            if got.is_empty() {
                continue;
            }
            let score = got.iter().sum::<f32>() as f64 / scan.best[j].len() as f64;
            if pages.is_some() && score >= SAMPLED_VERSION_SCORE {
                versions.push((j, "version", overlap, Some(score)));
            } else {
                similar.push((j, score));
            }
        }
    }
    // 사본 먼저, 다른 판은 같은 본문이 많은 순
    versions.sort_by(|a, b| (a.1 != "copy").cmp(&(b.1 != "copy"))
        .then(b.3.unwrap_or(b.2).partial_cmp(&a.3.unwrap_or(a.2)).unwrap_or(std::cmp::Ordering::Equal)));
    versions.truncate(MAX_VERSIONS);
    similar.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    // 경로만 다르고 내용이 같은 문서(서로의 사본)는 한 줄로 — 같은 줄이 두 번 보이지 않게 뒤의 것은 그 줄의 사본으로
    let mut kept: Vec<(usize, f64)> = vec![];
    let mut copies: Vec<Vec<String>> = vec![];
    let mut by_hash: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    for (j, sc) in similar {
        let h = pl_str(&scan.first[j].1, "content_hash");
        if !h.is_empty() {
            if let Some(&k) = by_hash.get(h) {
                copies[k].push(scan.docs[j].clone());
                continue;
            }
        }
        // 상위 MAX_DOCS 줄이 다 찼으면 더 넣지 않는다(이미 든 줄의 사본은 위에서 받는다)
        if kept.len() >= MAX_DOCS {
            continue;
        }
        if !h.is_empty() {
            by_hash.insert(h.to_string(), kept.len());
        }
        kept.push((j, sc));
        copies.push(vec![]);
    }
    let similar = kept;

    // ── 문단 짝에 쓸 청크 payload(상대 · 기준)
    let mut want: Vec<i64> = vec![];
    for (j, _) in &similar {
        let mut q: Vec<(usize, &(f32, i64))> = scan.best[*j].iter().enumerate().filter(|(_, b)| b.1 >= 0).collect();
        q.sort_by(|a, b| b.1 .0.partial_cmp(&a.1 .0).unwrap_or(std::cmp::Ordering::Equal));
        want.extend(q.iter().take(PAIRS * 2).map(|(_, b)| b.1));
    }
    let theirs = r.store.payloads(&want)?;

    // ── 줄 만들기
    let state = indexer::load_state();
    let docs_state = indexer::docs(&state);
    let meta_rows = {
        let home = crate::meta::hook::data_dir();
        crate::meta::query::rows(&crate::meta::store::default_path(&home)).ok()
    };
    let mut lines: Vec<Value> = vec![];
    let mut no = 0usize;
    let mut places = 0usize;
    let mut n_copy = 0usize;
    for (j, group, overlap, near) in &versions {
        no += 1;
        let key = &scan.docs[*j];
        let name = pl_str(&scan.first[*j].1, "doc_name").to_string();
        let mut dl = doc_line(no, key, &name, &[], "", docs_state.get(key), None, false, 0);
        dl["group"] = json!(group);
        dl["overlap"] = json!(round2(*overlap));
        // 유사도로 판정한 다른 판(고른 쪽만 읽은 PDF) — 화면은 "내용 N% 비슷" 으로 보인다
        if let Some(sc) = near {
            dl["near"] = json!((sc * 100.0).round());
        }
        add_meta(&mut dl, key, meta_rows.as_deref());
        lines.push(dl);
        if *group == "copy" {
            n_copy += 1;
        }
    }
    for (si, (j, score)) in similar.iter().enumerate() {
        no += 1;
        let key = &scan.docs[*j];
        let name = pl_str(&scan.first[*j].1, "doc_name").to_string();
        let mut dl = doc_line(no, key, &name, &[], "", docs_state.get(key), Some(*score), false, 0);
        dl["group"] = json!("similar");
        dl["pct"] = json!((score * 100.0).round());
        dl["overlap"] = json!(round2(scan.same[*j].len() as f64 / nb));
        if !copies[si].is_empty() {
            dl["copies"] = json!(copies[si]);
        }
        add_meta(&mut dl, key, meta_rows.as_deref());
        // 가장 비슷한 짝 — 상대 청크가 겹치지 않게 점수 순으로 PAIRS 개
        let mut q: Vec<(usize, (f32, i64))> = scan.best[*j].iter().copied().enumerate().filter(|(_, b)| b.1 >= 0).collect();
        q.sort_by(|a, b| b.1 .0.partial_cmp(&a.1 .0).unwrap_or(std::cmp::Ordering::Equal));
        let mut used: HashSet<i64> = HashSet::new();
        let mut pls: Vec<Value> = vec![];
        for (qi, (s, id)) in q {
            if pls.len() >= PAIRS || !used.insert(id) {
                continue;
            }
            if let Some(tp) = theirs.get(&id) {
                pls.push(pair_line(no, tp, &pts[picks[qi]].2, s));
            }
        }
        dl["hits"] = json!(pls.len());
        lines.push(dl);
        places += pls.len();
        lines.extend(pls);
    }

    let src_name = pl_str(&pts[0].2, "doc_name").to_string();
    let done = json!({
        "kind": "similar", "ms": (t0.elapsed().as_secs_f64() * 1000.0).round(),
        "scan_ms": scan_ms.round(), "embed_ms": embed_ms.round(), "parse_ms": parse_ms.round(),
        "docs": no, "places": places,
        "groups": {"copy": n_copy, "version": versions.len() - n_copy, "similar": similar.len()},
        "source": {"path": path, "doc_path": src, "name": src_name, "chunks": pts.len(), "used": picks.len(), "mode": mode,
                   "pages_read": pages.map(|p| p.0), "pages_total": pages.map(|p| p.1), "text": summary_text(&pts)},
        "scope_docs": scope.as_ref().map(|(n, _)| json!(n)).unwrap_or(Value::Null),
    });
    Ok((lines, done))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keep_pages_spread() {
        // 300쪽 이하는 전부
        assert!((0..300).all(|i| keep_page(i, 300, 32, 2)));
        // 2,705쪽 · 32곳 × 2쪽 = 64쪽, 첫 곳은 1 · 2쪽(0 · 1)
        let k: Vec<usize> = (0..2705).filter(|&i| keep_page(i, 2705, 32, 2)).collect();
        assert_eq!(k.len(), 64);
        assert_eq!(&k[..2], &[0, 1]);
        // 64곳 × 1쪽
        assert_eq!((0..2705).filter(|&i| keep_page(i, 2705, 64, 1)).count(), 64);
    }

    #[test]
    fn even_picks_spread() {
        assert_eq!(pick_even(5, 32), vec![0, 1, 2, 3, 4]);
        let p = pick_even(100, 4);
        assert_eq!(p, vec![0, 25, 50, 75]);
        assert_eq!(pick_even(64, 32).len(), 32);
    }

    #[test]
    fn body_key_ignores_prefix_and_space() {
        let a = "[문서A > 1장]\n가나다라마바사아자차카타파하 가나다라마바사아자차카타파하 가나다라마바사아자차카타파하";
        let b = "[문서B > 3절]\n가나다라마바사아자차카타파하가나다라마바사아자차카타파하  가나다라마바사아자차카타파하";
        assert!(body_key(a).is_some());
        assert_eq!(body_key(a), body_key(b));
        assert_eq!(body_key("[문서 > 절]\n짧은 글"), None);
    }
}
