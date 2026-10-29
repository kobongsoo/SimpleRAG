//------------------------------------------------------------------
// CLI 명령들 (Python cli.py)
//=> index / ask / chat / extract / search / clear / status / backend / warmup (+ Rust 판 import).
//   ask·chat 은 2단계 응답을 그대로 화면에 옮긴다 — 근거를 먼저 찍고 그 아래 답을 흘린다.
//   RAGSearchBox 가 읽는 줄 모양(질문> · ── 근거 · ── 답변 · ── 소요 · @index · @ev)은 Python 판과 같다.
//------------------------------------------------------------------

use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use serde_json::{json, Map, Value};

use crate::app::{App, Event, NO_DOCS_IN_FOLDER, NO_RELEVANT, UPLOAD_MISSING};
use crate::commands;
use crate::config::{self, cfg};
use crate::generate::backend;
use crate::generate::prompts::{clean_answer, recite, short_clean, short_echo_hold, soft_clean, strip_instruction_echo};
use crate::indexer;
use crate::pyutil;
use crate::retrieve::tget;

// Ctrl+C 가 눌렸나(생성 중이면 그 답변만 취소)
pub static INTERRUPT: AtomicBool = AtomicBool::new(false);

// 표준 출력에 쓰고 바로 내보낸다(파이프에서도 줄 단위로 보이게)
pub fn out(s: &str) {
    let mut o = std::io::stdout().lock();
    let _ = o.write_all(s.as_bytes());
    let _ = o.flush();
}
pub fn outln(s: &str) {
    out(&format!("{}\n", s));
}
// 진행 안내는 stderr(답변 출력에 섞이지 않게)
pub fn log(s: &str) {
    let mut e = std::io::stderr().lock();
    let _ = writeln!(e, "{}", s);
    let _ = e.flush();
}

//------------------------------------------------------------------
// 명령 인자
//------------------------------------------------------------------
#[derive(Debug, Default, Clone)]
pub struct Args {
    pub cmd: String,
    pub dir: Option<String>,
    pub rebuild: bool,
    pub no_recursive: bool,
    pub allow_delete: bool,
    pub max_add: Option<usize>,
    pub query: Option<String>,
    pub top_k: Option<usize>,
    pub max_tokens: Option<usize>,
    pub folder: Option<String>,
    pub model: Option<String>,
    pub precise: bool,
    pub no_stream: bool,
    pub path: Option<String>,
    pub chars: Option<usize>,
    pub blocks: bool,
    pub dump: Option<String>,
    pub doc: Option<String>,
    pub yes: bool,
    pub verbose: bool,
    pub reprobe: bool,
    pub from: Option<String>,
    pub out: Option<String>,
    pub no_llm: bool,
    pub group: bool,
}

fn model_mode(alias: &str) -> String {
    match alias {
        config::FAST_GEN_MODEL => "빠름".into(),
        config::PRECISE_GEN_MODEL => "기본".into(),
        config::KOREAN_GEN_MODEL => "한국어".into(),
        config::PRECISION_GEN_MODEL => "정밀".into(),
        a => a.into(),
    }
}

// --precise / --model 고르기
fn pick_model(a: &Args) -> Result<Option<String>, String> {
    if a.precise {
        if let Some(m) = &a.model {
            if m != config::PRECISE_GEN_MODEL {
                return Err(format!("--precise 와 --model {} 를 함께 쓸 수 없습니다", m));
            }
        }
        return Ok(Some(config::PRECISE_GEN_MODEL.into()));
    }
    Ok(a.model.clone())
}

//------------------------------------------------------------------
// 백그라운드 리랭커 적재가 끝나면 stderr 로 한 줄 알리기(chat 전용)
//------------------------------------------------------------------
fn announce_rerank(app: &std::sync::Arc<App>) {
    let app = app.clone();
    std::thread::spawn(move || match app.wait_rerank() {
        Some(Ok(ms)) => log(&format!("리랭커 적재 완료 ({:.0}ms)", ms)),
        Some(Err(e)) => log(&format!("리랭커 적재 실패 — {}", e)),
        None => log("리랭커 적재 완료 (0ms)"),
    });
}

// ── index ─────────────────────────────────────────────────

pub fn cmd_index(a: &Args) -> i32 {
    let app = App::new(None);
    app.warmup(true, &mut |_| {}, Some(false));
    let dir = a.dir.clone().unwrap_or_default();
    let mut lg = |m: &str| outln(m);
    let r = indexer::index_folder(&dir, &app.embedder, &app.store, &app.bm25, a.rebuild, &mut lg,
                                  !a.no_recursive, a.allow_delete, a.max_add);
    let code = match r {
        Err(e) => {
            log(&format!("\n❌ {}", e));
            let res = obj(json!({"op": "index", "ok": false, "why": pyutil::head(&e, 200)}));
            outln(&commands::format_result(&res, "@index-summary "));
            1
        }
        Ok(s) => {
            let res = obj(json!({
                "files": s.files, "changed": s.changed, "added": s.c("added"), "modified": s.c("modified"),
                "same": s.c("same"), "moved": s.c("moved"), "copied": s.c("copied"), "removed": s.removed,
                "delete_blocked": s.delete_blocked, "held_new": s.held_new, "chunks": s.chunks,
                "sec": s.sec, "op": "index", "ok": true, "failed": s.failed.len(),
            }));
            outln(&commands::format_result(&res, "@index-summary "));
            outln(&format!("\n문서 {}건 / 추가 {}건 · 수정 {}건 · 내용 같음 {}건 · 이동 {}건 · 복사 {}건 · 삭제 {}건 / 청크 {}개 / {:.1}s",
                s.files, s.c("added"), s.c("modified"), s.c("same"), s.c("moved"), s.c("copied"), s.removed,
                pyutil::comma(s.chunks), s.sec));
            if !s.failed.is_empty() {
                outln(&format!("실패 {}건 (실패한 문서는 옛 버전이 그대로 남아 있습니다)", s.failed.len()));
            }
            if !s.delete_blocked.is_empty() {
                log(&format!("⚠️ 삭제를 멈췄습니다: {}. 정말 지운 것이면 --allow-delete 를 붙여 다시 실행하세요.", s.delete_blocked));
                3
            } else {
                0
            }
        }
    };
    app.close();
    code
}

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

// ── ask / chat 출력 ──────────────────────────────────────

//------------------------------------------------------------------
// 스트리밍 출력 버퍼 — 개행·40자·60ms 마다 내보내고 끝은 붙들어 둔다(되뱉기 제거용, Python StreamWriter 와 같다)
//=> 예전에는 끝 90자를 늘 붙들어 답의 첫 90자가 모일 때까지 화면이 비었다(화면 첫 글자가 약 3.5초 늦음).
//   지금은 같은 걸러 내기를 지키면서 덜 붙든다.
//    1) 붙드는 길이 56자 — 가장 긴 되뱉기 패턴 "…(40자) 바탕으로 답변하겠습니"(안 끝난 꼴)가 52자
//    2) 문장 끝(. ! ? 뒤 공백, 줄바꿈)까지는 바로 내보낸다 — 되뱉기 패턴은 문장부호·줄바꿈을 넘지 않는다
//    3) 내보낼 조각까지 정리해 봐서 글자가 바뀌면 마무리(finish)에 맡긴다.
//       굵게 별표·없는 인용 번호는 soft_clean 으로 걷어 내고 내보낸다
//   위 1~3 은 되뱉기 거르기를 켰을 때(generation.echo_filter: true)만이다. 기본(끔)은 붙들지 않고 바로
//   내보낸다(hold 0, 긴 되뱉기 검사 없음) — 짧은 되뱉기·인용 덩어리의 앞부분일 수 있는 꼬리와
//   짝 없는 "**" · 닫히지 않은 "[" 만 잠깐 붙든다(설계서 ②③).
// -필드: n_ev = 근거 개수(정리 때 없는 인용 번호를 가린다) — 근거 사건에서 정해진다
//------------------------------------------------------------------
struct StreamWriter {
    min_chars: usize,
    interval: f64,
    hold: usize,
    fin: bool,
    pending: String,
    emitted: String,
    n: usize,
    last: Instant,
    n_ev: Option<usize>,
    // 되뱉기 거르기(켜면 hold 56 + 내보내기 전 되뱉기 검사, 끄면 hold 0)
    echo: bool,
    // 짧은 되뱉기 거르기(설계서 ②③, 끔일 때 켠다) — 다 온 짧은 잡음은 지우고, 그 앞부분일 수 있는
    //   꼬리(보통 2~6자, 최대 40자)만 붙든다
    short: bool,
    // 인용 번호 바로잡기(설계서 ⑤)용 근거 본문 — 근거 사건에서 채운다. 문장 끝 뒤 번호("…다. [1]")는
    //   문장이 다 쓰인 뒤에 오므로 내보내기 직전에 바로잡을 수 있다
    evidence: Option<Vec<String>>,
    // 시험용 — Some 이면 화면 대신 여기에 모은다
    capture: Option<String>,
}

