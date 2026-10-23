//------------------------------------------------------------------
// 문서 구조 추측 (Python structure.py)
//=> 평문에서 절 제목 줄을 정규식으로 찾는다. 문서마다 문법 하나(조문 → 마크다운 → 번호절 순)를
//   고르고, 부속서 표지(별표·붙임·부칙 …)는 따로 경계로 더한다.
//------------------------------------------------------------------

use once_cell::sync::Lazy;
use regex::Regex;

use crate::pyutil;

// 문법 이름과 정규식 — 순서대로 시도한다
pub static GRAMMARS: Lazy<Vec<(&'static str, Regex)>> = Lazy::new(|| {
    vec![
        ("조문", Regex::new(r"(?m)^[ \t]*제\s*\d+\s*조\s*[(【][^\n]{0,60}?[)】][ \t]*$|^[ \t]*제\s*\d+\s*조[ \t][^\n]{0,60}$").unwrap()),
        ("마크다운", Regex::new(r"(?m)^[ \t]*#{1,6}[ \t]+\S[^\n]*$").unwrap()),
        ("번호절", Regex::new(r"(?m)^[ \t]*\d+(?:\.\d+)*\.?[ \t]+\S[^\n]{0,80}$").unwrap()),
    ]
});
pub const MIN_HEADINGS: usize = 3;
static TABLE_LINE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[ \t]*\|.*\|[ \t]*$").unwrap());
// 부속서 표지(별표·붙임·부칙 …). Python 원식의 `[\dA-Z]+(?![가-힣])` 는 역추적으로
//   "첫 영숫자 바로 뒤가 한글이 아니면(또는 줄 끝이면) 통과"와 같은 뜻이다 — 전방탐색 없이 그 뜻대로 쓴다
//   (fancy-regex 역추적은 긴 코드 파일에서 멈추듯 느려졌다).
pub static APPENDIX: Lazy<Regex> = Lazy::new(|| {
    Regex::new(concat!(
        r"(?m)^[ \t]*[\[(]?\s*",
        r"(?:별\s*표|별\s*첨|붙\s*임|참\s*고|서\s*식|부\s*록|첨\s*부)",
        r"\s*#?\s*[\dA-Z](?:[^가-힣\n][^\n]*)?$",
        r"|^[ \t]*부\s*칙(?:[^가-힣\n][^\n]*)?$"
    )).unwrap()
});
pub const MIN_APPENDIX: usize = 2;
pub const TITLE_MAX: usize = 40;
pub static NUMBERING: Lazy<Regex> = Lazy::new(|| Regex::new(r"^(제\s*\d+\s*조|#{1,6}|[\dA-Z]+(?:\.\d+)*\.?)").unwrap());

//------------------------------------------------------------------
// 부속서 표지 줄의 시작 위치들(바이트)
//------------------------------------------------------------------
pub fn appendix_starts(text: &str) -> Vec<usize> {
    APPENDIX.find_iter(text).map(|m| m.start()).collect()
}

//------------------------------------------------------------------
// 부속서 표지 줄 개수 (findall 길이)
//------------------------------------------------------------------
pub fn appendix_count(text: &str) -> usize {
    APPENDIX.find_iter(text).count()
}

//------------------------------------------------------------------
// 한 줄 전체가 부속서 표지인가 (_APPENDIX.fullmatch)
//------------------------------------------------------------------
pub fn appendix_fullmatch(line: &str) -> bool {
    APPENDIX.find_iter(line).any(|m| m.start() == 0 && m.end() == line.len())
}

//------------------------------------------------------------------
// 한 줄 전체가 문법 제목인가 (pat.fullmatch)
//------------------------------------------------------------------
pub fn grammar_fullmatch(pat: &Regex, line: &str) -> bool {
    pat.find_iter(line).any(|m| m.start() == 0 && m.end() == line.len())
}

