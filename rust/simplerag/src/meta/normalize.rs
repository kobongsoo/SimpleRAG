//------------------------------------------------------------------
// 메타데이터 정규화 (메타데이터 검색 설계서 §7 사람 · §8 날짜 · §9 제목 · §10 형식 · 출처 · 유형)
//=> extract 의 원본 값(RawMeta)을 검색에 쓸 값(DocMeta)으로 바꾼다. Python meta/normalize.py 를
//   그대로 옮겼다 — 같은 raw 에서 같은 정규화 값이 나와야 두 판이 meta.sqlite 를 함께 쓸 수 있다.
//   속성 값은 지저분하다(실측: 작성자 16% 쓰레기 · 19% 템플릿 상속 · 쓸 만한 제목 29%).
//   파일을 다시 읽지 않으므로 규칙을 바꾸면 meta rebuild 한 번으로 모두 다시 계산된다.
//
//   Python re 와 다른 점을 메우는 곳
//    · 뒤돌아보기((?<=…) · (?<!…))와 앞보기((?=…) · (?!…))는 fancy_regex 로
//    · 제목 비교 키의 낱말 글자는 파이썬 \w(isalnum)와 같게 pyfmt::is_word 로
//    · 공백 판정 · strip · split 은 pyutil(파이썬 isspace 규칙)로
//------------------------------------------------------------------

use std::collections::{HashMap, HashSet};

use fancy_regex::Regex as FRegex;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{Map, Value};

use super::pyfmt::{self, py_str, truthy};
use super::rules::Rules;
use crate::pyutil;

// 1980-01-01 UTC — 이보다 이른 날짜는 이상값
const MIN_TS: i64 = 315_532_800;
const KST: i64 = 9 * 3600;

static HANGUL: Lazy<Regex> = Lazy::new(|| Regex::new(r"[가-힣]").unwrap());

// 한 번만 컴파일하는 정규식 묶음
macro_rules! re {
    ($name:ident, $pat:expr) => {
        static $name: Lazy<Regex> = Lazy::new(|| Regex::new($pat).unwrap());
    };
}
macro_rules! fre {
    ($name:ident, $pat:expr) => {
        static $name: Lazy<FRegex> = Lazy::new(|| FRegex::new($pat).unwrap());
    };
}

re!(RE_HEX, r"^<([0-9A-Fa-f]{4,})>$");
re!(RE_PAREN, r"\s*[\(\[（][^)\]）]*[\)\]）]\s*");
re!(RE_NUMLIKE, r"^[\d\s\-_.]+$");
re!(RE_ONE_LATIN, r"^[A-Za-z]$");
re!(RE_KNAME_REV, r"^([가-힣]{1,3})\s+([가-힣])$");
re!(RE_WS, r"\s+");
re!(RE_KNAME, r"^[가-힣]{2,5}$");
re!(RE_ACCOUNT, r"^[A-Za-z0-9._\-]+$");
re!(RE_ENGLISH, r"^[A-Za-z][A-Za-z .,'\-]+$");

//------------------------------------------------------------------
// 사람 한 명(doc_people 한 줄)
// -필드: name = 대표 이름 / role = author|editor|template / raw = 원본 값 글자
// -필드: source = "<kind>.<칸>" / conf = 신뢰도
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub struct Person {
    pub name: String,
    pub role: String,
    pub raw: String,
    pub source: String,
    pub conf: f64,
}

// ── 사람 (§7) ─────────────────────────────────────────────

//------------------------------------------------------------------
// 사람 값 하나 정규화
//=> 원본 작성자 칸 값을 "대표 이름"으로 바꾼다. 사람이 아니면 None.
//    1) 정리 — NUL · 앞뒤 공백, 괄호 안 소속, 직함, 이메일 도메인(앞부분은 계정 ID)
//    2) 쓰레기 — 목록 일치 · 한 글자 영문 · 숫자만 · 깨진 문자열(라틴 확장 30% 넘음)
//    3) 한글 이름 — "봉수 고" → 고봉수, "고 봉수" → 고봉수, 그 밖은 공백만 뺀다
//    4) 영문 — 소문자 · 공백 하나로. 공백 없는 영숫자는 계정 ID
//    5) 별칭 사전 — bong9 → 고봉수
//
// -in: value   = 원본 값(글자가 아니면 None)
// -in: rules   = load_rules() 결과
// -in: aliases = {별칭 소문자: 대표 이름} (people.yaml, 없으면 None)
//
// -out: Some((이름, 종류)) — 종류 = korean|english|account|alias. 사람이 아니면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn norm_person(value: Option<&Value>, rules: &Rules, aliases: Option<&HashMap<String, String>>) -> Option<(String, String)> {
    match value {
        Some(Value::String(s)) => norm_person_str(s, rules, aliases),
        _ => None,
    }
}

// 글자 값 정규화(norm_person 본체)
pub fn norm_person_str(value: &str, rules: &Rules, aliases: Option<&HashMap<String, String>>) -> Option<(String, String)> {
    let s0 = value.split('\0').next().unwrap_or("");
    let s = pyutil::strip(s0);
    // 여러 사람을 ; 로 이어 쓴 칸 — 사람으로 읽히는 첫 값을 쓴다
    if s.contains(';') {
        for part in s.split(';') {
            if let Some(got) = norm_person_str(part, rules, aliases) {
                return Some(got);
            }
        }
        return None;
    }
    let s = unmangle(s);
    if s.is_empty() {
        return None;
    }
    // 괄호 안 소속 · 메모(고봉수(품질팀)) 를 뗀다
    let s = RE_PAREN.replace_all(&s, " ").to_string();
    let mut s = pyutil::strip(&s).to_string();
    // 이메일은 앞부분만(계정 ID 로 본다)
    if s.contains('@') {
        s = pyutil::strip(s.split('@').next().unwrap_or("")).to_string();
    }
    if s.is_empty() {
        return None;
    }
    let low = pyfmt::lower(&s);
    if rules.junk_people.iter().any(|j| *j == low) || rules.re_junk_people.iter().any(|p| p.is_match(&low).unwrap_or(false)) {
        return None;
    }
    // 깨진 문자열 — 라틴 확장(À~ɏ) 비율이 30% 넘으면 복구할 수 없는 모지바케다
    let n = s.chars().count();
    let ext = s.chars().filter(|c| ('\u{00c0}'..='\u{024f}').contains(c)).count();
    if ext > 0 && ext as f64 / n as f64 > 0.3 {
        return None;
    }
    if RE_NUMLIKE.is_match(&s) || RE_ONE_LATIN.is_match(&s) {
        return None;
    }
    if HANGUL.is_match(&s) {
        // 부서 낱말("정보화전략팀")을 뗀다 — 이름 뒤에 소속을 붙여 쓴 칸
        let words: Vec<&str> = pyutil::split_ws(&s).into_iter().filter(|w| {
            !rules.org_suffix.iter().any(|o| w.ends_with(o.as_str()) && w.chars().count() > o.chars().count() + 1)
        }).collect();
        if !words.is_empty() {
            s = words.join(" ");
        }
        // 직함을 뗀다(앞뒤 어디든, 공백으로 떨어져 있거나 붙어 있거나)
        for (tail, head) in &rules.re_titles {
            s = tail.replace_all(&s, "").to_string();
            s = head.replace_all(&s, "").to_string();
        }
        let s = pyutil::strip(&s).to_string();
        let name = match RE_KNAME_REV.captures(&s) {
            Some(m) => format!("{}{}", &m[2], &m[1]),   // "봉수 고" → 고봉수
            None => RE_WS.replace_all(&s, "").to_string(),   // "고 봉수" · "고봉수" → 고봉수
        };
        if !RE_KNAME.is_match(&name) {
            // 한글이 섞였지만 이름 모양이 아니다(회사명 · 문장) — 사람으로 보지 않는다
            return None;
        }
        if rules.junk_people.iter().any(|j| *j == name) {
            return None;
        }
        return Some(alias(&name, "korean", aliases));
    }
    // 영문 · 계정 ID
    if RE_ACCOUNT.is_match(&s) {
        return Some(alias(&low, "account", aliases));
    }
    // "Han, Hyun Bae" 처럼 성 뒤에 쉼표 — 쉼표를 공백으로(낱말 순서는 별칭에서 무시한다)
    if RE_ENGLISH.is_match(&s) && s.matches(',').count() <= 1 {
        let name = RE_WS.replace_all(&low.replace(',', " "), " ").to_string();
        let name = pyutil::strip(&name).to_string();
        if rules.junk_people.iter().any(|j| *j == name) {
            return None;
        }
        return Some(alias(&name, "english", aliases));
    }
    None
}

