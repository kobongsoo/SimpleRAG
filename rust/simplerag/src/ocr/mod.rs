//------------------------------------------------------------------
// 스캔 문서·그림 파일 OCR (OCR 설계서 2026-11-13)
//=> 검출 PP-OCRv6_tiny_det + 인식 korean_PP-OCRv5_mobile_rec (둘 다 ONNX, onnxruntime 로 돌림).
//   RapidOCR 3.9.2 를 정답으로 두고 같은 결과가 나오게 옮겼다(OpenCV 없음).
//    · 엔진은 처음 쓸 때 한 번 적재하고, 대기열이 끝나면 unload() 로 내려 메모리를 돌려준다.
//    · 결과(줄 목록)는 파일 내용 해시로 캐시한다 — 재구축·이동·이름 바꿈에도 다시 OCR 하지 않는다.
//------------------------------------------------------------------

pub mod det;
pub mod geom;
pub mod image;
pub mod imgops;
pub mod rec;

use std::collections::BTreeMap;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use crate::config::cfg;
use crate::pyutil;
pub use imgops::Bgr;

// 모델·후처리가 바뀌면 올린다 — 옛 캐시는 무시하고 다시 OCR 한다
pub const ENGINE: &str = "ppocrv6-tiny-det+ko-ppocrv5-mobile-rec/1";
pub const DET_FILE: &str = "PP-OCRv6_tiny_det.onnx";
pub const REC_FILE: &str = "korean_PP-OCRv5_mobile_rec.onnx";
pub const DICT_FILE: &str = "korean_PP-OCRv5_mobile_rec.dict.txt";
// PDF 쪽을 그리는 해상도 — 측정(2026-11-13)도 200dpi 로 했다
pub const DPI: f32 = 200.0;
// 줄 점수가 이보다 낮으면 버린다(RapidOCR text_score 기본값)
const TEXT_SCORE: f32 = 0.5;
// 그림 크기 한계(RapidOCR Global.max_side_len · min_side_len)
const MAX_SIDE: usize = 4000;
const MIN_SIDE: usize = 30;

//------------------------------------------------------------------
// OCR 한 줄 (검출 상자 하나)
// -필드: text  = 읽은 글
// -필드: score = 글자 확률 평균(0~1)
// -필드: quad  = 상자 네 꼭짓점 [왼위, 오른위, 오른아래, 왼아래] — 입력 그림 픽셀 좌표
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Line {
    pub text: String,
    pub score: f32,
    pub quad: [[f32; 2]; 4],
}

impl Line {
    // 상자를 감싸는 축 정렬 범위 (x0, y0, x1, y1)
    pub fn bbox(&self) -> [f32; 4] {
        let xs = self.quad.iter().map(|p| p[0]);
        let ys = self.quad.iter().map(|p| p[1]);
        [xs.clone().fold(f32::MAX, f32::min), ys.clone().fold(f32::MAX, f32::min),
         xs.fold(f32::MIN, f32::max), ys.fold(f32::MIN, f32::max)]
    }
}

// 적재된 두 모델과 글자 사전
struct Engine {
    det: ort::session::Session,
    rec: ort::session::Session,
    dict: Vec<String>,
}

static ENGINE_SLOT: Mutex<Option<Engine>> = Mutex::new(None);

//------------------------------------------------------------------
// 모델 파일이 다 있나
//=> 설정 extract.ocr 와 상관없이 파일만 본다. 없으면 OCR 을 끈 것처럼 동작한다(설계서 §10).
//
// -out: Ok(()) / Err(없는 파일 설명)
//------------------------------------------------------------------
pub fn models_present() -> Result<(), String> {
    let d = &cfg().ocr_dir;
    for f in [DET_FILE, REC_FILE, DICT_FILE] {
        let p = pyutil::join(d, f);
        if !std::path::Path::new(&p).is_file() {
            return Err(format!("OCR 모델 파일 없음: {}", p));
        }
    }
    Ok(())
}

