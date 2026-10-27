//------------------------------------------------------------------
// 동의어 후보 제안 — 놓친 질문을 모아 두었다가 "이 일상어 → 이 문서 용어" 후보를 뽑는다
//   (동의어 설계서 3단계 §7, Python retrieve/synonym_suggest.py 와 같은 규칙)
//=> 사전을 미리 다 알 수 없으니, 실제로 근거를 못 찾은 질문에서 사전에 넣을 짝을 찾는다.
//   판단(같은 뜻인가)은 사람이 승인 화면에서 한다 — 여기서는 고를 후보만 만든다. 질문 속도와 무관한 일괄 작업.
//
//   1) 기록: 답할 때 "관련 근거 없음"이었거나 리랭커 최고 점수가 0 미만인 질문을 synonym_misses.jsonl 에 한 줄씩
//   2) 제안(suggest-synonyms 명령): 질문을 다시 검색한 리랭커 후보 10건에 글자로 없는 질문 낱말을 넓힐 대상으로,
//      10건의 명사꼴 문서 용어를 e5 유사도로 줄 세우고, 상위 10개 중 1.7B 가 고른 하나를 맨 앞에 둔다.
//      1.7B 가 고르지 않은 낱말 · 사전에 이미 있는 일상어 · [아님] 한 짝은 뺀다. 자주 놓친 낱말부터.
//   결과: <데이터 폴더>/synonym_candidates.json (승인 화면이 읽는다 — 4단계)
//------------------------------------------------------------------

use std::collections::HashMap;
use std::io::{BufRead, Write};

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Value};

use crate::config::{self, cfg};
use crate::embed::Embedder;
use crate::generate::prompts::build_prompt;
use crate::pyutil;
use crate::retrieve::{keywords, Retriever, Timing};
use crate::synonyms;

pub const MISSES_NAME: &str = "synonym_misses.jsonl";
pub const CANDIDATES_NAME: &str = "synonym_candidates.json";
// 이 점수 미만이면 놓친 질문으로 본다(리랭커 bge 점수) — 일상어로 놓친 질문 14/18 · 골든 5/100 이 걸린다
const MISS_BELOW: f64 = 0.0;
// 기록 파일이 이보다 커지면 .old 로 넘기고 새로 쓴다
const MISSES_MAX_BYTES: u64 = 1_000_000;
// 최근 질문 수 · 낱말마다 후보 수 · LLM 에게 보일 후보 수 · 파일에 담을 낱말 수(화면은 앞의 몇 개만)
const RECENT: usize = 200;
const SHOW: usize = 3;
const LLM_CHOICES: usize = 10;
const KEEP_WORDS: usize = 100;
// 문서 용어로 치지 않는 끝 글자 — 동사·형용사·관형사꼴을 걸러 명사꼴만 남긴다
const VERB_END: &str = "다고며여어아서면게지한할된될는은을던든록라려니요야냐도의";
// 짧은 동사꼴 질문 말의 끝 글자 — 세 글자 이하로 이 글자로 끝나면 넓힐 대상이 아니다
const SHORT_VERB_END: &str = "고로야서지게요줘래니면데까";
// 일상 질문에 흔한 말 — 문서에 없어도 넓힐 대상이 아니다
const QUESTION_WORDS: &[&str] = &[
    "며칠", "몇일", "얼마", "얼마야", "얼마나", "얼마까지", "나와", "나오", "나오는", "나오나", "하면", "하는", "해야", "해도",
    "되나", "될까", "되는", "되면", "있어", "있나", "있는", "없어", "없나", "안에", "달에", "언제", "어디", "누구", "누구한테",
    "뭐야", "무슨", "받아", "받을", "받나", "받으면", "받는", "줘야", "주나", "주는", "주면", "그럼", "그냥", "근데", "이거", "그거",
    "내가", "제가", "우리", "회사", "회사에서", "어떻게", "어떡해", "알려줘", "궁금해", "쳐야", "치나", "쓰면", "쓰나", "가면",
    "갈때", "할때", "몇개", "몇번", "몇명", "최대", "아니면", "같은", "다른", "중간에", "끼면", "얼만데", "보통", "따로", "총",
];
// 문서 용어로 치지 않는 문법 말
const GRAMMAR_TERMS: &[&str] = &["따른", "따라", "으로", "위와", "같이", "이에", "이내", "내에", "통해", "의해", "경우", "경우에", "해당",
    "다음", "이후", "이전", "또는", "및", "중에", "있음", "없음", "이상", "이하", "미만", "초과", "각호", "제외"];
