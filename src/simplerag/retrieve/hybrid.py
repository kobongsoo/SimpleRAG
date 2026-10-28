#------------------------------------------------------------------
# 하이브리드 검색 (설계서 결정6)
#=> dense(벡터) 와 BM25 를 각각 돌려 RRF 로 합친 뒤 상위 K개를 돌려준다.
#
#   왜 RRF 인가: 두 검색기의 점수 체계가 달라(코사인 vs BM25 점수) 그대로
#   더할 수 없다. 순위만 쓰면 정규화가 필요 없다.
#     score(d) = Σ 1/(k + rank(d)),  k=60
#
#   왜 1차 후보를 10건씩 뽑는가: 융합에 여유가 있어야 두 검색기의 서로 다른
#   판단이 반영된다. top-3 만 뽑아 합치면 사실상 교집합만 남는다.
#   (후보 수·k 는 config.yaml 의 retrieval.dense_top_k / bm25_top_k / rrf_k, §35)
#
#   실측: 실문서 Recall@3 100%, 지연 10ms(385청크) / 166ms(5만청크).
#------------------------------------------------------------------

import os
import re

from .. import config
from . import synonyms

# 공백만 지운 문자열로 같은지 본다. 추출기가 같은 문서를 조금 다르게 띄어쓰는
# 경우가 있어 공백을 무시해야 사본이 잡힌다.
_WS = re.compile(r"\s+")


#------------------------------------------------------------------
# 중복 판정 키
#=> 같은 내용을 담은 청크를 하나로 본다. 지금은 **완전 일치만** 잡는다.
#   유사도 기반(부분 중복)까지 손대면 서로 다른 조문을 같은 것으로 묶어
#   근거를 잃을 위험이 있어, 확실한 것부터 처리한다.
#
#   🔴 문서 제목 접두는 빼고 비교한다 (REPORT §36.6, 계획서 1-1)
#   구조 청킹은 본문 앞에 `[파일명 > 절 제목]` 을 붙인다(structure.make_prefix). 같은 규정이
#   두 파일(예: 14.유형자산관리지침_19.03.29.doc / 58_유형자산 관리 지침-…-변경안.doc)로 있으면
#   본문·절 제목이 같아도 **파일명이 달라** 키가 달라지고, 사본이 근거 3칸 중 2칸을 차지했다.
#    1) doc_name(파일명)에서 확장자를 뗀 제목을 구한다 — 인덱서가 접두에 쓴 제목과 같은 규칙
#    2) 청크 안의 `[제목 > ` 은 `[> ` 로, `[제목]` 은 `[]` 로 바꾼다
#       — 절 제목은 남긴다. 본문이 같아도 다른 절이면 다른 근거로 본다
#       — 작은 절을 합친 청크는 머리가 여러 개라 모두 바꾼다
#    3) 공백을 지운 문자열을 키로 쓴다
#   doc_name 이 없으면(고정 청킹·옛 호출) 종전과 같이 본문 전체로 비교한다.
#
# -in: text     = 청크 본문
# -in: doc_name = 청크가 속한 파일명 (선택, 예: "14.유형자산관리지침_19.03.29.doc")
#
# -out: 비교용 문자열(빈 청크는 None — 중복 판정에서 제외)
# -out: error = 예외 없음
#------------------------------------------------------------------
def dedup_key(text, doc_name=None):
    text = text or ""
    title = os.path.splitext(os.path.basename(doc_name))[0] if doc_name else ""
    if title:
        # 파일명만 지우고 괄호·절 제목은 남긴다 — 접두 모양은 make_prefix 의 "[" + " > ".join + "]"
        text = text.replace("[" + title + " > ", "[> ").replace("[" + title + "]", "[]")
    flat = _WS.sub("", text)
    return flat or None


