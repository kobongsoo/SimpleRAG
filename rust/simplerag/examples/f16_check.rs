//------------------------------------------------------------------
// 벡터 반 정밀도(f16) 영향 재기 (진단용 예제)
//=> 저장소 벡터를 f32 대신 f16 으로 들면 메모리가 절반이 된다. 대신 점수가 조금 바뀌므로
//   실제 질문으로 dense 검색 순위가 얼마나 같은지, 훑기 속도는 어떤지 잰다.
//    1) points.sqlite 벡터를 읽어 정규화(f32) → 같은 값을 f16 으로 한 벌 더
//    2) 질문마다 임베딩 → f32 · f16 각각 전체 내적 → 상위 K 비교
//    3) 전체 훑기 시간(질문당)을 각각 잰다
//
// 실행: cargo run --release --example f16_check -- <points.sqlite> <질문 파일(한 줄 한 문항)>
//------------------------------------------------------------------

use std::time::Instant;

use rusqlite::{Connection, OpenFlags};
use simplerag::embed::Embedder;

// 벡터 정규화
fn normalize(v: &mut [f32]) {
    let n = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if n > 1e-12 {
        v.iter_mut().for_each(|x| *x /= n);
    }
}

// f32 → f16 비트(가장 가까운 짝수 반올림) — 정규화된 벡터라 넘침·아주 작은 값만 신경 쓴다
fn to_f16(x: f32) -> u16 {
    let b = x.to_bits();
    let sign = ((b >> 16) & 0x8000) as u16;
    let exp = ((b >> 23) & 0xff) as i32 - 127 + 15;
    let man = b & 0x7f_ffff;
    if exp >= 31 {
        return sign | 0x7c00;
    }
    if exp <= 0 {
        // 아주 작은 값(subnormal) — 가수에 숨은 1 을 붙여 밀어낸다
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

// f16 비트 → f32 (한 칸, 느린 길)
fn from_f16(h: u16) -> f32 {
    let sign = ((h & 0x8000) as u32) << 16;
    let exp = ((h >> 10) & 0x1f) as u32;
    let man = (h & 0x3ff) as u32;
    let bits = if exp == 0 {
        if man == 0 {
            sign
        } else {
            // subnormal → 정규화
            let mut e = 127 - 15 + 1;
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

// f32 내적 — 저장소와 같은 8칸 묶음
fn dot32(a: &[f32], b: &[f32]) -> f32 {
    let mut acc = [0f32; 8];
    let mut i = 0;
    while i + 8 <= a.len() {
        for k in 0..8 {
            acc[k] += a[i + k] * b[i + k];
        }
        i += 8;
    }
    acc.iter().sum()
}

// f16 내적 — F16C 명령(8칸씩 f32 로 풀기)이 있으면 그것으로
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "f16c,avx,fma")]
unsafe fn dot16_f16c(a: &[u16], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= a.len() {
        let h = _mm_loadu_si128(a.as_ptr().add(i) as *const __m128i);
        let x = _mm256_cvtph_ps(h);
        let y = _mm256_loadu_ps(b.as_ptr().add(i));
        acc = _mm256_fmadd_ps(x, y, acc);
        i += 8;
    }
    let mut t = [0f32; 8];
    _mm256_storeu_ps(t.as_mut_ptr(), acc);
    t.iter().sum()
}

// f32 내적 — f16 과 같은 AVX·FMA 명령으로(속도 차이에서 '명령' 몫과 '절반 크기' 몫을 나눠 보려고)
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx,fma")]
unsafe fn dot32_avx(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let mut acc = _mm256_setzero_ps();
    let mut i = 0;
    while i + 8 <= a.len() {
        acc = _mm256_fmadd_ps(_mm256_loadu_ps(a.as_ptr().add(i)), _mm256_loadu_ps(b.as_ptr().add(i)), acc);
        i += 8;
    }
    let mut t = [0f32; 8];
    _mm256_storeu_ps(t.as_mut_ptr(), acc);
    t.iter().sum()
}

// f16 내적 — 명령이 없으면 한 칸씩 풀어서
fn dot16_slow(a: &[u16], b: &[f32]) -> f32 {
    let mut acc = [0f32; 8];
    let mut i = 0;
    while i + 8 <= a.len() {
        for k in 0..8 {
            acc[k] += from_f16(a[i + k]) * b[i + k];
        }
        i += 8;
    }
    acc.iter().sum()
}

// 점수 상위 k 의 (점 순번) — 점수 같으면 순번 작은 쪽
fn topk(scores: &[f32], k: usize) -> Vec<usize> {
    let mut idx: Vec<usize> = (0..scores.len()).collect();
    idx.select_nth_unstable_by(k, |a, b| scores[*b].partial_cmp(&scores[*a]).unwrap().then(a.cmp(b)));
    idx.truncate(k);
    idx.sort_by(|a, b| scores[*b].partial_cmp(&scores[*a]).unwrap().then(a.cmp(b)));
    idx
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    let conn = Connection::open_with_flags(&a[1], OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let qs: Vec<String> = std::fs::read_to_string(&a[2]).unwrap().lines().filter(|l| !l.trim().is_empty()).map(|s| s.to_string()).collect();

    // 1) 벡터 읽기 — 벡터 대기(빈 벡터) 점은 뺀다
    let mut v32: Vec<Vec<f32>> = vec![];
    let mut st = conn.prepare("SELECT vec FROM points ORDER BY id").unwrap();
    let mut rows = st.query([]).unwrap();
    while let Some(r) = rows.next().unwrap() {
        let b: Vec<u8> = r.get(0).unwrap();
        if b.is_empty() {
            continue;
        }
        let mut v: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        normalize(&mut v);
        v32.push(v);
    }
    let v16: Vec<Vec<u16>> = v32.iter().map(|v| v.iter().map(|x| to_f16(*x)).collect()).collect();
    // 변환 오차 — 칸 하나 기준 최대
    let max_err = v32.iter().zip(&v16).flat_map(|(a, b)| a.iter().zip(b).map(|(x, h)| (x - from_f16(*h)).abs())).fold(0f32, f32::max);
    let fast = is_x86_feature_detected!("f16c") && is_x86_feature_detected!("avx") && is_x86_feature_detected!("fma");
    println!("벡터 {} 개 · {} 차원 · 칸 오차 최대 {:.2e} · F16C {}", v32.len(), v32[0].len(), max_err, fast);

    // 2) 질문 임베딩
    let emb = Embedder::new();
    emb.ensure_loaded().unwrap();
    let qv: Vec<Vec<f32>> = qs.iter().map(|q| emb.embed_query(q).unwrap()).map(|mut v| {
        normalize(&mut v);
        v
    }).collect();

    // 3) 질문마다 비교
    let (mut t32, mut t16, mut t32a) = (0f64, 0f64, 0f64);
    let mut same_order = [0usize; 3];
    let mut same_set = [0usize; 3];
    let mut overlap_sum = [0usize; 3];
    let mut max_dscore = 0f32;
    let ks = [3usize, 10, 50];
    for q in &qv {
        let t = Instant::now();
        let s32: Vec<f32> = v32.iter().map(|v| dot32(v, q)).collect();
        t32 += t.elapsed().as_secs_f64();
        if fast {
            let t = Instant::now();
            let s: Vec<f32> = v32.iter().map(|v| unsafe { dot32_avx(v, q) }).collect();
            t32a += t.elapsed().as_secs_f64();
            std::hint::black_box(s);
        }
        let t = Instant::now();
        let s16: Vec<f32> = if fast {
            v16.iter().map(|v| unsafe { dot16_f16c(v, q) }).collect()
        } else {
            v16.iter().map(|v| dot16_slow(v, q)).collect()
        };
        t16 += t.elapsed().as_secs_f64();
        for (j, &k) in ks.iter().enumerate() {
            let (x, y) = (topk(&s32, k), topk(&s16, k));
            if x == y {
                same_order[j] += 1;
            }
            let xs: std::collections::HashSet<_> = x.iter().collect();
            let ov = y.iter().filter(|i| xs.contains(i)).count();
            overlap_sum[j] += ov;
            if ov == k {
                same_set[j] += 1;
            }
        }
        // 상위 50 안에서 점수 차 최대
        for i in topk(&s32, 50) {
            max_dscore = max_dscore.max((s32[i] - s16[i]).abs());
        }
    }
    let n = qv.len();
    println!("질문 {} 개", n);
    for (j, &k) in ks.iter().enumerate() {
        println!("상위 {:>2}: 순서까지 같음 {}/{} · 묶음 같음 {}/{} · 평균 겹침 {:.2}/{}", k, same_order[j], n, same_set[j], n, overlap_sum[j] as f64 / n as f64, k);
    }
    println!("상위 50 점수 차 최대 {:.2e}", max_dscore);
    println!("전체 훑기(질문당): f32 지금 {:.1} ms · f32 AVX {:.1} ms · f16 AVX {:.1} ms",
        t32 / n as f64 * 1000.0, t32a / n as f64 * 1000.0, t16 / n as f64 * 1000.0);
}