// cp1252 의 0x80~0x9F 자리 글자(파이썬 cp1252 가 정의한 것만) — (글자, 바이트)
const CP1252_HIGH: &[(char, u8)] = &[
    ('€', 0x80), ('‚', 0x82), ('ƒ', 0x83), ('„', 0x84), ('…', 0x85), ('†', 0x86), ('‡', 0x87), ('ˆ', 0x88),
    ('‰', 0x89), ('Š', 0x8A), ('‹', 0x8B), ('Œ', 0x8C), ('Ž', 0x8E), ('‘', 0x91), ('’', 0x92), ('“', 0x93),
    ('”', 0x94), ('•', 0x95), ('–', 0x96), ('—', 0x97), ('˜', 0x98), ('™', 0x99), ('š', 0x9A), ('›', 0x9B),
    ('œ', 0x9C), ('ž', 0x9E), ('Ÿ', 0x9F),
];
const CP1252_CHARS: &str = "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ";

// 파이썬 s.encode("latin-1") — U+00FF 넘는 글자가 있으면 None
fn enc_latin1(s: &str) -> Option<Vec<u8>> {
    s.chars().map(|c| if (c as u32) <= 0xFF { Some(c as u8) } else { None }).collect()
}

// 파이썬 s.encode("cp1252") — 0x80~0x9F 의 정의 안 된 자리(0x81 · 0x8D · 0x8F · 0x90 · 0x9D)는 실패
fn enc_cp1252(s: &str) -> Option<Vec<u8>> {
    s.chars().map(|c| {
        let u = c as u32;
        if u < 0x80 || (0xA0..=0xFF).contains(&u) {
            Some(u as u8)
        } else {
            CP1252_HIGH.iter().find(|(ch, _)| *ch == c).map(|(_, b)| *b)
        }
    }).collect()
}

//------------------------------------------------------------------
// 깨진 글자 되살리기
//=> 실측에서 본 두 가지를 되돌린다(되돌린 결과에 한글이 있을 때만 받아들인다).
//    1) <C0AFBFACC1A4> — CP949 바이트를 hex 로 적은 값(PDF)
//    2) °ø·æ · ê³€ë´›… — CP949 / UTF-8 바이트를 Latin-1 · CP1252 로 잘못 읽은 값
//   못 되돌리면 원래 글자를 그대로 돌려준다(뒤 단계가 깨진 값으로 버린다).
//
// -in: s = 원본 글자
//
// -out: 되살린 글자(또는 원래 글자)
// -out: error = 없음
//------------------------------------------------------------------
pub fn unmangle(s: &str) -> String {
    if let Some(m) = RE_HEX.captures(s) {
        let hx = &m[1];
        if hx.len() % 2 != 0 {
            return s.to_string();
        }
        let bytes: Option<Vec<u8>> = (0..hx.len()).step_by(2).map(|i| u8::from_str_radix(&hx[i..i + 2], 16).ok()).collect();
        return match bytes.and_then(|b| pyfmt::decode_cp949(&b)) {
            Some(t) => {
                let t = pyutil::strip(&t).to_string();
                if HANGUL.is_match(&t) { t } else { s.to_string() }
            }
            None => s.to_string(),
        };
    }
    let suspicious = s.chars().any(|c| ('\u{0080}'..='\u{00ff}').contains(&c) || CP1252_CHARS.contains(c));
    if !HANGUL.is_match(s) && suspicious {
        type Enc = fn(&str) -> Option<Vec<u8>>;
        type Dec = fn(&[u8]) -> Option<String>;
        let tries: [(Enc, Dec); 3] = [(enc_latin1, pyfmt::decode_cp949), (enc_cp1252, pyfmt::decode_utf8), (enc_cp1252, pyfmt::decode_cp949)];
        for (enc, dec) in tries {
            let t = match enc(s).and_then(|b| dec(&b)) {
                Some(t) => pyutil::strip(&t).to_string(),
                None => continue,
            };
            // 흔한 한글 2,350자(EUC-KR) 밖의 음절이 나오면 잘못 되살린 것이다(실측: ê³€ë´›… → 곀봛싟)
            if HANGUL.is_match(&t) && !t.chars().any(|c| ('À'..='ɏ').contains(&c)) && common_hangul(&t) {
                return t;
            }
        }
    }
    s.to_string()
}

//------------------------------------------------------------------
// 한글 음절이 모두 흔한 2,350자(KS X 1001 완성형)에 드는가
//=> 파이썬 euc-kr 은 드문 음절을 8바이트 조합형으로 적어 주므로 2바이트인지로 본다.
//   encoding_rs 의 EUC-KR 은 확장 완성형(UHC)이라 모든 음절을 2바이트로 적는다 —
//   그래서 완성형 영역(첫 바이트 0xB0~0xC8, 둘째 0xA1~0xFE)인지로 판정한다.
//------------------------------------------------------------------
fn common_hangul(t: &str) -> bool {
    t.chars().filter(|c| ('가'..='힣').contains(c)).all(|c| {
        let mut buf = [0u8; 4];
        let (b, _, bad) = encoding_rs::EUC_KR.encode(c.encode_utf8(&mut buf));
        !bad && b.len() == 2 && (0xB0..=0xC8).contains(&b[0]) && b[1] >= 0xA1
    })
}

// 별칭 사전 적용 — 영문 이름은 낱말 순서가 달라도 같은 사람(youngho yoon = yoon youngho)
fn alias(name: &str, kind: &str, aliases: Option<&HashMap<String, String>>) -> (String, String) {
    if let Some(al) = aliases {
        if !al.is_empty() {
            let key = pyfmt::lower(name);
            if let Some(c) = al.get(&key) {
                return (c.clone(), "alias".into());
            }
            if kind == "english" {
                let mut ws = pyutil::split_ws(&key);
                ws.sort();
                if let Some(c) = al.get(&ws.join(" ")) {
                    return (c.clone(), "alias".into());
                }
            }
        }
    }
    (name.to_string(), kind.to_string())
}

//------------------------------------------------------------------
// 관련인 목록 (§4 #9 · §7-1 ⑥)
//=> 만든 이 = 작성, 마지막 저장자 = 수정. 템플릿 상속 문서의 만든 이는 원본양식(신뢰도 0.3).
//   같은 사람이 두 칸에 있으면 높은 역할 하나만 남긴다.
//
// -in: doc = raw["doc"] / kind = raw["kind"] / rules · aliases / inherited = 템플릿 상속 문서인가
//
// -out: [Person]
// -out: error = 없음
//------------------------------------------------------------------
pub fn people_of(doc: &Map<String, Value>, kind: &str, rules: &Rules, aliases: Option<&HashMap<String, String>>, inherited: bool) -> Vec<Person> {
    let mut out: Vec<Person> = vec![];
    for (field, role) in [("author", "author"), ("last_saved_by", "editor")] {
        let raw = doc.get(field);
        let (name, how) = match norm_person(raw, rules, aliases) {
            Some(g) => g,
            None => continue,
        };
        // 별칭이 없는 계정 ID 는 덜 믿는다
        let mut conf = if matches!(how.as_str(), "korean" | "alias" | "english") { 0.9 } else { 0.6 };
        let mut r = role;
        if role == "author" && inherited {
            r = "template";
            conf = 0.3;
        }
        out.push(Person { name, role: r.into(), raw: py_str(raw.unwrap()), source: format!("{}.{}", kind, field), conf });
    }
    // 같은 이름이면 높은 역할 하나만(작성 > 수정 > 원본양식) — 처음 나온 이름 순서를 지킨다
    let rank = |r: &str| match r { "author" => 3, "editor" => 2, _ => 1 };
    let mut best: Vec<Person> = vec![];
    for p in out {
        match best.iter_mut().find(|b| b.name == p.name) {
            Some(cur) => {
                if rank(&p.role) > rank(&cur.role) {
                    *cur = p;
                }
            }
            None => best.push(p),
        }
    }
    best
}