impl StreamWriter {
    fn new() -> Self {
        StreamWriter { min_chars: 40, interval: 0.06, hold: 56, fin: false, pending: String::new(), emitted: String::new(), n: 0, last: Instant::now(), n_ev: None, echo: true, short: false, evidence: None, capture: None }
    }
    // 설정에 맞춘 출력기 — echo=false(기본)면 꼬리를 붙들지 않고 되뱉기 검사도 하지 않는다
    fn with_echo(echo: bool) -> Self {
        let mut w = StreamWriter::new();
        w.echo = echo;
        if !echo {
            w.hold = 0;
            w.short = true;
        }
        w
    }
    // 지금 내보내도 되는 앞부분(원문) — 위 1~3
    fn ready_part(&self) -> String {
        let p = &self.pending;
        let plen = pyutil::clen(p);
        let mut out: &str = if self.short {
            // 짧은 되뱉기의 앞부분일 수 있는 꼬리만 붙든다(없으면 전부 내보낸다)
            pyutil::head(p, plen.saturating_sub(short_echo_hold(p, 40)))
        } else {
            let by_hold = pyutil::head(p, plen.saturating_sub(self.hold)).len();
            &p[..by_hold.max(last_sentence_end(p))]
        };
        // 끝의 "*" 는 붙든다 — 모델이 "**" 를 "*" 두 토큰으로 내면 짝 세기가 어긋난다(골든셋 q025)
        out = out.trim_end_matches('*');
        // 짝이 안 맞는 "**" 가 있으면 그 앞까지 — 닫는 별표가 오면 함께 걷어 낸다
        if out.matches("**").count() % 2 == 1 {
            out = &out[..out.rfind("**").unwrap()];
        }
        // 닫히지 않은 "[" 는 인용 번호의 앞부분일 수 있다
        if let Some(b) = out.rfind('[') {
            if !out[b..].contains(']') {
                out = &out[..b];
            }
        }
        if out.is_empty() || !self.echo {
            return out.to_string();
        }
        let need = no_space(&format!("{}{}", self.emitted, soft_clean(out, self.n_ev)));
        // 받아 둔 글 전체를 정리해 보고 내보낼 앞부분이 그대로인지 본다 — 뒤를 봐야 판정되는
        //   되뱉기("근거 [1]" 의 '근거' 는 뒤에 [1] 이 와야 지운다, 골든셋 q096)도 놓치지 않게
        let all = format!("{}{}", self.emitted, soft_clean(p, self.n_ev));
        if !no_space(&strip_instruction_echo(&all, self.n_ev)).starts_with(&need) {
            return String::new();
        }
        out.to_string()
    }
    fn write(&mut self, text: &str) {
        self.pending.push_str(text);
        self.n += pyutil::clen(text);
        if text.contains('\n') || self.n >= self.min_chars || self.last.elapsed().as_secs_f64() >= self.interval {
            self.flush();
        }
    }
    // 내보낼 조각의 문장 끝 뒤 인용 번호 바로잡기 — 이미 나간 글 + 조각 + 붙든 글을 함께 보고(뒤에 낱말이
    //   오는지 알아야 문장 앞 번호와 가릴 수 있다) 조각 안의 번호만 고친다. 길이가 바뀌면 고치지 않는다
    fn recite_out(&self, out_s: String) -> String {
        let ev = match &self.evidence {
            Some(e) if !e.is_empty() => e,
            _ => return out_s,
        };
        let head = self.emitted.len();
        let full = format!("{}{}{}", self.emitted, out_s, self.pending);
        let (fixed, fixes) = recite(&full, ev, true, head);
        if fixes.is_empty() || fixed.len() != full.len() {
            return out_s;
        }
        fixed[head..head + out_s.len()].to_string()
    }
    fn flush(&mut self) {
        let out_s = if self.fin {
            std::mem::take(&mut self.pending)
        } else {
            if self.short {
                // 아직 화면에 안 나간 글에서 다 온 짧은 되뱉기는 미리 지운다(끝맺음이 와야 지우는 판)
                self.pending = short_clean(&self.pending, false);
            }
            let raw = self.ready_part();
            if raw.is_empty() {
                return;
            }
            self.pending = self.pending[raw.len()..].to_string();
            let soft = soft_clean(&raw, self.n_ev);
            self.recite_out(soft)
        };
        if out_s.is_empty() {
            return;
        }
        self.emitted.push_str(&out_s);
        match self.capture.as_mut() {
            Some(c) => c.push_str(&out_s),
            None => out(&out_s),
        }
        self.n = 0;
        self.last = Instant::now();
    }
    // 마무리 — 되뱉기를 지운 뒤 남은 꼬리를 내보낸다(이미 나간 글자와 공백 무시로 이어질 때만)
    //   final(생성기가 정한 남길 답)을 주면 그것을 기준으로 — 생성기가 반복·상한으로 잘라 낸 꼬리가
    //   아직 여기 붙들려 있으면 내보내지 않고 버린다. 이미 나간 뒤라면 더 붙이지 않는다
    fn finish(&mut self, n_ev: Option<usize>, final_text: Option<&str>) {
        self.fin = true;
        let full = match final_text {
            Some(f) => f.to_string(),
            None => format!("{}{}", self.emitted, self.pending),
        };
        let cleaned = clean_answer(&full, n_ev, self.echo);
        let mut tail = self.tail_after(&cleaned);
        if tail.is_none() && final_text.is_some() && no_space(&self.emitted).starts_with(&no_space(&cleaned)) {
            // 남길 답보다 이미 더 나갔다 — 되돌릴 수 없으니 더 붙이지 않는다
            tail = Some(String::new());
        }
        let tail = tail.or_else(|| Some(clean_answer(&self.pending, n_ev, self.echo)));
        if let Some(t) = tail {
            self.pending = t;
        }
        self.flush();
    }
    fn tail_after(&self, cleaned: &str) -> Option<String> {
        let need: Vec<char> = self.emitted.chars().filter(|c| !pyutil::is_space(*c)).collect();
        let cs: Vec<char> = cleaned.chars().collect();
        let (mut i, mut k) = (0, 0);
        while k < need.len() && i < cs.len() {
            if !pyutil::is_space(cs[i]) {
                if cs[i] != need[k] {
                    return None;
                }
                k += 1;
            }
            i += 1;
        }
        if k < need.len() { None } else { Some(cs[i..].iter().collect()) }
    }
}

// 마지막 문장 끝(바이트 위치, 없으면 0) — '.' '!' '?' 바로 뒤에 공백이 온 자리, 또는 줄바꿈 바로 뒤.
//   그 앞의 글은 되뱉기 패턴에 걸릴 수 없다. 소수점·글 끝의 마침표는 문장 끝으로 보지 않는다
fn last_sentence_end(s: &str) -> usize {
    let cs: Vec<(usize, char)> = s.char_indices().collect();
    for k in (0..cs.len()).rev() {
        let (i, c) = cs[k];
        if c == '\n' {
            return i + 1;
        }
        if matches!(c, '.' | '!' | '?') && cs.get(k + 1).map(|x| x.1.is_whitespace()).unwrap_or(false) {
            return i + 1;
        }
    }
    0
}

// 공백을 뺀 글자들 — 정리 함수가 공백을 접어도 같은 글로 보려고
fn no_space(s: &str) -> String {
    s.chars().filter(|c| !pyutil::is_space(*c)).collect()
}

// 소요 블록의 "답변모델 … · parent …" 한 줄
fn answer_setup_line(app: &App) -> String {
    let alias = app.generator.alias.clone();
    let parent = if cfg().parent_mode == "section" { format!("section(상한 {}토큰)", cfg().parent_max_tokens) } else { cfg().parent_mode.clone() };
    // 기본 모델(1.7B)은 모드 이름을 달지 않는다 — 정밀 모드(4B)·빠름·한국어처럼 바꾼 때만 보인다
    if alias == config::PRECISE_GEN_MODEL {
        return format!("답변모델 {} · parent {}", alias, parent);
    }
    format!("답변모델 {} ({}) · parent {}", alias, model_mode(&alias), parent)
}

//------------------------------------------------------------------
// 업로드 명령 처리 (/upload · /upload-remove · /upload-forget — Python cli._upload_command 와 같다)
//   인자는 ASCII JSON — {"conv", "path"} / {"conv", "name"} / {"conv"} 또는 {"all": true}.
//   /upload 진행은 stderr 에 "[upload] …" 줄로 낸다(RAGSearchBox 진행 막대용)
//------------------------------------------------------------------
fn upload_command(app: &App, cmd: &str, arg: &str) -> Value {
    let op = cmd.trim_start_matches('/');
    let req: Value = match serde_json::from_str(if arg.trim().is_empty() { "{}" } else { arg.trim() }) {
        Ok(v @ Value::Object(_)) => v,
        _ => return json!({"op": op, "ok": false, "why": format!("형식이 잘못됐습니다: {} {{\"conv\": …}}", cmd)}),
    };
    let conv = req.get("conv").and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
    let mut r = if cmd == "/upload-forget" {
        // 메모리 캐시만 비운다(폴더는 RAGSearchBox 가 지운다)
        let all = req.get("all").and_then(|v| v.as_bool()).unwrap_or(false);
        crate::uploads::forget(if all || conv.is_empty() { None } else { Some(&conv) });
        json!({"ok": true, "conv": conv})
    } else if conv.is_empty() {
        json!({"ok": false, "why": "대화 id(conv)가 없습니다"})
    } else if cmd == "/upload-remove" {
        crate::uploads::remove(&conv, req.get("name").and_then(|v| v.as_str()).unwrap_or(""))
    } else {
        crate::uploads::add(&conv, req.get("path").and_then(|v| v.as_str()).unwrap_or(""), &app.embedder)
    };
    r["op"] = json!(op);
    r
}

//------------------------------------------------------------------
// 메타데이터 조건 꼬리말 — timing.meta 의 칩 · 문서 수 · 완화 내역 (Python render_answer 와 같은 글자)
//
// -in: timing = 검색 시간 묶음
// -out: " · 조건 고봉수 (작성) · 엑셀(문서 14개) — 조건을 풀어 찾음: …" / 조건이 없으면 빈 문자열
//------------------------------------------------------------------
pub fn meta_scope(timing: &crate::retrieve::Timing) -> String {
    let m = match timing.get("meta") {
        Some(m) => m,
        None => return String::new(),
    };
    let chips: Vec<&str> = m["chips"].as_array().map(|a| a.iter().filter_map(|c| c["label"].as_str()).collect()).unwrap_or_default();
    let mut s = format!(" · 조건 {}", chips.join(" · "));
    if let Some(n) = m["docs"].as_i64() {
        s += &format!("(문서 {}개)", n);
    }
    let relaxed: Vec<&str> = m["relaxed"].as_array().map(|a| a.iter().filter_map(|v| v.as_str()).collect()).unwrap_or_default();
    if !relaxed.is_empty() {
        s += &format!(" — 조건을 풀어 찾음: {}", relaxed.join(", "));
    }
    s
}

