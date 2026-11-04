//------------------------------------------------------------------
// 탐색기 모드 AI 정리 (탐색기 AI 정리 · 벡터 백그라운드 설계서 §4)
//=> 답을 찾지 않고, 화면 목록의 상위 문서를 문서별로 한 줄씩 요약한다. 다시 검색하지 않는다.
//    1) 화면이 관련도 순으로 보낸 후보에서 본문이 거의 같은 문서를 뺀다
//    2) 앞 MAX_DOCS 건을 문서마다 한 번씩 생성한다(작은 모델은 한 번에 묶으면 형식이 무너진다)
//    3) 쓸 수 없는 줄은 찾기 발췌로 바꾼다(되뱉기 · 근거에 없는 숫자 · 너무 짧은 줄)
//   공통 요약은 만들지 않는다 — 작은 모델은 문서별 줄을 이어 붙일 뿐 새 정보가 없었다(2026-11-03 실사용).
//   출력은 ── 근거 / ── 답변 / ── 소요 블록을 그대로 쓴다 — 워커 해석기를 새로 만들지 않는다.
//   답변 블록의 줄: "[겹침] n" · "[n] 글" · "[n!] 발췌"(앞서 쓴 n 번 줄을 바꿈) · "[오류] 글"
//------------------------------------------------------------------

use std::collections::HashSet;
use std::sync::atomic::Ordering;
use std::time::Instant;

use serde_json::{json, Value};

use crate::app::App;
use crate::cli::{answer_setup_line, out, outln, INTERRUPT};
use crate::find::{clean_body, split_prefix};
use crate::generate::prompts::{numbers_supported, BRIEF_LINE_SYSTEM};
use crate::pyutil;
use crate::store::pl_str;

pub const MAX_DOCS: usize = 3;      // 정리할 문서 수 상한 — 네 번째 줄은 새 내용이 드물다(설계서 §4-2)
const CAP_CHARS: usize = 450;       // 문서 하나에서 모델에 넣는 글자 수
const SAME_RATIO: f64 = 0.8;        // 글자 2개 조각이 이만큼 겹치면 같은 내용으로 본다
const LINE_TOKENS: usize = 90;      // 문서별 한 줄의 토큰 상한
const MIN_LINE_CHARS: usize = 20;   // 이보다 짧으면 문장이 아니라 조각이다
const INTERRUPTED: &str = "KeyboardInterrupt";
// 모델이 지시문을 옮겨 적을 때 나오는 말 — 원문에 있으면 되뱉기가 아니다
const ECHO_WORDS: [&str; 3] = ["한 문장", "문서 내용", "검색어"];

//------------------------------------------------------------------
// 정리할 문서 하나
// -필드: no = 화면에 보일 번호(겹침을 뺀 뒤 1부터) / doc · path · page = 문서 이름 · 경로 · 쪽
// -필드: text = 모델에 넣을 글 / snippet = 쓸 수 없는 줄을 대신할 찾기 발췌
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct Item {
    pub no: usize,
    pub doc: String,
    pub path: String,
    pub page: Value,
    pub text: String,
    pub snippet: String,
}

// 공백을 뺀 글자 2개 조각들 — 복사본 · 버전만 다른 문서를 가려낸다
fn grams(s: &str) -> HashSet<(char, char)> {
    let c: Vec<char> = s.chars().filter(|c| !c.is_whitespace()).collect();
    c.windows(2).map(|w| (w[0], w[1])).collect()
}

// 겹침 정도 — 교집합 / 작은 쪽. 한쪽이 잘려 있어도 같은 글을 잡으려고 작은 쪽으로 나눈다
fn overlap(a: &HashSet<(char, char)>, b: &HashSet<(char, char)>) -> f64 {
    a.intersection(b).count() as f64 / a.len().min(b.len()).max(1) as f64
}

//------------------------------------------------------------------
// 겹치는 문서 빼고 앞에서 limit 건 고르기
//=> 후보 순서(관련도)대로 보며 앞서 고른 문서와 거의 같은 본문은 건너뛴다.
//
// -in: items = 후보(관련도 순) / limit = 고를 수
//
// -out: (고른 문서 — no 를 1부터 다시 매김, 겹쳐서 뺀 수)
// -out: error = 없음
//------------------------------------------------------------------
pub fn pick(items: Vec<Item>, limit: usize) -> (Vec<Item>, usize) {
    let mut kept: Vec<Item> = vec![];
    let mut seen: Vec<HashSet<(char, char)>> = vec![];
    let mut dropped = 0;
    for mut it in items {
        if kept.len() >= limit {
            break;
        }
        let g = grams(&it.text);
        if seen.iter().any(|h| overlap(&g, h) >= SAME_RATIO) {
            dropped += 1;
            continue;
        }
        seen.push(g);
        it.no = kept.len() + 1;
        kept.push(it);
    }
    (kept, dropped)
}

