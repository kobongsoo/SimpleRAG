//------------------------------------------------------------------
// 사람 별칭 사전 — 자동 제안 + 승인 (메타데이터 검색 설계서 §7-2 · §17 3단계 · 결정 D2, Python meta/people.py)
//=> 계정 ID · 영문 이름(bong9, Cho Kang Lae)을 대표 이름(고봉수, 조강래)으로 잇는 사전을 만든다.
//   자동으로 적용하지 않는다 — 후보를 보여 주고 사람이 승인한 것만 쓴다(오연결 위험, 동의어 사전과 같은 흐름).
//
//   후보 근거 세 가지
//    ① 같은 문서 동시 출현 — 한 문서의 만든 이 / 마지막 저장자가 한글 이름 / 계정·영문이고 그 짝이 2번 이상
//    ② 로마자 일치 — Cho Kang Lae · kim-sung-su ↔ 조강래 · 김성수 (성 표기 표 + 음절 표기 변형)
//    ③ 경로 단서 — 파일 이름에 한글 이름(…_고봉수.xlsx · (고봉수))이 있고 그 문서 작성 · 저장자가 계정
//
//   파일(데이터 루트, synonyms.yaml 과 같은 자리)
//    people.default.yaml — 배포 기본값(배포 때 덮어씀)
//    people.yaml        — 사용자 사전: people: {대표: [별칭…]} · rejected: [[별칭, 대표]…] · me: 대표
//    people_candidates.json — 마지막 후보 목록(승인 화면 · 명령이 읽는다)
//------------------------------------------------------------------

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;
use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::normalize as norm;
use super::pyfmt;
use super::rules::{load_rules, yaml_str};
use crate::pyutil;

pub const HEADER: &str = "# ─────────────────────────────────────────────────────────────────────
# 사람 별칭 사전 — 계정 ID · 영문 이름을 대표 이름으로 잇는다 (plan/메타데이터검색_설계서_20261029.html §7-2)
#
#   people:   대표 이름: [별칭, 별칭, ...]     예) 고봉수: [bong9, bong49]
#   rejected: [[별칭, 대표 이름], ...]          후보로 다시 올리지 않는 짝
#   me:       대표 이름                          \"내가 만든 문서\" 의 '나'
#   후보는 simplerag meta people suggest 로 만들고 approve · reject 로 고른다.
#   고친 뒤에는 simplerag meta rebuild 로 다시 정규화한다(approve · reject 는 저절로 한다).
# ─────────────────────────────────────────────────────────────────────
";

// ── 로마자 (②) ───────────────────────────────────────────

// 성 — 실제로 쓰는 표기들(국어 로마자 표기법 + 관용 표기)
const SURNAMES: &[(&str, &str)] = &[
    ("김", "kim gim"), ("이", "lee yi rhee lie i"), ("박", "park pak bak"), ("최", "choi choe chey"), ("정", "jung jeong chung joung"),
    ("조", "cho jo joe"), ("강", "kang gang"), ("윤", "yoon yun"), ("장", "jang chang"), ("임", "lim im rim"), ("한", "han"),
    ("오", "oh o"), ("서", "seo suh so"), ("신", "shin sin"), ("권", "kwon gwon"), ("황", "hwang"), ("안", "ahn an"),
    ("송", "song"), ("류", "ryu yoo yu"), ("유", "yoo yu you"), ("홍", "hong"), ("전", "jeon jun chun"), ("고", "ko go koh"),
    ("문", "moon mun"), ("양", "yang"), ("손", "son sohn"), ("배", "bae bai"), ("백", "baek paek baik"), ("허", "heo huh hur"),
    ("남", "nam"), ("노", "noh no roh"), ("하", "ha"), ("곽", "kwak gwak"), ("성", "sung seong"), ("차", "cha"),
    ("주", "joo ju chu"), ("우", "woo u"), ("구", "koo gu ku"), ("민", "min"), ("나", "na ra"), ("진", "jin chin"),
    ("지", "ji chi"), ("엄", "um eom"), ("채", "chae"), ("원", "won"), ("천", "chun cheon"), ("방", "bang"), ("공", "kong gong"),
    ("현", "hyun hyeon"), ("함", "ham"), ("변", "byun byeon"), ("염", "yeom yum"), ("여", "yeo yuh"), ("추", "choo chu"),
    ("도", "do doh"), ("소", "so"), ("석", "seok suk"), ("선", "sun seon"), ("설", "seol sul"), ("마", "ma"), ("길", "gil kil"),
    ("연", "yeon yun"), ("위", "wi wee"), ("표", "pyo"), ("명", "myung myeong"), ("기", "ki gi"), ("반", "ban"), ("왕", "wang"),
    ("금", "keum geum"), ("옥", "ok"), ("육", "yook yuk"), ("인", "in"), ("맹", "maeng"), ("제", "je"), ("모", "mo"),
    ("탁", "tak"), ("국", "kook guk"), ("은", "eun"), ("편", "pyun pyeon"), ("용", "yong"), ("예", "ye"), ("경", "kyung gyeong"),
    ("봉", "bong"), ("사", "sa"), ("부", "boo bu"), ("태", "tae"), ("목", "mok"), ("형", "hyung hyeong"), ("두", "doo du"),
    ("동", "dong"), ("호", "ho"), ("승", "seung"), ("상", "sang"), ("시", "si"), ("단", "dan"),
];
const INIT: &[&str] = &["g k", "kk", "n", "d t", "tt", "r l", "m", "b p", "pp", "s", "ss", "", "j ch", "jj", "ch", "k", "t", "p", "h"];
const MED: &[&str] = &["a", "ae", "ya", "yae", "eo u o", "e", "yeo yu yo", "ye", "o oh", "wa", "wae", "oe oi", "yo", "u oo", "wo", "we",
                       "wi", "yu yoo", "eu u", "ui eui", "i ee y"];