//------------------------------------------------------------------
// 질의 1건 화면 출력 (ask·chat 공용) — true = 정상, false = 사용자 중단
//------------------------------------------------------------------
pub fn render_answer(app: &App, query: &str, top_k: Option<usize>, max_tokens: Option<usize>, stream: bool, folder: Option<&str>,
                     prev: Option<&str>, conv: Option<&str>, uploads: bool) -> bool {
    // 되뱉기 거르기는 설정을 따른다(기본 끔 — 켜면 첫 문장을 붙들어 화면 첫 글자가 약 2초 늦어진다)
    let mut writer = StreamWriter::with_echo(cfg().echo_filter);
    let mut parts = String::new();
    let mut n_ev: Option<usize> = None;
    let mut early = false;
    let mut interrupted = false;
    INTERRUPT.store(false, Ordering::SeqCst);
    let r = app.answer(query, top_k, max_tokens, folder, prev, conv, uploads, &mut |ev| {
        if early {
            return;
        }
        match ev {
            Event::Evidence(chunks, timing) => {
                n_ev = Some(chunks.len());
                writer.n_ev = n_ev;
                // 문장 끝 뒤 인용 번호를 내보내기 전에 바로잡으려고 근거 본문을 넘긴다(설계서 ⑤)
                writer.evidence = Some(chunks.iter().map(|c| c.text.clone()).collect());
                if folder.is_some() && timing.get("scope_docs").and_then(|v| v.as_i64()) == Some(0) {
                    outln(&format!("\n{}\n", NO_DOCS_IN_FOLDER));
                    early = true;
                    return;
                }
                if timing.get("upload_missing").and_then(|v| v.as_bool()).unwrap_or(false) {
                    // 올린 문서가 있던 대화인데 업로드 폴더가 없다 — 인덱싱 폴더로 넘어가지 않는다
                    outln(&format!("\n{}\n", UPLOAD_MISSING));
                    early = true;
                    return;
                }
                if timing.get("no_relevant").and_then(|v| v.as_bool()).unwrap_or(false) {
                    outln(&format!("\n{}\n", NO_RELEVANT));
                    early = true;
                    return;
                }
                let scope = if let Some(n) = timing.get("uploads") {
                    // 올린 문서에서 찾은 답 — 범위를 업로드 문서로 적는다
                    format!(" · 범위 업로드 문서(문서 {}건)", n)
                } else { match folder {
                    Some(f) => {
                        let base = pyutil::basename(pyutil::rstrip_chars(f, "\\/"));
                        format!(" · 범위 {}(문서 {}건)", if base.is_empty() { f } else { base },
                                timing.get("scope_docs").map(|v| v.to_string()).unwrap_or("?".into()))
                    }
                    None => String::new(),
                } };
                // 메타데이터 조건(메타데이터 검색 설계서 §1-1) — "고봉수 (작성) · 2025년 · 작성일(문서 12개)"
                let scope = scope + &meta_scope(&timing);
                outln(&format!("\n── 근거 {}건 ({:.0}ms){} ─────────────────────", chunks.len(), tget(&timing, "total_ms"), scope));
                let full = std::env::var("SIMPLERAG_EVIDENCE_FULL").map(|v| v == "1").unwrap_or(false);
                for (i, c) in chunks.iter().enumerate() {
                    let body = pyutil::strip(&c.text.replace('\n', " ")).to_string();
                    outln(&format!("  [{}] {}", i + 1, c.doc_name));
                    outln(&format!("      {}{}", pyutil::head(&body, 160), if pyutil::clen(&body) > 160 { "…" } else { "" }));
                    if full {
                        let v = json!({"no": i + 1, "path": c.doc_path, "text": c.text});
                        outln(&format!("  @ev {}", pyutil::json_ascii(&v, false)));
                    }
                }
                outln("\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──");
            }
            Event::Token(t) => {
                if INTERRUPT.load(Ordering::SeqCst) {
                    interrupted = true;
                    return;
                }
                if stream {
                    writer.write(&t);
                } else {
                    parts.push_str(&t);
                }
            }
            Event::Done(d) => {
                if stream {
                    // 문장 앞 번호는 이미 나갔으니 문장 끝 뒤 번호만 고친 답(answer_stream)으로 맞춘다
                    writer.finish(n_ev, Some(&d.answer_stream));
                } else {
                    out(pyutil::strip(&d.answer));
                }
                // 반복·상한으로 잘라 냈으면 본문 뒤에 표시(인용 번호 정리가 끝난 뒤라 따로 붙인다)
                if let Some(note) = d.note {
                    out(&format!(" {}", note));
                }
                if !stream {
                    outln("");
                }
                let t = &d.timing;
                outln("\n\n── 소요 ─────────────────────────────────");
                let rr = if t.contains_key("rerank_ms") { format!(" + 리랭킹 {:.0}", tget(t, "rerank_ms")) } else { String::new() };
                outln(&format!("  검색 {:.0}ms (임베딩 {:.0} + dense {:.0} + BM25 {:.0}{})",
                    tget(t, "total_ms"), tget(t, "embed_ms"), tget(t, "dense_ms"), tget(t, "bm25_ms"), rr));
                if let Some(e) = t.get("rerank_error").and_then(|v| v.as_str()) {
                    outln(&format!("  ⚠️ 리랭킹 실패 — RRF 순서로 답했습니다: {}", e));
                }
                outln(&format!("  첫 글자 {:.2}s / 완료 {:.2}s", d.ttft_s, d.total_s));
                if let Some(s) = t.get("synonyms").and_then(|v| v.as_array()).filter(|a| !a.is_empty()) {
                    // 동의어 사전으로 키워드 검색을 넓혔으면 무엇을 넓혔는지(동의어 설계서 §5)
                    outln(&format!("  검색어 넓힘: {}", s.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(", ")));
                }
                if t.get("multiturn").and_then(|v| v.as_bool()).unwrap_or(false) {
                    // 앞 질문을 검색어에 이어 붙였다(멀티턴)
                    outln("  앞 질문 이어 붙임");
                }
                outln(&format!("  {}", answer_setup_line(app)));
                if !d.cited.is_empty() {
                    // 바로잡은 번호가 있으면 무엇을 바꿨는지 함께 — 본문에 이미 나간 문장 앞 번호는 못 고쳤을 수 있다
                    let fx = d.cite_fixes.iter().map(|(a, b)| format!("[{}]→[{}]", a, b)).collect::<Vec<_>>().join(", ");
                    outln(&format!("  인용 근거: {}{}",
                        d.cited.iter().map(|n| format!("[{}]", n)).collect::<Vec<_>>().join(", "),
                        if fx.is_empty() { String::new() } else { format!(" (인용 번호 바로잡음: {})", fx) }));
                }
            }
        }
    });
    if let Err(e) = r {
        writer.finish(n_ev, None);
        log(&format!("\n오류: {}", e));
    }
    if interrupted {
        writer.finish(n_ev, None);
        outln("\n\n[중단됨]");
        return false;
    }
    true
}

pub fn cmd_ask(a: &Args) -> i32 {
    if a.query.as_deref().map(|q| q.is_empty()).unwrap_or(true) {
        return cmd_chat(a);
    }
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = App::new(model.as_deref());
    app.warmup(false, &mut |m| log(m), None);
    app.wait_rerank();
    let ok = render_answer(&app, a.query.as_deref().unwrap(), a.top_k, a.max_tokens, !a.no_stream, a.folder.as_deref(), None, None, false);
    app.close();
    if ok { 0 } else { 130 }
}

//------------------------------------------------------------------
// stdin 한 줄 — UTF-8 이 아니면 cp949 로 푼다(RAGSearchBox 는 cp949 로 보낸다)
//------------------------------------------------------------------
fn read_line() -> Option<String> {
    let mut buf = vec![];
    match std::io::stdin().lock().read_until(b'\n', &mut buf) {
        Ok(0) | Err(_) => None,
        Ok(_) => {
            while buf.last().map(|b| *b == b'\n' || *b == b'\r').unwrap_or(false) {
                buf.pop();
            }
            Some(match std::str::from_utf8(&buf) {
                Ok(s) => s.to_string(),
                Err(_) => encoding_rs::EUC_KR.decode(&buf).0.into_owned(),
            })
        }
    }
}

