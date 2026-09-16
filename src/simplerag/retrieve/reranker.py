#------------------------------------------------------------------
# 리랭커 — bge-reranker-base int8 ONNX (REPORT §27, §29, §31)
#=> RRF 로 합친 후보 RERANK_POOL(기본 10)건을 크로스인코더로 다시 점수 매겨
#   상위 TOP_K(3)건을 고른다.
#
#   임베딩(바이인코더)과 무엇이 다른가
#     임베딩은 질의와 문서를 **따로** 벡터로 만들어 거리만 잰다. 빠르지만
#     "질문에 딱 맞는 답이 들어 있는가"를 세밀하게 보지 못한다.
#     크로스인코더는 질의와 근거를 **한 문장으로 붙여** 모델에 넣고 관련성
#     점수 하나를 낸다. 정확하지만 후보마다 모델을 돌려야 해서 전체 코퍼스에는
#     못 쓰고, 이미 좁혀진 5건을 재정렬하는 데만 쓴다.
#
#   실측(506문항): 정답률 +14~15문항(CPU·iGPU 세 번 모두 + 방향, 비유의),
#   검색 지연 약 +0.45초(§29.3). exp5_rerank/reranker.py 와 같은 설정이다 —
#   운영이 실험 수치를 재현하도록 토크나이저 절단·패딩·스레드를 바꾸지 않았다.
#------------------------------------------------------------------

import os
import threading
import time

from .. import config

# 토크나이저 재사용을 검증한 파일 쌍(바이트 크기로 식별, REPORT §33).
#   e5-small-ko 와 bge-reranker-base 의 tokenizer.json 은 model(250,002 Unigram)·
#   normalizer·pre_tokenizer·post_processor·decoder 가 같고, 절단·패딩 설정과 <mask>
#   플래그만 다르다. 모델 파일을 바꾸면 크기가 달라져 자동으로 파일 파싱으로 돌아간다.
_SHARE_VERIFIED_BYTES = {"embed": 17083053, "rerank": 17082798}
# XLM-R 특수토큰 id — 원천 토크나이저가 이와 다르면 재사용하지 않는다
_XLMR_SPECIAL_IDS = {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "<mask>": 250001}


class RerankError(Exception):
    """리랭커 적재 실패(모델 파일 없음 등)."""