// 구어 조사 — keywords 가 떼지 않는 것("공무원한테" → "공무원")
const SPOKEN_JOSA: &[&str] = &["한테서", "한테", "께서", "에서는", "이랑", "랑", "하고"];

const LLM_SYSTEM: &str = "당신은 한국어 낱말 뜻을 판정하는 도우미입니다.";

static HANGUL_TERM: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[가-힣]{2,8}$").unwrap());
static HANGUL_WORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[가-힣]{2,}$").unwrap());
static WS: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());
static NUM: Lazy<Regex> = Lazy::new(|| Regex::new(r"\d+").unwrap());

// 지금 시각(초까지, Python isoformat 과 같은 꼴)
fn now_iso() -> String {
    chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}

// 공백 지운 글
fn flat(s: &str) -> String {
    WS.replace_all(s, "").into_owned()
}

// 마지막 글자가 set 에 있나
fn ends_in(w: &str, set: &str) -> bool {
    w.chars().last().map(|c| set.contains(c)).unwrap_or(false)
}

// 구어 조사 떼기 — 떼고도 두 글자 이상 남을 때만
fn strip_spoken(w: &str) -> String {
    for j in SPOKEN_JOSA {
        if w.ends_with(j) && pyutil::clen(w) >= pyutil::clen(j) + 2 {
            return w[..w.len() - j.len()].to_string();
        }
    }
    w.to_string()
}

//------------------------------------------------------------------
// 놓친 질문 기록 (Python log_miss 와 같다)
//=> 근거를 못 찾았거나 리랭커가 확신하지 못한 질문만 데이터 폴더의 synonym_misses.jsonl 에 한 줄로 남긴다.
//   설정(retrieval.synonym_log)이 꺼졌으면 하지 않는다. 쓰기 실패는 삼킨다 — 기록 때문에 답이 죽으면 안 된다.
//   돌려줌: true = 기록함
//------------------------------------------------------------------
pub fn log_miss(query: &str, timing: &Timing) -> bool {
    if !cfg().synonym_log || query.is_empty() {
        return false;
    }
    let top = timing.get("rerank_top").and_then(|v| v.as_f64());
    let no_rel = timing.get("no_relevant").and_then(|v| v.as_bool()).unwrap_or(false);
    if !no_rel && top.map(|t| t >= MISS_BELOW).unwrap_or(true) {
        return false;
    }
    let rec = json!({"t": now_iso(), "q": query, "top": top, "no_relevant": no_rel,
                     "expanded": timing.get("synonyms").cloned().unwrap_or(json!([]))});
    let path = pyutil::join(&cfg().root, MISSES_NAME);
    if std::fs::metadata(&path).map(|m| m.len() > MISSES_MAX_BYTES).unwrap_or(false) {
        let _ = std::fs::rename(&path, format!("{}.old", path));
    }
    let r = std::fs::OpenOptions::new().create(true).append(true).open(&path)
        .and_then(|mut f| writeln!(f, "{}", rec));
    r.is_ok()
}

