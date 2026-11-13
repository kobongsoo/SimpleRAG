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
pub mod pdftable;
pub mod postprocess;
pub mod pptx;
pub mod tables;
pub mod xlsx;

use std::io::Read;

use blocks::{Block, Document};

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
    parser_for(&pyutil::ext_lower(path)).is_some() || (crate::ocr::image::is_image(path) && crate::ocr::images_enabled())
}

//------------------------------------------------------------------
// 그림 파일 → 문서 (OCR 설계서 §4-4)
//=> 그림 파일은 글자층이 없으므로 OCR 캐시만 본다.
//    1) 캐시에 있으면 쪽(TIFF 는 여러 쪽)마다 OCR 줄을 줄 묶기해 본문 문단 블록으로
//    2) 없으면 빈 문서 + "OCR 대기" 표시 — 색인이 대기열에 넣고, 끝나면 다시 반영한다
//   글자 없는 그림(아이콘·사진)은 캐시에 빈 쪽으로 남아 청크 0개 문서가 된다(실패 아님).
//
// -in: path = 그림 파일 / title = 문서 제목
//
// -out: Ok(Document)
// -out: error = 파일 해시를 못 읽으면 Err(오류문)
//------------------------------------------------------------------
fn extract_image(path: &str, title: &str) -> Result<Document, String> {
    let mut doc = Document::new("image", title);
    let mut info = crate::ocr::DocOcr { whole: true, ..Default::default() };
    let hash = crate::indexer::content_hash(path).map_err(|e| format!("그림 파일 읽기 실패: {}", e))?;
    match crate::ocr::load_cache(&hash) {
        Some(c) => {
            doc.pages = Some(c.pages.len() as i64);
            for (no, lines) in &c.pages {
                let boxes: Vec<(String, [f32; 4])> = lines.iter().map(|l| (l.t.clone(), l.b)).collect();
                for (text, _, _) in crate::ocr::group_rows(&boxes) {
                    let t = pyutil::strip(&text);
                    if !t.is_empty() {
                        doc.blocks.push(Block::new(blocks::PARAGRAPH, t).pg(Some(*no as i64)).src(blocks::SRC_OCR));
                    }
                }
                info.done.push(*no);
            }
        }
        None => info.pending.push(1),
    }
    crate::ocr::note(path, info);
    Ok(doc)
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
    // 그림 파일 — OCR 이 켜져 있을 때만(꺼져 있으면 아래 기존 추출로 가서 실패한다)
    if crate::ocr::image::is_image(path) && crate::ocr::images_enabled() {
        let mut doc = extract_image(path, &title)?;
        postprocess::finalize(&mut doc);
        return Ok(doc);
    }
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
// 문서 추출 — PDF 는 고른 쪽만 (비슷한 문서 찾기의 큰 바깥 PDF)
//=> PDF 가 아니거나 keep 이 None 이면 extract_document 와 같다. PDF 구조 추출이 실패하면 오류를 그대로 돌려준다
//   (기존 추출로 폴백하면 전체 쪽을 다시 읽어 고른 쪽만 읽는 뜻이 없어진다).
//
// -in: path · title = extract_document 와 같음 / keep = 쪽 번호(0부터), 전체 쪽 수 → 읽을지
//
// -out: Ok(Document)
// -out: error = 추출 실패 문자열
//------------------------------------------------------------------
pub fn extract_document_keep(path: &str, title: Option<&str>, keep: Option<&dyn Fn(usize, usize) -> bool>) -> Result<Document, String> {
    let keep = match keep {
        Some(k) if pyutil::ext_lower(path) == ".pdf" => k,
        _ => return extract_document(path, title),
    };
    let title = title.map(|t| t.to_string()).unwrap_or_else(|| pyutil::splitext(pyutil::basename(path)).0.to_string());
    // 파서 안의 패닉도 실패로 — 워커가 멈추지 않게
    let mut doc = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| pdf::extract_keep(path, &title, Some(keep))))
        .map_err(|_| "PDF 추출 중 내부 오류".to_string())??;
    postprocess::finalize(&mut doc);
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
