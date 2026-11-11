//------------------------------------------------------------------
// PDF 테두리 표 검출 — 선 격자 → 셀 (Python docmodel/pdftable.py 와 같은 규칙)
//=> PDF 에는 '표'라는 표시가 없다. 대신 표 테두리는 가로·세로 선(경로 객체)으로 그려져 있다.
//   선과 글자 위치는 pdfium.rs 가 읽어 오고, 여기서는 그 좌표만으로 표를 만든다.
//    1) 얇고 긴 경로는 선, 사각형은 네 변을 선으로 모은다(pdfium.rs::page_segments)
//    2) 서로 닿는 선끼리 묶어 표 후보로 만든다(가로 2개·세로 2개 이상) — components
//    3) 선 좌표로 격자를 만들고, 경계선이 없는 칸끼리는 병합 셀로 묶는다 — build
//    4) 글자를 셀에 넣는다 — 가로는 글자 상자 중심, 세로는 기준선
//    5) 장식 상자·글 상자·목차 상자처럼 표가 아닌 것을 거른다 — looks_like_table
//   테두리 없는 표는 찾지 않는다 — 기존처럼 줄 단위 본문으로 남는다.
//   실측: report/PDF파서_비교_OpenDataLoader_20261111.html §7
//------------------------------------------------------------------

use std::collections::{BTreeSet, HashMap, HashSet};

use once_cell::sync::Lazy;
use regex::Regex;

use crate::pyutil;

pub const TOL: f64 = 2.0; // 같은 선·같은 좌표로 보는 차이(pt)
pub const THIN: f64 = 2.5; // 이보다 얇으면 선
pub const MIN_SEG: f64 = 3.0; // 이보다 짧은 선은 무시(글자 장식·밑줄 조각)
pub const MAX_PATHS: usize = 5000; // 경로가 이보다 많으면 그림 쪽으로 보고 건너뜀(속도 방어)
const MIN_REAL_ROWS: usize = 2; // 서로 다른 글이 2칸 이상인 행이 이만큼은 있어야 표
const MIN_REAL_SHARE: f64 = 0.5; // …그리고 그런 행이 전체 행의 이 비율 이상
const MAX_EMPTY_SHARE: f64 = 0.5; // 빈 칸이 이 비율을 넘으면 장식 상자
const MIN_TABLE_CHARS: usize = 10; // 표 전체 글자가 이보다 적으면 표지 장식
const MAX_ONE_CELL: f64 = 0.7; // 한 셀 글자가 전체의 이 비율을 넘으면 글 상자
const MAX_MEDIAN_CELL: usize = 200; // 셀 글자 수 중앙값이 이보다 길면 2단 편집 상자
const MAX_BREAK_SHARE: f64 = 0.3; // 셀 안 줄바꿈이 글자 수의 이 비율을 넘으면 세로 글씨
const MAX_EXPAND: f64 = 2.5; // 병합 셀을 펼친 글이 원래 글의 이 배를 넘으면 레이아웃 격자

static LEADER: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?:\.{3,}|·{3,}|…{2,}|-{4,})\s*\d{0,4}").unwrap());

// 선 하나 — (좌표, 시작, 끝). 가로선은 (y, x0, x1), 세로선은 (x, y0, y1). PDF 좌표(y 는 위로 증가)
pub type Seg = (f64, f64, f64);

//------------------------------------------------------------------
// 글자 하나의 위치
// -필드: ch  = 글자
// -필드: cx  = 글자 상자 가로 중심
// -필드: y   = 기준선(쉼표처럼 낮은 글자도 같은 줄로)
// -필드: pos = 쪽 글(get_text_range) 안의 글자 위치
//------------------------------------------------------------------
pub struct CharPos {
    pub ch: char,
    pub cx: f64,
    pub y: f64,
    pub pos: usize,
}

//------------------------------------------------------------------
// 찾은 표 하나
// -필드: rows = 행 × 칸 글자(병합 셀은 펼쳐 같은 글자로 채움)
// -필드: bbox = (x0, y0, x1, y1)
// -필드: used = 셀에 들어간 글자 위치(쪽 글 기준) — 본문 줄에서 뺄 글자
//------------------------------------------------------------------
pub struct Table {
    pub rows: Vec<Vec<String>>,
    pub bbox: (f64, f64, f64, f64),
    pub used: BTreeSet<usize>,
}

