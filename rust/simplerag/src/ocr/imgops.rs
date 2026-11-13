//------------------------------------------------------------------
// OCR 용 그림 다루기 (OpenCV 없이)
//=> RapidOCR 가 쓰는 cv2.resize(INTER_LINEAR) · warpPerspective(INTER_CUBIC, BORDER_REPLICATE) ·
//   np.rot90 을 같은 규칙으로 옮겼다. 그림은 BGR 3바이트 픽셀을 줄줄이 담는다(pdfium 출력과 같은 순서).
//------------------------------------------------------------------

//------------------------------------------------------------------
// BGR 그림
// -필드: w · h = 가로·세로 픽셀
// -필드: data  = BGR 순서 3바이트 × w × h (위 줄부터)
//------------------------------------------------------------------
#[derive(Clone)]
pub struct Bgr {
    pub w: usize,
    pub h: usize,
    pub data: Vec<u8>,
}

impl Bgr {
    // 흰 바탕 그림
    pub fn white(w: usize, h: usize) -> Bgr {
        Bgr { w, h, data: vec![255; w * h * 3] }
    }

    // (x, y) 의 c 번째 채널 값 — 경계 밖은 가장자리 값(BORDER_REPLICATE)
    #[inline]
    fn at(&self, x: isize, y: isize, c: usize) -> f32 {
        let x = x.clamp(0, self.w as isize - 1) as usize;
        let y = y.clamp(0, self.h as isize - 1) as usize;
        self.data[(y * self.w + x) * 3 + c] as f32
    }
}

//------------------------------------------------------------------
// 크기 바꾸기 (cv2.resize, INTER_LINEAR)
//=> 픽셀 가운데 맞춤 규칙: 원본 좌표 = (새 좌표 + 0.5) × 비율 − 0.5, 0 아래는 0 으로.
//   줄이기에도 같은 쌍선형을 쓴다(cv2 기본값과 같음 — INTER_AREA 가 아님).
//
// -in: src = 원본 / nw · nh = 새 크기(1 이상)
//
// -out: 새 그림
// -out: error = 없음
//------------------------------------------------------------------
pub fn resize_linear(src: &Bgr, nw: usize, nh: usize) -> Bgr {
    let (nw, nh) = (nw.max(1), nh.max(1));
    if nw == src.w && nh == src.h {
        return src.clone();
    }
    let sx = src.w as f32 / nw as f32;
    let sy = src.h as f32 / nh as f32;
    // 가로 방향 이웃 두 칸과 무게를 미리 계산해 둔다(줄마다 같다)
    let xs: Vec<(usize, usize, f32)> = (0..nw).map(|x| {
        let f = ((x as f32 + 0.5) * sx - 0.5).max(0.0);
        let x0 = (f.floor() as usize).min(src.w - 1);
        let x1 = (x0 + 1).min(src.w - 1);
        (x0, x1, f - x0 as f32)
    }).collect();
    let mut out = vec![0u8; nw * nh * 3];
    for y in 0..nh {
        let f = ((y as f32 + 0.5) * sy - 0.5).max(0.0);
        let y0 = (f.floor() as usize).min(src.h - 1);
        let y1 = (y0 + 1).min(src.h - 1);
        let wy = f - y0 as f32;
        let r0 = &src.data[y0 * src.w * 3..(y0 + 1) * src.w * 3];
        let r1 = &src.data[y1 * src.w * 3..(y1 + 1) * src.w * 3];
        let o = &mut out[y * nw * 3..(y + 1) * nw * 3];
        for (x, &(x0, x1, wx)) in xs.iter().enumerate() {
            for c in 0..3 {
                let a = r0[x0 * 3 + c] as f32 * (1.0 - wx) + r0[x1 * 3 + c] as f32 * wx;
                let b = r1[x0 * 3 + c] as f32 * (1.0 - wx) + r1[x1 * 3 + c] as f32 * wx;
                o[x * 3 + c] = (a * (1.0 - wy) + b * wy).round().clamp(0.0, 255.0) as u8;
            }
        }
    }
    Bgr { w: nw, h: nh, data: out }
}

// 3차 보간 무게 (cv2 INTER_CUBIC, A = −0.75)
#[inline]
fn cubic_w(t: f32) -> [f32; 4] {
    const A: f32 = -0.75;
    let w0 = ((A * (t + 1.0) - 5.0 * A) * (t + 1.0) + 8.0 * A) * (t + 1.0) - 4.0 * A;
    let w1 = ((A + 2.0) * t - (A + 3.0)) * t * t + 1.0;
    let w2 = ((A + 2.0) * (1.0 - t) - (A + 3.0)) * (1.0 - t) * (1.0 - t) + 1.0;
    [w0, w1, w2, 1.0 - w0 - w1 - w2]
}

