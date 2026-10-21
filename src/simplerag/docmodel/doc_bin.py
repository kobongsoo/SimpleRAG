#------------------------------------------------------------------
# DOC(Word 97-2003 바이너리) → 블록 (재설계서 §8 — (d) 블록화 + (a) 스타일 번호 스파이크)
#=> 기존 추출기(MpowerClassify office_legacy.DocExtractor)는 piece table 로 글자만 이어 붙이고
#   셀 구분자 0x07 을 탭으로 바꿨다. 여기서는 공개 명세 [MS-DOC] 를 따라 문단마다 속성(PAPX)을
#   읽어 블록을 만든다.
#    · 글자     — piece table(CLX) 을 따라 압축(cp949)/비압축(UTF-16LE) 조각을 잇는다(이식)
#    · 영역     — FIB 의 글자 수(ccpText·ccpFtn·ccpHdd …)로 본문·각주·머리말·글상자를 나눈다
#    · 문단     — 문단 끝 표시(\r · 셀 끝 0x07 · 쪽나눔 0x0C)마다 PAPX(FKP)에서 속성을 찾는다
#                 istd(스타일 번호) · 표 안 여부(sprmPFInTable/sprmPItap) · 행 끝(sprmPFTtp)
#                 · 목록(sprmPIlfo/sprmPIlvl) · 개요 수준(sprmPOutLvl) · 머리글 행(sprmTTableHeader)
#    · 스타일   — STSH 에서 스타일 번호 → 내장 번호(sti: 1~9 = 제목 1~9, 19~27 = 목차)·이름·개요 수준
#    · 필드     — 0x13 명령 0x14 결과 0x15 중 **명령은 버리고 결과만** 남긴다. 기존 추출은 명령문
#                 (TOC \o "1-3" …)까지 본문에 남겨 표 머리글로 잡히는 사고가 있었다(REPORT §35)
#   PAPX 를 못 읽으면 ValueError — 상위(extract_document)가 기존 추출(legacy)로 폴백한다.
#   한계: 세로 병합 셀(TDefTable) 정보는 아직 읽지 않는다 — 이어진 칸은 빈칸으로 남는다.
#------------------------------------------------------------------

import struct

from . import blocks as B
from . import tables

try:
    import olefile
except ImportError:          # pragma: no cover — requirements 에 있다
    olefile = None

# FIB 안 위치 (Word 97 FibRgLw97 · FibRgFcLcb97)
_CCP_OFF = 0x4C              # ccpText 부터 4바이트씩: Text, Ftn, Hdd, Mcr, Atn, Edn, Txbx, HdrTxbx
_FC_STSHF = 0xA2             # fcStshf / lcbStshf
_FC_PLCF_BTE_PAPX = 0x102    # fcPlcfBtePapx / lcbPlcfBtePapx
_FC_CLX = 0x1A2              # fcClx / lcbClx

# 문단 속성 sprm
_SPRM_IN_TABLE = 0x2416
_SPRM_TTP = 0x2417
_SPRM_ITAP = 0x6649
_SPRM_INNER_TTP = 0x244C
_SPRM_ILVL = 0x260A
_SPRM_ILFO = 0x460B
_SPRM_OUTLVL = 0x2640
_SPRM_TABLE_HEADER = 0x3404

# 문단을 끝내는 글자 — 문단 끝 · 셀/행 끝 · 쪽/구역 나눔
_PARA_END = ("\r", "\x07", "\x0c")
# 본문에서 지울 제어 글자(그림·각주 참조 표시 등) / 바꿀 글자
_DROP = {"\x01", "\x02", "\x03", "\x04", "\x05", "\x08", "\x1f"}
_SUBST = {"\x0b": "\n", "\x1e": "-", "\x09": "\t"}


