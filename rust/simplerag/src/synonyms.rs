//------------------------------------------------------------------
// 동의어 사전 — 일상어 질문을 규정 용어로 넓혀 키워드 검색(BM25)만 돕는다 (Python retrieve/synonyms.py 와 같다)
//=> 사용자는 "할아버지 돌아가시면 휴가 며칠" 처럼 묻는데 문서에는 "조부모 사망" 으로 적혀 있다.
//   사전에 있는 일상어가 질문에 있으면 문서 용어를 BM25 질의에만 덧붙인다. 의미 검색·리랭커는 원래 질문 그대로.
//   판단(비슷한 말인가)은 사전을 만드는 사람이 한다 — 질문할 때는 글자 그대로 찾기만 한다.
//
//   사전 파일(같은 모양, 뒤가 앞선다):
//    1) <프로그램 폴더>/synonyms.default.yaml — 배포에 동봉하는 기본 사전
//    2) <데이터 폴더>/synonyms.yaml (없으면 <프로그램 폴더>/synonyms.yaml) — 사용자 사전
//   파일이 바뀌면(수정 시각) 다음 질문 때 다시 읽는다.
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::SystemTime;

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;

use crate::config::cfg;
use crate::pyutil;

const DEFAULT_NAME: &str = "synonyms.default.yaml";
const USER_NAME: &str = "synonyms.yaml";
// 일상어 앞자리 — 글 처음이거나 공백·괄호·문장부호 뒤(낱말 중간 "할아버지" 의 "아버지" 는 맞지 않게)
const BOUNDARY: &str = r#"(?:^|(?<=[\s(\[{,.!?·"'“‘/]))"#;
static WS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());

// (일상어 정규식, 일상어, 문서 용어)
type Rule = (FRegex, String, String);
type CacheKey = Vec<(String, Option<SystemTime>)>;
static CACHE: Lazy<Mutex<(Option<CacheKey>, Vec<Rule>)>> = Lazy::new(|| Mutex::new((None, vec![])));

// 사전 파일 경로들 — 기본 사전(프로그램 폴더), 사용자 사전(데이터 폴더 우선)
fn paths() -> Vec<String> {
    let c = cfg();
    let mut out = vec![];
    let d = pyutil::join(&c.app_dir, DEFAULT_NAME);
    if std::path::Path::new(&d).is_file() {
        out.push(d);
    }
    for base in [&c.root, &c.app_dir] {
        let u = pyutil::join(base, USER_NAME);
        if std::path::Path::new(&u).is_file() {
            out.push(u);
            break;
        }
    }
    out
}

// 사전 읽기 — 파일들을 합쳐 규칙 목록을 만든다. 긴 일상어부터("애기 낳" 이 "애기" 보다 먼저), rejected 짝은 뺀다
fn load(paths: &[String]) -> Vec<Rule> {
    let mut table: HashMap<String, String> = HashMap::new();
    let mut rejected: HashSet<(String, String)> = HashSet::new();
    for p in paths {
        let y: serde_yaml::Value = match std::fs::read_to_string(p).ok().and_then(|s| serde_yaml::from_str(&s).ok()) {
            Some(v) => v,
            None => continue, // 읽지 못한 파일은 건너뛴다 — 검색은 사전 없이도 돈다
        };
        if let Some(m) = y.get("synonyms").and_then(|v| v.as_mapping()) {
            for (term, words) in m {
                let term = yaml_str(term);
                for w in words.as_sequence().into_iter().flatten() {
                    if let Some(w) = w.as_str().map(|s| s.trim()).filter(|s| !s.is_empty()) {
                        table.insert(w.to_string(), term.trim().to_string());
                    }
                }
            }
        }
        for pair in y.get("rejected").and_then(|v| v.as_sequence()).into_iter().flatten() {
            if let Some(s) = pair.as_sequence().filter(|s| s.len() == 2) {
                rejected.insert((yaml_str(&s[0]).trim().to_string(), yaml_str(&s[1]).trim().to_string()));
            }
        }
    }
    let mut words: Vec<&String> = table.keys().collect();
    words.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()).then(a.cmp(b)));
    let mut rules = vec![];
    for w in words {
        let t = &table[w];
        if rejected.contains(&(w.clone(), t.clone())) {
            continue;
        }
        let body = w.split_whitespace().map(|p| fancy_regex::escape(p).into_owned()).collect::<Vec<_>>().join(r"\s+");
        if let Ok(re) = FRegex::new(&format!("{}{}", BOUNDARY, body)) {
            rules.push((re, w.clone(), t.clone()));
        }
    }
    rules
}

