//------------------------------------------------------------------
// 반 정밀도(f16) 벡터 — 변환 · 내적 (메모리 절감 설계서 B안)
//=> 저장소가 메모리에 드는 벡터를 f32(4바이트) 대신 f16(2바이트)으로 들어 약 207MB 를 줄인다.
//   디스크(points.sqlite)는 그대로 f32 이고, 질의 벡터도 f32 다. 내적은 f16 칸을 f32 로 풀면서 f32 로 더한다.
//    · 변환: 가장 가까운 짝수 반올림(round-to-nearest-even) — 정규화된 벡터라 칸 오차 최대 약 1.2e-4
//    · 내적: CPU 에 F16C · AVX · FMA 가 있으면 8칸씩 한 명령으로 풀어 곱해 더한다(시작 때 한 번 확인)
//            없으면 한 칸씩 풀어서 — F16C 는 2012년 이후 Intel · AMD 에 다 있어 사실상 안전장치다
//   실측(2026-11-13, 28만 × 384, 골든 204문항): dense 상위 10 묶음 같음 199/204, 상위 50 점수 차 최대 5e-5.
//   half 크레이트를 쓰지 않는 것은 의존성을 늘리지 않으려고다(examples/f16_check.rs 에서 같은 코드를 검증).
//------------------------------------------------------------------

//------------------------------------------------------------------
// f32 → f16 비트
//=> 지수를 f16 범위로 옮기고 가수 23비트를 10비트로 줄이며 가장 가까운 짝수로 반올림한다.
//    1) 너무 크면 무한대, 너무 작으면 0 또는 아주 작은 수(subnormal)
//    2) 반올림으로 가수가 넘치면 지수를 하나 올린다
//
// -in: x = f32 값
//
// -out: f16 비트(u16)
// -out: error = 없음
//------------------------------------------------------------------
pub fn to_f16(x: f32) -> u16 {
    let b = x.to_bits();
    let sign = ((b >> 16) & 0x8000) as u16;
    let exp = ((b >> 23) & 0xff) as i32 - 127 + 15;
    let man = b & 0x7f_ffff;
    // NaN 은 NaN 으로(가수 윗비트를 남긴다)
    if (b >> 23) & 0xff == 0xff {
        return sign | 0x7c00 | if man != 0 { 0x200 } else { 0 };
    }
    if exp >= 31 {
        return sign | 0x7c00;
    }
    if exp <= 0 {
        // 아주 작은 값 — 숨은 1 을 붙여 밀어내며 반올림
        if exp < -10 {
            return sign;
        }
        let m = man | 0x80_0000;
        let shift = (14 - exp) as u32;
        let half = 1u32 << (shift - 1);
        let r = m + half - 1 + ((m >> shift) & 1);
        return sign | (r >> shift) as u16;
    }
    let r = man + 0xfff + ((man >> 13) & 1);
    if r & 0x80_0000 != 0 {
        // 반올림으로 가수가 넘치면 지수를 하나 올린다
        return sign | (((exp + 1) as u16) << 10);
    }
    sign | ((exp as u16) << 10) | (r >> 13) as u16
}

//------------------------------------------------------------------
// f16 비트 → f32
// -in: h = f16 비트 / -out: f32 값 / error = 없음
//------------------------------------------------------------------
pub fn from_f16(h: u16) -> f32 {
    let sign = ((h & 0x8000) as u32) << 16;
    let exp = ((h >> 10) & 0x1f) as u32;
    let man = (h & 0x3ff) as u32;
    let bits = if exp == 0 {
        if man == 0 {
            sign
        } else {
            // subnormal → 정규화
            let mut e: i32 = 127 - 15 + 1;
            let mut m = man;
            while m & 0x400 == 0 {
                m <<= 1;
                e -= 1;
            }
            sign | ((e as u32) << 23) | ((m & 0x3ff) << 13)
        }
    } else if exp == 31 {
        sign | 0x7f80_0000 | (man << 13)
    } else {
        sign | ((exp + 127 - 15) << 23) | (man << 13)
    };
    f32::from_bits(bits)
}

//------------------------------------------------------------------
// f32 묶음 → f16 묶음
//=> 저장소 적재 때 28만 × 384 칸을 바꾸므로 한 칸씩이면 약 3초가 더 든다.
//   F16C 가 있으면 8칸씩 한 명령(_mm256_cvtps_ph, 가장 가까운 짝수 반올림)으로 바꾼다 — to_f16 과 결과가 같다.
//
// -in: v = f32 값들
//
// -out: f16 비트들
// -out: error = 없음
//------------------------------------------------------------------
pub fn encode(v: &[f32]) -> Vec<u16> {
    #[cfg(target_arch = "x86_64")]
    if *USE_F16C {
        // SAFETY: USE_F16C 가 참이면 이 CPU 에 f16c · avx 가 있다
        return unsafe { encode_f16c(v) };
    }
    v.iter().map(|x| to_f16(*x)).collect()
}

// F16C 판 — 8칸씩 바꾸고 꼬리는 한 칸씩
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "f16c,avx")]
unsafe fn encode_f16c(v: &[f32]) -> Vec<u16> {
    use std::arch::x86_64::*;
    let mut out = vec![0u16; v.len()];
    let mut i = 0;
    while i + 8 <= v.len() {
        let h = _mm256_cvtps_ph::<_MM_FROUND_TO_NEAREST_INT>(_mm256_loadu_ps(v.as_ptr().add(i)));
        _mm_storeu_si128(out.as_mut_ptr().add(i) as *mut __m128i, h);
        i += 8;
    }
    while i < v.len() {
        out[i] = to_f16(v[i]);
        i += 1;
    }
    out
}

