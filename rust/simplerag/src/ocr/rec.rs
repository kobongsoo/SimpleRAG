//------------------------------------------------------------------
// 글자 인식 (RapidOCR TextRecognizer · CTCLabelDecode)
//=> 1) 잘라낸 줄 그림을 가로/세로 비율 순으로 정렬해 6개씩 묶는다
//   2) 묶음 안 가장 넓은 비율(최소 320/48)로 폭을 정하고, 높이 48 로 줄인 뒤 오른쪽을 0 으로 채운다
//   3) 정규화 (x/255 − 0.5)/0.5 — 채널 순서는 BGR 그대로
//   4) 칸마다 확률이 가장 큰 글자를 고르고, 앞 칸과 같으면 합치고, 0번(공백 기호)은 뺀다
//   5) 글자 = ['blank'] + 사전 + [' '] 의 그 번호, 점수 = 고른 글자 확률 평균
//------------------------------------------------------------------

use ort::session::{Session, SessionInputValue};
use ort::value::Tensor;

use super::imgops::{self, Bgr};

const IMG_H: usize = 48;
const IMG_W: usize = 320;
const BATCH: usize = 6;

//------------------------------------------------------------------
// 글자 사전 읽기
//=> 한 줄에 한 글자. 앞에 'blank'(0번), 끝에 띄어쓰기를 붙인다(PaddleOCR use_space_char).
//
// -in: path = 사전 txt 경로
//
// -out: 글자 목록
// -out: error = 파일을 못 읽으면 Err("OCRError: …")
//------------------------------------------------------------------
pub fn load_dict(path: &str) -> Result<Vec<String>, String> {
    let s = std::fs::read_to_string(path).map_err(|e| format!("OCRError: 글자 사전 읽기 실패({}): {}", path, e))?;
    let mut v = vec!["blank".to_string()];
    // readlines + strip("\n") 와 같게 — 끝 줄바꿈 뒤 빈 줄은 줄이 아니다
    v.extend(s.split('\n').map(|l| l.trim_end_matches('\r').to_string()));
    if v.last().map_or(false, |l| l.is_empty()) && s.ends_with('\n') {
        v.pop();
    }
    v.push(" ".to_string());
    Ok(v)
}

//------------------------------------------------------------------
// 줄 그림들 → (글, 점수) (핵심)
//
// -in: sess = 인식 모델 세션 / dict = load_dict 결과 / crops = 잘라낸 줄 그림들
//
// -out: Ok(입력 순서 그대로의 (글, 점수) 목록)
// -out: error = 추론 실패면 Err("OCRError: …")
//------------------------------------------------------------------
pub fn recognize(sess: &mut Session, dict: &[String], crops: &[Bgr]) -> Result<Vec<(String, f32)>, String> {
    let mut out = vec![(String::new(), 0f32); crops.len()];
    // np.argsort 대신 안정 정렬 — 비율이 같은 줄은 원래 순서
    let mut idx: Vec<usize> = (0..crops.len()).collect();
    idx.sort_by(|&a, &b| {
        let ra = crops[a].w as f32 / crops[a].h as f32;
        let rb = crops[b].w as f32 / crops[b].h as f32;
        ra.partial_cmp(&rb).unwrap_or(std::cmp::Ordering::Equal)
    });
    for part in idx.chunks(BATCH) {
        let mut max_ratio = IMG_W as f32 / IMG_H as f32;
        for &i in part {
            max_ratio = max_ratio.max(crops[i].w as f32 / crops[i].h as f32);
        }
        let width = (IMG_H as f32 * max_ratio) as usize;
        let plane = IMG_H * width;
        let mut x = vec![0f32; part.len() * 3 * plane];
        for (bi, &i) in part.iter().enumerate() {
            let c = &crops[i];
            let ratio = c.w as f32 / c.h as f32;
            let rw = ((IMG_H as f32 * ratio).ceil() as usize).min(width).max(1);
            let r = imgops::resize_linear(c, rw, IMG_H);
            let base = bi * 3 * plane;
            for yy in 0..IMG_H {
                for xx in 0..rw {
                    let p = &r.data[(yy * rw + xx) * 3..(yy * rw + xx) * 3 + 3];
                    for ch in 0..3 {
                        x[base + ch * plane + yy * width + xx] = (p[ch] as f32 / 255.0 - 0.5) / 0.5;
                    }
                }
            }
        }
        let input = Tensor::from_array(([part.len(), 3usize, IMG_H, width], x)).map_err(|e| format!("OCRError: {}", e))?;
        let feeds: Vec<(String, SessionInputValue)> = vec![("x".into(), input.into())];
        let outputs = sess.run(feeds).map_err(|e| format!("OCRError: 인식 추론 실패: {}", e))?;
        let (oshape, prob) = outputs[0].try_extract_tensor::<f32>().map_err(|e| format!("OCRError: {}", e))?;
        let (t, k) = (oshape[1] as usize, oshape[2] as usize);
        for (bi, &i) in part.iter().enumerate() {
            out[i] = ctc_decode(&prob[bi * t * k..(bi + 1) * t * k], t, k, dict);
        }
    }
    Ok(out)
}

//------------------------------------------------------------------
// CTC 탐욕 풀이 (CTCLabelDecode.decode, remove_duplicate=True)
//=> 칸마다 가장 큰 확률의 번호 → 앞 칸과 같은 번호·0번은 뺀다 → 남은 글자를 잇고 확률을 평균.
//   확률은 소수 5자리로 반올림해 평균한다(파이썬과 점수를 맞추려고).
//------------------------------------------------------------------
fn ctc_decode(p: &[f32], t: usize, k: usize, dict: &[String]) -> (String, f32) {
    let mut text = String::new();
    let mut confs: Vec<f64> = vec![];
    let mut prev = usize::MAX;
    for s in 0..t {
        let row = &p[s * k..(s + 1) * k];
        let (mut bi, mut bv) = (0usize, f32::MIN);
        for (j, &v) in row.iter().enumerate() {
            if v > bv {
                bv = v;
                bi = j;
            }
        }
        let keep = bi != prev && bi != 0;
        prev = bi;
        if !keep {
            continue;
        }
        if let Some(ch) = dict.get(bi) {
            text.push_str(ch);
        }
        confs.push((bv as f64 * 1e5).round() / 1e5);
    }
    let score = if confs.is_empty() { 0.0 } else { confs.iter().sum::<f64>() / confs.len() as f64 };
    (text, ((score * 1e5).round() / 1e5) as f32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ctc_merges_repeats_and_drops_blank() {
        let dict: Vec<String> = ["blank", "가", "나", " "].iter().map(|s| s.to_string()).collect();
        // 칸: 가 가 blank 가 나 → "가가나"
        let mut p = vec![0f32; 5 * 4];
        for (s, j) in [1usize, 1, 0, 1, 2].iter().enumerate() {
            p[s * 4 + j] = 0.9;
        }
        let (t, sc) = ctc_decode(&p, 5, 4, &dict);
        assert_eq!(t, "가가나");
        assert!((sc - 0.9).abs() < 1e-4);
    }
}