//------------------------------------------------------------------
// 기록된 질문 읽기 — 줄마다 "q"(없으면 "user_input"), 같은 질문은 한 번만 최근 순서로
//   돌려줌: [(질문, 횟수)] 최근 RECENT 개 (파일이 없으면 빈 목록)
//------------------------------------------------------------------
pub fn read_misses(path: &str) -> Vec<(String, usize)> {
    let f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return vec![],
    };
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut order: Vec<String> = vec![];
    for line in std::io::BufReader::new(f).lines().map_while(Result::ok) {
        let r: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let q = r.get("q").or_else(|| r.get("user_input")).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
        if q.is_empty() {
            continue;
        }
        if !counts.contains_key(&q) {
            order.push(q.clone());
        }
        *counts.entry(q).or_insert(0) += 1;
    }
    let skip = order.len().saturating_sub(RECENT);
    order.into_iter().skip(skip).map(|q| { let n = counts[&q]; (q, n) }).collect()
}

//------------------------------------------------------------------
// 질문 하나에서 넓힐 낱말과 문서 용어 후보 뽑기 (Python words_and_terms 와 같다)
//   넓힐 낱말 = 질문의 한글 핵심 낱말 중 질문 말이 아니고, 검색 후보 글에 글자로 없고, 사전이 아직 넓히지 않는 것
//   문서 용어 = 검색 후보 글의 한글 핵심 낱말(2~8자, 명사꼴, 문법 말 아님) 중 넓힐 낱말이 아닌 것
//------------------------------------------------------------------
pub fn words_and_terms(query: &str, texts: &[String]) -> (Vec<String>, Vec<String>) {
    let body = texts.join(" ");
    let fl = flat(&body);
    let mut words: Vec<String> = vec![];
    for w in keywords(query).iter().map(|w| strip_spoken(w)) {
        if words.contains(&w) {
            continue;
        }
        if QUESTION_WORDS.contains(&w.as_str()) || fl.contains(&w) || !HANGUL_WORD.is_match(&w)
            || (pyutil::clen(&w) <= 3 && ends_in(&w, SHORT_VERB_END)) || synonyms::covers(&w) {
            continue;
        }
        words.push(w);
    }
    let mut terms: Vec<String> = vec![];
    for t in keywords(&body) {
        if terms.contains(&t) || !HANGUL_TERM.is_match(&t) || ends_in(&t, VERB_END)
            || GRAMMAR_TERMS.contains(&t.as_str()) || words.contains(&t) {
            continue;
        }
        terms.push(t);
    }
    (words, terms)
}

//------------------------------------------------------------------
// 1.7B 에게 같은 뜻인 문서 용어 하나 고르게 하기 (Python llm_pick 과 같은 프롬프트)
//   돌려줌: 고른 용어, 고르지 못했거나(0·엉뚱한 답) 생성 실패면 None
//------------------------------------------------------------------
pub fn llm_pick(complete: &dyn Fn(&str, usize) -> Result<String, String>, query: &str, word: &str, cands: &[String]) -> Option<String> {
    let list = cands.iter().enumerate().map(|(i, c)| format!("{}. {}", i + 1, c)).collect::<Vec<_>>().join("\n");
    let user = format!("질문: {}\n\n질문 속 낱말 \"{}\" 를 규정 문서에서는 어떤 용어로 쓸까요? 같은 뜻인 용어를 아래 후보에서 하나만 고르세요. \
                        같은 뜻인 것이 없으면 0.\n\n후보:\n{}\n\n번호만 답하세요.", query, word, list);
    let out = complete(&build_prompt(LLM_SYSTEM, &user), 6).ok()?;
    let k: usize = NUM.find(&out).and_then(|m| m.as_str().parse().ok()).unwrap_or(0);
    if k >= 1 && k <= cands.len() { Some(cands[k - 1].clone()) } else { None }
}

// 낱말별로 모으는 중간 값
struct TermAcc {
    term: String,
    score: f64,
    llm: bool,
    votes: usize,
    best_rank: usize,
    docs: Vec<String>,
}
struct WordAcc {
    word: String,
    count: usize,
    examples: Vec<String>,
    terms: Vec<TermAcc>,
}

