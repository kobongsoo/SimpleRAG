//------------------------------------------------------------------
// OCR 검출 후처리용 도형 계산 (OpenCV 없이)
//=> cv2.minAreaRect · boxPoints · getPerspectiveTransform 와 RapidOCR 의 점 정렬 규칙을 옮겼다.
//------------------------------------------------------------------

pub type P = [f32; 2];

//------------------------------------------------------------------
// 볼록 껍질 (Andrew 단조 사슬)
//=> 정수 점들의 볼록 껍질을 시계 반대 방향으로 돌려준다. 한 줄 위의 점은 양 끝만 남는다.
//
// -in: pts = 점 목록 (x, y)
//
// -out: 껍질 꼭짓점 — 점이 0~2개면 그대로(중복 제거)
// -out: error = 없음
//------------------------------------------------------------------
pub fn convex_hull(mut pts: Vec<(i32, i32)>) -> Vec<(i32, i32)> {
    pts.sort_unstable();
    pts.dedup();
    if pts.len() <= 2 {
        return pts;
    }
    let cross = |o: (i32, i32), a: (i32, i32), b: (i32, i32)| -> i64 {
        (a.0 - o.0) as i64 * (b.1 - o.1) as i64 - (a.1 - o.1) as i64 * (b.0 - o.0) as i64
    };
    let mut lower: Vec<(i32, i32)> = vec![];
    for &p in &pts {
        while lower.len() >= 2 && cross(lower[lower.len() - 2], lower[lower.len() - 1], p) <= 0 {
            lower.pop();
        }
        lower.push(p);
    }
    let mut upper: Vec<(i32, i32)> = vec![];
    for &p in pts.iter().rev() {
        while upper.len() >= 2 && cross(upper[upper.len() - 2], upper[upper.len() - 1], p) <= 0 {
            upper.pop();
        }
        upper.push(p);
    }
    lower.pop();
    upper.pop();
    lower.extend(upper);
    lower
}

//------------------------------------------------------------------
// 넓이가 가장 작은 회전 사각형 (cv2.minAreaRect + boxPoints)
//=> 볼록 껍질의 변마다 그 변 방향으로 모든 점을 투영해 감싸는 사각형을 만들고, 넓이가 가장
//   작은 것을 고른다(회전 캘리퍼스). 점이 하나·둘이면 폭이 0 인 사각형이 된다.
//
// -in: hull = 볼록 껍질 꼭짓점
//
// -out: (네 꼭짓점, 짧은 변 길이)
// -out: error = 점이 없으면 None
//------------------------------------------------------------------
pub fn min_area_rect(hull: &[(i32, i32)]) -> Option<([P; 4], f32)> {
    if hull.is_empty() {
        return None;
    }
    let pf: Vec<[f64; 2]> = hull.iter().map(|&(x, y)| [x as f64, y as f64]).collect();
    if pf.len() == 1 {
        let p = [pf[0][0] as f32, pf[0][1] as f32];
        return Some(([p; 4], 0.0));
    }
    let mut best: Option<(f64, [P; 4], f32)> = None;
    let n = pf.len();
    for i in 0..n {
        let a = pf[i];
        let b = pf[(i + 1) % n];
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let len = (dx * dx + dy * dy).sqrt();
        if len < 1e-9 {
            continue;
        }
        let e = [dx / len, dy / len];
        let nrm = [-e[1], e[0]];
        let (mut s0, mut s1, mut t0, mut t1) = (f64::MAX, f64::MIN, f64::MAX, f64::MIN);
        for p in &pf {
            let s = p[0] * e[0] + p[1] * e[1];
            let t = p[0] * nrm[0] + p[1] * nrm[1];
            s0 = s0.min(s);
            s1 = s1.max(s);
            t0 = t0.min(t);
            t1 = t1.max(t);
        }
        let area = (s1 - s0) * (t1 - t0);
        if best.as_ref().map_or(true, |b| area < b.0 - 1e-9) {
            let corner = |s: f64, t: f64| -> P { [(e[0] * s + nrm[0] * t) as f32, (e[1] * s + nrm[1] * t) as f32] };
            let pts = [corner(s0, t0), corner(s1, t0), corner(s1, t1), corner(s0, t1)];
            let short = (s1 - s0).min(t1 - t0) as f32;
            best = Some((area, pts, short));
        }
    }
    best.map(|(_, p, s)| (p, s))
}

