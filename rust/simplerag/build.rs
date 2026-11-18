//! 실행 파일에 버전 정보(VERSIONINFO) 리소스를 넣는다.
//! 탐색기 → 속성 → 자세히 의 파일 버전·제품 이름·제품 버전·저작권·언어가 여기서 나온다.
//! 버전 숫자는 Cargo.toml 의 version 을 그대로 쓰므로 버전을 올리면 따라 바뀐다.
use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=Cargo.toml");
    // 윈도 대상일 때만 리소스를 만든다
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let rc = version_rc(
        "SimpleRAG — 로컬 문서 검색·질의응답 엔진",
        "SimpleRAG",
        "simplerag-rs",
        "simplerag-rs.exe",
    );
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("version.rc");
    // rc.exe 가 한글을 바르게 읽도록 UTF-16LE(BOM 포함)로 쓴다
    fs::write(&out, utf16le_with_bom(&rc)).unwrap();
    let _ = embed_resource::compile(&out, embed_resource::NONE);
}

// VERSIONINFO 블록 문자열을 만든다 — 언어는 한국어(0x0412), 코드페이지는 유니코드(1200)
fn version_rc(desc: &str, product: &str, internal: &str, original: &str) -> String {
    let ver = env::var("CARGO_PKG_VERSION").unwrap();
    let n = |k: &str| env::var(k).unwrap_or_else(|_| "0".into());
    let (ma, mi, pa) = (n("CARGO_PKG_VERSION_MAJOR"), n("CARGO_PKG_VERSION_MINOR"), n("CARGO_PKG_VERSION_PATCH"));
    format!(
        r#"1 VERSIONINFO
FILEVERSION {ma},{mi},{pa},0
PRODUCTVERSION {ma},{mi},{pa},0
FILEOS 0x40004
FILETYPE 0x1
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "041204B0"
    BEGIN
      VALUE "CompanyName", "kobongsoo"
      VALUE "FileDescription", "{desc}"
      VALUE "FileVersion", "{ver}"
      VALUE "InternalName", "{internal}"
      VALUE "LegalCopyright", "Copyright (c) 2026 kobongsoo. MIT License."
      VALUE "OriginalFilename", "{original}"
      VALUE "ProductName", "{product}"
      VALUE "ProductVersion", "{ver}"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x0412, 1200
  END
END
"#
    )
}

// 문자열을 UTF-16LE 바이트로 바꾸고 앞에 BOM 을 붙인다
fn utf16le_with_bom(s: &str) -> Vec<u8> {
    let mut v = vec![0xFF, 0xFE];
    for u in s.encode_utf16() {
        v.extend_from_slice(&u.to_le_bytes());
    }
    v
}
