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
// 되뱉기·중복 인용 정리
//=> 지시문을 옮겨 적은 부분을 지우고, 끝에 반복된 인용을 하나로 접고, 공백을 정리한다.
//   정리 뒤 본문이 사라지면(숫자·괄호·마침표만 남으면) 원문을 돌려준다.
//------------------------------------------------------------------
pub fn strip_instruction_echo(body: &str, n_evidence: Option<usize>) -> String {
    let mut cleaned = LEADING_MARKER.replace(body, "").into_owned();
    for pat in ECHO_PATTERNS.iter() {
        cleaned = fsub(pat, &cleaned, " ");
    }
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
    let mut body = pyutil::strip(text).to_string();
    for marker in ["답변:", "인용:"] {
        if let Some(i) = body.find(marker) {
            body = pyutil::strip(&body[i + marker.len()..]).to_string();
            break;
        }
    }
    body = strip_instruction_echo(&body, n_evidence);
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
