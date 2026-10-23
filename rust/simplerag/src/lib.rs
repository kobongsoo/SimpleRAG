//------------------------------------------------------------------
// SimpleRAG Rust 판 라이브러리
//=> Python simplerag 패키지와 같은 구성으로 모듈을 나눴다.
//   설정(config) → 추출(legacy·docmodel·extract) → 청킹(chunk) → 임베딩(embed) →
//   저장(store·bm25) → 검색(hybrid·rerank·parent) → 생성(llama·generate) → 명령(cli)
//------------------------------------------------------------------

pub mod app;
pub mod bm25;
pub mod chunk;
pub mod cli;
pub mod commands;
pub mod config;
pub mod docmodel;
pub mod embed;
pub mod extract;
pub mod generate;
pub mod htmlparse;
pub mod indexer;
pub mod legacy;
pub mod pdfium;
pub mod pickle;
pub mod pyutil;
pub mod retrieve;
pub mod store;
pub mod structure;
pub mod tok;
pub mod xlsdate;
pub mod xmldom;