//------------------------------------------------------------------
// 후보 제안 (본체, Python suggest 와 같다)
//=> 놓친 질문마다 검색 후보 10건 → 넓힐 낱말·문서 용어 → e5 유사도 → (있으면) 1.7B 고르기 → 낱말별로 모은다.
//
// -in: retriever = 검색기 / embedder = e5 / questions = [(질문, 횟수)]
// -in: complete  = (프롬프트, 최대 토큰) → 글. None 이면 e5 순서만
// -in: log       = 진행 출력
//
// -out: Ok(json {updated, n_questions, llm, candidates:[{word, count, examples, terms:[{term, score, llm, docs}]}]})
// -out: error = 검색·임베딩 실패면 Err
//------------------------------------------------------------------
pub fn suggest(retriever: &Retriever, embedder: &Embedder, questions: &[(String, usize)],
               complete: Option<&dyn Fn(&str, usize) -> Result<String, String>>, log: &mut dyn FnMut(&str)) -> Result<Value, String> {
    let rejected = synonyms::rejected();
    let mut cache: HashMap<String, Vec<f32>> = HashMap::new();
    let mut by_word: Vec<WordAcc> = vec![];
    let mut index: HashMap<String, usize> = HashMap::new();
    let pool_n = cfg().rerank_pool as usize;
    for (i, (q, n)) in questions.iter().enumerate() {
        let (pool, _) = retriever.search_rrf(q, pool_n, None)?;
        let texts: Vec<String> = pool.iter().map(|c| c.text.clone()).collect();
        let (words, terms) = words_and_terms(q, &texts);
        log(&format!("[{}/{}] {} — 넓힐 낱말 {}", i + 1, questions.len(), pyutil::head(q, 40),
                     if words.is_empty() { "없음".to_string() } else { words.join(", ") }));
        if words.is_empty() || terms.is_empty() {
            continue;
        }
        // 같은 낱말은 한 번만 임베딩한다(질문 접두). 묶음 구성이 다르면 채움 길이가 달라 값이 조금 흔들리므로
        //   Python 과 같게 부른다 — 질문마다 새 문서 용어를 순서대로 한 묶음, 넓힐 낱말은 하나씩
        let embed_new = |ws: &[String], cache: &mut HashMap<String, Vec<f32>>| -> Result<(), String> {
            let need: Vec<String> = ws.iter().filter(|w| !cache.contains_key(*w)).cloned().collect();
            if !need.is_empty() {
                for (w, v) in need.iter().zip(embedder.embed(&need, config::QUERY_PREFIX)?) {
                    cache.insert(w.clone(), v);
                }
            }
            Ok(())
        };
        embed_new(&terms, &mut cache)?;
        for w in &words {
            embed_new(std::slice::from_ref(w), &mut cache)?;
            let wv = &cache[w];
            let mut scored: Vec<(String, f64)> = terms.iter()
                .map(|t| (t.clone(), cache[t].iter().zip(wv).map(|(a, b)| a * b).sum::<f32>() as f64)).collect();
            scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            let mut ranked: Vec<(String, f64)> = scored.into_iter().filter(|(t, _)| !rejected.contains(&(w.clone(), t.clone()))).collect();
            let pick = match complete {
                Some(f) => {
                    let cands: Vec<String> = ranked.iter().take(LLM_CHOICES).map(|(t, _)| t.clone()).collect();
                    match llm_pick(f, q, w, &cands) {
                        Some(p) => Some(p),
                        // 1.7B 가 "같은 뜻 없음"이라 한 낱말은 뺀다
                        None => continue,
                    }
                }
                None => None,
            };
            if let Some(p) = &pick {
                // LLM 이 고른 것을 맨 앞으로 — 나머지는 e5 순서
                let pos = ranked.iter().position(|(t, _)| t == p).unwrap();
                let x = ranked.remove(pos);
                ranked.insert(0, x);
            }
            let k = *index.entry(w.clone()).or_insert_with(|| {
                by_word.push(WordAcc { word: w.clone(), count: 0, examples: vec![], terms: vec![] });
                by_word.len() - 1
            });
            let ent = &mut by_word[k];
            ent.count += n;
            if !ent.examples.contains(q) && ent.examples.len() < 3 {
                ent.examples.push(q.clone());
            }
            for (rank, (t, sc)) in ranked.iter().take(SHOW).enumerate() {
                let is_pick = pick.as_deref() == Some(t.as_str());
                let mut docs: Vec<String> = vec![];
                for c in &pool {
                    let d = pyutil::basename(&c.doc_name).to_string();
                    if flat(&c.text).contains(t.as_str()) && !docs.contains(&d) {
                        docs.push(d);
                    }
                }
                docs.truncate(2);
                let sc = pyutil::round_to(*sc, 3);
                match ent.terms.iter_mut().find(|x| &x.term == t) {
                    None => ent.terms.push(TermAcc { term: t.clone(), score: sc, llm: is_pick, votes: 1, best_rank: rank, docs }),
                    // 여러 질문에서 나온 같은 짝은 더 좋은 순위·점수를 남기고 표를 더한다
                    Some(cur) => {
                        cur.votes += 1;
                        cur.llm = cur.llm || is_pick;
                        cur.best_rank = cur.best_rank.min(rank);
                        cur.score = cur.score.max(sc);
                    }
                }
            }
        }
    }
    let mut out: Vec<(usize, f64, Value)> = vec![];
    for mut ent in by_word {
        ent.terms.sort_by(|a, b| (!a.llm, a.best_rank).cmp(&(!b.llm, b.best_rank))
            .then(b.votes.cmp(&a.votes))
            .then(b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal)));
        let top = ent.terms.first().map(|t| t.score).unwrap_or(0.0);
        let terms: Vec<Value> = ent.terms.iter().take(SHOW)
            .map(|t| json!({"term": t.term, "score": t.score, "llm": t.llm, "docs": t.docs})).collect();
        out.push((ent.count, top, json!({"word": ent.word, "count": ent.count, "examples": ent.examples, "terms": terms})));
    }
    // 자주 놓친 낱말부터, 같으면 1위 후보 점수가 높은 것부터(안정 정렬)
    out.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal)));
    let cands: Vec<Value> = out.into_iter().take(KEEP_WORDS).map(|x| x.2).collect();
    Ok(json!({"updated": now_iso(), "n_questions": questions.len(), "llm": complete.is_some(), "candidates": cands}))
}