//------------------------------------------------------------------
// 대화형 명령 (워커) — 모델을 한 번 올리고 질문을 계속 받는다
//------------------------------------------------------------------
pub fn cmd_chat(a: &Args) -> i32 {
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = std::sync::Arc::new(App::new(model.as_deref()));
    let mut top_k = a.top_k;
    let mut rr_warned = false;
    let mut chat_folder: Option<String> = None;
    outln("모델 적재 중...");
    let warm = app.warmup(false, &mut |m| log(m), None);
    let count = app.store.stats().map(|s| s.0).unwrap_or(0);
    let alias = model.clone().unwrap_or_else(|| cfg().default_gen_model.clone());
    // 구성요소가 실패했으면 stderr 로 알린다(RAGSearchBox 가 꼬리를 보여 준다)
    for (k, v) in &warm.items {
        if let Some(e) = v.as_str() {
            log(&format!("[warmup] {} 실패 — {}", k, e));
        }
    }
    outln(&format!("준비 완료 ({:.1}초) — {}청크 / {} ({} 모드)", warm.total_ms / 1000.0, pyutil::comma(count as i64),
        config::gen_file(&alias).unwrap_or(&alias), model_mode(&alias)));
    outln(&format!("생성 {} / 리랭킹 {}", backend::label(&warm.backend),
        if warm.rerank { if warm.rerank_deferred { "켬(백그라운드 적재 중)" } else { "켬" } } else { "끔" }));
    if warm.rerank && warm.rerank_deferred {
        announce_rerank(&app);
    }
    outln(&format!("모델: 임베딩 {} / 리랭커 {}", pyutil::basename(pyutil::rstrip_chars(&cfg().model_dir, "\\/")),
        pyutil::basename(pyutil::rstrip_chars(&cfg().rerank_dir, "\\/"))));
    outln("기능: ask-folder index-commands ask-prev upload find");
    outln("질문을 입력하세요.  종료: exit 또는 Ctrl+D   도움말: /help\n");
    loop {
        out("질문> ");
        let line = match read_line() {
            Some(l) => l,
            None => {
                outln("");
                break;
            }
        };
        let mut query = pyutil::strip(&line).to_string();
        if query.is_empty() {
            continue;
        }
        if ["exit", "quit", "종료", "/exit", "/quit"].contains(&query.to_lowercase().as_str()) {
            break;
        }
        let mut ask_folder = chat_folder.clone();
        // /ask 의 prev — 같은 대화의 바로 앞 질문(멀티턴이 검색에 이어 붙인다)
        let mut ask_prev: Option<String> = None;
        // /ask 의 conv · uploads — RAGSearchBox 대화 id 와 올린 문서가 있던 대화인가(업로드 문서에서만 찾기)
        let mut ask_conv: Option<String> = None;
        let mut ask_uploads = false;
        if query.starts_with("/ask ") || query == "/ask" {
            let raw = pyutil::strip(&query[4..]).to_string();
            let raw = if raw.is_empty() { "{}".to_string() } else { raw };
            match serde_json::from_str::<Value>(&raw) {
                Ok(Value::Object(req)) => {
                    query = pyutil::strip(&req.get("q").map(|v| v.as_str().map(|s| s.to_string()).unwrap_or_else(|| v.to_string())).unwrap_or_default()).to_string();
                    ask_folder = req.get("folder").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string());
                    ask_prev = req.get("prev").and_then(|v| v.as_str()).map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
                    ask_conv = req.get("conv").and_then(|v| v.as_str()).map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
                    ask_uploads = req.get("uploads").and_then(|v| v.as_bool()).unwrap_or(false);
                }
                _ => {
                    outln("  /ask 형식이 잘못됐습니다: /ask {\"q\": \"질문\", \"folder\": \"폴더\"}\n");
                    continue;
                }
            }
            if query.is_empty() {
                continue;
            }
        } else if query.starts_with('/') {
            let parts: Vec<&str> = pyutil::split_ws(&query);
            let cmd = parts[0].to_lowercase();
            let rest = pyutil::strip(&query[parts[0].len()..]).to_string();
            if cmd == "/help" {
                outln(&format!("  /topk N     근거 개수 변경 (현재 {})", top_k.unwrap_or(cfg().top_k as usize)));
                outln("  /status     인덱스 상태");
                outln("  /folder <폴더>       이후 질문은 그 폴더(하위 포함) 문서에서만 찾기. 비우면 해제");
                outln("  /index-doc <경로>    문서 한 건 반영(추가·수정)");
                outln("  /remove-doc <경로>   지운 문서를 인덱스에서 빼기");
                outln("  /bm25                키워드 색인 다시 만들기");
                outln("  /index-plan <폴더>   폴더와 인덱스 비교(바꾸지 않음)");
                outln("  /suggest-synonyms    놓친 질문으로 동의어 사전 후보 뽑기");
                outln("  /find {\"q\": 검색어, \"folder\": 폴더}  탐색기 모드 찾기 — 문서 ▸ 쪽 (@find 줄)");
                outln("  exit        종료\n");
            } else if cmd == "/topk" && parts.len() > 1 && parts[1].chars().all(|c| c.is_ascii_digit()) && !parts[1].is_empty() {
                top_k = parts[1].parse().ok();
                outln(&format!("  근거 개수를 {}개로 변경했습니다.\n", parts[1]));
            } else if cmd == "/status" {
                let (n, mb) = app.store.stats().unwrap_or((0, 0.0));
                outln(&format!("  {}청크 / {:.1}MB\n", pyutil::comma(n as i64), mb));
            } else if cmd == "/folder" {
                let arg = pyutil::strip_chars(&rest, "\"").to_string();
                chat_folder = if arg.is_empty() { None } else { Some(arg) };
                outln(&format!("  검색 범위: {}\n", chat_folder.as_deref().unwrap_or("인덱스 전체")));
            } else if cmd == "/upload" || cmd == "/upload-remove" || cmd == "/upload-forget" {
                // 문서 업로드(업로드 설계서 §7) — 대화별 업로드 인덱스. 결과는 "@index {json}" 한 줄
                outln(&commands::format_result(upload_command(&app, &cmd, &rest).as_object().unwrap(), commands::PREFIX));
            } else if cmd == "/find" {
                // 탐색기 모드 찾기(탐색기 모드 설계서 §15) — 답 없이 문서 ▸ 쪽 목록을 "@find" 줄로
                let raw = if rest.is_empty() { "{}".to_string() } else { rest.clone() };
                match serde_json::from_str::<Value>(&raw) {
                    Ok(Value::Object(req)) => {
                        let q = pyutil::strip(req.get("q").and_then(|v| v.as_str()).unwrap_or("")).to_string();
                        let folder = req.get("folder").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string());
                        let limit = req.get("limit").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                        if q.is_empty() {
                            outln(&crate::find::error_line("검색어가 비었습니다"));
                        } else {
                            // 조건 칩에서 끈 조건 · 넓힌 기간({"off": [...], "widen": N})
                            let opts = crate::meta::query::PlanOpts::from_json(&Value::Object(req.clone()));
                            match crate::find::find_with(&app.retriever, &q, folder.as_deref(), limit, &opts) {
                                Ok((lines, done)) => crate::find::print_lines(&lines, &done, &mut |s| outln(s)),
                                Err(e) => outln(&crate::find::error_line(&e)),
                            }
                        }
                    }
                    _ => outln(&crate::find::error_line("/find 형식이 잘못됐습니다: /find {\"q\": \"검색어\"}")),
                }
            } else if cmd == "/suggest-synonyms" {
                // 동의어 후보 만들기(동의어 설계서 4단계) — 인덱스를 쥔 이 워커가 올려 둔 모델로 직접 한다.
                //   명령 시간 제한(RAGSearchBox 300초) 안에 끝나게 최근 질문 WORKER_RECENT 개만 본다
                let gen = app.generator.clone();
                let complete = move |p: &str, n: usize| gen.complete(p, n);
                let (summary, _) = crate::synonym_suggest::run(&app.retriever, &app.embedder, Some(&complete), None, None,
                                                               crate::synonym_suggest::WORKER_RECENT, &mut |_| {});
                outln(&commands::format_result(&summary, commands::PREFIX));
            } else if commands::COMMANDS.contains(&cmd.as_str()) {
                let res = commands::run(&app, &cmd, &rest);
                outln(&commands::format_result(&res, commands::PREFIX));
            } else {
                outln("  알 수 없는 명령입니다. /help 를 입력하세요.\n");
            }
            continue;
        }
        render_answer(&app, &query, top_k, a.max_tokens, !a.no_stream, ask_folder.as_deref(), ask_prev.as_deref(),
                      ask_conv.as_deref(), ask_uploads);
        outln("");
        if let Some(Err(e)) = app.rerank_load.lock().unwrap().clone() {
            if !rr_warned {
                outln(&format!("  ⚠️ 리랭커 적재 실패 — 리랭킹 없이 답합니다: {}\n", e));
                rr_warned = true;
            }
        }
    }
    outln("종료합니다.");
    app.close();
    0
}

// ── extract ───────────────────────────────────────────────

pub fn cmd_extract(a: &Args) -> i32 {
    let path = a.path.clone().unwrap_or_default();
    if !std::path::Path::new(&path).is_file() {
        log(&format!("파일 없음: {}", path));
        return 2;
    }
    let chars = a.chars.unwrap_or(1200);
    if a.blocks {
        return extract_blocks_preview(&path, chars, a.dump.as_deref());
    }
    let (text, n_rows) = match crate::extract::extract_text(&path, true) {
        Ok(x) => x,
        Err(e) => {
            log(&format!("추출 실패: ExtractError: {}", e));
            return 1;
        }
    };
    if text.is_empty() {
        outln("추출 텍스트 없음 — 스캔 이미지 PDF 이거나 지원하지 않는 포맷입니다.");
        return 1;
    }
    outln(&format!("파일    : {}", pyutil::basename(&path)));
    outln(&format!("문자수  : {}", pyutil::comma(pyutil::clen(&text) as i64)));
    outln(&if n_rows > 0 { format!("표 복원 : {}행", n_rows) } else { "표 복원 : 없음".into() });
    let emb = crate::embed::Embedder::new();
    let tok = match emb.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let chunks = crate::chunk::split_text(&text, &tok, None, None);
    outln(&format!("청크수  : {}개 ({}토큰 기준)", chunks.len(), cfg().chunk_tokens));
    outln(&"-".repeat(66));
    outln(pyutil::head(&text, chars));
    let n = pyutil::clen(&text);
    if n > chars {
        outln(&format!("\n... (이하 {}자 생략)", pyutil::comma((n - chars) as i64)));
    }
    0
}

