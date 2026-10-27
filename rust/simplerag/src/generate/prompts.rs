//------------------------------------------------------------------
// 프롬프트 조립 + 출력 정리 (Python generate/prompts.py · chatformat.py)
//=> RAG 생성 단계의 '문자열 계약'을 한곳에 모은다. 글자 하나까지 Python 판과 같아야 같은 답이 나온다.
//    · 시스템 지시문(v0·v4·v5·custom)
//    · 사용자 메시지 "[근거]\n[1] …\n\n[질문]\n…"
//    · 모델별 채팅 형식(qwen3 · midm · kanana)과 중단 문자열
//    · 지시문 되뱉기·중복 인용·없는 인용 번호 정리
//------------------------------------------------------------------

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;

use crate::config::{self, cfg};
use crate::pyutil;

pub const SYSTEM_PROMPT: &str = "당신은 사내 문서 검색 도우미입니다. 아래 [근거] 안의 내용만 사용해 한국어로 간결하게 답변하세요. 근거에 없는 내용은 추측하지 마세요. 답변은 3~5문장으로 요약하고 마지막에 근거 번호를 [1] 형식으로 표기하세요.";
pub const SYSTEM_PROMPT_V4: &str = "당신은 사내 문서 검색 도우미입니다. 아래 [근거] 안의 내용만 사용해 한국어로 간결하게 답변하세요. 근거에 없는 내용은 추측하지 마세요. 질문에 대한 답을 첫 문장에 바로 쓰고, 답변은 1~2문장으로 요약하고 마지막에 근거 번호를 [1] 형식으로 표기하세요.";
pub const SYSTEM_PROMPT_V5: &str = "당신은 사내 문서 검색 도우미입니다. 아래 [근거] 안의 내용만 사용해 한국어로 간결하게 답변하세요. 근거에 없는 내용은 추측하지 마세요. 근거에 적힌 숫자·금액·기간·명칭은 바꾸거나 계산하지 말고 근거에 적힌 그대로 옮겨 쓰세요. 답변은 3~5문장으로 요약하고 마지막에 근거 번호를 [1] 형식으로 표기하세요.";

//------------------------------------------------------------------
// 지금 설정의 시스템 지시문
//------------------------------------------------------------------
pub fn active_system_prompt() -> String {
    match cfg().prompt.as_str() {
        "custom" => cfg().system_prompt_text.clone(),
        "v4" => SYSTEM_PROMPT_V4.into(),
        "v5" => SYSTEM_PROMPT_V5.into(),
        _ => SYSTEM_PROMPT.into(),
    }
}

//------------------------------------------------------------------
// 사용자 메시지 — 근거에 번호를 매긴다(설정이 켜져 있으면 표를 평문으로 편다)
//------------------------------------------------------------------
pub fn build_user_message(query: &str, chunks: &[String]) -> String {
    let blocks: Vec<String> = chunks.iter().enumerate().map(|(i, c)| {
        let c = if cfg().flatten_tables { super::tableflat::flatten_tables(c) } else { c.clone() };
        format!("[{}] {}", i + 1, c)
    }).collect();
    format!("[근거]\n{}\n\n[질문]\n{}", blocks.join("\n\n"), query)
}