// f16 묶음 → f32 묶음
pub fn decode(v: &[u16]) -> Vec<f32> {
    v.iter().map(|h| from_f16(*h)).collect()
}

// F16C · AVX · FMA 를 쓸까 — 시작 때 한 번 확인(SIMPLERAG_NO_AVX=1 이면 끈다 — 비교 측정용)
#[cfg(target_arch = "x86_64")]
static USE_F16C: once_cell::sync::Lazy<bool> = once_cell::sync::Lazy::new(|| {
    std::env::var("SIMPLERAG_NO_AVX").map(|v| v.trim() != "1").unwrap_or(true)
        && is_x86_feature_detected!("f16c")
        && is_x86_feature_detected!("avx")
        && is_x86_feature_detected!("fma")
});

//------------------------------------------------------------------
// f16 벡터 · f32 질의 내적
// -in: a = f16 벡터 / b = f32 질의 (길이가 다르면 짧은 쪽까지만)
// -out: 내적 / error = 없음
//------------------------------------------------------------------
pub fn dot(a: &[u16], b: &[f32]) -> f32 {
    #[cfg(target_arch = "x86_64")]
    if *USE_F16C {
        // SAFETY: USE_F16C 가 참이면 이 CPU 에 f16c · avx · fma 가 있다
        return unsafe { dot_f16c(a, b) };
    }
    dot_plain(a, b)
}

// F16C 판 — 8칸씩 f32 로 풀어(_mm256_cvtph_ps) FMA 로 곱해 더한다
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "f16c,avx,fma")]
unsafe fn dot_f16c(a: &[u16], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let n = a.len().min(b.len());
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= n {
        let h = _mm_loadu_si128(a.as_ptr().add(i) as *const __m128i);
        acc = _mm256_fmadd_ps(_mm256_cvtph_ps(h), _mm256_loadu_ps(b.as_ptr().add(i)), acc);
        i += 8;
    }
    let mut t = [0f32; 8];
    _mm256_storeu_ps(t.as_mut_ptr(), acc);
    let mut s: f32 = t.iter().sum();
    // 8 로 나눠떨어지지 않는 꼬리
    while i < n {
        s += from_f16(a[i]) * b[i];
        i += 1;
    }
    s
}

// 명령 없는 판 — 한 칸씩 풀어서(8칸 묶음)
fn dot_plain(a: &[u16], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let mut acc = [0f32; 8];
    let mut i = 0;
    while i + 8 <= n {
        for k in 0..8 {
            acc[k] += from_f16(a[i + k]) * b[i + k];
        }
        i += 8;
    }
    let mut s: f32 = acc.iter().sum();
    while i < n {
        s += from_f16(a[i]) * b[i];
        i += 1;
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    // 정규화 범위 값은 왕복 오차가 작고, 특수값(0 · 무한대 · 아주 작은 수)은 그대로 돌아온다
    #[test]
    fn round_trip() {
        for x in [0.0f32, -0.0, 1.0, -1.0, 0.5, 0.123456, -0.0731, 1e-5, 6.1e-5, 65504.0] {
            let y = from_f16(to_f16(x));
            assert!((x - y).abs() <= x.abs() * 1e-3 + 1e-7, "{} → {}", x, y);
        }
        assert_eq!(from_f16(to_f16(1e9)), f32::INFINITY);
        assert_eq!(to_f16(1e-12), 0);
        assert!(from_f16(to_f16(f32::NAN)).is_nan());
    }

    // 짝수 반올림 — 1 + 2^-11 은 딱 중간이라 짝수(1.0)로, 1 + 3·2^-11 은 위로
    #[test]
    fn ties_to_even() {
        assert_eq!(to_f16(1.0 + 2f32.powi(-11)), to_f16(1.0));
        assert_eq!(from_f16(to_f16(1.0 + 3.0 * 2f32.powi(-11))), 1.0 + 2.0 * 2f32.powi(-10));
    }

    // 변환 — F16C 판(encode)과 한 칸씩 판(to_f16)이 비트까지 같다(정규화 범위 · 아주 작은 수 · 반올림 경계 포함)
    #[test]
    fn encode_matches_scalar() {
        let mut xs: Vec<f32> = (0..20000).map(|i| ((i as f32) * 0.618_034).sin() * 0.3).collect();
        xs.extend([0.0, -0.0, 1e-7, -3e-6, 6.1e-5, 1.0 + 2f32.powi(-11), 1.0 + 3.0 * 2f32.powi(-11), 65504.0, 70000.0]);
        let fast = encode(&xs);
        let slow: Vec<u16> = xs.iter().map(|x| to_f16(*x)).collect();
        assert_eq!(fast, slow);
    }

    // 내적 — 명령 판과 한 칸씩 판이 거의 같고, f32 내적과도 오차 안
    #[test]
    fn dot_matches() {
        let a: Vec<f32> = (0..384).map(|i| ((i * 37 % 101) as f32 - 50.0) / 400.0).collect();
        let b: Vec<f32> = (0..384).map(|i| ((i * 53 % 97) as f32 - 48.0) / 300.0).collect();
        let h = encode(&a);
        let exact: f32 = a.iter().zip(&b).map(|(x, y)| x * y).sum();
        assert!((dot(&h, &b) - exact).abs() < 1e-3);
        assert!((dot(&h, &b) - dot_plain(&h, &b)).abs() < 1e-5);
        // 길이가 8 의 배수가 아니어도(꼬리)
        assert!((dot(&h[..13], &b[..13]) - dot_plain(&h[..13], &b[..13])).abs() < 1e-6);
    }
}
