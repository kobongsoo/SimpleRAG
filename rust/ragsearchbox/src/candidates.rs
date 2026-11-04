//------------------------------------------------------------------
// 사전 후보 알림 — 동의어 · 이름 별칭 후보를 쉬는 동안 뒤에서 만들고, 새 후보가 생기면 하루 한 번 알린다
//=> 예전에는 설정 창 [후보 만들기] 를 눌러야 후보가 생겨 사전이 잘 늘지 않았다.
//    1) 이름 별칭 — 하루 한 번, 쉬는 동안(가벼운 명령)
//    2) 동의어 — 지난번 뒤로 놓친 질문이 10개 넘게 쌓였을 때만, 쉬는 동안(1.7B 로 1~3분 · 질문을 막지 않게)
//    3) 알림 — 아직 고르지 않은 후보 묶음이 지난 알림과 다를 때만, 하루 한 번까지. 누르면 설정 창 그 탭
//   상태는 INI [UI] CandidateState 에 JSON 한 줄로 둔다.
//------------------------------------------------------------------
use serde_json::{json, Value};

use crate::{people, synonyms};

// 쉬는 것으로 볼 시간 — 화면을 이만큼 건드리지 않았을 때만 뒤에서 만든다
pub const IDLE_SECS: u64 = 600;
// 같은 일을 다시 할 간격 — 알림 · 이름 별칭 만들기 · 동의어 만들기 모두 하루
pub const DAY_SECS: i64 = 86_400;
// 동의어 후보를 다시 만들 만큼 새로 쌓인 놓친 질문 수
pub const NEW_MISSES: usize = 10;

//------------------------------------------------------------------
// 알림 상태 — INI 에 남겨 다시 실행해도 같은 후보를 또 알리지 않는다
// -필드: last_notify = 마지막 알림 시각(유닉스 초)
// -필드: last_sig    = 마지막으로 알린 후보 묶음 서명
// -필드: ppl_at      = 이름 별칭 후보를 마지막으로 자동으로 만든 시각
// -필드: syn_at      = 동의어 후보를 마지막으로 자동으로 만든 시각
// -필드: syn_base    = 그때의 놓친 질문 수(이만큼에서 NEW_MISSES 가 더 쌓이면 다시 만든다)
//------------------------------------------------------------------
#[derive(Default, Clone, PartialEq, Debug)]
pub struct CandState {
    pub last_notify: i64,
    pub last_sig: String,
    pub ppl_at: i64,
    pub syn_at: i64,
    pub syn_base: usize,
}

impl CandState {
    //------------------------------------------------------------------
    // INI 글 → 상태 — 비었거나 깨졌으면 처음 상태
    //------------------------------------------------------------------
    pub fn parse(raw: &str) -> CandState {
        let v: Value = serde_json::from_str(raw).unwrap_or(Value::Null);
        CandState {
            last_notify: v["last_notify"].as_i64().unwrap_or(0),
            last_sig: v["last_sig"].as_str().unwrap_or("").to_string(),
            ppl_at: v["ppl_at"].as_i64().unwrap_or(0),
            syn_at: v["syn_at"].as_i64().unwrap_or(0),
            syn_base: v["syn_base"].as_u64().unwrap_or(0) as usize,
        }
    }

    // 상태 → INI 글 한 줄
    pub fn text(&self) -> String {
        json!({"last_notify": self.last_notify, "last_sig": self.last_sig, "ppl_at": self.ppl_at,
               "syn_at": self.syn_at, "syn_base": self.syn_base}).to_string()
    }
}

// 지금 시각(유닉스 초)
pub fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

