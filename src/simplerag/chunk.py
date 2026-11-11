#------------------------------------------------------------------
# 토큰 기준 청킹
#=> 문서 텍스트를 임베딩 모델 토큰 수 기준으로 잘라 청크 문자열을 만든다.
#   문자 길이로 자르면 한국어는 토큰 수 편차가 커서 prefill 예산이 흔들린다.
#
#   ⚠️ 이 모듈을 쓰기 전에 토크나이저의 절단이 반드시 해제돼 있어야 한다.
#   e5 의 tokenizer.json 에는 max_length=512 절단 설정이 박혀 있어서,
#   해제하지 않고 encode 하면 긴 문서의 앞 512토큰만 남고 나머지가 조용히
#   사라진다(취업규칙 11,830토큰 → 512토큰, 96% 유실). 설계서 결정12.
#   → OnnxEmbedder.ensure_loaded() 가 no_truncation() 을 호출한다.
#------------------------------------------------------------------

import re

from . import config


#------------------------------------------------------------------
# 토큰 id 목록을 겹침 포함해 자르기
#=> start 를 step 만큼 밀며 창을 뜬다. 마지막 창이 끝에 닿으면 멈춘다.
#
# -in: ids        = 문서 전체 토큰 id
# -in: size       = 청크 1개의 토큰 수
# -in: overlap    = 인접 청크가 겹치는 토큰 수
#
# -out: [[id,...], ...] = 청크별 토큰 id 목록
#------------------------------------------------------------------
def chunk_ids(ids, size, overlap):
    if not ids:
        return []

    # overlap 이 size 이상이면 start 가 전진하지 못해 무한 루프가 된다.
    step = max(1, size - min(overlap, size - 1))

    out = []
    for start in range(0, len(ids), step):
        piece = ids[start:start + size]
        # 꼬리 조각은 의미가 거의 없어 버린다(검색 노이즈만 늘린다).
        if len(piece) >= config.MIN_CHUNK_TOKENS:
            out.append(piece)
        if start + size >= len(ids):
            break
    return out


#------------------------------------------------------------------
# 문서 텍스트 -> 청크 문자열 목록 (핵심)
#=> 토큰으로 잘라낸 뒤 다시 텍스트로 디코드한다. 임베딩 단계에서 프리픽스와
#   특수토큰을 다시 붙이므로 여기서는 순수 본문만 만든다.
#
# -in: text    = 추출된 문서 텍스트
# -in: tok     = tokenizers.Tokenizer (절단 해제된 상태여야 함)
# -in: size    = 청크 토큰 수 (None 이면 설정값)
# -in: overlap = 겹침 토큰 수 (None 이면 설정값)
#
# -out: chunks = 청크 텍스트 리스트(내용이 없으면 [])
#------------------------------------------------------------------
def split_text(text, tok, size=None, overlap=None):
    if not text or not text.strip():
        return []

    size = size or config.CHUNK_TOKENS
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap

    ids = tok.encode(text, add_special_tokens=False).ids
    return [tok.decode(piece) for piece in chunk_ids(ids, size, overlap)]


#------------------------------------------------------------------
# 토큰 길이 (짧은 캐시 포함)
#=> 패킹은 같은 조각의 길이를 여러 번 묻는다. 인코딩이 비싸므로 캐시한다.
#------------------------------------------------------------------
def _tlen(tok, s, _cache={}):
    key = (id(tok), s)
    n = _cache.get(key)
    if n is None:
        n = len(tok.encode(s, add_special_tokens=False).ids)
        if len(_cache) > 20000:
            _cache.clear()
        _cache[key] = n
    return n


