//------------------------------------------------------------------
// SimpleRAG 전용 구조화 추출기 (Python docmodel/__init__.py · extract.py)
//=> 파일 하나를 Document(블록 목록)로 만든다.
//    1) 확장자와 파일 머리(매직 바이트)로 파서를 고른다
//    2) 파서가 있으면 파서 → 후처리(postprocess)
//    3) 파서가 없거나 실패하면 기존 추출기 평문을 raw_text 로 담는다(legacy 폴백)
//------------------------------------------------------------------

pub mod assemble;
pub mod blocks;
pub mod doc_bin;
pub mod docx;
pub mod html_md;
pub mod hwpx;
pub mod ooxml;
pub mod pdf;
pub mod postprocess;
pub mod pptx;
pub mod tables;
pub mod xlsx;

use std::io::Read;

use blocks::Document;

use crate::pyutil;

const ZIP_MAGIC: &[u8] = b"PK\x03\x04";
const OLE_MAGIC: &[u8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1";
const PDF_MAGIC: &[u8] = b"%PDF";

type Parser = fn(&str, &str) -> Result<Document, String>;

// 확장자 → (파서, 기대하는 파일 머리)
fn parser_for(ext: &str) -> Option<(Parser, Option<&'static [u8]>)> {
    Some(match ext {
        ".docx" => (docx::extract as Parser, Some(ZIP_MAGIC)),
        ".pptx" => (pptx::extract, Some(ZIP_MAGIC)),
        ".xlsx" | ".xlsm" => (xlsx::extract, Some(ZIP_MAGIC)),
        ".hwpx" => (hwpx::extract, Some(ZIP_MAGIC)),
        ".doc" => (doc_bin::extract, Some(OLE_MAGIC)),
        ".pdf" => (pdf::extract, Some(PDF_MAGIC)),
        ".html" | ".htm" => (html_md::extract_html, None),
        ".md" | ".markdown" => (html_md::extract_md, None),
        ".ipynb" => (html_md::extract_ipynb, None),
        _ => return None,
    })
}

//------------------------------------------------------------------
// 블록 경로로 처리하는 확장자인가
//------------------------------------------------------------------
pub fn supports(path: &str) -> bool {
    parser_for(&pyutil::ext_lower(path)).is_some()
}

// 파일 머리가 기대한 바이트인가
fn has_magic(path: &str, magic: &[u8]) -> bool {
    let mut buf = vec![0u8; magic.len()];
    match std::fs::File::open(path).and_then(|mut f| f.read_exact(&mut buf)) {
        Ok(_) => buf == magic,
        Err(_) => false,
    }
}

//------------------------------------------------------------------
// 문서 하나 추출 (핵심)
//
// -in: path  = 파일 경로
// -in: title = 문서 제목(None 이면 파일명에서 확장자를 뗀 것)
//
// -out: Ok(Document) — 블록 경로 또는 폴백 평문
// -out: error = 폴백 추출까지 실패하면 Err(사유)
//------------------------------------------------------------------
pub fn extract_document(path: &str, title: Option<&str>) -> Result<Document, String> {
    let title = title.map(|t| t.to_string()).unwrap_or_else(|| pyutil::splitext(pyutil::basename(path)).0.to_string());
    let ext = pyutil::ext_lower(path);
    let mut warnings = vec![];
    match parser_for(&ext) {
        Some((parser, magic)) if magic.map(|m| has_magic(path, m)).unwrap_or(true) => {
            // 파서 안에서 나는 패닉도 실패로 보고 폴백한다(한 문서가 인덱싱을 멈추지 않게)
            let r = std::panic::catch_unwind(|| parser(path, &title));
            match r {
                Ok(Ok(mut doc)) => {
                    postprocess::finalize(&mut doc);
                    return Ok(doc);
                }
                Ok(Err(e)) => warnings.push(format!("구조 파서 실패 → 기존 추출: ValueError: {}", pyutil::head(&e, 120))),
                Err(_) => warnings.push("구조 파서 실패 → 기존 추출: 내부 오류".to_string()),
            }
        }
        Some(_) => warnings.push(format!("확장자는 {} 이지만 파일 머리가 다름 → 기존 추출", ext)),
        None => {}
    }
    let (text, _) = crate::extract::extract_text(path, true)?;
    let mut doc = Document::new("legacy", &title);
    doc.raw_text = Some(text);
    doc.warnings = warnings;
    Ok(doc)
}

//------------------------------------------------------------------
// 진단용 블록 JSON 저장 — <폴더>/<이름>.blocks.json
//------------------------------------------------------------------
pub fn dump_json(doc: &Document, out_dir: &str, name: &str) -> std::io::Result<String> {
    std::fs::create_dir_all(out_dir)?;
    let path = pyutil::join(out_dir, &format!("{}.blocks.json", name));
    let s = serde_json::to_string_pretty(&doc.to_json()).unwrap_or_default();
    std::fs::write(&path, s)?;
    Ok(path)
}