//------------------------------------------------------------------
// 결과 파일로 쓰기 — 다 쓴 뒤 바꿔 넣는다(승인 화면이 쓰는 도중의 파일을 읽지 않게)
//   돌려줌: 쓴 경로 / 쓰기 실패면 Err
//------------------------------------------------------------------
pub fn save(result: &Value, path: Option<&str>) -> Result<String, String> {
    let path = path.map(|p| p.to_string()).unwrap_or_else(|| pyutil::join(&cfg().root, CANDIDATES_NAME));
    let tmp = format!("{}.tmp", path);
    std::fs::write(&tmp, serde_json::to_string_pretty(result).map_err(|e| e.to_string())?).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, &path).map_err(|e| e.to_string())?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Python 과 같은 낱말 거르기 — 질문 말·구어 조사·짧은 동사꼴·문서에 있는 말
    #[test]
    fn words_like_python() {
        let texts = vec!["경조사 휴가는 조부모 사망 시 3일을 부여한다. 공무원 등에 따른 규정".to_string()];
        let (w, t) = words_and_terms("할아버지 돌아가시면 휴가 며칠 줘? 공무원한테 되고", &texts);
        assert_eq!(w, vec!["할아버지", "돌아가시면"], "{:?}", w);
        assert!(t.contains(&"조부모".to_string()) && t.contains(&"사망".to_string()) && !t.contains(&"따른".to_string()), "{:?}", t);
    }
}
