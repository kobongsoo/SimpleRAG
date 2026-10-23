//------------------------------------------------------------------
// 작은 XML 트리 (파이썬 xml.etree.ElementTree 와 같은 모양)
//=> Python 추출기들은 ET.fromstring() 으로 트리를 만든 뒤 iter()·text·tail 로 글자를 모은다.
//   같은 결과를 내려고 quick-xml 이벤트로 같은 모양의 트리를 만든다.
//    · tag  = 지역 이름(접두사·네임스페이스 뗌) — Python 코드가 _local(tag) 로 비교하던 것
//    · text = 여는 태그 바로 뒤 첫 자식 전까지의 글자, tail = 닫는 태그 뒤 다음 형제 전까지의 글자
//------------------------------------------------------------------

use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader;

//------------------------------------------------------------------
// 요소 하나
// -필드: tag      = 지역 이름(예: "p", "t")
// -필드: prefix   = 접두사(예: "w", "hp") — 없으면 ""
// -필드: attrs    = (접두사 포함 이름, 값) 목록. 예: ("r:id", "rId3"), ("val", "1")
// -필드: children = 자식 요소
// -필드: text / tail = ElementTree 와 같은 뜻
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct Node {
    pub tag: String,
    pub prefix: String,
    pub attrs: Vec<(String, String)>,
    pub children: Vec<Node>,
    pub text: String,
    pub tail: String,
}

impl Node {
    //------------------------------------------------------------------
    // 속성 값 꺼내기
    //=> "r:id" 처럼 접두사까지 주면 정확히 그 이름을, "val" 처럼 지역 이름만 주면
    //   접두사와 상관없이 지역 이름이 같은 첫 속성을 돌려준다.
    //
    // -in: name = 속성 이름
    //
    // -out: Option<&str>
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn get(&self, name: &str) -> Option<&str> {
        if let Some(v) = self.attrs.iter().find(|(k, _)| k == name) {
            return Some(&v.1);
        }
        if !name.contains(':') {
            for (k, v) in &self.attrs {
                if k.rsplit(':').next() == Some(name) {
                    return Some(v);
                }
            }
        }
        None
    }

    //------------------------------------------------------------------
    // 자기 자신부터 모든 후손을 문서 순서로 — ET 의 el.iter()
    //------------------------------------------------------------------
    pub fn iter(&self) -> Vec<&Node> {
        let mut out = vec![];
        fn walk<'a>(n: &'a Node, out: &mut Vec<&'a Node>) {
            out.push(n);
            for c in &n.children {
                walk(c, out);
            }
        }
        walk(self, &mut out);
        out
    }

    //------------------------------------------------------------------
    // 지역 이름이 tag 인 요소만 iter()
    //------------------------------------------------------------------
    pub fn iter_tag(&self, tag: &str) -> Vec<&Node> {
        self.iter().into_iter().filter(|n| n.tag == tag).collect()
    }

    //------------------------------------------------------------------
    // 직계 자식 가운데 지역 이름이 tag 인 첫 요소 — ET 의 el.find(tag)
    //------------------------------------------------------------------
    pub fn child(&self, tag: &str) -> Option<&Node> {
        self.children.iter().find(|c| c.tag == tag)
    }

    // 직계 자식 가운데 지역 이름이 tag 인 요소 모두
    pub fn children_tag(&self, tag: &str) -> Vec<&Node> {
        self.children.iter().filter(|c| c.tag == tag).collect()
    }

    //------------------------------------------------------------------
    // 첫 후손(자기 제외) — ET 의 el.find(".//tag")
    //------------------------------------------------------------------
    pub fn find_desc(&self, tag: &str) -> Option<&Node> {
        for c in &self.children {
            if c.tag == tag {
                return Some(c);
            }
            if let Some(x) = c.find_desc(tag) {
                return Some(x);
            }
        }
        None
    }

    //------------------------------------------------------------------
    // 모든 글자 이어 붙이기 — "".join(el.itertext())
    //------------------------------------------------------------------
    pub fn itertext(&self) -> String {
        let mut s = String::new();
        fn walk(n: &Node, s: &mut String, top: bool) {
            s.push_str(&n.text);
            for c in &n.children {
                walk(c, s, false);
            }
            if !top {
                s.push_str(&n.tail);
            }
        }
        walk(self, &mut s, true);
        s
    }
}

// 이름 바이트 → (접두사, 지역 이름)
fn split_name(raw: &[u8]) -> (String, String) {
    let s = String::from_utf8_lossy(raw);
    match s.split_once(':') {
        Some((p, l)) => (p.to_string(), l.to_string()),
        None => (String::new(), s.to_string()),
    }
}