//------------------------------------------------------------------
// OCR 을 쓸 수 있나 — 설정이 켜져 있고 모델 파일이 다 있을 때
//=> 모델이 없으면 로그에 한 번만 경고한다.
//------------------------------------------------------------------
pub fn enabled() -> bool {
    if !cfg().ocr {
        return false;
    }
    static WARNED: std::sync::Once = std::sync::Once::new();
    match models_present() {
        Ok(()) => true,
        Err(e) => {
            WARNED.call_once(|| crate::cli::log(&format!("⚠️ {} — 스캔 문서 OCR 을 건너뜁니다", e)));
            false
        }
    }
}

// 그림 파일도 OCR 하나 — ocr 이 켜져 있고 ocr_images 도 켜져 있을 때
pub fn images_enabled() -> bool {
    cfg().ocr_images && enabled()
}

// 엔진 적재(이미 있으면 그대로)
fn ensure(slot: &mut Option<Engine>) -> Result<&mut Engine, String> {
    if slot.is_none() {
        models_present().map_err(|e| format!("OCRError: {}", e))?;
        let d = &cfg().ocr_dir;
        let th = cfg().ocr_threads.max(1) as usize;
        let det = crate::embed::make_session(&pyutil::join(d, DET_FILE), th, true)
            .map_err(|e| format!("OCRError: 검출 모델 적재 실패: {}", e))?;
        let rec = crate::embed::make_session(&pyutil::join(d, REC_FILE), th, true)
            .map_err(|e| format!("OCRError: 인식 모델 적재 실패: {}", e))?;
        let dict = rec::load_dict(&pyutil::join(d, DICT_FILE))?;
        *slot = Some(Engine { det, rec, dict });
    }
    Ok(slot.as_mut().unwrap())
}

//------------------------------------------------------------------
// 엔진 내리기 — 대기열을 다 처리했거나 멈출 때 메모리를 돌려준다
//------------------------------------------------------------------
pub fn unload() {
    *ENGINE_SLOT.lock().unwrap_or_else(|p| p.into_inner()) = None;
}

//------------------------------------------------------------------
// 그림 한 장 OCR (핵심)
//=> 1) 긴 변이 4000 을 넘으면 줄이고, 짧은 변이 30 미만이면 늘린다(좌표는 나중에 되돌린다)
//   2) 검출 → 상자마다 바로 세워 잘라내기 → 인식
//   3) 빈 글·점수 0.5 미만 줄은 버린다
//
// -in: img = BGR 그림
//
// -out: Ok(줄 목록 — 읽는 순서, 원래 그림 좌표)
// -out: error = 모델 적재·추론 실패면 Err("OCRError: …")
//------------------------------------------------------------------
pub fn ocr_image(img: &Bgr) -> Result<Vec<Line>, String> {
    if img.w == 0 || img.h == 0 {
        return Ok(vec![]);
    }
    // 1) 크기 한계 — 비율을 기억해 두었다가 상자 좌표를 원래 크기로 되돌린다
    let mut work = std::borrow::Cow::Borrowed(img);
    let mut scale = 1f32;
    if img.w.max(img.h) > MAX_SIDE {
        scale = MAX_SIDE as f32 / img.w.max(img.h) as f32;
    } else if img.w.min(img.h) < MIN_SIDE {
        scale = MIN_SIDE as f32 / img.w.min(img.h) as f32;
    }
    if scale != 1.0 {
        let nw = ((img.w as f32 * scale) as usize).max(1);
        let nh = ((img.h as f32 * scale) as usize).max(1);
        work = std::borrow::Cow::Owned(imgops::resize_linear(img, nw, nh));
    }
    let mut g = ENGINE_SLOT.lock().unwrap_or_else(|p| p.into_inner());
    let eng = ensure(&mut g)?;
    // 2) 검출 → 잘라내기 → 인식
    let quads = det::detect(&mut eng.det, &work)?;
    let mut kept = vec![];
    let mut crops = vec![];
    for q in quads {
        if let Some(c) = imgops::rotate_crop(&work, &q) {
            kept.push(q);
            crops.push(c);
        }
    }
    let texts = rec::recognize(&mut eng.rec, &eng.dict, &crops)?;
    drop(g);
    // 3) 걸러 내고 좌표를 원래 크기로
    let mut out = vec![];
    for (q, (t, s)) in kept.into_iter().zip(texts) {
        if pyutil::strip(&t).is_empty() || s < TEXT_SCORE {
            continue;
        }
        let quad = q.map(|p| [p[0] / scale, p[1] / scale]);
        out.push(Line { text: t, score: s, quad });
    }
    Ok(out)
}