const MIDM_PREAMBLE: &str = concat!(
    "Cutting Knowledge Date: December 2024\n",
    "Today Date: 04 Jul 2025\n\n",
    "Mi:dm(믿:음)은 KT에서 개발한 AI 기반 어시스턴트이다. 너는 Mi:dm으로서 사용자에게 ",
    "유용하고 안전한 응답을 제공해야 한다.\n\n",
    "Mi:dm은 December 2024까지의 지식으로 학습되었으며 그 외의 지식을 묻는 경우에는 ",
    "한계를 인정해야 한다.\n",
    "오늘 날짜는 04 Jul 2025임을 참고하여 응답을 제공한다.\n\n",
    "어시스턴트는 기본적으로 \"한국어\"를 사용한다. 사용자의 요청에 따라 생각하고 응답하는 ",
    "언어는 달라질 수 있으며, 다른 요구사항이 없다면 입력 언어를 따라 응답하라.\n",
    "코드 작성 시에는 요구되는 언어의 소스코드로 작성해야 하며, STEM(과학, 기술, 공학, 수학) ",
    "분야의 전문 용어는 원문을 그대로 유지하여 출력한다.\n\n",
    "Mi:dm은 사용자 친화적으로 답변을 제공해야 한다. 사용자의 요청이 없다면 기본적으로 ",
    "경어체를 사용해야 한다.\n",
    "사용자의 요청에 따라 유용하고 관련성 있는 답변을 제공해야 한다. 이는 요청의 내용을 ",
    "반영하여 이루어져야 한다.\n",
    "특히, 사용자가 특정 답변의 형태를 요구한 경우 해당 형태를 반드시 준수하여 응답을 ",
    "제공한다.(예. JSON, SQL, 코드 등) 그렇지 않다면 응답을 제공하기 가장 적절한 형태로 제공한다.\n",
    "다양한 시각이 존재하는 주제에 대해서는 중립적인 입장을 유지해야 하지만, 한국 특화 ",
    "인공지능 어시스턴트로서 정치적, 역사적, 외교적으로 한국인의 입장에서 응답을 생성해야 한다.\n",
    "모호하거나 복잡한 요청으로 인해 답변 작성이 어려운 경우, 다시 한 번 생각해보고 추가정보를 ",
    "요청해야 한다.\n\n",
    "Mi:dm은 응답을 제공할 때 어시스턴트의 안전성 측면에서 다음 지침을 *반드시* 준수해야 한다.\n",
    "- 비속어와 욕설을 사용하지 않아야 한다.\n",
    "- 신뢰할 수 있는 응답을 생성하고, 전문영역에 대한 한계와 불확실성을 인정해야 한다.\n",
    "- 사회의 보편적 규범과 가치에 따라 윤리적이고 중립적이어야 하며, 편향성을 지녀서는 안 된다.\n",
    "- 인공지능으로서의 정체성을 인지하고 의인화하지 않아야 한다.\n",
    "- 개인정보, 사생활 등 민감정보를 포함한 요청에 대한 답변을 거절해야 한다. 다만, 해당정보를 ",
    "사용할 수 없는 형태(비식별화된 형태)로 제공하는 것은 제한적으로 응답을 허용한다.\n\n",
    "이 모든 지침은 응답을 제공할 때 출력되지 않아야 한다.\n\n",
    "Mi:dm은 사용자의 요청을 처리하기 위해 제공된 도구(함수)를 호출할 수 있다.\n",
);

//------------------------------------------------------------------
// 모델에 맞는 채팅 프롬프트
//=> qwen3 은 <think> 를 미리 닫아 사고 토큰 생성을 막는다(약 3배 빠르다).
//------------------------------------------------------------------
pub fn build_chat_prompt(system: &str, user: &str, alias: &str) -> String {
    match config::gen_format(alias) {
        "midm" => format!(
            "<|start_header_id|>system<|end_header_id|>\n\n{}{}<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n{}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n",
            MIDM_PREAMBLE, system, user),
        "kanana" => format!(
            "<|start_header_id|>system<|end_header_id|>\n\n\n<|eot_id|><|start_header_id|>system<|end_header_id|>\n\n{}<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n{}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n",
            system, user),
        _ => format!(
            "<|im_start|>system\n{}<|im_end|>\n<|im_start|>user\n{}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
            system, user),
    }
}

// Qwen3 형식(백엔드 측정용 — prompts.build_prompt 와 같다)
pub fn build_prompt(system: &str, user: &str) -> String {
    build_chat_prompt(system, user, config::PRECISE_GEN_MODEL)
}

//------------------------------------------------------------------
// 모델에 맞는 생성 중단 문자열
//------------------------------------------------------------------
pub fn stop_strings(alias: &str) -> Vec<&'static str> {
    match config::gen_format(alias) {
        "midm" | "kanana" => vec!["<|eot_id|>", "<|end_of_text|>"],
        _ => vec!["<|im_end|>", "<|endoftext|>"],
    }
}

// ── 출력 정리 ─────────────────────────────────────────────