const FIN: &[&str] = &["", "k g", "kk k", "k", "n", "n", "n", "t d", "l r", "k", "m", "l", "l", "l", "p", "l", "m", "p b", "p", "t s",
                       "t", "ng", "t", "t", "k", "t", "p", "t"];

// 성 표기 찾기
fn surname(ch: char) -> Option<&'static str> {
    let s = ch.to_string();
    SURNAMES.iter().find(|(k, _)| *k == s).map(|(_, v)| *v)
}

// 흔한 성의 로마자 — 이 글자만으로 된 계정(yoon · sung · lee)은 이름 일부로 보지 않는다
static COMMON_SURNAME_ROMAN: Lazy<HashSet<String>> = Lazy::new(|| {
    "김이박최정조강윤장임한오서신권황안송류유홍전고문양손배백허남노하곽성차주우구민나진지".chars()
        .flat_map(|c| surname(c).unwrap_or("").split(' ').filter(|x| !x.is_empty()).map(|x| x.to_string()).collect::<Vec<_>>())
        .collect()
});

thread_local! {
    // 이름 쌍마다 새로 만드는 정규식 — 파이썬 re 캐시처럼 한 번 만든 것은 다시 쓴다
    static RE_CACHE: RefCell<HashMap<String, Regex>> = RefCell::new(HashMap::new());
}

// 정규식 전체 일치(re.fullmatch) — 컴파일한 것은 캐시
fn fullmatch(pat: &str, s: &str) -> bool {
    RE_CACHE.with(|c| {
        let mut c = c.borrow_mut();
        if !c.contains_key(pat) {
            match Regex::new(&format!("^(?:{})$", pat)) {
                Ok(r) => {
                    c.insert(pat.to_string(), r);
                }
                Err(_) => return false,
            }
        }
        c[pat].is_match(s)
    })
}

// 표기 후보들 → 정규식 조각 ("a b" → (?:a|b))
fn alt(s: &str) -> String {
    if s.contains(' ') {
        format!("(?:{})", s.split(' ').map(regex::escape).collect::<Vec<_>>().join("|"))
    } else {
        regex::escape(s)
    }
}

// 한글 음절 하나 → 로마자 표기 후보 정규식
fn syl(ch: char) -> String {
    let code = ch as i64 - 0xAC00;
    if !(0..11172).contains(&code) {
        return regex::escape(&ch.to_string());
    }
    let (i, m, f) = ((code / 588) as usize, ((code % 588) / 28) as usize, (code % 28) as usize);
    format!("{}{}{}", alt(INIT[i]), alt(MED[m]), alt(FIN[f]))
}

static RE_KNAME24: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[가-힣]{2,4}$").unwrap());
static RE_TOKSEP: Lazy<Regex> = Lazy::new(|| Regex::new(r"[\s\-_.,]+").unwrap());
static RE_AZ: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[a-z]+$").unwrap());
static RE_NOT_AZ: Lazy<Regex> = Lazy::new(|| Regex::new(r"[^a-z]").unwrap());
static RE_NAME3: Lazy<FRegex> = Lazy::new(|| FRegex::new(r"(?<![가-힣])([가-힣]{3})(?![가-힣])").unwrap());

// 성 정규식 — 표에 있으면 그 표기들, 없으면 음절 표기
fn sur_re(first: char) -> String {
    match surname(first) {
        Some(s) => format!("(?:{})", s.split_whitespace().collect::<Vec<_>>().join("|")),
        None => syl(first),
    }
}

