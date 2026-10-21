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
#
# -in: doc     = docmodel.Document
# -in: tok     = tokenizers.Tokenizer (절단 해제 상태)
# -in: title   = 문서 제목(접두에 쓴다). None 이면 doc.title
# -in: size, overlap = None 이면 설정값
#
# -out: [dict, …] — {text, section_id, path, source, page_from, page_to}
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
                 "page_from": None, "page_to": None}
                for c in split_structured(doc.raw_text, tok, title=title,
                                          size=size, overlap=overlap)]

    out = []
    buf, buf_n, buf_secs = [], 0, []

    # 모아 둔 작은 절들을 청크 하나로 내보낸다 — 메타는 첫 절 기준, 쪽은 합친 범위
    def flush():
        if buf:
            out.append(_chunk_meta("\n\n".join(buf).strip(), buf_secs))
            buf.clear()
            buf_secs.clear()

    for sec in build_sections(doc):
        units = section_units(sec)
        if not units:
            continue
        prefix = make_prefix(title, sec.path)
        whole = (prefix + "\n".join(u for u, _ in units)).strip()
        n = _tlen(tok, whole)

        # (A) 절이 통째로 예산에 들어간다 — 앞의 절들과 묶을 수 있으면 묶는다
        if n <= size:
            if buf_n + n > size:
                flush()
                buf_n = 0
            buf.append(whole)
            buf_secs.append(sec)
            buf_n += n
            continue

        # (B) 절 하나가 예산을 넘는다 — 모아 둔 것을 먼저 내보내고 단위 경계에서 쪼갠다
        flush()
        buf_n = 0
        budget = max(config.MIN_CHUNK_TOKENS, size - _tlen(tok, prefix))
        for piece in _pack_units(units, tok, budget, overlap):
            if piece.strip():
                out.append(_chunk_meta((prefix + piece).strip(), [sec]))
    flush()

    out = _enforce_hard_max(out, tok, overlap)
    return [c for c in out if _tlen(tok, c["text"]) >= config.MIN_CHUNK_TOKENS]


#------------------------------------------------------------------
# 청크 메타 만들기
#
# -in: text = 청크 글자
# -in: secs = 이 청크에 든 절 목록(첫 절이 대표)
#
# -out: dict = {text, section_id, path, source, page_from, page_to}
# -out: error = 없음
#------------------------------------------------------------------
def _chunk_meta(text, secs):
    first = secs[0]
    pages = [p for s in secs for p in (s.page_from, s.page_to) if p is not None]
    src = first.heading.source if first.heading is not None else "parser"
    return {"text": text, "section_id": first.id, "path": list(first.path),
            "source": src, "page_from": min(pages) if pages else None,
            "page_to": max(pages) if pages else None}


#------------------------------------------------------------------
# 단위 묶기 — 큰 절을 블록·표 행 경계에서 자른다
#=> 단위를 차례로 담다가 예산을 넘으면 조각을 끊는다. 표 행으로 새 조각이 시작되면
#   그 표의 머리글을 앞에 붙인다(머리글 토큰은 예산에 세지 않는다 — 기존 규칙과 같다).
#
# -in: units   = [(글자, 머리글 또는 None), …] (assemble.section_units)
# -in: tok     = 토크나이저
# -in: budget  = 조각 하나의 토큰 예산(접두 제외)
# -in: overlap = 단위 내부 분할 때의 겹침 토큰 수
#
# -out: [조각 글자, …]
# -out: error = 없음
#------------------------------------------------------------------
def _pack_units(units, tok, budget, overlap):
    pieces, cur, cur_n = [], [], 0
    soft = budget * max(1.0, float(config.CHUNK_OVERFLOW_RATIO))

    # 지금 조각을 끊어 낸다
    def emit():
        if cur:
            pieces.append("\n".join(cur))
            cur.clear()

    for i, (text, header) in enumerate(units):
        n = _tlen(tok, text)
        hdr = header if (header and _tlen(tok, header) <= config.TABLE_HEADER_MAX_TOKENS) else None
        # 표 머리글 단위(다음 단위가 이 글을 머리글로 갖는다) — 첫 행과 함께 들어갈 자리가 없으면
        # 지금 조각을 먼저 끊는다. 머리글만 조각 끝에 홀로 남으면 다음 조각에 또 붙어 두 번 들어간다.
        nxt = units[i + 1] if i + 1 < len(units) else None
        if cur and nxt is not None and nxt[1] == text \
                and cur_n + n + _tlen(tok, nxt[0]) > budget:
            emit()
            cur_n = 0
        if n > budget:
            emit()
            cur_n = 0
            if n <= soft and n <= config.CHUNK_HARD_MAX:
                # 약한 초과 허용 — 단위를 쪼개지 않고 통째로 한 조각
                pieces.append((hdr + "\n" + text) if hdr else text)
            else:
                subs = _split_unit(text, tok, budget, overlap)
                if hdr and subs:
                    subs[0] = hdr + "\n" + subs[0]
                pieces.extend(subs)
            continue
        if cur_n + n > budget:
            emit()
            cur_n = 0
        if not cur and hdr:
            # 표 행으로 조각이 시작된다 — 머리글이 없으면 '5 가 일수인지 금액인지' 알 수 없다
            cur.append(hdr)
        cur.append(text)
        cur_n += n
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