// extract --blocks — 블록·절·청크 미리보기
fn extract_blocks_preview(path: &str, chars: usize, dump: Option<&str>) -> i32 {
    let mut doc = match crate::docmodel::extract_document(path, None) {
        Ok(d) => d,
        Err(e) => {
            log(&format!("추출 실패: ExtractError: {}", e));
            return 1;
        }
    };
    outln(&format!("파일    : {}", pyutil::basename(path)));
    outln(&format!("경로    : {}", if doc.is_blocks() { format!("블록 ({})", doc.format) } else { "폴백 — 기존 추출 + 정규식 절 추측".into() }));
    for w in &doc.warnings {
        outln(&format!("경고    : {}", w));
    }
    if let Some(d) = dump {
        if let Ok(p) = crate::docmodel::dump_json(&doc, d, pyutil::basename(path)) {
            outln(&format!("블록JSON: {}", p));
        }
    }
    if doc.is_blocks() {
        let mut kinds: Vec<(String, usize)> = vec![];
        for b in &doc.blocks {
            let k = format!("{}/{}", b.kind, b.source);
            match kinds.iter_mut().find(|x| x.0 == k) {
                Some(x) => x.1 += 1,
                None => kinds.push((k, 1)),
            }
        }
        outln(&format!("블록    : {}개  {{{}}}", doc.blocks.len(), kinds.iter().map(|(k, n)| format!("'{}': {}", k, n)).collect::<Vec<_>>().join(", ")));
        let secs = crate::docmodel::assemble::build_sections(&mut doc);
        outln(&format!("절      : {}개", secs.len()));
        for s in secs.iter().take(30) {
            let pages = match s.page_from { Some(a) => format!(" p{}-{}", a, s.page_to.unwrap_or(a)), None => String::new() };
            let path_s = if s.path.is_empty() { "(머리말)".to_string() } else { s.path.join(" > ") };
            outln(&format!("   [{:>3}] {}{} · 블록 {}", s.id, path_s, pages, s.blocks.len()));
        }
        if secs.len() > 30 {
            outln(&format!("   ... (이하 {}개 생략)", secs.len() - 30));
        }
    }
    let emb = crate::embed::Embedder::new();
    let tok = match emb.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let chunks = crate::chunk::split_blocks(&mut doc, &tok, None);
    let mut lens: Vec<usize> = chunks.iter().map(|c| tok.tlen(&c.text)).collect();
    lens.sort();
    if lens.is_empty() {
        outln("청크    : 없음");
    } else {
        outln(&format!("청크    : {}개 (토큰 최소 {} · 중앙 {} · 최대 {}, 예산 {})", chunks.len(), lens[0], lens[lens.len() / 2], lens[lens.len() - 1], cfg().chunk_tokens));
    }
    outln(&"-".repeat(66));
    let mut shown = 0;
    for (i, c) in chunks.iter().enumerate() {
        if shown >= chars {
            outln(&format!("\n... (청크 {}개 더)", chunks.len() - i));
            break;
        }
        outln(&format!("── 청크 {} ──", i));
        outln(&c.text);
        shown += pyutil::clen(&c.text);
    }
    0
}

// ── search ────────────────────────────────────────────────

pub fn cmd_search(a: &Args) -> i32 {
    let app = App::new(None);
    app.warmup(true, &mut |_| {}, None);
    app.wait_rerank();
    let q = a.query.clone().unwrap_or_default();
    let chars = a.chars.unwrap_or(200);
    // --group — 탐색기 모드 찾기(워커 /find 와 같은 출력)
    if a.group {
        let code = match crate::find::find(&app.retriever, &q, a.folder.as_deref(), a.top_k.unwrap_or(0)) {
            Ok((lines, done)) => {
                crate::find::print_lines(&lines, &done, &mut |s| outln(s));
                0
            }
            Err(e) => {
                outln(&crate::find::error_line(&e));
                1
            }
        };
        app.close();
        return code;
    }
    let r = app.retriever.search(&q, a.top_k, a.folder.as_deref());
    let code = match r {
        Err(e) => {
            log(&format!("검색 실패: {}", e));
            1
        }
        Ok((chunks, t)) => {
            outln(&format!("\n질의: {}", q));
            if let Some(f) = &a.folder {
                outln(&format!("범위: {} (문서 {}건, 벡터 한 벌)", f, t.get("scope_docs").map(|v| v.to_string()).unwrap_or_default()));
            }
            let rr = if t.contains_key("rerank_ms") { format!(" + 리랭킹 {:.0}", tget(&t, "rerank_ms")) } else { String::new() };
            if t.contains_key("meta") {
                outln(&format!("조건:{}", meta_scope(&t).trim_start_matches(" · 조건")));
            }
            outln(&format!("검색 {:.0}ms (임베딩 {:.0} + dense {:.0} + BM25 {:.0} + 융합 {:.0}{})\n",
                tget(&t, "total_ms"), tget(&t, "embed_ms"), tget(&t, "dense_ms"), tget(&t, "bm25_ms"), tget(&t, "fuse_ms"), rr));
            if chunks.is_empty() {
                if a.folder.is_some() && t.get("scope_docs").and_then(|v| v.as_i64()) == Some(0) {
                    outln("이 폴더(하위 포함)에는 인덱싱된 문서가 없습니다.");
                } else {
                    outln("검색 결과 없음 — 인덱스가 비었거나 질의어가 코퍼스에 없습니다.");
                }
                1
            } else {
                for (i, c) in chunks.iter().enumerate() {
                    outln(&format!("[{}] {}  (폴더: {})", i + 1, c.doc_name, if c.folder.is_empty() { "-" } else { &c.folder }));
                    let body = pyutil::strip(&c.text.replace('\n', " ")).to_string();
                    outln(&format!("    {}{}\n", pyutil::head(&body, chars), if pyutil::clen(&body) > chars { "…" } else { "" }));
                }
                0
            }
        }
    };
    app.close();
    code
}

//------------------------------------------------------------------
// 동의어 후보 제안 명령 (동의어 설계서 3단계, Python cmd_suggest 와 같다)
//=> 놓친 질문 기록(synonym_misses.jsonl)을 읽어 "일상어 → 문서 용어" 후보를 synonym_candidates.json 에 쓴다.
//    1) 검색 구성요소를 올린다(1.7B 는 --no-llm 이 아니면 함께 — 후보 하나를 고르게 한다)
//    2) 질문마다 후보를 뽑아 낱말별로 모은다 · 3) 결과 파일을 쓰고 상위 후보를 보여 준다
//   --from 에 바꿔 말한 질문 세트(jsonl)를 넣으면 평가에 쓸 수 있다.
//   돌려줌: 0 = 성공, 1 = 기록된 질문 없음·실패
//------------------------------------------------------------------
pub fn cmd_suggest(a: &Args) -> i32 {
    use crate::synonym_suggest as ss;
    let src = a.from.clone().unwrap_or_else(|| pyutil::join(&cfg().root, ss::MISSES_NAME));
    let app = App::new(None);
    app.warmup(a.no_llm, &mut |s| log(s), None);
    app.wait_rerank();
    let gen = app.generator.clone();
    let complete = move |p: &str, n: usize| gen.complete(p, n);
    let llm: Option<&dyn Fn(&str, usize) -> Result<String, String>> = if a.no_llm { None } else { Some(&complete) };
    // 인덱스를 다른 프로세스(실행 중인 chat 워커)가 쥐고 있으면 검색에서 실패한다 — 이유는 요약 줄의 why 로
    let (summary, result) = ss::run(&app.retriever, &app.embedder, llm, Some(&src), a.out.as_deref(), ss::RECENT, &mut |s| log(s));
    match &result {
        Some(res) => {
            let cands = res["candidates"].as_array().cloned().unwrap_or_default();
            outln(&format!("
질문 {}개 → 후보 낱말 {}개 ({}) — {}", res["n_questions"], cands.len(),
                if res["llm"].as_bool().unwrap_or(false) { "1.7B 고르기" } else { "e5 순서" }, summary["path"].as_str().unwrap_or("")));
            for c in cands.iter().take(10) {
                let terms = c["terms"].as_array().cloned().unwrap_or_default().iter()
                    .map(|t| format!("{}{} {:.2}", t["term"].as_str().unwrap_or(""), if t["llm"].as_bool().unwrap_or(false) { "★" } else { "" },
                                     t["score"].as_f64().unwrap_or(0.0)))
                    .collect::<Vec<_>>().join(" · ");
                outln(&format!("  {} ({}회) → {}", c["word"].as_str().unwrap_or(""), c["count"], terms));
            }
        }
        None => {
            let why = summary.get("why").and_then(|v| v.as_str()).unwrap_or("");
            if why.starts_with("기록된 질문이 없습니다") { outln(why) } else { outln(&format!("후보 제안 실패: {}", why)) }
        }
    }
    // RAGSearchBox 가 읽는 결과 한 줄 — chat 워커의 /suggest-synonyms 와 같은 모양
    outln(&crate::commands::format_result(&summary, crate::commands::PREFIX));
    app.close();
    if summary["ok"].as_bool().unwrap_or(false) { 0 } else { 1 }
}

// ── status ────────────────────────────────────────────────