#------------------------------------------------------------------
# 순서대로 고유한 청크만 top_k 개 고르기
#=> 리랭킹 **뒤에** 한 번 더 사본을 거른다(§37 e11 에서 발견).
#   _dedup 은 고유 후보가 리랭커 풀(10)보다 적으면 사본으로 풀을 채운다. 사본은 본문이 같아
#   리랭커 점수도 **똑같이** 나오므로, 원본이 상위면 사본도 바로 옆에 붙어 근거 3칸에 함께 들어갔다
#   (g014·g018·g019 — 사본 쌍 문서 d04 는 고유 조각이 적어 풀이 사본으로 채워졌다).
#    1) 주어진 순서(리랭커 점수 순)대로 훑으며 처음 보는 키만 담는다
#    2) 고유한 것이 top_k 에 못 미치면 건너뛴 사본으로 채운다 — 근거 개수는 줄이지 않는다
#
# -in: chunks = 순위 순 청크 dict 목록 (text, doc_name)
# -in: top_k  = 최종 개수
#
# -out: 청크 dict 목록 (최대 top_k 개, 순서 보존)
# -out: error = 예외 없음
#------------------------------------------------------------------
def pick_unique(chunks, top_k):
    picked, seen, dropped = [], set(), []
    for c in chunks:
        key = dedup_key(c.get("text", ""), c.get("doc_name"))
        if key is not None and key in seen:
            dropped.append(c)
            continue
        if key is not None:
            seen.add(key)
        picked.append(c)
        if len(picked) >= top_k:
            return picked
    return (picked + dropped)[:top_k]


#------------------------------------------------------------------
# 동의어 넓힘 때 덧붙일 근거 (RRF 1위)
#=> 동의어 사전이 질문을 넓혀 RRF 1위로 끌어올린 청크를, 리랭커가 원래 일상어 질문만 보고
#   상위 top_k 밖으로 밀어낼 때가 많다. 그때 그 청크를 top_k+1 번째 근거로 되살린다.
#    1) 설정(retrieval.synonym_extra)이 꺼졌거나 이 질문에서 넓힘이 없었으면 붙이지 않는다
#    2) 근거가 top_k 를 못 채웠으면(작은 폴더) 붙이지 않는다 — 이미 후보를 다 쓴 것이다
#    3) RRF 1위가 이미 근거에 있거나 근거와 내용이 같은 사본이면 붙이지 않는다
#   대가: 근거 한 건(평균 약 100토큰)만큼 TTFT 가 늘어난다(실측 중앙값 2.96 → 4.17초, 넓힘이 일어난 질문만).
#
# -in: picked = 리랭킹으로 고른 근거 목록
# -in: first  = RRF 1위 청크(리랭킹 전 후보의 맨 앞)
# -in: timing = 1단계 검색 timing(넓힘 내역 "synonyms" 가 있으면 넓힘이 일어난 것)
# -in: top_k  = 원래 근거 개수
#
# -out: 덧붙일 청크 dict, 붙일 것이 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def synonym_extra(picked, first, timing, top_k):
    if not config.SYNONYM_EXTRA or not timing.get("synonyms") or len(picked) < top_k:
        return None
    # 같은 청크이거나 내용이 같은 사본이면 한 건 더 넣어도 새 정보가 없다
    key = dedup_key(first.get("text", ""), first.get("doc_name"))
    for c in picked:
        if c.get("id") == first.get("id") or (key and dedup_key(c.get("text", ""), c.get("doc_name")) == key):
            return None
    return first


# 청크 글 앞의 "[문서 > 절]" 머리 한 줄 — 이웃을 이어 붙일 때 머리가 겹쳐 찍히지 않게 뗀다
_HEAD = re.compile(r"^\[[^\]\n]*\]\n")


