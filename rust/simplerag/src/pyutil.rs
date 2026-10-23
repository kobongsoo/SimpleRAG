//------------------------------------------------------------------
// 파이썬 동작 맞추기 도우미
//=> Python 판과 "글자까지 같은" 결과를 내려면 str.strip()·split()·os.path·json.dumps 같은
//   기본 함수의 미묘한 규칙(공백 정의, 경로 정규화, ASCII 이스케이프)을 그대로 따라야 한다.
//   Rust 표준 함수와 규칙이 다른 것들을 여기 한곳에 모았다.
//------------------------------------------------------------------

use std::path::{Path, PathBuf};

//------------------------------------------------------------------
// 파이썬 str.isspace() 와 같은 공백 판정
//=> Rust char::is_whitespace 는 유니코드 White_Space 속성만 보지만, 파이썬은
//   0x1C~0x1F(파일·그룹·레코드·단위 구분자)도 공백으로 본다. 그 차이를 메운다.
//
// -in: c = 판정할 글자
//
// -out: bool = 파이썬 isspace() 와 같은 결과
// -out: error = 없음
//------------------------------------------------------------------
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

//------------------------------------------------------------------
// 파이썬 s.strip() — 앞뒤 공백 제거
//
// -in: s = 원문
//
// -out: &str = 앞뒤 공백을 걷어 낸 조각
// -out: error = 없음
//------------------------------------------------------------------
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

// 파이썬 s.lstrip() / s.rstrip()
pub fn lstrip(s: &str) -> &str {
    s.trim_start_matches(is_space)
}
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

//------------------------------------------------------------------
// 파이썬 s.strip(chars) — 지정 글자 집합을 앞뒤에서 제거
//
// -in: s     = 원문
// -in: chars = 걷어 낼 글자들
//
// -out: &str
// -out: error = 없음
//------------------------------------------------------------------
pub fn strip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_matches(|c: char| chars.contains(c))
}
pub fn lstrip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_start_matches(|c: char| chars.contains(c))
}
pub fn rstrip_chars<'a>(s: &'a str, chars: &str) -> &'a str {
    s.trim_end_matches(|c: char| chars.contains(c))
}

//------------------------------------------------------------------
// 파이썬 s.split() — 공백 덩어리로 자르고 빈 조각은 버림
//
// -in: s = 원문
//
// -out: Vec<&str> = 공백이 아닌 조각들
// -out: error = 없음
//------------------------------------------------------------------
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(is_space).filter(|p| !p.is_empty()).collect()
}

//------------------------------------------------------------------
// 파이썬 " ".join(s.split()) — 공백을 한 칸으로 접기
//
// -in: s = 원문
//
// -out: String
// -out: error = 없음
//------------------------------------------------------------------
pub fn collapse_ws(s: &str) -> String {
    split_ws(s).join(" ")
}

//------------------------------------------------------------------
// 글자(코드포인트) 수 — 파이썬 len(str)
//------------------------------------------------------------------
pub fn clen(s: &str) -> usize {
    s.chars().count()
}

//------------------------------------------------------------------
// 파이썬 s[:n] — 앞에서 n 글자(코드포인트)
//
// -in: s = 원문
// -in: n = 글자 수
//
// -out: &str
// -out: error = 없음
//------------------------------------------------------------------
pub fn head(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

//------------------------------------------------------------------
// 파이썬 s[a:b] — 글자 단위 조각 (범위 밖은 잘라 맞춤)
//------------------------------------------------------------------
pub fn slice(s: &str, a: usize, b: usize) -> &str {
    if a >= b {
        return "";
    }
    let start = s.char_indices().nth(a).map(|(i, _)| i).unwrap_or(s.len());
    let end = s.char_indices().nth(b).map(|(i, _)| i).unwrap_or(s.len());
    &s[start..end.max(start)]
}

//------------------------------------------------------------------
// 파이썬 s[n:] — n 글자 뒤 나머지
//------------------------------------------------------------------
pub fn tail_from(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[i..],
        None => "",
    }
}