//------------------------------------------------------------------
// 아직 고르지 않은 후보 — (동의어 낱말 수, 이름 별칭 수, 묶음 서명)
//=> 서명은 남은 후보 이름들을 정렬해 이은 글의 해시 — 승인 · [아님] 으로 줄거나 새 후보가 생기면 바뀐다
//------------------------------------------------------------------
pub fn pending(home: &str) -> (usize, usize, String) {
    let (syn_rows, n_syn) = synonyms::pending(home, usize::MAX);
    let (ppl_rows, n_ppl) = people::pending(home, usize::MAX);
    let mut keys: Vec<String> = syn_rows.iter().map(|r| format!("s:{}={}", r["word"].as_str().unwrap_or(""), r["terms"])).collect();
    keys.extend(ppl_rows.iter().map(|r| format!("p:{}={}", r["alias"].as_str().unwrap_or(""), r["name"].as_str().unwrap_or(""))));
    keys.sort();
    // FNV-1a — 같은 묶음이면 늘 같은 값(실행마다 바뀌는 기본 해시는 쓰지 않는다)
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in keys.join("\n").bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    (n_syn, n_ppl, if keys.is_empty() { String::new() } else { format!("{:016x}", h) })
}

//------------------------------------------------------------------
// 알릴까 — 남은 후보가 있고, 지난번에 알린 묶음과 다르고, 하루가 지났을 때만
//------------------------------------------------------------------
pub fn should_notify(st: &CandState, sig: &str, t: i64) -> bool {
    !sig.is_empty() && sig != st.last_sig && t - st.last_notify >= DAY_SECS
}

// 알림 글 — "동의어 후보 5개 · 이름 별칭 후보 3개가 있습니다 — 눌러서 확인하세요"
pub fn message(n_syn: usize, n_ppl: usize) -> String {
    let mut parts = vec![];
    if n_syn > 0 {
        parts.push(format!("동의어 후보 {}개", n_syn));
    }
    if n_ppl > 0 {
        parts.push(format!("이름 별칭 후보 {}개", n_ppl));
    }
    format!("{}가 있습니다 — 눌러서 확인하고 등록하세요", parts.join(" · "))
}

// 동의어 후보를 다시 만들 때인가 — 놓친 질문이 충분히 쌓였고 하루가 지났을 때
pub fn syn_due(st: &CandState, n_miss: usize, t: i64) -> bool {
    n_miss >= st.syn_base + NEW_MISSES && t - st.syn_at >= DAY_SECS
}

// 이름 별칭 후보를 다시 만들 때인가 — 하루에 한 번
pub fn ppl_due(st: &CandState, t: i64) -> bool {
    t - st.ppl_at >= DAY_SECS
}

#[cfg(test)]
mod tests {
    use super::*;

    // 알림 · 자동 만들기 판정
    #[test]
    fn rules() {
        let mut st = CandState::default();
        let t = 1_800_000_000;
        assert!(!should_notify(&st, "", t), "후보가 없으면 알리지 않는다");
        assert!(should_notify(&st, "abc", t));
        st.last_sig = "abc".into();
        st.last_notify = t;
        assert!(!should_notify(&st, "abc", t + 3 * DAY_SECS), "같은 묶음은 다시 알리지 않는다");
        assert!(!should_notify(&st, "def", t + 100), "하루 안에는 다시 알리지 않는다");
        assert!(should_notify(&st, "def", t + DAY_SECS));
        assert!(!syn_due(&st, 9, t) && syn_due(&st, 10, t));
        st.syn_base = 10;
        st.syn_at = t;
        assert!(!syn_due(&st, 25, t + 100) && syn_due(&st, 20, t + DAY_SECS) && !syn_due(&st, 19, t + DAY_SECS));
        assert_eq!(CandState::parse(&st.text()), st);
        assert_eq!(CandState::parse("깨짐"), CandState::default());
        assert_eq!(message(5, 0), "동의어 후보 5개가 있습니다 — 눌러서 확인하고 등록하세요");
        assert_eq!(message(2, 3), "동의어 후보 2개 · 이름 별칭 후보 3개가 있습니다 — 눌러서 확인하고 등록하세요");
    }
}
