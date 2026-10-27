//------------------------------------------------------------------
// simplerag-rs 진입점 (Python cli.main)
//=> 인자를 풀어 명령을 고르고, 설정 오류는 "설정 오류: …" 로 알리고 2 로 끝낸다.
//   콘솔 글자 색은 시작 때 저장해 두었다가 끝날 때 되돌린다(llama.cpp 가 바꿀 수 있다).
//   Ctrl+C 는 프로세스를 죽이지 않고 '지금 답변 취소' 표시만 세운다.
//------------------------------------------------------------------

use std::sync::atomic::Ordering;

use simplerag::cli::{self, Args};
use simplerag::config;

const USAGE: &str = "usage: simplerag-rs [-h] {index,ask,chat,extract,search,clear,status,backend,warmup,import,suggest-synonyms} ...

로컬 문서 RAG (Rust 판)

명령:
  index    폴더 인덱싱(증분)            --dir <폴더> [--rebuild] [--no-recursive] [--allow-delete] [--max-add N]
  ask      질의 (질문 생략 시 대화형)   [질문] [--top-k N] [--max-tokens N] [--folder <폴더>] [--model M] [--precise] [--no-stream]
  chat     대화형 질의 (모델 1회 적재) [--top-k N] [--max-tokens N] [--model M] [--precise] [--no-stream]
  extract  추출 미리보기               <파일> [--chars N] [--blocks] [--dump <폴더>]
  search   검색만 수행(LLM 미적재)     <질의> [--top-k N] [--chars N] [--folder <폴더>]
  clear    인덱스 삭제                 [--doc <경로>] [-y]
  status   인덱스 상태                 [-v]
  backend  생성 백엔드(CPU/iGPU) 확인  [--reprobe]
  warmup   예열만 수행                 [--model M] [--precise]
  import   Python 판 인덱스 가져오기   --from <Python 데이터 폴더(예: dist\\simplerag)>
  suggest-synonyms  놓친 질문으로 동의어 사전 후보 뽑기  [--from <질문 jsonl>] [--out <결과 json>] [--no-llm]
";

// 인자 오류 — argparse 처럼 사용법과 함께 2 로 끝낸다
fn arg_error(msg: &str) -> ! {
    eprintln!("{}", USAGE.lines().next().unwrap());
    eprintln!("simplerag-rs: error: {}", msg);
    std::process::exit(2);
}

//------------------------------------------------------------------
// 인자 풀기
//------------------------------------------------------------------
fn parse(argv: &[String]) -> Args {
    let mut a = Args::default();
    if argv.is_empty() {
        arg_error("the following arguments are required: cmd");
    }
    if argv[0] == "-h" || argv[0] == "--help" {
        print!("{}", USAGE);
        std::process::exit(0);
    }
    a.cmd = argv[0].clone();
    let valid = ["index", "ask", "chat", "extract", "search", "clear", "status", "backend", "warmup", "import", "suggest-synonyms"];
    if !valid.contains(&a.cmd.as_str()) {
        arg_error(&format!("argument cmd: invalid choice: '{}' (choose from {})", a.cmd,
            valid.iter().map(|v| format!("'{}'", v)).collect::<Vec<_>>().join(", ")));
    }
    let mut i = 1;
    let mut positional: Vec<String> = vec![];
    let next = |i: &mut usize, name: &str| -> String {
        *i += 1;
        argv.get(*i).cloned().unwrap_or_else(|| arg_error(&format!("argument {}: expected one argument", name)))
    };
    let num = |s: String, name: &str| -> usize {
        s.parse().unwrap_or_else(|_| arg_error(&format!("argument {}: invalid int value: '{}'", name, s)))
    };
    while i < argv.len() {
        let t = argv[i].as_str();
        match t {
            "-h" | "--help" => {
                print!("{}", USAGE);
                std::process::exit(0);
            }
            "--dir" => a.dir = Some(next(&mut i, "--dir")),
            "--rebuild" => a.rebuild = true,
            "--no-recursive" => a.no_recursive = true,
            "--allow-delete" => a.allow_delete = true,
            "--max-add" => a.max_add = Some(num(next(&mut i, "--max-add"), "--max-add")),
            "--top-k" => a.top_k = Some(num(next(&mut i, "--top-k"), "--top-k")),
            "--max-tokens" => a.max_tokens = Some(num(next(&mut i, "--max-tokens"), "--max-tokens")),
            "--folder" => a.folder = Some(next(&mut i, "--folder")),
            "--model" => {
                let m = next(&mut i, "--model");
                if !config::USER_GEN_MODELS.contains(&m.as_str()) {
                    arg_error(&format!("argument --model: invalid choice: '{}' (choose from {})", m,
                        config::USER_GEN_MODELS.iter().map(|v| format!("'{}'", v)).collect::<Vec<_>>().join(", ")));
                }
                a.model = Some(m);
            }
            "--precise" => a.precise = true,
            "--no-stream" => a.no_stream = true,
            "--chars" => a.chars = Some(num(next(&mut i, "--chars"), "--chars")),
            "--blocks" => a.blocks = true,
            "--dump" => a.dump = Some(next(&mut i, "--dump")),
            "--doc" => a.doc = Some(next(&mut i, "--doc")),
            "-y" | "--yes" => a.yes = true,
            "-v" | "--verbose" => a.verbose = true,
            "--reprobe" => a.reprobe = true,
            "--from" => a.from = Some(next(&mut i, "--from")),
            "--out" => a.out = Some(next(&mut i, "--out")),
            "--no-llm" => a.no_llm = true,
            s if s.starts_with("--") => arg_error(&format!("unrecognized arguments: {}", s)),
            s => positional.push(s.to_string()),
        }
        i += 1;
    }
    match a.cmd.as_str() {
        "index" if a.dir.is_none() => arg_error("the following arguments are required: --dir"),
        "import" if a.from.is_none() => arg_error("the following arguments are required: --from"),
        "ask" => a.query = positional.first().cloned(),
        "search" => {
            if positional.is_empty() {
                arg_error("the following arguments are required: query");
            }
            a.query = positional.first().cloned();
        }
        "extract" => {
            if positional.is_empty() {
                arg_error("the following arguments are required: path");
            }
            a.path = positional.first().cloned();
        }
        _ => {}
    }
    a
}

