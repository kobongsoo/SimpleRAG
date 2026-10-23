//------------------------------------------------------------------
// 범위 폴더 (Python scope.py)
//=> 설정한 폴더 가운데 지금 보이는 것만 '활성' 으로 둔다(외장·네트워크 드라이브 대비, 몇 분마다 다시 확인).
//   경로 비교는 대소문자를 무시하고, 하위 폴더도 범위 안으로 본다.
//------------------------------------------------------------------

use std::time::{Duration, Instant};

//------------------------------------------------------------------
// 비교용 경로 표기 — 절대 경로·소문자·끝 구분자 없음(드라이브 루트는 둔다)
//------------------------------------------------------------------
pub fn norm(path: &str) -> String {
    if path.is_empty() {
        return String::new();
    }
    let abs = std::path::absolute(path).map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|_| path.to_string());
    let mut p = abs.replace('/', "\\").to_lowercase();
    while p.len() > 3 && p.ends_with('\\') {
        p.pop();
    }
    p
}

//------------------------------------------------------------------
// path 가 root 와 같거나 그 아래인가
//------------------------------------------------------------------
pub fn is_under(path: &str, root: &str) -> bool {
    let (p, r) = (norm(path), norm(root));
    if p.is_empty() || r.is_empty() {
        return false;
    }
    if p == r {
        return true;
    }
    p.starts_with(&if r.ends_with('\\') { r.clone() } else { format!("{}\\", r) })
}

//------------------------------------------------------------------
// 범위 — 설정 폴더 · 활성 폴더 · 마지막 확인 시각
//------------------------------------------------------------------
pub struct Scope {
    pub configured: Vec<String>,
    pub active: Vec<String>,
    recheck: Duration,
    checked_at: Option<Instant>,
}

impl Scope {
    pub fn new(folders: &[String], recheck_min: i64) -> Scope {
        let mut s = Scope { configured: folders.to_vec(), active: vec![], recheck: Duration::from_secs(recheck_min.max(1) as u64 * 60), checked_at: None };
        s.refresh(true);
        s
    }

    //------------------------------------------------------------------
    // 다시 확인 — 활성 목록이 바뀌었으면 true
    //------------------------------------------------------------------
    pub fn refresh(&mut self, force: bool) -> bool {
        if !force {
            if let Some(t) = self.checked_at {
                if t.elapsed() < self.recheck {
                    return false;
                }
            }
        }
        self.checked_at = Some(Instant::now());
        let found: Vec<String> = self.configured.iter().filter(|p| std::path::Path::new(p).is_dir()).cloned().collect();
        let changed = found != self.active;
        self.active = found;
        changed
    }

    pub fn usable(&self) -> bool {
        !self.active.is_empty()
    }

    //------------------------------------------------------------------
    // path 가 범위 안인가 — (안이면 true, 속한 루트 또는 이유)
    //------------------------------------------------------------------
    pub fn contains(&mut self, path: &str) -> (bool, String) {
        self.refresh(false);
        if self.active.is_empty() {
            return (false, "범위 폴더 미설정".into());
        }
        if path.is_empty() {
            return (false, "폴더를 알 수 없음".into());
        }
        for r in &self.active {
            if is_under(path, r) {
                return (true, r.clone());
            }
        }
        (false, "범위 밖".into())
    }
}