#------------------------------------------------------------------
# 큰 절을 내부 분할
#=> 절 하나가 예산을 넘으면 그 절만 따로 쪼갠다. 문단 → 줄 → 토큰 순으로
#   물러선다. **이때만** 겹침을 준다 — 의미가 이어지는 곳을 끊기 때문이다.
#   절 경계에서 끊을 때는 겹침이 필요 없다.
#
# -in: body, tok, budget, overlap
#
# -out: [조각 문자열, ...]
#------------------------------------------------------------------
def _split_oversized(body, tok, budget, overlap):
    units = [u for u in body.split("\n") if u.strip()]
    out, cur, cur_n = [], [], 0

    for u in units:
        n = _tlen(tok, u)
        if n > budget:                    # 한 줄이 예산을 넘으면 토큰으로 자른다
            if cur:
                out.append("\n".join(cur))
                cur, cur_n = [], 0
            ids = tok.encode(u, add_special_tokens=False).ids
            out += [tok.decode(p) for p in chunk_ids(ids, budget, overlap)]
            continue
        if cur_n + n > budget:
            out.append("\n".join(cur))
            cur, cur_n = [], 0
        cur.append(u)
        cur_n += n

    if cur:
        out.append("\n".join(cur))
    return out


#------------------------------------------------------------------
# 구조 인식 청킹 (핵심 — 설계서 결정3 재설계)
#=> 절 경계를 지키면서 예산이 허락하는 만큼 작은 절들을 묶는다.
#
#   왜 '절 = 청크' 가 아닌가: 실측한 절 413개의 길이 중앙값이 **36토큰**이다.
#   그대로 청크로 쓰면 변별력 없는 조각이 쏟아진다. 대신 85%가 128토큰 안에
#   들어가므로, **크기를 키우지 않고도** 대부분의 절을 온전히 담을 수 있다.
#
#   규칙
#     절 ≤ 남은 예산      → 현재 청크에 이어 붙인다
#     절 > 남은 예산      → 청크를 끊고 새 청크에서 시작 (겹침 없음)
#     절 > 예산 (15%)     → 그 절만 내부 분할 (이때만 겹침)
#   표 행이 든 청크에는 머리글 줄을 함께 넣는다 — 단 머리글이
#   config.TABLE_HEADER_MAX_TOKENS 이하일 때만(§35, 비정상 머리글 재부착 버그).
#
# -in: text    = 추출된 문서 텍스트
# -in: tok     = tokenizers.Tokenizer (절단 해제 상태)
# -in: title   = 문서 제목(접두에 쓴다). None 이면 접두를 붙이지 않는다
# -in: size, overlap = None 이면 설정값
#
# -out: chunks = 청크 텍스트 리스트
#------------------------------------------------------------------
def split_structured(text, tok, title=None, size=None, overlap=None):
    from .structure import make_prefix, split_sections, table_headers

    if not text or not text.strip():
        return []

    size = size or config.CHUNK_TOKENS
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap

    out, buf, buf_n = [], [], 0

    def flush():
        # 모아 둔 작은 절들을 청크 하나로 내보낸다.
        if buf:
            out.append("\n\n".join(buf).strip())
            buf.clear()

    for section, body in split_sections(text):
        prefix = make_prefix(title, section) if (title or section) else ""
        lines = body.split("\n")
        heads = table_headers(lines)

        whole = (prefix + body).strip()
        n = _tlen(tok, whole)

        # (A) 절이 통째로 예산에 들어간다 — 앞의 절들과 묶을 수 있으면 묶는다.
        if n <= size:
            if buf_n + n > size:
                flush()
                buf_n = 0
            buf.append(whole)
            buf_n += n
            continue

        # (B) 절 하나가 예산을 넘는다 — 모아 둔 것을 먼저 내보내고 따로 쪼갠다.
        flush()
        buf_n = 0
        budget = max(config.MIN_CHUNK_TOKENS, size - _tlen(tok, prefix))
        for piece in _split_oversized(body, tok, budget, overlap):
            if not piece.strip():
                continue
            # 표 행으로 시작하는데 머리글이 없으면 되살린다(M3).
            first = piece.lstrip().split("\n", 1)[0]
            if heads and first.startswith("|"):
                hdr = next((h for i, h in heads.items()
                            if lines[i].strip() == first.strip()), None)
                # ⚠️ 머리글이 비정상적으로 길면 붙이지 않는다(REPORT §35). 머리글 판정이
                #    느슨해 Word 목차 필드 코드(2,646토큰)·설명 문장 행이 머리글로 잡혔고,
                #    그 줄이 조각마다 다시 붙어 128토큰 설계의 청크가 2,768토큰까지 커졌다.
                #    그런 청크 하나가 근거로 뽑히면 2048 컨텍스트를 넘겨 파이프라인이 죽었다.
                if (hdr and hdr not in piece
                        and _tlen(tok, hdr) <= config.TABLE_HEADER_MAX_TOKENS):
                    piece = hdr + "\n" + piece
            out.append((prefix + piece).strip())

    flush()

    # 꼬리 조각은 검색 노이즈만 늘린다. 단 접두가 길이의 대부분이면
    # 내용이 없다는 뜻이므로 그것도 버린다.
    return [c for c in out if _tlen(tok, c) >= config.MIN_CHUNK_TOKENS]