//------------------------------------------------------------------
// 가까운 좌표끼리 하나로
//=> 선 두께·그리기 오차로 같은 경계가 1~2pt 씩 어긋난다. TOL 안의 값은 평균 하나로 친다.
// -in: vals = 좌표 목록
// -out: 오름차순 대표 좌표
//------------------------------------------------------------------
fn cluster(vals: &[f64]) -> Vec<f64> {
    let mut v = vals.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mut groups: Vec<Vec<f64>> = vec![];
    for x in v {
        match groups.last_mut() {
            // 바로 앞 값과 TOL 안이면 같은 무리
            Some(g) if x - g[g.len() - 1] <= TOL => g.push(x),
            _ => groups.push(vec![x]),
        }
    }
    // 파이썬 sum(g)/len(g) 와 같은 순서로 더한다(소수 오차까지 같게)
    groups.iter().map(|g| g.iter().fold(0.0, |s, x| s + x) / g.len() as f64).collect()
}

// 합집합-찾기의 대표 찾기
fn find(par: &mut [usize], mut a: usize) -> usize {
    while par[a] != a {
        par[a] = par[par[a]];
        a = par[a];
    }
    a
}

//------------------------------------------------------------------
// 서로 닿는 선끼리 표 후보로 묶기
//=> 가로선과 세로선이 (오차 안에서) 교차하거나 맞닿으면 같은 표다.
// -in: h = 가로선 목록 / v = 세로선 목록
// -out: [(가로선들, 세로선들)] — 각 2개 이상인 묶음만, 첫 선이 나온 순서
//------------------------------------------------------------------
pub fn components(h: &[Seg], v: &[Seg]) -> Vec<(Vec<Seg>, Vec<Seg>)> {
    let n = h.len() + v.len();
    let mut par: Vec<usize> = (0..n).collect();
    for (i, &(y, x0, x1)) in h.iter().enumerate() {
        for (j, &(x, y0, y1)) in v.iter().enumerate() {
            if x0 - TOL <= x && x <= x1 + TOL && y0 - TOL <= y && y <= y1 + TOL {
                let a = find(&mut par, i);
                let b = find(&mut par, h.len() + j);
                par[a] = b;
            }
        }
    }
    // 대표별 묶음 — 파이썬 dict 처럼 처음 나온 순서를 지킨다
    let mut order: Vec<Vec<usize>> = vec![];
    let mut slot: HashMap<usize, usize> = HashMap::new();
    for i in 0..n {
        let r = find(&mut par, i);
        let k = *slot.entry(r).or_insert_with(|| {
            order.push(vec![]);
            order.len() - 1
        });
        order[k].push(i);
    }
    let mut out = vec![];
    for idx in order {
        let hh: Vec<Seg> = idx.iter().filter(|&&i| i < h.len()).map(|&i| h[i]).collect();
        let vv: Vec<Seg> = idx.iter().filter(|&&i| i >= h.len()).map(|&i| v[i - h.len()]).collect();
        if hh.len() >= 2 && vv.len() >= 2 {
            out.push((hh, vv));
        }
    }
    out
}

//------------------------------------------------------------------
// 경계선이 있나 — 칸 경계 위치에 그 칸의 가운데를 지나는 선이 있으면 경계, 없으면 병합 셀
// -in: segs = 같은 방향 선 / at = 경계 좌표 / a, b = 칸의 시작·끝(반대 축)
//------------------------------------------------------------------
fn has_line(segs: &[Seg], at: f64, a: f64, b: f64) -> bool {
    let m = (a + b) / 2.0;
    segs.iter().any(|&(s, s0, s1)| (s - at).abs() <= TOL && s0 - TOL <= m && m <= s1 + TOL)
}

// 병합 셀 대표 찾기(경로 압축 없이 — 묶음만 같으면 결과가 같다)
fn root(par: &[usize], mut a: usize) -> usize {
    while par[a] != a {
        a = par[a];
    }
    a
}

