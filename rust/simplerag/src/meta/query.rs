//------------------------------------------------------------------
// 메타데이터 질의 해석 · 필터 · 가산 · 완화 (메타데이터 검색 설계서 §12 · §13 · 결정 D3 · D4)
//=> Python meta/query.py 와 같은 규칙. "작년에 고봉수님이 만든 절차서 양식을 찾아줘" 를
//     조건(사람 · 기간 · 형식 · 출처 · 스캔 · 유형 · 양식) + 본문 검색어("절차서 양식") 로 나누고,
//   meta.sqlite 로 문서 범위(필터)와 점수 보정(가산)을 만든다. 규칙만 쓴다(LLM 없음).
//    · 사람은 색인(doc_people) · 별칭 사전에 있는 이름만 사람으로 본다
//    · 유형어 · 양식어만 있는 질의는 조건으로 보지 않는다(본문 질의 하락 없음)
//    · 내용을 묻는 질문 꼴이면 "만든 · 받은 … + 문서 낱말" 이 붙어 있을 때만 조건으로 쓴다
//      (골든 본문 질문 362개 오탐 21 → 0)
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::time::Instant;

use chrono::{Datelike, Duration, NaiveDate};
use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Value};

use crate::pyutil;

// 형식 낱말 → 형식 (§12-2)
const FORMAT_WORDS: &[(&str, &str)] = &[
    (r"엑셀\s*파일|엑셀|xlsx|xls", "excel"),
    (r"피피티|파워\s*포인트|ppt|pptx|발표\s*자료", "ppt"),
    (r"한글\s*파일|한글\s*문서|hwpx|hwp", "hwp"),
    (r"pdf|피디에프", "pdf"),
    (r"워드\s*파일|워드\s*문서|워드|docx", "word"),
];
// 받은 경로
const RECV_WORDS: &[(&str, &str)] = &[
    (r"(?:이?메일|첨부)(?:로|에서|으로)?\s*(?:받은|온|왔던|받았던)", "mail"),
    (r"(?:구글\s*)?드라이브(?:에서|로)?\s*(?:받은|내려받은)", "drive"),
    (r"(?:클로드|챗\s*gpt|chatgpt|claude)(?:에서|로)?\s*(?:받은|만든)", "ai"),
];
const SCAN_WORDS: &str = r"스캔한|스캔된|스캔본|스캔";
const VERB_AUTHOR: &str = r"만든|만드신|작성한|작성하신|작성된|써\s*둔|쓴";
const VERB_EDITOR: &str = r"고친|수정한|수정하신|수정된|업데이트한|최근에\s*본|손본";
const VERB_RECV: &str = r"받은|받았던|내려\s*받은|다운\s*받은|다운로드한|다운로드\s*받은|들어온";
const NAME_TAIL: &str = r"(?:님|씨|이가|께서|이|가|의|과|와|랑|이랑)?";
const FILLER: &str = r"(?:을|를)?\s*(?:찾아\s*줘|찾아\s*주세요|찾아|보여\s*줘|알려\s*줘|있어\??|있나요\??|어디\s*있어\??|뭐야\??|주세요|줘)";
const GENERIC: &[&str] = &["문서", "파일", "자료", "것", "거", "중", "중에", "중에서", "그중에", "관련", "관련된"];
const QUESTION_END: &str = r"[?？]\s*$|(?:는가|은가|인가|나요|니까|까요|일까|을까|ㄹ까|습니까|입니까|었나|했나|되나|하나|있나|없나|어때|뭐야|뭔가|되지|하지|돼|줘|해|야)\s*[.?？]*\s*$";
const QUESTION_WORD: &str = r"얼마|몇\s*(?:개|명|일|도|배|번|년|월|시간|퍼센트|%)?|어떻게|어떤|무엇|무슨|뭐|언제|누가|누구|왜|어디|어느";

// 가산 크기 — 리랭커 점수 척도 기준(Python 과 같다)
const BOOST_DATE: f64 = 1.0;
const BOOST_TYPE: f64 = 1.0;
const BOOST_FORM: f64 = 1.0;
const DATE_CONF_MIN: f64 = 0.7;
const TYPE_FILTER_MIN: f64 = 5.0;
fn boost_role(role: &str) -> f64 {
    match role {
        "author" => 1.5,
        "editor" => 0.8,
        "template" => 0.3,
        _ => 0.3,
    }
}

