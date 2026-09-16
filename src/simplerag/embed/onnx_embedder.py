#------------------------------------------------------------------
# ONNX + onnxruntime 임베더 (설계서 결정4 / 결정12)
#=> e5-small-ko 를 ONNX(int8)로 돌려 CPU 에서 384차원 벡터를 만든다.
#   PyTorch 를 안 쓰므로 의존성이 가볍고 완전 오프라인으로 동작한다.
#   무거운 import 는 ensure_loaded() 안으로 미뤄(지연 로딩) 이 모듈 자체는
#   가볍게 import 되게 한다.
#
#   CSOClassify 의 OnnxEmbedder 와 같은 자산(model.onnx/tokenizer.json)을 쓰되,
#   여러 청크를 한 번에 넣는 배치 경로를 둔다.
#------------------------------------------------------------------

import os
import threading
import time

from .. import config
from .base import Embedder, EmbedError


class OnnxEmbedder(Embedder):
    #------------------------------------------------------------------
    # 생성자 — 설정만 보관(모델은 아직 로드 안 함)
    #=> 실제 무거운 로딩은 ensure_loaded() 로 미뤄 시작을 빠르게 한다.
    #
    # -in: spec        = config.EmbedSpec (없으면 기본값)
    # -in: num_threads = onnxruntime intra-op 스레드 수
    #
    # -out: 없음
    #------------------------------------------------------------------
    def __init__(self, spec=None, num_threads=None):
        self.spec = spec or config.EMBED
        self.num_threads = num_threads or config.EMBED_THREADS
        self._session = None
        self._tokenizer = None
        self._input_names = None
        self._load_lock = threading.Lock()    # 중복 로드 방지(예열 스레드와 경합)

    #------------------------------------------------------------------
    # 로드 여부 확인
    #------------------------------------------------------------------
    def is_ready(self):
        return self._session is not None and self._tokenizer is not None

    #------------------------------------------------------------------
    # 모델 적재 보장(지연 로딩) — 핵심
    #=> 처음 호출 때만 tokenizer.json 과 model.onnx 를 올린다. 락으로 감싸
    #   예열 스레드와 질의 스레드가 동시에 들어와도 실제 로딩은 한 번만 한다.
    #    1) 이미 준비됐으면 0.0 반환
    #    2) 무거운 라이브러리를 이 시점에 import
    #    3) 토크나이저 로드 후 '절단 해제'(결정12) → onnxruntime 세션 생성
    #
    # -out: load_ms = 실제 로딩 밀리초(이미 로드면 0.0)
    # -out: error = 파일/라이브러리 없음, 세션 생성 실패 시 EmbedError
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0

        with self._load_lock:
            if self.is_ready():          # 락 대기 중 다른 스레드가 끝냈을 수 있다
                return 0.0

            t0 = time.perf_counter()
            mdir = self.spec.model_dir
            onnx_path = os.path.join(mdir, self.spec.model_file)
            tok_path = os.path.join(mdir, "tokenizer.json")

            if not os.path.isfile(onnx_path):
                raise EmbedError("model.onnx 없음: " + onnx_path)
            if not os.path.isfile(tok_path):
                raise EmbedError("tokenizer.json 없음: " + tok_path)

            try:
                import onnxruntime as ort
                from tokenizers import Tokenizer
            except ImportError as e:
                raise EmbedError("필수 라이브러리 미설치(onnxruntime/tokenizers): {}".format(e))

            try:
                self._tokenizer = Tokenizer.from_file(tok_path)
            except Exception as e:
                raise EmbedError("tokenizer 로딩 실패: {}".format(e))

            # ⚠️ 결정12 — 절단 해제. tokenizer.json 에 max_length=512 가 박혀 있어
            # 이걸 끄지 않으면 청킹 시 문서 앞부분만 남고 나머지가 조용히 사라진다.
            # (청크 하나를 모델에 넣을 때의 512 상한은 _encode_batch 에서 직접 자른다)
            self._tokenizer.no_truncation()

            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            if self.num_threads:
                so.intra_op_num_threads = int(self.num_threads)

            try:
                self._session = ort.InferenceSession(
                    onnx_path, sess_options=so, providers=["CPUExecutionProvider"])
            except Exception as e:
                raise EmbedError("onnxruntime 세션 생성 실패: {}".format(e))

            # 모델마다 token_type_ids 유무가 다르므로 실제 요구 입력만 넣는다.
            self._input_names = {i.name for i in self._session.get_inputs()}
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 토크나이저 접근 (청킹 모듈이 쓴다)
    #=> 절단이 해제된 토크나이저를 돌려준다. 로드 전이면 먼저 올린다.
    #------------------------------------------------------------------
    @property
    def tokenizer(self):
        self.ensure_loaded()
        return self._tokenizer

    #------------------------------------------------------------------
    # 배치 토크나이즈 -> 모델 입력 텐서
    #=> 배치 안에서 가장 긴 시퀀스에 맞춰 패딩한다. 여기서는 모델 상한(512)을
    #   직접 적용한다 — 절단 설정을 껐으므로 방어가 필요하다.
    #
    # -in: texts, prefix
    #
    # -out: (feeds, attention) = 세션 입력 dict, 마스크 배열
    #------------------------------------------------------------------
    def _encode_batch(self, texts, prefix):
        import numpy as np

        cap = self.spec.max_tokens_model
        encs = [self._tokenizer.encode(prefix + t, add_special_tokens=True)
                for t in texts]
        lens = [min(len(e.ids), cap) for e in encs]
        width = max(lens) if lens else 1

        ids = np.zeros((len(encs), width), dtype=np.int64)
        attn = np.zeros((len(encs), width), dtype=np.int64)
        for i, e in enumerate(encs):
            n = lens[i]
            ids[i, :n] = e.ids[:n]
            attn[i, :n] = e.attention_mask[:n]

        feeds = {}
        if "input_ids" in self._input_names:
            feeds["input_ids"] = ids
        if "attention_mask" in self._input_names:
            feeds["attention_mask"] = attn
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        return feeds, attn

    #------------------------------------------------------------------
    # 텍스트 목록 -> 벡터 행렬 (핵심)
    #=> 마스크 가중 평균 풀링 후 L2 정규화 (e5 계열 규약).
    #   패딩 자리를 빼고 평균내지 않으면 짧은 문장의 벡터가 0쪽으로 끌린다.
    #
    # -in: texts  = 원문 목록(프리픽스 미포함)
    # -in: prefix = 'passage: ' 또는 'query: ' (None 이면 문서용)
    #
    # -out: vecs = shape (N, dim) float32, L2 정규화됨
    # -out: error = 추론 실패 시 EmbedError
    #------------------------------------------------------------------
    def embed(self, texts, prefix=None):
        import numpy as np

        self.ensure_loaded()
        if not texts:
            return np.zeros((0, self.spec.dim), dtype=np.float32)

        prefix = self.spec.passage_prefix if prefix is None else prefix
        feeds, attn = self._encode_batch(texts, prefix)

        try:
            hidden = self._session.run(None, feeds)[0]        # (B, T, dim)
        except Exception as e:
            raise EmbedError("추론 실패: {}".format(e))

        m = attn.astype(np.float32)[:, :, None]
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        vecs = summed / counts

        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return (vecs / np.clip(norms, 1e-9, None)).astype(np.float32)

    #------------------------------------------------------------------
    # 문서 청크 목록을 배치로 나눠 임베딩
    #=> 진행 콜백을 받아 인덱싱 진행률/ETA 표시에 쓴다.
    #
    # -in: chunks    = 청크 텍스트 목록
    # -in: batch     = 배치 크기(None 이면 설정값)
    # -in: on_batch  = 배치 끝날 때마다 호출되는 콜백 fn(done, total)
    #
    # -out: vecs = (N, dim)
    #------------------------------------------------------------------
    def embed_passages(self, chunks, batch=None, on_batch=None):
        import numpy as np

        batch = batch or config.EMBED_BATCH
        out = np.empty((len(chunks), self.spec.dim), dtype=np.float32)
        for i in range(0, len(chunks), batch):
            part = chunks[i:i + batch]
            out[i:i + len(part)] = self.embed(part, self.spec.passage_prefix)
            if on_batch:
                on_batch(min(i + batch, len(chunks)), len(chunks))
        return out

    #------------------------------------------------------------------
    # 질의 1건 임베딩
    #=> e5 규약상 질의에는 'query: ' 프리픽스를 붙여야 한다.
    #
    # -out: vec = (dim,) 1차원 벡터
    #------------------------------------------------------------------
    def embed_query(self, query):
        return self.embed([query], self.spec.query_prefix)[0]