//------------------------------------------------------------------
// 파이썬 str.partition(sep) — (앞, 구분자 있었나, 뒤)
//------------------------------------------------------------------
pub fn partition<'a>(s: &'a str, sep: &str) -> (&'a str, bool, &'a str) {
    match s.find(sep) {
        Some(i) => (&s[..i], true, &s[i + sep.len()..]),
        None => (s, false, ""),
    }
}

//------------------------------------------------------------------
// 파이썬 str.rpartition(sep)
//------------------------------------------------------------------
pub fn rpartition<'a>(s: &'a str, sep: &str) -> (&'a str, bool, &'a str) {
    match s.rfind(sep) {
        Some(i) => (&s[..i], true, &s[i + sep.len()..]),
        None => ("", false, s),
    }
}

//------------------------------------------------------------------
// 천 단위 쉼표 — 파이썬 "{:,}".format(n)
//------------------------------------------------------------------
pub fn comma(n: i64) -> String {
    let neg = n < 0;
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    if neg {
        format!("-{}", out)
    } else {
        out
    }
}

//------------------------------------------------------------------
// 파이썬 round(x, nd) — 소수 nd 자리 반올림(표시용)
//=> 파이썬은 '정확한 십진 표현' 기준 반올림이다. 문자열로 찍었다 다시 읽어 같은 효과를 낸다.
//------------------------------------------------------------------
pub fn round_to(x: f64, nd: usize) -> f64 {
    format!("{:.*}", nd, x).parse().unwrap_or(x)
}

//------------------------------------------------------------------
// 파이썬 repr(float) 와 같은 실수 문자열
//=> 정수값이면 "3.0", 매우 크거나 작으면 "1e+16" 꼴. JSON 출력에도 쓴다.
//
// -in: v = 실수
//
// -out: String
// -out: error = 없음
//------------------------------------------------------------------
pub fn float_repr(v: f64) -> String {
    if v.is_nan() {
        return "nan".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let a = v.abs();
    // 파이썬 repr 은 1e16 이상, 1e-4 미만에서 지수 표기를 쓴다
    if a >= 1e16 || a < 1e-4 {
        let s = format!("{:e}", v); // 예: 1e16, 1.5e-5
        let (mant, exp) = s.split_once('e').unwrap();
        let exp_i: i32 = exp.parse().unwrap_or(0);
        let sign = if exp_i < 0 { "-" } else { "+" };
        return format!("{}e{}{:02}", mant, sign, exp_i.abs());
    }
    let s = format!("{}", v);
    if s.contains('.') {
        s
    } else {
        format!("{}.0", s)
    }
}

//------------------------------------------------------------------
// JSON 직렬화 — 파이썬 json.dumps(obj, ensure_ascii=True, separators=...)
//=> 워커 출력(@index · @ev)은 ASCII 로만 써야 cp949 콘솔·파이프에서도 깨지지 않는다.
//   키 순서는 넣은 순서를 지킨다(serde_json preserve_order).
//
// -in: v       = JSON 값
// -in: compact = true 면 (",", ":"), false 면 파이썬 기본 (", ", ": ")
//
// -out: String = 한 줄 JSON
// -out: error = 없음
//------------------------------------------------------------------
pub fn json_ascii(v: &serde_json::Value, compact: bool) -> String {
    let mut out = String::new();
    write_json(v, compact, &mut out);
    out
}

// JSON 문자열 하나를 ASCII 이스케이프로 쓴다(파이썬 json 모듈과 같은 규칙)
fn write_json_str(s: &str, out: &mut String) {
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
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// JSON 값을 재귀로 쓴다
fn write_json(v: &serde_json::Value, compact: bool, out: &mut String) {
    use serde_json::Value;
    let (item_sep, kv_sep) = if compact { (",", ":") } else { (", ", ": ") };
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                let f = n.as_f64().unwrap_or(0.0);
                let r = float_repr(f);
                // 파이썬 json 은 nan/inf 를 NaN/Infinity 로 쓴다
                out.push_str(match r.as_str() {
                    "nan" => "NaN",
                    "inf" => "Infinity",
                    "-inf" => "-Infinity",
                    _ => &r,
                });
            }
        }
        Value::String(s) => write_json_str(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_json(x, compact, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_json_str(k, out);
                out.push_str(kv_sep);
                write_json(x, compact, out);
            }
            out.push('}');
        }
    }
}