static ECHO_PATTERNS: Lazy<Vec<FRegex>> = Lazy::new(|| {
    [
        r"(?:근거\s*번호를?\s*)?\[\d+\]\s*형식으로\s*(?:근거\s*번호\s*)?(?:표기|표시|답변|작성|기재)[가-힣]*\.?",
        r"답변은\s*\d+\s*[~\-]\s*\d+\s*문장으로\s*요약[가-힣]*\.?",
        r"근거에\s*없는\s*내용은\s*추측하지\s*마(?:세요|십시오)\.?",
        r"근거\s*(?:번호를?)?\s*(?=\[\d+\])",
        r"근거\s*번호\s*[:：]\s*(?=\[\d+\])",
        r"\[?\d+\]?\s*번?\s*의?\s*근거\s*번호는\s*\[?\d+\]?\s*(?:입니다|이다)\.?",
        r"근거\s*번호(?:입니다|이다)\.?",
        r"근거에\s*없는\s*내용은\s*추측하지\s*(?:않습니다|않았습니다|않으며|않고|않아)[,，]?\s*",
        r"[^.\n]{0,40}(?:간결하게|바탕으로|참고하여|근거로)\s*답변(?:합니다|드립니다|하겠습니다)\.?",
        r"(?<![가-힣])번호\s*\[?\d+\]?\s*로\s*답변(?:합니다|드립니다|하겠습니다)\.?",
        r"\[\s*근거\s*번호\s*\d+\s*\]",
        r"\[(?:근거|질문)\]\s*",
    ].iter().map(|p| FRegex::new(p).unwrap()).collect()
});
static MD_BOLD: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?s)\*\*(.+?)\*\*").unwrap());
static MD_STRAY: Lazy<Regex> = Lazy::new(|| Regex::new(r"\*{2,}").unwrap());
static LEADING_MARKER: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\s*\[(?:질문|근거)\]\s*").unwrap());
static TRAILING_CITES: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:\s*\[\d+\]\s*번?)+\s*$").unwrap());
static CITE: Lazy<Regex> = Lazy::new(|| Regex::new(r"\[(\d+)\]").unwrap());
static WS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());
static SP_PUNCT: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+([.,])").unwrap());
static DOTS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\.(?:\s*\.)+").unwrap());
static LEAD_PUNCT: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[.,]\s*").unwrap());

// fancy-regex 로 모두 바꾸기
fn fsub(re: &FRegex, s: &str, rep: &str) -> String {
    re.replace_all(s, rep).into_owned()
}

// ── 짧은 되뱉기 (거르기 켬·끔과 상관없이 지운다 — 설계서 ②③, Python _SHORT_* 와 같다) ──
//   모양이 고정된 짧은 잡음이라 몇 글자만 붙들면 판정된다. 골든셋 100문항(1.7B, 거르기 끔)에서 남은 9건:
//   "근거 [1]에 따르면" 5건 · "근거 번호: [2]" · "[1] 형식으로 표기:" · "[1]의 근거 번호는 [1]입니다" ·
//   끝 인용 두 번 찍기 "[1][3][1][3]". LIVE 는 스트리밍 중(끝맺음이 와야 지움), FINAL 은 답이 끝난 뒤.
static SHORT_COMMON: Lazy<Vec<FRegex>> = Lazy::new(|| {
    vec![
        FRegex::new(r"\[\d+\]\s*의\s*근거\s*번호는\s*\[\d+\]\s*입니다\.?").unwrap(),
        FRegex::new(r"근거\s*번호\s*:?\s*\[\d+\]").unwrap(),
        // 낱말 '근거' 뒤에 바로 인용 번호가 올 때만 — "근거 규정은 …" 같은 본문은 건드리지 않는다
        FRegex::new(r"(?<![가-힣])근거\s*(?=\[\d+\])").unwrap(),
    ]
});
static SHORT_FORMAT_LIVE: Lazy<FRegex> =
    Lazy::new(|| FRegex::new(r"(?:근거\s*번호\s*)?\[\d+\]\s*형식으로\s*(?:근거\s*번호를?\s*)?표기[^.\n:]*[.:]").unwrap());