//------------------------------------------------------------------
// 줄 묶기 — OCR 상자들 → 화면의 한 줄씩 (설계서 §5-4)
//=> 1) 상자를 세로 가운데로 정렬하고, 세로로 절반 이상 겹치면 같은 줄로 묶는다
//   2) 줄 안에서는 왼쪽부터 잇고, 상자 사이 틈이 글자 높이의 0.5배보다 넓으면 띄어쓰기를 넣는다
//      (한국어 인식은 상자 안 띄어쓰기를 자주 빠뜨린다 — 상자 사이 띄어쓰기라도 살린다)
//
// -in: lines = OCR 줄 목록 (bbox 를 쓴다)
//
// -out: [(줄 글, [x0, y0, x1, y1], 평균 높이)] — 위에서 아래 순서
// -out: error = 없음
//------------------------------------------------------------------
pub fn group_rows(lines: &[(String, [f32; 4])]) -> Vec<(String, [f32; 4], f32)> {
    let mut idx: Vec<usize> = (0..lines.len()).collect();
    idx.sort_by(|&a, &b| {
        let ca = (lines[a].1[1] + lines[a].1[3]) / 2.0;
        let cb = (lines[b].1[1] + lines[b].1[3]) / 2.0;
        ca.partial_cmp(&cb).unwrap_or(std::cmp::Ordering::Equal)
    });
    // 줄마다 (상자 번호들, 세로 범위)
    let mut rows: Vec<(Vec<usize>, f32, f32)> = vec![];
    for i in idx {
        let b = lines[i].1;
        let h = (b[3] - b[1]).max(1.0);
        let mut placed = false;
        if let Some(r) = rows.last_mut() {
            let rh = (r.2 - r.1).max(1.0);
            let ov = b[3].min(r.2) - b[1].max(r.1);
            if ov >= 0.5 * h.min(rh) {
                r.0.push(i);
                r.1 = r.1.min(b[1]);
                r.2 = r.2.max(b[3]);
                placed = true;
            }
        }
        if !placed {
            rows.push((vec![i], b[1], b[3]));
        }
    }
    let mut out = vec![];
    for (mut ids, _, _) in rows {
        ids.sort_by(|&a, &b| lines[a].1[0].partial_cmp(&lines[b].1[0]).unwrap_or(std::cmp::Ordering::Equal));
        let avg_h = ids.iter().map(|&i| lines[i].1[3] - lines[i].1[1]).sum::<f32>() / ids.len() as f32;
        let mut text = String::new();
        let mut bb = [f32::MAX, f32::MAX, f32::MIN, f32::MIN];
        let mut prev_x1: Option<f32> = None;
        for &i in &ids {
            let (t, b) = (&lines[i].0, lines[i].1);
            if let Some(px) = prev_x1 {
                if b[0] - px > 0.5 * avg_h {
                    text.push(' ');
                }
            }
            text.push_str(pyutil::strip(t));
            prev_x1 = Some(prev_x1.map_or(b[2], |p| p.max(b[2])));
            bb = [bb[0].min(b[0]), bb[1].min(b[1]), bb[2].max(b[2]), bb[3].max(b[3])];
        }
        out.push((text, bb, avg_h));
    }
    out
}

// ── 어느 쪽을 OCR 하나 (설계서 §4-2) ───────────────────────────