#------------------------------------------------------------------
# 정밀 모드 — 근거에 이웃 청크 이어 붙이기 (코드수정 설계서 §7 ③ C4)
#=> 답이 다음 청크로 잘려 넘어간 근거를 채운다. 근거마다 같은 문서의 바로 앞·뒤 청크(±1) 가운데
#   리랭커 후보(pool) 안에 든 것만 붙인다 — 질문과 상관있다고 이미 뽑힌 것만 붙여 근거가 덜 길어진다
#   (C1~C5 다섯 조건 중 토큰당 가장 나음: 정답 발췌 63 → 73, 근거 340 → 458 토큰).
#    1) 설정(generation.precision)이 꺼졌으면 그대로 돌려준다
#    2) 근거마다 (문서, 청크 번호 ±1) 를 후보에서 찾는다. 이미 근거로 든 청크는 붙이지 않는다
#    3) 앞 이웃은 머리 줄 뒤·본문 앞에, 뒤 이웃은 본문 뒤에 머리 줄을 떼고 잇는다
#
# -in: picked = 고른 근거 목록 [{text, doc_path, chunk_idx, ...}]
# -in: pool   = 리랭커 후보 전체(같은 모양)
#
# -out: (새 근거 목록, 붙인 이웃 수) — 원래 dict 는 바꾸지 않고 text 만 바꾼 사본을 쓴다
# -out: error = 없음
#------------------------------------------------------------------
def attach_neighbors(picked, pool):
    if not config.PRECISION:
        return picked, 0
    by = {(c.get("doc_path"), c.get("chunk_idx")): c for c in pool}
    out, n = [], 0
    for c in picked:
        idx, path = c.get("chunk_idx"), c.get("doc_path")
        if idx is None or not path:
            out.append(c)
            continue
        # 같은 문서에서 이미 근거로 든 청크는 다시 넣으면 같은 글이 두 번 들어간다
        used = {x.get("chunk_idx") for x in picked if x.get("doc_path") == path}
        before, after = [], []
        for j in (idx - 1, idx + 1):
            p = by.get((path, j))
            if p is None or j in used:
                continue
            (before if j < idx else after).append(_HEAD.sub("", p.get("text", ""), count=1))
        if not before and not after:
            out.append(c)
            continue
        text = c["text"]
        if before:
            head = _HEAD.match(text)
            text = (head.group(0) if head else "") + "\n".join(before) + "\n" + _HEAD.sub("", text, count=1)
        if after:
            text = text + "\n" + "\n".join(after)
        n += len(before) + len(after)
        out.append(dict(c, text=text))
    return out, n


#------------------------------------------------------------------
# RRF 융합
#=> 여러 순위 목록을 순위 기반 점수로 합친다.
#
# -in: ranked_lists = [[doc_id 순위대로], ...]
# -in: k            = 순위 완충 상수
#
# -out: [(doc_id, score), ...] 점수 내림차순
#------------------------------------------------------------------
def rrf(ranked_lists, k=None):
    k = config.RRF_K if k is None else k
    scores = {}
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


#------------------------------------------------------------------
# 관련도 문턱 (리랭커 점수) — "질문에 대한 내용이 없다" 판정의 첫째 신호
#=> 리랭커(크로스인코더)의 가장 높은 점수가 이보다 낮으면 무조건 "관련 없음".
#   리랭커 점수만으로는 가를 수 없었다(2026-09-18 실측, 운영 인덱스 복사본):
#     관련 없는 "출장시 숙박비는" vs 비트코인 조각 = -2.45, 관련 있는 골든 질문 최저 = -3.20.
#   그래서 이 문턱은 "누가 봐도 엉뚱한 것" 만 막게 낮게(-6) 두고, 애매한 구간은 둘째 신호
#   (핵심 낱말 겹침, relevance_soft)로 가른다.
#   환경변수 SIMPLERAG_RELEVANCE_MIN 으로 바꾼다("off" 면 판정 전체를 끈다).
#   RAGSearchBox 는 INI [SimpleRAG] RelevanceMin 을 넘긴다. 리랭커가 꺼져 있으면 쓰지 않는다.
#   (config.yaml 로 옮기는 것은 config.py 정리 뒤로 미뤘다)
#
# -in: 없음
#
# -out: 문턱 float 또는 None(끔)
# -out: error = 없음 (잘못된 값이면 기본 -6)
#------------------------------------------------------------------
def relevance_min():
    raw = os.environ.get("SIMPLERAG_RELEVANCE_MIN", "-6").strip().lower()
    if raw in ("", "off", "none"):
        return None
    try:
        return float(raw)
    except ValueError:
        return -6.0