#------------------------------------------------------------------
# DOC 추출 (진입점)
#
# -in: path  = .doc 경로
# -in: title = 문서 제목
#
# -out: Document (블록 경로, 후처리 전)
# -out: error = OLE 아님·구조 이상·PAPX 없음이면 ValueError (상위가 legacy 로 폴백)
#------------------------------------------------------------------
def extract(path, title=""):
    if olefile is None:
        raise ValueError("olefile 미설치")
    try:
        ole = olefile.OleFileIO(path)
    except Exception as e:      # noqa: BLE001 — OLE 가 아니면 폴백
        raise ValueError("doc 열기 실패: {}".format(type(e).__name__))
    try:
        wd = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if flags & 0x0100:
            raise ValueError("암호화된 doc")
        tname = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(tname):
            tname = "1Table" if ole.exists("1Table") else "0Table"
        tbl = ole.openstream(tname).read()
    finally:
        ole.close()

    pieces = _pieces(wd, tbl)
    text = "".join(p[4] for p in pieces)
    papx = _papx_runs(wd, tbl)
    if not papx:
        raise ValueError("doc 문단 속성(PAPX) 없음")
    styles = _styles(tbl, wd)

    doc = B.Document(format="doc", title=title)
    ccp = struct.unpack_from("<8I", wd, _CCP_OFF)
    stories = _stories(ccp, len(text))
    w = _Walker(text, pieces, papx, styles)
    w.main(stories["text"], doc.blocks)
    # 머리말·꼬리말은 표시만 하고 청크에서 뺀다(Word 는 둘을 한 영역에 둔다 — 모두 header 로 표시)
    for t in w.plain(stories["hdd"]) + w.plain(stories["hdrtxbx"]):
        doc.blocks.append(B.Block(B.HEADER, t))
    # 글상자 본문은 본문 문단으로 뒤에 붙인다(기존 추출도 같은 글자를 담는다)
    for t in w.plain(stories["txbx"]):
        doc.blocks.append(B.Block(B.PARAGRAPH, t))
    notes = w.plain(stories["ftn"]) + w.plain(stories["edn"]) + w.plain(stories["atn"])
    if notes:
        doc.blocks.append(B.Block(B.HEADING, "각주", level=1, synthetic=True))
        doc.blocks.extend(B.Block(B.NOTE, t) for t in notes)
    return doc


#------------------------------------------------------------------
# 영역(story) 나누기
#=> 모든 영역의 글자는 한 줄로 이어져 있고 FIB 가 영역마다 글자 수를 알려 준다.
#   차례: 본문 · 각주 · 머리말 · 매크로 · 메모 · 미주 · 글상자 · 머리말 글상자.
#
# -in: ccp   = FIB 의 글자 수 8개
# -in: total = 이어 붙인 전체 글자 수
#
# -out: dict = {이름: (시작 CP, 끝 CP)}
# -out: error = 없음 (범위를 넘으면 전체 길이로 자른다)
#------------------------------------------------------------------
def _stories(ccp, total):
    names = ("text", "ftn", "hdd", "mcr", "atn", "edn", "txbx", "hdrtxbx")
    out, pos = {}, 0
    for name, n in zip(names, ccp):
        out[name] = (min(pos, total), min(pos + n, total))
        pos += n
    return out


#------------------------------------------------------------------
# piece table → 조각 목록
#=> CLX 의 Pcdt 를 찾아 조각마다 (시작 CP, 끝 CP, FC 기준, 압축 여부, 글자)를 만든다.
#   압축 조각은 1바이트/글자(cp949 근사), 비압축은 UTF-16LE 다(MpowerClassify DocExtractor 이식).
#
# -in: wd  = WordDocument 스트림 바이트
# -in: tbl = Table 스트림 바이트
#
# -out: list[(cp0, cp1, fc0, compressed, text)]
# -out: error = piece table 이 없으면 ValueError
#------------------------------------------------------------------
def _pieces(wd, tbl):
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, _FC_CLX)
    clx = tbl[fc_clx: fc_clx + lcb_clx]
    pcd = b""
    i = 0
    while i < len(clx):
        t = clx[i]
        if t == 0x01:            # Prc — 서식 묶음, 건너뛴다
            if i + 3 > len(clx):
                break
            i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
        elif t == 0x02:          # Pcdt — 여기에 piece 목록이 있다
            lcb = struct.unpack_from("<I", clx, i + 1)[0]
            pcd = clx[i + 5: i + 5 + lcb]
            break
        else:
            break
    n = (len(pcd) - 4) // 12
    if n <= 0:
        raise ValueError("doc piece table 없음")
    cps = struct.unpack_from("<%dI" % (n + 1), pcd, 0)
    base = (n + 1) * 4
    out = []
    for k in range(n):
        fc = struct.unpack_from("<I", pcd, base + k * 8 + 2)[0]
        cnt = cps[k + 1] - cps[k]
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            # 압축 조각은 명세상 1바이트 = 1글자(cp1252)다. 한글은 비압축(UTF-16) 조각에 들어간다.
            # 글자 수가 CP 수와 정확히 같아야 문단 끝 표시의 위치(CP→FC)가 맞는다.
            raw = wd[off: off + cnt]
            txt = "".join(chr(x) if x < 0x80 else bytes([x]).decode("cp1252", "replace") for x in raw)
            out.append((cps[k], cps[k + 1], off, True, txt))
        else:
            out.append((cps[k], cps[k + 1], fc, False, wd[fc: fc + cnt * 2].decode("utf-16-le", "replace")))
    return out