static SHORT_FORMAT_FINAL: Lazy<FRegex> =
    Lazy::new(|| FRegex::new(r"(?:근거\s*번호\s*)?\[\d+\]\s*형식으로\s*(?:근거\s*번호를?\s*)?표기[^.\n:]*(?:[.:]|$)").unwrap());
// 인용 덩어리가 곧바로 한 번 더 — "[1][3][1][3]" → "[1][3]"
static DUP_CITE_RUN: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(\[\d+\](?:\s*\[\d+\])*)\s*\1(?!\d)").unwrap());
// 붙들기 판정용 정규형(숫자→1, 공백·쌍점 제거)으로 적은 짧은 되뱉기
const SHORT_CANON: [&str; 7] = ["[1]의근거번호는[1]입니다", "[1]형식으로표기", "[1]형식으로근거번호표기", "[1]형식으로근거번호를표기", "근거번호[1]형식으로표기", "근거번호[1]", "근거[1]"];
const FORMAT_CANON: [&str; 4] = ["[1]형식으로표기", "[1]형식으로근거번호표기", "[1]형식으로근거번호를표기", "근거번호[1]형식으로표기"];
static CANON_LEAD_RUN: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(?:\[1\])*").unwrap());
static CANON_RUN: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(?:\[1\])*(?:\[1?)?$").unwrap());
static CANON_DIGITS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d+").unwrap());
static CANON_DROP: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\s:]").unwrap());
static TERMINATOR: Lazy<Regex> = Lazy::new(|| Regex::new(r"[.:\n]").unwrap());

//------------------------------------------------------------------
// 짧은 되뱉기 지우기 (Python short_clean 과 같다)
//=> SHORT_* 를 지우고 곧바로 되풀이된 인용 덩어리를 하나로 접는다. 공백은 건드리지 않는다.
//   final=false(스트리밍 중)면 "[1] 형식으로 표기" 는 끝맺음(. : 줄바꿈)이 온 뒤에만 지운다.
//------------------------------------------------------------------
pub fn short_clean(text: &str, final_: bool) -> String {
    // "근거 번호 [1] 형식으로 표기…" 를 통째로 지우려고 '형식으로 표기' 를 먼저(Python 과 같은 순서)
    let mut t = fsub(if final_ { &SHORT_FORMAT_FINAL } else { &SHORT_FORMAT_LIVE }, text, "");
    for pat in SHORT_COMMON.iter() {
        t = fsub(pat, &t, "");
    }
    // 여러 번 되풀이돼도 다 접히게 바뀌지 않을 때까지
    loop {
        let folded = fsub(&DUP_CITE_RUN, &t, "$1");
        if folded == t {
            return t;
        }
        t = folded;
    }
}

// ── 인용 번호 바로잡기 (설계서 ⑤, Python recite 와 같다) ──────────────────
//   답의 내용은 맞는데 번호가 엉뚱한 근거를 가리키는 일이 있다(지시문 예시 "[1] 형식으로" 탓인지 [1] 로 쏠린다).
//   문장의 수치가 인용한 근거에는 하나도 없고 다른 근거 하나에만 모두 있으면 그 번호로 바꾼다.
static FACT_NUM: Lazy<Regex> = Lazy::new(|| Regex::new(r"(\d(?:[\d,.]*\d)?)\s*([가-힣%]?)").unwrap());
static SENT_END: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"[.!?](?=\s|$)|\n").unwrap());
static CITE_RUN: Lazy<Regex> = Lazy::new(|| Regex::new(r"\[\d+\](?:[\s,]*\[\d+\])*").unwrap());
static TRAILING_POS: Lazy<Regex> = Lazy::new(|| Regex::new(r"[.!?]\s*$").unwrap());
static NORM_EV: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\s,]").unwrap());

// 문장의 수치 — 인용 번호를 뺀 문장의 (숫자, 단위). 단위 없는 한 자리 숫자(목록 번호 등)는 뺀다
fn facts(sentence: &str) -> Vec<(String, String)> {
    let s = CITE.replace_all(sentence, " ");
    FACT_NUM
        .captures_iter(&s)
        .map(|c| (c[1].replace(',', ""), c.get(2).map_or("", |m| m.as_str()).to_string()))
        .filter(|(num, unit)| num.chars().count() >= 2 || !unit.is_empty())
        .collect()
}

