//------------------------------------------------------------------
// 블록 모델 (Python docmodel/blocks.py)
//=> 추출 결과를 '한 덩어리 문자열'이 아니라 종류·제목 수준·쪽이 붙은 블록 목록으로 담는다.
//   포맷별 추출기는 모두 이 모양으로 결과를 내고, 청킹기는 이 모양만 받는다.
//------------------------------------------------------------------

use serde_json::{json, Map, Value};

pub const HEADING: &str = "heading";
pub const PARAGRAPH: &str = "paragraph";
pub const LIST_ITEM: &str = "list_item";
pub const TABLE: &str = "table";
pub const CAPTION: &str = "caption";
pub const CODE: &str = "code";
pub const HEADER: &str = "header";
pub const FOOTER: &str = "footer";
pub const PAGE_NUMBER: &str = "page_number";
pub const TOC: &str = "toc";
pub const NOTE: &str = "note";

// 청크에 넣지 않는 종류(진단 JSON 에는 남는다)
pub fn excluded(t: &str) -> bool {
    matches!(t, HEADER | FOOTER | PAGE_NUMBER | TOC)
}

pub const SRC_PARSER: &str = "parser";
pub const SRC_INFERRED: &str = "inferred";
pub const SRC_REGEX: &str = "regex";

//------------------------------------------------------------------
// 블록 하나
// -필드: kind        = 블록 종류(위 상수)
// -필드: text        = 블록 글자(표는 후처리에서 렌더링 글이 채워진다)
// -필드: level       = 제목 수준 1~9 / 목록 깊이. 그 밖 0
// -필드: rows        = 표만 — 행 × 칸 글자
// -필드: header_rows = 표만 — 머리글 행 수
// -필드: page        = 시작 쪽·슬라이드·시트 번호(1부터)
// -필드: path        = 속한 제목 경로(절 조립에서 채움)
// -필드: source      = parser | inferred | regex
// -필드: synthetic   = 문서에 없는 글자(제목 없는 슬라이드의 "슬라이드 3" 등)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Block {
    pub kind: &'static str,
    pub text: String,
    pub level: i64,
    pub rows: Option<Vec<Vec<String>>>,
    pub header_rows: usize,
    pub page: Option<i64>,
    pub path: Vec<String>,
    pub source: &'static str,
    pub synthetic: bool,
}

impl Block {
    // 기본값으로 블록 만들기
    pub fn new(kind: &'static str, text: impl Into<String>) -> Block {
        Block {
            kind,
            text: text.into(),
            level: 0,
            rows: None,
            header_rows: 0,
            page: None,
            path: vec![],
            source: SRC_PARSER,
            synthetic: false,
        }
    }
    pub fn lv(mut self, level: i64) -> Block {
        self.level = level;
        self
    }
    pub fn pg(mut self, page: Option<i64>) -> Block {
        self.page = page;
        self
    }
    pub fn src(mut self, s: &'static str) -> Block {
        self.source = s;
        self
    }
    // 표 블록
    pub fn table(rows: Vec<Vec<String>>, header_rows: usize) -> Block {
        let mut b = Block::new(TABLE, "");
        b.rows = Some(rows);
        b.header_rows = header_rows;
        b
    }

    //------------------------------------------------------------------
    // 진단 JSON 용 사전 — 기본값 필드는 뺀다
    //------------------------------------------------------------------
    pub fn to_json(&self) -> Value {
        let mut d = Map::new();
        d.insert("type".into(), json!(self.kind));
        d.insert("text".into(), json!(self.text));
        if self.level != 0 {
            d.insert("level".into(), json!(self.level));
        }
        if let Some(r) = &self.rows {
            d.insert("rows".into(), json!(r));
            d.insert("header_rows".into(), json!(self.header_rows));
        }
        if let Some(p) = self.page {
            d.insert("page".into(), json!(p));
        }
        if !self.path.is_empty() {
            d.insert("path".into(), json!(self.path));
        }
        if self.source != SRC_PARSER {
            d.insert("source".into(), json!(self.source));
        }
        if self.synthetic {
            d.insert("synthetic".into(), json!(true));
        }
        Value::Object(d)
    }
}

//------------------------------------------------------------------
// 문서 하나
// -필드: format   = docx | pptx | xlsx | hwpx | html | md | ipynb | doc | pdf | legacy
// -필드: title    = 문서 제목(파일명에서 확장자 뗀 것)
// -필드: blocks   = 블록 목록(읽기 순서)
// -필드: raw_text = 폴백 경로의 평문(블록 경로면 None)
// -필드: pages    = 쪽·슬라이드·시트 수
// -필드: warnings = 경고 문장
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct Document {
    pub format: String,
    pub title: String,
    pub blocks: Vec<Block>,
    pub raw_text: Option<String>,
    pub pages: Option<i64>,
    pub warnings: Vec<String>,
}

impl Document {
    pub fn new(format: &str, title: &str) -> Document {
        Document { format: format.into(), title: title.into(), ..Default::default() }
    }

    // 블록 경로인가(폴백 평문이 아니면)
    pub fn is_blocks(&self) -> bool {
        self.raw_text.is_none()
    }

    //------------------------------------------------------------------
    // 문서 전체 글자 — 블록 경로면 합성이 아닌 모든 블록 글자를 줄로 잇는다
    //------------------------------------------------------------------
    pub fn full_text(&self) -> String {
        if let Some(r) = &self.raw_text {
            return r.clone();
        }
        self.blocks.iter().filter(|b| !b.text.is_empty() && !b.synthetic).map(|b| b.text.as_str())
            .collect::<Vec<_>>().join("\n")
    }

    // 진단 JSON
    pub fn to_json(&self) -> Value {
        json!({
            "format": self.format, "title": self.title, "pages": self.pages,
            "warnings": self.warnings,
            "raw_text": self.raw_text,
            "blocks": self.blocks.iter().map(|b| b.to_json()).collect::<Vec<_>>(),
        })
    }
}