class Reranker:
    #------------------------------------------------------------------
    # 생성자 — 경로만 보관(모델은 아직 로드 안 함)
    #
    # -in: model_dir = 모델 폴더(None 이면 config.RERANK_DIR)
    # -in: threads   = ONNX 스레드 수(None 이면 config.RERANK_THREADS = 임베딩과 같은 4)
    # -in: tokenizer_source = 토크나이저를 빌려 올 임베더(OnnxEmbedder). None 이면
    #                         늘 tokenizer.json 을 직접 파싱한다
    #
    # -out: 없음
    # -out: error = 없음
    #------------------------------------------------------------------
    def __init__(self, model_dir=None, threads=None, tokenizer_source=None):
        self.model_dir = model_dir or config.RERANK_DIR
        self.threads = threads or config.RERANK_THREADS
        self.tokenizer_source = tokenizer_source
        self.tokenizer_shared = None      # 적재 후: 임베더 구성요소를 재사용했는가
        self._sess = None
        self._tok = None
        self._lock = threading.Lock()

    def is_ready(self):
        return self._sess is not None

    #------------------------------------------------------------------
    # 모델 적재 보장 (지연 로딩)
    #=> 예열 스레드와 질의 스레드가 동시에 불러도 한 번만 올린다.
    #   세션을 맨 마지막에 넣어, is_ready() 가 반쯤 올라온 상태를 보지 않게 한다.
    #
    # -in: 없음
    #
    # -out: load_ms = 적재 밀리초(이미 올라와 있으면 0.0)
    # -out: error = 모델/토크나이저 파일이 없으면 RerankError
    #------------------------------------------------------------------
    def ensure_loaded(self):
        if self.is_ready():
            return 0.0
        with self._lock:
            if self.is_ready():
                return 0.0

            onnx_path = os.path.join(self.model_dir, "model_int8.onnx")
            tok_path = os.path.join(self.model_dir, "tokenizer.json")
            for p in (onnx_path, tok_path):
                if not os.path.isfile(p):
                    raise RerankError("리랭커 모델 없음: " + p)

            import onnxruntime as ort
            from tokenizers import Tokenizer

            t0 = time.perf_counter()
            opts = ort.SessionOptions()
            # 임베딩과 같은 근거 — P-core 4개가 실질 상한(REPORT §8.1)
            opts.intra_op_num_threads = self.threads
            opts.inter_op_num_threads = 1
            sess = ort.InferenceSession(onnx_path, sess_options=opts,
                                        providers=["CPUExecutionProvider"])
            tok, shared = self._build_tokenizer(tok_path)
            tok.enable_truncation(max_length=config.RERANK_MAX_TOKENS)
            tok.enable_padding(pad_id=1, pad_token="<pad>")   # XLM-R 의 pad id

            self._tok = tok
            self.tokenizer_shared = shared
            self._sess = sess
            return (time.perf_counter() - t0) * 1000.0

    #------------------------------------------------------------------
    # 임베더 토크나이저를 빌려 써도 되는가
    #=> 하나라도 못 맞추면 None — 조용히 틀린 토큰을 만드느니 0.9초를 쓴다.
    #    1) config.RERANK_SHARE_TOKENIZER 가 켜져 있고 원천(임베더)이 주어졌는가
    #    2) 두 tokenizer.json 이 검증한 파일(바이트 크기)과 같은가
    #    3) 원천의 어휘 수가 250,002 이고 특수토큰 id 가 XLM-R 과 같은가
    #
    # -in: 없음
    #
    # -out: 원천 Tokenizer 또는 None
    # -out: error = 없음 (파일 없음·원천 적재 실패도 None 으로 처리한다)
    #------------------------------------------------------------------
    def _shareable_source(self):
        if not config.RERANK_SHARE_TOKENIZER or self.tokenizer_source is None:
            return None
        try:
            embed_tok = os.path.join(self.tokenizer_source.spec.model_dir, "tokenizer.json")
            rerank_tok = os.path.join(self.model_dir, "tokenizer.json")
            if (os.path.getsize(embed_tok) != _SHARE_VERIFIED_BYTES["embed"]
                    or os.path.getsize(rerank_tok) != _SHARE_VERIFIED_BYTES["rerank"]):
                return None
            # 임베더가 아직 적재 중이면 여기서 기다린다(두 번 파싱하지 않기 위해)
            base = self.tokenizer_source.tokenizer
        except Exception:
            return None
        if base.get_vocab_size() != 250002:
            return None
        if any(base.token_to_id(k) != v for k, v in _XLMR_SPECIAL_IDS.items()):
            return None
        return base

    #------------------------------------------------------------------
    # 토크나이저 준비 — 임베더가 파싱해 둔 구성요소를 재사용 (기동 시간)
    #=> tokenizer.json(17MB) 파싱은 0.88초 동안 **GIL 을 통째로 쥔다**(REPORT §33 실측,
    #   틱 0%). 병렬 예열 중이면 그동안 Qdrant 적재(순수 파이썬)가 멈춰 기동이 그만큼
    #   늦어진다. 리랭커를 켜자 기동이 1.5초 늘어난 원인의 절반이 이것이다.
    #    1) 재사용할 수 없으면(_shareable_source 가 None) 파일을 파싱한다 — 종전과 같다
    #    2) 원천의 model·normalizer·pre_tokenizer·post_processor·decoder 를 그대로 붙인다.
    #       새 Tokenizer 객체라 임베더 쪽 설정(절단 해제)은 건드리지 않는다
    #    3) 특수토큰을 다시 등록하되 <mask> 는 리랭커 파일의 플래그(lstrip·normalized)로
    #   동등성: 질문×청크 2,578쌍(초장문 절단·특수문자열 포함) ids/attention_mask
    #   불일치 0건, 한 쌍씩·5개 배치 모두(REPORT §33).
    #
    # -in: tok_path = 리랭커 tokenizer.json 경로(재사용하지 못할 때 파싱)
    #
    # -out: (tokenizer, shared) = 절단·패딩 설정 전 토크나이저, 재사용 여부
    # -out: error = 파일 파싱 실패 시 tokenizers 예외 전파
    #------------------------------------------------------------------
    def _build_tokenizer(self, tok_path):
        from tokenizers import AddedToken, Tokenizer

        base = self._shareable_source()
        if base is None:
            return Tokenizer.from_file(tok_path), False

        tok = Tokenizer(base.model)
        tok.normalizer = base.normalizer
        tok.pre_tokenizer = base.pre_tokenizer
        tok.post_processor = base.post_processor
        tok.decoder = base.decoder

        added = base.get_added_tokens_decoder()
        specials = []
        for i in sorted(added):
            t = added[i]
            # 두 파일에서 유일하게 다른 항목 — 리랭커 tokenizer.json 의 플래그로 맞춘다
            if t.content == "<mask>":
                t = AddedToken("<mask>", single_word=False, lstrip=True, rstrip=False,
                               normalized=True, special=True)
            specials.append(t)
        tok.add_special_tokens(specials)
        return tok, True

    #------------------------------------------------------------------
    # 질의-근거 쌍 점수 매기기 (핵심)
    #=> 후보 전부를 한 배치로 넣는다(5건이면 forward 1회).
    #    1) (질의, 근거) 쌍을 토큰화 — 모델이 두 문장 사이에 구분 토큰을 넣는다
    #    2) 짧은 쌍은 패딩해 길이를 맞춘다
    #    3) 쌍마다 로짓 1개 = 관련성 점수(클수록 관련)
    #
    # -in: query    = 사용자 질문
    # -in: passages = 근거 텍스트 목록
    #
    # -out: scores = passages 순서대로의 점수 리스트(빈 입력이면 [])
    # -out: error = 적재 실패 시 RerankError, 추론 실패 시 onnxruntime 예외 전파
    #------------------------------------------------------------------
    def score(self, query, passages):
        self.ensure_loaded()
        if not passages:
            return []

        import numpy as np

        enc = self._tok.encode_batch([(query, p) for p in passages])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        logits = self._sess.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        return logits.reshape(-1).tolist()
