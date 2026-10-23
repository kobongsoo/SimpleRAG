//------------------------------------------------------------------
// 엑셀 날짜 서식 판정 + 일련번호 → 날짜 글자 (csoclassify/docmodel xlsdate.py 와 같은 규칙)
//=> 엑셀은 날짜를 숫자(1900-01-01 기준 일수)로 저장하고 '서식'으로만 날짜처럼 보여 준다.
//   그 숫자를 그대로 두면 "45291" 같은 값이 남으므로, 서식이 날짜·시각이면 글자로 바꾼다.
//------------------------------------------------------------------

pub const KIND_NONE: u8 = 0;
pub const KIND_DATE: u8 = 1;
pub const KIND_TIME: u8 = 2;
pub const KIND_DATETIME: u8 = 3;

//------------------------------------------------------------------
// 내장 서식 번호 → 날짜 종류
//------------------------------------------------------------------
pub fn builtin_kind(fmt_id: i64) -> u8 {
    if (14..=17).contains(&fmt_id) {
        return KIND_DATE;
    }
    if (18..=21).contains(&fmt_id) {
        return KIND_TIME;
    }
    if fmt_id == 22 {
        return KIND_DATETIME;
    }
    if (27..=36).contains(&fmt_id) || (50..=58).contains(&fmt_id) {
        return KIND_DATE;
    }
    if (45..=47).contains(&fmt_id) {
        return KIND_TIME;
    }
    KIND_NONE
}

//------------------------------------------------------------------
// 사용자 서식 문자열 → 날짜 종류
//=> 따옴표 안 글자·역슬래시 다음 글자·[색] 같은 대괄호는 건너뛰고 y/d(날짜), h/s(시각)를 찾는다.
//------------------------------------------------------------------
pub fn code_kind(code: &str) -> u8 {
    let head: Vec<char> = code.split(';').next().unwrap_or("").chars().collect();
    let (mut has_date, mut has_time) = (false, false);
    let n = head.len();
    let mut i = 0;
    while i < n {
        let ch = head[i];
        if ch == '"' {
            i += 1;
            while i < n && head[i] != '"' {
                i += 1;
            }
        } else if ch == '\\' {
            i += 1;
        } else if ch == '[' {
            let j = head[i + 1..].iter().position(|&c| c == ']').map(|p| p + i + 1);
            let inner: String = match j {
                Some(j) => head[i + 1..j].iter().collect(),
                None => head[i + 1..].iter().collect(),
            };
            let t = crate::pyutil::strip(&inner).to_lowercase();
            if !t.is_empty() && t.chars().all(|c| "hms".contains(c)) {
                has_time = true;
            }
            i = j.unwrap_or(n);
        } else if "yYdD".contains(ch) {
            has_date = true;
        } else if "hHsS".contains(ch) {
            has_time = true;
        }
        i += 1;
    }
    match (has_date, has_time) {
        (true, true) => KIND_DATETIME,
        (true, false) => KIND_DATE,
        (false, true) => KIND_TIME,
        _ => KIND_NONE,
    }
}

//------------------------------------------------------------------
// 파이썬 round() — 0.5 는 짝수 쪽으로
//------------------------------------------------------------------
pub fn round_half_even(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 {
        2.0 * (x / 2.0).round()
    } else {
        r
    }
}

//------------------------------------------------------------------
// 일련번호 → 날짜 글자
// -in: v = 일련번호 글자(셀 값) / kind = 날짜 종류
// -out: Some(글자) 또는 None(날짜가 아님·범위 밖)
//------------------------------------------------------------------
pub fn serial_to_string(v: &str, kind: u8) -> Option<String> {
    if kind == KIND_NONE {
        return None;
    }
    let v: f64 = crate::pyutil::strip(v).parse().ok()?;
    serial_f64(v, kind)
}

// 숫자판
pub fn serial_f64(v: f64, kind: u8) -> Option<String> {
    if v.is_nan() || !(0.0..2958466.0).contains(&v) {
        return None;
    }
    let mut days = v.floor() as i64;
    let mut secs = round_half_even((v - days as f64) * 86400.0) as i64;
    if secs >= 86400 {
        secs -= 86400;
        days += 1;
    }
    let time_s = format!("{:02}:{:02}:{:02}", secs / 3600, (secs % 3600) / 60, secs % 60);
    if kind == KIND_TIME {
        return Some(time_s);
    }
    let date_s = if days == 60 {
        "1900-02-29".to_string()
    } else {
        let base = if days >= 61 {
            chrono::NaiveDate::from_ymd_opt(1899, 12, 30)?
        } else {
            chrono::NaiveDate::from_ymd_opt(1899, 12, 31)?
        };
        let d = base.checked_add_signed(chrono::Duration::days(days))?;
        d.format("%Y-%m-%d").to_string()
    };
    if kind == KIND_DATE {
        return Some(date_s);
    }
    Some(format!("{} {}", date_s, time_s))
}
