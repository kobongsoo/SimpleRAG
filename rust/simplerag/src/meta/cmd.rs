//------------------------------------------------------------------
// 메타데이터 명령 (메타데이터 검색 설계서 §17, Python cli.cmd_meta · _meta_people)
//=> simplerag-rs meta [--db P] scan --dir D | rebuild | stats | show <경로> | people <하위 명령>
//   문서 속성을 뽑아 정규화하고 <데이터 루트>/meta.sqlite 에 둔다. 모델을 올리지 않는다.
//   출력 글자는 Python 판과 같다.
//------------------------------------------------------------------

use std::collections::HashSet;
use std::time::Instant;


use super::{people, pyfmt, store};
use crate::cli::outln;
use crate::config::cfg;
use crate::pyutil;

const USAGE: &str = "usage: simplerag-rs meta [--db DB] {scan,rebuild,stats,show,people} ...";

// 인자 오류 — argparse 처럼 사용법과 함께 2
fn arg_error(msg: &str) -> i32 {
    eprintln!("{}", USAGE);
    eprintln!("simplerag-rs meta: error: {}", msg);
    2
}

//------------------------------------------------------------------
// meta 명령 진입점
//    scan    — 폴더 아래 모든 파일의 속성을 읽고(바뀐 파일만) 끝에 다시 정규화
//    rebuild — 파일을 다시 읽지 않고 raw 로 다시 정규화(규칙 · 별칭 사전을 바꾼 뒤)
//    stats   — 채움률 · 상속 · 형식 · 사람 상위
//    show    — 문서 하나의 정규화 값 · 관련인 · 원본 값
//    people  — 사람 별칭 사전(suggest · list · approve · reject · remove · me)
//
// -in: argv = "meta" 뒤의 인자들
//
// -out: 종료 코드 — 0 = 성공, 1 = 실패(폴더 없음 · 문서 없음 · 잘못된 입력), 2 = 인자 오류
// -out: error = 없음
//------------------------------------------------------------------
pub fn run(argv: &[String]) -> i32 {
    let mut db: Option<String> = None;
    let mut dir: Option<String> = None;
    let mut pos: Vec<String> = vec![];
    let mut i = 0;
    while i < argv.len() {
        match argv[i].as_str() {
            "-h" | "--help" => {
                outln(USAGE);
                return 0;
            }
            "--db" => {
                i += 1;
                match argv.get(i) {
                    Some(v) => db = Some(v.clone()),
                    None => return arg_error("argument --db: expected one argument"),
                }
            }
            "--dir" => {
                i += 1;
                match argv.get(i) {
                    Some(v) => dir = Some(v.clone()),
                    None => return arg_error("argument --dir: expected one argument"),
                }
            }
            s if s.starts_with("--") => return arg_error(&format!("unrecognized arguments: {}", s)),
            s => pos.push(s.to_string()),
        }
        i += 1;
    }
    let sub = match pos.first() {
        Some(s) => s.clone(),
        None => return arg_error("the following arguments are required: meta_cmd"),
    };
    let valid = ["scan", "rebuild", "stats", "show", "people", "plan"];
    if !valid.contains(&sub.as_str()) {
        return arg_error(&format!("argument meta_cmd: invalid choice: '{}' (choose from 'scan', 'rebuild', 'stats', 'show', 'people')", sub));
    }
    if sub == "scan" && dir.is_none() {
        return arg_error("the following arguments are required: --dir");
    }
    if sub == "show" && pos.len() < 2 {
        return arg_error("the following arguments are required: path");
    }
    let db_path = db.clone().unwrap_or_else(|| store::default_path(&cfg().root));
    // --db 를 주면 규칙 · 사람 사전도 그 파일 옆에서 읽고 쓴다(시험용 DB 가 데이터 루트를 건드리지 않게)
    let root = if db.is_some() { pyutil::dirname(&pyutil::abspath(&db_path)) } else { cfg().root.clone() };
    let conn = match store::connect(&db_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("meta.sqlite 를 열지 못했습니다: {}", e);
            return 1;
        }
    };
    let r: Result<i32, String> = (|| {
        match sub.as_str() {
            "people" => return people_cmd(&pos[1..], &conn, &root),
            // 질의 해석 · 계획 점검(시험용) — 질의 파일 한 줄에 하나, 결과를 JSON 한 줄씩
            "plan" => return plan_cmd(&pos[1..], &db_path, &root),
            "scan" => {
                let res = match store::scan(&conn, dir.as_deref().unwrap(), &root, &mut |s| outln(s)) {
                    Ok(r) => r,
                    Err(store::ScanError::NotFound(f)) => {
                        outln(&format!("폴더가 없습니다: {}", f));
                        return Ok(1);
                    }
                    Err(store::ScanError::Sql(e)) => return Err(e.to_string()),
                };
                outln(&format!("메타데이터: 파일 {}개 · 읽음 {} · 그대로 {} · 빠짐 {} · 실패 {} / 문서 {} · 상속 {} · 관련인 {} / {}초",
                    res.files, res.read, res.skipped, res.removed, res.failed, res.renorm.docs, res.renorm.inherited,
                    res.renorm.people, pyutil::float_repr(res.sec)));
                outln(&format!("저장: {}", db_path));
            }
            "rebuild" => {
                let t0 = Instant::now();
                let res = store::renormalize(&conn, &root, None).map_err(|e| e.to_string())?;
                outln(&format!("다시 정규화: 문서 {} · 상속 {} · 관련인 {} / {:.1}초", res.docs, res.inherited, res.people,
                    t0.elapsed().as_secs_f64()));
            }
            "stats" => {
                let s = store::stats(&conn).map_err(|e| e.to_string())?;
                outln(&pyfmt::dumps(&s, Some(1)));
            }
            "show" => {
                match store::show(&conn, &pos[1]).map_err(|e| e.to_string())? {
                    Some(v) => outln(&pyfmt::dumps(&v, Some(1))),
                    None => {
                        outln(&format!("meta.sqlite 에 없는 문서입니다: {}", pos[1]));
                        return Ok(1);
                    }
                }
            }
            _ => {}
        }
        Ok(0)
    })();
    match r {
        Ok(c) => c,
        Err(e) => {
            eprintln!("메타데이터 명령 실패: {}", e);
            1
        }
    }
}