# ── 블록 기반 청킹 (plan/구조화추출_청킹_재설계서.html §11) ─────────────
# 문장 경계 — 마침표·물음표·느낌표(와 한국어 종결 '다.') 뒤 공백에서 끊는다
_SENT_END = re.compile(r"(?<=[.!?。])\s+")


#------------------------------------------------------------------
# 블록 기반 청킹 (핵심 — 재설계서 §11)
#=> 구조화 추출기(docmodel)가 낸 Document 를 청크로 자른다. 규칙은 split_structured 와
#   같고 입력만 다르다 — 정규식으로 추측한 절 대신 파일이 알려 준 절·블록·표 행을 쓴다.
#    1) 폴백 문서(raw_text)는 split_structured 를 그대로 부른다 → 지금과 글자까지 같은 청크
#    2) 절이 예산 안이면 앞 절들과 묶는다(겹침 없음)
#    3) 절이 예산을 넘으면 '단위'(블록 · 표 행) 경계에서 끊는다
#    4) 단위 하나가 예산을 넘으면 그 단위 안에서만 줄 → (문장) → 토큰 순으로 쪼갠다(이때만 겹침)
#       단 예산 × overflow_ratio 이하면 쪼개지 않고 통째로 둔다(약한 초과 허용, 기본 1.0 = 끔)
#    5) 표 행으로 시작하는 조각에는 그 표의 머리글 행을 다시 붙인다(파일이 알려 준 머리글,
#       길이 상한 TABLE_HEADER_MAX_TOKENS 는 기존과 같다)
#    6) 어떤 청크도 chunk.hard_max(e5 입력 한계)를 넘지 않게 마지막에 자른다
#    7) 제목만 든 청크를 만들지 않는다 — 제목 줄만 남는 조각은 내보내지 않고 다음 조각 맨 앞에
#       붙인다. 절 안(큰 문단 앞에서 제목이 홀로 떨어지는 경우)과 절 사이(본문 없는 상위 제목 절이
#       큰 하위 절 앞에서 홀로 내보내지는 경우) 둘 다다. 제목만 든 청크는 근거로 뽑혀도 답할
#       재료가 없고, 제목 글자는 이미 같은 절 청크마다 접두에 들어 있다.
#    8) (chunk.table_whole > 0 일 때만) 그 토큰 이하의 표는 행으로 나누지 않고 통째로 한 단위로 둔다
#
# -in: doc     = docmodel.Document
# -in: tok     = tokenizers.Tokenizer (절단 해제 상태)
# -in: title   = 문서 제목(접두에 쓴다). None 이면 doc.title
# -in: size, overlap = None 이면 설정값
#
# -out: [dict, …] — {text, section_id, path, source, page_from, page_to, page}
#       (폴백 문서는 section_id/page 가 None, source="legacy")
# -out: error = 없음 (빈 문서는 [])
#------------------------------------------------------------------
def split_blocks(doc, tok, title=None, size=None, overlap=None):
    from .docmodel.assemble import build_sections, make_prefix, section_units

    title = doc.title if title is None else title
    size = size or config.CHUNK_TOKENS
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap

    if not doc.is_blocks:
        return [{"text": c, "section_id": None, "path": [], "source": "legacy",
                 "page_from": None, "page_to": None, "page": None}
                for c in split_structured(doc.raw_text, tok, title=title,
                                          size=size, overlap=overlap)]

    out = []
    pf = _PageFinder()
    buf, buf_n, buf_secs = [], 0, []

    # 모아 둔 작은 절들을 청크 하나로 내보낸다 — 메타는 첫 절 기준, 쪽은 합친 범위
    def flush():
        if buf:
            out.append(_chunk_meta("\n\n".join(buf).strip(), buf_secs, pf))
            buf.clear()
            buf_secs.clear()

    # 모아 둔 절 끝에서 '들어오는 절의 상위 제목뿐인 절'들을 떼어 낸다 — 앞 청크 끝에 매달거나
    # 홀로 내보내지 않고 들어오는 절과 함께 보낸다. [(whole, sec), …]
    # 🔴 상위 제목인지 꼭 본다 — 번호 조항 문서는 "3.2 '회원'이라 함은 …" 처럼 제목 줄이 곧
    #    내용인 절이 줄줄이 이어진다. 이것까지 떼면 묶음이 끝없이 커진다(동호회규정에서 실측).
    def peel(nxt):
        k = len(buf_secs)
        while k and _heading_only(buf_secs[k - 1]) and _is_ancestor(buf_secs[k - 1], nxt):
            k -= 1
        tail = list(zip(buf[k:], buf_secs[k:]))
        del buf[k:]
        del buf_secs[k:]
        return tail

    # 작은 표 통째로(chunk.table_whole 토큰 이하, 기본 0 = 끔) — 끄면 표는 늘 행 단위
    keep_whole = None
    if config.CHUNK_TABLE_WHOLE:
        keep_whole = lambda t: _tlen(tok, t) <= config.CHUNK_TABLE_WHOLE  # noqa: E731

    for sec in build_sections(doc):
        units = section_units(sec, keep_whole)
        if not units:
            continue
        prefix = make_prefix(title, sec.path)
        whole = (prefix + "\n".join(u for u, _ in units)).strip()
        n = _tlen(tok, whole)

        # (A) 절이 통째로 예산에 들어간다 — 앞의 절들과 묶을 수 있으면 묶는다
        if n <= size:
            if buf_n + n > size:
                tail = peel(sec)
                flush()
                buf_n = 0
                # 떼어 낸 제목 절은 새로 모으는 묶음 맨 앞에 다시 넣는다(이 절과 함께 나간다)
                for w, s in tail:
                    buf.append(w)
                    buf_secs.append(s)
                    buf_n += _tlen(tok, w)
            buf.append(whole)
            buf_secs.append(sec)
            buf_n += n
            continue

        # (B) 절 하나가 예산을 넘는다 — 모아 둔 것을 먼저 내보내고 단위 경계에서 쪼갠다
        tail = peel(sec)
        flush()
        buf_n = 0
        # 떼어 낸 제목 절들의 제목 줄은 이 절 첫 조각 맨 앞에 붙인다
        lead = "\n".join(section_units(s)[0][0] for _, s in tail) or None
        head = _has_heading(sec)
        budget = max(config.MIN_CHUNK_TOKENS, size - _tlen(tok, prefix))
        for piece in _pack_units(units, tok, budget, overlap, head=head, lead=lead):
            if piece.strip():
                out.append(_chunk_meta((prefix + piece).strip(), [sec], pf))
    flush()

    out = _enforce_hard_max(out, tok, overlap)
    return [c for c in out if _tlen(tok, c["text"]) >= config.MIN_CHUNK_TOKENS]


