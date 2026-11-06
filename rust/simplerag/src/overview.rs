//------------------------------------------------------------------
// 업로드 문서 개요 (plan/채팅_첨부_드래그앤드롭_요약질문_설계서_20261106.md §3)
//=> "이 문서는 무슨 내용?" · "요약해줘" 같은 질문은 문서 전체가 답이라 검색으로는 맞는 조각을 고를 수 없다.
//   올린 문서가 있는 대화에서만, 질문을 규칙으로 가려(LLM 라우터 없음 — TTFT · 작은 모델 정확도 때문)
//   문서마다 앞 · 가운데 · 끝 조각을 모아 개요 답을 만든다.
//    1) is_overview — 개요를 묻는 말이 있고, 그 말과 문서 지칭어를 빼면 본문 낱말이 남지 않을 때만 true
//       (본문 질문이 개요로 잘못 가는 쪽이 더 나쁘므로 보수적으로 — "갱신 금액 요약해줘" 는 검색)
//    2) pick — 문서별로 조각을 골라 글자 상한까지 자른다(n_ctx 2048 안에 들어가게)
//   검색이 문턱에 걸려 근거가 0건일 때도 거절 대신 이 개요로 넘긴다(app.rs answer — overview "fallback").
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use crate::pyutil;
use crate::retrieve::{keywords, Chunk};
use crate::store::pl_str;
use crate::uploads::UploadCorpus;

// 문서 1건일 때 모델에 넣는 글자 수 · 여러 건일 때 문서 하나에 넣는 글자 수(brief.rs CAP_CHARS 와 같은 값)
pub const CAP_ONE: usize = 1400;
pub const CAP_EACH: usize = 450;
// 문서 하나에서 고르는 조각 수 상한(앞 2 · 가운데 1 · 끝 1)
const PICK_MAX: usize = 4;
// 개요 답에 넣을 문서 수 상한(업로드 한도와 같다)
pub const MAX_DOCS: usize = 5;
// 검색이 막혀 개요로 넘어갈 때 답 앞에 붙이는 한 줄
pub const FALLBACK_NOTE: &str = "질문과 딱 맞는 부분은 찾지 못해 문서 개요를 보여 드립니다.\n";

// 개요를 묻는 말 — 이 중 하나가 있어야 개요 후보다
static CUE: Lazy<Regex> = Lazy::new(|| Regex::new(concat!(
    r"(?i)(요약|정리|개요|핵심|요점|간추|한눈에|주제|무슨\s*내용|어떤\s*내용|뭔\s*내용|무슨\s*문서|어떤\s*문서|",
    r"무엇에\s*관한|뭐에\s*관한|무엇을\s*다루|뭘\s*다루|무엇에\s*대한|뭐에\s*대한|무슨\s*얘기|무슨\s*이야기|주요\s*내용|전체\s*내용|전반적|설명|소개|",
    r"summar|overview|what.{0,30}about|tl;?dr)")).unwrap());
// 문서를 가리키는 말 — 지우고 본다("이문서는" → "는")
static DOC_REF: Lazy<Regex> = Lazy::new(|| Regex::new(
    r"(?i)(이|그|저|해당|첨부한?|올린|업로드한?|이번)?\s*(문서|파일|자료|서류|계약서|보고서|견적서|제안서|기획서|계획서|명세서|회의록|논문|지침|규정|규칙|매뉴얼|안내서|공문|pdf|document|file|doc)"
).unwrap());
// 남아도 본문 낱말이 아닌 말의 앞부분 — 낱말이 이것으로 시작하면 버린다(소문자 비교)
const STOP: &[&str] = &[
    "요약", "정리", "개요", "핵심", "요점", "간추", "한눈", "주제", "내용", "무슨", "어떤", "무엇", "뭐", "뭔", "뭘", "관한", "관해",
    "대해", "대한", "다루", "간단", "짧게", "전체", "전반", "대략", "대충", "주요", "주로", "해줘", "해주", "해 줘", "알려", "설명",
    "소개", "말해", "보여", "얘기", "이야기", "부탁", "주세", "줄래", "인가", "이야", "입니", "있나", "인지", "한번", "가요", "나요", "세요",
    "이거", "이건", "이게", "이것", "그거", "그건", "저거",
    "what", "about", "this", "the", "is", "it", "please", "tell", "me", "give", "summar", "overview", "tl", "dr", "can", "you",
];

//------------------------------------------------------------------
// 개요형 질문인가
//=> 개요를 묻는 말(CUE)이 있고, 문서 지칭어 · 개요 말 · 질문 꼬리를 다 빼면 본문 낱말이 하나도 남지 않을 때만 true.
//    1) CUE 가 없으면 false
//    2) 문서 지칭어를 지운 뒤 retrieve::keywords 로 낱말(조사 뗌 · 흔한 질문 말 뺌)을 뽑는다
//    3) STOP 으로 시작하는 낱말을 버리고, 남는 게 없으면 true
//
// -in: q = 사용자 질문
//
// -out: bool = 개요 경로로 보낼 질문이면 true
// -out: error = 없음
//------------------------------------------------------------------
pub fn is_overview(q: &str) -> bool {
    if !CUE.is_match(q) {
        return false;
    }
    let rest = DOC_REF.replace_all(q, " ");
    keywords(&rest).iter().all(|w| {
        let w = w.to_lowercase();
        // 개요 말 · 질문 꼬리로 시작하면 본문 낱말이 아니다
        STOP.iter().any(|s| w.starts_with(s))
    })
}

