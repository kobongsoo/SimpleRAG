//------------------------------------------------------------------
// 사람 이름 승인 — 별칭 후보 읽기 · 승인한 별칭 목록 (메타데이터 검색 설계서 §7-2 · 6단계)
//=> SimpleRAG 의 "meta people suggest" 가 만든 후보(people_candidates.json)를 설정 창 사람 탭에 보여 준다.
//   [맞음] · [아님] · [빼기] · [나] 는 이 파일이 아니라 simplerag-rs 의 "meta people …" 명령이 people.yaml 에
//   적는다(같은 글 모양 · 고친 뒤 다시 정규화까지 한 곳에서). 여기서는 읽기만 한다.
//   워커는 people.yaml · meta.sqlite 수정 시각이 바뀌면 다음 찾기 때 이름 목록을 다시 읽는다.
//------------------------------------------------------------------

use serde_json::{json, Value};

pub const USER_NAME: &str = "people.yaml";
pub const CANDIDATES_NAME: &str = "people_candidates.json";
// 사람 탭에 한 번에 보여 줄 후보 줄 수 — 승인하면 다음 후보가 올라온다
pub const SHOW_ROWS: usize = 6;

//------------------------------------------------------------------
// 사람 사전 읽기
// -필드: people = [(대표 이름, [별칭…])] / rejected = [(별칭, 이름)] / me = "나" 의 대표 이름
//------------------------------------------------------------------
#[derive(Default)]
pub struct User {
    pub people: Vec<(String, Vec<String>)>,
    pub rejected: Vec<(String, String)>,
    pub me: Option<String>,
}

// YAML 값 → 글자
fn ystr(v: &serde_yaml::Value) -> String {
    match v {
        serde_yaml::Value::String(s) => s.trim().to_string(),
        other => serde_yaml::to_string(other).unwrap_or_default().trim().to_string(),
    }
}

//------------------------------------------------------------------
// people.yaml 읽기 — 없거나 못 읽으면 빈 사전(쓰기는 simplerag-rs 가 하므로 덮어쓸 걱정이 없다)
//
// -in: home = SimpleRAG 데이터 폴더
// -out: 사람 사전
//------------------------------------------------------------------
pub fn load_user(home: &str) -> User {
    let mut u = User::default();
    let text = match std::fs::read_to_string(std::path::Path::new(home).join(USER_NAME)) {
        Ok(t) => t,
        Err(_) => return u,
    };
    let y: serde_yaml::Value = match serde_yaml::from_str(&text) {
        Ok(y) => y,
        Err(_) => return u,
    };
    if let Some(m) = y.get("people").and_then(|v| v.as_mapping()) {
        for (n, al) in m {
            let als: Vec<String> = al.as_sequence().into_iter().flatten().map(ystr).filter(|s| !s.is_empty()).collect();
            u.people.push((ystr(n), als));
        }
    }
    for r in y.get("rejected").and_then(|v| v.as_sequence()).into_iter().flatten() {
        if let Some(s) = r.as_sequence() {
            if s.len() == 2 {
                u.rejected.push((ystr(&s[0]), ystr(&s[1])));
            }
        }
    }
    u.me = y.get("me").map(ystr).filter(|s| !s.is_empty() && s != "~" && s != "null");
    u
}

//------------------------------------------------------------------
// 아직 고르지 않은 후보 — 이미 승인했거나 [아님] 한 짝은 뺀다
//
// -in: home = 데이터 폴더 / n = 보여 줄 줄 수
// -out: (줄 [{alias, name, why}], 남은 후보 수)
//------------------------------------------------------------------
pub fn pending(home: &str, n: usize) -> (Vec<Value>, usize) {
    let text = match std::fs::read_to_string(std::path::Path::new(home).join(CANDIDATES_NAME)) {
        Ok(t) => t,
        Err(_) => return (vec![], 0),
    };
    let cands: Vec<Value> = serde_json::from_str(&text).unwrap_or_default();
    let u = load_user(home);
    let approved = |a: &str| u.people.iter().any(|(_, als)| als.iter().any(|x| x.eq_ignore_ascii_case(a)));
    let rejected = |a: &str, nm: &str| u.rejected.iter().any(|(x, y)| x.eq_ignore_ascii_case(a) && y == nm);
    let left: Vec<Value> = cands.into_iter().filter(|c| {
        let (a, nm) = (c["alias"].as_str().unwrap_or(""), c["name"].as_str().unwrap_or(""));
        !a.is_empty() && !approved(a) && !rejected(a, nm)
    }).collect();
    let total = left.len();
    let rows = left.into_iter().take(n).map(|c| {
        // 왜 후보인지 — 이름 모양 근거 · 같은 문서 · 파일명 · 예시 문서
        let mut why = vec![c["kind"].as_str().unwrap_or("").to_string()];
        if c["cooc"].as_u64().unwrap_or(0) > 0 {
            why.push(format!("같은 문서 {}건", c["cooc"]));
        }
        if c["path"].as_u64().unwrap_or(0) > 0 {
            why.push(format!("파일명 {}건", c["path"]));
        }
        why.push(format!("이 별칭 문서 {}건", c["docs"]));
        let ex: Vec<String> = c["examples"].as_array().into_iter().flatten().filter_map(|e| e.as_str())
            .take(2).map(|s| s.chars().take(30).collect()).collect();
        if !ex.is_empty() {
            why.push(format!("예: {}", ex.join(" / ")));
        }
        json!({"alias": c["alias"], "name": c["name"], "why": why.join(" · ")})
    }).collect();
    (rows, total)
}

//------------------------------------------------------------------
// 승인한 별칭 짝 — [[별칭, 대표 이름], …] (목록 · [빼기] 용)
//------------------------------------------------------------------
pub fn approved_pairs(home: &str) -> Vec<(String, String)> {
    let u = load_user(home);
    let mut v = vec![];
    for (n, als) in u.people {
        for a in als {
            v.push((a, n.clone()));
        }
    }
    v
}

//------------------------------------------------------------------
// 설정 창에 넘길 사람 탭 값
//
// -in: home = 데이터 폴더 / msg = 마지막 결과 글
// -out: {rows, n_left, approved, me, note}
//------------------------------------------------------------------
pub fn info(home: &str, msg: &str) -> Value {
    let (rows, n_left) = pending(home, SHOW_ROWS);
    let has_meta = std::path::Path::new(home).join("meta.sqlite").is_file();
    let note = if !msg.is_empty() {
        msg.to_string()
    } else if !has_meta {
        "아직 문서 속성(meta.sqlite)이 없습니다 — 인덱싱하면 만들어집니다".into()
    } else if n_left == 0 {
        "고를 후보가 없습니다 — [후보 만들기] 로 새로 찾습니다".into()
    } else {
        format!("남은 후보 {}개 — 같은 사람이면 [맞음]: 그 별칭으로 적힌 문서도 이 이름으로 찾습니다", n_left)
    };
    json!({"rows": rows, "n_left": n_left, "approved": approved_pairs(home), "me": load_user(home).me, "note": note})
}
