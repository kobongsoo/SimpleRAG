//------------------------------------------------------------------
// 그림 파일 읽기 — OCR 입력 (OCR 설계서 §4-4)
//=> .png · .jpg · .jpeg · .bmp 는 image crate 로 읽고 EXIF 방향대로 세운다(휴대폰 사진).
//   .tif · .tiff 는 tiff crate 로 쪽마다 읽는다(스캐너·팩스의 여러 쪽 TIFF). 흑백 팩스는
//   "0 = 흰색(WhiteIsZero)" 표시를 보고 직접 뒤집는다 — image crate 는 첫 쪽만 읽고 뒤집지도 않는다.
//   투명한 부분은 흰 바탕에 얹는다(투명 바탕 글자가 검은 바탕이 되지 않게).
//------------------------------------------------------------------

use super::Bgr;

// 이보다 큰 그림 파일은 건너뛴다(설계서 §4-4)
pub const MAX_FILE_BYTES: u64 = 50 * 1024 * 1024;
// 짧은 변이 이보다 작으면 아이콘으로 보고 OCR 하지 않는다
pub const MIN_SHORT_SIDE: usize = 100;
// OCR 대상 그림 확장자
pub const IMAGE_EXTS: &[&str] = &[".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"];

// 이 경로가 OCR 대상 그림 파일인가(확장자만 본다)
pub fn is_image(path: &str) -> bool {
    let e = crate::pyutil::ext_lower(path);
    IMAGE_EXTS.contains(&e.as_str())
}

//------------------------------------------------------------------
// 그림 파일 → 쪽 그림들 (핵심)
//
// -in: path = 그림 파일 경로
//
// -out: Ok(쪽 그림 목록 — TIFF 만 여러 장, 나머지는 한 장)
// -out: error = 50MB 초과·읽기·풀기 실패면 Err(오류문)
//------------------------------------------------------------------
pub fn load(path: &str) -> Result<Vec<Bgr>, String> {
    let size = std::fs::metadata(path).map_err(|e| format!("그림 파일 읽기 실패: {}", e))?.len();
    if size > MAX_FILE_BYTES {
        return Err(format!("그림 파일이 {}MB 를 넘습니다 ({}MB)", MAX_FILE_BYTES >> 20, size >> 20));
    }
    let ext = crate::pyutil::ext_lower(path);
    if ext == ".tif" || ext == ".tiff" {
        // TIFF 를 직접 못 풀면(팔레트 등) image crate 로 첫 쪽만이라도 읽는다
        if let Ok(v) = tiff_frames(path) {
            if !v.is_empty() {
                return Ok(v);
            }
        }
    }
    Ok(vec![load_one(path)?])
}

// image crate 로 한 장 읽기 — EXIF 방향 적용, 투명은 흰 바탕에
fn load_one(path: &str) -> Result<Bgr, String> {
    use image::ImageDecoder;
    let rd = image::ImageReader::open(path).map_err(|e| format!("그림 파일 열기 실패: {}", e))?
        .with_guessed_format().map_err(|e| format!("그림 형식 알 수 없음: {}", e))?;
    let mut dec = rd.into_decoder().map_err(|e| format!("그림 풀기 실패: {}", e))?;
    let orient = dec.orientation().unwrap_or(image::metadata::Orientation::NoTransforms);
    let mut img = image::DynamicImage::from_decoder(dec).map_err(|e| format!("그림 풀기 실패: {}", e))?;
    img.apply_orientation(orient);
    let rgba = img.to_rgba8();
    let (w, h) = (rgba.width() as usize, rgba.height() as usize);
    let mut out = Bgr::white(w, h);
    for (i, p) in rgba.pixels().enumerate() {
        let a = p[3] as u32;
        // 흰 바탕(255) 위에 알파 합성
        let blend = |c: u8| ((c as u32 * a + 255 * (255 - a)) / 255) as u8;
        out.data[i * 3] = blend(p[2]);
        out.data[i * 3 + 1] = blend(p[1]);
        out.data[i * 3 + 2] = blend(p[0]);
    }
    Ok(out)
}

