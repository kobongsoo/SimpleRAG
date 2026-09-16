#------------------------------------------------------------------
# RAG 파이프라인 — 2단계 응답 오케스트레이션 (설계서 결정8)
#=> 검색 결과(근거)를 먼저 방출하고, 그 뒤에 LLM 요약을 스트리밍한다.
#
#   왜 2단계인가 — 두 가지 이유가 있고 둘 다 중요하다.
#    1) 속도: RAG 의 답은 이미 검색 결과 안에 있다. 근거를 0.17초에 띄우면
#       체감 지연이 사실상 사라지고, 2.75초 뒤 도착하는 요약은 덤이 된다.
#    2) 안전: 0.6B 의 실문서 정답률은 83% 다. 즉 약 17% 는 틀린다.
#       사용자가 근거로 직접 검증할 수 있어야 하므로 근거 노출은 선택이 아니다.
#
#   호출자는 이벤트 스트림을 받는다:
#     ("evidence", chunks, timing)  ← 검색 직후 (약 10~170ms)
#     ("token", str)                ← 첫 글자부터 스트리밍
#     ("done", {answer, cited, ...})
#------------------------------------------------------------------

import time

from . import config
from .generate.prompts import parse_answer


class RagPipeline:
    #------------------------------------------------------------------
    # 생성자 — 구성요소 주입
    #
    # -in: retriever = HybridRetriever
    # -in: generator = GgufGenerator
    #------------------------------------------------------------------
    def __init__(self, retriever, generator):
        self.retriever = retriever
        self.generator = generator

    #------------------------------------------------------------------
    # 질의 응답 (핵심) — 이벤트 제너레이터
    #=> 근거를 먼저 내보내고 토큰을 흘린다. 호출자(CLI/UI)가 이 순서대로
    #   화면을 갱신하면 2단계 UX 가 그대로 구현된다.
    #
    # -in: query      = 사용자 질문
    # -in: top_k      = 근거 개수(None 이면 설정값=3)
    # -in: max_tokens = 답변 토큰 상한
    #
    # -out: Iterator[tuple] = ("evidence"|"token"|"done", ...)
    #------------------------------------------------------------------
    def answer(self, query, top_k=None, max_tokens=None):
        t0 = time.perf_counter()

        chunks, timing = self.retriever.search(query, top_k=top_k)
        yield ("evidence", chunks, timing)

        if not chunks:
            yield ("done", {"answer": "검색된 근거가 없습니다.", "cited": [],
                            "ttft_s": 0.0, "total_s": round(time.perf_counter() - t0, 2),
                            "timing": timing})
            return

        texts = [c["text"] for c in chunks]
        t_gen0 = time.perf_counter()
        t_first = None
        parts = []

        for piece in self.generator.stream(query, texts, max_tokens=max_tokens):
            if t_first is None:
                t_first = time.perf_counter()
            parts.append(piece)
            yield ("token", piece)

        t_end = time.perf_counter()
        raw = "".join(parts)
        # 근거 개수를 함께 넘긴다 — 없는 번호를 인용으로 읽지 않게 한다.
        body, cited = parse_answer(raw, len(texts))

        yield ("done", {
            "answer": body,
            "cited": cited,
            # 사용자가 체감하는 TTFT — 검색까지 포함한 '첫 글자까지'
            "ttft_s": round((t_first or t_end) - t0, 2),
            "llm_ttft_s": round((t_first or t_end) - t_gen0, 2),
            "total_s": round(t_end - t0, 2),
            "timing": timing,
        })

    #------------------------------------------------------------------
    # 동기 응답 (평가/배치용)
    #=> 스트리밍 없이 한 번에 받는다. 스트리밍 오버헤드(약 30%)가 없어
    #   일괄 평가에 유리하다.
    #
    # -out: dict = {answer, cited, chunks, timing, total_s}
    #------------------------------------------------------------------
    def answer_sync(self, query, top_k=None, max_tokens=None):
        t0 = time.perf_counter()
        chunks, timing = self.retriever.search(query, top_k=top_k)
        if not chunks:
            return {"answer": "검색된 근거가 없습니다.", "cited": [],
                    "chunks": [], "timing": timing, "total_s": 0.0}

        raw = self.generator.generate(query, [c["text"] for c in chunks],
                                      max_tokens=max_tokens)
        body, cited = parse_answer(raw, len(chunks))
        return {"answer": body, "cited": cited, "chunks": chunks,
                "timing": timing, "total_s": round(time.perf_counter() - t0, 2)}
