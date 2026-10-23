//------------------------------------------------------------------
// DOC(Word 97-2003 바이너리) → 블록 (Python docmodel/doc_bin.py)
//=> 조각표(piece table)로 글자를 모으고, 문단 속성(PAPX)으로 스타일·표·목록을 읽는다.
//    · 문단 끝 표시: \r(문단) · \x07(표 칸/행 끝) · \x0c(쪽 나눔)
//    · 표: in_table/itap 문단을 칸으로 모으고 ttp(행 끝 표시)에서 행을 닫는다
//    · 머리말·글상자·각주·미주·메모는 이야기(story) 구간별로 평문 줄로 읽는다
//------------------------------------------------------------------

use std::collections::HashMap;
use std::io::Read;

use super::blocks::{self as B, Block, Document};
use super::tables;
use crate::pyutil;

const CCP_OFF: usize = 0x4C;
const FC_STSHF: usize = 0xA2;
const FC_PLCF_BTE_PAPX: usize = 0x102;
const FC_CLX: usize = 0x1A2;
const SPRM_IN_TABLE: u16 = 0x2416;
const SPRM_TTP: u16 = 0x2417;
const SPRM_ITAP: u16 = 0x6649;
const SPRM_INNER_TTP: u16 = 0x244C;
const SPRM_ILVL: u16 = 0x260A;
const SPRM_ILFO: u16 = 0x460B;
const SPRM_OUTLVL: u16 = 0x2640;
const SPRM_TABLE_HEADER: u16 = 0x3404;

fn u16at(b: &[u8], o: usize) -> Result<u16, String> {
    b.get(o..o + 2).map(|s| u16::from_le_bytes([s[0], s[1]])).ok_or_else(|| "struct.error".to_string())
}
fn u32at(b: &[u8], o: usize) -> Result<u32, String> {
    b.get(o..o + 4).map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]])).ok_or_else(|| "struct.error".to_string())
}
// 파이썬 바이트 자르기 b[a:b] — 범위 밖은 잘라 맞춤
fn sl(b: &[u8], a: usize, z: usize) -> &[u8] {
    let a = a.min(b.len());
    &b[a..z.min(b.len()).max(a)]
}

// 문단 속성
#[derive(Debug, Clone, Default)]
struct Props {
    istd: Option<u16>,
    in_table: Option<bool>,
    ttp: Option<bool>,
    inner_ttp: Option<bool>,
    itap: Option<i32>,
    ilvl: Option<u8>,
    ilfo: Option<u16>,
    outlvl: Option<u8>,
    tbl_header: Option<bool>,
}

//------------------------------------------------------------------
// DOC 추출 (진입점)
//------------------------------------------------------------------
pub fn extract(path: &str, title: &str) -> Result<Document, String> {
    let mut ole = cfb::open(path).map_err(|_| "doc 열기 실패: OSError".to_string())?;
    let read = |ole: &mut cfb::CompoundFile<std::fs::File>, n: &str| -> Result<Vec<u8>, String> {
        let mut s = ole.open_stream(n).map_err(|_| "doc 열기 실패: OSError".to_string())?;
        let mut b = vec![];
        s.read_to_end(&mut b).map_err(|e| e.to_string())?;
        Ok(b)
    };
    let wd = read(&mut ole, "/WordDocument")?;
    let flags = u16at(&wd, 0x0A)?;
    if flags & 0x0100 != 0 {
        return Err("암호화된 doc".into());
    }
    let mut tname = if flags & 0x0200 != 0 { "1Table" } else { "0Table" };
    if !ole.exists(format!("/{}", tname)) {
        tname = if ole.exists("/1Table") { "1Table" } else { "0Table" };
    }
    let tbl = read(&mut ole, &format!("/{}", tname))?;
    let pieces = pieces(&wd, &tbl)?;
    let text: Vec<char> = pieces.iter().flat_map(|p| p.4.chars()).collect();
    let papx = papx_runs(&wd, &tbl)?;
    if papx.is_empty() {
        return Err("doc 문단 속성(PAPX) 없음".into());
    }
    let styles = styles(&tbl, &wd);
    let mut doc = Document::new("doc", title);
    let mut ccp = [0u32; 8];
    for (k, v) in ccp.iter_mut().enumerate() {
        *v = u32at(&wd, CCP_OFF + k * 4)?;
    }
    let stories = stories(&ccp, text.len());
    let w = Walker::new(&text, &pieces, &papx, &styles);
    w.main(stories["text"], &mut doc.blocks)?;
    for t in w.plain(stories["hdd"])?.into_iter().chain(w.plain(stories["hdrtxbx"])?) {
        doc.blocks.push(Block::new(B::HEADER, t));
    }
    for t in w.plain(stories["txbx"])? {
        doc.blocks.push(Block::new(B::PARAGRAPH, t));
    }
    let mut notes = w.plain(stories["ftn"])?;
    notes.extend(w.plain(stories["edn"])?);
    notes.extend(w.plain(stories["atn"])?);
    if !notes.is_empty() {
        let mut h = Block::new(B::HEADING, "각주").lv(1);
        h.synthetic = true;
        doc.blocks.push(h);
        doc.blocks.extend(notes.into_iter().map(|t| Block::new(B::NOTE, t)));
    }
    Ok(doc)
}