// 그림 넓이를 셀 격자 — 겹친 그림을 한 번만 센다(2026-11-13 분포 실측과 같은 40×40)
const GRID: usize = 40;

// 그림 상자들이 쪽을 덮는 비율(0~1)
fn coverage(boxes: &[[f32; 4]], pw: f32, ph: f32) -> f32 {
    if boxes.is_empty() || pw <= 0.0 || ph <= 0.0 {
        return 0.0;
    }
    let mut m = [[false; GRID]; GRID];
    for b in boxes {
        let x0 = ((b[0].max(0.0) / pw) * GRID as f32) as usize;
        let x1 = (((b[2].min(pw) / pw) * GRID as f32).ceil() as usize).min(GRID);
        let y0 = ((b[1].max(0.0) / ph) * GRID as f32) as usize;
        let y1 = (((b[3].min(ph) / ph) * GRID as f32).ceil() as usize).min(GRID);
        for row in m.iter_mut().take(y1).skip(y0) {
            for c in row.iter_mut().take(x1).skip(x0) {
                *c = true;
            }
        }
    }
    m.iter().flatten().filter(|&&c| c).count() as f32 / (GRID * GRID) as f32
}

//------------------------------------------------------------------
// PDF 쪽 가운데 OCR 할 쪽 고르기 (설계서 §4-2)
//=> 1) 반복 그림 — 문서 쪽의 절반 이상(3쪽 이상)에 같은 자리·같은 픽셀 크기로 나오는 그림(로고·배경·틀)
//   2) 쪽마다: 공백 뺀 글자 수 n, 모든 그림 넓이 all, 반복 아니고 쪽의 2% 이상인 그림 넓이 real
//      · n < 10 이고 all ≥ 30%                → OCR (스캔 쪽)
//      · n < 10                               → 건너뜀 (빈 쪽·간지)
//      · real ≥ 70% 이고 n < 200              → OCR (스캔 쪽에 쪽 번호·도장 글자가 얹힌 쪽, 그림 속 글 디자인)
//      · 그 밖                                → 글자층만
//   3) 앞에서부터 ocr_max_pages 쪽까지만
//
// -in: pages = pdfium 으로 읽은 쪽들(images · page_w · page_h 가 채워져 있어야 한다)
//
// -out: (OCR 할 쪽 번호(0부터) 목록, 상한에 걸려 뺀 쪽 수)
// -out: error = 없음
//------------------------------------------------------------------
pub fn pdf_ocr_pages(pages: &[crate::pdfium::PageText]) -> (Vec<usize>, usize) {
    let np = pages.len();
    // 1) 반복 그림 열쇠 — (정수로 반올림한 범위, 픽셀 크기)
    let key = |o: &crate::pdfium::ImgObj| ([o.b[0].round() as i32, o.b[1].round() as i32, o.b[2].round() as i32, o.b[3].round() as i32], o.px);
    let mut rep: std::collections::HashMap<([i32; 4], (u32, u32)), usize> = std::collections::HashMap::new();
    for p in pages {
        let set: std::collections::HashSet<_> = p.images.iter().map(key).collect();
        for k in set {
            *rep.entry(k).or_insert(0) += 1;
        }
    }
    let need = (np as f32 * 0.5).max(3.0);
    let repeated = |o: &crate::pdfium::ImgObj| np >= 3 && rep.get(&key(o)).map_or(false, |&c| c as f32 >= need);
    let mut out = vec![];
    for (i, p) in pages.iter().enumerate() {
        if p.images.is_empty() {
            continue;
        }
        let n = p.text.chars().filter(|c| !c.is_whitespace()).count();
        let all: Vec<[f32; 4]> = p.images.iter().map(|o| o.b).collect();
        let page_area = p.page_w * p.page_h;
        let real: Vec<[f32; 4]> = p.images.iter()
            .filter(|o| !repeated(o) && (o.b[2] - o.b[0]) * (o.b[3] - o.b[1]) >= 0.02 * page_area)
            .map(|o| o.b).collect();
        // 그림 좌표는 PDF 좌표(아래가 0) — 넓이 비율만 보므로 위아래 방향은 상관없다
        let cov_all = coverage(&all, p.page_w, p.page_h);
        let cov_real = coverage(&real, p.page_w, p.page_h);
        let hit = if n < 10 { cov_all >= 0.3 } else { cov_real >= 0.7 && n < 200 };
        if hit {
            out.push(i);
        }
    }
    let cap = cfg().ocr_max_pages.max(1) as usize;
    let dropped = out.len().saturating_sub(cap);
    out.truncate(cap);
    (out, dropped)
}