#------------------------------------------------------------------
# PAPX 실행 목록 읽기
#=> PlcBtePapx(Table 스트림)가 가리키는 FKP 페이지(512바이트, WordDocument 스트림)마다
#   (FC 시작, FC 끝, 속성) 실행을 모은다. 문단 끝 표시의 FC 로 그 문단의 속성을 찾는다.
#
# -in: wd, tbl = 스트림 바이트
#
# -out: list[(fc0, fc1, props dict)] FC 순 정렬
# -out: error = 없음 (구조가 이상하면 빈 목록 — 상위가 폴백)
#------------------------------------------------------------------
def _papx_runs(wd, tbl):
    fc, lcb = struct.unpack_from("<II", wd, _FC_PLCF_BTE_PAPX)
    plc = tbl[fc: fc + lcb]
    n = (len(plc) - 4) // 8
    if n <= 0:
        return []
    pns = struct.unpack_from("<%dI" % n, plc, (n + 1) * 4)
    runs = []
    for pn in pns:
        page = wd[(pn & 0x3FFFFF) * 512: (pn & 0x3FFFFF) * 512 + 512]
        if len(page) < 512:
            continue
        crun = page[511]
        fcs = struct.unpack_from("<%dI" % (crun + 1), page, 0)
        for j in range(crun):
            boff = page[(crun + 1) * 4 + j * 13] * 2
            props = _parse_papx(page, boff) if boff else {"istd": 0}
            runs.append((fcs[j], fcs[j + 1], props))
    runs.sort(key=lambda r: r[0])
    return runs


#------------------------------------------------------------------
# FKP 안 PapxInFkp 하나 해석
#=> 첫 바이트 cb 가 0 이 아니면 길이 2·cb−1, 0 이면 다음 바이트 cb' 로 길이 2·cb'.
#   내용은 istd(2바이트) + sprm 나열이다.
#
# -in: page = FKP 512바이트
# -in: off  = PapxInFkp 시작 위치
#
# -out: dict = {istd, in_table, ttp, itap, ilvl, ilfo, outlvl, tbl_header}
# -out: error = 없음 (잘린 데이터는 읽은 데까지)
#------------------------------------------------------------------
def _parse_papx(page, off):
    cb = page[off]
    if cb:
        start, size = off + 1, 2 * cb - 1
    else:
        start, size = off + 2, 2 * page[off + 1]
    data = page[start: start + size]
    props = {"istd": struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0}
    props.update(_sprms(data[2:]))
    return props