//------------------------------------------------------------------
// 한글 이름 ↔ 영문 · 계정 이름이 같은 사람으로 읽히는가 (②)
//=> 성은 표, 이름 음절은 표기 변형을 허락한다. 순서(성 앞 · 뒤)와 띄어쓰기 · 하이픈은 따지지 않는다.
//   "Cho Kang Lae" · "kang-lae cho" · "kim-sung-su" · "choiwoosun" 이 각각 조강래 · 조강래 · 김성수 · 최우선.
//
// -in: kname = 한글 이름(2~4자) / ename = 영문 · 계정(소문자)
//
// -out: true / false
// -out: error = 없음
//------------------------------------------------------------------
pub fn romanized_match(kname: &str, ename: &str) -> bool {
    if !RE_KNAME24.is_match(kname) {
        return false;
    }
    let low = pyfmt::lower(ename);
    let toks: Vec<&str> = RE_TOKSEP.split(&low).filter(|t| !t.is_empty()).collect();
    if toks.is_empty() || toks.iter().any(|t| !RE_AZ.is_match(t)) {
        return false;
    }
    let cs: Vec<char> = kname.chars().collect();
    let sr = sur_re(cs[0]);
    let given: String = cs[1..].iter().map(|c| syl(*c)).collect();
    // 성 + 이름(붙여 쓰기 · 띄어 쓰기 모두) 를 앞뒤 순서 둘 다 본다
    let flat = toks.concat();
    if fullmatch(&format!("{}{}", sr, given), &flat) || fullmatch(&format!("{}{}", given, sr), &flat) {
        // 한 덩어리 짧은 계정(kim)은 너무 흔해 받지 않는다
        return toks.len() >= 2 || flat.chars().count() >= 6;
    }
    false
}

// 음절 첫 소리 한 글자 정규식 — ㄱ=g/k, ㄹ=r/l, 받침 없는 ㅇ 은 모음 첫 글자
fn ini_re(ch: char) -> Option<String> {
    let code = ch as i64 - 0xAC00;
    if !(0..11172).contains(&code) {
        return None;
    }
    let ini = INIT[(code / 588) as usize];
    let mut firsts: BTreeSet<char> = ini.split(' ').filter(|x| !x.is_empty()).filter_map(|x| x.chars().next()).collect();
    if firsts.is_empty() {
        firsts = MED[((code % 588) / 28) as usize].split(' ').filter_map(|x| x.chars().next()).collect();
    }
    Some(format!("[{}]", firsts.iter().collect::<String>()))
}

// 이름(성 뺀) 음절들의 첫 소리 — 강래 → [gk][lr]
fn given_initials(kname: &str) -> Option<String> {
    let parts: Option<Vec<String>> = kname.chars().skip(1).map(ini_re).collect();
    parts.map(|p| p.concat())
}

//------------------------------------------------------------------
// 계정 ID 가 이름 로마자의 일부인가 — bong9 ↔ 고봉수(봉), kobong ↔ 고봉수(고봉), bongsu ↔ 고봉수(봉수)
//=> 계정 글자(숫자 · 기호 뺀 것)가 [성+이름 · 이름 · 성+이름 첫 음절 · 이름 첫 음절] 중 하나와 통째로 같으면 참.
//   성만 같은 것(kim · lee · yoon)은 너무 흔해 받지 않는다. 글자가 3자 미만이면 받지 않는다.
//
// -in: kname = 한글 이름 / letters = 계정 글자(소문자 영문만)
//
// -out: true / false
// -out: error = 없음
//------------------------------------------------------------------
pub fn partial_match(kname: &str, letters: &str) -> bool {
    if letters.chars().count() < 3 || !RE_KNAME24.is_match(kname) {
        return false;
    }
    // 흔한 성 표기만으로 된 계정(yoon · sung · lee)은 누구라도 될 수 있다 — 외자 이름(허성)에도 맞추지 않는다
    if COMMON_SURNAME_ROMAN.contains(letters) {
        return false;
    }
    let cs: Vec<char> = kname.chars().collect();
    let sr = sur_re(cs[0]);
    let given: String = cs[1..].iter().map(|c| syl(*c)).collect();
    let first = syl(cs[1]);
    let mut pats = vec![format!("{}{}", sr, given), format!("{}{}", given, sr), given.clone(),
                        format!("{}{}", sr, first), format!("{}{}", first, sr), first.clone()];
    // 회사 계정에 흔한 "이름 이니셜 + 성"(skchoi · dhkim · jhrho) · "성 + 이름 이니셜"(kimsj)
    if let Some(gi) = given_initials(kname) {
        pats.push(format!("{}{}", gi, sr));
        pats.push(format!("{}{}", sr, gi));
    }
    pats.iter().any(|p| fullmatch(p, letters))
}