// 이야기 구간 — (시작, 끝) 글자 위치
fn stories(ccp: &[u32; 8], total: usize) -> HashMap<&'static str, (usize, usize)> {
    let names = ["text", "ftn", "hdd", "mcr", "atn", "edn", "txbx", "hdrtxbx"];
    let mut out = HashMap::new();
    let mut pos = 0usize;
    for (name, n) in names.iter().zip(ccp) {
        out.insert(*name, (pos.min(total), (pos + *n as usize).min(total)));
        pos += *n as usize;
    }
    out
}

// cp1252 한 바이트(정의 안 된 자리는 U+FFFD — 파이썬 decode("cp1252", "replace"))
fn cp1252(b: u8) -> char {
    if b < 0x80 {
        return b as char;
    }
    if matches!(b, 0x81 | 0x8D | 0x8F | 0x90 | 0x9D) {
        return '\u{FFFD}';
    }
    let one = [b];
    let (s, _, _) = encoding_rs::WINDOWS_1252.decode(&one);
    s.chars().next().unwrap_or('\u{FFFD}')
}

// 조각 — (cp 시작, cp 끝, fc, 압축 여부, 글자)
type Piece = (u32, u32, usize, bool, String);

fn pieces(wd: &[u8], tbl: &[u8]) -> Result<Vec<Piece>, String> {
    let fc_clx = u32at(wd, FC_CLX)? as usize;
    let lcb_clx = u32at(wd, FC_CLX + 4)? as usize;
    let clx = sl(tbl, fc_clx, fc_clx + lcb_clx);
    let mut pcd: &[u8] = &[];
    let mut i = 0;
    while i < clx.len() {
        match clx[i] {
            0x01 => {
                if i + 3 > clx.len() {
                    break;
                }
                i += 3 + u16at(clx, i + 1)? as usize;
            }
            0x02 => {
                let lcb = u32at(clx, i + 1)? as usize;
                pcd = sl(clx, i + 5, i + 5 + lcb);
                break;
            }
            _ => break,
        }
    }
    let n = (pcd.len() as i64 - 4) / 12;
    if n <= 0 {
        return Err("doc piece table 없음".into());
    }
    let n = n as usize;
    let cps: Vec<u32> = (0..=n).map(|k| u32at(pcd, k * 4)).collect::<Result<_, _>>()?;
    let base = (n + 1) * 4;
    let mut out = vec![];
    for k in 0..n {
        let fc = u32at(pcd, base + k * 8 + 2)?;
        let cnt = cps[k + 1].wrapping_sub(cps[k]) as usize;
        if fc & 0x4000_0000 != 0 {
            let off = ((fc & 0x3FFF_FFFF) / 2) as usize;
            let raw = sl(wd, off, off + cnt);
            out.push((cps[k], cps[k + 1], off, true, raw.iter().map(|&b| cp1252(b)).collect()));
        } else {
            let fcu = fc as usize;
            let raw = sl(wd, fcu, fcu + cnt * 2);
            let u: Vec<u16> = raw.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            out.push((cps[k], cps[k + 1], fcu, false, String::from_utf16_lossy(&u)));
        }
    }
    Ok(out)
}