//------------------------------------------------------------------
// 네 점 사각형을 바로 세운 직사각형으로 잘라내기 (RapidOCR get_rotate_crop_image)
//=> 1) 가로 = 위·아래 변 중 긴 것, 세로 = 왼·오른 변 중 긴 것 (소수점 버림)
//   2) 결과 픽셀마다 원본 좌표를 원근 변환으로 구해 3차 보간으로 읽는다(경계 밖은 가장자리 값)
//   3) 세로가 가로의 1.5배 이상이면 시계 반대 방향으로 90° 돌린다(세로쓰기 상자)
//
// -in: img = 원본 / pts = [왼위, 오른위, 오른아래, 왼아래] (x, y)
//
// -out: 잘라낸 그림 — 크기가 0 이면 None
// -out: error = 없음
//------------------------------------------------------------------
pub fn rotate_crop(img: &Bgr, pts: &[[f32; 2]; 4]) -> Option<Bgr> {
    let d = |a: [f32; 2], b: [f32; 2]| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt();
    let cw = d(pts[0], pts[1]).max(d(pts[2], pts[3])) as usize;
    let ch = d(pts[0], pts[3]).max(d(pts[1], pts[2])) as usize;
    if cw == 0 || ch == 0 {
        return None;
    }
    let dst = [[0.0, 0.0], [cw as f32, 0.0], [cw as f32, ch as f32], [0.0, ch as f32]];
    // 결과 좌표 → 원본 좌표 (cv2 는 역행렬로 거꾸로 찾아 읽는다)
    let m = super::geom::perspective(&dst, pts)?;
    let mut out = vec![0u8; cw * ch * 3];
    for v in 0..ch {
        for u in 0..cw {
            let (x, y) = (u as f64, v as f64);
            let den = m[6] * x + m[7] * y + m[8];
            if den.abs() < 1e-12 {
                continue;
            }
            let sx = ((m[0] * x + m[1] * y + m[2]) / den) as f32;
            let sy = ((m[3] * x + m[4] * y + m[5]) / den) as f32;
            let (fx, fy) = (sx.floor(), sy.floor());
            let (wx, wy) = (cubic_w(sx - fx), cubic_w(sy - fy));
            let (ix, iy) = (fx as isize, fy as isize);
            for c in 0..3 {
                let mut acc = 0f32;
                for (j, wyj) in wy.iter().enumerate() {
                    let mut row = 0f32;
                    for (i, wxi) in wx.iter().enumerate() {
                        row += img.at(ix - 1 + i as isize, iy - 1 + j as isize, c) * wxi;
                    }
                    acc += row * wyj;
                }
                out[(v * cw + u) * 3 + c] = acc.round().clamp(0.0, 255.0) as u8;
            }
        }
    }
    let crop = Bgr { w: cw, h: ch, data: out };
    if ch as f32 / cw as f32 >= 1.5 {
        return Some(rot90_ccw(&crop));
    }
    Some(crop)
}

//------------------------------------------------------------------
// 시계 반대 방향 90° 돌리기 (np.rot90)
//=> 새 그림의 (x, y) = 원본의 (w−1−y, x). 새 크기는 가로·세로가 바뀐다.
//------------------------------------------------------------------
pub fn rot90_ccw(src: &Bgr) -> Bgr {
    let (nw, nh) = (src.h, src.w);
    let mut out = vec![0u8; nw * nh * 3];
    for y in 0..nh {
        for x in 0..nw {
            let (ox, oy) = (src.w - 1 - y, x);
            let s = (oy * src.w + ox) * 3;
            let d = (y * nw + x) * 3;
            out[d..d + 3].copy_from_slice(&src.data[s..s + 3]);
        }
    }
    Bgr { w: nw, h: nh, data: out }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resize_keeps_flat_color() {
        let mut g = Bgr::white(10, 6);
        for p in g.data.chunks_mut(3) {
            p.copy_from_slice(&[10, 20, 30]);
        }
        let r = resize_linear(&g, 4, 3);
        assert!(r.data.chunks(3).all(|p| p == [10, 20, 30]));
    }

    #[test]
    fn rot90_moves_top_right_to_top_left() {
        // 2×1 그림: 왼쪽 검정, 오른쪽 흰색 → 돌리면 1×2 에서 위가 흰색
        let g = Bgr { w: 2, h: 1, data: vec![0, 0, 0, 255, 255, 255] };
        let r = rot90_ccw(&g);
        assert_eq!((r.w, r.h), (1, 2));
        assert_eq!(&r.data[0..3], &[255, 255, 255]);
    }

    #[test]
    fn crop_axis_aligned_matches_source() {
        // 가로 줄무늬 그림에서 축에 맞춘 상자를 잘라내면 같은 값이 나와야 한다
        let mut g = Bgr::white(20, 10);
        for y in 0..10 {
            for x in 0..20 {
                let v = (y * 20) as u8;
                g.data[(y * 20 + x) * 3..(y * 20 + x) * 3 + 3].copy_from_slice(&[v, v, v]);
            }
        }
        let c = rotate_crop(&g, &[[2.0, 2.0], [12.0, 2.0], [12.0, 6.0], [2.0, 6.0]]).unwrap();
        assert_eq!((c.w, c.h), (10, 4));
        assert_eq!(c.data[0], 40);
        assert_eq!(c.data[(3 * 10) * 3], 100);
    }
}