//------------------------------------------------------------------
// 이니셜 일치 — ckl ↔ 조강래 (성 첫 글자 + 이름 음절 첫 소리, 표기 변형 허락)
//
// -in: kname = 한글 이름 / letters = 계정 글자
//
// -out: true / false
// -out: error = 없음
//------------------------------------------------------------------
pub fn initials_match(kname: &str, letters: &str) -> bool {
    let first = match kname.chars().next() {
        Some(c) => c,
        None => return false,
    };
    let sur = match surname(first) {
        Some(s) => s,
        None => return false,
    };
    if letters.chars().count() < 3 {
        return false;
    }
    let sf: BTreeSet<char> = sur.split_whitespace().filter_map(|x| x.chars().next()).collect();
    let sur_first = format!("[{}]", sf.iter().collect::<String>());
    match given_initials(kname) {
        Some(gi) if !gi.is_empty() => fullmatch(&format!("{}{}", sur_first, gi), letters),
        _ => false,
    }
}

// ── 사전 파일 ─────────────────────────────────────────────

//------------------------------------------------------------------
// 사용자 사전 (people.yaml)
// -필드: people   = [(대표 이름, [별칭…])] — 파일 순서
// -필드: rejected = [(별칭, 대표 이름)] — 다시 후보로 올리지 않는 짝
// -필드: me       = "나" 의 대표 이름
//------------------------------------------------------------------
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PeopleUser {
    pub people: Vec<(String, Vec<String>)>,
    pub rejected: Vec<(String, String)>,
    pub me: Option<String>,
}

impl PeopleUser {
    // 대표 이름의 별칭 목록(없으면 새로 — 파이썬 setdefault)
    fn entry(&mut self, name: &str) -> &mut Vec<String> {
        if let Some(i) = self.people.iter().position(|(k, _)| k == name) {
            return &mut self.people[i].1;
        }
        self.people.push((name.to_string(), vec![]));
        &mut self.people.last_mut().unwrap().1
    }

    //------------------------------------------------------------------
    // 파이썬 load_user() 결과와 같은 JSON 모양 — {people: {…}, rejected: [[…]], me: …}
    //------------------------------------------------------------------
    pub fn to_json(&self) -> Value {
        let mut p = Map::new();
        for (k, v) in &self.people {
            p.insert(k.clone(), json!(v));
        }
        json!({"people": Value::Object(p),
               "rejected": self.rejected.iter().map(|(a, b)| json!([a, b])).collect::<Vec<_>>(),
               "me": self.me})
    }
}

// yaml 목록(또는 글자 — 파이썬은 글자 하나씩) → 글자 목록
fn yaml_list(v: &serde_yaml::Value) -> Vec<String> {
    match v {
        serde_yaml::Value::Sequence(s) => s.iter().map(yaml_str).collect(),
        serde_yaml::Value::String(s) => s.chars().map(|c| c.to_string()).collect(),
        serde_yaml::Value::Mapping(m) => m.keys().map(yaml_str).collect(),
        _ => vec![],
    }
}

//------------------------------------------------------------------
// 사용자 사전 읽기 — {people: {대표: [별칭]}, rejected: [[별칭, 대표]], me: 대표}
//
// -in: root = 데이터 루트
//
// -out: PeopleUser (없으면 빈 모양)
// -out: error = 없음 (깨졌으면 빈 모양)
//------------------------------------------------------------------
pub fn load_user(root: &str) -> PeopleUser {
    let mut out = PeopleUser::default();
    let p = pyutil::join(root, "people.yaml");
    let d: serde_yaml::Value = match std::fs::read_to_string(&p).ok().and_then(|s| serde_yaml::from_str(&s).ok()) {
        Some(d) => d,
        None => return out,
    };
    let m = match d.as_mapping() {
        Some(m) => m,
        None => return out,
    };
    if let Some(pm) = m.get("people").and_then(|v| v.as_mapping()) {
        for (k, v) in pm {
            out.people.push((yaml_str(k), yaml_list(v)));
        }
    }
    if let Some(rs) = m.get("rejected").and_then(|v| v.as_sequence()) {
        for r in rs {
            let pair = yaml_list(r);
            if pair.len() == 2 && !pair[0].is_empty() && !pair[1].is_empty() {
                out.rejected.push((pair[0].clone(), pair[1].clone()));
            }
        }
    }
    out.me = match m.get("me") {
        None | Some(serde_yaml::Value::Null) => None,
        Some(v) => Some(yaml_str(v)),
    };
    out
}