// ── 콘솔 ───────────────────────────────────────────────────

static SAVED_ATTR: std::sync::Mutex<Option<u16>> = std::sync::Mutex::new(None);

// 시작 때 콘솔 글자 속성 저장
fn console_install() {
    use windows_sys::Win32::System::Console::{GetConsoleScreenBufferInfo, GetStdHandle, CONSOLE_SCREEN_BUFFER_INFO, STD_OUTPUT_HANDLE};
    unsafe {
        let h = GetStdHandle(STD_OUTPUT_HANDLE);
        let mut info: CONSOLE_SCREEN_BUFFER_INFO = std::mem::zeroed();
        if GetConsoleScreenBufferInfo(h, &mut info) != 0 {
            *SAVED_ATTR.lock().unwrap() = Some(info.wAttributes);
        }
    }
}

// 저장해 둔 속성으로 되돌리기
fn console_restore() {
    use windows_sys::Win32::System::Console::{GetStdHandle, SetConsoleTextAttribute, STD_OUTPUT_HANDLE};
    if let Some(a) = *SAVED_ATTR.lock().unwrap() {
        unsafe {
            SetConsoleTextAttribute(GetStdHandle(STD_OUTPUT_HANDLE), a);
        }
    }
}

// Ctrl+C 처리기 — 표시만 세우고 프로세스는 살린다
unsafe extern "system" fn ctrl_handler(kind: u32) -> i32 {
    if kind == 0 {
        cli::INTERRUPT.store(true, Ordering::SeqCst);
        return 1;
    }
    0
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    // 백엔드 측정용 자식 프로세스 — 출력(JSON 한 줄)이 오염되지 않게 가장 먼저 가로챈다
    if argv.first().map(|s| s == "_probe-backend").unwrap_or(false) {
        if let Err(e) = config::init() {
            eprintln!("설정 오류: {}", e);
            std::process::exit(2);
        }
        std::process::exit(simplerag::generate::backend::probe_main(&argv[1..]));
    }
    let cfg = match config::init() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("설정 오류: {}", e);
            std::process::exit(2);
        }
    };
    simplerag::embed::ensure_dylib();
    let args = parse(&argv);
    for w in &cfg.warnings {
        eprintln!("설정 경고: {}", w);
    }
    console_install();
    unsafe {
        windows_sys::Win32::System::Console::SetConsoleCtrlHandler(Some(ctrl_handler), 1);
    }
    let code = match args.cmd.as_str() {
        "index" => cli::cmd_index(&args),
        "ask" => cli::cmd_ask(&args),
        "chat" => cli::cmd_chat(&args),
        "extract" => cli::cmd_extract(&args),
        "search" => cli::cmd_search(&args),
        "clear" => cli::cmd_clear(&args),
        "status" => cli::cmd_status(&args),
        "backend" => cli::cmd_backend(&args),
        "warmup" => cli::cmd_warmup(&args),
        "import" => cli::cmd_import(&args),
        "suggest-synonyms" => cli::cmd_suggest(&args),
        _ => 2,
    };
    console_restore();
    std::process::exit(code);
}
