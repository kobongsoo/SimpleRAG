//------------------------------------------------------------------
// 글자 영역 검출 (RapidOCR TextDetector · DBPostProcess)
//=> 1) 그림을 검출 모델 크기로 맞춘다 — 짧은 변이 736 보다 작으면 늘리고, 두 변을 32 배수로
//   2) 정규화 (x/255 − mean)/std, mean·std 는 모델 inference.yml 값(ImageNet)
//   3) 확률 지도를 0.3 으로 이진화 → 2×2 팽창 → 붙은 덩어리마다 최소 넓이 사각형
//   4) 상자 안 평균 확률이 0.5 미만이면 버리고, 넓이×1.6/둘레 만큼 넓혀 원래 그림 좌표로
//   5) 위→아래, 같은 줄(세로 차 10px 미만)은 왼→오른 순서로 정렬
//------------------------------------------------------------------

use ort::session::{Session, SessionInputValue};
use ort::value::Tensor;

use super::geom::{self, P};
use super::imgops::{self, Bgr};

const LIMIT_SIDE: usize = 736;
const MEAN: [f32; 3] = [0.485, 0.456, 0.406];
const STD: [f32; 3] = [0.229, 0.224, 0.225];
const THRESH: f32 = 0.3;
const BOX_THRESH: f32 = 0.5;
const UNCLIP: f32 = 1.6;
const MIN_SIZE: f32 = 3.0;
// 모델 inference.yml 의 max_candidates — 빽빽한 쪽에서 덩어리를 잘라 먹지 않게 넉넉히
const MAX_CANDIDATES: usize = 3000;
const SORT_Y_THRESHOLD: f32 = 10.0;

// 파이썬 round() — 반올림 중 .5 는 짝수로(은행가 반올림)
fn round_half_even(x: f64) -> f64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && r % 2.0 != 0.0 { r - x.signum() } else { r }
}

