#------------------------------------------------------------------
# 리랭커 토크나이저 재사용 회귀 테스트 (REPORT §33)
#=> 기동 시간을 줄이려고 리랭커가 tokenizer.json 을 직접 파싱하지 않고 임베더가
#   파싱해 둔 구성요소로 조립하게 바꿨다. 이 파일은 그 지름길이 **한 토큰도 다르지
#   않은지**, 그리고 조건이 안 맞으면 종전 경로(파일 파싱)로 돌아가는지 본다.
#
#   왜 점수까지 비교하는가
#     ids 가 같으면 ONNX 입력이 바이트 단위로 같으므로 점수도 같아야 한다. 그걸
#     실제로 확인해 두면 "토크나이저만 봤다"는 빈틈이 없다.
#
#   실행: .venv/Scripts/python.exe bench/test_reranker_tokenizer.py
#         (임베더·리랭커 모델을 실제로 올린다. 인덱스는 쓰지 않는다)
#------------------------------------------------------------------

import sys

sys.path.insert(0, "src")
sys.path.insert(0, "bench")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                              # noqa: E402
from simplerag.embed.onnx_embedder import OnnxEmbedder    # noqa: E402
from simplerag.retrieve import reranker as R              # noqa: E402

FAILED = []
TOTAL = [0]


#------------------------------------------------------------------
# 단언 도우미 — 실패해도 멈추지 않고 끝까지 돌린다
#
# -in: name = 테스트 이름
# -in: cond = 참이어야 하는 조건
# -in: note = 실패 시 함께 출력할 설명
#
# -out: 없음
# -out: error = 없음 (실패는 FAILED 에 쌓는다)
#------------------------------------------------------------------
def check(name, cond, note=""):
    TOTAL[0] += 1
    print(("  OK   %s" if cond else "  FAIL %s  " + str(note)) % name)
    if not cond:
        FAILED.append(name)


#------------------------------------------------------------------
# 비교용 질문-근거 쌍 만들기
#=> 인덱스 없이 만들 수 있는 쌍만 쓴다(Qdrant 잠금을 피하려고).
#    1) 평가 506문항 질문 × 다른 질문을 이어 붙인 가짜 근거
#    2) 512토큰을 넘기는 초장문(절단 경로)
#    3) 특수토큰 문자열·빈 문자열·전각/호환 문자(정규화 경로)
#
# -in: 없음
#
# -out: [(query, passage), ...]
# -out: error = 없음
#------------------------------------------------------------------
def make_pairs():
    from eval_cases_v3 import CASES
    qs = [c["q"] for c in CASES]
    pairs = [(q, " ".join(qs[(i + k) % len(qs)] for k in range(1, 4))) for i, q in enumerate(qs)]
    long_ko = "출장여비 규정에 따라 숙박비 상한액은 서울특별시 70,000원으로 한다. " * 120
    pairs += [(qs[i], long_ko) for i in range(20)]
    pairs += [("<s> 질문 <mask> </s>", "<pad> 본문 <unk> <s></s> " * 50), ("", "빈 질문"),
              ("질문만", ""), ("  공백  앞뒤  ", "\t탭\n줄바꿈　전각공백"),
              ("ＡＢＣ①㈜", "ｶﾀｶﾅ ﬁ ligature é é"), ("a<mask>b", "  <mask>  "),
              ("<MASK> <Mask>", "<mask><mask> <mask>"), ("앞 <mask>", "<mask> 뒤")]
    return pairs


def main():
    emb = OnnxEmbedder()
    emb.ensure_loaded()
    emb_trunc_before = emb.tokenizer.truncation
    emb_ids_before = emb.tokenizer.encode("query: 임베더 토크나이저가 바뀌면 안 된다").ids

    print("\n[경로 선택]")
    shared = R.Reranker(tokenizer_source=emb)
    shared.ensure_loaded()
    check("임베더를 주면 구성요소 재사용", shared.tokenizer_shared is True, shared.tokenizer_shared)

    own = R.Reranker()
    own.ensure_loaded()
    check("원천이 없으면 파일 파싱(종전 경로)", own.tokenizer_shared is False, own.tokenizer_shared)

    old = config.RERANK_SHARE_TOKENIZER
    config.RERANK_SHARE_TOKENIZER = False
    try:
        off = R.Reranker(tokenizer_source=emb)
        off.ensure_loaded()
        check("SIMPLERAG_RERANK_SHARE_TOKENIZER=0 → 파일 파싱", off.tokenizer_shared is False)
    finally:
        config.RERANK_SHARE_TOKENIZER = old

    saved = dict(R._SHARE_VERIFIED_BYTES)
    R._SHARE_VERIFIED_BYTES["rerank"] = 1
    try:
        other = R.Reranker(tokenizer_source=emb)
        other.ensure_loaded()
        check("검증 안 된 파일(크기 불일치) → 파일 파싱", other.tokenizer_shared is False)
    finally:
        R._SHARE_VERIFIED_BYTES.clear()
        R._SHARE_VERIFIED_BYTES.update(saved)

    print("\n[동등성 — 토큰과 점수]")
    pairs = make_pairs()
    bad_single = sum(1 for p in pairs
                     if shared._tok.encode(*p).ids != own._tok.encode(*p).ids
                     or shared._tok.encode(*p).attention_mask != own._tok.encode(*p).attention_mask)
    check("ids/attention_mask 한 쌍씩 불일치 0 (%d쌍)" % len(pairs), bad_single == 0, bad_single)

    bad_batch = 0
    bad_score = 0
    trunc = 0
    for i in range(0, len(pairs), 5):
        batch = pairs[i:i + 5]
        for x, y in zip(shared._tok.encode_batch(batch), own._tok.encode_batch(batch)):
            bad_batch += (x.ids != y.ids or x.attention_mask != y.attention_mask)
            trunc += (len(x.ids) == config.RERANK_MAX_TOKENS)
        if i < 150:      # 점수는 앞쪽 30배치(150쌍)만 — ONNX 추론이 느려서
            q = batch[0][0]
            ps = [p for _, p in batch]
            if shared.score(q, ps) != own.score(q, ps):
                bad_score += 1
    check("ids/attention_mask 5개 배치 불일치 0", bad_batch == 0, bad_batch)
    check("512 절단 경로가 실제로 시험됨(%d건)" % trunc, trunc >= 20, trunc)
    check("리랭커 점수 완전 일치(30배치, 부동소수 ==)", bad_score == 0, bad_score)

    print("\n[임베더 무결성]")
    check("임베더 토크나이저 절단 설정 그대로(해제 상태)",
          emb.tokenizer.truncation == emb_trunc_before and emb_trunc_before is None,
          emb.tokenizer.truncation)
    check("임베더 토큰 결과 그대로",
          emb.tokenizer.encode("query: 임베더 토크나이저가 바뀌면 안 된다").ids == emb_ids_before)

    print("\n%d개 중 %d개 실패" % (TOTAL[0], len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