#------------------------------------------------------------------
# 청크 메타 만들기
#=> 대표 절(첫 절)의 출처 정보에, 청크가 절 글의 어디에 있는지로 찾은 쪽을 붙인다(탐색기 모드 설계서 §13).
#    1) _PageFinder 로 시작 쪽 · 쪽 범위를 찾는다
#    2) 못 찾으면 예전처럼 절들의 쪽 범위를 쓰고 page 는 None
#
# -in: text = 청크 글자
# -in: secs = 이 청크에 든 절 목록(첫 절이 대표)
# -in: pf   = 문서 하나 동안 쓰는 _PageFinder (절 글 · 찾기 자리를 기억)
#
# -out: dict = {text, section_id, path, source, page_from, page_to, page}
# -out: error = 없음
#------------------------------------------------------------------
def _chunk_meta(text, secs, pf):
    first = secs[0]
    pages = [p for s in secs for p in (s.page_from, s.page_to) if p is not None]
    src = first.heading.source if first.heading is not None else "parser"
    found = pf.locate(text, secs)
    if found is not None:
        # 청크 자리를 찾았으면 쪽 범위를 그 자리로 좁힌다
        page, lo, hi = found
    else:
        # 못 찾으면 절 범위(넓지만 틀리지는 않음)
        page, lo, hi = None, (min(pages) if pages else None), (max(pages) if pages else None)
    return {"text": text, "section_id": first.id, "path": list(first.path),
            "source": src, "page_from": lo, "page_to": hi, "page": page}