//------------------------------------------------------------------
// 네 점 정렬 (RapidOCR DBPostProcess.get_mini_boxes 의 순서 규칙)
//=> x 로 정렬한 뒤 왼쪽 두 점 중 위(y 작은)를 0번, 아래를 3번, 오른쪽 두 점 중 위를 1번,
//   아래를 2번으로 둔다 → [왼위, 오른위, 오른아래, 왼아래].
//------------------------------------------------------------------
pub fn order_mini(pts: &[P; 4]) -> [P; 4] {
    let mut p = pts.to_vec();
    // sorted(key=x) 는 안정 정렬 — 같은 x 면 원래 순서 유지
    p.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal));
    let (i1, i4) = if p[1][1] > p[0][1] { (0, 1) } else { (1, 0) };
    let (i2, i3) = if p[3][1] > p[2][1] { (2, 3) } else { (3, 2) };
    [p[i1], p[i2], p[i3], p[i4]]
}

//------------------------------------------------------------------
// 네 점을 시계 방향으로 (RapidOCR order_points_clockwise)
//=> x 로 정렬해 왼쪽 두 점·오른쪽 두 점으로 나누고, 각각 y 로 정렬해 [왼위, 오른위, 오른아래, 왼아래].
//------------------------------------------------------------------
pub fn order_clockwise(pts: &[P; 4]) -> [P; 4] {
    let mut p = pts.to_vec();
    // np.argsort 기본(quicksort)은 안정이 아니지만 같은 x 에서도 아래 y 정렬이 순서를 정한다
    p.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal));
    let mut left = [p[0], p[1]];
    let mut right = [p[2], p[3]];
    left.sort_by(|a, b| a[1].partial_cmp(&b[1]).unwrap_or(std::cmp::Ordering::Equal));
    right.sort_by(|a, b| a[1].partial_cmp(&b[1]).unwrap_or(std::cmp::Ordering::Equal));
    [left[0], right[0], right[1], left[1]]
}

//------------------------------------------------------------------
// 다각형 넓이·둘레 (shapely Polygon.area · length)
//------------------------------------------------------------------
pub fn area_perimeter(pts: &[P]) -> (f32, f32) {
    let n = pts.len();
    let (mut a, mut l) = (0f64, 0f64);
    for i in 0..n {
        let p = pts[i];
        let q = pts[(i + 1) % n];
        a += p[0] as f64 * q[1] as f64 - q[0] as f64 * p[1] as f64;
        l += (((q[0] - p[0]) as f64).powi(2) + ((q[1] - p[1]) as f64).powi(2)).sqrt();
    }
    ((a / 2.0).abs() as f32, l as f32)
}

//------------------------------------------------------------------
// 사각형을 바깥으로 넓히기 (DB unclip)
//=> RapidOCR 는 pyclipper 로 거리 d 만큼 둥글게 넓힌 뒤 다시 최소 넓이 사각형을 구한다.
//   둥근 모서리는 사각형 안에 들어가므로, 결과는 각 변을 d 만큼 밀어낸 사각형과 같다.
//
// -in: rect = order_mini 순서의 네 점 / d = 넓힐 거리
//
// -out: 넓힌 네 점 (순서 같음)
// -out: error = 없음
//------------------------------------------------------------------
pub fn expand_rect(rect: &[P; 4], d: f32) -> [P; 4] {
    let c = [(rect[0][0] + rect[2][0]) / 2.0, (rect[0][1] + rect[2][1]) / 2.0];
    let unit = |a: P, b: P| -> P {
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let l = (dx * dx + dy * dy).sqrt();
        if l < 1e-6 { [0.0, 0.0] } else { [dx / l, dy / l] }
    };
    // 0→1 방향(가로), 0→3 방향(세로)
    let u = unit(rect[0], rect[1]);
    let v = unit(rect[0], rect[3]);
    let mut out = [[0f32; 2]; 4];
    for (i, p) in rect.iter().enumerate() {
        // 꼭짓점이 가운데에서 어느 쪽에 있는지(가로·세로 부호)대로 d 만큼 더 민다
        let rel = [p[0] - c[0], p[1] - c[1]];
        let su = (rel[0] * u[0] + rel[1] * u[1]).signum();
        let sv = (rel[0] * v[0] + rel[1] * v[1]).signum();
        out[i] = [p[0] + (u[0] * su + v[0] * sv) * d, p[1] + (u[1] * su + v[1] * sv) * d];
    }
    out
}