// 수치가 근거에 있는가 — "숫자+단위" 가 있거나, 세 자리 이상 숫자는 숫자만 있어도(표처럼 단위가 떨어진 근거)
fn fact_in(fact: &(String, String), ev: &str) -> bool {
    let (num, unit) = fact;
    ev.contains(&format!("{}{}", num, unit)) || (num.replace('.', "").chars().count() >= 3 && ev.contains(num.as_str()))
}

// 인용 하나가 가리킬 근거 — 문장 수치가 근거 k 에 하나라도 있으면 그대로, 다른 근거 하나에만 모두 있으면 그 번호
fn recite_one(sentence: &str, k: usize, evs: &[String]) -> usize {
    let f = facts(sentence);
    if f.is_empty() || k < 1 || k > evs.len() {
        return k;
    }
    if f.iter().any(|x| fact_in(x, &evs[k - 1])) {
        return k;
    }
    let cands: Vec<usize> = (1..=evs.len()).filter(|&j| j != k && f.iter().all(|x| fact_in(x, &evs[j - 1]))).collect();
    if cands.len() == 1 { cands[0] } else { k }
}

// 문장 경계 끝 위치들(바이트) — SENT_END 의 끝
fn sent_end_positions(s: &str) -> Vec<usize> {
    SENT_END.find_iter(s).filter_map(|m| m.ok()).map(|m| m.end()).collect()
}

//------------------------------------------------------------------
// 인용 번호 바로잡기 (Python recite 와 같다)
//=> 인용 덩어리마다 딸린 문장을 정해 recite_one 으로 고른다.
//    · 문장부호 바로 뒤이고 뒤에 낱말이 이어지지 않는 덩어리("…다. [1]") → 앞 문장의 출처
//    · 그 밖("[1]에 따르면 …", "…다. [3] 근거에 따르면 …") → 덩어리가 들어 있는 문장
//    · 번호가 여럿인 덩어리("[1], [2], [3]")는 건드리지 않는다
//   trailing_only = 앞 문장의 출처인 덩어리만(스트리밍), start = 이 위치(바이트) 앞의 덩어리는 건너뜀
//   돌려줌: (고친 본문, [(원래 번호, 바꾼 번호)])
//------------------------------------------------------------------
pub fn recite(body: &str, evidence: &[String], trailing_only: bool, start: usize) -> (String, Vec<(usize, usize)>) {
    if body.is_empty() || evidence.is_empty() {
        return (body.to_string(), vec![]);
    }
    let evs: Vec<String> = evidence.iter().map(|e| NORM_EV.replace_all(e, "").into_owned()).collect();
    let mut out = String::new();
    let mut fixes = vec![];
    let mut last = 0usize;
    for run in CITE_RUN.find_iter(body) {
        let nums: std::collections::BTreeSet<usize> =
            CITE.captures_iter(run.as_str()).filter_map(|c| c[1].parse().ok()).collect();
        if run.start() < start || nums.len() != 1 {
            continue;
        }
        let k = *nums.iter().next().unwrap();
        let before = &body[..run.start()];
        let after = &body[run.end()..];
        let nxt = after.trim_start_matches([' ', '\t']).chars().next();
        let word_follows = nxt.map_or(false, |c| c.is_alphanumeric());
        let trailing = TRAILING_POS.is_match(before) && !word_follows;
        let sentence: &str = if trailing {
            let head = before.trim_end();
            // 끝의 문장부호 하나를 뺀 곳에서 앞 문장 경계를 찾는다
            let cut = head.char_indices().last().map_or(0, |(i, _)| i);
            let from = sent_end_positions(&head[..cut]).last().copied().unwrap_or(0);
            &head[from..]
        } else if trailing_only {
            continue;
        } else {
            let from = sent_end_positions(before).last().copied().unwrap_or(0);
            let to = SENT_END
                .find_from_pos(body, run.end())
                .ok()
                .flatten()
                .map_or(body.len(), |m| m.end());
            &body[from..to]
        };
        let j = recite_one(sentence, k, &evs);
        if j != k {
            out.push_str(&body[last..run.start()]);
            out.push_str(&CITE.replace_all(run.as_str(), format!("[{}]", j).as_str()));
            last = run.end();
            fixes.push((k, j));
        }
    }
    out.push_str(&body[last..]);
    (out, fixes)
}