// ── 날짜 (§8) ─────────────────────────────────────────────

const MONTHS: [&str; 12] = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

re!(RE_PDF_DATE, r"^D:([0-9]{4})([0-9]{2})?([0-9]{2})?([0-9]{2})?([0-9]{2})?([0-9]{2})?\s*([Zz+\-])?\s*([0-9]{2})?'?([0-9]{2})?");
re!(RE_KO_DATE, r"^([0-9]{4})년\s*([0-9]{1,2})월\s*([0-9]{1,2})일(?:\s*\S+요일)?(?:\s*(오전|오후)\s*([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?)?");
re!(RE_WORDS, r"[a-z]+");
re!(RE_YEAR, r"\b(19|20)\d{2}\b");
re!(RE_HMS, r"\d{1,2}:\d{2}(:\d{2})?");
fre!(RE_DAY, r"\b(\d{1,2})\b(?!:)");
re!(RE_TM, r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?");

// (연월일시분초, 시간대 초) → 1970 기준 마이크로초 / 파이썬 datetime 이 거절하는 값이면 None
fn to_us(y: i64, mo: i64, d: i64, h: i64, mi: i64, s: i64, us: i64, off: i64) -> Option<i64> {
    if !pyfmt::valid_dt(y, mo, d, h, mi, s) {
        return None;
    }
    let secs = pyfmt::days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s - off;
    Some(secs * 1_000_000 + us)
}

// 숫자 글자 → 정수(빈 칸이면 기본값)
fn num(m: Option<regex::Match>, dflt: i64) -> i64 {
    m.and_then(|x| x.as_str().parse().ok()).unwrap_or(dflt)
}

//------------------------------------------------------------------
// 날짜 문자열 → UTC epoch 초
//=> 실측에서 본 형식을 모두 받는다.
//    W3CDTF / ISO(OOXML · HWPX · OLE · 파일 시스템) — 시간대 없으면 UTC(OLE FILETIME 은 UTC)
//    PDF D:20100823131638+09'00' — 오프셋 적용, 없으면 KST
//    PDF 자유 형식 "Mon Jan 12 08:51:48 2004" · "March 15, 2024 14:09"
//    HWP "2005년 10월 7일 금요일 오전 10:28:32" — KST
//   1980년 이전 · 내일 이후는 버린다(1900-12-31 같은 이상값).
//
// -in: s   = 날짜 문자열(글자가 아니면 None)
// -in: now = 기준 시각(epoch, 미래 판정용 — None 이면 지금)
//
// -out: Some(epoch 초) / 못 읽거나 이상하면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn parse_date(v: Option<&Value>, now: Option<i64>) -> Option<i64> {
    match v {
        Some(Value::String(s)) => parse_date_str(s, now),
        _ => None,
    }
}

// 글자 날짜 해석(parse_date 본체)
pub fn parse_date_str(s: &str, now: Option<i64>) -> Option<i64> {
    let s = pyutil::strip(s);
    if s.is_empty() {
        return None;
    }
    let mut us: Option<i64> = None;
    if let Some(m) = RE_PDF_DATE.captures(s) {
        let (y, mo, d) = (num(m.get(1), 0), num(m.get(2), 1), num(m.get(3), 1));
        let (h, mi, se) = (num(m.get(4), 0), num(m.get(5), 0), num(m.get(6), 0));
        let sign = m.get(7).map(|x| x.as_str()).unwrap_or("");
        let off = if (sign == "+" || sign == "-") && m.get(8).is_some() {
            let o = num(m.get(8), 0) * 3600 + num(m.get(9), 0) * 60;
            // 파이썬 timezone() 은 24시간 이상 오프셋을 받지 않는다
            if o >= 86400 {
                return None;
            }
            if sign == "+" { o } else { -o }
        } else if sign == "Z" || sign == "z" {
            0
        } else {
            KST        // 오프셋이 없는 PDF 날짜는 한국 시간으로 본다
        };
        us = Some(to_us(y, mo, d, h, mi, se, 0, off)?);
    }
    if us.is_none() {
        us = fromisoformat(&s.replace('Z', "+00:00"));
    }
    if us.is_none() {
        if let Some(m) = RE_KO_DATE.captures(s) {
            let mut h = num(m.get(5), 0);
            let ap = m.get(4).map(|x| x.as_str()).unwrap_or("");
            if ap == "오후" && h < 12 {
                h += 12;
            }
            if ap == "오전" && h == 12 {
                h = 0;
            }
            us = Some(to_us(num(m.get(1), 0), num(m.get(2), 0), num(m.get(3), 0), h, num(m.get(6), 0), num(m.get(7), 0), 0, KST)?);
        }
    }
    if us.is_none() {
        us = free_date(s);
    }
    let us = us?;
    let now = now.unwrap_or_else(|| pyutil::now_ts() as i64);
    // 파이썬은 실수 초로 비교한다 — 마이크로초 단위로 같은 비교
    if us < MIN_TS * 1_000_000 || (us as f64) / 1e6 > now as f64 + 86400.0 {
        return None;
    }
    Some(us.div_euclid(1_000_000))
}

// 숫자 n 자리(ASCII) 읽기 — CPython parse_digits
fn digits(b: &[u8], p: usize, n: usize) -> Option<i64> {
    if p + n > b.len() {
        return None;
    }
    let mut v = 0i64;
    for &c in &b[p..p + n] {
        if !c.is_ascii_digit() {
            return None;
        }
        v = v * 10 + (c - b'0') as i64;
    }
    Some(v)
}

// 글자 끝이면 0(NUL) — CPython 은 NUL 로 끝나는 버퍼를 읽는다
fn at(b: &[u8], p: usize) -> u8 {
    *b.get(p).unwrap_or(&0)
}

//------------------------------------------------------------------
// [HH[:?MM[:?SS[{.,}fff[fff]]]]] 읽기 — CPython parse_hh_mm_ss_ff
//
// -in: b = 바이트 / p0 · end = 읽을 범위
//
// -out: Ok((시, 분, 초, 마이크로초, 뒤에 남은 것이 있나)) / 형식이 틀리면 Err
// -out: error = 없음
//------------------------------------------------------------------
fn hh_mm_ss_ff(b: &[u8], p0: usize, end: usize) -> Result<(i64, i64, i64, i64, bool), ()> {
    let mut p = p0;
    let mut vals = [0i64; 3];
    let mut has_sep = true;
    for i in 0..3 {
        vals[i] = digits(b, p, 2).ok_or(())?;
        p += 2;
        let c = at(b, p);
        p += 1;
        if i == 0 {
            has_sep = c == b':';
        }
        if p >= end {
            return Ok((vals[0], vals[1], vals[2], 0, c != 0));
        } else if has_sep && c == b':' {
            continue;
        } else if c == b'.' || c == b',' {
            break;
        } else if !has_sep {
            p -= 1;
        } else {
            return Err(());
        }
    }
    // 소수 초 — 6자리까지 읽고 나머지 숫자는 버린다
    let remains = end.saturating_sub(p);
    let to_parse = remains.min(6);
    if to_parse == 0 {
        return Err(());
    }
    let mut us = digits(b, p, to_parse).ok_or(())?;
    p += to_parse;
    us *= [100000, 10000, 1000, 100, 10, 1][to_parse - 1];
    while at(b, p).is_ascii_digit() {
        p += 1;
    }
    Ok((vals[0], vals[1], vals[2], us, at(b, p) != 0))
}