// YAML 1.1 에서 글자가 아닌 값으로 읽히는 모양(PyYAML 암묵 해석) — 따옴표가 필요하다
static RE_YAML_SPECIAL: Lazy<Regex> = Lazy::new(|| Regex::new(concat!(
    r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF|y|Y|n|N|~|null|Null|NULL|<<|=",
    r"|[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?|[-+]?\.[0-9][0-9_]*(?:[eE][-+]?[0-9]+)?",
    r"|[-+]?0[bxo][0-9a-fA-F_]+|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)",
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}.*)$")).unwrap());

//------------------------------------------------------------------
// YAML 스칼라 하나 쓰기 — 그냥 쓰면 다르게 읽히는 글자만 작은따옴표로
//=> PyYAML safe_dump 와 같은 판단을 보수적으로 흉내 낸다(따옴표를 더 써도 두 판이 같은 글자로 읽는다).
//
// -in: s = 글자
//
// -out: YAML 글자
// -out: error = 없음
//------------------------------------------------------------------
fn yaml_scalar(s: &str) -> String {
    let first = s.chars().next();
    let need = s.is_empty()
        || first.map(|c| "-?:,[]{}#&*!|>'\"%@` ".contains(c)).unwrap_or(false)
        || s.ends_with(' ') || s.ends_with(':')
        || s.contains(": ") || s.contains(" #") || s.chars().any(|c| ",[]{}".contains(c) || (c as u32) < 0x20)
        || RE_YAML_SPECIAL.is_match(s);
    if need {
        format!("'{}'", s.replace('\'', "''"))
    } else {
        s.to_string()
    }
}

//------------------------------------------------------------------
// 사용자 사전 쓰기 — 머리 설명을 붙여 people.yaml 로
//=> 파이썬 yaml.safe_dump(default_flow_style=None) 모양: 글자만 든 목록은 [a, b], 나머지는 블록.
//   대표 이름은 정렬하고, 별칭은 정렬 · 중복 제거, 빈 대표는 뺀다.
//
// -in: root = 데이터 루트 / data = PeopleUser
//
// -out: Ok(파일 경로)
// -out: error = 쓰기 실패는 Err(io)
//------------------------------------------------------------------
pub fn save_user(root: &str, data: &PeopleUser) -> std::io::Result<String> {
    let p = pyutil::join(root, "people.yaml");
    let mut people: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (k, v) in &data.people {
        if !v.is_empty() {
            people.entry(k.clone()).or_default().extend(v.iter().cloned());
        }
    }
    let mut body = String::from(HEADER);
    if people.is_empty() {
        body.push_str("people: {}\n");
    } else {
        body.push_str("people:\n");
        for (k, v) in &people {
            let items: Vec<String> = v.iter().map(|a| yaml_scalar(a)).collect();
            body.push_str(&format!("  {}: [{}]\n", yaml_scalar(k), items.join(", ")));
        }
    }
    if data.rejected.is_empty() {
        body.push_str("rejected: []\n");
    } else {
        body.push_str("rejected:\n");
        for (a, b) in &data.rejected {
            body.push_str(&format!("- [{}, {}]\n", yaml_scalar(a), yaml_scalar(b)));
        }
    }
    if let Some(me) = data.me.as_ref().filter(|m| !m.is_empty()) {
        body.push_str(&format!("me: {}\n", yaml_scalar(me)));
    }
    // 파이썬 텍스트 모드 쓰기와 같게 줄 끝은 CRLF
    std::fs::write(&p, body.replace('\n', "\r\n").as_bytes())?;
    Ok(p)
}

//------------------------------------------------------------------
// 별칭 승인 · 직접 추가 — 대표 이름 아래에 별칭을 넣는다(다른 대표에 있던 같은 별칭은 옮긴다)
//
// -in: root / alias = 별칭(계정 · 영문) / name = 대표 이름
//
// -out: Ok(안내 글)
// -out: error = 쓰기 실패는 Err(io)
//------------------------------------------------------------------
pub fn approve(root: &str, alias: &str, name: &str) -> std::io::Result<String> {
    let alias = pyfmt::lower(pyutil::strip(alias));
    let name = pyutil::strip(name).to_string();
    if alias.is_empty() || name.is_empty() {
        return Ok("별칭과 대표 이름을 모두 적으세요".into());
    }
    let mut d = load_user(root);
    for (_, v) in d.people.iter_mut() {
        v.retain(|a| pyfmt::lower(a) != alias);
    }
    d.entry(&name).push(alias.clone());
    d.rejected.retain(|(a, b)| !(pyfmt::lower(a) == alias && *b == name));
    save_user(root, &d)?;
    Ok(format!("승인: {} → {}", alias, name))
}

//------------------------------------------------------------------
// 후보 무시 — 그 짝은 다시 후보로 올리지 않는다
//
// -in: root / alias = 별칭 / name = 대표 이름
//
// -out: Ok(안내 글)
// -out: error = 쓰기 실패는 Err(io)
//------------------------------------------------------------------
pub fn reject(root: &str, alias: &str, name: &str) -> std::io::Result<String> {
    let alias = pyfmt::lower(pyutil::strip(alias));
    let name = pyutil::strip(name).to_string();
    let mut d = load_user(root);
    if !d.rejected.iter().any(|(a, b)| *a == alias && *b == name) {
        d.rejected.push((alias.clone(), name.clone()));
    }
    let e = d.entry(&name);
    e.retain(|a| pyfmt::lower(a) != alias);
    save_user(root, &d)?;
    Ok(format!("무시: {} → {} (다시 제안하지 않음)", alias, name))
}

//------------------------------------------------------------------
// 승인한 별칭 빼기
//
// -in: root / alias = 별칭
//
// -out: Ok(안내 글)
// -out: error = 쓰기 실패는 Err(io)
//------------------------------------------------------------------
pub fn remove(root: &str, alias: &str) -> std::io::Result<String> {
    let alias = pyfmt::lower(pyutil::strip(alias));
    let mut d = load_user(root);
    let mut hit = false;
    for (_, v) in d.people.iter_mut() {
        if v.iter().any(|a| pyfmt::lower(a) == alias) {
            hit = true;
            v.retain(|a| pyfmt::lower(a) != alias);
        }
    }
    save_user(root, &d)?;
    Ok(if hit { format!("뺌: {}", alias) } else { format!("사전에 없는 별칭입니다: {}", alias) })
}

// HKCU 아래 글자 값 하나 읽기(없으면 None)
fn reg_string(subkey: &str, value: &str) -> Option<String> {
    use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_SZ};
    let wide = |s: &str| s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let (k, v) = (wide(subkey), wide(value));
    let mut size: u32 = 0;
    unsafe {
        if RegGetValueW(HKEY_CURRENT_USER, k.as_ptr(), v.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(), std::ptr::null_mut(), &mut size) != 0
            || size == 0 {
            return None;
        }
        let mut buf = vec![0u16; (size as usize).div_ceil(2)];
        if RegGetValueW(HKEY_CURRENT_USER, k.as_ptr(), v.as_ptr(), RRF_RT_REG_SZ, std::ptr::null_mut(),
                        buf.as_mut_ptr() as *mut std::ffi::c_void, &mut size) != 0 {
            return None;
        }
        let n = buf.iter().position(|c| *c == 0).unwrap_or(buf.len());
        Some(String::from_utf16_lossy(&buf[..n]))
    }
}

