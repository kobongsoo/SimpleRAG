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


class HybridRetriever:
    #------------------------------------------------------------------
    # 생성자 — 구성요소 주입
    #
    # -in: embedder, store, bm25
    # -in: reranker = Reranker 또는 None(리랭킹 끔). App.warmup() 이 생성 백엔드를
    #                 정한 뒤 붙이거나 뗀다 — 켤지는 백엔드에 달렸기 때문이다.
    #------------------------------------------------------------------
    def __init__(self, embedder, store, bm25, reranker=None):
        self.embedder = embedder
        self.store = store
        self.bm25 = bm25
        self.reranker = reranker

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
    #
    # -out: (chunks, timing) — timing 에 rerank_ms(리랭킹 시) 또는 rerank_error 추가
    # -out: error = 검색 구성요소 예외는 전파, 리랭커 예외는 삼키고 RRF 순서로 폴백
    #------------------------------------------------------------------
    def search(self, query, top_k=None, first_stage=None):
        import time

        top_k = top_k or config.TOP_K
        if self.reranker is None:
            return self._search(query, top_k, first_stage)

        # 재정렬할 여지가 있어야 하므로 top_k 보다 넉넉히 뽑는다
        pool = max(top_k, config.RERANK_POOL)
        chunks, timing = self._search(query, pool, first_stage)
        if len(chunks) <= top_k:
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
        ranked = [chunks[i] for i in order]
        # 사본은 점수가 원본과 같아 나란히 올라온다 — 설정이 켜져 있으면 고유한 것부터 채운다
        picked = pick_unique(ranked, top_k) if config.DEDUP_EVIDENCE else ranked[:top_k]
        return picked, timing

    #------------------------------------------------------------------
    # 1단계 검색 (RRF)
    #=> 질의 임베딩 → dense/BM25 각각 1차 후보 → RRF → top-K.
    #   BM25 인덱스가 없으면(캐시 미생성) dense 단독으로 자동 폴백한다.
    #
    # -in: query       = 사용자 질문
    # -in: top_k       = 최종 반환 개수(None 이면 설정값)
    # -in: first_stage = 두 검색기 공통 1차 후보 수(None 이면 설정값 DENSE_TOP_K / BM25_TOP_K)
    #
    # -out: (chunks, timing)
    #        chunks = [{id, text, doc_name, doc_path, chunk_idx}, ...] 순위 순
    #        timing = {embed_ms, dense_ms, bm25_ms, fuse_ms, total_ms}
    #------------------------------------------------------------------
    def _search(self, query, top_k=None, first_stage=None):
        import time

        top_k = top_k or config.TOP_K
        # 두 검색기의 후보 수는 따로 정한다(config.yaml). first_stage 를 주면 둘 다 그 값
        dense_k = first_stage or config.DENSE_TOP_K
        bm25_k = first_stage or config.BM25_TOP_K

        t0 = time.perf_counter()
        qv = self.embedder.embed_query(query)
        t1 = time.perf_counter()

        dense = self.store.search(qv, dense_k)
        dense_ids = [pid for pid, _, _ in dense]
        payloads = {pid: pl for pid, _, pl in dense}
        t2 = time.perf_counter()

        # bm25_top_k 가 0 이면 BM25 를 건너뛰고 dense 단독으로 융합한다
        bm_ids = (self.bm25.search(query, self.embedder.tokenizer, bm25_k)
                  if bm25_k > 0 else [])
        t3 = time.perf_counter()

        lists = [dense_ids] + ([bm_ids] if bm_ids else [])
        fused = [pid for pid, _ in rrf(lists)]

        # 융합 목록 전체의 payload 를 한 번에 묶어 가져온다(왕복을 늘리지 않게).
        missing = [pid for pid in fused if pid not in payloads]
        if missing:
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
                           "chunk_idx": pl.get("chunk_idx", 0)})
        t4 = time.perf_counter()

        timing = {
            "embed_ms": round((t1 - t0) * 1000, 1),
            "dense_ms": round((t2 - t1) * 1000, 1),
            "bm25_ms": round((t3 - t2) * 1000, 1),
            "fuse_ms": round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
        }
        return chunks, timing

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