//------------------------------------------------------------------
// 글자층과 겹친 OCR 줄 빼기 (설계서 §4-2 ②)
//=> OCR 줄 상자 넓이의 절반 이상이 글자층 글자 상자들과 겹치면 버린다 — 그 자리에는 더 정확한
//   글자층 글이 이미 있다(쪽 번호·도장 글자를 두 번 넣지 않게).
//
// -in: lines = OCR 줄 / chars = 글자층 글자 상자 [x0, y0, x1, y1] (같은 그림 좌표)
//
// -out: 남긴 줄
// -out: error = 없음
//------------------------------------------------------------------
pub fn drop_overlapping(lines: Vec<Line>, chars: &[[f32; 4]]) -> Vec<Line> {
    if chars.is_empty() {
        return lines;
    }
    lines.into_iter().filter(|l| {
        let b = l.bbox();
        let area = ((b[2] - b[0]) * (b[3] - b[1])).max(1.0);
        // 글자 상자끼리는 거의 겹치지 않으므로 겹친 넓이를 더해 근사한다
        let ov: f32 = chars.iter().map(|c| {
            let w = (b[2].min(c[2]) - b[0].max(c[0])).max(0.0);
            let h = (b[3].min(c[3]) - b[1].max(c[1])).max(0.0);
            w * h
        }).sum();
        ov < 0.5 * area
    }).collect()
}

// ── 추출 → 색인 사이의 OCR 정보 ─────────────────────────────

//------------------------------------------------------------------
// 문서 하나의 OCR 상태 (추출이 남기고 sync_doc 이 읽는다)
// -필드: pending = 아직 OCR 하지 않은 쪽(1부터, 그림 파일은 [1]) — 비어 있지 않으면 대기열로 간다
// -필드: done    = OCR 글이 들어간 쪽(1부터) — 그 쪽 청크에 ocr 표시를 단다
// -필드: whole   = 그림 파일처럼 모든 청크가 OCR 글인가
//------------------------------------------------------------------
#[derive(Debug, Clone, Default)]
pub struct DocOcr {
    pub pending: Vec<usize>,
    pub done: Vec<usize>,
    pub whole: bool,
}

// 추출은 준비 스레드에서도 돌므로 스레드 사이에서 보이는 표에 남긴다(열쇠 = 절대 경로)
static DOC_OCR: Mutex<Option<std::collections::HashMap<String, DocOcr>>> = Mutex::new(None);

// 추출 결과의 OCR 상태 남기기 — 비어 있으면 지운다
pub fn note(path: &str, info: DocOcr) {
    let mut g = DOC_OCR.lock().unwrap_or_else(|p| p.into_inner());
    let m = g.get_or_insert_with(Default::default);
    let k = pyutil::abspath(path);
    if info.pending.is_empty() && info.done.is_empty() && !info.whole {
        m.remove(&k);
    } else {
        m.insert(k, info);
    }
}

// 남긴 OCR 상태 꺼내기(꺼내면 지운다)
pub fn take_note(path: &str) -> DocOcr {
    let mut g = DOC_OCR.lock().unwrap_or_else(|p| p.into_inner());
    g.get_or_insert_with(Default::default).remove(&pyutil::abspath(path)).unwrap_or_default()
}

// ── 캐시 ─────────────────────────────────────────────────────