#------------------------------------------------------------------
# 애매한 구간의 윗선 — 이 점수 아래에서 핵심 낱말이 하나도 겹치지 않으면 "관련 없음"
#=> 리랭커가 확신하는 점수(이 값 이상)면 낱말이 달라도(동의어 등) 믿는다.
#   환경변수 SIMPLERAG_RELEVANCE_SOFT(기본 1).
#
# -in: 없음
#
# -out: float
# -out: error = 없음 (잘못된 값이면 1)
#------------------------------------------------------------------
def relevance_soft():
    try:
        return float(os.environ.get("SIMPLERAG_RELEVANCE_SOFT", "1"))
    except ValueError:
        return 1.0


# 질문 낱말 끝의 조사·어미 — 떼고 핵심 낱말만 본다(긴 것부터 맞춘다)
_JOSA = sorted(["은", "는", "이", "가", "을", "를", "의", "에", "에서", "으로", "로", "와", "과", "도", "만",
                "요", "까", "인가요", "인가", "입니까", "나요", "은요", "는요", "이란", "란", "에게", "께",
                "부터", "까지", "보다", "처럼", "이나", "나", "하나요", "되나요", "있나요", "하면", "되면", "시"],
               key=len, reverse=True)
# 어느 문서에나 겹칠 수 있는 질문 말 — 핵심 낱말로 치지 않는다
_GENERIC = {"얼마", "무엇", "어떻게", "어떤", "언제", "누가", "어디", "알려줘", "알려", "주세요", "방법", "경우",
            "대한", "관련", "무엇인가", "뭔가", "있는", "하는", "되는", "기준", "내용", "얼마인가"}


#------------------------------------------------------------------
# 질문의 핵심 낱말
#=> 한글·영문·숫자 덩어리로 자르고, 끝의 조사를 떼고, 두 글자 이상이며 흔한 질문 말이
#   아닌 것만 남긴다. 예) "출장시 숙박비는" → ["출장", "숙박비"]
#
# -in: q = 질문
#
# -out: 낱말 목록
# -out: error = 없음
#------------------------------------------------------------------
def keywords(q):
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]+", q or ""):
        for j in _JOSA:
            if len(w) > len(j) + 1 and w.endswith(j):
                w = w[: -len(j)]
                break
        if len(w) >= 2 and w not in _GENERIC:
            out.append(w)
    return out


#------------------------------------------------------------------
# 핵심 낱말이 근거 후보에 얼마나 들어 있나 (0~1)
#=> 세 글자 이상 낱말은 앞 두 글자만 있어도 든 것으로 본다(합성어·띄어쓰기 차이).
#   실측: 관련 있는 골든 질문 50개 가운데 겹침이 0 인 것은 하나도 없었고,
#   비트코인 폴더의 "출장시 숙박비는" 은 0 이었다.
#
# -in: q     = 질문
# -in: texts = 후보 본문들
#
# -out: 비율 또는 None(핵심 낱말이 없는 질문 — 판정에 쓰지 않는다)
# -out: error = 없음
#------------------------------------------------------------------
def keyword_overlap(q, texts):
    kws = keywords(q)
    if not kws:
        return None
    blob = " ".join(texts)
    hit = sum(1 for k in kws if k in blob or (len(k) >= 3 and k[:2] in blob))
    return hit / len(kws)