pub fn cmd_status(a: &Args) -> i32 {
    let app = App::new(None);
    let (n, mb) = match app.store.stats() {
        Ok(s) => s,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let state = indexer::load_state();
    let c = cfg();
    outln(&format!("인덱스   : {}청크 / {:.1}MB", pyutil::comma(n as i64), mb));
    outln(&format!("문서     : {}건", indexer::docs(&state).len()));
    outln(&format!("BM25캐시 : {}", if std::path::Path::new(&c.bm25_path).is_file() { "있음" } else { "없음(dense 단독 폴백)" }));
    outln(&format!("설정파일 : {}", c.config_path.as_deref().unwrap_or("없음(코드 기본값)")));
    outln(&format!("청킹     : {}토큰 / 겹침 {} / {} / 머리글 상한 {}", c.chunk_tokens, c.chunk_overlap, c.chunk_mode, c.table_header_max_tokens));
    let rec = state.get("chunk_params").filter(|v| !v.is_null()).cloned();
    let built = match &rec {
        Some(r) => r.get("extract_engine").and_then(|v| v.as_str()).unwrap_or("legacy").to_string(),
        None => "기록 없음".into(),
    };
    outln(&format!("추출엔진 : {} (인덱스: {})", c.extract_engine, built));
    if !indexer::docs(&state).is_empty() && rec.is_none() {
        outln("  ⚠️ 인덱스에 청킹 설정 기록 없음(이전 버전에서 만듦)");
    } else if let Some(r) = &rec {
        let mut tmp = state.clone();
        tmp.insert("docs".into(), json!({"x": 1}));
        if indexer::check_chunk_params(&mut tmp, false, &mut |_| {}).is_err() {
            outln(&format!("  ⚠️ 인덱스는 다른 청킹 설정으로 만들어짐 — --rebuild 필요: {}", indexer::show_params(r)));
        }
    }
    outln(&format!("검색     : dense {} + BM25 {} → RRF(k={}) → 리랭커 입력 {} → sLLM 근거 {}건",
        c.dense_top_k, c.bm25_top_k, fmt_g(c.rrf_k), c.rerank_pool, c.top_k));
    let others: Vec<&str> = config::USER_GEN_MODELS.iter().copied().filter(|m| *m != c.default_gen_model).collect();
    outln(&format!("생성모델 : {} ({} 모드)   ← {}", config::gen_file(&c.default_gen_model).unwrap_or(""), model_mode(&c.default_gen_model),
        if c.default_gen_model == config::FAST_GEN_MODEL { "한 번만 정밀하게: ask --precise".to_string() } else { format!("다른 모델: ask --model {}", others.join(" | ")) }));
    match backend::peek(None) {
        Some(cached) => outln(&format!("생성백엔드: {} — {}", backend::label(cached["backend"].as_str().unwrap_or("cpu")), cached.get("reason").and_then(|v| v.as_str()).unwrap_or(""))),
        None => outln("생성백엔드: 미측정 — 첫 질문 때 자동 측정(`backend` 명령으로 미리 가능)"),
    }
    outln(&format!("리랭커   : {}", if std::path::Path::new(&pyutil::join(&c.rerank_dir, "model_int8.onnx")).is_file() { "있음" } else { "없음(리랭킹 비활성)" }));
    if a.verbose {
        outln("\n문서별 청크 수:");
        let mut v: Vec<(&String, &Value)> = indexer::docs(&state).iter().collect();
        v.sort_by_key(|(_, m)| std::cmp::Reverse(m.get("chunks").and_then(|x| x.as_i64()).unwrap_or(0)));
        for (p, m) in v {
            outln(&format!("  {:>5}  {}", m.get("chunks").and_then(|x| x.as_i64()).unwrap_or(0), m.get("name").and_then(|x| x.as_str()).unwrap_or(p)));
        }
    }
    app.close();
    0
}

// 파이썬 "{:g}" 흉내
fn fmt_g(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 { format!("{}", f as i64) } else { format!("{}", f) }
}

// ── clear ─────────────────────────────────────────────────

fn ask_yes(prompt: &str) -> bool {
    out(prompt);
    matches!(read_line().map(|s| pyutil::strip(&s).to_lowercase()).as_deref(), Some("y") | Some("yes"))
}

pub fn cmd_clear(a: &Args) -> i32 {
    let c = cfg();
    if let Some(doc) = &a.doc {
        let target = pyutil::abspath(doc);
        let mut state = indexer::load_state();
        let meta = indexer::docs(&state).get(&target).cloned();
        let meta = match meta {
            Some(m) => m,
            None => {
                log(&format!("인덱스에 없는 문서입니다: {}", target));
                log("  (경로가 인덱싱 당시와 정확히 같아야 합니다. `status -v` 로 확인하세요)");
                return 1;
            }
        };
        if !a.yes {
            outln(&format!("다음 문서의 청크 {}개를 인덱스에서 제거합니다:", meta.get("chunks").map(|v| v.to_string()).unwrap_or("?".into())));
            outln(&format!("  {}", target));
            if !ask_yes("진행할까요? [y/N] ") {
                outln("취소했습니다.");
                return 1;
            }
        }
        let app = App::new(None);
        if let Err(e) = app.store.ensure_loaded().and_then(|_| indexer::remove_doc(&target, &mut state, &app.store)) {
            log(&e);
            return 1;
        }
        outln("제거했습니다. BM25 는 다음 `index --dir <폴더>` 때 다시 만들어집니다 (그 전에도 지운 문서는 근거로 나오지 않습니다).");
        app.close();
        return 0;
    }
    let mut targets: Vec<(String, &str, u64)> = vec![];
    for (p, label) in [(&c.index_dir, "벡터 인덱스"), (&c.bm25_path, "BM25 캐시"), (&c.state_path, "증분 상태")] {
        let path = std::path::Path::new(p);
        if path.is_dir() {
            let size = std::fs::read_dir(path).map(|rd| rd.flatten().map(|e| e.metadata().map(|m| m.len()).unwrap_or(0)).sum()).unwrap_or(0);
            targets.push((p.clone(), label, size));
        } else if path.is_file() {
            targets.push((p.clone(), label, std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)));
        }
    }
    if targets.is_empty() {
        outln(&format!("삭제할 인덱스가 없습니다. (데이터 루트: {})", c.root));
        return 0;
    }
    let state = indexer::load_state();
    outln(&format!("데이터 루트: {}", c.root));
    outln(&format!("문서 {}건 / 다음 항목을 삭제합니다:", indexer::docs(&state).len()));
    for (p, label, size) in &targets {
        outln(&format!("  {:<12} {:>8.1} MB  {}", label, *size as f64 / 1024.0 / 1024.0, pyutil::basename(p)));
    }
    if !a.yes {
        outln("\n⚠️ 되돌릴 수 없습니다. 다시 인덱싱하려면 문서 수에 따라 수십 분이 걸립니다.");
        if !ask_yes("정말 삭제할까요? [y/N] ") {
            outln("취소했습니다.");
            return 1;
        }
    }
    let mut failed = vec![];
    for (p, label, _) in &targets {
        let path = std::path::Path::new(p);
        let r = if path.is_dir() { std::fs::remove_dir_all(path) } else { std::fs::remove_file(path) };
        match r {
            Ok(_) => outln(&format!("  삭제: {}", label)),
            Err(e) => failed.push((label, pyutil::head(&e.to_string(), 80).to_string())),
        }
    }
    if !failed.is_empty() {
        log("\n일부 삭제 실패 — 다른 프로세스가 사용 중일 수 있습니다:");
        for (l, w) in failed {
            log(&format!("  {}: {}", l, w));
        }
        return 1;
    }
    outln("\n삭제 완료. `index --dir <폴더>` 로 다시 인덱싱하세요.");
    0
}

// ── backend / warmup ──────────────────────────────────────

pub fn cmd_backend(a: &Args) -> i32 {
    let info = backend::select(None, a.reprobe, &mut |m| outln(m));
    let src = match info.get("source").and_then(|v| v.as_str()).unwrap_or("") {
        "cache" => "저장된 측정", "probe" => "방금 측정", "config" => "환경변수 지정", "check" => "사전 점검", "loaded" => "이미 적재됨", _ => "-",
    };
    let b = info["backend"].as_str().unwrap_or("cpu").to_string();
    outln(&format!("\n생성 백엔드 : {}", backend::label(&b)));
    outln(&format!("선택 근거   : {} — {}", src, info.get("reason").and_then(|v| v.as_str()).unwrap_or("")));
    for (kind, lab) in [("cpu", "CPU"), ("vulkan", "iGPU")] {
        let m = match info.get(kind) {
            Some(m) if m.is_object() => m,
            _ => continue,
        };
        if m.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
            let f = |k: &str| m.get(k).and_then(|v| v.as_f64()).unwrap_or(0.0);
            outln(&format!("  {:<5} prefill {:>5.0} t/s  ({}토큰 {:.2}초 / 적재 {:.1}초 / 첫회 {:.1}초)",
                lab, f("prefill_tps"), m.get("prompt_tokens").and_then(|v| v.as_i64()).unwrap_or(0), f("prefill_s"), f("load_s"), f("warm_s")));
        } else {
            outln(&format!("  {:<5} 사용 불가 — {}", lab, m.get("error").and_then(|v| v.as_str()).unwrap_or("")));
        }
    }
    let use_rr = backend::rerank_enabled(&b);
    let has_rr = std::path::Path::new(&pyutil::join(&cfg().rerank_dir, "model_int8.onnx")).is_file();
    outln(&format!("리랭킹      : {}{}", if use_rr { "켬" } else { "끔" }, if use_rr && !has_rr { " (⚠️ 모델 없음 → 실제로는 끔)" } else { "" }));
    outln(&format!("저장 위치   : {}", cfg().backend_cache_path));
    outln("\n강제 지정: 환경변수 SIMPLERAG_GEN_BACKEND=cpu|vulkan, SIMPLERAG_RERANK=1|0");
    outln("다시 측정: backend --reprobe");
    0
}