// PAPX 구간 — (fc 시작, fc 끝, 속성)
fn papx_runs(wd: &[u8], tbl: &[u8]) -> Result<Vec<(u32, u32, Props)>, String> {
    let fc = u32at(wd, FC_PLCF_BTE_PAPX)? as usize;
    let lcb = u32at(wd, FC_PLCF_BTE_PAPX + 4)? as usize;
    let plc = sl(tbl, fc, fc + lcb);
    let n = (plc.len() as i64 - 4) / 8;
    if n <= 0 {
        return Ok(vec![]);
    }
    let n = n as usize;
    let mut runs = vec![];
    for k in 0..n {
        let pn = u32at(plc, (n + 1) * 4 + k * 4)?;
        let off = ((pn & 0x3FFFFF) as usize) * 512;
        let page = sl(wd, off, off + 512);
        if page.len() < 512 {
            continue;
        }
        let crun = page[511] as usize;
        let fcs: Vec<u32> = (0..=crun).map(|j| u32at(page, j * 4)).collect::<Result<_, _>>()?;
        for j in 0..crun {
            let bidx = (crun + 1) * 4 + j * 13;
            let boff = *page.get(bidx).ok_or("IndexError")? as usize * 2;
            let props = if boff != 0 { parse_papx(page, boff)? } else { Props { istd: Some(0), ..Default::default() } };
            runs.push((fcs[j], fcs[j + 1], props));
        }
    }
    runs.sort_by_key(|r| r.0);
    Ok(runs)
}

fn parse_papx(page: &[u8], off: usize) -> Result<Props, String> {
    let cb = *page.get(off).ok_or("IndexError")? as usize;
    let (start, size) = if cb != 0 { (off + 1, 2 * cb - 1) } else { (off + 2, 2 * (*page.get(off + 1).ok_or("IndexError")? as usize)) };
    let data = sl(page, start, start + size);
    let mut p = sprms(if data.len() >= 2 { &data[2..] } else { &[] });
    p.istd = Some(if data.len() >= 2 { u16::from_le_bytes([data[0], data[1]]) } else { 0 });
    Ok(p)
}

// 속성 수정자(sprm) 목록 풀기 — 필요한 것만 읽는다
fn sprms(data: &[u8]) -> Props {
    let mut out = Props::default();
    let mut i = 0;
    while i + 2 <= data.len() {
        let sprm = u16::from_le_bytes([data[i], data[i + 1]]);
        i += 2;
        let spra = sprm >> 13;
        let size = match spra {
            0 | 1 => 1,
            2 | 4 | 5 => 2,
            3 => 4,
            7 => 3,
            _ => {
                if sprm == 0xD608 && i + 2 <= data.len() {
                    u16::from_le_bytes([data[i], data[i + 1]]) as usize + 1
                } else if i < data.len() {
                    data[i] as usize + 1
                } else {
                    break;
                }
            }
        };
        let val = sl(data, i, i + size);
        i += size;
        if val.is_empty() {
            break;
        }
        match sprm {
            SPRM_IN_TABLE => out.in_table = Some(val[0] != 0),
            SPRM_TTP => out.ttp = Some(val[0] != 0),
            SPRM_INNER_TTP => out.inner_ttp = Some(val[0] != 0),
            SPRM_ITAP if val.len() == 4 => out.itap = Some(i32::from_le_bytes([val[0], val[1], val[2], val[3]])),
            SPRM_ILVL => out.ilvl = Some(val[0]),
            SPRM_ILFO if val.len() == 2 => out.ilfo = Some(u16::from_le_bytes([val[0], val[1]])),
            SPRM_OUTLVL => out.outlvl = Some(val[0]),
            SPRM_TABLE_HEADER => out.tbl_header = Some(val[0] != 0),
            _ => {}
        }
    }
    out
}

// 스타일 — (sti, 기반 스타일, 이름, 개요 수준)
type Style = Option<(u16, u16, String, Option<u8>)>;

fn styles(tbl: &[u8], wd: &[u8]) -> Vec<Style> {
    let go = || -> Result<Vec<Style>, String> {
        let fc = u32at(wd, FC_STSHF)? as usize;
        let lcb = u32at(wd, FC_STSHF + 4)? as usize;
        let st = sl(tbl, fc, fc + lcb);
        let cb_stshi = u16at(st, 0)? as usize;
        let cstd = u16at(st, 2)? as usize;
        let cb_base = u16at(st, 4)? as usize;
        let mut pos = 2 + cb_stshi;
        let mut out = vec![];
        for _ in 0..cstd {
            let cb = u16at(st, pos)? as usize;
            let std = sl(st, pos + 2, pos + 2 + cb);
            pos += 2 + cb;
            if cb == 0 || std.len() < 10 {
                out.push(None);
                continue;
            }
            let w0 = u16at(std, 0)?;
            let w1 = u16at(std, 2)?;
            let (sti, stk, base) = (w0 & 0x0FFF, w1 & 0x000F, w1 >> 4);
            let mut name = String::new();
            let mut outlvl = None;
            let mut p = cb_base;
            if p + 2 <= std.len() {
                let cch = u16at(std, p)? as usize;
                let raw = sl(std, p + 2, p + 2 + cch * 2);
                let u: Vec<u16> = raw.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
                name = String::from_utf16_lossy(&u);
                p += 2 + cch * 2 + 2;
                p += p & 1;
                if stk == 1 && p + 2 <= std.len() {
                    let cbu = u16at(std, p)? as usize;
                    let upx = sl(std, p + 2, p + 2 + cbu);
                    if upx.len() >= 2 {
                        outlvl = sprms(&upx[2..]).outlvl;
                    }
                }
            }
            out.push(Some((sti, base, name, outlvl)));
        }
        Ok(out)
    };
    go().unwrap_or_default()
}

