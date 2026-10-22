#------------------------------------------------------------------
# Parent-Child 실험 A — 검색은 청크로, LLM 에는 절(부모)을 (재설계서 §12)
#=> 검색·리랭크는 지금처럼 128토큰 청크(자식)로 하고, sLLM 에 넣는 근거만 그 청크가 속한
#   '같은 문서·같은 절'의 청크를 이어 붙인 부모로 바꾼다.
#    · 재색인이 필요 없다 — blocks 엔진 payload 의 section_id 로 형제 청크를 찾는다
#    · 같은 절이 여러 번 검색되면 부모는 한 번만 넣고 다음 순위 청크로 채운다
#    · 부모가 상한(parent_max_tokens)을 넘으면 자식 청크를 가운데 두고 앞뒤 청크를 번갈아 붙인다
#    · section_id 가 없는 청크(legacy 경로 문서)는 자식 그대로 쓴다
#   목적: 근거가 128토큰 청크 경계에서 잘리는 문항('질문은 이 청크, 답은 다음 청크')을 줄인다.
#   대가: 근거 토큰이 늘어 prefill(TTFT)이 늘어난다.
#------------------------------------------------------------------

import re

# 청크 첫 줄의 출처 접두 "[문서 > 장 > 조]" — 이어 붙일 때 두 번째 청크부터는 뗀다
_PREFIX = re.compile(r"^\[[^\n]*\]$")


#------------------------------------------------------------------
# 청크 글을 (접두, 본문)으로 나누기
#
# -in: text = 청크 글
#
# -out: (접두 줄 또는 "", 본문)
# -out: error = 없음
#------------------------------------------------------------------
def _split_prefix(text):
    first, _, rest = (text or "").partition("\n")
    if _PREFIX.match(first.strip()):
        return first.strip(), rest.strip()
    return "", (text or "").strip()


#------------------------------------------------------------------
# 부모 글 만들기
#=> 자식 청크를 가운데 두고 앞·뒤 형제 청크를 번갈아 붙이다가 상한에 닿으면 멈춘다.
#   절 전체가 상한 안이면 절 전체가 된다. 접두는 맨 앞에 한 번만 둔다.
#
# -in: child      = 검색된 청크 dict (text · chunk_idx)
# -in: siblings   = 같은 절 청크 payload 목록(chunk_idx 순)
# -in: tlen       = 토큰 길이 함수 fn(str) → int
# -in: max_tokens = 부모 상한(토큰)
#
# -out: str = 부모 글 (형제가 없으면 자식 글 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def build_parent(child, siblings, tlen, max_tokens):
    if len(siblings) <= 1:
        return child["text"]
    idx = next((i for i, s in enumerate(siblings) if s.get("chunk_idx") == child.get("chunk_idx")), None)
    if idx is None:
        return child["text"]
    prefix, _ = _split_prefix(child["text"])
    bodies = [_split_prefix(s.get("text", ""))[1] for s in siblings]
    lens = [tlen(b) for b in bodies]
    chosen = {idx}
    total = tlen(prefix) + lens[idx]
    lo, hi = idx - 1, idx + 1
    lo_ok = hi_ok = True
    # 뒤를 먼저 붙인다 — 조문은 앞 청크에 제목, 뒤 청크에 답이 이어지는 경우가 많다.
    # 한쪽이 상한에 걸리면 그쪽은 멈춘다(중간을 건너뛰어 끊긴 글을 만들지 않게)
    while lo_ok or hi_ok:
        grew = False
        if hi_ok and hi < len(bodies) and total + lens[hi] <= max_tokens:
            chosen.add(hi)
            total += lens[hi]
            hi += 1
            grew = True
        else:
            hi_ok = False
        if lo_ok and lo >= 0 and total + lens[lo] <= max_tokens:
            chosen.add(lo)
            total += lens[lo]
            lo -= 1
            grew = True
        else:
            lo_ok = False
        if not grew:
            break
    body = "\n".join(bodies[j] for j in sorted(chosen) if bodies[j])
    return (prefix + "\n" + body).strip() if prefix else body


#------------------------------------------------------------------
# 검색 결과를 부모로 넓히기 (진입점)
#=> 순위 순으로 청크를 보며 부모를 만들고, 이미 넣은 절은 건너뛴다. top_k 개가 차면 멈춘다.
#   각 결과 dict 의 text 는 부모 글로 바뀌고, 원래 청크 글은 child_text 에 남는다.
#
# -in: chunks     = 검색 결과(순위 순, top_k 보다 넉넉히)
# -in: store      = VectorStore (section_chunks 사용)
# -in: tlen       = 토큰 길이 함수
# -in: top_k      = 최종 근거 수
# -in: max_tokens = 부모 상한(토큰)
#
# -out: [dict, …] 최대 top_k 개
# -out: error = 형제 조회 실패 시 그 청크는 자식 그대로(예외를 삼킨다 — 근거를 잃지 않게)
#------------------------------------------------------------------
def expand(chunks, store, tlen, top_k, max_tokens):
    out, seen = [], set()
    for c in chunks:
        sid = c.get("section_id")
        key = (c.get("doc_path"), sid) if sid is not None else ("id", c.get("id"))
        if key in seen:
            continue
        seen.add(key)
        text = c["text"]
        if sid is not None:
            try:
                text = build_parent(c, store.section_chunks(c.get("doc_path"), sid), tlen, max_tokens)
            except Exception:       # noqa: BLE001 — 부모를 못 만들면 자식으로 답한다
                text = c["text"]
        out.append(dict(c, text=text, child_text=c["text"]))
        if len(out) >= top_k:
            break
    return out