pub fn cmd_warmup(a: &Args) -> i32 {
    let model = match pick_model(a) {
        Ok(m) => m,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let app = App::new(model.as_deref());
    let r = app.warmup(false, &mut |m| log(m), None);
    outln(&format!("  생성      {} / 리랭킹 {}", backend::label(&r.backend), if r.rerank { "켬" } else { "끔" }));
    for k in ["embedder", "qdrant", "bm25", "reranker", "llm", "llm_warm"] {
        match r.items.get(k) {
            None => continue,
            Some(Value::String(s)) => outln(&format!("  {:<9} ❌ {}", k, s)),
            Some(v) => {
                let f = v.as_f64().unwrap_or(0.0);
                if f < 0.0 { outln(&format!("  {:<9} — 캐시 없음", k)) } else { outln(&format!("  {:<9} {:.0}ms", k, f)) }
            }
        }
    }
    outln(&format!("  {:<9} {:.0}ms (병렬 총합 = 준비 완료)", "합계", r.total_ms));
    if r.rerank_deferred {
        match app.wait_rerank() {
            Some(Err(e)) => outln(&format!("  {:<9} ❌ {} (백그라운드)", "reranker", e)),
            Some(Ok(ms)) => outln(&format!("  {:<9} {:.0}ms (준비 완료 뒤 백그라운드)", "reranker", ms)),
            None => outln(&format!("  {:<9} 0ms (준비 완료 뒤 백그라운드)", "reranker")),
        }
    }
    app.close();
    0
}

// ── import (Rust 판 전용) ─────────────────────────────────

//------------------------------------------------------------------
// Python 판 인덱스 가져오기
//=> <폴더>/qdrant_data/collection/rag/storage.sqlite 의 점(pickle)과 index_state.json 을 읽어
//   Rust 판 저장소·상태 파일로 옮기고 BM25 를 새로 만든다. 다시 임베딩하지 않는다.
//   Python 판이 실행 중이어도 읽을 수 있게 sqlite 를 임시 사본으로 떠서 읽는다.
//------------------------------------------------------------------
pub fn cmd_import(a: &Args) -> i32 {
    let src = pyutil::abspath(&a.from.clone().unwrap_or_default());
    let sqlite = pyutil::join(&pyutil::join(&pyutil::join(&pyutil::join(&src, "qdrant_data"), "collection"), "rag"), "storage.sqlite");
    let state_src = pyutil::join(&src, "index_state.json");
    if !std::path::Path::new(&sqlite).is_file() || !std::path::Path::new(&state_src).is_file() {
        log(&format!("가져올 Python 판 인덱스가 없습니다: {}", src));
        log("  (qdrant_data\\collection\\rag\\storage.sqlite 와 index_state.json 이 있어야 합니다)");
        return 2;
    }
    let tmp = std::env::temp_dir().join(format!("simplerag_import_{}.sqlite", std::process::id()));
    if let Err(e) = std::fs::copy(&sqlite, &tmp) {
        log(&format!("사본을 만들 수 없습니다: {}", e));
        return 1;
    }
    let t0 = Instant::now();
    let conn = match rusqlite::Connection::open(&tmp) {
        Ok(c) => c,
        Err(e) => {
            log(&e.to_string());
            return 1;
        }
    };
    let mut ids = vec![];
    let mut vecs = vec![];
    let mut pls = vec![];
    let mut bad = 0;
    {
        let mut st = conn.prepare("SELECT point FROM points").unwrap();
        let rows = st.query_map([], |r| r.get::<_, Vec<u8>>(0)).unwrap();
        for b in rows.flatten() {
            match crate::pickle::point_of(&b) {
                Ok((id, v, p)) => {
                    ids.push(id);
                    vecs.push(v);
                    pls.push(p);
                }
                Err(_) => bad += 1,
            }
        }
    }
    drop(conn);
    let _ = std::fs::remove_file(&tmp);
    outln(&format!("Python 판 점 {}개 읽음{} ({:.1}s)", pyutil::comma(ids.len() as i64),
        if bad > 0 { format!(" · 읽지 못함 {}개", bad) } else { String::new() }, t0.elapsed().as_secs_f64()));
    let app = App::new(None);
    if let Err(e) = app.store.recreate().and_then(|_| app.store.upsert(&ids, &vecs, &pls)) {
        log(&e);
        return 1;
    }
    let raw = std::fs::read(&state_src).unwrap_or_default();
    let state: indexer::State = serde_json::from_slice::<Value>(&raw).ok().and_then(|v| v.as_object().cloned()).unwrap_or_else(indexer::empty_state);
    let mut state = state;
    if let Err(e) = indexer::save_state(&state) {
        log(&e);
        return 1;
    }
    for f in ["gen_backend.json"] {
        let s = pyutil::join(&src, f);
        let d = pyutil::join(&cfg().root, f);
        if std::path::Path::new(&s).is_file() && !std::path::Path::new(&d).is_file() {
            let _ = std::fs::copy(&s, &d);
        }
    }
    outln("BM25 인덱스 구축 중...");
    let tok = match app.embedder.tokenizer() {
        Ok(t) => t,
        Err(e) => {
            log(&e);
            return 1;
        }
    };
    let (texts, bids, _) = indexer::collect_for_bm25(&app.store, None, &mut |m| outln(m)).unwrap_or_default();
    match app.bm25.build(&texts, &bids, &tok) {
        Ok(s) => outln(&format!("BM25 인덱스 {}건 구축 {:.1}s", pyutil::comma(texts.len() as i64), s)),
        Err(e) => log(&e),
    }
    state.insert("bm25_built_at".into(), pyutil::jf(pyutil::now_ts()));
    let _ = indexer::save_state(&state);
    outln(&format!("\n가져오기 완료 — 문서 {}건 / 청크 {}개 → {}", indexer::docs(&state).len(), pyutil::comma(ids.len() as i64), cfg().root));
    app.close();
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    // 글을 step 글자씩 흘려 넣고 마무리한 뒤 (화면에 나간 전체, 마무리 전에 나가 있던 글자 수)
    fn stream(raw: &str, n: usize, step: usize) -> (String, usize) {
        let mut w = StreamWriter::new();
        w.capture = Some(String::new());
        w.n_ev = Some(n);
        w.interval = 0.0; // 매번 내보내 보게 — 붙들기 규칙만 시험한다
        let cs: Vec<char> = raw.chars().collect();
        for c in cs.chunks(step) {
            w.write(&c.iter().collect::<String>());
        }
        let emitted = pyutil::clen(&w.emitted);
        w.finish(Some(n), None);
        (w.capture.unwrap(), emitted)
    }

    #[test]
    fn echo_removed_while_streaming_early() {
        // bench/test_output_cleanup.py 와 같은 사례 — 앞 문장을 붙여 보류 구간을 넘긴 뒤 되뱉기가 온다
        let pad = "규정에 따라 처리합니다. ".repeat(6);
        let cases = [
            ("휴가비는 100만원입니다. 근거 번호: [1]", "근거 번호", "100만원"),
            ("휴가는 5일입니다. [3] 번의 근거 번호는 [3]입니다", "근거 번호는", "5일"),
            ("숙박비는 70,000원입니다. [1] 근거 번호입니다", "근거 번호입니다", "70,000"),
            ("경조금을 받습니다. 근거에 없는 내용은 추측하지 않습니다. [1]", "추측하지", "경조금"),
            ("휴가비는 100만원입니다. [1]과 [2]의 정보를 바탕으로 답변합니다", "바탕으로 답변", "100만원"),
            ("교통비는 티머니로 지원합니다. [1]번으로 간결하게 답변합니다", "간결하게 답변", "티머니"),
            ("권고 모델은 **DELL P2422H** 입니다. [1]", "**", "DELL P2422H"),
            ("유급휴가는 1일입니다. [1] 번호 [1]로 답변합니다. [1]", "답변합니다", "1일"),
            ("자연어 질의 검색을 제공합니다. [1] 형식으로 근거 번호 표기.", "근거 번호", "자연어"),
            ("리프레시 휴가비를 지급합니다. [1]  근거 번호 [1] 형식으로 표기하세요.", "형식으로 표기", "리프레시"),
        ];
        for (raw, gone, kept) in cases {
            for step in [1, 3, 7] {
                let (out, emitted) = stream(&format!("{}{}", pad, raw), 3, step);
                assert!(!out.contains(gone) && out.contains(kept) && emitted > 0, "{} / {}자씩: {}", raw, step, out);
            }
        }
        // 굵게 별표가 "*" 두 토큰으로 갈라져 와도 별표가 하나도 남지 않는다(골든셋 q025)
        let q025 = "자녀가 다른 기관에서 장학금을 받는 경우, 학자금 지급은 **차액이 아닌 전체 금액을 지급**하며, \
                    **장학금을 받은 금액에서 차액을 차감한 후** 지급됩니다.  \n자녀 학자금 신청은 **납부 후 1개월 이내**에 \
                    신청해야 합니다.  \n학자금 신청서류는 해당 부서장 결재 후 경영지원본부에 신청해야 합니다. [1]";
        for step in [1, 2, 3] {
            let (out, _) = stream(q025, 3, step);
            assert!(!out.contains('*') && out.contains("차액이 아닌 전체 금액을 지급하며"), "{}자씩: {}", step, out);
        }
        // 앞쪽의 없는 인용 [12] 도 화면에 나가기 전에 괄호를 벗긴다(숫자는 남긴다)
        let early = format!("[12] 앞쪽에 찍힌 잘못된 인용. {}\n근거 번호 [1] 형식으로 표기하세요.", "본문이 길게 이어진다. ".repeat(20));
        let (out, _) = stream(&early, 3, 3);
        assert!(!out.contains("[12]") && out.contains("12 앞쪽에") && !out.contains("형식으로 표기"), "{}", out);
    }

    // 생성기(출력 관리기) → 화면 출력기 이어서 — 남길 답으로 마무리한다(render_answer 와 같게)
    fn pipe(raw: &str, n: usize, step: usize) -> String {
        let mut w = StreamWriter::new();
        w.capture = Some(String::new());
        w.n_ev = Some(n);
        w.interval = 0.0;
        let o = crate::generate::generator::replay_guard(raw, step, &mut |s| w.write(s));
        let (body, _) = crate::generate::prompts::parse_answer(&o.text, Some(n));
        w.finish(Some(n), Some(&body));
        w.capture.unwrap()
    }

    #[test]
    fn guard_and_writer_together() {
        // q044: 반복이 앞 문장의 '중간 구절'에서 시작됐다 — 출력기가 붙들던 첫머리는 남길 답 기준으로 버린다
        let q044 = "휴직기간이 끝나는 사원은 휴직기간 만료일 7일 전까지 복직원을 제출해야 한다.  \n\
                    [1]에 따르면, 휴직기간이 만료된 때에는 그 만료일 7일 전까지 복직원을 제출해야 한다.  \n\
                    휴직기간이 끝나지 않으면 해고가 가능하며, 휴직자로서 정당한 사유 없이 휴직기간 만료일 후 7 일이 경과할 때까지 \
                    복직원을 제출하지 않은 경우 [3]에 따라 해고될 수 있다.  \n \
                    [3]에 따르면, 휴직자로서 정당한 사유 없이 휴직기간 만료일 후 7 일이 경과할 때까지 복직원을 제출하지 않은 경우 \
                    해고될 수 있다.";
        // q096: "근거 [1]에 따르면" 의 '근거' — 뒤에 [1] 이 와야 지우는 되뱉기
        let q096 = "2021년 대비 2026년 세계 디지털 휴먼 시장 규모는 약 9.8배로 커질 것으로 전망됩니다.  \n\
                    근거 [1]에 따르면, 2021년 146.8억 달러에서 2026년 987.5억 달러로 9.8배 증가할 예정입니다. [1]";
        for step in [1, 2, 3] {
            let out = pipe(q044, 3, step);
            assert!(out.trim_end().ends_with("해고될 수 있다.") && !out.contains("[3]에 따르면"), "q044 {}자씩: {}", step, out);
            let out = pipe(q096, 3, step);
            assert!(!out.contains("근거 [") && !out.contains("근거\n") && out.contains("[1]에 따르면"), "q096 {}자씩: {}", step, out);
        }
    }

    #[test]
    fn first_sentence_goes_out_without_waiting_for_hold() {
        // 첫 문장이 끝나면(뒤에 공백) 56자를 기다리지 않고 나간다 — 화면 첫 글자 앞당기기
        let mut w = StreamWriter::new();
        w.capture = Some(String::new());
        w.interval = 0.0;
        for p in ["휴가는 ", "5일입니다", ".", " [1", "] 다음"] {
            w.write(p);
        }
        assert_eq!(w.capture.as_deref(), Some("휴가는 5일입니다."));
        // 소수점·문장 끝 뒤의 인용 번호는 붙든다
        assert_eq!(last_sentence_end("값은 3.5일"), 0);
        assert_eq!(last_sentence_end("끝. [1"), "끝.".len());
    }

    #[test]
    fn echo_filter_off_streams_without_hold() {
        // 되뱉기 거르기 끔(기본) — 첫 조각부터 바로 나가고, 짝 없는 별표·닫히지 않은 "[" 만 붙든다
        let mut w = StreamWriter::with_echo(false);
        w.capture = Some(String::new());
        w.interval = 0.0;
        w.n_ev = Some(3);
        w.write("휴가는 ");
        assert_eq!(w.capture.as_deref(), Some("휴가는 "));
        for p in ["**5", "일**입니다", " [1", "]. 근거 번호 [1] 형식으로 표기하세요."] {
            w.write(p);
        }
        w.finish(Some(3), None);
        let out = w.capture.unwrap();
        assert!(out.starts_with("휴가는 5일입니다 [1]."), "{}", out);
        assert!(!out.contains('*'), "{}", out);
        // 짧은 되뱉기("근거 번호 [1] 형식으로 표기…")는 끔에서도 지운다(설계서 ②)
        assert!(!out.contains("형식으로 표기") && !out.contains("근거 번호"), "{}", out);
        let (b, _) = crate::generate::prompts::parse_answer_opt("**답**은 50만원입니다. [1] [12]", Some(3), false);
        assert_eq!(b, "답은 50만원입니다. [1] 12");
    }

    // 거르기 끔 스트리밍 흉내 — (화면 출력, 첫 출력까지 넣은 글자 수)
    fn stream_off(raw: &str, n: usize, step: usize) -> (String, Option<usize>) {
        let mut w = StreamWriter::with_echo(false);
        w.capture = Some(String::new());
        w.interval = 0.0;
        w.n_ev = Some(n);
        let cs: Vec<char> = raw.chars().collect();
        let mut first = None;
        for (i, ch) in cs.chunks(step).enumerate() {
            w.write(&ch.iter().collect::<String>());
            if first.is_none() && !w.emitted.is_empty() {
                first = Some((i + 1) * step);
            }
        }
        w.finish(Some(n), None);
        (w.capture.unwrap(), first)
    }

    #[test]
    fn citations_are_reattached() {
        // 인용 번호 바로잡기(설계서 ⑤) — Python bench/test_output_cleanup.py 의 같은 사례
        use crate::generate::prompts::recite;
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<String>>();
        let q013 = s(&["이사회는 대표이사가 소집한다.", "정기이사회는 매 분기 종료 후 20일 내에 개최한다.", "임시이사회는 필요시 개최한다."]);
        let q076 = s(&["유니콘 기업 목록 Byju's 인도", "VIPKid 중국 외국어 교육 45억 달러", "에듀테크 시장 동향"]);
        let cases: Vec<(&str, Vec<String>, &str)> = vec![
            ("정기이사회는 매 분기 종료 후 20일 내에 개최합니다.  [1]", q013.clone(), "정기이사회는 매 분기 종료 후 20일 내에 개최합니다.  [2]"),
            ("[3] 근거에 따르면, VIPKid의 기업가치 평가액은 45억 달러입니다. [1]", q076.clone(),
             "[2] 근거에 따르면, VIPKid의 기업가치 평가액은 45억 달러입니다. [2]"),
            ("최종 6개이다. [1], [2], [3] 근거에 따르면 6개이다.", s(&["가", "6개 선정", "다"]), "최종 6개이다. [1], [2], [3] 근거에 따르면 6개이다."),
            ("평가액은 99억 달러입니다. [1]", q076.clone(), "평가액은 99억 달러입니다. [1]"),
            ("이사회는 대표이사가 소집한다. [2]", q013.clone(), "이사회는 대표이사가 소집한다. [2]"),
            ("휴가는 1일이다. [2]", s(&["1일 휴가", "1일 휴가비"]), "휴가는 1일이다. [2]"),
        ];
        for (body, ev, want) in cases {
            assert_eq!(recite(body, &ev, false, 0).0, want);
        }
        let (g, _) = recite("[3] 근거에 따르면, VIPKid 평가액은 45억 달러입니다.", &q076, true, 0);
        assert!(g.starts_with("[3]"), "{}", g);
        // 스트리밍 — 문장 끝 뒤 번호는 내보내기 전에 바로잡는다
        for step in [1, 3, 7] {
            let mut w = StreamWriter::with_echo(false);
            w.capture = Some(String::new());
            w.interval = 0.0;
            w.n_ev = Some(3);
            w.evidence = Some(q013.clone());
            let raw = "정기이사회는 매 분기 종료 후 20일 내에 개최합니다.  [1]\n다음 문장입니다.";
            let cs: Vec<char> = raw.chars().collect();
            for ch in cs.chunks(step) {
                w.write(&ch.iter().collect::<String>());
            }
            w.finish(Some(3), None);
            let out = w.capture.unwrap();
            assert!(out.contains("[2]") && !out.contains("[1]"), "({}자씩) {}", step, out);
        }
    }

    #[test]
    fn short_echo_removed_without_delay() {
        // 골든셋 100문항(1.7B, 거르기 끔)에서 실제로 남았던 잡음 — Python bench/test_output_cleanup.py 와 같은 사례
        let cases = [
            ("지원금은 80만원의 50%인 40만원이다.  
[1] 근거 [1]에 따르면, 50만원 초과분의 50%를 지급한다.", "근거", "[1]에 따르면, 50만원 초과분"),
            ("대출 한도는 3천만원입니다.  
[2] 근거 번호: [2]", "근거 번호", "3천만원입니다."),
            ("대표이사에 대한 조치는 이사회가 의결을 맡는다. [1][3][1][3]", "[1][3][1][3]", "[1][3]"),
            ("정액의 10분의 1에 상당한 금액을 지급받는다.  
근거 [1]에 따르면, 15일 초과 시 10분의 1을 뺀다.", "근거", "[1]에 따르면, 15일 초과"),
            ("데이터 처리 전에 검증 작업이 수행된다.  

[1] 형식으로 표기:", "형식으로", "검증 작업이 수행된다."),
            ("형사처벌 대상으로 간주됩니다.  
[1]의 근거 번호는 [1]입니다.", "근거 번호는", "간주됩니다."),
            // 2차 측정(짧은 거르기 1판)에서 남은 2건
            ("40만원이다.  \n[1] 근거 [1]에 따르면, 50만원 초과분의 50%를 지급한다.", "[1] [1]", "[1]에 따르면"),
            ("[2] 제공자가 알 수 없는 경우  \n\n[1] 형식으로 근거 번호를 표기하면 다음과 같다.", "형식으로", "알 수 없는 경우"),
        ];
        for (raw, gone, kept) in cases {
            for step in [1, 3, 7] {
                let (out, first) = stream_off(raw, 3, step);
                assert!(!out.contains(gone) && out.contains(kept), "{} ({}자씩): {}", gone, step, out);
                assert!(first.map_or(false, |f| f <= 12), "첫 출력이 늦음 ({}자씩): {:?}", step, first);
            }
            let (b, _) = crate::generate::prompts::parse_answer_opt(raw, Some(3), false);
            assert!(!b.contains(gone), "비스트리밍: {}", b);
        }
        // 지우면 안 되는 것
        for (raw, kept) in [
            ("지급 근거 규정은 제5조입니다. [1]", "근거 규정은"),
            ("[1]에 따르면 휴가는 5일이고, [2]에 따르면 3일이다.", "[2]에 따르면"),
            ("휴가는 5일이다. [1][2]", "[1][2]"),
            ("근무 형태는 [1] 교대제로 정한다.", "근무 형태는"),
        ] {
            for step in [1, 3] {
                let (out, _) = stream_off(raw, 3, step);
                assert!(out.contains(kept), "남겨야 함 ({}자씩): {}", step, out);
            }
        }
    }
}