static RE_HANGUL: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[가-힣]{2,4}$").unwrap());
static RE_YEAR: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"재작년|작년|지난\s*해|전년|올해|금년|((?:19|20)\d{2})\s*년(?:도)?|(?<![\d.])(\d{2})\s*년(?:도)?(?!\s*(?:전|간|차))").unwrap());
static RE_MONTH: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(?<!\d)(1[0-2]|[1-9])\s*월(\s*쯤|\s*경)?").unwrap());
static RE_HALF: Lazy<Regex> = Lazy::new(|| Regex::new(r"상반기|하반기").unwrap());
static RE_REL: Lazy<Regex> = Lazy::new(|| Regex::new(r"이번\s*달|지난\s*달|저번\s*달|지난\s*주|저번\s*주|이번\s*주|최근|요즘|(\d+)\s*년\s*전|(\d+)\s*개월\s*전").unwrap());
static RE_ME: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(?<![가-힣])(내가|제가|나의|저의|내|제)(?=\s|$|[가-힣])").unwrap());
static RE_ME_VERB: Lazy<Regex> = Lazy::new(|| Regex::new(&format!(r"^\s*(?:{}|{})", VERB_AUTHOR, VERB_EDITOR)).unwrap());
static RE_FILL_END: Lazy<Regex> = Lazy::new(|| Regex::new(&format!(r"{}\s*[.?!]*\s*$", FILLER)).unwrap());
static RE_PARTICLE: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(?<![가-힣])(에|에서|중|중에|의|을|를|은|는|이|가|로|으로)(?=\s|$)").unwrap());
static RE_NONWORD: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[\W_]+$").unwrap());
static RE_FIND_END: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:찾아|보여|알려)\s*(?:줘|주세요|줄래|봐)\s*[.?？!]*\s*$").unwrap());
static RE_Q_END: Lazy<Regex> = Lazy::new(|| Regex::new(QUESTION_END).unwrap());
static RE_Q_WORD: Lazy<Regex> = Lazy::new(|| Regex::new(QUESTION_WORD).unwrap());
// 질의마다 만들면 수십 ms — 한 번만 만든다
static RE_NAME_TAIL: Lazy<Regex> = Lazy::new(|| Regex::new(&format!("^{}", NAME_TAIL)).unwrap());
static RE_ALIAS_KEY: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[a-z0-9._\-]{3,}$").unwrap());
static RE_VERBS: Lazy<Vec<(Regex, Option<&'static str>, &'static str)>> = Lazy::new(|| vec![
    (Regex::new(VERB_AUTHOR).unwrap(), Some("author"), "created"),
    (Regex::new(VERB_EDITOR).unwrap(), Some("editor"), "modified"),
    (Regex::new(VERB_RECV).unwrap(), None, "added"),
]);
static RE_RECV: Lazy<Vec<(Regex, &'static str)>> = Lazy::new(|| RECV_WORDS.iter().map(|(p, k)| (Regex::new(p).unwrap(), *k)).collect());
static RE_FORMAT: Lazy<Vec<(FRegex, &'static str)>> = Lazy::new(|| FORMAT_WORDS.iter()
    .map(|(p, f)| (FRegex::new(&format!(r"(?<![a-z가-힣])(?:{})(?![a-z])", p)).unwrap(), *f)).collect());
static RE_SCAN: Lazy<Regex> = Lazy::new(|| Regex::new(SCAN_WORDS).unwrap());
static RE_LAST_YEAR: Lazy<Regex> = Lazy::new(|| Regex::new(r"지난\s*해|전년").unwrap());
static RE_SPACES: Lazy<Regex> = Lazy::new(|| Regex::new(r"\s+").unwrap());
// 문서 낱말 검사식 — 유형어 목록이 같으면 다시 쓰고, 별칭 검사식은 별칭마다 한 번
static RE_DOC_CTX: Lazy<Mutex<Option<(Vec<String>, Regex)>>> = Lazy::new(|| Mutex::new(None));
static RE_ALIASES: Lazy<Mutex<HashMap<String, Option<FRegex>>>> = Lazy::new(|| Mutex::new(HashMap::new()));

//------------------------------------------------------------------
// 해석 결과
// -필드: people = [(이름, 역할, 나인가)] / date = 기간 / date_field = created|modified|added
//        format · recv · scanned · doc_type · is_form = 조건 / query = 본문 검색어
//        conditions = 조건으로 쓸 질의인가 / skipped = 조건을 버린 까닭(내용 질문)
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct Parsed {
    pub people: Vec<(String, Option<String>, bool)>,
    pub date: Option<DateRange>,
    pub date_field: String,
    pub format: Option<String>,
    pub recv: Option<String>,
    pub scanned: bool,
    pub doc_type: Option<String>,
    pub is_form: bool,
    pub query: String,
    pub conditions: bool,
    pub skipped: Option<String>,
}

// 기간 [from, to) (KST 자정 기준 epoch 초)
#[derive(Debug, Clone)]
pub struct DateRange {
    pub from: i64,
    pub to: i64,
    pub label: String,
}

// 화면 칩 하나
#[derive(Debug, Clone)]
pub struct Chip {
    pub kind: String,
    pub label: String,
    pub mode: String,
}

//------------------------------------------------------------------
// 필터 · 가산 계획
// -필드: query = 본문 검색어 / docs = 필터 문서 집합(None = 전체) / boosts = {경로: 가산}
//        chips · relaxed = 화면에 보일 조건 · 완화 내역 / meta_only = 본문 검색어가 남지 않음
//        order = (메타만 질의일 때) 가산 · 수정일 순 문서 / ms = 계획에 든 시간
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Plan {
    pub query: String,
    pub docs: Option<HashSet<String>>,
    pub boosts: HashMap<String, f64>,
    pub chips: Vec<Chip>,
    pub relaxed: Vec<String>,
    pub meta_only: bool,
    pub order: Vec<String>,
    pub ms: f64,
    pub off: Vec<String>,
    pub widen: i64,
}

impl Plan {
    // 워커 · 화면에 넘길 JSON — {chips, relaxed, query, docs, ms, meta_only}
    pub fn to_json(&self) -> Value {
        json!({
            "chips": self.chips.iter().map(|c| json!({"kind": c.kind, "label": c.label, "mode": c.mode})).collect::<Vec<_>>(),
            "relaxed": self.relaxed, "query": self.query,
            "docs": self.docs.as_ref().map(|d| json!(d.len())).unwrap_or(Value::Null),
            "ms": (self.ms * 10.0).round() / 10.0, "meta_only": self.meta_only,
            "off": self.off, "widen": self.widen,
        })
    }
}

// meta.sqlite 의 문서 한 줄(필터 · 표시용 칸)
#[derive(Debug, Clone, Default)]
pub struct Row {
    pub format: Option<String>,
    pub scanned: Option<i64>,
    pub recv_kind: Option<String>,
    pub modified: Option<i64>,
    pub created: Option<i64>,
    pub created_conf: Option<f64>,
    pub added: Option<i64>,
    pub doc_type: Option<String>,
    pub doc_type_score: Option<f64>,
    pub is_form: Option<i64>,
    pub form_score: Option<f64>,
    pub title: Option<String>,
}

// 읽어 둔 meta.sqlite — 문서 줄 · 이름별 (경로, 역할) · 경로별 [(이름, 역할)]
pub struct Cached {
    pub rows: HashMap<String, Row>,
    pub by_name: HashMap<String, Vec<(String, String)>>,
    pub by_path: HashMap<String, Vec<(String, String)>>,
}

static CACHE: Lazy<Mutex<HashMap<String, (i64, std::sync::Arc<Cached>)>>> = Lazy::new(|| Mutex::new(HashMap::new()));
static NAMES: Lazy<Mutex<HashMap<String, ((i64, i64), std::sync::Arc<(HashSet<String>, HashMap<String, String>, Option<String>)>)>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

// 파일 수정 시각(ms) — 없으면 0
fn mtime_ms(p: &str) -> i64 {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_millis() as i64).unwrap_or(0)
}

