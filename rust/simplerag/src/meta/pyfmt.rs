//------------------------------------------------------------------
// 파이썬 값 모양 맞추기 (메타데이터 전용 도우미)
//=> meta.sqlite 의 raw_json 을 Python 판과 글자까지 같게 쓰고, 정규화 규칙이 Python 의
//   str() · 참/거짓 판정 · json.dumps(ensure_ascii=False) · datetime.isoformat() 과 같은 결과를
//   내도록 작은 도우미를 모았다.
//------------------------------------------------------------------

use serde_json::Value;

use crate::pyutil;

//------------------------------------------------------------------
// json.dumps(v, ensure_ascii=False[, indent=n]) 와 같은 글자
//=> 한글은 그대로 두고, 구분자는 파이썬 기본(", " · ": "). indent 를 주면 줄을 바꾼다.
//
// -in: v      = JSON 값
// -in: indent = None 이면 한 줄, Some(n) 이면 n 칸 들여쓰기
//
// -out: String
// -out: error = 없음
//------------------------------------------------------------------
pub fn dumps(v: &Value, indent: Option<usize>) -> String {
    let mut out = String::new();
    write(v, indent, 0, &mut out);
    out
}

// 문자열 하나 — 따옴표 · 역슬래시 · 제어 문자만 이스케이프
fn write_str(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

// 값 하나를 재귀로(들여쓰기 깊이 level)
fn write(v: &Value, indent: Option<usize>, level: usize, out: &mut String) {
    let nl = |out: &mut String, lv: usize| {
        if let Some(n) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(n * lv));
        }
    };
    let sep = if indent.is_some() { "," } else { ", " };
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                out.push_str(&pyutil::float_repr(n.as_f64().unwrap_or(0.0)));
            }
        }
        Value::String(s) => write_str(s, out),
        Value::Array(a) => {
            if a.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(sep);
                }
                nl(out, level + 1);
                write(x, indent, level + 1, out);
            }
            nl(out, level);
            out.push(']');
        }
        Value::Object(m) => {
            if m.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(sep);
                }
                nl(out, level + 1);
                write_str(k, out);
                out.push_str(": ");
                write(x, indent, level + 1, out);
            }
            nl(out, level);
            out.push('}');
        }
    }
}

//------------------------------------------------------------------
// 파이썬 str(v) — JSON 값을 파이썬이 글자로 바꾸는 모양
//=> 문자열은 그대로, 정수는 숫자, True/False/None, 실수는 repr.
//
// -in: v = JSON 값
//
// -out: String
// -out: error = 없음
//------------------------------------------------------------------
pub fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else {
                pyutil::float_repr(n.as_f64().unwrap_or(0.0))
            }
        }
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

//------------------------------------------------------------------
// 파이썬 참/거짓 판정 — bool(v)
//=> None · False · 0 · "" · [] · {} 는 거짓.
//
// -in: v = JSON 값(없으면 None)
//
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
pub fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

//------------------------------------------------------------------
// CP949(UHC) 엄격 풀기 — 파이썬 bytes.decode("cp949") (틀린 바이트가 있으면 None)
//
// -in: b = 바이트
//
// -out: Some(글자) / 못 풀면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn decode_cp949(b: &[u8]) -> Option<String> {
    encoding_rs::EUC_KR.decode_without_bom_handling_and_without_replacement(b).map(|c| c.into_owned())
}

//------------------------------------------------------------------
// UTF-8 엄격 풀기 — 파이썬 bytes.decode("utf-8")
//------------------------------------------------------------------
pub fn decode_utf8(b: &[u8]) -> Option<String> {
    std::str::from_utf8(b).ok().map(|s| s.to_string())
}

// ── 날짜 · 시각 (proleptic 그레고리력, 파이썬 datetime 과 같은 계산) ──

//------------------------------------------------------------------
// 연월일 → 1970-01-01 부터의 일수
//
// -in: y, m, d = 연 · 월 · 일
//
// -out: 일수(음수 가능)
// -out: error = 없음
//------------------------------------------------------------------
pub fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

//------------------------------------------------------------------
// 1970-01-01 부터의 일수 → (연, 월, 일)
//------------------------------------------------------------------
pub fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

// 그 달의 날 수
pub fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => if (y % 4 == 0 && y % 100 != 0) || y % 400 == 0 { 29 } else { 28 },
        _ => 0,
    }
}

//------------------------------------------------------------------
// 파이썬 datetime(...) 이 받아 주는 값인가 (1~9999년, 시 0~23, 분·초 0~59)
//------------------------------------------------------------------
pub fn valid_dt(y: i64, mo: i64, d: i64, h: i64, mi: i64, s: i64) -> bool {
    (1..=9999).contains(&y) && (1..=12).contains(&mo) && d >= 1 && d <= days_in_month(y, mo)
        && (0..24).contains(&h) && (0..60).contains(&mi) && (0..60).contains(&s)
}

//------------------------------------------------------------------
// 1970 기준 마이크로초 → isoformat 글자 (파이썬 datetime.isoformat)
//=> 마이크로초가 0 이면 소수 부분을 쓰지 않는다. suffix 는 "+00:00" 같은 시간대 꼬리.
//
// -in: us     = 1970-01-01 00:00:00 부터의 마이크로초
// -in: suffix = 시간대 꼬리(없으면 "")
//
// -out: "YYYY-MM-DDTHH:MM:SS[.ffffff]<suffix>"
// -out: error = 없음
//------------------------------------------------------------------
pub fn isoformat_us(us: i64, suffix: &str) -> String {
    let secs = us.div_euclid(1_000_000);
    let frac = us.rem_euclid(1_000_000);
    let days = secs.div_euclid(86400);
    let sod = secs.rem_euclid(86400);
    let (y, m, d) = civil_from_days(days);
    let mut s = format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", y, m, d, sod / 3600, (sod % 3600) / 60, sod % 60);
    if frac != 0 {
        s.push_str(&format!(".{:06}", frac));
    }
    s.push_str(suffix);
    s
}