//------------------------------------------------------------------
// 캐시의 한 줄
// -필드: t = 글 / s = 점수 / b = [x0, y0, x1, y1] (200dpi 그림 픽셀 좌표)
//------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CachedLine {
    pub t: String,
    pub s: f32,
    pub b: [f32; 4],
}

//------------------------------------------------------------------
// 파일 하나의 OCR 캐시 (설계서 §7)
// -필드: engine = 만든 엔진 이름(ENGINE 과 다르면 버림)
// -필드: dpi    = PDF 쪽을 그린 해상도(그림 파일은 0)
// -필드: pages  = 쪽 번호(1부터) → 줄 목록. OCR 해 본 쪽은 줄이 없어도 빈 목록으로 남는다
//------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct Cache {
    pub engine: String,
    pub dpi: f32,
    pub pages: BTreeMap<usize, Vec<CachedLine>>,
}

// <데이터 폴더>\ocr_cache\<해시 앞 2자>\<해시>.json
pub fn cache_path(hash: &str) -> String {
    let dir = pyutil::join(&pyutil::join(&cfg().root, "ocr_cache"), &hash[..hash.len().min(2)]);
    pyutil::join(&dir, &format!("{}.json", hash))
}

//------------------------------------------------------------------
// 캐시 읽기 — 없거나, 깨졌거나, 엔진이 다르면 None
//------------------------------------------------------------------
pub fn load_cache(hash: &str) -> Option<Cache> {
    let s = std::fs::read_to_string(cache_path(hash)).ok()?;
    let c: Cache = serde_json::from_str(&s).ok()?;
    if c.engine != ENGINE {
        return None;
    }
    Some(c)
}

//------------------------------------------------------------------
// 캐시 쓰기 — 임시 파일에 쓴 뒤 바꿔 넣는다(쓰다 꺼져도 반쪽 파일이 남지 않게)
//
// -out: error = 폴더·파일 쓰기 실패면 Err(오류문)
//------------------------------------------------------------------
pub fn save_cache(hash: &str, c: &Cache) -> Result<(), String> {
    let p = cache_path(hash);
    std::fs::create_dir_all(pyutil::dirname(&p)).map_err(|e| format!("OCR 캐시 폴더 만들기 실패: {}", e))?;
    let tmp = format!("{}.tmp", p);
    std::fs::write(&tmp, serde_json::to_string(c).unwrap_or_default()).map_err(|e| format!("OCR 캐시 쓰기 실패: {}", e))?;
    std::fs::rename(&tmp, &p).map_err(|e| format!("OCR 캐시 바꿔 넣기 실패: {}", e))
}

// 캐시 지우기 — 없으면 조용히 넘어간다
pub fn remove_cache(hash: &str) {
    let _ = std::fs::remove_file(cache_path(hash));
}

// OCR 줄 → 캐시 줄 (점수는 소수 3자리)
pub fn to_cached(lines: &[Line]) -> Vec<CachedLine> {
    lines.iter().map(|l| CachedLine { t: l.text.clone(), s: (l.score * 1000.0).round() / 1000.0, b: l.bbox() }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_join_with_gap_space() {
        // 같은 줄 두 상자(틈 넓음) + 아래 줄 하나
        let lines = vec![
            ("수신:".to_string(), [0.0, 0.0, 40.0, 20.0]),
            ("나우로보틱스".to_string(), [60.0, 1.0, 160.0, 21.0]),
            ("참조".to_string(), [0.0, 40.0, 30.0, 60.0]),
        ];
        let r = group_rows(&lines);
        assert_eq!(r.len(), 2);
        assert_eq!(r[0].0, "수신: 나우로보틱스");
        assert_eq!(r[1].0, "참조");
    }

    #[test]
    fn rows_no_space_when_touching() {
        let lines = vec![("가나".to_string(), [0.0, 0.0, 40.0, 20.0]), ("다".to_string(), [42.0, 0.0, 60.0, 20.0])];
        assert_eq!(group_rows(&lines)[0].0, "가나다");
    }
}