//------------------------------------------------------------------
// 실수 하나를 JSON 숫자 값으로 (NaN 은 null)
//------------------------------------------------------------------
pub fn jf(x: f64) -> serde_json::Value {
    serde_json::Number::from_f64(x).map(serde_json::Value::Number).unwrap_or(serde_json::Value::Null)
}

// ── 경로 (파이썬 ntpath 규칙) ─────────────────────────────

//------------------------------------------------------------------
// 파이썬 os.path.abspath — 절대 경로(파일 시스템을 보지 않는 정규화)
//=> 윈도우 파이썬은 GetFullPathNameW 를 쓴다. Rust std::path::absolute 도 같은 API 를 써서
//   "D:/a/../b" → "D:\b" 처럼 같은 글자가 나온다. 상태 파일의 키가 이 글자라 꼭 맞춰야 한다.
//
// -in: p = 경로
//
// -out: String = 절대 경로 글자
// -out: error = 없음 (실패하면 원문 그대로)
//------------------------------------------------------------------
pub fn abspath(p: &str) -> String {
    if p.is_empty() {
        return std::env::current_dir().map(|d| d.to_string_lossy().into_owned()).unwrap_or_default();
    }
    match std::path::absolute(Path::new(p)) {
        Ok(a) => {
            let s = a.to_string_lossy().into_owned();
            // 파이썬 abspath 는 끝의 구분자를 지운다("D:\a\" → "D:\a"). 드라이브 루트는 둔다
            if s.len() > 3 && (s.ends_with('\\') || s.ends_with('/')) {
                s.trim_end_matches(['\\', '/']).to_string()
            } else {
                s
            }
        }
        Err(_) => p.to_string(),
    }
}

//------------------------------------------------------------------
// 파이썬 os.path.normcase — 소문자 + '/' → '\'
//------------------------------------------------------------------
pub fn normcase(p: &str) -> String {
    p.replace('/', "\\").to_lowercase()
}

//------------------------------------------------------------------
// 파이썬 os.path.basename — 마지막 구분자 뒤
//------------------------------------------------------------------
pub fn basename(p: &str) -> &str {
    let p2 = strip_drive(p);
    match p2.rfind(['\\', '/']) {
        Some(i) => &p2[i + 1..],
        None => p2,
    }
}

// 드라이브 문자("C:") 를 뗀 나머지 — basename 계산용
fn strip_drive(p: &str) -> &str {
    let b = p.as_bytes();
    if b.len() >= 2 && b[1] == b':' && b[0].is_ascii_alphabetic() {
        &p[2..]
    } else {
        p
    }
}

//------------------------------------------------------------------
// 파이썬 os.path.dirname — 마지막 구분자 앞(구분자 연속은 정리)
//------------------------------------------------------------------
pub fn dirname(p: &str) -> String {
    let (drive, rest) = {
        let b = p.as_bytes();
        if b.len() >= 2 && b[1] == b':' && b[0].is_ascii_alphabetic() {
            (&p[..2], &p[2..])
        } else {
            ("", p)
        }
    };
    match rest.rfind(['\\', '/']) {
        Some(i) => {
            let head = &rest[..=i];
            // 끝 구분자들을 지우되 루트 하나는 남긴다
            let trimmed = head.trim_end_matches(['\\', '/']);
            let head = if trimmed.is_empty() { &head[..1] } else { trimmed };
            format!("{}{}", drive, head)
        }
        None => drive.to_string(),
    }
}