//------------------------------------------------------------------
// 윈도우 FILETIME(1601 기준 100ns) → datetime.fromtimestamp(st_*, timezone.utc).isoformat()
//=> 파이썬 os.stat 은 st_mtime = 초 + 나노초×1e-9 (double) 로 만들고, fromtimestamp 는
//   그 실수를 마이크로초로 반올림(짝수 쪽)한다. 같은 실수 계산을 그대로 따라야
//   마지막 자리까지 같은 글자가 나온다.
//
// -in: ft = FILETIME 값
//
// -out: "YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00"
// -out: error = 없음
//------------------------------------------------------------------
pub fn filetime_iso_utc(ft: u64) -> String {
    let ft = ft as i64;
    let sec = ft / 10_000_000 - 11_644_473_600;
    let nsec = (ft % 10_000_000) * 100;
    let t: f64 = sec as f64 + nsec as f64 * 1e-9;
    // _PyTime_DoubleToDenominator(ROUND_HALF_EVEN)
    let intpart = t.trunc();
    let mut floatpart = (t - intpart) * 1e6;
    let r = floatpart.round();
    floatpart = if (floatpart - r).abs() == 0.5 { 2.0 * (floatpart / 2.0).round() } else { r };
    let mut ip = intpart as i64;
    let mut us = floatpart as i64;
    if us >= 1_000_000 {
        us -= 1_000_000;
        ip += 1;
    } else if us < 0 {
        us += 1_000_000;
        ip -= 1;
    }
    isoformat_us(ip * 1_000_000 + us, "+00:00")
}

//------------------------------------------------------------------
// int(os.stat(p).st_mtime) — 파이썬과 같은 실수 계산으로 자른 수정 시각(초)
//=> 초 + 나노초×1e-9 를 double 로 더하면 .9999999 초가 다음 초로 올라가는 일이 있다.
//   meta scan 의 "바뀌지 않았나" 비교가 파이썬과 같게 그 계산을 그대로 한다.
//
// -in: path = 파일 경로
//
// -out: Some(초) / stat 실패면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn st_mtime_int(path: &str) -> Option<i64> {
    use std::os::windows::fs::MetadataExt;
    let ft = std::fs::metadata(path).ok()?.last_write_time() as i64;
    let sec = ft / 10_000_000 - 11_644_473_600;
    let nsec = (ft % 10_000_000) * 100;
    Some((sec as f64 + nsec as f64 * 1e-9) as i64)
}

//------------------------------------------------------------------
// 파이썬 str.lower() 가까운 소문자
//=> Rust to_lowercase 와 파이썬 lower 는 실측 자료 범위(한글 · 영문 · 기호)에서 같다.
//------------------------------------------------------------------
pub fn lower(s: &str) -> String {
    s.to_lowercase()
}

//------------------------------------------------------------------
// 파이썬 \w 와 같은 낱말 글자인가 (str.isalnum() 또는 '_')
//=> Rust 정규식의 \w 는 결합 부호(Mark)를 넣고 ① 같은 기타 숫자(No)를 빼서 파이썬과 다르다.
//   제목 비교 키(_key)가 ① · ² 를 지키도록 char::is_alphanumeric 로 판정한다
//   (원 문자 Ⓐ~ⓩ 은 파이썬에서 낱말 글자가 아니라 뺀다).
//
// -in: c = 글자
//
// -out: bool
// -out: error = 없음
//------------------------------------------------------------------
pub fn is_word(c: char) -> bool {
    c == '_' || (c.is_alphanumeric() && !('\u{24B6}'..='\u{24E9}').contains(&c))
}

//------------------------------------------------------------------
// 파이썬 int(str) — 앞뒤 공백 · 부호 · 숫자 사이 밑줄 하나 허용
//
// -in: s = 글자
//
// -out: Some(정수) / 정수가 아니면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn py_int(s: &str) -> Option<i64> {
    let t = pyutil::strip(s);
    let (neg, body) = match t.chars().next() {
        Some('-') => (true, &t[1..]),
        Some('+') => (false, &t[1..]),
        _ => (false, t),
    };
    if body.is_empty() || body.starts_with('_') || body.ends_with('_') || body.contains("__") {
        return None;
    }
    let mut v: i64 = 0;
    for c in body.chars() {
        if c == '_' {
            continue;
        }
        let d = c.to_digit(10)? as i64;
        v = v.checked_mul(10)?.checked_add(d)?;
    }
    Some(if neg { -v } else { v })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fmt_rules() {
        assert_eq!(days_from_civil(1970, 1, 1), 0);
        assert_eq!(civil_from_days(days_from_civil(2024, 2, 29)), (2024, 2, 29));
        assert_eq!(isoformat_us(0, "+00:00"), "1970-01-01T00:00:00+00:00");
        assert_eq!(isoformat_us(1_500_000, ""), "1970-01-01T00:00:01.500000");
        let v = serde_json::json!({"a": "가", "b": [1, 2.5, null], "c": {}});
        assert_eq!(dumps(&v, None), r#"{"a": "가", "b": [1, 2.5, null], "c": {}}"#);
        assert_eq!(dumps(&v, Some(1)), "{\n \"a\": \"가\",\n \"b\": [\n  1,\n  2.5,\n  null\n ],\n \"c\": {}\n}");
        assert_eq!(py_int(" 12 "), Some(12));
        assert_eq!(py_int("1.5"), None);
        assert!(is_word('①') && is_word('가') && !is_word('-'));
    }
}