//------------------------------------------------------------------
// meta.sqlite 를 메모리에 (파일 · -wal 이 바뀔 때만 다시 읽는다)
//=> 질의마다 수천 줄을 다시 읽으면 20ms 남짓 — 한 번 읽어 두면 계획이 수 ms.
//
// -in: db = meta.sqlite 경로
// -out: 읽어 둔 내용 / 파일이 없거나 못 읽으면 Err
//------------------------------------------------------------------
pub fn rows(db: &str) -> Result<std::sync::Arc<Cached>, String> {
    if !std::path::Path::new(db).is_file() {
        return Err("meta.sqlite 없음".into());
    }
    let mt = mtime_ms(db).max(mtime_ms(&format!("{}-wal", db)));
    if let Some((m, c)) = CACHE.lock().unwrap().get(db) {
        if *m == mt {
            return Ok(c.clone());
        }
    }
    let conn = rusqlite::Connection::open_with_flags(db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| e.to_string())?;
    let mut rows = HashMap::new();
    {
        let mut st = conn.prepare("SELECT doc_path, format, scanned, recv_kind, modified_ts, created_ts, created_conf, added_ts, \
            doc_type, doc_type_score, is_form, form_score, title_display FROM doc_meta").map_err(|e| e.to_string())?;
        let it = st.query_map([], |r| {
            Ok((r.get::<_, String>(0)?, Row {
                format: r.get(1)?, scanned: r.get(2)?, recv_kind: r.get(3)?, modified: r.get(4)?, created: r.get(5)?,
                created_conf: r.get(6)?, added: r.get(7)?, doc_type: r.get(8)?, doc_type_score: r.get(9)?,
                is_form: r.get(10)?, form_score: r.get(11)?, title: r.get(12)?,
            }))
        }).map_err(|e| e.to_string())?;
        for x in it.flatten() {
            rows.insert(x.0, x.1);
        }
    }
    let mut by_name: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut by_path: HashMap<String, Vec<(String, String)>> = HashMap::new();
    {
        let mut st = conn.prepare("SELECT doc_path, name, role FROM doc_people").map_err(|e| e.to_string())?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?.unwrap_or_default())))
            .map_err(|e| e.to_string())?;
        for (p, n, role) in it.flatten() {
            by_name.entry(n.clone()).or_default().push((p.clone(), role.clone()));
            by_path.entry(p).or_default().push((n, role));
        }
    }
    let c = std::sync::Arc::new(Cached { rows, by_name, by_path });
    CACHE.lock().unwrap().insert(db.to_string(), (mt, c.clone()));
    Ok(c)
}

//------------------------------------------------------------------
// 질의에서 사람으로 볼 이름들 — (한글 이름, 별칭 사전, me)
//------------------------------------------------------------------
fn names(db: &str, root: &str) -> std::sync::Arc<(HashSet<String>, HashMap<String, String>, Option<String>)> {
    let pp = format!("{}\\people.yaml", root.trim_end_matches(['\\', '/']));
    let key = (mtime_ms(db), mtime_ms(&pp));
    if let Some((k, v)) = NAMES.lock().unwrap().get(db) {
        if *k == key {
            return v.clone();
        }
    }
    let mut set: HashSet<String> = rows(db).map(|c| c.by_name.keys().filter(|n| RE_HANGUL.is_match(n)).cloned().collect())
        .unwrap_or_default();
    let aliases = super::normalize::load_aliases(root);
    for v in aliases.values() {
        if RE_HANGUL.is_match(v) {
            set.insert(v.clone());
        }
    }
    let me = super::people::load_user(root).me;
    let v = std::sync::Arc::new((set, aliases, me));
    NAMES.lock().unwrap().insert(db.to_string(), (key, v.clone()));
    v
}

static DOC_TYPES: Lazy<Mutex<HashMap<String, (i64, std::sync::Arc<Vec<String>>)>>> = Lazy::new(|| Mutex::new(HashMap::new()));

// 유형어 목록 — 규칙을 읽으면 정규식을 수백 개 만들어 수십 ms 라, meta_rules.yaml 이 바뀔 때만 다시 읽는다
fn doc_types(root: &str) -> std::sync::Arc<Vec<String>> {
    let mt = mtime_ms(&crate::pyutil::join(root, "meta_rules.yaml"));
    if let Some((m, v)) = DOC_TYPES.lock().unwrap().get(root) {
        if *m == mt {
            return v.clone();
        }
    }
    let v = std::sync::Arc::new(super::rules::load_rules(root).doc_types);
    DOC_TYPES.lock().unwrap().insert(root.to_string(), (mt, v.clone()));
    v
}

// ── 시간 (§12-1) ──────────────────────────────────────────

// KST 자정 epoch 초
fn kst_ts(d: NaiveDate) -> i64 {
    d.and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp() - 9 * 3600
}

fn month_range(y: i32, m: u32) -> (NaiveDate, NaiveDate) {
    let s = NaiveDate::from_ymd_opt(y, m, 1).unwrap();
    let e = if m == 12 { NaiveDate::from_ymd_opt(y + 1, 1, 1) } else { NaiveDate::from_ymd_opt(y, m + 1, 1) }.unwrap();
    (s, e)
}

fn prev_month_start(d: NaiveDate) -> NaiveDate {
    let p = d.with_day(1).unwrap() - Duration::days(1);
    p.with_day(1).unwrap()
}