// 스타일 → (종류, 수준) — 기반 스타일을 최대 10단계 따라간다
fn style_kind(styles: &[Style], istd: Option<u16>) -> (Option<&'static str>, i64) {
    use once_cell::sync::Lazy;
    use regex::Regex;
    static HEADING_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:heading|제목|개요)\s*(\d)$").unwrap());
    static TOC_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:toc|목차)\s*\d*$|^toc heading$").unwrap());
    static CAPTION_NAME: Lazy<Regex> = Lazy::new(|| Regex::new(r"(?i)^(?:caption|캡션)$").unwrap());
    let mut istd = istd;
    for _ in 0..10 {
        let i = match istd {
            Some(i) if (i as usize) < styles.len() => i,
            _ => return (None, 0),
        };
        let (sti, base, name, outlvl) = match &styles[i as usize] {
            Some(s) => s.clone(),
            None => return (None, 0),
        };
        let nm = pyutil::strip(&name);
        if (1..=9).contains(&sti) {
            return (Some("heading"), sti as i64);
        }
        if (19..=27).contains(&sti) || TOC_NAME.is_match(nm) {
            return (Some("toc"), 0);
        }
        if sti == 34 || CAPTION_NAME.is_match(nm) {
            return (Some("caption"), 0);
        }
        if sti == 62 {
            return (Some("heading"), 1);
        }
        if let Some(m) = HEADING_NAME.captures(nm) {
            return (Some("heading"), m.get(1).unwrap().as_str().parse::<i64>().unwrap_or(1).max(1));
        }
        if let Some(o) = outlvl {
            if o < 9 {
                return (Some("heading"), o as i64 + 1);
            }
        }
        if base == 0x0FFF || base == i {
            return (None, 0);
        }
        istd = Some(base);
    }
    (None, 0)
}

// 문단 훑개
struct Walker<'a> {
    text: &'a [char],
    pieces: &'a [Piece],
    papx: &'a [(u32, u32, Props)],
    styles: &'a [Style],
}

impl<'a> Walker<'a> {
    fn new(text: &'a [char], pieces: &'a [Piece], papx: &'a [(u32, u32, Props)], styles: &'a [Style]) -> Self {
        Walker { text, pieces, papx, styles }
    }

    // 글자 위치(cp) → 그 문단의 속성
    fn props(&self, cp: usize) -> Props {
        let k = self.pieces.partition_point(|p| p.0 as usize <= cp) as i64 - 1;
        if k < 0 {
            return Props { istd: Some(0), ..Default::default() };
        }
        let (cp0, _, fc0, comp, _) = &self.pieces[k as usize];
        let fc = (*fc0 as i64 + (cp as i64 - *cp0 as i64) * if *comp { 1 } else { 2 }) as u32;
        let j = self.papx.partition_point(|r| r.0 <= fc) as i64 - 1;
        if j >= 0 && fc < self.papx[j as usize].1 {
            return self.papx[j as usize].2.clone();
        }
        Props { istd: Some(0), ..Default::default() }
    }

    // 구간 → (문단 글자, 끝 표시, 끝 위치) 목록. 필드 명령문·제어 글자는 뺀다
    fn paragraphs(&self, span: (usize, usize)) -> Result<Vec<(String, char, usize)>, String> {
        let mut out = vec![];
        let mut buf = String::new();
        let mut fields: Vec<bool> = vec![];
        let (a, b) = span;
        for cp in a..b {
            let ch = *self.text.get(cp).ok_or("IndexError: string index out of range")?;
            match ch {
                '\x13' => { fields.push(true); continue; }
                '\x14' => { if let Some(l) = fields.last_mut() { *l = false; } continue; }
                '\x15' => { fields.pop(); continue; }
                '\r' | '\x07' | '\x0c' => {
                    out.push((std::mem::take(&mut buf), ch, cp));
                    continue;
                }
                _ => {}
            }
            if fields.iter().any(|f| *f) {
                continue;
            }
            if matches!(ch, '\x01' | '\x02' | '\x03' | '\x04' | '\x05' | '\x08' | '\x1f') {
                continue;
            }
            match ch {
                '\x0b' => buf.push('\n'),
                '\x1e' => buf.push('-'),
                c => buf.push(c),
            }
        }
        if !buf.is_empty() {
            out.push((buf, '\r', b.saturating_sub(1)));
        }
        Ok(out)
    }