//------------------------------------------------------------------
// 파이썬 3.12 datetime.fromisoformat(s) → (시간대 없으면 UTC 로 본) 1970 기준 마이크로초
//=> CPython _datetimemodule.c 의 순서를 그대로 따른다(날짜 YYYY-MM-DD · YYYYMMDD, 구분자는 아무 글자,
//   시각 HH[:MM[:SS[.f…]]], 시간대 Z · ±HH[:MM[:SS[.f]]]). 주(W) · 서수일 형식은 받지 않는다.
//
// -in: s = 글자
//
// -out: Some(마이크로초) / 형식이 아니면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn fromisoformat(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    let len = b.len();
    if len < 7 {
        return None;
    }
    // 날짜 끝(구분자 자리) — 주 표시(W)는 받지 않는다
    let sep = if len == 7 {
        7
    } else if b[4] == b'-' {
        if b[5] == b'W' { return None; }
        10
    } else {
        if b[4] == b'W' { return None; }
        8
    };
    // 날짜
    let year = digits(b, 0, 4)?;
    let mut p = 4;
    let uses_sep = at(b, p) == b'-';
    if uses_sep {
        p += 1;
    }
    let month = digits(b, p, 2)?;
    p += 2;
    if uses_sep {
        if at(b, p) != b'-' {
            return None;
        }
        p += 1;
    }
    let day = digits(b, p, 2)?;
    let (mut h, mut mi, mut se, mut us, mut off) = (0, 0, 0, 0, 0i64);
    if len > sep {
        // 시각 — 시간대 기호(Z + -)가 나오는 곳까지
        let t0 = sep + 1;
        let mut tz = t0;
        loop {
            let c = at(b, tz);
            if c == b'Z' || c == b'+' || c == b'-' {
                break;
            }
            tz += 1;
            if tz >= len {
                break;
            }
        }
        let (hh, mm, ss, ff, rest) = hh_mm_ss_ff(b, t0, tz).ok()?;
        h = hh;
        mi = mm;
        se = ss;
        us = ff;
        if tz >= len {
            if rest {
                return None;
            }
        } else if at(b, tz) == b'Z' {
            if at(b, tz + 1) != 0 {
                return None;
            }
        } else {
            let sign = if at(b, tz) == b'-' { -1 } else { 1 };
            let (th, tm, ts, tus, rest2) = hh_mm_ss_ff(b, tz + 1, len).ok()?;
            if rest2 {
                return None;
            }
            let o = th * 3600 + tm * 60 + ts;
            // timezone() 은 24시간 미만만 받는다
            if o * 1_000_000 + tus >= 86_400_000_000 {
                return None;
            }
            off = sign * o;
            // 시간대 소수 초는 마이크로초로 반영
            us -= sign * tus;
        }
    }
    let base = to_us(year, month, day, h, mi, se, 0, off)?;
    Some(base + us)
}

//------------------------------------------------------------------
// 영문 자유 형식 — "Mon Jan 12 08:51:48 2004" · "March 15, 2024 14:09" (KST 로 본다)
//
// -in: s = 글자
//
// -out: Some(1970 기준 마이크로초) / 못 읽으면 None
// -out: error = 없음
//------------------------------------------------------------------
fn free_date(s: &str) -> Option<i64> {
    let low = pyfmt::lower(s);
    let mut mon = None;
    for w in RE_WORDS.find_iter(&low) {
        let w = w.as_str();
        if w.len() >= 3 {
            if let Some(i) = MONTHS.iter().position(|m| *m == &w[..3]) {
                mon = Some(i as i64 + 1);
                break;
            }
        }
    }
    let year = RE_YEAR.find(&low)?;
    let mon = mon?;
    let ystr = year.as_str().to_string();
    let rest = low.replace(&ystr, " ");
    let no_time = RE_HMS.replace_all(&rest, " ").to_string();
    let day = RE_DAY.captures(&no_time).ok().flatten().and_then(|c| c.get(1).map(|m| m.as_str().parse::<i64>().unwrap_or(1)));
    let tm = RE_TM.captures(&rest);
    let (h, mi, se) = match &tm {
        Some(t) => (num(t.get(1), 0), num(t.get(2), 0), num(t.get(3), 0)),
        None => (0, 0, 0),
    };
    to_us(ystr.parse().ok()?, mon, day.unwrap_or(1), h, mi, se, 0, KST)
}

//------------------------------------------------------------------
// 대표 날짜 세 가지 (§8-3)
//=> 수정일(대표) = 문서 내부 수정일 → 없으면 파일 시스템 수정일
//   작성일 = 문서 내부 생성일(상속이면 신뢰도 0.3). 수정일보다 1시간 넘게 늦으면 버린다
//   PC 에 들어온 날 = 파일 시스템 생성일(복사 · 내려받은 시점)
//   HWP 는 생성일이 없을 때 날짜 문자열(PIDSI 20)을 보조로 쓴다.
//
// -in: raw = RawMeta / inherited = 템플릿 상속 문서인가
//
// -out: (modified_ts, created_ts, created_conf, added_ts)
// -out: error = 없음
//------------------------------------------------------------------
pub fn dates_of(raw: &Value, inherited: bool) -> (Option<i64>, Option<i64>, Option<f64>, Option<i64>) {
    let doc = obj(raw.get("doc"));
    let fs = obj(raw.get("fs"));
    let mod_ = parse_date(doc.get("modified"), None).or_else(|| parse_date(fs.get("modified"), None));
    let mut cre = parse_date(doc.get("created"), None).or_else(|| parse_date(doc.get("date_str"), None));
    if let (Some(c), Some(m)) = (cre, mod_) {
        if c > m + 3600 {
            cre = None;        // 생성일이 수정일보다 늦다 — 생성일 쪽이 틀렸다
        }
    }
    let conf = cre.map(|_| if inherited { 0.3 } else { 0.9 });
    (mod_, cre, conf, parse_date(fs.get("created"), None))
}

// raw 안의 dict 칸(없거나 dict 가 아니면 빈 것) — 파이썬 raw.get("doc") or {}
static EMPTY: Lazy<Map<String, Value>> = Lazy::new(Map::new);
pub fn obj(v: Option<&Value>) -> &Map<String, Value> {
    v.and_then(|x| x.as_object()).unwrap_or(&EMPTY)
}

// ── 제목 (§9) ─────────────────────────────────────────────

re!(RE_PAREN_NUM, r"\s*[\(\[]\d+[\)\]]\s*$");
re!(RE_COPY, r"(?i)\s*-\s*(복사본|사본|copy)(\s*\(\d+\))?$");
re!(RE_DATE8, r"[\s_\-]*[\(\[]?(19|20)\d{2}[.\-_]?\d{2}[.\-_]?\d{2}[\)\]]?");
fre!(RE_DATE_DOT, r"[\s_\-]+\d{2}\.\d{2}\.\d{2}(?=$|[\s_\-])");
fre!(RE_DATE67, r"[\s_\-]+\d{6,7}(?=$|[\s_\-])");
fre!(RE_VER, r"(?i)[\s_\-]*(?<![A-Za-z])(v|ver\.?)\s?\d+(\.\d+)*(?![\w.])");
re!(RE_FINAL, r"(?i)[\s_\-]*(최종본|최종|수정본|수정|final)$");
re!(RE_LEADNUM, r"^(\d+[._\-])+\s*");
re!(RE_UNDERS, r"[_]+");