_FIND_LEN = 24                  # 청크 머리 · 꼬리에서 대조할 글자 수(공백 뺀)
_FIND_TRY = (0, 40, 80, 120)    # 머리 대조가 안 되면 이만큼 뒤에서 다시(표 머리글을 다시 붙인 조각)
_FIND_MIN = 8                   # 이보다 짧으면 흔한 글이라 대조하지 않는다


#------------------------------------------------------------------
# 청크의 실제 쪽 찾기 (절 글 대조)
#=> 청킹은 절의 단위(제목 · 블록 · 표 행)를 이어 붙여 자르므로, 청크는 절 글의 한 토막이다.
#   그래서 절 글(공백 뺀)과 글자마다의 쪽을 만들어 두고, 청크 머리 · 꼬리가 절 글 어디에 있는지 찾는다.
#    1) 청크 앞의 접두 "[문서 > 절]\n" 은 뗀다(절 글에는 없다)
#    2) 머리 24자를 첫 절 글에서 찾는다 — 같은 절의 앞 청크 자리 뒤부터(같은 글귀가 반복돼도 순서대로).
#       표 머리글을 다시 붙인 조각은 머리가 앞쪽 표 머리에 걸리므로 40 · 80 · 120자 뒤에서도 찾아 본다
#    3) 꼬리 24자를 마지막 절 글에서 찾는다(머리 자리 뒤부터) → 끝 쪽
#   Rust chunk.rs PageFinder 와 같은 규칙.
#
# -필드: secs = 절 번호 → [절 글, 글자마다의 쪽, 다음 찾기 시작 자리]
#------------------------------------------------------------------
class _PageFinder:
    #------------------------------------------------------------------
    # 빈 찾기 상태 만들기
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self):
        self.secs = {}

    #------------------------------------------------------------------
    # 절 글 준비(절마다 한 번 만들어 둔다)
    #
    # -in: sec = 절
    #
    # -out: [절 글, 글자마다의 쪽 목록, 다음 찾기 시작 자리]
    # -out: error = 없음
    #------------------------------------------------------------------
    def _sec(self, sec):
        got = self.secs.get(sec.id)
        if got is None:
            hay, pg = [], []
            for t, p in _unit_pages(sec):
                for ch in t:
                    if not ch.isspace():
                        hay.append(ch)
                        pg.append(p)
            got = ["".join(hay), pg, 0]
            self.secs[sec.id] = got
        return got

    #------------------------------------------------------------------
    # 청크의 쪽 찾기
    #
    # -in: text = 청크 글자
    # -in: secs = 청크가 속한 절 목록
    #
    # -out: (시작 쪽, 최소 쪽, 최대 쪽) = 찾은 쪽
    # -out: error = 머리를 못 찾거나 쪽이 없으면 None (예외 없음)
    #------------------------------------------------------------------
    def locate(self, text, secs):
        body = text
        # 접두는 "[문서 > 절]\n" 모양이다(make_prefix)
        if text.startswith("[") and "]\n" in text:
            body = text[text.index("]\n") + 2:]
        sq = "".join(body.split())
        st = self._sec(secs[0])
        hay, pg, cur = st
        hit = None
        for off in _FIND_TRY:
            needle = sq[off:off + _FIND_LEN]
            if len(needle) < _FIND_MIN:
                break
            # 앞 청크 자리 뒤에서 먼저, 없으면 절 처음부터
            hit = hay.find(needle, cur)
            if hit < 0:
                hit = hay.find(needle)
            if hit >= 0:
                break
            hit = None
        if hit is None or pg[hit] is None:
            return None
        p0 = pg[hit]
        # 다음 청크는 이 자리 뒤부터(겹침이 있어 끝이 아니라 시작 뒤로 둔다)
        st[2] = hit + 1
        # 꼬리 — 마지막 절 글에서
        tail = sq[-_FIND_LEN:]
        hay2, pg2, _ = self._sec(secs[-1])
        start = hit if len(secs) == 1 else 0
        p1 = None
        if len(tail) >= _FIND_MIN:
            e = hay2.find(tail, start)
            if e >= 0:
                p1 = pg2[e + len(tail) - 1]
        if p1 is None:
            p1 = p0
        return p0, min(p0, p1), max(p0, p1)