//------------------------------------------------------------------
// 사람 별칭 사전 명령 (메타데이터 검색 설계서 §7-2 · 3단계)
//=> suggest 로 후보를 만들고, approve · reject 로 고른다. 사전을 바꾸면 곧바로 다시 정규화한다.
//    suggest              — 후보 만들기(people_candidates.json) · 상위 후보 출력
//    list                 — 승인한 별칭 · 무시한 짝 · 나
//    approve <별칭> <이름> — 승인(직접 추가도 같다)  ·  reject <별칭> <이름> — 다시 제안하지 않음
//    remove <별칭>         — 승인한 별칭 빼기  ·  me <이름> — "나" 정하기(이 PC 계정도 별칭으로)
//
// -in: args = [하위 명령, 별칭?, 이름?] / conn = meta.sqlite / root = 사전 폴더
//
// -out: Ok(0) = 성공, Ok(1) = 잘못된 입력, Ok(2) = 인자 오류
// -out: error = 사전 쓰기 · sqlite 실패는 Err(오류문)
//------------------------------------------------------------------
fn people_cmd(args: &[String], conn: &rusqlite::Connection, root: &str) -> Result<i32, String> {
    let choices = ["suggest", "list", "approve", "reject", "remove", "me"];
    let pc = match args.first() {
        Some(p) => p.as_str(),
        None => return Ok(arg_error("the following arguments are required: people_cmd")),
    };
    if !choices.contains(&pc) {
        return Ok(arg_error(&format!("argument people_cmd: invalid choice: '{}' (choose from 'suggest', 'list', 'approve', 'reject', 'remove', 'me')", pc)));
    }
    if args.len() > 3 {
        return Ok(arg_error(&format!("unrecognized arguments: {}", args[3..].join(" "))));
    }
    let alias = args.get(1).cloned();
    let name = args.get(2).cloned();
    if pc == "suggest" {
        let cands = people::suggest(conn, root).map_err(|e| e.to_string())?;
        outln(&format!("별칭 후보 {}개 (점수 순 — 이름 모양 로마자 5 · 이름 일부 3 · 이니셜 2 + 같은 문서×2 + 파일명)", cands.len()));
        for c in cands.iter().take(40) {
            let mut why = vec![c.kind.to_string()];
            if c.cooc > 0 {
                why.push(format!("같은 문서 {}건", c.cooc));
            }
            if c.path > 0 {
                why.push(format!("파일명 {}건", c.path));
            }
            let ex: Vec<&str> = c.examples.iter().take(2).map(|e| pyutil::head(e, 28)).collect();
            outln(&format!("  {:>3}  {:<22} → {:<6} ({}) 예: {}", c.score, pyutil::head(&c.alias, 22), c.name, why.join(" · "), ex.join(" / ")));
        }
        outln("승인: simplerag meta people approve <별칭> <이름>   무시: ... reject <별칭> <이름>");
        return Ok(0);
    }
    if pc == "list" {
        outln(&pyfmt::dumps(&people::load_user(root).to_json(), Some(1)));
        return Ok(0);
    }
    if (pc == "approve" || pc == "reject") && !(alias.as_deref().map(|s| !s.is_empty()).unwrap_or(false)
        && name.as_deref().map(|s| !s.is_empty()).unwrap_or(false)) {
        outln(&format!("별칭과 이름을 모두 적으세요: meta people {} <별칭> <이름>", pc));
        return Ok(1);
    }
    let io = |e: std::io::Error| format!("OSError: {}", e);
    let msg = match pc {
        "approve" => people::approve(root, alias.as_deref().unwrap(), name.as_deref().unwrap()).map_err(io)?,
        "reject" => people::reject(root, alias.as_deref().unwrap(), name.as_deref().unwrap()).map_err(io)?,
        "remove" => people::remove(root, alias.as_deref().unwrap_or("")).map_err(io)?,
        // me 는 이름 하나만 받는다(첫 칸에 적어도 된다)
        _ => {
            let n = name.clone().filter(|s| !s.is_empty()).or_else(|| alias.clone()).unwrap_or_default();
            people::set_me(root, &n).map_err(io)?
        }
    };
    outln(&msg);
    let res = store::renormalize(conn, root, None::<&HashSet<String>>).map_err(|e| e.to_string())?;
    outln(&format!("다시 정규화: 문서 {} · 관련인 {}", res.docs, res.people));
    Ok(0)
}