//------------------------------------------------------------------
// 시간 표현 찾기 — (기간, 뗄 글자 범위들)
//=> 올해 · 작년 · 재작년 · 2023년 · 23년 · 이번/지난 달 · 3월(쯤) · 상/하반기 · 지난주 · 최근 · N년/개월 전.
//
// -in: q = 질의 / today = 기준 날짜
// -out: (Some(기간), [(시작, 끝) 바이트 범위]) / 없으면 (None, [])
//------------------------------------------------------------------
pub fn parse_time(q: &str, today: NaiveDate) -> (Option<DateRange>, Vec<(usize, usize)>) {
    let mut spans = vec![];
    let mut y: Option<i32> = None;
    let mut label: Vec<String> = vec![];
    if let Ok(Some(m)) = RE_YEAR.captures(q) {
        let t = m.get(0).unwrap().as_str();
        y = Some(if t.contains("재작년") {
            today.year() - 2
        } else if t.contains("작년") || RE_LAST_YEAR.is_match(t) {
            today.year() - 1
        } else if t.contains("올해") || t.contains("금년") {
            today.year()
        } else if let Some(g) = m.get(1) {
            g.as_str().parse().unwrap_or(today.year())
        } else {
            2000 + m.get(2).map(|g| g.as_str().parse::<i32>().unwrap_or(0)).unwrap_or(0)
        });
        let mm = m.get(0).unwrap();
        spans.push((mm.start(), mm.end()));
        label.push(format!("{}년", y.unwrap()));
    }
    let mm = RE_MONTH.captures(q).ok().flatten();
    let half = RE_HALF.find(q);
    let rel = RE_REL.captures(q);
    let mut range: Option<(NaiveDate, NaiveDate)> = None;
    if let Some(mm) = mm {
        let mon: u32 = mm.get(1).unwrap().as_str().parse().unwrap_or(1);
        let yy = y.unwrap_or(if mon <= today.month() { today.year() } else { today.year() - 1 });
        let (mut s0, mut e0) = month_range(yy, mon);
        let approx = mm.get(2).is_some();
        if approx {
            // "3월쯤" — 앞뒤 한 달 더
            s0 = prev_month_start(s0);
            e0 = month_range(e0.year(), e0.month()).1;
        }
        range = Some((s0, e0));
        let g = mm.get(0).unwrap();
        spans.push((g.start(), g.end()));
        label = vec![format!("{}년 {}월{}", yy, mon, if approx { "쯤" } else { "" })];
    } else if let Some(h) = half {
        let yy = y.unwrap_or(today.year());
        range = Some(if h.as_str() == "상반기" {
            (NaiveDate::from_ymd_opt(yy, 1, 1).unwrap(), NaiveDate::from_ymd_opt(yy, 7, 1).unwrap())
        } else {
            (NaiveDate::from_ymd_opt(yy, 7, 1).unwrap(), NaiveDate::from_ymd_opt(yy + 1, 1, 1).unwrap())
        });
        spans.push((h.start(), h.end()));
        label = vec![format!("{}년 {}", yy, h.as_str())];
    } else if let Some(yy) = y {
        range = Some((NaiveDate::from_ymd_opt(yy, 1, 1).unwrap(), NaiveDate::from_ymd_opt(yy + 1, 1, 1).unwrap()));
    } else if let Some(r) = rel {
        let t = r.get(0).unwrap().as_str();
        let tomorrow = today + Duration::days(1);
        range = if Regex::new(r"^이번\s*달").unwrap().is_match(t) {
            Some(month_range(today.year(), today.month()))
        } else if Regex::new(r"^(지난|저번)\s*달").unwrap().is_match(t) {
            let p = today.with_day(1).unwrap() - Duration::days(1);
            Some(month_range(p.year(), p.month()))
        } else if Regex::new(r"^(지난|저번)\s*주").unwrap().is_match(t) {
            Some((today - Duration::days(7), tomorrow))
        } else if Regex::new(r"^이번\s*주").unwrap().is_match(t) {
            Some((today - Duration::days(today.weekday().num_days_from_monday() as i64), tomorrow))
        } else if t == "최근" || t == "요즘" {
            Some((today - Duration::days(90), tomorrow))
        } else if let Some(n) = r.get(1) {
            let yy = today.year() - n.as_str().parse::<i32>().unwrap_or(0);
            Some((NaiveDate::from_ymd_opt(yy, 1, 1).unwrap(), NaiveDate::from_ymd_opt(yy + 1, 1, 1).unwrap()))
        } else if let Some(n) = r.get(2) {
            let mut p = today.with_day(1).unwrap();
            for _ in 0..n.as_str().parse::<i32>().unwrap_or(0) {
                p = prev_month_start(p);
            }
            Some(month_range(p.year(), p.month()))
        } else {
            None
        };
        let g = r.get(0).unwrap();
        spans.push((g.start(), g.end()));
        label = vec![RE_SPACES.replace_all(t, " ").to_string()];
    }
    match range {
        None => (None, vec![]),
        Some((s, e)) => (Some(DateRange { from: kst_ts(s), to: kst_ts(e), label: label.join(" ") }), spans),
    }
}

// 바이트 위치가 이미 뗀 범위 안인가
fn in_cut(cut: &[(usize, usize)], i: usize) -> bool {
    cut.iter().any(|(a, b)| *a <= i && i < *b)
}

// 바이트 위치 앞 · 뒤 글자가 한글인가
fn hangul_before(text: &str, i: usize) -> bool {
    text[..i].chars().next_back().map_or(false, |c| ('가'..='힣').contains(&c))
}
fn hangul_at(text: &str, i: usize) -> bool {
    text[i..].chars().next().map_or(false, |c| ('가'..='힣').contains(&c))
}