#------------------------------------------------------------------
# sprm 나열 해석 (필요한 것만 뽑는다)
#=> sprm 은 2바이트 번호 + 피연산자다. 피연산자 길이는 번호의 spra(위 3비트)로 정해지고,
#   spra 6 은 가변 길이(첫 바이트가 길이, sprmTDefTable 은 2바이트 길이)다.
#
# -in: data = grpprl 바이트
#
# -out: dict = 알아본 속성들
# -out: error = 없음
#------------------------------------------------------------------
def _sprms(data):
    out = {}
    i = 0
    while i + 2 <= len(data):
        sprm = struct.unpack_from("<H", data, i)[0]
        i += 2
        spra = sprm >> 13
        if spra in (0, 1):
            size = 1
        elif spra in (2, 4, 5):
            size = 2
        elif spra == 3:
            size = 4
        elif spra == 7:
            size = 3
        else:
            if sprm == 0xD608 and i + 2 <= len(data):        # sprmTDefTable
                size = struct.unpack_from("<H", data, i)[0] + 1
            elif i < len(data):
                size = data[i] + 1
            else:
                break
        val = data[i: i + size]
        i += size
        if not val:
            break
        if sprm == _SPRM_IN_TABLE:
            out["in_table"] = val[0] != 0
        elif sprm == _SPRM_TTP:
            out["ttp"] = val[0] != 0
        elif sprm == _SPRM_INNER_TTP:
            out["inner_ttp"] = val[0] != 0
        elif sprm == _SPRM_ITAP and len(val) == 4:
            out["itap"] = struct.unpack("<i", val)[0]
        elif sprm == _SPRM_ILVL:
            out["ilvl"] = val[0]
        elif sprm == _SPRM_ILFO and len(val) == 2:
            out["ilfo"] = struct.unpack("<H", val)[0]
        elif sprm == _SPRM_OUTLVL:
            out["outlvl"] = val[0]
        elif sprm == _SPRM_TABLE_HEADER:
            out["tbl_header"] = val[0] != 0
    return out


#------------------------------------------------------------------
# 스타일 시트(STSH) 읽기
#=> 스타일마다 (내장 번호 sti, 기반 스타일 istdBase, 이름, 개요 수준)을 모은다.
#   sti 는 언어와 무관한 내장 번호라 한글 Word 의 '제목 1' 도 sti 1 이다 — 이름보다 믿을 만하다.
#
# -in: tbl = Table 스트림
# -in: wd  = WordDocument 스트림 (fcStshf 위치를 읽는다)
#
# -out: list[(sti, istdBase, name, outlvl)] — 빈 칸은 None
# -out: error = 없음 (못 읽으면 빈 목록 — 제목은 정규식 폴백에 맡긴다)
#------------------------------------------------------------------
def _styles(tbl, wd):
    try:
        fc, lcb = struct.unpack_from("<II", wd, _FC_STSHF)
        st = tbl[fc: fc + lcb]
        cb_stshi = struct.unpack_from("<H", st, 0)[0]
        cstd, cb_base = struct.unpack_from("<HH", st, 2)
        pos = 2 + cb_stshi
        out = []
        for _ in range(cstd):
            cb = struct.unpack_from("<H", st, pos)[0]
            std = st[pos + 2: pos + 2 + cb]
            pos += 2 + cb
            if not cb or len(std) < 10:
                out.append(None)
                continue
            w0, w1, w2 = struct.unpack_from("<HHH", std, 0)
            sti, stk, base = w0 & 0x0FFF, w1 & 0x000F, w1 >> 4
            name, outlvl = "", None
            p = cb_base
            if p + 2 <= len(std):
                cch = struct.unpack_from("<H", std, p)[0]
                name = std[p + 2: p + 2 + cch * 2].decode("utf-16-le", "replace")
                p += 2 + cch * 2 + 2
                p += p & 1
                # 문단 스타일의 첫 UPX 가 문단 속성(istd + grpprl)이다
                if stk == 1 and p + 2 <= len(std):
                    cbu = struct.unpack_from("<H", std, p)[0]
                    upx = std[p + 2: p + 2 + cbu]
                    outlvl = _sprms(upx[2:]).get("outlvl") if len(upx) >= 2 else None
            out.append((sti, base, name, outlvl))
        return out
    except struct.error:
        return []