//------------------------------------------------------------------
// 문서 하나에서 조각 고르기 — 앞 2 · 가운데 1 · 끝 1 (겹치면 하나로)
//=> 앞부분은 표지 · 목차일 때가 많아 가운데와 끝도 하나씩 섞는다. 조각 순서는 문서 순서 그대로.
//
// -in: n = 문서의 조각 수
//
// -out: Vec<usize> = 고른 조각 자리(0부터, 오름차순)
// -out: error = 없음 (n=0 이면 빈 목록)
//------------------------------------------------------------------
pub fn pick_positions(n: usize) -> Vec<usize> {
    let mut v: Vec<usize> = [0, 1, n / 2, n.saturating_sub(1)].into_iter().filter(|i| *i < n).collect();
    v.sort();
    v.dedup();
    v.truncate(PICK_MAX);
    v
}

//------------------------------------------------------------------
// 업로드 코퍼스 → 문서별 개요 조각
//=> 조각을 문서(doc_path)별로 묶어 chunk_idx 순으로 세우고, pick_positions 로 고른 뒤 글자 상한까지 자른다.
//    1) 문서는 처음 올린 순서(조각 id 순)
//    2) 문서 1건이면 CAP_ONE, 여러 건이면 문서마다 CAP_EACH 글자를 고른 조각에 나눠 준다
//
// -in: c = 대화의 업로드 코퍼스
//
// -out: Vec<Vec<Chunk>> = 문서마다 고른 조각(최대 MAX_DOCS 건)
// -out: error = 없음 (조각이 없으면 빈 목록)
//------------------------------------------------------------------
pub fn pick(c: &UploadCorpus) -> Vec<Vec<Chunk>> {
    let mut ids: Vec<i64> = c.payloads.keys().copied().collect();
    ids.sort();
    // 문서별로 묶기 — 처음 나온 순서를 지킨다
    let mut order: Vec<String> = vec![];
    let mut by_doc: std::collections::HashMap<String, Vec<Chunk>> = std::collections::HashMap::new();
    for id in ids {
        let p = &c.payloads[&id];
        let path = pl_str(p, "doc_path").to_string();
        if !by_doc.contains_key(&path) {
            order.push(path.clone());
        }
        by_doc.entry(path.clone()).or_default().push(Chunk {
            id,
            text: pl_str(p, "text").to_string(),
            doc_name: pl_str(p, "doc_name").to_string(),
            doc_path: path,
            folder: pl_str(p, "folder").to_string(),
            chunk_idx: p.get("chunk_idx").and_then(|v| v.as_i64()).unwrap_or(0),
            section_id: p.get("section_id").and_then(|v| v.as_i64()),
            child_text: None,
        });
    }
    order.truncate(MAX_DOCS);
    let cap = if order.len() <= 1 { CAP_ONE } else { CAP_EACH };
    order.into_iter().map(|path| {
        let mut chunks = by_doc.remove(&path).unwrap_or_default();
        chunks.sort_by_key(|c| c.chunk_idx);
        let picked: Vec<Chunk> = pick_positions(chunks.len()).into_iter().map(|i| chunks[i].clone()).collect();
        // 글자 상한을 고른 조각 수로 나눠 각 조각을 자른다 — 한 조각이 상한을 다 먹지 않게
        let each = (cap / picked.len().max(1)).max(80);
        picked.into_iter().map(|mut ch| {
            if pyutil::clen(&ch.text) > each {
                ch.text = pyutil::head(&ch.text, each).to_string();
            }
            ch
        }).collect()
    }).filter(|v: &Vec<Chunk>| !v.is_empty()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // 개요형 — 개요 말만 있고 본문 낱말이 없는 질문
    #[test]
    fn overview_questions() {
        for q in ["이문서는 무슨내용인가?", "이 문서는 무슨 내용인가요?", "요약해줘", "요약해 줘", "핵심만 정리해줘",
                  "이 파일 어떤 문서야?", "올린 자료 간단히 요약해 주세요", "이 계약서 무슨 내용이야", "한눈에 정리해줘",
                  "What is this document about?", "이 지침 요약해줘", "이거 뭐에 관한 문서야?", "간단히 설명해줘", "이 규정은 어떤 내용이야?", "summarize this", "전체 내용 요약", "주요 내용 알려줘", "이 문서 개요"] {
            assert!(is_overview(q), "개요형이어야 함: {}", q);
        }
    }

    // 본문 질문 — 개요 말이 있어도 본문 낱말이 남으면 검색으로
    #[test]
    fn content_questions() {
        for q in ["갱신 금액은 얼마인가?", "갱신 금액 요약해줘", "결제 조건 정리해줘", "핵심 성과 지표는?", "주제별 예산은?",
                  "이 문서에서 담당자 이메일은?", "36개월 옵션 가격은?", "summarize the pricing", "연차 규정 요약",
                  "견적 만료일은 언제인가?", "세금 포함 여부는?", "연차 규정은 어떤 내용이야?", "출장 지침 요약", "휴가 신청 방법 설명해줘", "이거 금액이 얼마야?"] {
            assert!(!is_overview(q), "본문 질문이어야 함: {}", q);
        }
    }

    #[test]
    fn positions() {
        assert!(pick_positions(0).is_empty());
        assert_eq!(pick_positions(1), vec![0]);
        assert_eq!(pick_positions(2), vec![0, 1]);
        assert_eq!(pick_positions(11), vec![0, 1, 5, 10]);
    }
}