// YAML 값 → 문자열(숫자 키 등도 글자로)
fn yaml_str(v: &serde_yaml::Value) -> String {
    match v {
        serde_yaml::Value::String(s) => s.clone(),
        other => serde_yaml::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

// 지금 쓸 규칙으로 f 를 부른다 — 파일 경로·수정 시각이 바뀌었으면 다시 읽는다
fn with_rules<T>(f: impl FnOnce(&[Rule]) -> T) -> T {
    let ps = paths();
    let key: CacheKey = ps.iter().map(|p| (p.clone(), std::fs::metadata(p).and_then(|m| m.modified()).ok())).collect();
    let mut g = CACHE.lock().unwrap();
    if g.0.as_ref() != Some(&key) {
        g.1 = load(&ps);
        g.0 = Some(key);
    }
    f(&g.1)
}

//------------------------------------------------------------------
// 질문 넓히기 (Python synonyms.expand 와 같다)
//=> 질문에 있는 일상어마다 문서 용어를 덧붙인 BM25 질의를 만든다.
//    · 문서 용어가 이미 질문에 있으면 덧붙이지 않는다(공백 무시)
//    · 같은 문서 용어는 한 번만 · 긴 일상어가 맞은 자리는 짧은 일상어가 다시 쓰지 않는다
//   돌려줌: (BM25 질의, [(일상어, 문서 용어)]) — 넓힐 것이 없으면 (질문, [])
//------------------------------------------------------------------
pub fn expand(query: &str) -> (String, Vec<(String, String)>) {
    if !cfg().synonyms || query.is_empty() {
        return (query.to_string(), vec![]);
    }
    with_rules(|rules| expand_with(rules, query))
}

// 이 낱말을 사전이 이미 넓히나(후보 제안이 다시 묻지 않게) — 넓히기를 끈 상태면 사전도 없는 것으로 본다
pub fn covers(word: &str) -> bool {
    cfg().synonyms && with_rules(|rules| !expand_with(rules, word).1.is_empty())
}

//------------------------------------------------------------------
// 사용자가 [아님] 한 짝 (Python synonyms.rejected 와 같다)
//=> 후보 제안(synonym_suggest)이 다시 묻지 않게 사전 파일들의 rejected 를 모은다. 읽지 못한 파일은 건너뛴다.
//------------------------------------------------------------------
pub fn rejected() -> HashSet<(String, String)> {
    let mut out = HashSet::new();
    for p in paths() {
        let y: serde_yaml::Value = match std::fs::read_to_string(&p).ok().and_then(|s| serde_yaml::from_str(&s).ok()) {
            Some(v) => v,
            None => continue,
        };
        for pair in y.get("rejected").and_then(|v| v.as_sequence()).into_iter().flatten() {
            if let Some(s) = pair.as_sequence().filter(|s| s.len() == 2) {
                out.insert((yaml_str(&s[0]).trim().to_string(), yaml_str(&s[1]).trim().to_string()));
            }
        }
    }
    out
}

// 주어진 규칙으로 질문 넓히기(expand 의 본체 — 시험에서 사전 파일 없이 쓴다)
fn expand_with(rules: &[Rule], query: &str) -> (String, Vec<(String, String)>) {
    let nq = WS.replace_all(query, "").into_owned();
    {
        let mut used: Vec<(usize, usize)> = vec![];
        let mut added: Vec<String> = vec![];
        let mut pairs = vec![];
        for (re, word, term) in rules {
            let m = match re.find(query) {
                Ok(Some(m)) => m,
                _ => continue,
            };
            if used.iter().any(|&(a, b)| a < m.end() && m.start() < b) {
                continue;
            }
            used.push((m.start(), m.end()));
            if nq.contains(WS.replace_all(term, "").as_ref()) || added.contains(term) {
                continue;
            }
            added.push(term.clone());
            pairs.push((word.clone(), term.clone()));
        }
        if added.is_empty() {
            (query.to_string(), vec![])
        } else {
            (format!("{} {}", query, added.join(" ")), pairs)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Python bench/paraphrase/test_synonyms.py 와 같은 사례 — 두 판이 같은 넓힘을 내는지
    #[test]
    fn expands_like_python() {
        let dir = std::env::temp_dir().join("simplerag_syn_test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.yaml");
        std::fs::write(&p, "synonyms:
  조부모: [할아버지]
  사망: [돌아가시, 별세]
  출산: [애기 낳]
  자녀: [애기]
  배우자: [와이프]
  배우자의 부모: [장인]
  자차: [내 차]
  부모: [아버지]
rejected:
  - [장인, 배우자의 부모]
").unwrap();
        let rules = load(&[p.to_string_lossy().into_owned()]);
        let cases = [
            ("할아버지 돌아가시면 휴가 며칠 줘?", "할아버지 돌아가시면 휴가 며칠 줘? 사망 조부모"),
            ("와이프가 애기 낳으면 휴가 며칠?", "와이프가 애기 낳으면 휴가 며칠? 출산 배우자"),
            ("조부모 사망 시 휴가", "조부모 사망 시 휴가"),
            ("할아버지께서 별세", "할아버지께서 별세 조부모 사망"),
            ("장인어른 돌아가시면", "장인어른 돌아가시면 사망"),
            ("현장 출근을 내  차로", "현장 출근을 내  차로 자차"),
        ];
        for (q, want) in cases {
            assert_eq!(expand_with(&rules, q).0, want, "{}", q);
        }
    }
}