// 판정용 정규형 — 숫자 덩어리는 1, 공백·쌍점은 지움
fn canon(s: &str) -> String {
    CANON_DIGITS.replace_all(&CANON_DROP.replace_all(s, ""), "1").into_owned()
}

// 이 꼬리가 짧은 되뱉기(또는 인용 덩어리)의 앞부분일 수 있는가 (Python _may_be_short_echo 와 같다)
fn may_be_short_echo(tail: &str) -> bool {
    let c = canon(tail);
    if c.is_empty() {
        return false;
    }
    if CANON_RUN.is_match(&c) {
        return true;
    }
    // 인용 번호 덩어리 뒤에 짧은 되뱉기가 이어질 수도 있다 — "[1] 근거 [1]에 따르면"(골든셋 q011)
    let rest = &c[CANON_LEAD_RUN.find(&c).map_or(0, |m| m.end())..];
    for x in [c.as_str(), rest] {
        if x.is_empty() {
            continue;
        }
        if SHORT_CANON.iter().any(|lit| lit.starts_with(x)) {
            return true;
        }
        if FORMAT_CANON.iter().any(|lit| x.starts_with(lit)) && !TERMINATOR.is_match(tail) {
            return true;
        }
    }
    false
}

//------------------------------------------------------------------
// 스트리밍에서 끝에서 붙들 글자 수 (Python short_echo_hold 와 같다)
//=> 끝 window 글자 안에서 '[' 나 '근' 으로 시작하는 꼬리 가운데 짧은 되뱉기의 앞부분일 수 있는
//   가장 앞 것부터 붙든다. 없으면 0 — 바로 내보낸다.
//------------------------------------------------------------------
pub fn short_echo_hold(text: &str, window: usize) -> usize {
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let n = chars.len();
    for k in n.saturating_sub(window)..n {
        let (b, ch) = chars[k];
        if (ch == '[' || ch == '근') && may_be_short_echo(&text[b..]) {
            return n - k;
        }
    }
    0
}

//------------------------------------------------------------------
// 없는 인용 번호의 괄호 벗기기 — 근거 n개 밖의 [k] 는 숫자만 남긴다(값일 수 있으니 지우지 않는다)
//------------------------------------------------------------------
pub fn drop_invalid_cites(body: &str, n_evidence: Option<usize>) -> String {
    let n = match n_evidence {
        Some(n) if n > 0 => n,
        _ => return body.to_string(),
    };
    if body.is_empty() {
        return body.to_string();
    }
    CITE.replace_all(body, |c: &regex::Captures| {
        let k: usize = c[1].parse().unwrap_or(0);
        if (1..=n).contains(&k) { c[0].to_string() } else { c[1].to_string() }
    }).into_owned()
}

//------------------------------------------------------------------
// 가벼운 정리 — 스트리밍으로 내보내는 조각에 바로 적용한다(Python soft_clean 과 같다)
//=> strip_instruction_echo 가운데 조각 안에서 끝나는 것만: 굵게 "**글**" → "글", 근거 밖 [k] 괄호 벗기기.
//   공백은 건드리지 않는다(줄바꿈이 화면에 그대로 남게).
//------------------------------------------------------------------
pub fn soft_clean(text: &str, n_evidence: Option<usize>) -> String {
    drop_invalid_cites(&MD_BOLD.replace_all(text, "$1"), n_evidence)
}

//------------------------------------------------------------------
// 되뱉기·중복 인용 정리
//=> 지시문을 옮겨 적은 부분을 지우고, 끝에 반복된 인용을 하나로 접고, 공백을 정리한다.
//   정리 뒤 본문이 사라지면(숫자·괄호·마침표만 남으면) 원문을 돌려준다.
//------------------------------------------------------------------
pub fn strip_instruction_echo(body: &str, n_evidence: Option<usize>) -> String {
    clean_answer(body, n_evidence, true)
}

