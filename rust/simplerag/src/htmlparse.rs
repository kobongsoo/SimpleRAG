//------------------------------------------------------------------
// HTML 토막 내기 (파이썬 html.parser.HTMLParser 흉내)
//=> Python 판의 두 HTML 추출기(기존 HtmlTextExtractor, 구조화 _HtmlBlocks)는 HTMLParser 의
//   handle_starttag / handle_endtag / handle_data 콜백으로 동작한다. 같은 순서의 사건을 내도록
//   작은 토크나이저를 만들었다.
//    · 태그 이름은 소문자
//    · <script>·<style> 안은 닫는 태그까지 통째로 글자(CDATA 내용 요소)
//    · 글자는 엔티티를 풀어서 준다(convert_charrefs=True)
//    · 주석·<!DOCTYPE>·<?...?> 는 버린다
//------------------------------------------------------------------

//------------------------------------------------------------------
// 사건 하나
// -필드: Start(태그, 속성, 스스로 닫힘 여부) / End(태그) / Data(글자)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub enum Ev {
    Start(String, Vec<(String, Option<String>)>, bool),
    End(String),
    Data(String),
}

// 엔티티 풀기 — html.unescape 와 같은 HTML5 규칙
pub fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    html_escape::decode_html_entities(s).into_owned()
}

//------------------------------------------------------------------
// HTML 글 → 사건 목록
//
// -in: html = HTML 원문
//
// -out: Vec<Ev>
// -out: error = 없음 (깨진 HTML 도 읽을 수 있는 데까지 읽는다)
//------------------------------------------------------------------
pub fn tokenize(html: &str) -> Vec<Ev> {
    let b = html.as_bytes();
    let n = b.len();
    let mut out = vec![];
    let mut i = 0;
    let mut text_start = 0;
    // 글자 조각을 사건으로 내보낸다
    let flush = |out: &mut Vec<Ev>, a: usize, z: usize| {
        if z > a {
            out.push(Ev::Data(unescape(&html[a..z])));
        }
    };
    while i < n {
        if b[i] != b'<' {
            i += 1;
            continue;
        }
        let rest = &html[i..];
        // 주석
        if rest.starts_with("<!--") {
            flush(&mut out, text_start, i);
            let end = rest[4..].find("-->").map(|p| i + 4 + p + 3).unwrap_or(n);
            i = end;
            text_start = i;
            continue;
        }
        // 선언·처리 지시
        if rest.starts_with("<!") || rest.starts_with("<?") {
            flush(&mut out, text_start, i);
            let end = rest.find('>').map(|p| i + p + 1).unwrap_or(n);
            i = end;
            text_start = i;
            continue;
        }
        // 닫는 태그
        if rest.starts_with("</") && rest.len() > 2 && (rest.as_bytes()[2] as char).is_ascii_alphabetic() {
            flush(&mut out, text_start, i);
            let end = rest.find('>').map(|p| i + p + 1).unwrap_or(n);
            let inner = &html[i + 2..end.saturating_sub(1).max(i + 2)];
            let name: String = inner
                .chars()
                .take_while(|c| !c.is_whitespace() && *c != '/' && *c != '>')
                .collect::<String>()
                .to_lowercase();
            out.push(Ev::End(name));
            i = end;
            text_start = i;
            continue;
        }
        // 여는 태그
        if rest.len() > 1 && (rest.as_bytes()[1] as char).is_ascii_alphabetic() {
            flush(&mut out, text_start, i);
            let end = find_tag_end(html, i + 1);
            let inner = &html[i + 1..end.saturating_sub(1).max(i + 1)];
            let (name, attrs, self_close) = parse_tag(inner);
            out.push(Ev::Start(name.clone(), attrs, self_close));
            i = end;
            text_start = i;
            // 스크립트·스타일 내용은 닫는 태그까지 글자 그대로(엔티티 풀지 않음)
            if (name == "script" || name == "style") && !self_close {
                let close = format!("</{}", name);
                let lower = html[i..].to_lowercase();
                let stop = lower.find(&close).map(|p| i + p).unwrap_or(n);
                if stop > i {
                    out.push(Ev::Data(html[i..stop].to_string()));
                }
                i = stop;
                text_start = i;
            }
            continue;
        }
        i += 1;
    }
    flush(&mut out, text_start, n);
    out
}

// 따옴표 안의 '>' 를 건너뛰며 태그 끝을 찾는다
fn find_tag_end(html: &str, from: usize) -> usize {
    let b = html.as_bytes();
    let mut q: u8 = 0;
    let mut i = from;
    while i < b.len() {
        let c = b[i];
        if q != 0 {
            if c == q {
                q = 0;
            }
        } else if c == b'"' || c == b'\'' {
            q = c;
        } else if c == b'>' {
            return i + 1;
        }
        i += 1;
    }
    b.len()
}

// 태그 안쪽 글자 → (이름, 속성, 스스로 닫힘)
fn parse_tag(inner: &str) -> (String, Vec<(String, Option<String>)>, bool) {
    let self_close = inner.trim_end().ends_with('/');
    let body = if self_close { inner.trim_end().trim_end_matches('/') } else { inner };
    let chars: Vec<char> = body.chars().collect();
    let mut i = 0;
    let mut name = String::new();
    while i < chars.len() && !chars[i].is_whitespace() && chars[i] != '/' {
        name.push(chars[i]);
        i += 1;
    }
    let mut attrs = vec![];
    loop {
        while i < chars.len() && (chars[i].is_whitespace() || chars[i] == '/') {
            i += 1;
        }
        if i >= chars.len() {
            break;
        }
        let mut k = String::new();
        while i < chars.len() && !chars[i].is_whitespace() && chars[i] != '=' && chars[i] != '/' {
            k.push(chars[i]);
            i += 1;
        }
        while i < chars.len() && chars[i].is_whitespace() {
            i += 1;
        }
        let mut val = None;
        if i < chars.len() && chars[i] == '=' {
            i += 1;
            while i < chars.len() && chars[i].is_whitespace() {
                i += 1;
            }
            let mut v = String::new();
            if i < chars.len() && (chars[i] == '"' || chars[i] == '\'') {
                let q = chars[i];
                i += 1;
                while i < chars.len() && chars[i] != q {
                    v.push(chars[i]);
                    i += 1;
                }
                i += 1;
            } else {
                while i < chars.len() && !chars[i].is_whitespace() {
                    v.push(chars[i]);
                    i += 1;
                }
            }
            val = Some(unescape(&v));
        }
        if !k.is_empty() {
            attrs.push((k.to_lowercase(), val));
        }
    }
    (name.to_lowercase(), attrs, self_close)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn basic() {
        let ev = tokenize("<p class=x>a &amp; b<br/>c</p><script>if(a<b)</script>");
        assert!(matches!(&ev[0], Ev::Start(t, _, false) if t == "p"));
        assert!(matches!(&ev[1], Ev::Data(d) if d == "a & b"));
        assert!(matches!(&ev[2], Ev::Start(t, _, true) if t == "br"));
        assert!(matches!(&ev[6], Ev::Data(d) if d == "if(a<b)"));
    }
}