class HybridRetriever:
    #------------------------------------------------------------------
    # 생성자 — 구성요소 주입
    #
    # -in: embedder, store, bm25
    # -in: reranker = Reranker 또는 None(리랭킹 끔). App.warmup() 이 생성 백엔드를
    #                 정한 뒤 붙이거나 뗀다 — 켤지는 백엔드에 달렸기 때문이다.
    # -in: matrix   = VectorMatrix(폴더 한정 검색용 벡터 한 벌). None 이면 폴더 검색은
    #                 Qdrant 필터로 한다(느리다 — 폴더 한정 검색 설계서 §4)
    #------------------------------------------------------------------
    def __init__(self, embedder, store, bm25, reranker=None, matrix=None):
        self.embedder = embedder
        self.store = store
        self.bm25 = bm25
        self.reranker = reranker
        self.matrix = matrix

    #------------------------------------------------------------------
    # 검색 (핵심) — 1단계 RRF + (켜져 있으면) 리랭킹
    #=> 리랭커가 없으면 종전과 완전히 같다(_search 그대로).
    #   리랭커가 있으면
    #    1) RRF·사본 제거까지 거친 후보를 RERANK_POOL(기본 10, §35.9)건 뽑는다
    #    2) 크로스인코더로 질의-근거 쌍마다 점수를 매긴다
    #    3) 점수 순으로 top_k 건만 남긴다(동점은 RRF 순위 유지 — 안정 정렬)
    #       — 이때 사본을 한 번 더 거른다(pick_unique). 풀이 사본으로 채워지면 같은 점수의
    #         사본이 원본 옆에 붙어 들어오기 때문이다(§37 e11)
    #   1)~3) 의 골격은 §27/§29/§31 실험 스크립트와 같은 절차다.
    #
    #   리랭커가 실패하면(모델 파일 손상 등) 질의를 죽이지 않고 RRF 순서로
    #   답한다 — 리랭킹은 품질 보탬이지 필수 단계가 아니다.
    #
    # -in: query       = 사용자 질문
    # -in: top_k       = 최종 반환 개수(None 이면 설정값)
    # -in: first_stage = 각 검색기 1차 후보 수(None 이면 설정값)
    # -in: folder      = 이 폴더(하위 포함) 문서의 청크만 근거로 쓴다. None 이면 인덱스 전체
    # -in: corpus      = 업로드 코퍼스(index/uploads.UploadCorpus). 주면 인덱싱 폴더 인덱스는 보지 않고
    #                    그 대화에 올린 문서에서만 찾는다(문서 업로드 설계서 §6). None 이면 지금과 같다
    #
    # -out: (chunks, timing) — timing 에 rerank_ms(리랭킹 시) 또는 rerank_error 추가.
    #        폴더를 주면 scope_docs(그 폴더의 인덱싱된 문서 수)·scope_via 도 담는다
    # -out: error = 검색 구성요소 예외는 전파, 리랭커 예외는 삼키고 RRF 순서로 폴백
    #------------------------------------------------------------------
    def search(self, query, top_k=None, first_stage=None, folder=None, corpus=None):
        import time

        top_k = top_k or config.TOP_K
        # 폴더가 없으면 예전과 똑같이 부른다(기존 시험의 가짜 _search 가 folder 를 모른다)
        extra = {"folder": folder} if folder else {}
        if corpus is not None:
            # 업로드 문서만 — 폴더 한정은 의미가 없다
            extra = {"corpus": corpus}
        if self.reranker is None:
            return self._search(query, top_k, first_stage, **extra)

        # 재정렬할 여지가 있어야 하므로 top_k 보다 넉넉히 뽑는다
        pool = max(top_k, config.RERANK_POOL)
        chunks, timing = self._search(query, pool, first_stage, **extra)
        gate = relevance_min()
        # 관련도 문턱을 쓰면 후보가 적어도 점수를 매겨야 한다 — 작은 폴더에서 문턱이 빠지지 않게
        if not chunks or (len(chunks) <= top_k and gate is None):
            return chunks, timing

        timing = dict(timing)
        t0 = time.perf_counter()
        try:
            scores = self.reranker.score(query, [c["text"] for c in chunks])
        except Exception as e:
            timing["rerank_error"] = "{}: {}".format(type(e).__name__, str(e)[:120])
            return chunks[:top_k], timing
        ms = round((time.perf_counter() - t0) * 1000, 1)

        order = sorted(range(len(chunks)), key=lambda i: -scores[i])
        timing["rerank_ms"] = ms
        timing["total_ms"] = round(timing["total_ms"] + ms, 1)
        top = float(scores[order[0]])
        timing["rerank_top"] = round(top, 2)
        # ⚠️ 가장 관련 있는 후보조차 질문과 동떨어졌으면 근거를 내지 않는다 — 억지로 붙인 근거로
        #    모델이 엉뚱한 답을 지어낸다(사용자 화면: 비트코인 글만 있는 폴더에서 "출장시 숙박비는").
        #    1) 점수가 문턱(-6) 아래면 무조건 관련 없음
        #    2) 애매한 구간(문턱~소프트)에서는 질문의 핵심 낱말이 후보 어디에도 없으면 관련 없음
        if gate is not None:
            ov = keyword_overlap(query, [c["text"] for c in chunks])
            timing["keyword_overlap"] = None if ov is None else round(ov, 2)
            if top < gate or (top < relevance_soft() and ov == 0):
                timing["no_relevant"] = True
                return [], timing
        ranked = [chunks[i] for i in order]
        # 사본은 점수가 원본과 같아 나란히 올라온다 — 설정이 켜져 있으면 고유한 것부터 채운다
        picked = pick_unique(ranked, top_k) if config.DEDUP_EVIDENCE else ranked[:top_k]
        # 동의어로 넓힌 질문이면 RRF 1위를 잃지 않게 한 건 더 붙인다 — 리랭커는 일상어 질문 그대로 보므로
        #   사전이 끌어올린 규정 조항을 다시 밀어낸다(바꿔 말한 질문 42문항: 29 → 37, 골든 100문항 85 그대로)
        extra = synonym_extra(picked, chunks[0], timing, top_k)
        if extra is not None:
            picked = picked + [extra]
            timing["synonym_extra"] = True
        # 정밀 모드면 근거마다 후보 안의 앞뒤 이웃 청크를 잇는다(4B 와 함께 골든 100문항 50 → 59)
        picked, n_nb = attach_neighbors(picked, chunks)
        if n_nb:
            timing["neighbors"] = n_nb
        return picked, timing

    #------------------------------------------------------------------
    # 1단계 검색 (RRF)
    #=> 질의 임베딩 → dense/BM25 각각 1차 후보 → RRF → top-K.
    #   BM25 인덱스가 없으면(캐시 미생성) dense 단독으로 자동 폴백한다.
    #
    # -in: query       = 사용자 질문
    # -in: top_k       = 최종 반환 개수(None 이면 설정값)
    # -in: first_stage = 두 검색기 공통 1차 후보 수(None 이면 설정값 DENSE_TOP_K / BM25_TOP_K)
    # -in: folder      = 폴더 한정 검색 폴더(None 이면 전체)
    # -in: corpus      = 업로드 코퍼스 — 주면 Qdrant·BM25 캐시 대신 그 대화의 청크로 찾는다(전수 내적 + 메모리 BM25)
    #
    # -out: (chunks, timing)
    #        chunks = [{id, text, doc_name, doc_path, chunk_idx}, ...] 순위 순
    #        timing = {embed_ms, dense_ms, bm25_ms, fuse_ms, total_ms}
    #                 (+ 폴더를 주면 scope_docs, scope_via · 업로드면 uploads=올린 문서 수)
    #------------------------------------------------------------------
    def _search(self, query, top_k=None, first_stage=None, folder=None, corpus=None):
        import time

        top_k = top_k or config.TOP_K
        # 두 검색기의 후보 수는 따로 정한다(config.yaml). first_stage 를 주면 둘 다 그 값
        dense_k = first_stage or config.DENSE_TOP_K
        bm25_k = first_stage or config.BM25_TOP_K

        t0 = time.perf_counter()
        # 폴더 한정이면 먼저 그 폴더에 문서가 있는지 본다 — 없으면 임베딩도 하지 않는다
        scope = self._scope(folder) if (folder and corpus is None) else None
        if scope is not None and scope["n_docs"] == 0:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            return [], {"embed_ms": 0.0, "dense_ms": 0.0, "bm25_ms": 0.0, "fuse_ms": 0.0,
                        "total_ms": ms, "scope_docs": 0, "scope_via": scope["via"]}
        qv = self.embedder.embed_query(query)
        t1 = time.perf_counter()

        if corpus is not None:
            # 업로드 문서 — 대화의 청크 벡터 전부와 내적(수백 청크라 1ms 안팎)
            dense = corpus.search(qv, dense_k)
        elif scope is None:
            dense = self.store.search(qv, dense_k)
        elif scope["via"] == "matrix":
            # 벡터 한 벌 + 폴더 표시 — 본문은 아래에서 후보 것만 가져온다
            dense = [(pid, s, None) for pid, s in self.matrix.search(qv, dense_k, scope["mask"])]
        else:
            dense = self.store.search(qv, dense_k, doc_paths=scope["doc_paths"])
        dense_ids = [pid for pid, _, _ in dense]
        payloads = {pid: pl for pid, _, pl in dense if pl}
        t2 = time.perf_counter()

        # bm25_top_k 가 0 이면 BM25 를 건너뛰고 dense 단독으로 융합한다
        bm_extra = {"allow_ids": scope["allow_ids"]} if scope else {}
        # 동의어 사전으로 BM25 질의만 넓힌다 — 일상어("할아버지")에 문서 용어("조부모")를 덧붙인다
        bm_query, syn_pairs = synonyms.expand(query)
        bm25 = corpus.bm25 if corpus is not None else self.bm25
        bm_ids = (bm25.search(bm_query, self.embedder.tokenizer, bm25_k, **bm_extra)
                  if bm25_k > 0 else [])
        t3 = time.perf_counter()

        lists = [dense_ids] + ([bm_ids] if bm_ids else [])
        fused = [pid for pid, _ in rrf(lists)]

        # 융합 목록 전체의 payload 를 한 번에 묶어 가져온다(왕복을 늘리지 않게).
        missing = [pid for pid in fused if pid not in payloads]
        if missing and corpus is not None:
            payloads.update({pid: corpus.payloads.get(pid) for pid in missing})
        elif missing:
            payloads.update(self._fetch_payloads(missing))
        # ⚠️ payload 가 없는 id 는 버린다. 문서를 지우거나 바꾼 뒤 BM25 를 아직 다시
        #    만들지 않았으면 BM25 가 이미 없는 점을 돌려준다 — 그대로 두면 본문 없는
        #    빈 근거가 답변에 섞였다(자동 인덱싱 설계서 §7).
        fused = [pid for pid in fused if payloads.get(pid)]

        if config.DEDUP_EVIDENCE:
            # 사본을 걸러내려면 top_k 보다 넉넉히 봐야 하므로 융합 목록 전체를 훑는다
            picked = self._dedup(fused, payloads, top_k)
        else:
            picked = fused[:top_k]

        chunks = []
        for pid in picked:
            pl = payloads.get(pid) or {}
            chunks.append({"id": pid, "text": pl.get("text", ""),
                           "doc_name": pl.get("doc_name", ""),
                           "doc_path": pl.get("doc_path", ""),
                           "folder": pl.get("folder", ""),
                           "chunk_idx": pl.get("chunk_idx", 0),
                           # blocks 엔진 인덱스에만 있다 — Parent-Child 의 '같은 절' 찾기에 쓴다
                           "section_id": pl.get("section_id")})
        t4 = time.perf_counter()

        timing = {
            "embed_ms": round((t1 - t0) * 1000, 1),
            "dense_ms": round((t2 - t1) * 1000, 1),
            "bm25_ms": round((t3 - t2) * 1000, 1),
            "fuse_ms": round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
        }
        if syn_pairs:
            # 동의어로 넓힌 내역 — 소요 블록에 "검색어 넓힘: 할아버지→조부모" 로 보여 준다
            timing["synonyms"] = ["{}→{}".format(w, t) for w, t in syn_pairs]
        if scope is not None:
            timing["scope_docs"] = scope["n_docs"]
            timing["scope_via"] = scope["via"]
        if corpus is not None:
            timing["uploads"] = corpus.n_files
        return chunks, timing

    #------------------------------------------------------------------
    # 폴더 범위 준비 (폴더 한정 검색 설계서 §5)
    #=> 1) 벡터 한 벌이 있으면(없으면 지금 만든다) 폴더 표시와 허용 점 id 를 꺼낸다 — 빠른 길
    #   2) 한 벌을 한도 때문에 못 만들었으면 상태 파일로 그 폴더 문서를 골라
    #      Qdrant 필터로 찾는다 — 느리지만 메모리를 안 쓰는 길
    #
    # -in: folder = 폴더
    #
    # -out: dict {via: "matrix"|"filter", n_docs, mask, allow_ids, doc_paths}
    # -out: error = 저장소 읽기 실패는 예외 전파
    #------------------------------------------------------------------
    def _scope(self, folder):
        import numpy as np

        from .vector_matrix import norm_dir, under

        if self.matrix is not None:
            self.matrix.ensure_built(self.store)
            if self.matrix.ready():
                m, n_docs = self.matrix.mask(folder)
                return {"via": "matrix", "n_docs": n_docs, "mask": m,
                        "allow_ids": self.matrix.allowed_ids(m), "doc_paths": None}

        # 느린 길 — 상태 파일에서 그 폴더 문서와 점 id 를 모은다
        from ..index.indexer import load_state
        f = norm_dir(folder)
        docs = {k: v for k, v in load_state().get("docs", {}).items()
                if under(os.path.normcase(k), f)}
        ids, no_range = [], []
        for k, meta in docs.items():
            rng = meta.get("ids")
            if rng:
                ids.extend(range(rng[0], rng[1]))
            else:
                no_range.append(k)
        if no_range:
            # 자동 인덱싱 전의 옛 문서(id 범위 없음) — 필터로 모은다(느리다)
            ids.extend(self.store.ids_of_docs(no_range))
        return {"via": "filter", "n_docs": len(docs), "mask": None,
                "allow_ids": np.asarray(ids, dtype=np.int64), "doc_paths": list(docs)}

    #------------------------------------------------------------------
    # 사본 걸러내기
    #=> 융합 순위를 위에서부터 훑으며 **처음 보는 내용만** 담는다.
    #   순위는 그대로 보존한다 — 사본을 건너뛸 뿐 재정렬하지 않는다.
    #
    #   후보가 전부 사본이라 top_k 를 못 채우면 남은 것으로 채운다. 근거를
    #   3건 요청했는데 1건만 주는 것보다는 중복이라도 주는 편이 안전하다
    #   (프롬프트가 [1][2][3] 번호를 전제로 한다).
    #
    #   사본 판정은 파일명 접두를 뺀 본문으로 한다(dedup_key, §36.6) — 파일 두 벌로 있는
    #   같은 규정의 조각이 근거 칸을 나눠 먹지 않게.
    #
    # -in: order    = 융합 순위대로 나열된 point id
    # -in: payloads = {point id: payload}
    # -in: top_k    = 최종 개수
    #
    # -out: [point id, ...] 최대 top_k 개
    # -out: error = 예외 없음
    #------------------------------------------------------------------
    def _dedup(self, order, payloads, top_k):
        picked, seen, dropped = [], set(), []
        for pid in order:
            pl = payloads.get(pid) or {}
            # 같은 파일명 접두만 지우도록 이 청크의 파일명을 함께 넘긴다
            key = dedup_key(pl.get("text", ""), pl.get("doc_name"))
            if key is not None and key in seen:
                dropped.append(pid)
                continue
            if key is not None:
                seen.add(key)
            picked.append(pid)
            if len(picked) >= top_k:
                return picked
        return (picked + dropped)[:top_k]

    #------------------------------------------------------------------
    # id 로 payload 조회 (BM25 단독 히트 보완)
    #
    # -out: {point_id: payload}
    #------------------------------------------------------------------
    def _fetch_payloads(self, ids):
        from ..index.store import COLLECTION

        self.store.ensure_collection()
        pts = self.store._client.retrieve(
            collection_name=COLLECTION, ids=list(ids), with_payload=True)
        return {p.id: p.payload for p in pts}