    // 본문 — 문단·표
    fn main(&self, span: (usize, usize), out: &mut Vec<Block>) -> Result<(), String> {
        let mut rows: Vec<Vec<String>> = vec![];
        let mut cells: Vec<String> = vec![];
        let mut cell_buf: Vec<String> = vec![];
        let mut head_rows = 0usize;
        let mut counting = true;
        let flush_table = |rows: &mut Vec<Vec<String>>, cells: &mut Vec<String>, cell_buf: &mut Vec<String>,
                           head_rows: &mut usize, counting: &mut bool, out: &mut Vec<Block>| {
            if !cells.is_empty() || !cell_buf.is_empty() {
                let mut r = std::mem::take(cells);
                if !cell_buf.is_empty() {
                    r.push(cell_buf.join("\n"));
                }
                rows.push(r);
            }
            if !rows.is_empty() {
                let width = rows.iter().map(|r| r.len()).max().unwrap_or(0);
                let grid: Vec<Vec<Option<String>>> = rows.iter().map(|r| {
                    let mut v: Vec<Option<String>> = r.iter().map(|c| Some(c.clone())).collect();
                    v.resize(width, Some(String::new()));
                    v
                }).collect();
                let fin = tables::finish(&grid);
                if !fin.is_empty() {
                    out.push(Block::table(fin, *head_rows));
                }
            }
            rows.clear();
            cells.clear();
            cell_buf.clear();
            *head_rows = 0;
            *counting = true;
        };
        for (txt, mark, cp) in self.paragraphs(span)? {
            let pr = self.props(cp);
            let itap = pr.itap.unwrap_or(if pr.in_table == Some(true) { 1 } else { 0 });
            if itap >= 1 || pr.in_table == Some(true) {
                if itap > 1 {
                    if !pyutil::strip(&txt).is_empty() {
                        cell_buf.push(txt);
                    }
                    continue;
                }
                if mark == '\x07' && pr.ttp == Some(true) {
                    rows.push(std::mem::take(&mut cells));
                    if counting && pr.tbl_header == Some(true) {
                        head_rows += 1;
                    } else {
                        counting = false;
                    }
                    continue;
                }
                cell_buf.push(txt);
                if mark == '\x07' {
                    cells.push(cell_buf.iter().filter(|s| !pyutil::strip(s).is_empty()).cloned().collect::<Vec<_>>().join("\n"));
                    cell_buf.clear();
                }
                continue;
            }
            flush_table(&mut rows, &mut cells, &mut cell_buf, &mut head_rows, &mut counting, out);
            if pyutil::strip(&txt).is_empty() {
                continue;
            }
            out.push(self.block(txt, &pr));
        }
        flush_table(&mut rows, &mut cells, &mut cell_buf, &mut head_rows, &mut counting, out);
        Ok(())
    }

    fn block(&self, txt: String, pr: &Props) -> Block {
        let (mut kind, mut level) = style_kind(self.styles, pr.istd);
        if let Some(o) = pr.outlvl {
            if o < 9 {
                kind = Some("heading");
                level = o as i64 + 1;
            }
        }
        match kind {
            Some("toc") => Block::new(B::TOC, txt),
            Some("heading") => Block::new(B::HEADING, txt).lv(level),
            Some("caption") => Block::new(B::CAPTION, txt),
            _ if pr.ilfo.unwrap_or(0) != 0 => Block::new(B::LIST_ITEM, txt).lv(pr.ilvl.unwrap_or(0) as i64 + 1),
            _ => Block::new(B::PARAGRAPH, txt),
        }
    }

    // 평문 줄(머리말·각주 등) — 표 칸 끝(\x07)은 같은 줄로 잇는다
    fn plain(&self, span: (usize, usize)) -> Result<Vec<String>, String> {
        let mut out = vec![];
        let mut cur: Vec<String> = vec![];
        for (txt, mark, _) in self.paragraphs(span)? {
            let t = pyutil::strip(&txt);
            if !t.is_empty() {
                cur.push(t.to_string());
            }
            if mark != '\x07' && !cur.is_empty() {
                out.push(cur.join(" "));
                cur.clear();
            }
        }
        if !cur.is_empty() {
            out.push(cur.join(" "));
        }
        Ok(out)
    }
}
