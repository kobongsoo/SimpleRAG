#------------------------------------------------------------------
# GGUF(llama.cpp) 생성기 (설계서 결정1 / 결정2)
#=> Qwen3-0.6B-Q4_K_M 을 llama-cpp-python 으로 CPU 에서 돌린다. 데몬(Ollama 등)을
#   띄우지 않고 임베딩 모델처럼 라이브러리로 로드해 쓴다.
#
#   실측 근거(REPORT)
#    - 스트리밍은 decode 처리량의 약 30% 를 먹지만(25.9 vs 37.0 t/s) 체감 속도를
#      위해 감수한다. 첫 글자가 2.75초에 나오는 것이 완료 시간보다 중요하다.
#    - flash_attn 은 CPU 에서 prefill 이득 / decode 손해가 상반되는데, RAG 는
#      프롬프트가 길어 prefill 이 TTFT 를 지배하므로 켜는 쪽이 총합에서 유리하다.
#------------------------------------------------------------------

import inspect
import os
import threading
import time

from .. import config
from .base import Generator, GenerateError
from .prompts import active_system_prompt, build_prompt, build_user_message


class GgufGenerator(Generator):
    #------------------------------------------------------------------
    # 생성자 — 설정만 보관(모델은 아직 로드 안 함)
    #
    # -in: model_alias = config.GEN_MODELS 의 키
    # -in: n_threads / n_ctx = 없으면 설정값
    # -in: backend = "cpu" | "vulkan" (None 이면 "cpu"). App.warmup() 이
    #                backend.select() 결과로 채운다. 이 값은 적재 인자만 바꾼다 —
    #                어느 DLL 을 읽을지는 select() 가 환경변수로 이미 정해 둔다.
    #------------------------------------------------------------------
    def __init__(self, model_alias=None, n_threads=None, n_ctx=None, backend=None):
        self.alias = model_alias or config.DEFAULT_GEN_MODEL
        self.n_threads = n_threads or config.GEN_THREADS
        self.n_ctx = n_ctx or config.GEN_N_CTX
        self.backend = backend or "cpu"
        self.last_warm_ms = None
        self._llm = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()   # 세션 공유 — 추론은 직렬화한다

    def is_ready(self):
        return self._llm is not None

    #------------------------------------------------------------------
    # 모델 적재 보장(지연 로딩)
    #=> llama-cpp-python 은 버전마다 생성자 인자가 달라(flash_attn 등) 실제
    #   시그니처에 있는 것만 골라 넘긴다. 없는 인자로 죽지 않게 하기 위함.
    #
    # -out: load_ms = 로딩 밀리초(이미 로드면 0.0)
    # -out: error = 모델 파일/라이브러리 없음 시 GenerateError
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0

        with self._load_lock:
            if self.is_ready():
                return 0.0

            path = config.gen_model_path(self.alias)
            if not os.path.isfile(path):
                raise GenerateError(
                    "생성 모델 없음: {}\n        bench/download_models.py 로 내려받으세요."
                    .format(path))

            try:
                import llama_cpp
                from llama_cpp import Llama
            except ImportError as e:
                raise GenerateError("llama-cpp-python 미설치: {}".format(e))

            # iGPU 로 정했는데 실제로 올라온 DLL 이 CPU 판이면(환경변수 누락 등)
            # 레이어를 올릴 곳이 없다. 틀린 라벨로 돌지 않게 CPU 로 바로잡는다.
            if self.backend == "vulkan" and not llama_cpp.llama_supports_gpu_offload():
                self.backend = "cpu"
            gpu = self.backend == "vulkan"

            # ⚠️ 백엔드별 인자는 반드시 여기서 명시한다. 아래 서명 필터는
            #    Llama.__init__ 에 있는 이름만 통과시키므로, 하위 클래스로 덮어
            #    넣으면 model_path 까지 걸러져 적재가 실패한다(REPORT §31.5).
            wanted = {
                "model_path": path,
                "n_ctx": self.n_ctx,
                "n_threads": self.n_threads,
                "n_threads_batch": self.n_threads,
                "n_batch": config.GEN_N_BATCH,
                "n_gpu_layers": config.GEN_GPU_LAYERS if gpu else 0,
                "flash_attn": config.GEN_FLASH_ATTN_GPU if gpu else config.GEN_FLASH_ATTN,
                "logits_all": False,
                "verbose": False,
            }
            sig = set(inspect.signature(Llama.__init__).parameters)
            kwargs = {k: v for k, v in wanted.items() if k in sig}

            t0 = time.perf_counter()
            try:
                self._llm = Llama(**kwargs)
            except Exception as e:
                raise GenerateError("모델 로딩 실패: {}".format(e))
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 예열 생성 — 첫 질문을 느리게 만드는 일회성 비용을 미리 치른다
    #=> 모델만 올려 두면 첫 질문에서 두 비용이 한꺼번에 나온다.
    #    1) Vulkan 셰이더 컴파일 — 새 PC 첫 실행에서 9.75초(REPORT §31.5).
    #       드라이버가 캐시해 두 번째 실행부터는 거의 0이다(§32 실측).
    #    2) 시스템 프롬프트 prefill — llama.cpp 는 앞부분이 같은 프롬프트의 KV 를
    #       재사용한다. 운영 지시문으로 한 번 돌려 두면 첫 질문도 두 번째 질문처럼
    #       그 부분을 건너뛴다.
    #   답은 버린다(1토큰). 결과가 아니라 부수 효과가 목적이다.
    #
    # -in: 없음
    #
    # -out: warm_ms = 예열 생성 밀리초(last_warm_ms 에도 남긴다)
    # -out: error = 적재 실패 시 GenerateError 전파
    #------------------------------------------------------------------
    def warm(self):
        self.ensure_loaded()
        t0 = time.perf_counter()
        for _ in self.stream("예열 질문입니다.", ["예열용 근거 문장입니다."] * 3,
                             max_tokens=1):
            pass
        self.last_warm_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return self.last_warm_ms

    #------------------------------------------------------------------
    # 컨텍스트에 맞춰 프롬프트 만들기 (안전장치)
    #=> 근거가 길면 프롬프트가 n_ctx(2048)를 넘어 llama.cpp 가 ValueError 로
    #   죽는다. 실제로 터졌다 — "Requested tokens (4061) exceed context
    #   window of 2048". 원인은 구조 청킹(CHUNK_MODE=structured)이 표/절을
    #   통째로 유지하기 때문인데, 인덱스 59,747청크 중 17개가 3,000자를 넘고
    #   최대 5,526자다. 이런 청크 하나가 근거로 뽑히면 그것만으로 컨텍스트를
    #   넘긴다. 평가 506문항 중 기준선은 우연히 안 걸렸고 리랭킹은 걸렸다.
    #
    #   왜 '자르기'인가 — 근거를 버리면(청크 수 축소) 인용 번호가 어긋나고,
    #   n_ctx 를 키우면 전 질의의 prefill 이 느려진다. 긴 근거의 뒷부분만
    #   잘라내면 청크 개수와 순서가 그대로라 인용 번호가 유지된다.
    #
    #   ⚠️ 기존 측정값과의 호환 — 넘칠 때만 자른다. 지금까지 정상 동작하던
    #      질의는 토크나이저조차 부르지 않고 완전히 같은 프롬프트를 만든다
    #      (아래 '싼 검사' 참고). 그래서 과거 벤치 결과가 무효화되지 않는다.
    #
    # -in: system     = 시스템 지시문
    # -in: query      = 사용자 질문
    # -in: chunks     = 근거 텍스트 목록
    # -in: max_tokens = 생성할 답변 토큰 상한(이만큼 자리를 비워둬야 한다)
    #
    # -out: prompt = n_ctx 안에 들어가는 프롬프트 문자열
    # -out: error = 없음 (아무리 잘라도 안 들어가면 최선의 시도를 그대로 반환)
    #------------------------------------------------------------------
    def _fit_prompt(self, system, query, chunks, max_tokens):
        # 답변용 자리 + 여유(BOS/특수토큰 등)를 뺀 나머지가 프롬프트 예산
        budget = max(128, self.n_ctx - max_tokens - 16)

        texts = list(chunks)
        prompt = build_prompt(system, build_user_message(query, texts))

        # 싼 검사 — 한글/영문 어느 쪽이든 1토큰이 1자보다 짧아지진 않으므로
        # '글자 수 <= 예산'이면 토큰 수도 반드시 예산 이하다. 대부분의 질의가
        # 여기서 끝나 토크나이저 호출 비용이 0이다(중앙값 근거 223자 x 3).
        if len(prompt) <= budget:
            return prompt

        # 여기부터는 진짜로 셀 수밖에 없다.
        for _ in range(60):
            n_tok = len(self._llm.tokenize(prompt.encode("utf-8"), special=True))
            if n_tok <= budget:
                return prompt
            # 가장 긴 근거를 20%씩 깎는다 — 짧은 근거는 건드리지 않는다
            i = max(range(len(texts)), key=lambda k: len(texts[k]))
            cut = int(len(texts[i]) * 0.8)
            if cut < 40:
                break
            texts[i] = texts[i][:cut]
            prompt = build_prompt(system, build_user_message(query, texts))

        return prompt

    #------------------------------------------------------------------
    # 스트리밍 생성 (핵심)
    #=> 토큰이 나오는 대로 흘려보낸다. 호출자가 첫 조각을 받는 시점이 TTFT 다.
    #
    #   llm.reset() 을 부르지 않는다 — llama.cpp 는 프롬프트 앞부분이 이전과
    #   일치하는 만큼 KV 를 재사용하므로, 시스템 프롬프트가 고정인 RAG 에서는
    #   그만큼 prefill 을 아낄 수 있다(벤치에서는 '차가운' 측정을 위해 일부러
    #   reset 했지만, 운영에서는 재사용이 이득이다).
    #
    # -in: query      = 사용자 질문
    # -in: chunks     = 근거 청크 텍스트 목록
    # -in: max_tokens = 답변 토큰 상한(None 이면 설정값)
    # -in: system     = 시스템 지시문(None 이면 설정 generation.prompt 의 지시문, 기본 V0)
    #
    # -out: Iterator[str] = 답변 조각
    # -out: error = 로딩 실패 시 GenerateError
    #------------------------------------------------------------------
    def stream(self, query, chunks, max_tokens=None, system=None):
        self.ensure_loaded()

        n_out = max_tokens or config.GEN_MAX_TOKENS
        # 긴 근거가 들어와도 컨텍스트를 넘겨 죽지 않게 맞춰준다
        # 지시문은 설정에서 고른다(계획서 1-3 실험) — 기본 V0 는 종전과 글자까지 같다
        prompt = self._fit_prompt(system or active_system_prompt(), query, chunks, n_out)

        with self._infer_lock:
            for piece in self._llm.create_completion(
                prompt,
                max_tokens=n_out,
                temperature=config.GEN_TEMPERATURE,
                stop=["<|im_end|>", "<|endoftext|>"],
                stream=True,
            ):
                text = piece["choices"][0]["text"]
                if text:
                    yield text

    #------------------------------------------------------------------
    # 비스트리밍 생성 (배치/평가용)
    #=> 스트리밍 오버헤드(약 30%)가 없어 일괄 처리에 유리하다.
    #
    # -out: text = 답변 전문
    #------------------------------------------------------------------
    def generate(self, query, chunks, max_tokens=None, system=None):
        self.ensure_loaded()

        n_out = max_tokens or config.GEN_MAX_TOKENS
        prompt = self._fit_prompt(system or active_system_prompt(), query, chunks, n_out)
        with self._infer_lock:
            r = self._llm.create_completion(
                prompt,
                max_tokens=n_out,
                temperature=config.GEN_TEMPERATURE,
                stop=["<|im_end|>", "<|endoftext|>"],
            )
        return r["choices"][0]["text"]