//------------------------------------------------------------------
// 답변 정리 — 되뱉기 거르기를 켜고 끌 수 있는 본체 (Python clean_answer 와 같다)
//=> echo=false 면 지시문 되뱉기(ECHO_PATTERNS · 첫머리 [질문]/[근거] 표식)만 건너뛴다.
//   굵게 별표 · 없는 인용 번호 · 끝 중복 인용 · 공백 정리는 켜고 끄는 것과 상관없이 한다.
//   (config.yaml generation.echo_filter — 기본 끔. 켜면 화면 첫 글자가 약 2초 늦어진다)
//------------------------------------------------------------------
pub fn clean_answer(body: &str, n_evidence: Option<usize>, echo: bool) -> String {
    let mut cleaned = body.to_string();
    if echo {
        cleaned = LEADING_MARKER.replace(&cleaned, "").into_owned();
        for pat in ECHO_PATTERNS.iter() {
            cleaned = fsub(pat, &cleaned, " ");
        }
    }
    // 짧은 되뱉기·인용 덩어리 중복은 켬·끔과 상관없이 — 붙들 글이 몇 자뿐이라 지연이 없다
    cleaned = short_clean(&cleaned, true);
    cleaned = MD_BOLD.replace_all(&cleaned, "$1").into_owned();
    cleaned = MD_STRAY.replace_all(&cleaned, "").into_owned();
    cleaned = drop_invalid_cites(&cleaned, n_evidence);
    if let Some(m) = TRAILING_CITES.find(&cleaned) {
        let mut seen = std::collections::HashSet::new();
        let mut uniq = vec![];
        for c in CITE.captures_iter(m.as_str()) {
            let n = c[1].to_string();
            if seen.insert(n.clone()) {
                uniq.push(n);
            }
        }
        cleaned = format!("{} {}", &cleaned[..m.start()], uniq.iter().map(|n| format!("[{}]", n)).collect::<String>());
    }
    cleaned = pyutil::strip(&WS.replace_all(&cleaned, " ")).to_string();
    cleaned = SP_PUNCT.replace_all(&cleaned, "$1").into_owned();
    cleaned = DOTS.replace_all(&cleaned, ".").into_owned();
    cleaned = LEAD_PUNCT.replace(&cleaned, "").into_owned();
    if pyutil::strip_chars(&cleaned, " .[]0123456789").is_empty() {
        body.to_string()
    } else {
        cleaned
    }
}

//------------------------------------------------------------------
// 출력 파싱 — (본문, 인용된 근거 번호)
//=> "답변:" / "인용:" 머리말이 있으면 그 뒤를 본문으로 본다.
//------------------------------------------------------------------
pub fn parse_answer(text: &str, n_evidence: Option<usize>) -> (String, Vec<usize>) {
    parse_answer_opt(text, n_evidence, true)
}

// 출력 파싱 — 되뱉기 거르기를 고를 수 있는 본체. 파이프라인은 설정(generation.echo_filter)을 넘긴다
pub fn parse_answer_opt(text: &str, n_evidence: Option<usize>, echo: bool) -> (String, Vec<usize>) {
    let mut body = pyutil::strip(text).to_string();
    for marker in ["답변:", "인용:"] {
        if let Some(i) = body.find(marker) {
            body = pyutil::strip(&body[i + marker.len()..]).to_string();
            break;
        }
    }
    body = clean_answer(&body, n_evidence, echo);
    let mut cited: Vec<usize> = CITE.captures_iter(&body).filter_map(|c| c[1].parse().ok()).collect();
    cited.sort();
    cited.dedup();
    if let Some(n) = n_evidence.filter(|n| *n > 0) {
        cited.retain(|k| (1..=n).contains(k));
    }
    (body, cited)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn echo() {
        let s = strip_instruction_echo("휴가비는 50만원입니다. [1] 근거 번호 [1] 형식으로 표기합니다.", Some(3));
        assert_eq!(s, "휴가비는 50만원입니다. [1]");
        assert_eq!(drop_invalid_cites("값은 [75]억 [2]", Some(3)), "값은 75억 [2]");
        let (b, c) = parse_answer("**답** 입니다 [2][1]", Some(3));
        assert_eq!(b, "답 입니다 [2][1]");
        assert_eq!(c, vec![1, 2]);
    }
}
