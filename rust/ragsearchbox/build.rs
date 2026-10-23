//! 실행 파일에 아이콘(리소스 번호 1)을 넣는다 — 탐색기 아이콘·트레이·창 아이콘이 이것을 쓴다.
fn main() {
    let _ = embed_resource::compile("app.rc", embed_resource::NONE);
}