//------------------------------------------------------------------
// 파일명 정리본 (§9-1 ③) — 앞 번호 · 날짜 · 버전 · 확장자를 떼고 _ 를 공백으로
//=> "25.출장여비규정_16.04.01.pdf" → "출장여비규정", "07_ISMS-GU-005-자산관리지침.pdf" → "ISMS-GU-005-자산관리지침"
//
// -in: name     = 파일 이름
// -in: versions = true 면 버전(v2.0 · 최종)까지 뗀다 — 버전 그룹 키용. 제목 후보는 false
//                 (제품 버전 "M드라이브 V9" 는 제목의 일부라서)
//
// -out: 정리한 이름(다 지워지면 확장자만 뗀 이름)
// -out: error = 없음
//------------------------------------------------------------------
pub fn clean_filename(name: &str, versions: bool) -> String {
    let mut base = pyutil::splitext(name).0.to_string();
    // 확장자가 겹친 이름(a.pdf.pdf)
    loop {
        let (stem, ext) = pyutil::splitext(&base);
        if [".pdf", ".doc", ".docx", ".hwp", ".xls", ".xlsx", ".ppt", ".pptx"].contains(&ext.to_lowercase().as_str()) {
            base = stem.to_string();
        } else {
            break;
        }
    }
    let mut s = base.clone();
    // 사본 표시 · 괄호 번호: (1) [1] - 복사본
    s = RE_PAREN_NUM.replace_all(&s, "").to_string();
    s = RE_COPY.replace_all(&s, "").to_string();
    // 날짜: _20190824 · _2019.03.29 · _24.04.01 · -2019.03.29 · (2019.03.29) · _230406
    s = RE_DATE8.replace_all(&s, " ").to_string();
    s = RE_DATE_DOT.replace_all(&s, " ").to_string();
    s = RE_DATE67.replace_all(&s, " ").to_string();
    if versions {
        // 버전: v2.0 · _v2.3.1 · ver1 · _최종 · 최종본 · _수정
        // _ 도 낱말 글자라 \b 로는 "_v1.1" 의 v 앞을 못 잡는다 — 영문자가 아닌 것 뒤로 본다
        s = RE_VER.replace_all(&s, " ").to_string();
        s = RE_FINAL.replace_all(&s, "").to_string();
    }
    // 앞 번호: 07_ · 25. · 4-5_ · 1_1_ · 01.
    s = RE_LEADNUM.replace_all(&s, "").to_string();
    s = RE_UNDERS.replace_all(&s, " ").to_string();
    let s = RE_WS.replace_all(&s, " ").to_string();
    let s = pyutil::strip_chars(&s, " -_.").to_string();
    if s.is_empty() { base } else { s }
}

re!(RE_MS_PREFIX, r"(?i)^microsoft (word|excel|powerpoint)\s*-\s*(.+)$");
re!(RE_FILE_LIKE, r"\.[A-Za-z]{2,5}$");

//------------------------------------------------------------------
// 속성 제목 정리 (§9-1 ①) — 쓰레기면 None
//=> "Microsoft Word - 경조사지원규정.doc" → 괄호 속 파일명만 남기고 다시 판정.
//   <B8AEB4…> 는 CP949 hex 로 풀어 본다(풀어서 파일명이면 버림). 상속 문서의 제목도 버린다.
//
// -in: title = 원본 제목(글자가 아니면 None) / rules / inherited
//
// -out: Some(정리한 제목) / 못 쓰면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn clean_prop_title(title: Option<&Value>, rules: &Rules, inherited: bool) -> Option<String> {
    let title = match title {
        Some(Value::String(s)) => s,
        _ => return None,
    };
    if inherited {
        return None;
    }
    let mut t = pyutil::strip(title).to_string();
    if let Some(m) = RE_HEX.captures(&t) {
        let hx = m[1].to_string();
        if hx.len() % 2 != 0 {
            return None;
        }
        let bytes: Vec<u8> = (0..hx.len()).step_by(2).map(|i| u8::from_str_radix(&hx[i..i + 2], 16).ok()).collect::<Option<_>>()?;
        t = pyutil::strip(&pyfmt::decode_cp949(&bytes)?).to_string();
    }
    if let Some(m) = RE_MS_PREFIX.captures(&t) {
        t = pyutil::strip(&m[2]).to_string();
    }
    if t.is_empty() || t.chars().any(|c| c < ' ') {
        return None;
    }
    // 깨진 글자(라틴 확장이 많음) · 파일 같은 이름(GabrickWeiss.book) · 너무 짧은 일반어(과정명)
    let n = t.chars().count();
    let ext = t.chars().filter(|c| ('À'..='ɏ').contains(c) || *c == '˘' || *c == '´').count();
    if ext > 0 && ext as f64 / n as f64 > 0.15 {
        return None;
    }
    if RE_FILE_LIKE.is_match(&t) {
        return None;
    }
    let t = join_spaced(&t);
    if rules.re_title_junk.iter().any(|p| p.is_match(&t).unwrap_or(false)) {
        return None;
    }
    if t.chars().count() < 4 {
        return None;
    }
    Some(t)
}

// "취  업  규  칙" 처럼 한 글자씩 띄운 제목을 붙인다
fn join_spaced(t: &str) -> String {
    let parts = pyutil::split_ws(t);
    if parts.len() >= 3 && parts.iter().all(|x| x.chars().count() == 1 && HANGUL.is_match(x)) {
        return parts.concat();
    }
    t.to_string()
}

//------------------------------------------------------------------
// 본문 첫 제목 (§9-1 ② · §9-2) — 1단계는 원본 값에 있는 것만(PPT 1번 슬라이드 제목)
//------------------------------------------------------------------
pub fn body_title(raw: &Value, rules: &Rules) -> Option<String> {
    let doc = obj(raw.get("doc"));
    let titles = doc.get("slide_titles").and_then(|v| v.as_array())?;
    let first = titles.first()?;
    let t = match first {
        Value::String(s) => pyutil::strip(s).to_string(),
        _ => String::new(),
    };
    let n = t.chars().count();
    let low = pyfmt::lower(&t);
    if (4..=60).contains(&n) && !["목차", "contents", "agenda"].contains(&low.as_str())
        && !rules.re_title_junk.iter().any(|p| p.is_match(&t).unwrap_or(false)) {
        return Some(t);
    }
    None
}

//------------------------------------------------------------------
// 비교용 키 — 공백 · 기호를 뺀 소문자 (파이썬 re.sub(r"[\W_]+", "", s.lower()))
//------------------------------------------------------------------
pub fn key(s: &str) -> String {
    pyfmt::lower(s).chars().filter(|c| *c != '_' && pyfmt::is_word(*c)).collect()
}

// 문자 3-gram 자카드
fn jaccard3(a: &str, b: &str) -> f64 {
    let grams = |s: &str| -> HashSet<String> {
        let cs: Vec<char> = s.chars().collect();
        (0..cs.len().saturating_sub(2)).map(|i| cs[i..i + 3].iter().collect()).collect()
    };
    let (ga, gb) = (grams(a), grams(b));
    if ga.is_empty() || gb.is_empty() {
        return 0.0;
    }
    let inter = ga.intersection(&gb).count();
    let uni = ga.union(&gb).count();
    inter as f64 / uni as f64
}

//------------------------------------------------------------------
// 대표 제목 고르기 (§9-3)
//=> 세 후보(① 속성 ② 본문 ③ 파일명 정리본) 중 둘이 비슷하면(포함 관계 · 3-gram 자카드 ≥ 0.6)
//   더 긴 쪽, 아니면 ② → ① → ③ 순서. 모두 없으면 원래 파일명.
//
// -in: prop · body · fname = 후보 / name = 원래 파일 이름
//
// -out: (대표 제목, 신뢰도 high|mid)
// -out: error = 없음
//------------------------------------------------------------------
pub fn pick_title(prop: Option<&str>, body: Option<&str>, fname: Option<&str>, name: &str) -> (String, &'static str) {
    let cands: Vec<&str> = [body, prop, fname].into_iter().flatten().filter(|c| !c.is_empty()).collect();
    for i in 0..cands.len() {
        for j in i + 1..cands.len() {
            let (a, b) = (key(cands[i]), key(cands[j]));
            if !a.is_empty() && !b.is_empty() && (b.contains(&a) || a.contains(&b) || jaccard3(&a, &b) >= 0.6) {
                let pick = if cands[i].chars().count() >= cands[j].chars().count() { cands[i] } else { cands[j] };
                return (pick.to_string(), "high");
            }
        }
    }
    if let Some(c) = cands.first() {
        return (c.to_string(), "mid");
    }
    (pyutil::splitext(name).0.to_string(), "mid")
}

// ── 형식 · 출처 · 유형 (§10) ───────────────────────────────