//------------------------------------------------------------------
// TIFF 쪽마다 읽기
//=> 흑백(1비트)·회색(8·16비트)·RGB(A)(8·16비트)·CMYK(8비트)를 BGR 로 바꾼다.
//   PhotometricInterpretation 0(WhiteIsZero)이면 밝기를 뒤집는다. 쪽 상한(ocr_max_pages)까지만.
// -out: error = 처음 쪽부터 못 풀면 Err — 뒤 쪽에서 실패하면 그때까지 읽은 쪽만 돌려준다
//------------------------------------------------------------------
fn tiff_frames(path: &str) -> Result<Vec<Bgr>, String> {
    use tiff::decoder::{Decoder, DecodingResult};
    use tiff::ColorType as C;
    let f = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mut dec = Decoder::new(std::io::BufReader::new(f)).map_err(|e| e.to_string())?;
    let max_pages = crate::config::cfg().ocr_max_pages.max(1) as usize;
    let mut out = vec![];
    loop {
        let frame = (|| -> Result<Bgr, String> {
            let (w, h) = dec.dimensions().map_err(|e| e.to_string())?;
            let (w, h) = (w as usize, h as usize);
            let ct = dec.colortype().map_err(|e| e.to_string())?;
            let white_is_zero = dec.get_tag_u32(tiff::tags::Tag::PhotometricInterpretation).unwrap_or(1) == 0;
            let res = dec.read_image().map_err(|e| e.to_string())?;
            let mut img = Bgr::white(w, h);
            // 회색 한 값 넣기(WhiteIsZero 면 뒤집음)
            let put_gray = |img: &mut Bgr, i: usize, g: u8| {
                let g = if white_is_zero { 255 - g } else { g };
                img.data[i * 3..i * 3 + 3].copy_from_slice(&[g, g, g]);
            };
            match (ct, res) {
                (C::Gray(1), DecodingResult::U8(v)) => {
                    let row = w.div_ceil(8);
                    if v.len() < row * h {
                        return Err("흑백 TIFF 크기 불일치".into());
                    }
                    for y in 0..h {
                        for x in 0..w {
                            let bit = (v[y * row + x / 8] >> (7 - (x % 8))) & 1;
                            put_gray(&mut img, y * w + x, if bit == 1 { 255 } else { 0 });
                        }
                    }
                }
                (C::Gray(8), DecodingResult::U8(v)) if v.len() >= w * h => {
                    for (i, &g) in v.iter().take(w * h).enumerate() {
                        put_gray(&mut img, i, g);
                    }
                }
                (C::Gray(16), DecodingResult::U16(v)) if v.len() >= w * h => {
                    for (i, &g) in v.iter().take(w * h).enumerate() {
                        put_gray(&mut img, i, (g >> 8) as u8);
                    }
                }
                (C::RGB(8), DecodingResult::U8(v)) if v.len() >= w * h * 3 => {
                    for (i, p) in v.chunks_exact(3).take(w * h).enumerate() {
                        img.data[i * 3..i * 3 + 3].copy_from_slice(&[p[2], p[1], p[0]]);
                    }
                }
                (C::RGB(16), DecodingResult::U16(v)) if v.len() >= w * h * 3 => {
                    for (i, p) in v.chunks_exact(3).take(w * h).enumerate() {
                        img.data[i * 3..i * 3 + 3].copy_from_slice(&[(p[2] >> 8) as u8, (p[1] >> 8) as u8, (p[0] >> 8) as u8]);
                    }
                }
                (C::RGBA(8), DecodingResult::U8(v)) if v.len() >= w * h * 4 => {
                    for (i, p) in v.chunks_exact(4).take(w * h).enumerate() {
                        let a = p[3] as u32;
                        let bl = |c: u8| ((c as u32 * a + 255 * (255 - a)) / 255) as u8;
                        img.data[i * 3..i * 3 + 3].copy_from_slice(&[bl(p[2]), bl(p[1]), bl(p[0])]);
                    }
                }
                (C::CMYK(8), DecodingResult::U8(v)) if v.len() >= w * h * 4 => {
                    for (i, p) in v.chunks_exact(4).take(w * h).enumerate() {
                        let k = 255 - p[3] as u32;
                        let cv = |c: u8| ((255 - c as u32) * k / 255) as u8;
                        img.data[i * 3..i * 3 + 3].copy_from_slice(&[cv(p[2]), cv(p[1]), cv(p[0])]);
                    }
                }
                (ct, _) => return Err(format!("지원하지 않는 TIFF 색 형식: {:?}", ct)),
            }
            Ok(img)
        })();
        match frame {
            Ok(img) => out.push(img),
            Err(e) if out.is_empty() => return Err(e),
            Err(_) => break,
        }
        if out.len() >= max_pages || !dec.more_images() || dec.next_image().is_err() {
            break;
        }
    }
    Ok(out)
}