//------------------------------------------------------------------
// 글자 상자 찾기 (핵심)
//
// -in: sess = 검출 모델 세션 / img = BGR 그림
//
// -out: Ok(상자 목록 — 각 [왼위, 오른위, 오른아래, 왼아래], 원래 그림 좌표, 읽는 순서)
// -out: error = 추론 실패면 Err("OCRError: …")
//------------------------------------------------------------------
pub fn detect(sess: &mut Session, img: &Bgr) -> Result<Vec<[P; 4]>, String> {
    let (h, w) = (img.h, img.w);
    // 1) 크기 — limit_type min: 짧은 변이 736 미만일 때만 늘린다
    let ratio = if h.min(w) < LIMIT_SIDE { LIMIT_SIDE as f64 / h.min(w) as f64 } else { 1.0 };
    let rh = (round_half_even((h as f64 * ratio).trunc() / 32.0) * 32.0) as usize;
    let rw = (round_half_even((w as f64 * ratio).trunc() / 32.0) * 32.0) as usize;
    if rh == 0 || rw == 0 {
        return Ok(vec![]);
    }
    let rs = imgops::resize_linear(img, rw, rh);
    // 2) CHW 정규화 — 채널 순서는 BGR 그대로(RapidOCR 와 같음)
    let plane = rw * rh;
    let mut x = vec![0f32; 3 * plane];
    for (i, px) in rs.data.chunks_exact(3).enumerate() {
        for c in 0..3 {
            x[c * plane + i] = (px[c] as f32 / 255.0 - MEAN[c]) / STD[c];
        }
    }
    let input = Tensor::from_array(([1usize, 3, rh, rw], x)).map_err(|e| format!("OCRError: {}", e))?;
    let feeds: Vec<(String, SessionInputValue)> = vec![("x".into(), input.into())];
    let outputs = sess.run(feeds).map_err(|e| format!("OCRError: 검출 추론 실패: {}", e))?;
    let (oshape, pred) = outputs[0].try_extract_tensor::<f32>().map_err(|e| format!("OCRError: {}", e))?;
    let (ph, pw) = (oshape[2] as usize, oshape[3] as usize);
    let pred = &pred[..ph * pw];
    // 3) 이진화 + 2×2 팽창(cv2.dilate, 기준점 가운데 → 자신·위·왼·왼위 중 하나라도 켜지면 켬)
    let seg: Vec<bool> = pred.iter().map(|&v| v > THRESH).collect();
    let mut mask = vec![false; ph * pw];
    for yy in 0..ph {
        for xx in 0..pw {
            let g = |dy: usize, dx: usize| yy >= dy && xx >= dx && seg[(yy - dy) * pw + (xx - dx)];
            mask[yy * pw + xx] = g(0, 0) || g(1, 0) || g(0, 1) || g(1, 1);
        }
    }
    let mut boxes = vec![];
    for comp in components(&mask, pw, ph).into_iter().take(MAX_CANDIDATES) {
        let hull = geom::convex_hull(comp);
        let Some((rect, short)) = geom::min_area_rect(&hull) else { continue };
        if short < MIN_SIZE {
            continue;
        }
        let rect = geom::order_mini(&rect);
        if box_score(pred, pw, ph, &rect) < BOX_THRESH {
            continue;
        }
        // 4) 넓히기 — 거리 = 넓이 × 1.6 / 둘레
        let (area, peri) = geom::area_perimeter(&rect);
        if peri <= 0.0 {
            continue;
        }
        let big = geom::expand_rect(&rect, area * UNCLIP / peri);
        let side = |a: P, b: P| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt();
        if side(big[0], big[1]).min(side(big[0], big[3])) < MIN_SIZE + 2.0 {
            continue;
        }
        let big = geom::order_mini(&big);
        // 검출 지도 좌표 → 원래 그림 좌표(반올림 · 그림 안으로)
        let mut q = [[0f32; 2]; 4];
        for (k, p) in big.iter().enumerate() {
            q[k] = [
                round_half_even(p[0] as f64 / pw as f64 * w as f64).clamp(0.0, w as f64) as f32,
                round_half_even(p[1] as f64 / ph as f64 * h as f64).clamp(0.0, h as f64) as f32,
            ];
        }
        // filter_det_res — 시계 방향 정렬, 그림 안으로 자르기(정수), 너비·높이 3 이하 버림
        let mut q = geom::order_clockwise(&q);
        for p in q.iter_mut() {
            p[0] = p[0].clamp(0.0, (w - 1) as f32).trunc();
            p[1] = p[1].clamp(0.0, (h - 1) as f32).trunc();
        }
        if (side(q[0], q[1]) as i32) <= 3 || (side(q[0], q[3]) as i32) <= 3 {
            continue;
        }
        boxes.push(q);
    }
    Ok(sort_boxes(boxes))
}

//------------------------------------------------------------------
// 붙은 덩어리 찾기 (cv2.findContours 의 바깥 윤곽 대신)
//=> 8방향으로 이어진 켜진 픽셀 묶음마다, 각 줄의 맨 왼쪽·맨 오른쪽 픽셀만 모은다. 볼록 껍질은
//   이 점들만으로 전체 픽셀의 껍질과 같다. 덩어리는 처음 만난 픽셀 순서(위→아래)로 나온다.
//------------------------------------------------------------------
fn components(mask: &[bool], w: usize, h: usize) -> Vec<Vec<(i32, i32)>> {
    let mut seen = vec![false; w * h];
    let mut out = vec![];
    let mut stack: Vec<usize> = vec![];
    for start in 0..w * h {
        if !mask[start] || seen[start] {
            continue;
        }
        seen[start] = true;
        stack.push(start);
        // 줄 번호 → (최소 x, 최대 x)
        let mut rows: std::collections::BTreeMap<usize, (usize, usize)> = std::collections::BTreeMap::new();
        while let Some(i) = stack.pop() {
            let (x, y) = (i % w, i / w);
            let e = rows.entry(y).or_insert((x, x));
            e.0 = e.0.min(x);
            e.1 = e.1.max(x);
            for dy in -1i32..=1 {
                for dx in -1i32..=1 {
                    if dx == 0 && dy == 0 {
                        continue;
                    }
                    let (nx, ny) = (x as i32 + dx, y as i32 + dy);
                    if nx < 0 || ny < 0 || nx >= w as i32 || ny >= h as i32 {
                        continue;
                    }
                    let j = ny as usize * w + nx as usize;
                    if mask[j] && !seen[j] {
                        seen[j] = true;
                        stack.push(j);
                    }
                }
            }
        }
        let mut pts = Vec::with_capacity(rows.len() * 2);
        for (y, (x0, x1)) in rows {
            pts.push((x0 as i32, y as i32));
            if x1 != x0 {
                pts.push((x1 as i32, y as i32));
            }
        }
        out.push(pts);
    }
    out
}