//------------------------------------------------------------------
// meta plan <질의 파일> [YYYY-MM-DD] — 질의 해석과 필터 · 가산 계획을 JSON 줄로 (Python 과 대조용)
//=> 1) 파일의 줄마다 parse → 해석 결과 2) 조건이 있으면 계획(칩 · 완화 · 문서 수 · 가산 합)
//
// -in: args = [질의 파일, 기준 날짜(없으면 오늘)] / db = meta.sqlite / root = 사전 폴더
// -out: 0 / 파일을 못 읽으면 1
//------------------------------------------------------------------
fn plan_cmd(args: &[String], db: &str, root: &str) -> Result<i32, String> {
    use super::query as Q;
    let text = match args.first().and_then(|f| std::fs::read_to_string(f).ok()) {
        Some(t) => t,
        None => {
            outln("질의 파일을 읽지 못했습니다");
            return Ok(1);
        }
    };
    let today = args.get(1).and_then(|d| chrono::NaiveDate::parse_from_str(d, "%Y-%m-%d").ok())
        .unwrap_or_else(|| (chrono::Utc::now() + chrono::Duration::hours(9)).date_naive());
    // 사람 이름 목록 — 색인된 한글 이름 + 별칭 값
    let re = regex::Regex::new(r"^[가-힣]{2,4}$").unwrap();
    let mut names: std::collections::HashSet<String> = Q::rows(db).map(|c| c.by_name.keys().filter(|n| re.is_match(n)).cloned().collect())
        .unwrap_or_default();
    let aliases = super::normalize::load_aliases(root);
    names.extend(aliases.values().filter(|v| re.is_match(v)).cloned());
    let me = super::people::load_user(root).me;
    let rules = super::rules::load_rules(root);
    for q in text.lines().map(|l| l.trim()).filter(|l| !l.is_empty()) {
        let p = Q::parse(q, &names, &aliases, me.as_deref(), &rules.doc_types, today);
        let mut v = serde_json::json!({
            "q": q, "query": p.query, "conditions": p.conditions,
            "people": p.people.iter().map(|(n, r, m)| serde_json::json!([n, r, m])).collect::<Vec<_>>(),
            "date": p.date.as_ref().map(|d| serde_json::json!([d.from, d.to, d.label])),
            "date_field": p.date_field, "format": p.format, "recv": p.recv, "scanned": p.scanned,
            "doc_type": p.doc_type, "is_form": p.is_form,
        });
        if let Some(pl) = Q::plan(q, db, root, None, Some(today)) {
            let sum: f64 = pl.boosts.values().sum();
            v["plan"] = serde_json::json!({
                "chips": pl.chips.iter().map(|c| format!("{}:{}", c.label, c.mode)).collect::<Vec<_>>(),
                "relaxed": pl.relaxed, "docs": pl.docs.as_ref().map(|d| d.len()), "boosts": pl.boosts.len(),
                "boost_sum": (sum * 100.0).round() / 100.0, "meta_only": pl.meta_only,
                "top": pl.order.iter().take(5).cloned().collect::<Vec<_>>(), "ms": pl.ms,
            });
        }
        outln(&v.to_string());
    }
    Ok(0)
}