#------------------------------------------------------------------
# 스타일 번호 → (종류, 수준)
#=> 기반 스타일(istdBase) 사슬을 따라 올라가며 처음 나오는 단서를 쓴다(최대 10단계).
#    sti 1~9 → 제목 1~9 · sti 19~27 → 목차 · sti 34 → 캡션 · sti 62 → 제목(Title) 수준 1
#    이름이 제목 N/개요 N/Heading N 이거나, 스타일의 개요 수준이 0~8 이면 제목
#
# -in: styles = _styles 결과
# -in: istd   = 문단 스타일 번호
#
# -out: (kind, level) — kind ∈ {"heading", "toc", "caption", None}
# -out: error = 없음
#------------------------------------------------------------------
def _style_kind(styles, istd):
    from .ooxml_docx import _CAPTION_NAME, _HEADING_NAME, _TOC_NAME

    for _ in range(10):
        if istd is None or istd >= len(styles) or styles[istd] is None:
            return None, 0
        sti, base, name, outlvl = styles[istd]
        if 1 <= sti <= 9:
            return "heading", sti
        if 19 <= sti <= 27 or _TOC_NAME.match(name.strip()):
            return "toc", 0
        if sti == 34 or _CAPTION_NAME.match(name.strip()):
            return "caption", 0
        if sti == 62:
            return "heading", 1
        m = _HEADING_NAME.match(name.strip())
        if m:
            return "heading", max(1, int(m.group(1)))
        if outlvl is not None and outlvl < 9:
            return "heading", outlvl + 1
        if base == 0x0FFF or base == istd:
            return None, 0
        istd = base
    return None, 0