//------------------------------------------------------------------
// 생성한 줄을 그대로 보여도 되는가
//=> 너무 짧거나, 지시문을 옮겨 적었거나, 원문에 없는 수치가 있으면 false.
//
// -in: line = 생성한 줄 / source = 문서 이름과 원문
//
// -out: true = 그대로 쓴다
// -out: error = 없음
//------------------------------------------------------------------
pub fn line_ok(line: &str, source: &str) -> bool {
    pyutil::clen(line) >= MIN_LINE_CHARS
        && !ECHO_WORDS.iter().any(|w| line.contains(w) && !source.contains(w))
        && numbers_supported(line, source)
}

// 줄을 쓸 수 없을 때 보일 발췌 — 찾기 발췌가 없으면 원문 앞부분
fn excerpt(it: &Item) -> String {
    let s = if it.snippet.trim().is_empty() { it.text.as_str() } else { it.snippet.as_str() };
    pyutil::head(&s.split_whitespace().collect::<Vec<_>>().join(" "), 120).to_string()
}

//------------------------------------------------------------------
// 조건만 맞은 문서처럼 쪽 줄이 없는 문서의 글 — 문서 앞쪽 청크
//
// -in: app = 앱 / path = 문서 경로
//
// -out: (글, 쪽) — 인덱스에 청크가 없으면 빈 글
// -out: error = 저장소 읽기 실패면 Err
//------------------------------------------------------------------
fn head_text(app: &App, path: &str) -> Result<(String, Value), String> {
    let docs: HashSet<String> = std::iter::once(path.to_string()).collect();
    let (_, ids) = app.store.docs_scope(&docs, None)?;
    let mut ids: Vec<i64> = ids.into_iter().collect();
    // 같은 문서의 청크는 id 순서가 문서 순서다
    ids.sort();
    ids.truncate(2);
    let pl = app.store.payloads(&ids)?;
    let parts: Vec<String> = ids.iter().filter_map(|i| pl.get(i))
        .map(|p| clean_body(&split_prefix(pl_str(p, "text")).0)).collect();
    let page = ids.first().and_then(|i| pl.get(i)).and_then(|p| p.get("page").cloned()).unwrap_or(Value::Null);
    Ok((parts.join("\n"), page))
}

//------------------------------------------------------------------
// 요청 글을 문서 목록으로
//=> 글이 없는 후보는 인덱스에서 앞쪽 청크를 읽고, 그래도 없으면 뺀다(스캔 문서 등).
//
// -in: app = 앱 / req = {"items": [{doc, path, page, text, snippet}]}
//
// -out: 후보(요청 순서)
// -out: error = 없음 — 읽지 못한 후보만 빠진다
//------------------------------------------------------------------
fn read_items(app: &App, req: &Value) -> Vec<Item> {
    let list = req.get("items").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let mut out = vec![];
    for v in list {
        let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
        let mut it = Item { no: 0, doc: s("doc"), path: s("path"), page: v.get("page").cloned().unwrap_or(Value::Null),
                            text: s("text"), snippet: s("snippet") };
        // 모두 찾기 쪽 줄은 본문 대신 청크 id 를 싣는다 — 그 청크들의 글을 쓴다
        let ids: Vec<i64> = v.get("ids").and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default();
        if it.text.trim().is_empty() && !ids.is_empty() {
            if let Ok(pl) = app.store.payloads(&ids) {
                it.text = ids.iter().filter_map(|i| pl.get(i)).map(|p| clean_body(&split_prefix(pl_str(p, "text")).0)).collect::<Vec<_>>().join("\n");
            }
        }
        if it.text.trim().is_empty() && !it.path.is_empty() {
            if let Ok((t, p)) = head_text(app, &it.path) {
                it.text = t;
                it.page = p;
            }
        }
        if it.text.trim().is_empty() {
            continue;
        }
        it.text = pyutil::head(&it.text, CAP_CHARS).to_string();
        out.push(it);
    }
    out
}

// 근거 블록 — 화면이 [n] 을 눌렀을 때 쓸 경로 · 쪽 · 글을 @ev 줄로 함께 싣는다
fn print_evidence(items: &[Item]) {
    outln(&format!("\n── 근거 {}건 (0ms) ─────────────────────", items.len()));
    for it in items {
        outln(&format!("  [{}] {}", it.no, it.doc));
        outln(&format!("      {}", excerpt(it)));
        let v = json!({"no": it.no, "path": it.path, "text": it.text, "page": it.page});
        outln(&format!("  @ev {}", pyutil::json_ascii(&v, false)));
    }
    outln("\n── 답변 (AI 참고 답변 — 원문으로 확인하세요) ──");
}