//------------------------------------------------------------------
// 질의 해석 (§12) — 규칙만
//=> 1) 사람 2) 시간 3) 동사(역할 · 날짜 칸) 4) 형식 · 받은 경로 · 스캔 · 유형 · 양식 5) 남은 말 = 본문 검색어
//
// -in: q = 질의 / names · aliases · me = 사람 이름들 / doc_types = 유형어 / today = 기준 날짜
// -out: 해석 결과
//------------------------------------------------------------------
pub fn parse(q: &str, names: &HashSet<String>, aliases: &HashMap<String, String>, me: Option<&str>,
             doc_types: &[String], today: NaiveDate) -> Parsed {
    let text = q.trim().to_string();
    // 영문 낱말만 소문자로(한글은 그대로 — 바이트 위치가 원문과 같게)
    let low = text.to_ascii_lowercase();
    let mut cut: Vec<(usize, usize)> = vec![];
    let mut out = Parsed { date_field: "modified".into(), ..Default::default() };

    // 1) 사람 — 긴 이름부터, 뒤에 붙은 조사까지
    let mut ns: Vec<&String> = names.iter().collect();
    ns.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()).then(a.cmp(b)));
    for n in ns {
        // 글자로 찾고(겹치지 않게 앞에서부터) 뒤에 붙은 조사 꼬리를 붙인다 — 정규식 이름+꼬리와 같다
        let mut from = 0;
        while let Some(off) = text[from..].find(n.as_str()) {
            let start = from + off;
            let end_n = start + n.len();
            let end = end_n + RE_NAME_TAIL.find(&text[end_n..]).map_or(0, |t| t.end());
            from = end.max(start + 1);
            while !text.is_char_boundary(from) {
                from += 1;
            }
            if in_cut(&cut, start) {
                continue;
            }
            // 이름이 더 긴 한글 낱말의 일부면(고봉수지원팀) 사람으로 보지 않는다
            if start > 0 && hangul_before(&text, start) {
                continue;
            }
            if end == end_n && end_n < text.len() && hangul_at(&text, end_n) {
                continue;
            }
            out.people.push((n.clone(), None, false));
            cut.push((start, end));
        }
    }
    let mut als: Vec<(&String, &String)> = aliases.iter().collect();
    als.sort();
    for (al, canon) in als {
        if !RE_ALIAS_KEY.is_match(al) || !low.contains(al.as_str()) {
            continue;
        }
        let re = RE_ALIASES.lock().unwrap().entry(al.clone())
            .or_insert_with(|| FRegex::new(&format!(r"(?<![a-z0-9]){}(?![a-z0-9]){}", regex::escape(al), NAME_TAIL)).ok()).clone();
        let re = match re {
            Some(r) => r,
            None => continue,
        };
        for m in re.find_iter(&low).flatten() {
            if !out.people.iter().any(|p| &p.0 == canon) {
                out.people.push((canon.clone(), None, false));
            }
            cut.push((m.start(), m.end()));
        }
    }
    if let (Some(me), Ok(Some(m))) = (me, RE_ME.captures(&text)) {
        let g = m.get(1).unwrap();
        let strong = ["내가", "제가", "나의", "저의"].contains(&g.as_str());
        let verb_next = RE_ME_VERB.is_match(&text[g.end()..]);
        if strong || verb_next {
            if !out.people.iter().any(|p| p.0 == me) {
                out.people.push((me.to_string(), None, true));
            }
            cut.push((g.start(), g.end()));
        }
    }

    // 2) 시간
    let (tm, spans) = parse_time(&text, today);
    if tm.is_some() {
        out.date = tm;
        cut.extend(spans);
    }

    // 3) 동사 — 역할과 날짜 칸
    let mut role: Option<String> = None;
    for (re, r, field) in RE_VERBS.iter() {
        if let Some(mv) = re.find(&text) {
            if role.is_none() {
                role = r.map(|s| s.to_string());
            }
            if out.date_field == "modified" {
                out.date_field = (*field).into();
            }
            cut.push((mv.start(), mv.end()));
            break;
        }
    }
    for p in out.people.iter_mut() {
        p.1 = role.clone();
    }

    // 4) 사전 — 받은 경로 · 형식 · 스캔 · 유형 · 양식
    for (re, kind) in RE_RECV.iter() {
        if let Some(mr) = re.find(&low) {
            out.recv = Some((*kind).into());
            if out.date.is_some() {
                out.date_field = "added".into();
            }
            cut.push((mr.start(), mr.end()));
            break;
        }
    }
    for (re, fmt) in RE_FORMAT.iter() {
        if let Ok(Some(mf)) = re.find(&low) {
            out.format = Some((*fmt).into());
            cut.push((mf.start(), mf.end()));
            break;
        }
    }
    if let Some(ms) = RE_SCAN.find(&text) {
        out.scanned = true;
        cut.push((ms.start(), ms.end()));
    }
    for w in doc_types {
        if text.contains(w.as_str()) {
            out.doc_type = Some(if w == "메뉴얼" { "매뉴얼".into() } else { w.clone() });
            break;
        }
    }
    out.is_form = ["양식", "서식", "템플릿"].iter().any(|w| text.contains(w));

    // 5) 본문 검색어 — 뗀 범위를 지우고 명령 · 군말을 뺀다(유형어 · 양식어는 남는다)
    let rest: String = text.char_indices().map(|(i, c)| if in_cut(&cut, i) { ' ' } else { c }).collect();
    let rest = RE_FILL_END.replace(&rest, " ").to_string();
    let rest = RE_PARTICLE.replace_all(&rest, " ").to_string();
    let words: Vec<&str> = rest.split_whitespace().filter(|w| !GENERIC.contains(w) && !RE_NONWORD.is_match(w)).collect();
    out.query = words.join(" ");
    out.conditions = !out.people.is_empty() || out.date.is_some() || out.format.is_some() || out.recv.is_some() || out.scanned;
    if out.conditions && is_question(&text) && !doc_context(&text, doc_types) {
        out.conditions = false;
        out.skipped = Some("내용 질문".into());
    }
    out
}

//------------------------------------------------------------------
// 내용을 묻는 질문 꼴인가 — 물음표 · 의문 어미 · 의문사 ("찾아줘 · 보여줘" 로 끝나면 아니다)
//------------------------------------------------------------------
pub fn is_question(text: &str) -> bool {
    let t = text.trim();
    if RE_FIND_END.is_match(t) {
        return false;
    }
    RE_Q_END.is_match(t) || RE_Q_WORD.is_match(t)
}

//------------------------------------------------------------------
// 문서를 가리키는 말이 있는가 — 문서 동작 바로 뒤 두 낱말 안에 문서 낱말
//------------------------------------------------------------------
pub fn doc_context(text: &str, doc_types: &[String]) -> bool {
    let mut g = RE_DOC_CTX.lock().unwrap();
    if let Some((k, re)) = g.as_ref() {
        if k.as_slice() == doc_types {
            return re.is_match(text);
        }
    }
    let mut nouns: Vec<String> = ["문서", "파일", "자료", "양식", "서식", "엑셀", "피피티", "ppt", "pdf", "한글", "워드", "hwp", "슬라이드"]
        .iter().map(|s| s.to_string()).collect();
    nouns.extend(doc_types.iter().cloned());
    let noun_re = nouns.iter().map(|n| regex::escape(n)).collect::<Vec<_>>().join("|");
    let verb_re = format!("{}|{}|{}", VERB_AUTHOR, VERB_EDITOR, VERB_RECV);
    match Regex::new(&format!(r"(?i)(?:{})\s*(?:\S+\s+){{0,2}}?\S*(?:{})", verb_re, noun_re)) {
        Ok(re) => {
            let hit = re.is_match(text);
            *g = Some((doc_types.to_vec(), re));
            hit
        }
        Err(_) => false,
    }
}