//------------------------------------------------------------------
// 표 후보 하나 → 행렬
//=> 격자를 만들고 병합 셀을 묶은 뒤 글자를 넣는다. 병합 셀은 펼쳐 같은 글자로 채운다.
//    · 위·아래 끝의 전체 폭 한 칸 행(쪽 배경 상자에 딸려 온 제목·문단)은 떼어 본문에 남긴다
//    · 빈 행·열은 지우고, 병합 셀로 똑같이 되풀이되는 이웃 열은 하나로 합친다
//    · 셀 안 줄바꿈이 글자 수의 MAX_BREAK_SHARE 를 넘으면 세로 글씨 장식으로 보고 버린다
//    · 병합 셀을 펼친 글이 원래 글의 MAX_EXPAND 배를 넘으면 쪽 레이아웃 격자로 보고 버린다
// -in: h, v  = 이 표의 가로·세로선
// -in: chars = 쪽 전체 글자 위치
// -out: Some(Table) / 표로 볼 수 없으면 None
//------------------------------------------------------------------
fn build(h: &[Seg], v: &[Seg], chars: &[CharPos]) -> Option<Table> {
    let mut ys = cluster(&h.iter().map(|s| s.0).collect::<Vec<_>>());
    ys.reverse(); // 위 → 아래
    let xs = cluster(&v.iter().map(|s| s.0).collect::<Vec<_>>()); // 왼쪽 → 오른쪽
    if ys.len() < 2 || xs.len() < 3 {
        return None;
    }
    let (nr, nc) = (ys.len() - 1, xs.len() - 1);
    let id = |r: usize, c: usize| r * nc + c;
    let mut par: Vec<usize> = (0..nr * nc).collect();
    for r in 0..nr {
        for c in 0..nc {
            // 오른쪽 경계선이 없으면 옆 칸과, 아래 경계선이 없으면 아래 칸과 같은 셀
            if c + 1 < nc && !has_line(v, xs[c + 1], ys[r + 1], ys[r]) {
                let a = root(&par, id(r, c + 1));
                par[a] = root(&par, id(r, c));
            }
            if r + 1 < nr && !has_line(h, ys[r + 1], xs[c], xs[c + 1]) {
                let a = root(&par, id(r + 1, c));
                par[a] = root(&par, id(r, c));
            }
        }
    }

    let mut text: HashMap<usize, String> = HashMap::new();
    let mut last_y: HashMap<usize, f64> = HashMap::new();
    let mut idxs: HashMap<usize, BTreeSet<usize>> = HashMap::new();
    let mut breaks = 0usize; // 셀 안 줄바꿈 수 — 세로 글씨 판정용
    for cp in chars {
        if !(xs[0] <= cp.cx && cp.cx <= xs[nc] && ys[nr] <= cp.y && cp.y <= ys[0]) {
            continue;
        }
        let c = (0..nc).filter(|&i| xs[i] <= cp.cx).max().unwrap_or(0);
        let r = (0..nr).filter(|&i| ys[i] >= cp.y).max().unwrap_or(0);
        let key = root(&par, id(r.min(nr - 1), c.min(nc - 1)));
        let s = text.entry(key).or_default();
        // 셀 안에서 줄이 바뀌면 공백 하나 — 줄바꿈된 낱말 사이가 붙지 않게
        if let Some(ly) = last_y.get(&key) {
            if (ly - cp.y).abs() > 3.0 {
                breaks += 1;
                if !s.is_empty() && !s.ends_with(' ') {
                    s.push(' ');
                }
            }
        }
        // U+FFFE · U+FFFF 는 pdfium 이 못 읽은 글자 — 버린다
        if cp.ch != '\u{fffe}' && cp.ch != '\u{ffff}' {
            s.push(cp.ch);
        }
        last_y.insert(key, cp.y);
        idxs.entry(key).or_default().insert(cp.pos);
    }
    if idxs.is_empty() {
        return None;
    }
    // 글자마다 줄이 바뀌면 세로 글씨(옆 띠 장식) — 표가 아니다
    let n_used: usize = idxs.values().map(|s| s.len()).sum();
    if breaks as f64 > n_used as f64 * MAX_BREAK_SHARE {
        return None;
    }

    let mut keys: Vec<Vec<usize>> = (0..nr).map(|r| (0..nc).map(|c| root(&par, id(r, c))).collect()).collect();
    let cell = |k: &usize| -> String {
        pyutil::split_ws(text.get(k).map(|s| s.as_str()).unwrap_or("")).join(" ")
    };
    let mut rows: Vec<Vec<String>> = keys.iter().map(|kr| kr.iter().map(&cell).collect()).collect();
    // 위·아래 끝의 '전체 폭 한 칸' 행은 표 밖 글이다 — 떼어 본문으로 돌린다
    let one_cell = |kr: &Vec<usize>| kr.iter().collect::<HashSet<_>>().len() == 1;
    while !keys.is_empty() && one_cell(&keys[0]) {
        keys.remove(0);
        rows.remove(0);
    }
    while !keys.is_empty() && one_cell(&keys[keys.len() - 1]) {
        keys.pop();
        rows.pop();
    }
    if rows.is_empty() {
        return None;
    }
    let mut used: BTreeSet<usize> = BTreeSet::new();
    for kr in &keys {
        for k in kr.iter().collect::<HashSet<_>>() {
            if let Some(s) = idxs.get(k) {
                used.extend(s.iter().copied());
            }
        }
    }
    // 빈 행·빈 열을 지우고, 병합 셀 때문에 똑같이 되풀이되는 이웃 열은 하나로 합친다
    let pairs: Vec<(&Vec<String>, &Vec<usize>)> =
        rows.iter().zip(keys.iter()).filter(|(row, _)| row.iter().any(|c| !c.is_empty())).collect();
    let cols: Vec<usize> = (0..nc).filter(|&c| pairs.iter().any(|(row, _)| !row[c].is_empty())).collect();
    let mut dedup: Vec<usize> = vec![];
    for c in cols {
        if let Some(&last) = dedup.last() {
            if pairs.iter().all(|(_, kr)| kr[c] == kr[last]) {
                continue;
            }
        }
        dedup.push(c);
    }
    let out_rows: Vec<Vec<String>> = pairs.iter().map(|(row, _)| dedup.iter().map(|&c| row[c].clone()).collect()).collect();
    if out_rows.len() < 2 || dedup.len() < 2 || used.is_empty() {
        return None;
    }
    // 병합 셀을 펼친 글이 원래 글의 몇 배로 불어나면 쪽 레이아웃 격자다 — 표로 쓰지 않는다
    let uniq_keys: HashSet<usize> = pairs.iter().flat_map(|(_, kr)| dedup.iter().map(move |&c| kr[c])).collect();
    let unique: usize = uniq_keys.iter().map(|k| text.get(k).map(|s| s.chars().count()).unwrap_or(0)).sum();
    let expanded: usize = out_rows.iter().flatten().map(|c| c.chars().count()).sum();
    if unique > 0 && expanded as f64 > unique as f64 * MAX_EXPAND {
        return None;
    }
    Some(Table { rows: out_rows, bbox: (xs[0], ys[nr], xs[nc], ys[0]), used })
}