//------------------------------------------------------------------
// 점이 볼록 다각형 안(경계 포함)에 있나 — cv2.fillPoly 마스크 근사
//------------------------------------------------------------------
pub fn inside_convex(poly: &[P], x: f32, y: f32) -> bool {
    let n = poly.len();
    let (mut pos, mut neg) = (false, false);
    for i in 0..n {
        let a = poly[i];
        let b = poly[(i + 1) % n];
        let c = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]);
        if c > 1e-4 {
            pos = true;
        } else if c < -1e-4 {
            neg = true;
        }
        if pos && neg {
            return false;
        }
    }
    true
}

//------------------------------------------------------------------
// 원근 변환 행렬 (cv2.getPerspectiveTransform)
//=> src 네 점을 dst 네 점으로 보내는 3×3 행렬(마지막 칸 1)을 8원 연립방정식으로 푼다.
//
// -in: src · dst = 네 점씩
//
// -out: 행렬 9칸(행 우선) / 풀 수 없으면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn perspective(src: &[P; 4], dst: &[P; 4]) -> Option<[f64; 9]> {
    let mut a = [[0f64; 9]; 8];
    for i in 0..4 {
        let (x, y) = (src[i][0] as f64, src[i][1] as f64);
        let (u, v) = (dst[i][0] as f64, dst[i][1] as f64);
        a[i] = [x, y, 1.0, 0.0, 0.0, 0.0, -x * u, -y * u, u];
        a[i + 4] = [0.0, 0.0, 0.0, x, y, 1.0, -x * v, -y * v, v];
    }
    // 부분 피벗 가우스 소거
    for col in 0..8 {
        let piv = (col..8).max_by(|&r1, &r2| a[r1][col].abs().partial_cmp(&a[r2][col].abs()).unwrap())?;
        if a[piv][col].abs() < 1e-12 {
            return None;
        }
        a.swap(col, piv);
        for r in 0..8 {
            if r != col {
                let f = a[r][col] / a[col][col];
                for k in col..9 {
                    a[r][k] -= f * a[col][k];
                }
            }
        }
    }
    let mut m = [0f64; 9];
    for i in 0..8 {
        m[i] = a[i][8] / a[i][i];
    }
    m[8] = 1.0;
    Some(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn min_rect_of_axis_box() {
        let hull = convex_hull(vec![(0, 0), (10, 0), (10, 4), (0, 4), (5, 2)]);
        let (pts, short) = min_area_rect(&hull).unwrap();
        assert!((short - 4.0).abs() < 1e-4);
        let o = order_mini(&pts);
        assert_eq!(o[0], [0.0, 0.0]);
        assert_eq!(o[2], [10.0, 4.0]);
    }

    #[test]
    fn expand_adds_distance_each_side() {
        let r = [[0.0, 0.0], [10.0, 0.0], [10.0, 4.0], [0.0, 4.0]];
        let e = expand_rect(&r, 1.0);
        assert_eq!(e[0], [-1.0, -1.0]);
        assert_eq!(e[2], [11.0, 5.0]);
    }

    #[test]
    fn perspective_identity_shift() {
        let s = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
        let d = [[2.0, 3.0], [3.0, 3.0], [3.0, 4.0], [2.0, 4.0]];
        let m = perspective(&s, &d).unwrap();
        assert!((m[2] - 2.0).abs() < 1e-9 && (m[5] - 3.0).abs() < 1e-9 && (m[0] - 1.0).abs() < 1e-9);
    }
}