#------------------------------------------------------------------
# 절 단위와 그 쪽
#=> 제목은 제목 블록의 쪽, 그 밖은 블록 글 전체를 그 블록의 쪽으로 둔다
#   (공백을 빼고 대조하므로 표를 행으로 나누지 않아도 같은 글이 된다).
#
# -in: sec = 절
#
# -out: [(글, 쪽), …]
# -out: error = 없음
#------------------------------------------------------------------
def _unit_pages(sec):
    out = []
    if sec.heading is not None and not sec.heading.synthetic and sec.heading.text.strip():
        out.append((sec.heading.text, sec.heading.page))
    for b in sec.blocks:
        if b.text and b.text.strip():
            out.append((b.text, b.page))
    return out


#------------------------------------------------------------------
# 절 제목이 단위로 들어가는 절인가
#=> section_units 는 합성 제목이 아니고 글자가 있는 제목만 첫 단위로 넣는다. 같은 조건이다.
#
# -in: sec = assemble.Section
#
# -out: bool = True 면 section_units(sec)[0] 이 절 제목이다
# -out: error = 없음
#------------------------------------------------------------------
def _has_heading(sec):
    h = sec.heading
    return h is not None and not h.synthetic and bool(h.text.strip())


#------------------------------------------------------------------
# 제목만 든 절인가
#=> 제목 바로 뒤에 하위 제목이 와서 본문 단위가 하나도 없는 절(예: "4. 지급기준" 다음에
#   곧장 "4.1 …"). 이런 절을 홀로 청크로 내면 제목 한 줄짜리 청크가 된다.
#
# -in: sec = assemble.Section
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _heading_only(sec):
    from .docmodel.assemble import section_units
    return _has_heading(sec) and len(section_units(sec)) == 1