//------------------------------------------------------------------
// 상자 안 평균 확률 (box_score_fast)
//=> 상자를 감싸는 범위에서, 상자(정수로 버린 꼭짓점) 안에 드는 픽셀의 확률 평균.
//------------------------------------------------------------------
fn box_score(pred: &[f32], w: usize, h: usize, rect: &[P; 4]) -> f32 {
    let xs = rect.iter().map(|p| p[0]);
    let ys = rect.iter().map(|p| p[1]);
    let xmin = (xs.clone().fold(f32::MAX, f32::min).floor() as i64).clamp(0, w as i64 - 1) as usize;
    let xmax = (xs.fold(f32::MIN, f32::max).ceil() as i64).clamp(0, w as i64 - 1) as usize;
    let ymin = (ys.clone().fold(f32::MAX, f32::min).floor() as i64).clamp(0, h as i64 - 1) as usize;
    let ymax = (ys.fold(f32::MIN, f32::max).ceil() as i64).clamp(0, h as i64 - 1) as usize;
    // fillPoly 는 꼭짓점을 정수로 바꿔 칠한다(astype(int32) = 0 쪽으로 버림)
    let poly: Vec<P> = rect.iter().map(|p| [p[0].trunc(), p[1].trunc()]).collect();
    let (mut sum, mut n) = (0f64, 0usize);
    for y in ymin..=ymax {
        for x in xmin..=xmax {
            if geom::inside_convex(&poly, x as f32, y as f32) {
                sum += pred[y * w + x] as f64;
                n += 1;
            }
        }
    }
    if n == 0 { 0.0 } else { (sum / n as f64) as f32 }
}

//------------------------------------------------------------------
// 읽는 순서로 정렬 (TextDetector.sorted_boxes)
//=> 왼위 꼭짓점 y 로 안정 정렬 → 이웃 y 차가 10 이상이면 새 줄 → 줄 안에서는 x 순서.
//------------------------------------------------------------------
fn sort_boxes(mut boxes: Vec<[P; 4]>) -> Vec<[P; 4]> {
    boxes.sort_by(|a, b| a[0][1].partial_cmp(&b[0][1]).unwrap_or(std::cmp::Ordering::Equal));
    let mut line = 0usize;
    let mut keyed: Vec<(usize, f32, usize, [P; 4])> = vec![];
    for (i, b) in boxes.iter().enumerate() {
        if i > 0 && b[0][1] - boxes[i - 1][0][1] >= SORT_Y_THRESHOLD {
            line += 1;
        }
        keyed.push((line, b[0][0], i, *b));
    }
    keyed.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal)).then(a.2.cmp(&b.2)));
    keyed.into_iter().map(|k| k.3).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn half_even_like_python() {
        assert_eq!(round_half_even(2.5), 2.0);
        assert_eq!(round_half_even(3.5), 4.0);
        assert_eq!(round_half_even(2.4), 2.0);
    }

    #[test]
    fn two_blobs_found() {
        let (w, h) = (20, 10);
        let mut m = vec![false; w * h];
        for y in 2..5 {
            for x in 1..6 {
                m[y * w + x] = true;
            }
            for x in 10..18 {
                m[y * w + x] = true;
            }
        }
        let c = components(&m, w, h);
        assert_eq!(c.len(), 2);
    }
}