//------------------------------------------------------------------
// 필터 · 가산 계획 (진입점)
//=> 1) 해석 2) 조건마다 필터 집합 · 가산 3) 교집합이 0 이면 완화(사람 → 가산, 기간 ±1년, 유형 · 양식 → 가산)
//   SIMPLERAG_META_QUERY=0 이면 끈다.
//
// -in: q = 질의 / db = meta.sqlite 경로 / root = 사전 폴더 / folder = 폴더 한정 범위 / today = 기준(None = 오늘)
// -out: Some(계획) / 메타가 없거나 조건이 없으면 None
//------------------------------------------------------------------
pub fn plan(q: &str, db: &str, root: &str, folder: Option<&str>, today: Option<NaiveDate>) -> Option<Plan> {
    plan_with(q, db, root, folder, today, &PlanOpts::default())
}

//------------------------------------------------------------------
// 화면에서 고친 조건 (메타데이터 검색 설계서 §14 조건 칩)
// -필드: off = 끈 조건 종류(person · date · format · recv · scanned · type · form)
//        widen = 기간을 앞뒤로 넓힐 햇수(0 = 그대로)
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct PlanOpts {
    pub off: Vec<String>,
    pub widen: i64,
}

impl PlanOpts {
    // 워커 요청 JSON 의 {"off": [...], "widen": N} 에서
    pub fn from_json(v: &Value) -> PlanOpts {
        PlanOpts {
            off: v["off"].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()).unwrap_or_default(),
            widen: v["widen"].as_i64().unwrap_or(0).clamp(0, 10),
        }
    }
    fn is_off(&self, kind: &str) -> bool {
        self.off.iter().any(|k| k == kind)
    }
}

//------------------------------------------------------------------
// 필터 · 가산 계획 — 화면에서 끈 조건 · 넓힌 기간을 반영
//=> 질의 자체에 조건이 없으면 None(예전과 같다). 조건이 있던 질의는 다 꺼도 계획을 돌려준다 —
//   화면이 꺼진 칩을 다시 켤 수 있게. 남은 조건도 본문 검색어도 없으면 원래 질의로 본문 검색한다.
//
// -in: plan 과 같음 + opts = 끈 조건 · 넓힐 햇수
// -out: Some(계획) / 메타가 없거나 질의에 조건이 없으면 None
//------------------------------------------------------------------
pub fn plan_with(q: &str, db: &str, root: &str, folder: Option<&str>, today: Option<NaiveDate>, opts: &PlanOpts) -> Option<Plan> {
    if std::env::var("SIMPLERAG_META_QUERY").map(|v| ["0", "off", "false"].contains(&v.trim())).unwrap_or(false) {
        return None;
    }
    let t0 = Instant::now();
    let cached = rows(db).ok()?;
    let nm = names(db, root);
    let doc_types = doc_types(root);
    let today = today.unwrap_or_else(|| (chrono::Utc::now() + Duration::hours(9)).date_naive());
    let mut p = parse(q, &nm.0, &nm.1, nm.2.as_deref(), &doc_types, today);
    if !p.conditions {
        return None;
    }
    // 화면에서 끈 조건은 지운다(본문 검색어에서는 이미 떼어 냈으므로 그대로 둔다)
    if opts.is_off("person") { p.people.clear(); }
    if opts.is_off("date") { p.date = None; }
    if opts.is_off("format") { p.format = None; }
    if opts.is_off("recv") { p.recv = None; }
    if opts.is_off("scanned") { p.scanned = false; }
    if opts.is_off("type") { p.doc_type = None; }
    if opts.is_off("form") { p.is_form = false; }
    if let (Some(d), true) = (p.date.as_mut(), opts.widen > 0) {
        // 기간 넓히기 — 연도가 내용(점검 연도)이고 파일 날짜가 다를 때 정답을 되찾게
        d.from -= opts.widen * 365 * 86400;
        d.to += opts.widen * 365 * 86400;
        d.label = format!("{} ±{}년", d.label, opts.widen);
    }
    let left = !p.people.is_empty() || p.date.is_some() || p.format.is_some() || p.recv.is_some() || p.scanned;
    if !left && p.query.trim().is_empty() {
        // 조건을 다 껐는데 본문 검색어도 없다 — 원래 질의 그대로 본문 검색
        p.query = q.trim().to_string();
    }
    let mut res = make_plan(&cached, &p, folder);
    res.off = opts.off.clone();
    res.widen = opts.widen;
    res.ms = t0.elapsed().as_secs_f64() * 1000.0;
    Some(res)
}

fn chip(kind: &str, label: String, mode: &str) -> Chip {
    Chip { kind: kind.into(), label, mode: mode.into() }
}