//------------------------------------------------------------------
// 이 PC 사용자의 계정 이름들 — Windows 계정명 · Office 사용자 이름 (§7-2 "나")
//
// -in: 없음
//
// -out: [이름, …] (대소문자 무시 중복 없음)
// -out: error = 없음 (레지스트리를 못 읽으면 Windows 계정명만)
//------------------------------------------------------------------
pub fn my_accounts() -> Vec<String> {
    let mut out = vec![];
    if let Ok(u) = std::env::var("USERNAME") {
        if !u.is_empty() {
            out.push(u);
        }
    }
    if let Some(v) = reg_string(r"Software\Microsoft\Office\Common\UserInfo", "UserName") {
        if !v.is_empty() {
            out.push(v);
        }
    }
    let mut seen = HashSet::new();
    out.into_iter().filter(|x| seen.insert(pyfmt::lower(x))).collect()
}

//------------------------------------------------------------------
// "나" 정하기 — 대표 이름을 me 로 적고, 이 PC 계정 이름들을 그 별칭으로 승인한다
//
// -in: root / name = 대표 이름
//
// -out: Ok(안내 글)
// -out: error = 쓰기 실패는 Err(io)
//------------------------------------------------------------------
pub fn set_me(root: &str, name: &str) -> std::io::Result<String> {
    let mut d = load_user(root);
    d.me = Some(pyutil::strip(name).to_string());
    save_user(root, &d)?;
    let mut added = vec![];
    let rules = load_rules(root);
    for a in my_accounts() {
        // 한글 이름이면 그 자체가 대표 이름 — 별칭으로 넣지 않는다
        if let Some((got, kind)) = norm::norm_person_str(&a, &rules, None) {
            if got != name && (kind == "account" || kind == "english") {
                approve(root, &got, name)?;
                added.push(got);
            }
        }
    }
    let tail = if added.is_empty() { String::new() } else { format!(" · 이 PC 계정 {} 를 별칭으로 승인", added.join(", ")) };
    Ok(format!("나 = {}{}", name, tail))
}

// ── 후보 만들기 ───────────────────────────────────────────

