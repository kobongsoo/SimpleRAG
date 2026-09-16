#------------------------------------------------------------------
# 구성요소 조립 + 병렬 예열 (설계서 결정9)
#=> 네 구성요소를 순차로 올리면 첫 질문이 4.6초 늦어진다(실측: Qdrant 2.35s
#   + 임베더 0.83s + BM25 0.76s + LLM 0.66s). 별도 스레드에서 동시에 올리면
#   가장 느린 하나(약 2.4초)로 수렴한다.
#
#   각 구성요소는 락으로 보호된 ensure_loaded() 를 갖고 있어, 예열 중에
#   사용자가 질문해도 중복 로드 없이 대기했다가 이어진다.
#------------------------------------------------------------------

import threading
import time

from . import config
from .embed.onnx_embedder import OnnxEmbedder
from .generate import backend as gen_backend
from .generate.gguf_generator import GgufGenerator
from .index.bm25 import Bm25Index
from .index.store import VectorStore
from .pipeline import RagPipeline
from .retrieve.hybrid import HybridRetriever
from .retrieve.reranker import Reranker


class App:
    #------------------------------------------------------------------
    # 생성자 — 구성요소 조립만(로딩 없음)
    #
    # -in: model_alias = 생성 모델 별칭(None 이면 기본 0.6B)
    #------------------------------------------------------------------
    def __init__(self, model_alias=None):
        self.embedder = OnnxEmbedder()
        self.store = VectorStore()
        self.bm25 = Bm25Index()
        self.generator = GgufGenerator(model_alias or config.DEFAULT_GEN_MODEL)
        # 리랭커는 만들어만 둔다 — 켤지는 warmup() 이 생성 백엔드를 정한 뒤 결정한다.
        # 토크나이저는 임베더가 파싱해 둔 것을 빌려 조립한다(파싱 0.88초 GIL 점유 제거, §33)
        self.reranker = Reranker(tokenizer_source=self.embedder)
        self.retriever = HybridRetriever(self.embedder, self.store, self.bm25)
        self.pipeline = RagPipeline(self.retriever, self.generator)
        self.backend = None      # warmup() 이 backend.select() 결과로 채운다
        # 백그라운드 리랭커 적재 결과: None(안 함·진행 중) | 적재 ms | "에러문자열"
        self.rerank_load = None
        self._rerank_thread = None

    #------------------------------------------------------------------
    # 병렬 예열 (핵심)
    #=> 구성요소를 각각 스레드로 올린다. 실패는 삼키지 않고 모아서 돌려준다
    #   — LLM 만 실패했는데 조용히 넘어가면 질의 시점에야 알게 된다.
    #    1) 생성 백엔드(CPU/iGPU)를 먼저 정한다 — llama_cpp 를 import 하기 전에
    #       정해야 DLL 을 고를 수 있다. 저장된 측정이 있으면 즉시, 없으면 측정
    #       (처음 한 번 약 15~40초, REPORT §32).
    #    2) 백엔드에 따라 리랭킹을 켤지 정한다(config.RERANK, 기본 auto = iGPU 일 때만)
    #    3) 임베더·Qdrant·BM25·(리랭커)·LLM 을 동시에 올린다. LLM 은 올린 뒤
    #       예열 생성까지 한다(셰이더 컴파일·시스템 프롬프트 KV)
    #
    # -in: wait      = True 면 전부 끝날 때까지 기다린다
    # -in: skip_llm  = True 면 생성 모델은 올리지 않는다(인덱싱·검색 전용 경로).
    #                  이때는 측정하지 않고 저장된 결과만 보고 리랭킹을 정한다
    # -in: on_log    = 진행 안내를 받을 함수(None 이면 조용히)
    # -in: rerank    = None 이면 설정·백엔드를 따르고, False 면 끈다(인덱싱처럼
    #                  검색을 안 하는 명령이 280MB 모델을 괜히 올리지 않게)
    #
    #   리랭커는 기본적으로 병렬 예열에 넣지 않고 **준비 완료 뒤 백그라운드로** 올린다
    #   (config.RERANK_DEFER_LOAD, §34). 적재 결과는 rerank_load / wait_rerank() 로 본다.
    #
    # -out: dict = {구성요소: 로딩ms 또는 "에러문자열", "_backend", "_rerank",
    #               "_rerank_deferred", "llm_warm"(예열 생성 ms), "_total_ms"(= 준비 완료)},
    #               wait=False 면 {}
    # -out: error = 없음 (구성요소 예외는 문자열로 담아 돌려준다)
    #------------------------------------------------------------------
    def warmup(self, wait=True, skip_llm=False, on_log=None, rerank=None):
        results = {}
        lock = threading.Lock()

        def run(name, fn):
            try:
                ms = fn()
            except Exception as e:
                ms = "{}: {}".format(type(e).__name__, str(e)[:200])
            with lock:
                results[name] = ms

        if skip_llm:
            # LLM 을 안 쓰는 명령에서 20초짜리 측정을 돌리지 않는다
            cached = gen_backend.peek(self.generator.alias)
            backend = cached["backend"] if cached else "cpu"
        else:
            self.backend = gen_backend.select(self.generator.alias, on_log=on_log)
            backend = self.backend["backend"]
            self.generator.backend = backend

        use_rerank = gen_backend.rerank_enabled(backend) if rerank is None else bool(rerank)
        self.retriever.reranker = self.reranker if use_rerank else None

        jobs = [
            ("embedder", self.embedder.ensure_loaded),
            ("qdrant", self.store.ensure_loaded),
            ("bm25", self.bm25.ensure_loaded),
        ]
        # 리랭커 적재는 GIL 을 쥐어 파이썬으로 도는 Qdrant 적재를 멈춘다(§33) — 병렬로 넣지
        # 않고 준비 완료 뒤로 미룬다. 설정으로 끄면 종전처럼 함께 올린다.
        defer = use_rerank and config.RERANK_DEFER_LOAD
        if use_rerank and not defer:
            jobs.append(("reranker", self.reranker.ensure_loaded))
        if not skip_llm:
            jobs.append(("llm", self._load_llm))

        t0 = time.perf_counter()
        threads = [threading.Thread(target=run, args=(n, f), daemon=True)
                   for n, f in jobs]
        for t in threads:
            t.start()
        if not wait:
            if defer:
                # 호출자를 막지 않되, 리랭커는 핵심 구성요소가 다 오른 뒤에 시작한다
                self._start_rerank_load(after=threads)
            return {}
        for t in threads:
            t.join()
        if defer:
            self._start_rerank_load()

        # 리랭커만 실패했으면 질의는 RRF 로 계속 받는다 — 매 질의 실패를 막으려 뗀다
        if isinstance(results.get("reranker"), str):
            self.retriever.reranker = None

        # 적재 중 DLL 불일치로 CPU 로 바로잡혔을 수 있어 생성기 값을 최종으로 본다
        results["_backend"] = backend if skip_llm else self.generator.backend
        results["_rerank"] = self.retriever.reranker is not None
        results["_rerank_deferred"] = defer
        if self.generator.last_warm_ms is not None:
            results["llm_warm"] = self.generator.last_warm_ms
        results["_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return results

    #------------------------------------------------------------------
    # LLM 적재 + 예열 생성 (예열 스레드 안에서 돈다)
    #=> 적재 ms 만 돌려주고 예열 ms 는 generator.last_warm_ms 에 남긴다 —
    #   두 비용을 따로 보고해야 어디서 느린지 가를 수 있다.
    #
    # -in: 없음
    #
    # -out: load_ms = 모델 적재 밀리초
    # -out: error = 적재·예열 실패 시 예외 전파(run 이 문자열로 담는다)
    #------------------------------------------------------------------
    def _load_llm(self):
        ms = self.generator.ensure_loaded()
        self.generator.warm()
        return ms

    #------------------------------------------------------------------
    # 리랭커 백그라운드 적재 시작 (준비 완료 뒤)
    #=> 리랭커 ONNX 세션 생성(약 0.6초)은 GIL 을 쥔다. 병렬 예열 중에 돌면 파이썬으로 도는
    #   Qdrant 적재가 그만큼 멈춰 준비가 늦어졌다(§33). 준비 완료 뒤로 미루면 chat 은 그만큼
    #   빨리 입력을 받는다.
    #   첫 질문이 적재 중에 오면 Reranker.ensure_loaded() 의 락에서 기다렸다가 리랭킹한다 —
    #   결과는 같고 그 질문만 늦는다. ask 는 준비 직후 바로 묻기 때문에 총시간은 줄지 않는다.
    #    1) after 스레드(아직 예열 중인 핵심 구성요소)가 끝나기를 기다린다
    #    2) 리랭커를 올리고 결과(ms 또는 에러)를 rerank_load 에 남긴다
    #    3) 실패하면 리트리버에서 떼어 이후 질의는 RRF 로 답한다(매 질의 실패 방지)
    #
    # -in: after = 먼저 끝나야 하는 스레드 목록(wait=False 경로에서만 넘긴다)
    #
    # -out: 없음 (데몬 스레드 시작)
    # -out: error = 없음 (적재 실패는 rerank_load 에 문자열로 남는다)
    #------------------------------------------------------------------
    def _start_rerank_load(self, after=()):
        # 백그라운드 본체 — 선행 스레드를 기다린 뒤 리랭커를 올린다
        def job():
            for t in after:
                t.join()
            try:
                self.rerank_load = self.reranker.ensure_loaded()
            except Exception as e:
                self.rerank_load = "{}: {}".format(type(e).__name__, str(e)[:200])
                self.retriever.reranker = None

        self._rerank_thread = threading.Thread(target=job, name="rerank-load", daemon=True)
        self._rerank_thread.start()

    #------------------------------------------------------------------
    # 백그라운드 리랭커 적재 기다리기
    #=> warmup 명령이 적재 시간을 보여 줄 때처럼, 끝난 결과가 꼭 필요할 때 쓴다.
    #
    # -in: timeout = 최대 대기 초(None 이면 끝날 때까지)
    #
    # -out: 적재 ms | "에러문자열" | None(백그라운드 적재를 안 했거나 시간 안에 안 끝남).
    #       질의가 먼저 올렸다면 0.0
    # -out: error = 없음
    #------------------------------------------------------------------
    def wait_rerank(self, timeout=None):
        if self._rerank_thread is not None:
            self._rerank_thread.join(timeout)
        return self.rerank_load

    #------------------------------------------------------------------
    # 정리
    #=> Qdrant local 모드는 폴더 락을 잡으므로 반드시 놓아 준다.
    #------------------------------------------------------------------
    def close(self):
        self.store.close()