class _Walker:
    #------------------------------------------------------------------
    # 문단 순회기 준비
    #
    # -in: text   = 모든 영역을 이은 글자(CP 순)
    # -in: pieces = _pieces 결과 (CP → FC 변환용)
    # -in: papx   = _papx_runs 결과
    # -in: styles = _styles 결과
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, text, pieces, papx, styles):
        import bisect
        self._bisect = bisect
        self.text = text
        self.pieces = pieces
        self.piece_starts = [p[0] for p in pieces]
        self.papx = papx
        self.papx_starts = [r[0] for r in papx]
        self.styles = styles

    #------------------------------------------------------------------
    # CP 의 문단 속성
    #=> CP → (어느 piece) → FC → (어느 PAPX 실행) 순으로 찾는다.
    #
    # -in: cp = 문단 끝 표시의 CP
    #
    # -out: dict 속성 (못 찾으면 {"istd": 0})
    # -out: error = 없음
    #------------------------------------------------------------------
    def props(self, cp):
        k = self._bisect.bisect_right(self.piece_starts, cp) - 1
        if k < 0:
            return {"istd": 0}
        cp0, _, fc0, comp, _ = self.pieces[k]
        fc = fc0 + (cp - cp0) * (1 if comp else 2)
        j = self._bisect.bisect_right(self.papx_starts, fc) - 1
        if j >= 0 and fc < self.papx[j][1]:
            return self.papx[j][2]
        return {"istd": 0}

    #------------------------------------------------------------------
    # 영역 하나를 문단 목록으로 (필드 명령 제거)
    #=> 글자를 차례로 보며 문단 끝 표시에서 끊는다. 필드는 0x13(시작) 0x14(구분) 0x15(끝)로
    #   감싸이는데, 시작~구분 사이(명령)는 버리고 구분~끝 사이(결과)만 남긴다. 필드는 겹칠 수 있다.
    #
    # -in: span = (시작 CP, 끝 CP)
    #
    # -out: list[(글자, 끝 표시 글자, 끝 CP)]
    # -out: error = 없음
    #------------------------------------------------------------------
    def paragraphs(self, span):
        out, buf = [], []
        fields = []          # 열린 필드마다 '명령 구간인가'
        a, b = span
        for cp in range(a, b):
            ch = self.text[cp]
            if ch == "\x13":
                fields.append(True)
                continue
            if ch == "\x14":
                if fields:
                    fields[-1] = False
                continue
            if ch == "\x15":
                if fields:
                    fields.pop()
                continue
            if ch in _PARA_END:
                out.append(("".join(buf), ch, cp))
                buf = []
                continue
            if any(fields):
                continue            # 필드 명령문 — 화면에 보이지 않는 글자
            if ch in _DROP:
                continue
            buf.append(_SUBST.get(ch, ch))
        if buf:
            out.append(("".join(buf), "\r", b - 1))
        return out

    #------------------------------------------------------------------
    # 본문 영역 → 블록 (핵심)
    #=> 문단마다 속성을 보고 표·제목·목록·본문 블록을 만든다.
    #    · 표 안 문단 — 셀 끝(0x07)까지 모아 칸 하나, 행 끝(TTP)에서 행 하나, 표 밖 문단이 오면 표 블록
    #    · 안쪽 표(itap>1)는 바깥 칸 글자로 이어 붙인다
    #    · 제목 — 문단 개요 수준 > 스타일(sti·이름·개요 수준)
    #    · 목록 — ilfo > 0
    #
    # -in: span = 본문 (시작 CP, 끝 CP)
    # -in: out  = 블록 리스트(제자리)
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def main(self, span, out):
        rows, cells, cell_buf = [], [], []
        head_rows, counting = 0, True

        # 모아 둔 표를 블록으로 내보낸다
        def flush_table():
            nonlocal rows, cells, cell_buf, head_rows, counting
            if cells or cell_buf:
                rows.append(cells + (["\n".join(cell_buf)] if cell_buf else []))
            if rows:
                width = max(len(r) for r in rows)
                grid = [r + [""] * (width - len(r)) for r in rows]
                fin = tables.finish(grid)
                if fin:
                    out.append(B.Block(B.TABLE, rows=fin, header_rows=head_rows))
            rows, cells, cell_buf, head_rows, counting = [], [], [], 0, True

        for txt, mark, cp in self.paragraphs(span):
            pr = self.props(cp)
            itap = pr.get("itap", 1 if pr.get("in_table") else 0)
            if itap >= 1 or pr.get("in_table"):
                if itap > 1:
                    # 안쪽 표 — 칸 구분 없이 바깥 칸의 글자로 잇는다
                    if txt.strip():
                        cell_buf.append(txt)
                    continue
                if mark == "\x07" and pr.get("ttp"):
                    # 행 끝 표시 — 지금까지의 칸들이 한 행
                    rows.append(cells)
                    if counting and pr.get("tbl_header"):
                        head_rows += 1
                    else:
                        counting = False
                    cells = []
                    continue
                cell_buf.append(txt)
                if mark == "\x07":
                    cells.append("\n".join(s for s in cell_buf if s.strip()))
                    cell_buf = []
                continue
            flush_table()
            if not txt.strip():
                continue
            out.append(self._block(txt, pr))
        flush_table()

    #------------------------------------------------------------------
    # 표 밖 문단 하나 → 블록
    #
    # -in: txt = 문단 글자
    # -in: pr  = 문단 속성
    #
    # -out: Block
    # -out: error = 없음
    #------------------------------------------------------------------
    def _block(self, txt, pr):
        kind, level = _style_kind(self.styles, pr.get("istd"))
        outlvl = pr.get("outlvl")
        if outlvl is not None and outlvl < 9:
            kind, level = "heading", outlvl + 1
        if kind == "toc":
            return B.Block(B.TOC, txt)
        if kind == "heading":
            return B.Block(B.HEADING, txt, level=level)
        if kind == "caption":
            return B.Block(B.CAPTION, txt)
        if pr.get("ilfo"):
            return B.Block(B.LIST_ITEM, txt, level=pr.get("ilvl", 0) + 1)
        return B.Block(B.PARAGRAPH, txt)

    #------------------------------------------------------------------
    # 본문 밖 영역(각주·머리말·글상자) → 글자 목록
    #=> 구조를 따지지 않고 문단 글자만 모은다. 표 칸 구분은 공백으로 잇는다.
    #
    # -in: span = (시작 CP, 끝 CP)
    #
    # -out: list[str]
    # -out: error = 없음
    #------------------------------------------------------------------
    def plain(self, span):
        out, cur = [], []
        for txt, mark, _ in self.paragraphs(span):
            if txt.strip():
                cur.append(txt.strip())
            if mark != "\x07" and cur:
                out.append(" ".join(cur))
                cur = []
        if cur:
            out.append(" ".join(cur))
        return out