#------------------------------------------------------------------
# 상위 절인가
#=> 제목 경로로 본다. 위 절의 경로가 아래 절 경로의 앞부분이고 아래 절이 더 깊으면 상위다.
#   (예: ["4. 지급기준"] 은 ["4. 지급기준", "4.1."] 의 상위, ["3.2 …"] 는 ["3.3"] 의 상위가 아니다)
#
# -in: upper = 위쪽 절(assemble.Section)
# -in: lower = 아래쪽 절
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def _is_ancestor(upper, lower):
    k = len(upper.path)
    return len(lower.path) > k and lower.path[:k] == upper.path


#------------------------------------------------------------------
# 단위 묶기 — 큰 절을 블록·표 행 경계에서 자른다
#=> 단위를 차례로 담다가 예산을 넘으면 조각을 끊는다. 표 행으로 새 조각이 시작되면
#   그 표의 머리글을 앞에 붙인다(머리글 토큰은 예산에 세지 않는다 — 기존 규칙과 같다).
#   WholeTable 단위(chunk.table_whole 이하의 작은 표)는 예산을 넘어도 hard_max 안이면 쪼개지 않는다.
#   제목 줄만 든 조각은 끊지 않는다 — 지금 조각에 절 제목(과 넘겨받은 상위 제목, 그 뒤의 표
#   머리글)만 들었으면 '머리 상태'로 보고, 다음 단위가 예산을 넘겨도 끊지 않고 함께 담는다.
#   다음 단위가 혼자 예산을 넘어 쪼개질 때는 머리 줄을 그 첫 조각 맨 앞에 붙인다. 그래서 그
#   조각은 머리 줄 토큰만큼 예산을 조금 넘을 수 있다(절대 상한은 _enforce_hard_max 가 지킨다).
#
# -in: units   = [(글자, 머리글 또는 None), …] (assemble.section_units)
# -in: tok     = 토크나이저
# -in: budget  = 조각 하나의 토큰 예산(접두 제외)
# -in: overlap = 단위 내부 분할 때의 겹침 토큰 수
# -in: head    = True 면 units[0] 이 절 제목이다(기본 False)
# -in: lead    = 첫 조각 맨 앞에 둘 제목 줄(앞의 '제목만 든 상위 절'에서 넘겨받음). 없으면 None
#
# -out: [조각 글자, …]
# -out: error = 없음
#------------------------------------------------------------------
def _pack_units(units, tok, budget, overlap, head=False, lead=None):
    from .docmodel.assemble import WholeTable

    pieces, cur = [], []
    # n = 지금 조각 토큰 수 · head_only = 지금 조각이 제목(·표 머리글) 줄뿐인가
    state = {"n": 0, "head_only": False}
    if lead:
        cur.append(lead)
        state["n"] = _tlen(tok, lead)
        state["head_only"] = True
    soft = budget * max(1.0, float(config.CHUNK_OVERFLOW_RATIO))

    # 지금 조각을 끊어 낸다
    def emit():
        if cur:
            pieces.append("\n".join(cur))
            cur.clear()
        state["n"] = 0
        state["head_only"] = False

    for i, (text, header) in enumerate(units):
        n = _tlen(tok, text)
        hdr = header if (header and _tlen(tok, header) <= config.TABLE_HEADER_MAX_TOKENS) else None
        # 표 머리글 단위(다음 단위가 이 글을 머리글로 갖는다) — 첫 행과 함께 들어갈 자리가 없으면
        # 지금 조각을 먼저 끊는다. 머리글만 조각 끝에 홀로 남으면 다음 조각에 또 붙어 두 번 들어간다.
        # 단 지금 조각이 제목뿐이면 끊지 않는다(제목이 홀로 남는다).
        nxt = units[i + 1] if i + 1 < len(units) else None
        is_hdr_unit = nxt is not None and nxt[1] == text
        if cur and not state["head_only"] and is_hdr_unit \
                and state["n"] + n + _tlen(tok, nxt[0]) > budget:
            emit()
        if n > budget:
            # 제목 줄뿐인 조각은 따로 내지 않고 이 단위 첫 조각 앞에 붙인다
            pre = []
            if state["head_only"]:
                pre = list(cur)
                # 끝이 이 행의 표 머리글이면 아래에서 다시 붙으므로 뺀다(두 번 들어가지 않게)
                if hdr and pre and pre[-1] == hdr:
                    pre.pop()
                cur.clear()
                state["n"] = 0
                state["head_only"] = False
            else:
                emit()
            if (n <= soft or isinstance(text, WholeTable)) and n <= config.CHUNK_HARD_MAX:
                # 약한 초과 허용 · 통째로 둘 작은 표 — 단위를 쪼개지 않고 한 조각
                subs = [(hdr + "\n" + text) if hdr else text]
            else:
                subs = _split_unit(text, tok, budget, overlap)
                if hdr and subs:
                    subs[0] = hdr + "\n" + subs[0]
            if pre and subs:
                subs[0] = "\n".join(pre) + "\n" + subs[0]
            elif pre:
                pieces.append("\n".join(pre))
            pieces.extend(subs)
            continue
        if state["n"] + n > budget and not state["head_only"]:
            emit()
        was_head_only = state["head_only"] and bool(cur)
        if not cur and hdr:
            # 표 행으로 조각이 시작된다 — 머리글이 없으면 '5 가 일수인지 금액인지' 알 수 없다
            cur.append(hdr)
        cur.append(text)
        state["n"] += n
        # 머리 상태가 이어지는 경우: 절 제목(첫 단위)이거나, 머리 상태에 표 머리글 단위가 더해질 때
        state["head_only"] = (head and i == 0) or (was_head_only and is_hdr_unit)
    emit()
    return pieces