//------------------------------------------------------------------
// 별칭 후보 하나
// -필드: alias · name = 별칭 → 대표 이름 / score = 점수 / kind = 로마자|이름 일부|이니셜
// -필드: cooc · path = 같은 문서 · 파일명 단서 문서 수 / docs · name_docs = 각각의 문서 수 / examples = 예시 파일명
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Candidate {
    pub alias: String,
    pub name: String,
    pub score: i64,
    pub kind: &'static str,
    pub cooc: usize,
    pub path: usize,
    pub docs: usize,
    pub name_docs: usize,
    pub examples: Vec<String>,
}

impl Candidate {
    pub fn to_json(&self) -> Value {
        json!({"alias": self.alias, "name": self.name, "score": self.score, "kind": self.kind, "cooc": self.cooc,
               "path": self.path, "docs": self.docs, "name_docs": self.name_docs, "examples": self.examples})
    }
}

//------------------------------------------------------------------
// 별칭 후보 만들기 (§7-2)
//=> meta.sqlite 의 원본 작성자 · 저장자 값을 (별칭 없이) 정규화해 근거를 모은다.
//   상속 문서(회사 양식)의 만든 이는 쓰지 않는다 — 양식을 만든 사람과 실제 작성자가 섞인다.
//   이름 모양 근거(로마자 · 이름 일부 · 이니셜)가 있어야만 후보로 올리고, 동시 출현 · 파일명은
//   점수를 더하는 데만 쓴다.
//    이름 모양: 영문 이름 = 로마자 일치(5) / 계정 = 로마자 일치(5) · 이름 일부(3) · 이니셜(2)
//    점수 = 이름 모양 + 동시 출현 문서 × 2 + 파일명 단서 문서 + 그 이름의 문서 수가 많으면 +1
//    한 별칭이 이름 일부 · 이니셜로 여러 이름에 똑같이 맞고 다른 근거로 갈리지 않으면 모호해 올리지 않는다.
//   이미 승인했거나 무시한 짝은 뺀다. 결과는 people_candidates.json 에도 쓴다.
//
// -in: conn = meta.sqlite / root = 데이터 루트
//
// -out: Ok([Candidate]) (점수 순)
// -out: error = sqlite 오류는 Err
//------------------------------------------------------------------
pub fn suggest(conn: &Connection, root: &str) -> rusqlite::Result<Vec<Candidate>> {
    let rules = load_rules(root);
    let user = load_user(root);
    let approved: HashSet<String> = user.people.iter().flat_map(|(_, v)| v.iter().map(|a| pyfmt::lower(a))).collect();
    let rejected: HashSet<(String, String)> = user.rejected.iter().map(|(a, b)| (pyfmt::lower(a), b.clone())).collect();
    // (별칭, 이름) → (같은 문서들, 파일명 단서 문서들)
    let mut ev: HashMap<(String, String), (BTreeSet<String>, BTreeSet<String>)> = HashMap::new();
    let mut names: HashMap<String, usize> = HashMap::new();
    let mut others: BTreeMap<String, usize> = BTreeMap::new();
    let mut st = conn.prepare("SELECT doc_path, raw_json, inherited FROM doc_meta")?;
    let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<i64>>(2)?)))?;
    for row in rows {
        let (path, raw_json, inh) = row?;
        let raw: Value = raw_json.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(Value::Null);
        let doc = norm::obj(raw.get("doc"));
        let mut got = vec![];
        for field in ["author", "last_saved_by"] {
            if field == "author" && inh.unwrap_or(0) != 0 {
                continue;
            }
            if let Some(g) = norm::norm_person(doc.get(field), &rules, None) {
                got.push(g);
            }
        }
        let kor: BTreeSet<String> = got.iter().filter(|(_, k)| k == "korean").map(|(n, _)| n.clone()).collect();
        let oth: BTreeSet<String> = got.iter().filter(|(_, k)| k == "account" || k == "english").map(|(n, _)| n.clone()).collect();
        for n in &kor {
            *names.entry(n.clone()).or_default() += 1;
        }
        for o in &oth {
            *others.entry(o.clone()).or_default() += 1;
        }
        for o in &oth {
            for n in &kor {
                ev.entry((o.clone(), n.clone())).or_default().0.insert(path.clone());
            }
            // ③ 경로 단서 — 파일 이름 속 한글 이름(3자)
            let stem = pyutil::splitext(pyutil::basename(&path)).0.to_string();
            let found: BTreeSet<String> = RE_NAME3.captures_iter(&stem).filter_map(|c| c.ok())
                .filter_map(|c| c.get(1).map(|m| m.as_str().to_string())).collect();
            for n in found {
                ev.entry((o.clone(), n)).or_default().1.insert(path.clone());
            }
        }
    }
    // 사람으로 나온 한글 이름만 대표 후보다(파일명 속 세 글자는 사람 이름이 아닐 수도 있다)
    let mut known: Vec<&String> = names.keys().collect();
    known.sort();
    let empty = (BTreeSet::new(), BTreeSet::new());
    let mut res: Vec<Candidate> = vec![];
    for (o, &odocs) in &others {
        if approved.contains(o) {
            continue;
        }
        let is_acct = !o.contains(' ');
        let letters = RE_NOT_AZ.replace_all(o, "").to_string();
        let mut cands: Vec<Candidate> = vec![];
        for n in &known {
            if rejected.contains(&(o.clone(), (*n).clone())) {
                continue;
            }
            // 이름 모양 근거 — 이것이 없으면 후보가 아니다
            let (kind, shape) = if romanized_match(n, o) {
                ("로마자", 5)
            } else if is_acct && partial_match(n, &letters) {
                ("이름 일부", 3)
            } else if is_acct && initials_match(n, &letters) {
                ("이니셜", 2)
            } else {
                continue;
            };
            let e = ev.get(&(o.clone(), (*n).clone())).unwrap_or(&empty);
            let c = e.0.len();
            let p = e.1.difference(&e.0).count();
            let nd = *names.get(*n).unwrap_or(&0);
            let score = shape + c as i64 * 2 + p as i64 + if nd >= 10 { 1 } else { 0 };
            let all: BTreeSet<&String> = e.0.iter().chain(e.1.iter()).collect();
            let examples = all.into_iter().take(3).map(|x| pyutil::basename(x).to_string()).collect();
            cands.push(Candidate { alias: o.clone(), name: (*n).clone(), score, kind, cooc: c, path: p, docs: odocs,
                                   name_docs: nd, examples });
        }
        if cands.is_empty() {
            continue;
        }
        cands.sort_by(|a, b| b.score.cmp(&a.score));
        // 같은 점수로 여러 이름에 맞으면 모호하다 — 로마자 일치가 아니면 올리지 않는다
        if cands.len() > 1 && cands[0].score == cands[1].score && cands[0].kind != "로마자" {
            continue;
        }
        res.push(cands.swap_remove(0));
    }
    res.sort_by(|a, b| b.score.cmp(&a.score).then_with(|| a.alias.cmp(&b.alias)));
    let js = Value::Array(res.iter().map(|c| c.to_json()).collect());
    let _ = std::fs::write(pyutil::join(root, "people_candidates.json"), pyfmt::dumps(&js, Some(1)).replace('\n', "\r\n"));
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::meta::rules::load_rules;

    // 사람 별칭 사전 (§7-2 · 3단계) — 이름 모양 근거 · 승인 · 무시 · 나
    #[test]
    fn people_rules() {
        assert!(romanized_match("조강래", "cho kang lae"));
        assert!(romanized_match("조강래", "kang-lae cho"));
        assert!(romanized_match("김성수", "kim-sung-su"));
        assert!(!romanized_match("김영민", "youngho yoon"));       // 다른 사람(윤영호)
        assert!(partial_match("고봉수", "bong"));
        assert!(partial_match("최성근", "skchoi"));                // 이름 이니셜 + 성
        assert!(!partial_match("이윤석", "yoon"));                 // 흔한 성 표기만으로는 받지 않는다
        assert!(!partial_match("허성", "sung"));
        assert!(initials_match("조강래", "ckl"));
        assert!(initials_match("박규영", "pky"));

        let r = load_rules("");
        let root = std::env::temp_dir().join(format!("srmeta_people_{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let root = root.to_string_lossy().into_owned();
        approve(&root, "bong9", "고봉수").unwrap();
        let al = norm::load_aliases(&root);
        assert_eq!(norm::norm_person_str("bong9", &r, Some(&al)), Some(("고봉수".into(), "alias".into())));
        reject(&root, "sung", "허성").unwrap();
        let d = load_user(&root);
        assert_eq!(d.people, vec![("고봉수".to_string(), vec!["bong9".to_string()])]);
        assert!(d.rejected.contains(&("sung".to_string(), "허성".to_string())));
        remove(&root, "bong9").unwrap();
        assert_eq!(norm::norm_person_str("bong9", &r, Some(&norm::load_aliases(&root))), Some(("bong9".into(), "account".into())));
        // 따옴표가 필요한 값도 다시 같은 글자로 읽힌다
        approve(&root, "7963", "고봉수").unwrap();
        approve(&root, "yes", "고봉수").unwrap();
        let d = load_user(&root);
        assert_eq!(d.people, vec![("고봉수".to_string(), vec!["7963".to_string(), "yes".to_string()])]);
        let _ = std::fs::remove_dir_all(&root);
    }
}