//------------------------------------------------------------------
// 문서 문법 고르기 — 제목 줄이 3개 이상 나오는 첫 문법
//
// -out: Some((이름, 정규식)) / None
//------------------------------------------------------------------
pub fn detect_grammar(text: &str) -> Option<(&'static str, &'static Regex)> {
    for (name, pat) in GRAMMARS.iter() {
        if pat.find_iter(text).count() >= MIN_HEADINGS {
            return Some((name, pat));
        }
    }
    None
}

//------------------------------------------------------------------
// 평문을 (절 이름표, 본문) 목록으로 나누기
//
// -in: text = 문서 평문
//
// -out: Vec<(Option<이름표>, 본문)> — 구조가 없으면 [(None, 전체)]
// -out: error = 없음
//------------------------------------------------------------------
pub fn split_sections(text: &str) -> Vec<(Option<String>, String)> {
    let g = detect_grammar(text);
    let app = appendix_starts(text);
    if g.is_none() && app.len() < MIN_APPENDIX {
        return vec![(None, text.to_string())];
    }
    let mut starts: Vec<usize> = match g {
        Some((_, pat)) => pat.find_iter(text).map(|m| m.start()).collect(),
        None => vec![],
    };
    starts.extend(app);
    starts.sort();
    starts.dedup();
    if starts.is_empty() {
        return vec![(None, text.to_string())];
    }
    let mut out = vec![];
    let head = pyutil::strip(&text[..starts[0]]);
    if !head.is_empty() {
        out.push((None, head.to_string()));
    }
    let mut bounds = starts.clone();
    bounds.push(text.len());
    for i in 0..bounds.len() - 1 {
        let seg = &text[bounds[i]..bounds[i + 1]];
        if pyutil::strip(seg).is_empty() {
            continue;
        }
        let (h, _, rest) = pyutil::partition(seg, "\n");
        out.push(label(h, rest));
    }
    out
}

// 제목 줄 → (이름표, 본문). 긴 줄은 번호만 이름표로 쓰고 줄은 본문에 남긴다
fn label(head: &str, rest: &str) -> (Option<String>, String) {
    let flat = pyutil::collapse_ws(head);
    if flat.is_empty() {
        return (None, rest.to_string());
    }
    if pyutil::clen(&flat) <= TITLE_MAX {
        let l = pyutil::strip(pyutil::lstrip_chars(&flat, "#")).to_string();
        return (if l.is_empty() { None } else { Some(l) }, rest.to_string());
    }
    let lab = NUMBERING.captures(&flat).map(|c| pyutil::strip(c.get(1).unwrap().as_str()).to_string());
    (lab, format!("{}\n{}", head, rest))
}

//------------------------------------------------------------------
// 표 머리글 줄 찾기 — {줄 번호: 그 줄이 속한 표의 머리글 줄}
//------------------------------------------------------------------
pub fn table_headers(lines: &[&str]) -> std::collections::BTreeMap<usize, String> {
    let mut out = std::collections::BTreeMap::new();
    let mut header: Option<String> = None;
    let mut run = false;
    for (i, ln) in lines.iter().enumerate() {
        if TABLE_LINE.is_match(ln) {
            if !run {
                run = true;
                if looks_like_header(ln) {
                    header = Some(pyutil::strip(ln).to_string());
                    continue;
                }
            }
            if let Some(h) = &header {
                out.insert(i, h.clone());
            }
        } else {
            run = false;
        }
    }
    out
}

// 이름표 칸이 2개 이상이고 숫자 칸이 없으면 머리글
fn looks_like_header(line: &str) -> bool {
    static NUM: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[-\d,.\s]+$").unwrap());
    let inner = pyutil::strip_chars(pyutil::strip(line), "|");
    let cells: Vec<&str> = inner.split('|').map(pyutil::strip).filter(|c| !c.is_empty()).collect();
    if cells.len() < 2 {
        return false;
    }
    !cells.iter().any(|c| NUM.is_match(c))
}

//------------------------------------------------------------------
// 청크 접두 "[문서 > 절]\n"
//------------------------------------------------------------------
pub fn make_prefix(title: Option<&str>, section: Option<&str>) -> String {
    let parts: Vec<&str> = [title, section].into_iter().flatten().filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return String::new();
    }
    format!("[{}]\n", parts.join(" > "))
}