//------------------------------------------------------------------
// 원본 프로그램 (§10-2) — Creator · Producer · Application 에 든 말로
//
// -in: doc = raw["doc"] / fmt = 형식 / rules
//
// -out: Some(hwp|word|excel|ppt|scanner|print|web|other) / 단서가 없으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn origin_app(doc: &Map<String, Value>, fmt: &str, rules: &Rules) -> Option<String> {
    let parts: Vec<String> = ["creator", "producer", "app"].iter().map(|k| {
        let v = doc.get(*k);
        if truthy(v) { py_str(v.unwrap()) } else { String::new() }
    }).collect();
    let text = pyfmt::lower(&parts.join(" "));
    if pyutil::strip(&text).is_empty() {
        // PDF 가 아니면 형식이 곧 원본 프로그램
        return if ["word", "excel", "ppt", "hwp"].contains(&fmt) { Some(fmt.to_string()) } else { None };
    }
    for (value, words) in &rules.origin_apps {
        if words.iter().any(|w| text.contains(w.as_str())) {
            return Some(value.clone());
        }
    }
    Some("other".into())
}

//------------------------------------------------------------------
// 파이썬 ipaddress.ip_address(host).is_private (3.12 표)
//
// -in: host = 글자
//
// -out: Some(사설 주소인가) / IP 주소가 아니면 None
// -out: error = 없음
//------------------------------------------------------------------
fn ip_private(host: &str) -> Option<bool> {
    if let Ok(a) = host.parse::<std::net::Ipv4Addr>() {
        let o = a.octets();
        let n = u32::from(a);
        let net = |base: [u8; 4], bits: u32| -> bool {
            let b = u32::from(std::net::Ipv4Addr::from(base));
            let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits) };
            (n & mask) == (b & mask)
        };
        let _ = o;
        return Some(net([0, 0, 0, 0], 8) || net([10, 0, 0, 0], 8) || net([127, 0, 0, 0], 8) || net([169, 254, 0, 0], 16)
            || net([172, 16, 0, 0], 12) || net([192, 0, 0, 0], 29) || net([192, 0, 0, 170], 31) || net([192, 0, 2, 0], 24)
            || net([192, 168, 0, 0], 16) || net([198, 18, 0, 0], 15) || net([198, 51, 100, 0], 24) || net([203, 0, 113, 0], 24)
            || net([240, 0, 0, 0], 4) || net([255, 255, 255, 255], 32));
    }
    if let Ok(a) = host.parse::<std::net::Ipv6Addr>() {
        let n = u128::from(a);
        let net = |base: u128, bits: u32| -> bool {
            let mask = if bits == 0 { 0 } else { u128::MAX << (128 - bits) };
            (n & mask) == (base & mask)
        };
        return Some(net(1, 128) || net(0, 128) || net(0xffff_0000_0000u128, 96) || net(0x0100u128 << 112, 64)
            || net(0x2001u128 << 112, 23) || net((0x2001u128 << 112) | (0x0db8u128 << 96), 32)
            || net((0x2001u128 << 112) | (0x0010u128 << 96), 28) || net(0xfc00u128 << 112, 7) || net(0xfe80u128 << 112, 10));
    }
    None
}

//------------------------------------------------------------------
// 받은 경로 (§10-3) — Zone.Identifier 의 호스트 → mail|drive|intranet|ai|web
//
// -in: host = 도메인(없으면 None) / rules
//
// -out: Some(종류) / 받은 정보가 없으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn recv_kind(host: Option<&str>, rules: &Rules) -> Option<String> {
    let host = host.filter(|h| !h.is_empty())?;
    if ip_private(host) == Some(true) {
        return Some("intranet".into());
    }
    for (kind, doms) in &rules.recv_domains {
        if doms.iter().any(|d| host == d || host.ends_with(&format!(".{}", d))) {
            return Some(kind.clone());
        }
    }
    if rules.intranet_prefix.iter().any(|p| host.starts_with(p.as_str())) {
        return Some("intranet".into());
    }
    Some("web".into())
}

re!(RE_PATHSEP, r"[\\/]");

//------------------------------------------------------------------
// 템플릿 이름(§4 #17) — 경로 · 확장자를 떼고, Normal 계열은 None
//------------------------------------------------------------------
pub fn template_of(doc: &Map<String, Value>, rules: &Rules) -> Option<String> {
    let t = match doc.get("template") {
        Some(Value::String(s)) if !pyutil::strip(s).is_empty() => s,
        _ => return None,
    };
    let t = RE_PATHSEP.split(pyutil::strip(t)).last().unwrap_or("").to_string();
    let low = pyfmt::lower(&t);
    let stem_low = pyfmt::lower(pyutil::splitext(&t).0);
    if rules.plain_templates.iter().any(|p| *p == low || *p == stem_low) {
        return None;
    }
    Some(t)
}

//------------------------------------------------------------------
// 문서 유형 · 양식 점수 (§10-4) — 규칙 점수(LLM 없음)
//=> 유형: 제목 후보 · 파일명 +3, 폴더명 +2, 템플릿명 +2 → 가장 높고 3점 이상인 유형
//   양식: 양식어(제목 · 파일명 +3, 폴더 +2, 템플릿 +2) · 양식 시트명 +1 · 템플릿 확장자 +5 → 3점 이상
//
// -in: texts = 제목 후보 · 파일명 글 / folders = 폴더 이름 목록 / template · sheets · ext / rules
//
// -out: (유형, 유형 점수, 양식인가, 양식 점수)
// -out: error = 없음
//------------------------------------------------------------------
pub fn type_scores(texts: &[Option<&str>], folders: &[String], template: Option<&str>, sheets: &[String], ext: &str,
                   rules: &Rules) -> (Option<String>, f64, bool, f64) {
    let title_txt = pyfmt::lower(&texts.iter().flatten().filter(|t| !t.is_empty()).cloned().collect::<Vec<_>>().join(" "));
    let folder_txt = pyfmt::lower(&folders.join(" "));
    let tmpl = pyfmt::lower(template.unwrap_or(""));
    let mut scores: Vec<(String, i64)> = vec![];
    for w in &rules.doc_types {
        let s = (if title_txt.contains(w.as_str()) { 3 } else { 0 }) + (if folder_txt.contains(w.as_str()) { 2 } else { 0 })
            + (if tmpl.contains(w.as_str()) { 2 } else { 0 });
        if s > 0 {
            // 메뉴얼 · 매뉴얼은 같은 유형
            let k = if w == "메뉴얼" { "매뉴얼".to_string() } else { w.clone() };
            match scores.iter_mut().find(|(n, _)| *n == k) {
                Some((_, v)) => *v = (*v).max(s),
                None => scores.push((k, s)),
            }
        }
    }
    // 파이썬 max() 는 같은 점수면 먼저 넣은 것
    let (mut dtype, mut dscore) = (None, 0);
    for (k, s) in &scores {
        if dtype.is_none() || *s > dscore {
            dtype = Some(k.clone());
            dscore = *s;
        }
    }
    let mut form = 0;
    if rules.form_words.iter().any(|w| title_txt.contains(w.as_str())) {
        form += 3;
    }
    if rules.form_folder_words.iter().any(|w| folder_txt.contains(w.as_str())) {
        form += 2;
    }
    if !tmpl.is_empty() && rules.form_words.iter().chain(rules.doc_types.iter()).any(|w| tmpl.contains(w.as_str())) {
        form += 2;
    }
    if sheets.iter().any(|s| rules.form_sheets.iter().any(|f| s.contains(f.as_str()))) {
        form += 1;
    }
    if rules.template_exts.iter().any(|e| e == ext) {
        form += 5;
    }
    (if dscore >= 3 { dtype } else { None }, dscore as f64, form >= 3, form as f64)
}

//------------------------------------------------------------------
// 정규화 결과 한 줄(doc_meta 의 정규화 칸)
// -필드: 이름이 doc_meta 열 이름과 같다. ext_mismatch 는 store 가 raw 에서 채운다.
//------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq)]
pub struct DocMeta {
    pub content_hash: Value,
    pub version_key: Option<String>,
    pub format: String,
    pub size: Value,
    pub pages: Option<i64>,
    pub modified_ts: Option<i64>,
    pub created_ts: Option<i64>,
    pub created_conf: Option<f64>,
    pub added_ts: Option<i64>,
    pub inherited: i64,
    pub title_display: String,
    pub title_text: String,
    pub folder_text: String,
    pub origin_app: Option<String>,
    pub scanned: Option<i64>,
    pub recv_kind: Option<String>,
    pub recv_host: Value,
    pub template: Option<String>,
    pub doc_type: Option<String>,
    pub doc_type_score: f64,
    pub is_form: i64,
    pub form_score: f64,
    pub status: Value,
    pub ext_mismatch: i64,
}