// 조건별 문서 집합 · 가산 → 교집합 · 완화
fn make_plan(c: &Cached, p: &Parsed, folder: Option<&str>) -> Plan {
    let fprefix = folder.map(|f| format!("{}\\", pyutil::rstrip_chars(&pyutil::normcase(&pyutil::abspath(f)), "\\/")));
    let rows: HashMap<&String, &Row> = c.rows.iter()
        .filter(|(k, _)| fprefix.as_ref().map_or(true, |f| pyutil::normcase(k).starts_with(f.as_str()))).collect();
    let allp: HashSet<String> = rows.keys().map(|k| (*k).clone()).collect();
    let mut hard: Vec<(&str, HashSet<String>)> = vec![];
    let mut soft: Vec<(&str, HashMap<String, f64>)> = vec![];
    let mut chips: Vec<Chip> = vec![];
    if let Some(f) = &p.format {
        hard.push(("format", rows.iter().filter(|(_, r)| r.format.as_deref() == Some(f)).map(|(k, _)| (*k).clone()).collect()));
        let lab = match f.as_str() { "excel" => "엑셀", "ppt" => "PPT", "hwp" => "한글", "pdf" => "PDF", _ => "워드" };
        chips.push(chip("format", lab.into(), "filter"));
    }
    if p.scanned {
        hard.push(("scanned", rows.iter().filter(|(_, r)| r.scanned == Some(1)).map(|(k, _)| (*k).clone()).collect()));
        chips.push(chip("scanned", "스캔본".into(), "filter"));
    }
    if let Some(rk) = &p.recv {
        hard.push(("recv", rows.iter().filter(|(_, r)| r.recv_kind.as_deref() == Some(rk)).map(|(k, _)| (*k).clone()).collect()));
        let lab = match rk.as_str() { "mail" => "메일로 받은", "drive" => "드라이브에서 받은", _ => "AI 도구에서 받은" };
        chips.push(chip("recv", lab.into(), "filter"));
    }
    if let Some(d) = &p.date {
        let (k, b) = date_sets(&rows, d.from, d.to, &p.date_field);
        hard.push(("date", k));
        soft.push(("date", b));
        let fl = match p.date_field.as_str() { "created" => "작성일", "added" => "받은 날", _ => "수정일" };
        chips.push(chip("date", format!("{} · {}", d.label, fl), "filter"));
    }
    if !p.people.is_empty() {
        let (k, b) = people_sets(c, &p.people, &allp);
        let has = !k.is_empty();
        for (n, role, me) in &p.people {
            let r = match role.as_deref() { Some("author") => "작성", Some("editor") => "수정", _ => "관련" };
            chips.push(chip("person", format!("{}{} ({})", n, if *me { " · 나" } else { "" }, r), if has { "filter" } else { "boost" }));
        }
        if has {
            hard.push(("people", k));
        }
        soft.push(("people", b));
    }
    if let Some(dt) = &p.doc_type {
        let strong = rows.values().any(|r| r.doc_type.as_deref() == Some(dt) && r.doc_type_score.unwrap_or(0.0) >= TYPE_FILTER_MIN);
        soft.push(("type", rows.iter().filter(|(_, r)| r.doc_type.as_deref() == Some(dt) && r.doc_type_score.unwrap_or(0.0) >= 3.0)
            .map(|(k, r)| ((*k).clone(), BOOST_TYPE * (r.doc_type_score.unwrap_or(0.0) / TYPE_FILTER_MIN).min(1.0))).collect()));
        if strong {
            hard.push(("type", rows.iter().filter(|(_, r)| r.doc_type.as_deref() == Some(dt) && r.doc_type_score.unwrap_or(0.0) >= 3.0)
                .map(|(k, _)| (*k).clone()).collect()));
        }
        chips.push(chip("type", dt.clone(), if strong { "filter" } else { "boost" }));
    }
    if p.is_form {
        let strong = rows.values().any(|r| r.is_form == Some(1) && r.form_score.unwrap_or(0.0) >= TYPE_FILTER_MIN);
        soft.push(("form", rows.iter().filter(|(_, r)| r.is_form == Some(1))
            .map(|(k, r)| ((*k).clone(), BOOST_FORM * (r.form_score.unwrap_or(0.0) / TYPE_FILTER_MIN).min(1.0))).collect()));
        if strong {
            hard.push(("form", rows.iter().filter(|(_, r)| r.is_form == Some(1)).map(|(k, _)| (*k).clone()).collect()));
        }
        chips.push(chip("form", "양식".into(), if strong { "filter" } else { "boost" }));
    }

    // 교집합 — 0 이면 하나씩 푼다(§13-3)
    let inter = |hard: &Vec<(&str, HashSet<String>)>| -> HashSet<String> {
        let mut out = allp.clone();
        for (_, s) in hard {
            out.retain(|k| s.contains(k));
        }
        out
    };
    let set_mode = |chips: &mut Vec<Chip>, kind: &str, mode: &str| {
        for ch in chips.iter_mut().filter(|c| c.kind == kind) {
            ch.mode = mode.into();
        }
    };
    let mut relaxed: Vec<String> = vec![];
    let mut docs = inter(&hard);
    if !hard.is_empty() && docs.is_empty() && hard.iter().any(|h| h.0 == "people") {
        hard.retain(|h| h.0 != "people");
        relaxed.push("작성자 조건 해제".into());
        set_mode(&mut chips, "person", "boost");
        docs = inter(&hard);
    }
    if !hard.is_empty() && docs.is_empty() && hard.iter().any(|h| h.0 == "date") {
        if let Some(d) = &p.date {
            let (k, b) = date_sets(&rows, d.from - 365 * 86400, d.to + 365 * 86400, &p.date_field);
            for h in hard.iter_mut().filter(|h| h.0 == "date") {
                h.1 = k.clone();
            }
            for s in soft.iter_mut().filter(|s| s.0 == "date") {
                s.1 = b.clone();
            }
        }
        relaxed.push("기간을 앞뒤 1년 넓힘".into());
        docs = inter(&hard);
    }
    for (k, msg) in [("type", "유형 조건 해제"), ("form", "양식 조건 해제")] {
        if !hard.is_empty() && docs.is_empty() && hard.iter().any(|h| h.0 == k) {
            hard.retain(|h| h.0 != k);
            relaxed.push(msg.into());
            set_mode(&mut chips, k, "boost");
            docs = inter(&hard);
        }
    }
    let mut boosts: HashMap<String, f64> = HashMap::new();
    for (_, part) in &soft {
        for (k, v) in part {
            *boosts.entry(k.clone()).or_insert(0.0) += v;
        }
    }
    let meta_only = p.query.trim().is_empty();
    let filtered = if hard.is_empty() { None } else { Some(docs) };
    let mut order: Vec<String> = vec![];
    if meta_only {
        // 본문 검색어가 없다 — 조건에 맞는 문서를 가산 · 수정일 순으로(§13-2)
        let cand: Vec<String> = match &filtered { Some(d) => d.iter().cloned().collect(), None => boosts.keys().cloned().collect() };
        order = cand;
        order.sort_by(|a, b| {
            let ba = boosts.get(a).copied().unwrap_or(0.0);
            let bb = boosts.get(b).copied().unwrap_or(0.0);
            bb.partial_cmp(&ba).unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| c.rows.get(b).and_then(|r| r.modified).unwrap_or(0).cmp(&c.rows.get(a).and_then(|r| r.modified).unwrap_or(0)))
                .then_with(|| a.cmp(b))
        });
    }
    Plan { query: p.query.clone(), docs: filtered, boosts, chips, relaxed, meta_only, order, ms: 0.0, off: vec![], widen: 0 }
}