#------------------------------------------------------------------
# 단위 하나를 쪼개기 (예산 초과 단위)
#=> _split_oversized 와 같은 차례(줄 → 토큰)에, 설정이 켜져 있으면 줄과 토큰 사이에
#   문장 경계를 끼운다. 블록 모델에서는 문단이 한 줄인 경우가 많아 줄 다음 곧바로
#   토큰 중간에서 잘리기 쉽기 때문이다.
#
# -in: text, tok, budget, overlap
#
# -out: [조각 글자, …]
# -out: error = 없음
#------------------------------------------------------------------
def _split_unit(text, tok, budget, overlap):
    if not config.CHUNK_SENTENCE_SPLIT:
        return _split_oversized(text, tok, budget, overlap)
    lines = []
    for ln in text.split("\n"):
        if ln.strip() and _tlen(tok, ln) > budget:
            # 예산을 넘는 줄은 문장으로 나눠 다시 담는다
            lines.extend(s for s in _SENT_END.split(ln) if s.strip())
        else:
            lines.append(ln)
    return _split_oversized("\n".join(lines), tok, budget, overlap)


#------------------------------------------------------------------
# 절대 상한 지키기
#=> e5 는 512 토큰(특수토큰 2개 포함)까지만 본다. 넘는 뒷부분은 벡터에 반영되지 않아
#   그 내용으로는 검색이 안 된다. 약한 초과·머리글 재부착이 겹쳐 상한을 넘은 청크는
#   토큰으로 잘라 나눈다(같은 메타를 물려준다).
#
# -in: chunks  = [dict, …]
# -in: tok     = 토크나이저
# -in: overlap = 겹침 토큰 수
#
# -out: [dict, …]
# -out: error = 없음
#------------------------------------------------------------------
def _enforce_hard_max(chunks, tok, overlap):
    hard = config.CHUNK_HARD_MAX
    out = []
    for c in chunks:
        if _tlen(tok, c["text"]) <= hard:
            out.append(c)
            continue
        ids = tok.encode(c["text"], add_special_tokens=False).ids
        for piece in chunk_ids(ids, hard, overlap):
            out.append(dict(c, text=tok.decode(piece)))
    return out