//------------------------------------------------------------------
// 문서별 한 줄 쓰기
//=> 문서를 차례로 생성해 흘려보낸다. 쓸 수 없는 줄은 발췌로 바꾼다. 취소되면 Err 로 돌아간다.
//
// -in: app = 앱 / q = 검색어 / items = 정리할 문서 / first = 첫 글자 시각
//
// -out: Ok(())
// -out: error = 생성 실패 · 취소면 Err
//------------------------------------------------------------------
fn doc_lines(app: &App, q: &str, items: &[Item], first: &mut Option<Instant>) -> Result<(), String> {
    for it in items {
        if INTERRUPT.load(Ordering::SeqCst) {
            return Err(INTERRUPTED.into());
        }
        let user = format!("[문서 내용]\n{}\n{}\n\n[검색어]\n{}", it.doc, it.text, q);
        out(&format!("[{}] ", it.no));
        let o = app.generator.stream_chat(BRIEF_LINE_SYSTEM, &user, LINE_TOKENS, Some("\n"), &mut |p| {
            first.get_or_insert_with(Instant::now);
            out(p);
        })?;
        outln("");
        let line = o.text.trim();
        if !line_ok(line, &format!("{}\n{}", it.doc, it.text)) {
            outln(&format!("[{}!] {}", it.no, excerpt(it)));
        }
    }
    Ok(())
}

//------------------------------------------------------------------
// /brief 처리 (핵심)
//=> 문서를 고르고 근거 블록 · 문서별 줄 · 소요 블록을 차례로 낸다.
//   어떤 경우에도 소요 블록까지 내보낸다 — 해석기는 그 표식으로 답이 끝난 줄 안다.
//
// -in: app = 앱 / raw = 요청 JSON {"q", "items"}
//
// -out: 없음 (표준 출력)
// -out: error = 없음 — 실패는 "[오류]" 줄로 낸다
//------------------------------------------------------------------
pub fn run(app: &App, raw: &str) {
    let t0 = Instant::now();
    INTERRUPT.store(false, Ordering::SeqCst);
    let req: Value = serde_json::from_str(raw).unwrap_or(Value::Null);
    let q = req.get("q").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let (items, dropped) = pick(read_items(app, &req), MAX_DOCS);
    print_evidence(&items);
    if dropped > 0 {
        outln(&format!("[겹침] {}", dropped));
    }
    let mut first: Option<Instant> = None;
    let r = doc_lines(app, &q, &items, &mut first);
    let cancelled = matches!(&r, Err(e) if e.contains(INTERRUPTED));
    match r {
        Err(e) if !cancelled => outln(&format!("\n[오류] {}", pyutil::head(&e, 200))),
        Err(_) => outln(""),
        Ok(()) => {}
    }
    let end = Instant::now();
    outln("\n\n── 소요 ─────────────────────────────────");
    outln(&format!("  첫 글자 {:.2}s / 완료 {:.2}s", first.unwrap_or(end).duration_since(t0).as_secs_f64(), end.duration_since(t0).as_secs_f64()));
    outln(&format!("  {}", answer_setup_line(app)));
    if cancelled {
        outln("  취소됨");
    }
    outln("");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(text: &str) -> Item {
        Item { text: text.into(), ..Default::default() }
    }

    #[test]
    fn picks_top_without_copies() {
        let a = "문서 분류 체계는 관리자 페이지에서 항목을 만들고 사용자 페이지에서 설정한다";
        let v = vec![item(a), item(&format!("{} 입니다", a)), item("보안 등급은 기밀 민감 공개로 나눈다"), item("개인정보 처리 위탁 계약서 내용"), item("다섯째 문서의 다른 내용")];
        let (kept, dropped) = pick(v, 3);
        assert_eq!(kept.len(), 3);
        assert_eq!(dropped, 1);
        assert_eq!(kept.iter().map(|i| i.no).collect::<Vec<_>>(), vec![1, 2, 3]);
    }

    #[test]
    fn rejects_bad_lines() {
        let src = "MpowerAI 12 로드맵 2026년말까지 정확도 향상을 목표로 한다";
        assert!(line_ok("MpowerAI 로드맵은 2026년말까지 정확도 향상을 목표로 합니다.", src));
        // 근거의 숫자를 단계 수로 지어낸 실제 사례(2026-11-03 시험)
        assert!(!line_ok("MpowerAI의 로드맵에 따르면, 12단계의 발전 과정이 포함되어 있습니다.", src));
        assert!(!line_ok("문서 분류 체계 카테고리 이름", src));
        assert!(!line_ok("이 문서는 개인정보 처리 업무를 설명하는 한 문장입니다.", src));
    }
}
