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