//------------------------------------------------------------------
// 파이썬 os.path.splitext — (앞, 확장자)  "a.b.txt" → ("a.b", ".txt")
//=> 맨 앞의 점들(".bashrc")은 확장자로 보지 않는다.
//------------------------------------------------------------------
pub fn splitext(p: &str) -> (&str, &str) {
    let base_start = p.len() - basename(p).len();
    let base = &p[base_start..];
    let lead = base.len() - base.trim_start_matches('.').len();
    match base[lead..].rfind('.') {
        Some(i) => {
            let at = base_start + lead + i;
            (&p[..at], &p[at..])
        }
        None => (p, ""),
    }
}

//------------------------------------------------------------------
// 파이썬 os.path.join(a, b)
//------------------------------------------------------------------
pub fn join(a: &str, b: &str) -> String {
    if a.is_empty() {
        return b.to_string();
    }
    // b 가 절대 경로면 b 가 이긴다
    let bb = b.as_bytes();
    if (bb.len() >= 2 && bb[1] == b':') || b.starts_with('\\') || b.starts_with('/') {
        return b.to_string();
    }
    if a.ends_with('\\') || a.ends_with('/') {
        format!("{}{}", a, b)
    } else {
        format!("{}\\{}", a, b)
    }
}

//------------------------------------------------------------------
// 확장자 소문자 — os.path.splitext(path)[1].lower()
//------------------------------------------------------------------
pub fn ext_lower(p: &str) -> String {
    splitext(p).1.to_lowercase()
}

//------------------------------------------------------------------
// 파일 수정 시각(초, 소수 버림) — int(os.stat(p).st_mtime)
//------------------------------------------------------------------
pub fn mtime_int(p: &str) -> std::io::Result<i64> {
    let m = std::fs::metadata(p)?;
    let t = m.modified()?;
    Ok(match t.duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_secs() as i64,
        Err(e) => -(e.duration().as_secs_f64().ceil() as i64),
    })
}

//------------------------------------------------------------------
// 지금 시각(유닉스 초, 소수) — time.time()
//------------------------------------------------------------------
pub fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

//------------------------------------------------------------------
// 실행 파일이 있는 폴더 — PyInstaller exe 의 sys.executable 폴더와 같은 역할
//------------------------------------------------------------------
pub fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

//------------------------------------------------------------------
// 오류 한 줄 — 파이썬 "{}: {}".format(type(e).__name__, str(e)[:n]) 흉내
//------------------------------------------------------------------
pub fn err_line(kind: &str, msg: &str, n: usize) -> String {
    format!("{}: {}", kind, head(msg, n))
}

//------------------------------------------------------------------
// 파일 쓰기(원자적) — 임시 파일에 쓰고 바꿔치기(os.replace)
//------------------------------------------------------------------
pub fn write_atomic(path: &str, data: &[u8]) -> std::io::Result<()> {
    let tmp = format!("{}.tmp", path);
    std::fs::write(&tmp, data)?;
    std::fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_rules() {
        assert_eq!(basename(r"D:\a\b.pdf"), "b.pdf");
        assert_eq!(dirname(r"D:\a\b.pdf"), r"D:\a");
        assert_eq!(dirname(r"D:\b.pdf"), r"D:\");
        assert_eq!(splitext(r"D:\a\b.c.pdf"), (r"D:\a\b.c", ".pdf"));
        assert_eq!(splitext(r"D:\a\.rc"), (r"D:\a\.rc", ""));
        assert_eq!(float_repr(3.0), "3.0");
        assert_eq!(float_repr(1e16), "1e+16");
        assert_eq!(float_repr(0.1), "0.1");
        assert_eq!(comma(1234567), "1,234,567");
        assert_eq!(head("가나다라", 2), "가나");
        let v = serde_json::json!({"a": "가", "b": [1, 2.5]});
        assert_eq!(json_ascii(&v, true), r#"{"a":"\uac00","b":[1,2.5]}"#);
    }
}