//------------------------------------------------------------------
// 표가 아닌 상자 거르기
//    1) 서로 다른 글이 2칸 이상 있는 행이 MIN_REAL_ROWS 개 미만이거나 전체의 MIN_REAL_SHARE 미만
//    2) 빈 칸이 MAX_EMPTY_SHARE 초과 → 장식 띠·세로 글씨 상자
//    3) 표 전체 글자가 MIN_TABLE_CHARS 미만 → 표지 장식
//    4) 셀 글자 수 중앙값이 MAX_MEDIAN_CELL 초과 → 2단 편집 상자
//    5) 한 셀 글자가 전체의 MAX_ONE_CELL 초과 → 글 상자
//    6) 점선 리더(……… 12)가 2칸 이상 → 목차 상자
// -out: true 면 표로 쓴다
//------------------------------------------------------------------
fn looks_like_table(rows: &[Vec<String>]) -> bool {
    let real = rows.iter()
        .filter(|row| row.iter().filter(|c| !c.is_empty()).collect::<HashSet<_>>().len() >= 2)
        .count();
    if real < MIN_REAL_ROWS || (real as f64) < rows.len() as f64 * MIN_REAL_SHARE {
        return false;
    }
    let n_cells: usize = rows.iter().map(|r| r.len()).sum();
    let empty = rows.iter().flatten().filter(|c| c.is_empty()).count();
    if empty as f64 > n_cells as f64 * MAX_EMPTY_SHARE {
        return false;
    }
    // 서로 다른 셀 글 → 글자 수
    let mut distinct: HashMap<&str, usize> = HashMap::new();
    for c in rows.iter().flatten() {
        if !c.is_empty() {
            distinct.insert(c.as_str(), c.chars().count());
        }
    }
    let total: usize = distinct.values().sum();
    if total < MIN_TABLE_CHARS {
        return false;
    }
    let mut lens: Vec<usize> = distinct.values().copied().collect();
    lens.sort_unstable();
    if lens[lens.len() / 2] > MAX_MEDIAN_CELL {
        return false;
    }
    if *lens.last().unwrap() as f64 > total as f64 * MAX_ONE_CELL {
        return false;
    }
    if distinct.keys().filter(|c| LEADER.is_match(c)).count() >= 2 {
        return false;
    }
    true
}

//------------------------------------------------------------------
// 표 후보들 → 표 (진입점의 순수 계산 부분)
//=> 후보마다 행렬을 만들고 거름 규칙을 통과한 것만 위(y 큰 쪽)부터 돌려준다.
// -in: comps = components 결과 / chars = 쪽 글자 위치
// -out: 표 목록(위 → 아래)
//------------------------------------------------------------------
pub fn tables_from(comps: &[(Vec<Seg>, Vec<Seg>)], chars: &[CharPos]) -> Vec<Table> {
    let mut out: Vec<Table> = comps.iter()
        .filter_map(|(h, v)| build(h, v, chars))
        .filter(|t| looks_like_table(&t.rows))
        .collect();
    // 읽기 순서 — 위부터(같으면 찾은 순서 유지: 안정 정렬)
    out.sort_by(|a, b| b.bbox.3.partial_cmp(&a.bbox.3).unwrap_or(std::cmp::Ordering::Equal));
    out
}
