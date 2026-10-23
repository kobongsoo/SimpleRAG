//------------------------------------------------------------------
// OOXML 공통 도우미 (Python docmodel/ooxml_common.py)
//=> ZIP 안 XML 읽기, 관계(.rels) 풀기, 엑셀 칸 주소 풀기.
//------------------------------------------------------------------

use std::io::Read;

use once_cell::sync::Lazy;
use regex::Regex;

use crate::xmldom::{self, Node};

pub type Zip = zip::ZipArchive<std::fs::File>;

//------------------------------------------------------------------
// ZIP 열기
//------------------------------------------------------------------
pub fn open(path: &str, what: &str) -> Result<Zip, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("{} 열기 실패: {}", what, e))?;
    zip::ZipArchive::new(f).map_err(|_| format!("{} 열기 실패: BadZipFile", what))
}

// 항목 이름 목록
pub fn names(z: &Zip) -> Vec<String> {
    z.file_names().map(|s| s.to_string()).collect()
}

//------------------------------------------------------------------
// ZIP 항목 하나를 XML 트리로 (없거나 깨졌으면 None)
//------------------------------------------------------------------
pub fn parse(z: &mut Zip, name: &str) -> Option<Node> {
    let mut e = z.by_name(name).ok()?;
    let mut b = vec![];
    e.read_to_end(&mut b).ok()?;
    xmldom::parse(&b).ok()
}

// 속성(지역 이름)
pub fn attr<'a>(el: Option<&'a Node>, name: &str) -> Option<&'a str> {
    el.and_then(|e| e.get(name))
}

// 자식(지역 이름)
pub fn child<'a>(el: Option<&'a Node>, name: &str) -> Option<&'a Node> {
    el.and_then(|e| e.child(name))
}

// posixpath.normpath — "a/b/../c" → "a/c"
pub fn normpath(p: &str) -> String {
    let mut out: Vec<&str> = vec![];
    for part in p.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if out.last().map(|l| *l != "..").unwrap_or(false) {
                    out.pop();
                } else {
                    out.push("..");
                }
            }
            x => out.push(x),
        }
    }
    let s = out.join("/");
    if s.is_empty() { ".".into() } else { s }
}

//------------------------------------------------------------------
// 부품의 관계 표 — {Id: (종류 끝 이름, 대상 전체 경로)}
//=> 외부 링크(TargetMode=External)는 뺀다. 문서 순서를 지킨다.
//------------------------------------------------------------------
pub fn rels(z: &mut Zip, part: &str) -> Vec<(String, (String, String))> {
    let (folder, base) = match part.rfind('/') {
        Some(i) => (&part[..i], &part[i + 1..]),
        None => ("", part),
    };
    let rel_path = if folder.is_empty() { format!("_rels/{}.rels", base) } else { format!("{}/_rels/{}.rels", folder, base) };
    let mut out: Vec<(String, (String, String))> = vec![];
    let root = match parse(z, &rel_path) {
        Some(r) => r,
        None => return out,
    };
    for r in &root.children {
        if r.tag != "Relationship" {
            continue;
        }
        let target = r.get("Target").unwrap_or("");
        if r.get("TargetMode") == Some("External") {
            continue;
        }
        let full = if target.starts_with('/') {
            target.trim_start_matches('/').to_string()
        } else if folder.is_empty() {
            normpath(target)
        } else {
            normpath(&format!("{}/{}", folder, target))
        };
        let kind = r.get("Type").unwrap_or("").rsplit('/').next().unwrap_or("").to_string();
        let id = r.get("Id").unwrap_or("").to_string();
        // 같은 Id 가 또 나오면 자리는 그대로 두고 값만 바꾼다(파이썬 dict 와 같다)
        match out.iter_mut().find(|(k, _)| *k == id) {
            Some(slot) => slot.1 = (kind, full),
            None => out.push((id, (kind, full))),
        }
    }
    out
}

static CELL_REF: Lazy<Regex> = Lazy::new(|| Regex::new(r"^([A-Z]+)(\d+)$").unwrap());

//------------------------------------------------------------------
// "B3" → (2, 1)  (0부터 행, 열)
//------------------------------------------------------------------
pub fn cell_ref(r: &str) -> Option<(i64, i64)> {
    let up = r.to_uppercase();
    let m = CELL_REF.captures(&up)?;
    let mut col = 0i64;
    for ch in m.get(1).unwrap().as_str().chars() {
        col = col * 26 + (ch as i64 - 64);
    }
    let row: i64 = m.get(2).unwrap().as_str().parse().ok()?;
    Some((row - 1, col - 1))
}

// 파이썬 str.isdigit() 근사(아스키 숫자만, 비어 있지 않음)
pub fn isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit())
}

// 이름 끝 숫자
pub fn num_key(name: &str, re: &Regex) -> i64 {
    re.captures(name).and_then(|c| c.get(1)).and_then(|m| m.as_str().parse().ok()).unwrap_or(0)
}
