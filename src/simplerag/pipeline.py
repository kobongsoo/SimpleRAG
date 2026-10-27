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

import re
import time

from . import config
from .generate.prompts import parse_answer, recite
from .retrieve import synonym_suggest

# 폴더 한정 검색에서 그 폴더에 인덱싱된 문서가 없을 때 알리는 글
NO_DOCS_IN_FOLDER = ("이 폴더(하위 포함)에는 인덱싱된 문서가 없습니다. "
                     "범위를 '지정 폴더 전체' 로 넓히거나, 문서가 인덱싱될 때까지 기다려 주세요.")
# 문서에서 질문과 관련된 내용을 찾지 못했을 때(리랭커 관련도 문턱 아래) 알리는 글
NO_RELEVANT = "질문에 대한 내용이 문서에 없습니다. 답변할 수 없습니다."


#------------------------------------------------------------------
# 본문의 인용 번호 목록
#=> 바로잡은 본문에서 다시 센다 — 근거 개수 밖의 번호는 넣지 않는다(parse_answer 와 같은 규칙).
#
# -in: body = 본문 / n = 근거 개수
#
# -out: 정렬된 번호 목록
# -out: error = 없음
#------------------------------------------------------------------
def _cited(body, n):
    return sorted({int(k) for k in re.findall(r"\[(\d+)\]", body) if 1 <= int(k) <= n})

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
    # -in: folder     = 이 폴더(하위 포함) 문서만 근거로 쓴다(폴더 한정 검색). None 이면 전체
    #
    # -out: Iterator[tuple] = ("evidence"|"token"|"done", ...)
    #       폴더에 인덱싱된 문서가 없으면 모델을 부르지 않고 done 의 answer 로 알린다
    #       (timing["scope_docs"] == 0) — 전체로 슬쩍 넓히지 않는다(설계서 §7)
    #------------------------------------------------------------------
    def answer(self, query, top_k=None, max_tokens=None, folder=None):
        t0 = time.perf_counter()

        if config.PARENT_MODE == "section":
            chunks, timing = self._search_parents(query, top_k, folder)
        else:
            chunks, timing = self.retriever.search(query, top_k=top_k, folder=folder)
        yield ("evidence", chunks, timing)
        # 근거를 못 찾았거나 리랭커가 확신하지 못한 질문은 동의어 후보 제안용으로 남긴다(동의어 설계서 3단계)
        synonym_suggest.log_miss(query, timing)

        if not chunks and timing.get("no_relevant"):
            # 가장 가까운 근거도 질문과 동떨어졌다 — 모델을 부르지 않는다(지어낸 답을 막으려고)
            yield ("done", {"answer": NO_RELEVANT, "cited": [],
                            "ttft_s": 0.0, "total_s": round(time.perf_counter() - t0, 2),
                            "timing": timing})
            return
        if not chunks and timing.get("scope_docs") == 0:
            yield ("done", {"answer": NO_DOCS_IN_FOLDER, "cited": [],
                            "ttft_s": 0.0, "total_s": round(time.perf_counter() - t0, 2),
                            "timing": timing})
            return
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
        # 생성기가 정한 남길 답 — 반복·상한으로 잘라 낸 꼬리는 흘려보낸 조각에는 있어도 여기에는 없다
        raw = getattr(self.generator, "last_text", None)
        if raw is None:
            raw = "".join(parts)
        # 근거 개수를 함께 넘긴다 — 없는 번호를 인용으로 읽지 않게 한다.
        # 되뱉기 거르기는 설정을 따른다(generation.echo_filter, 기본 끔)
        body, _ = parse_answer(raw, len(texts), echo=config.ECHO_FILTER)
        # 인용 번호 바로잡기(설계서 ⑤) — 문장 수치가 다른 근거 하나에만 있으면 그 번호로.
        #   answer 는 다 고친 답(비스트리밍·평가), answer_stream 은 문장 끝 뒤 번호만 고친 답 —
        #   스트리밍 화면은 문장 앞 번호를 이미 내보냈으니 화면 출력기는 이것으로 마무리한다
        fixed, fixes = recite(body, texts)
        stream_body, _ = recite(body, texts, trailing_only=True)
        cited = _cited(fixed, len(texts))

        yield ("done", {
            "answer": fixed,
            "answer_stream": stream_body,
            "cited": cited,
            "cite_fixes": fixes,
            # 반복 루프·상한으로 답을 잘라 냈다는 표시(없으면 None) — 화면은 본문 뒤에 따로 붙인다
            "note": getattr(self.generator, "last_note", None),
            # 사용자가 체감하는 TTFT — 검색까지 포함한 '첫 글자까지'
            "ttft_s": round((t_first or t_end) - t0, 2),
            "llm_ttft_s": round((t_first or t_end) - t_gen0, 2),
            "total_s": round(t_end - t0, 2),
            "timing": timing,
        })

    #------------------------------------------------------------------
    # 부모(절) 근거로 검색 — Parent-Child 실험 A (재설계서 §12)
    #=> 같은 절이 여러 번 검색되면 부모는 하나만 넣으므로, top_k 의 두 배를 받아 놓고
    #   서로 다른 절의 부모로 top_k 개를 채운다.
    #    1) 검색(리랭크·관련 근거 판정까지 그대로)
    #    2) 청크마다 같은 절 형제 청크를 모아 부모 글로 바꾼다(parent.expand)
    #
    # -in: query, top_k, folder = answer 와 같음
    #
    # -out: (chunks, timing) — chunks[i]["text"] 는 부모 글, ["child_text"] 는 원래 청크.
    #        timing 에 parent_ms(부모 만들기 시간)
    # -out: error = 검색 예외는 전파, 부모 만들기 실패는 그 청크만 자식 그대로
    #------------------------------------------------------------------
    def _search_parents(self, query, top_k, folder):
        from .chunk import _tlen
        from .retrieve import parent

        k = top_k or config.TOP_K
        chunks, timing = self.retriever.search(query, top_k=k * 2, folder=folder)
        if not chunks:
            return chunks, timing
        t = time.perf_counter()
        tok = self.retriever.embedder.tokenizer
        chunks = parent.expand(chunks, self.retriever.store, lambda s: _tlen(tok, s),
                               k, config.PARENT_MAX_TOKENS)
        timing = dict(timing, parent_ms=round((time.perf_counter() - t) * 1000, 1))
        return chunks, timing

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
        body, _ = parse_answer(raw, len(chunks), echo=config.ECHO_FILTER)
        texts = [c["text"] for c in chunks]
        body, fixes = recite(body, texts)
        return {"answer": body, "cited": _cited(body, len(chunks)), "cite_fixes": fixes, "chunks": chunks,
                "timing": timing, "total_s": round(time.perf_counter() - t0, 2)}