// 파이썬 int(pages) — 글자 · 정수 · 참거짓을 받는다
fn py_int_value(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)),
        Value::Bool(b) => Some(*b as i64),
        Value::String(s) => pyfmt::py_int(s),
        _ => None,
    }
}

//------------------------------------------------------------------
// 파이썬 os.path.relpath(path, start) — 같은 드라이브 안에서
//------------------------------------------------------------------
fn relpath(path: &str, start: &str) -> String {
    let a = pyutil::abspath(start);
    let b = pyutil::abspath(path);
    let sl: Vec<&str> = a.split('\\').filter(|x| !x.is_empty()).collect();
    let pl: Vec<&str> = b.split('\\').filter(|x| !x.is_empty()).collect();
    let mut i = 0;
    while i < sl.len() && i < pl.len() && pyutil::normcase(sl[i]) == pyutil::normcase(pl[i]) {
        i += 1;
    }
    let mut rel: Vec<&str> = vec![".."; sl.len() - i];
    rel.extend(&pl[i..]);
    if rel.is_empty() { ".".into() } else { rel.join("\\") }
}

//------------------------------------------------------------------
// RawMeta → DocMeta (정규화 진입점)
//=> meta.sqlite 의 doc_meta 한 줄 + doc_people 여러 줄을 만든다.
//
// -in: path      = 문서 경로(index_state.json 키와 같은 절대경로)
// -in: raw       = extract_raw() 결과(JSON)
// -in: rules     = load_rules() / aliases = 별칭 사전
// -in: inherited = 템플릿 상속 문서인가(store 가 전체를 보고 정해 준다)
// -in: base      = 인덱싱 폴더(폴더 토큰을 그 아래 경로로만 잡는다, 없으면 부모 폴더 하나)
//
// -out: (DocMeta, [Person])
// -out: error = 없음
//------------------------------------------------------------------
pub fn normalize(path: &str, raw: &Value, rules: &Rules, aliases: Option<&HashMap<String, String>>, inherited: bool,
                 base: Option<&str>) -> (DocMeta, Vec<Person>) {
    let doc = obj(raw.get("doc"));
    let fs = obj(raw.get("fs"));
    let name = match raw.get("name") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => pyutil::basename(path).to_string(),
    };
    let fmt = match raw.get("format") {
        Some(v) if truthy(Some(v)) => py_str(v),
        _ => "other".to_string(),
    };
    // 폴더 이름들 — 인덱싱 폴더 아래 경로만(위쪽 D:\Project 같은 이름은 분류가 아니다)
    let parent = pyutil::dirname(path);
    let folders: Vec<String> = match base.filter(|b| !b.is_empty()) {
        Some(b) if pyutil::normcase(&parent).starts_with(&pyutil::normcase(&pyutil::abspath(b))) => {
            let rel = relpath(&parent, b);
            if rel == "." { vec![] } else { rel.split('\\').map(|s| s.to_string()).collect() }
        }
        _ => if parent.is_empty() { vec![] } else { vec![pyutil::basename(&parent).to_string()] },
    };
    let prop = clean_prop_title(doc.get("title"), rules, inherited);
    let body = body_title(raw, rules);
    let fname = clean_filename(&name, false);
    let fname_opt = if fname.is_empty() { None } else { Some(fname.as_str()) };
    let (display, _) = pick_title(prop.as_deref(), body.as_deref(), fname_opt, &name);
    let tmpl = template_of(doc, rules);
    let sheets_v: Vec<Value> = match doc.get("sheets") {
        Some(Value::Array(a)) => a.clone(),
        _ => vec![],
    };
    let sheets: Vec<String> = sheets_v.iter().map(|s| if truthy(Some(s)) { py_str(s) } else { String::new() }).collect();
    let ext = raw.get("ext").map(py_str).unwrap_or_else(|| "None".into());
    let (dtype, dscore, is_form, fscore) = type_scores(&[prop.as_deref(), body.as_deref(), fname_opt], &folders,
                                                       tmpl.as_deref(), &sheets, &ext, rules);
    let origin = origin_app(doc, &fmt, rules);
    // pages = doc.pages or doc.slides or len(sheets)
    let pages_v: Option<Value> = if truthy(doc.get("pages")) {
        doc.get("pages").cloned()
    } else if truthy(doc.get("slides")) {
        doc.get("slides").cloned()
    } else if !sheets_v.is_empty() {
        Some(Value::from(sheets_v.len() as i64))
    } else {
        None
    };
    let pages = pages_v.as_ref().and_then(py_int_value);
    let mut scanned = None;
    if fmt == "pdf" {
        // 앞 3쪽 글자가 거의 없거나 스캐너가 만든 PDF 면 스캔본
        let tc = doc.get("text_chars").and_then(|v| v.as_f64());
        let low_text = matches!(tc, Some(t) if pages.map(|p| p != 0).unwrap_or(false) && t < 50.0);
        scanned = Some((low_text || origin.as_deref() == Some("scanner")) as i64);
    }
    let (modified_ts, created_ts, created_conf, added_ts) = dates_of(raw, inherited);
    let vkey = key(&clean_filename(&name, true));
    let title_text = [prop.as_deref(), body.as_deref(), fname_opt].iter().flatten().filter(|t| !t.is_empty())
        .cloned().collect::<Vec<_>>().join(" | ");
    let host = fs.get("host").cloned().unwrap_or(Value::Null);
    let host_s = match &host {
        Value::String(s) => Some(s.as_str()),
        _ => None,
    };
    let status = match raw.get("status") {
        Some(v) if truthy(Some(v)) => v.clone(),
        _ => Value::from("ok"),
    };
    let meta = DocMeta {
        content_hash: fs.get("sha1").cloned().unwrap_or(Value::Null),
        version_key: if vkey.is_empty() { None } else { Some(vkey) },
        format: fmt,
        size: fs.get("size").cloned().unwrap_or(Value::Null),
        pages,
        modified_ts,
        created_ts,
        created_conf,
        added_ts,
        inherited: inherited as i64,
        title_display: display,
        title_text,
        folder_text: folders.join(" / "),
        origin_app: origin,
        scanned,
        recv_kind: recv_kind(host_s, rules),
        recv_host: host,
        template: tmpl,
        doc_type: dtype,
        doc_type_score: dscore,
        is_form: is_form as i64,
        form_score: fscore,
        status,
        ext_mismatch: truthy(raw.get("ext_mismatch")) as i64,
    };
    let kind = raw.get("kind").map(py_str).unwrap_or_else(|| "None".into());
    (meta, people_of(doc, &kind, rules, aliases, inherited))
}

