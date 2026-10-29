//------------------------------------------------------------------
// 문서 메타데이터 (메타데이터 검색 설계서 plan/메타데이터검색_설계서_20261029.html)
//=> 문서 속성(사람 · 날짜 · 제목 · 형식 · 출처 · 유형)을 뽑아 정규화하고 meta.sqlite 에 둔다.
//   Python simplerag.meta 패키지를 그대로 옮긴 것이다 — 두 판이 같은 meta.sqlite 를 읽고 쓴다
//   (같은 표 구조 · 같은 raw_json 모양 · 같은 정규화 값).
//
//   extract   — 포맷별 원본 값(RawMeta) 추출 + 매직 바이트 형식 판정 (§6 · §10-1)
//   normalize — 원본 값 → 정규화 값(DocMeta): 사람 · 날짜 · 제목 · 출처 · 유형 (§7~§10)
//   rules     — 쓰레기 이름 · 원본 프로그램 · 받은 경로 · 유형어 사전 (meta_rules.yaml 로 덧붙임)
//   store     — meta.sqlite 저장 · 템플릿 상속 판정 · 다시 정규화 (§11)
//   hook      — 색인 연동(인덱서가 문서를 넣고 · 옮기고 · 베끼고 · 뺄 때) (§11-4)
//   people    — 사람 별칭 사전 자동 제안 + 승인 (§7-2)
//   cmd       — simplerag-rs meta 명령(Python cmd_meta 와 같은 출력)
//------------------------------------------------------------------

pub mod cmd;
pub mod extract;
pub mod hook;
pub mod normalize;
pub mod people;
pub mod query;
pub mod pyfmt;
pub mod rules;
pub mod store;