// 여는 태그 → 빈 노드(이름·속성)
fn start_node(e: &BytesStart, reader: &Reader<&[u8]>) -> Node {
    let (prefix, tag) = split_name(e.name().as_ref());
    let mut attrs = vec![];
    for a in e.attributes().with_checks(false).flatten() {
        let k = String::from_utf8_lossy(a.key.as_ref()).to_string();
        let v = a
            .decode_and_unescape_value(reader.decoder())
            .map(|c| c.into_owned())
            .unwrap_or_else(|_| String::from_utf8_lossy(&a.value).into_owned());
        attrs.push((k, v));
    }
    Node { tag, prefix, attrs, ..Default::default() }
}

// 글자를 지금 자리(마지막 자식의 tail 또는 부모 text)에 붙인다
fn push_text(stack: &mut [Node], s: &str) {
    if let Some(top) = stack.last_mut() {
        match top.children.last_mut() {
            Some(last) => last.tail.push_str(s),
            None => top.text.push_str(s),
        }
    }
}

//------------------------------------------------------------------
// XML 바이트 → 트리 (ET.fromstring)
//=> 잘못된 XML 이면 Err — Python 의 ET.ParseError 와 같은 자리에서 실패하게 한다.
//   선언·주석·처리 지시는 버린다. 엔티티(&amp; 등)와 문자 참조는 풀어 넣는다.
//
// -in: data = XML 바이트(UTF-8)
//
// -out: Ok(루트 노드) / Err(오류문)
// -out: error = 형식 오류·빈 문서면 Err
//------------------------------------------------------------------
pub fn parse(data: &[u8]) -> Result<Node, String> {
    // BOM 은 글자로 들어가지 않게 뗀다
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data);
    let mut reader = Reader::from_reader(data);
    reader.config_mut().trim_text(false);
    reader.config_mut().check_end_names = false;
    let mut stack: Vec<Node> = vec![Node::default()]; // 맨 아래는 가짜 문서 노드
    loop {
        match reader.read_event() {
            Ok(Event::Start(e)) => {
                let n = start_node(&e, &reader);
                stack.push(n);
            }
            Ok(Event::Empty(e)) => {
                let n = start_node(&e, &reader);
                stack.last_mut().unwrap().children.push(n);
            }
            Ok(Event::End(_)) => {
                if stack.len() <= 1 {
                    return Err("짝이 맞지 않는 닫는 태그".into());
                }
                let n = stack.pop().unwrap();
                stack.last_mut().unwrap().children.push(n);
            }
            Ok(Event::Text(t)) => {
                let s = match t.unescape() {
                    Ok(c) => c.into_owned(),
                    Err(_) => unescape_loose(&String::from_utf8_lossy(&t)),
                };
                push_text(&mut stack, &s);
            }
            Ok(Event::CData(c)) => {
                let s = String::from_utf8_lossy(&c).into_owned();
                push_text(&mut stack, &s);
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(e) => return Err(format!("XML 파싱 실패: {}", e)),
        }
    }
    if stack.len() != 1 {
        return Err("닫히지 않은 태그".into());
    }
    let mut doc = stack.pop().unwrap();
    // 루트 요소는 하나여야 한다(ET 와 같다)
    match doc.children.len() {
        0 => Err("루트 요소 없음".into()),
        _ => Ok(doc.children.remove(0)),
    }
}

//------------------------------------------------------------------
// 느슨한 엔티티 풀기 — 표준 5개와 숫자 참조만, 모르는 것은 그대로
//------------------------------------------------------------------
pub fn unescape_loose(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(i) = rest.find('&') {
        out.push_str(&rest[..i]);
        let after = &rest[i..];
        if let Some(j) = after.find(';') {
            let ent = &after[1..j];
            let rep = match ent {
                "amp" => Some("&".to_string()),
                "lt" => Some("<".to_string()),
                "gt" => Some(">".to_string()),
                "quot" => Some("\"".to_string()),
                "apos" => Some("'".to_string()),
                _ if ent.starts_with("#x") || ent.starts_with("#X") => {
                    u32::from_str_radix(&ent[2..], 16).ok().and_then(char::from_u32).map(|c| c.to_string())
                }
                _ if ent.starts_with('#') => ent[1..].parse::<u32>().ok().and_then(char::from_u32).map(|c| c.to_string()),
                _ => None,
            };
            if let Some(r) = rep {
                out.push_str(&r);
                rest = &after[j + 1..];
                continue;
            }
        }
        out.push('&');
        rest = &after[1..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn text_tail() {
        let n = parse(b"<a>x<b>y</b>z<c/>w</a>").unwrap();
        assert_eq!(n.text, "x");
        assert_eq!(n.children[0].tail, "z");
        assert_eq!(n.children[1].tail, "w");
        assert_eq!(n.itertext(), "xyzw");
        let m = parse(b"<w:p xmlns:w='u'><w:r><w:t>&amp;1</w:t></w:r></w:p>").unwrap();
        assert_eq!(m.tag, "p");
        assert_eq!(m.iter_tag("t")[0].text, "&1");
    }
}