//------------------------------------------------------------------
// 기간 조건 — (필터 집합, 가산). 작성일은 신뢰도 0.7 이상일 때만, 아니면 수정일(결정 D3).
//   날짜를 모르는 문서는 떨어뜨리지 않는다.
//   "만든"이어도 수정일(파일 날짜)이 기간 안이면 남기고 가산은 절반 — 사람이 기억하는 날짜는 파일 날짜인
//   경우가 많다(메타 골든셋 t01: 작성 2009 · 수정 2014 문서를 "2014년에 만든" 으로 찾음).
//------------------------------------------------------------------
fn date_sets(rows: &HashMap<&String, &Row>, lo: i64, hi: i64, field: &str) -> (HashSet<String>, HashMap<String, f64>) {
    let mut keep = HashSet::new();
    let mut boost = HashMap::new();
    for (k, r) in rows {
        let ts = match field {
            "created" => if r.created.is_some() && r.created_conf.unwrap_or(0.0) >= DATE_CONF_MIN { r.created } else { r.modified },
            "added" => r.added,
            _ => r.modified,
        };
        match ts {
            None => {
                keep.insert((*k).clone());
            }
            Some(t) if lo <= t && t < hi => {
                keep.insert((*k).clone());
                boost.insert((*k).clone(), BOOST_DATE);
            }
            _ => {
                if field == "created" && r.modified.map_or(false, |m| lo <= m && m < hi) {
                    keep.insert((*k).clone());
                    boost.insert((*k).clone(), BOOST_DATE / 2.0);
                }
            }
        }
    }
    (keep, boost)
}

//------------------------------------------------------------------
// 사람 조건 — (필터 집합, 가산). 그 이름이 관련인인 문서. 역할로 떨어뜨리지 않고 역할별 가산만 다르게.
//   질의 동사와 같은 역할이면 +0.5. 여러 사람이면 교집합.
//------------------------------------------------------------------
fn people_sets(c: &Cached, people: &[(String, Option<String>, bool)], allp: &HashSet<String>) -> (HashSet<String>, HashMap<String, f64>) {
    let mut keep: Option<HashSet<String>> = None;
    let mut boost: HashMap<String, f64> = HashMap::new();
    for (name, want, _) in people {
        let mut got: HashMap<String, f64> = HashMap::new();
        for (path, role) in c.by_name.get(name).map(|v| v.as_slice()).unwrap_or(&[]) {
            let mut b = boost_role(role);
            if want.as_deref() == Some(role.as_str()) {
                b += 0.5;
            }
            let e = got.entry(path.clone()).or_insert(0.0);
            if b > *e {
                *e = b;
            }
        }
        let s: HashSet<String> = got.keys().filter(|k| allp.contains(*k)).cloned().collect();
        keep = Some(match keep { None => s, Some(k) => k.intersection(&s).cloned().collect() });
        for (k, v) in got {
            *boost.entry(k).or_insert(0.0) += v;
        }
    }
    (keep.unwrap_or_default(), boost)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, dd: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, dd).unwrap()
    }

    #[test]
    fn parse_examples() {
        let names: HashSet<String> = ["고봉수", "조강래"].iter().map(|s| s.to_string()).collect();
        let al: HashMap<String, String> = [("bong9".to_string(), "고봉수".to_string())].into_iter().collect();
        let types: Vec<String> = ["절차서", "보고서", "규정", "확인서"].iter().map(|s| s.to_string()).collect();
        let today = d(2026, 10, 29);
        let p = parse("작년에 고봉수님이 만든 절차서 양식을 찾아줘", &names, &al, Some("고봉수"), &types, today);
        assert!(p.conditions);
        assert_eq!(p.query, "절차서 양식");
        assert_eq!(p.people[0].0, "고봉수");
        assert_eq!(p.people[0].1.as_deref(), Some("author"));
        assert_eq!(p.date_field, "created");
        assert_eq!(p.date.as_ref().unwrap().label, "2025년");
        assert!(p.is_form);
        // 유형어만 · 내용 질문은 조건 없음
        assert!(!parse("휴가 규정에서 연차 이월", &names, &al, Some("고봉수"), &types, today).conditions);
        assert!(!parse("2016년 한국의 점수는 몇 점인가?", &names, &al, Some("고봉수"), &types, today).conditions);
        assert!(!parse("내가 결혼하면 휴가 며칠 줘?", &names, &al, Some("고봉수"), &types, today).conditions);
        // 질문이어도 문서를 가리키면 조건 유지
        assert!(parse("작년에 고봉수님이 만든 보고서에서 예산은 얼마야?", &names, &al, Some("고봉수"), &types, today).conditions);
        let p = parse("내가 만든 엑셀", &names, &al, Some("고봉수"), &types, today);
        assert_eq!(p.format.as_deref(), Some("excel"));
        assert!(p.people[0].2);
        assert_eq!(p.query, "");
        let p = parse("메일로 받은 한글 파일 중 점검 확인서", &names, &al, None, &types, today);
        assert_eq!((p.recv.as_deref(), p.format.as_deref(), p.query.as_str()), (Some("mail"), Some("hwp"), "점검 확인서"));
    }

    #[test]
    fn time_ranges() {
        let today = d(2026, 10, 29);
        let (r, _) = parse_time("작년 3월쯤 고친 PPT", today);
        let r = r.unwrap();
        assert_eq!(r.label, "2025년 3월쯤");
        assert_eq!(r.from, kst_ts(d(2025, 2, 1)));
        assert_eq!(r.to, kst_ts(d(2025, 5, 1)));
        let (r, _) = parse_time("지난주에 받은 제안서", today);
        assert_eq!(r.unwrap().from, kst_ts(d(2026, 10, 22)));
        let (r, _) = parse_time("12월에 만든", today);
        assert_eq!(r.unwrap().label, "2025년 12월");
    }
}