//------------------------------------------------------------------
// 별칭 사전 읽기 (§7-2) — people.default.yaml(배포) + people.yaml(사용자)
//=> 형식: people: {대표이름: [별칭, …]} (people 모듈이 쓰는 모양). 결과는 {별칭 소문자: 대표 이름}.
//   대표 이름 자신도 넣는다(영문 이름 순서 무시용 키도 함께).
//
// -in: root = 데이터 폴더
//
// -out: {별칭: 대표 이름}
// -out: error = 없음 (파일이 없거나 깨졌으면 빈 사전)
//------------------------------------------------------------------
pub fn load_aliases(root: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    if root.is_empty() {
        return out;
    }
    for fname in ["people.default.yaml", "people.yaml"] {
        let p = pyutil::join(root, fname);
        let data: serde_yaml::Value = match std::fs::read_to_string(&p).ok().and_then(|s| serde_yaml::from_str(&s).ok()) {
            Some(d) => d,
            None => continue,
        };
        let map = match data.as_mapping() {
            Some(m) => m,
            None => continue,
        };
        // people.yaml 모양: people: {대표: [별칭]} (rejected · me 는 별칭이 아니다). 예전 평평한 모양도 받는다
        let entries: Vec<(serde_yaml::Value, serde_yaml::Value)> = match map.get("people").and_then(|v| v.as_mapping()) {
            Some(pm) => pm.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
            None => map.iter().filter(|(k, _)| !matches!(k.as_str(), Some("rejected") | Some("me")))
                .map(|(k, v)| (k.clone(), v.clone())).collect(),
        };
        for (canon, v) in entries {
            let als: serde_yaml::Value = match &v {
                serde_yaml::Value::Mapping(m) => m.get("aliases").cloned().unwrap_or(serde_yaml::Value::Null),
                other => other.clone(),
            };
            let canon_s = super::rules::yaml_str(&canon);
            let mut items: Vec<String> = vec![canon_s.clone()];
            match &als {
                serde_yaml::Value::Sequence(s) => items.extend(s.iter().map(super::rules::yaml_str)),
                // 파이썬 list("abc") 는 글자 하나씩
                serde_yaml::Value::String(s) if !s.is_empty() => items.extend(s.chars().map(|c| c.to_string())),
                _ => {}
            }
            for a in items {
                let a = pyfmt::lower(pyutil::strip(&a));
                if !a.is_empty() {
                    let mut ws = pyutil::split_ws(&a);
                    ws.sort();
                    let k2 = ws.join(" ");
                    out.insert(a, canon_s.clone());
                    out.insert(k2, canon_s.clone());
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::meta::rules::load_rules;

    fn np(s: &str, r: &Rules) -> Option<String> {
        norm_person_str(s, r, None).map(|g| g.0)
    }

    // 사람 이름 정규화 (§7-1) — 순서 바뀐 한글 이름 · 직함 · 소속 · 이메일 · hex · 모지바케 · 쓰레기 값
    #[test]
    fn person() {
        let r = load_rules("");
        let ok = [
            ("봉수 고", "고봉수"), ("고 봉수", "고봉수"), ("고봉수 과장", "고봉수"), ("고봉수님", "고봉수"),
            ("김은지(Eline)", "김은지"), ("이경준 매니저 정보화전략팀", "이경준"), ("<C0AFBFACC1A4>", "유연정"),
            ("Han, Hyun Bae", "han hyun bae"), ("sdchoi@mocomsys.com", "sdchoi"), ("jungcw", "jungcw"),
            ("Windows 사용자;juyeong.choi@dncompany.com", "juyeong.choi"),
        ];
        for (raw, want) in ok {
            assert_eq!(np(raw, &r).as_deref(), Some(want), "{}", raw);
        }
        for junk in ["User", "Microsoft 계정", "m", "PC", "Administrator", "7963", "ê³€ë´›ì‹Ÿ",
                     "Keysight Technologies", "INSUNG DATA INC.", "PC301", "MND-PC", "PptxGenJS", "(주)한글과컴퓨터"] {
            assert_eq!(np(junk, &r), None, "{}", junk);
        }
        // 별칭 사전 — 계정 · 영문 이름(낱말 순서 무시) → 대표 이름
        let mut al = HashMap::new();
        al.insert("bong9".to_string(), "고봉수".to_string());
        al.insert("cho kang lae".to_string(), "조강래".to_string());
        assert_eq!(norm_person_str("bong9", &r, Some(&al)), Some(("고봉수".into(), "alias".into())));
        assert_eq!(norm_person_str("Kang Lae Cho", &r, Some(&al)), Some(("조강래".into(), "alias".into())));
        // 모지바케 되살리기 — CP949 를 Latin-1 로 읽은 값
        assert_eq!(np("°íºÀ¼ö", &r).as_deref(), Some("고봉수"));
    }

    // 날짜 해석 (§8-1) — 실측 형식 다섯 가지와 이상값(UTC epoch)
    #[test]
    fn dates() {
        let utc = |y: i64, mo: i64, d: i64, h: i64, mi: i64, s: i64| pyfmt::days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s;
        let p = |s: &str| parse_date_str(s, None);
        assert_eq!(p("D:20100823131638+09'00'"), Some(utc(2010, 8, 23, 4, 16, 38)));
        assert_eq!(p("D:20100823131638"), Some(utc(2010, 8, 23, 4, 16, 38)));     // 오프셋 없음 = KST
        assert_eq!(p("2023-11-10T02:00:23Z"), Some(utc(2023, 11, 10, 2, 0, 23)));
        assert_eq!(p("2012-03-22T07:05:00"), Some(utc(2012, 3, 22, 7, 5, 0)));   // OLE FILETIME = UTC
        assert_eq!(p("2005년 10월 7일 금요일 오후 10:28:32"), Some(utc(2005, 10, 7, 13, 28, 32)));
        assert_eq!(p("Mon Jan 12 08:51:48 2004"), Some(utc(2004, 1, 11, 23, 51, 48)));
        assert_eq!(p("March 15, 2024 14:09"), Some(utc(2024, 3, 15, 5, 9, 0)));
        assert_eq!(p("2024-03-15T14:09:00.5+09:00"), Some(utc(2024, 3, 15, 5, 9, 0)));
        assert_eq!(p("2024-03-15 14:09:00.123+0000"), Some(utc(2024, 3, 15, 14, 9, 0)));
        assert_eq!(p("D:20100823131638-0900"), Some(utc(2010, 8, 23, 22, 16, 38)));
        for bad in ["1900-12-31T15:00:00", "1601-01-01 09:00:00", "2999-01-01T00:00:00Z", "", "2023-02-30T00:00:00"] {
            assert_eq!(p(bad), None, "{}", bad);
        }
        assert_eq!(parse_date(None, None), None);
    }

    // 제목 (§9) — 쓰레기 속성 제목 · 파일명 정리본 · 대표 제목 고르기
    #[test]
    fn titles() {
        let r = load_rules("");
        let t = |s: &str, inh: bool| clean_prop_title(Some(&Value::from(s)), &r, inh);
        for junk in ["슬라이드 1", "PowerPoint 프레젠테이션", "ARIA2.dvi", "3", "GabrickWeiss.book", "과정명"] {
            assert_eq!(t(junk, false), None, "{}", junk);
        }
        assert_eq!(t("Microsoft Word - 경조사지원규정.doc", false), None);
        assert_eq!(t("취  업  규  칙", false).as_deref(), Some("취업규칙"));
        assert_eq!(t("유형자산 관리 규정", true), None);          // 상속 문서의 제목은 버린다
        assert_eq!(clean_filename("25.출장여비규정_16.04.01.pdf", true), "출장여비규정");
        assert_eq!(clean_filename("11_M드라이브 V9 사용자 매뉴얼_20190915.pptx", false), "M드라이브 V9 사용자 매뉴얼");
        assert_eq!(clean_filename("문서분류체계_v1.1.pptx", true), "문서분류체계");
        assert_eq!(clean_filename("09_설치확인서_2020525.pdf.pdf", true), "설치확인서");
        // 비슷한 두 후보는 긴 쪽
        assert_eq!(pick_title(None, Some("M드라이브 V9"), Some("M드라이브 V9 사용자 매뉴얼"), "x").0, "M드라이브 V9 사용자 매뉴얼");
        assert_eq!(key("① 개요_A-b"), "①개요ab");
    }

    // 출처 · 유형 (§10)
    #[test]
    fn origin_recv_type() {
        let r = load_rules("");
        let mut d = Map::new();
        d.insert("producer".into(), Value::from("Hancom PDF 1.3.0.538"));
        assert_eq!(origin_app(&d, "pdf", &r).as_deref(), Some("hwp"));
        d.insert("producer".into(), Value::from("ApeosPort-VI C3371"));
        assert_eq!(origin_app(&d, "pdf", &r).as_deref(), Some("scanner"));
        assert_eq!(recv_kind(Some("ezwebmail.bizmeka.com"), &r).as_deref(), Some("mail"));
        assert_eq!(recv_kind(Some("10.10.4.10"), &r).as_deref(), Some("intranet"));
        assert_eq!(recv_kind(Some("claude.ai"), &r).as_deref(), Some("ai"));
        assert_eq!(recv_kind(Some("example.com"), &r).as_deref(), Some("web"));
        let (dtype, _, form, _) = type_scores(&[Some("업무 처리 절차서 양식")], &["13_샘플_템플릿".to_string()], None, &[], "docx", &r);
        assert!(dtype.as_deref() == Some("절차서") && form);
    }
}
