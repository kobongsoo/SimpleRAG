#------------------------------------------------------------------
# 동의어 넓히기 시험 — Rust synonyms.rs 의 expands_like_python 과 같은 사례
#=> 사전 파일 없이 임시 사전으로 규칙을 만들어, 두 판이 같은 BM25 질의를 내는지 확인한다.
#
#   사용: .venv\Scripts\python bench/paraphrase/test_synonyms.py
#------------------------------------------------------------------
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
sys.stdout.reconfigure(encoding="utf-8")

from simplerag.retrieve.synonyms import _load, expand_with  # noqa: E402

YAML = ("synonyms:\n  조부모: [할아버지]\n  사망: [돌아가시, 별세]\n  출산: [애기 낳]\n  자녀: [애기]\n  배우자: [와이프]\n"
        "  배우자의 부모: [장인]\n  자차: [내 차]\n  부모: [아버지]\nrejected:\n  - [장인, 배우자의 부모]\n")
CASES = [
    ("할아버지 돌아가시면 휴가 며칠 줘?", "할아버지 돌아가시면 휴가 며칠 줘? 사망 조부모"),   # 명사·동사 둘 다
    ("와이프가 애기 낳으면 휴가 며칠?", "와이프가 애기 낳으면 휴가 며칠? 출산 배우자"),     # 긴 "애기 낳" 이 "애기" 보다 먼저
    ("조부모 사망 시 휴가", "조부모 사망 시 휴가"),                                         # 문서 용어로 물으면 그대로
    ("할아버지께서 별세", "할아버지께서 별세 조부모 사망"),                                 # 조사가 붙어도 맞음
    ("장인어른 돌아가시면", "장인어른 돌아가시면 사망"),                                    # rejected 짝은 쓰지 않음
    ("현장 출근을 내  차로", "현장 출근을 내  차로 자차"),                                  # 일상어 속 공백 여러 개
]

fail = 0
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "s.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(YAML)
    rules = _load([p])
    for q, want in CASES:
        got = expand_with(rules, q)[0]
        ok = got == want
        fail += not ok
        print(("✅ " if ok else "🔴 ") + q + ("" if ok else "\n     got:  " + got + "\n     want: " + want))
    # "할아버지" 속 "아버지" 는 낱말 중간이라 맞지 않는다
    got = expand_with(rules, "할아버지 생신")[0]
    fail += "부모" in got.split("할아버지 생신", 1)[1].replace("조부모", "")

# 넓힘 때 RRF 1위 덧붙이기(hybrid.synonym_extra) — 넓힘 있고 · 3건 다 찼고 · 근거에 없을 때만
from simplerag import config  # noqa: E402
from simplerag.retrieve.hybrid import synonym_extra  # noqa: E402

c = lambda i, t: {"id": i, "text": t, "doc_name": "a.pdf"}  # 시험용 청크
three = [c(1, "가"), c(2, "나"), c(3, "다")]
syn = {"synonyms": ["할아버지→조부모"]}
config.SYNONYM_EXTRA = True
EXTRA = [
    ("넓힘 있고 RRF 1위가 빠짐 → 붙임", synonym_extra(three, c(9, "라"), syn, 3), 9),
    ("넓힘 없음 → 안 붙임", synonym_extra(three, c(9, "라"), {}, 3), None),
    ("이미 근거에 있음 → 안 붙임", synonym_extra(three, c(2, "나"), syn, 3), None),
    ("내용이 같은 사본 → 안 붙임", synonym_extra(three, c(9, "다 "), syn, 3), None),
    ("근거가 3건 미만 → 안 붙임", synonym_extra(three[:2], c(9, "라"), syn, 3), None),
]
config.SYNONYM_EXTRA = False
EXTRA.append(("옵션 꺼짐 → 안 붙임", synonym_extra(three, c(9, "라"), syn, 3), None))
for name, got, want in EXTRA:
    ok = (got and got["id"]) == want if want else got is None
    fail += not ok
    print(("✅ " if ok else "🔴 ") + name)

# 후보 제안의 낱말 거르기(synonym_suggest.words_and_terms) — Rust words_like_python 과 같은 사례
from simplerag.retrieve.synonym_suggest import words_and_terms  # noqa: E402

config.SYNONYMS = False
w, t = words_and_terms("할아버지 돌아가시면 휴가 며칠 줘? 공무원한테 되고",
                       ["경조사 휴가는 조부모 사망 시 3일을 부여한다. 공무원 등에 따른 규정"])
ok = w == ["할아버지", "돌아가시면"] and "조부모" in t and "사망" in t and "따른" not in t
fail += not ok
print(("✅ " if ok else "🔴 ") + "후보 제안 낱말 거르기" + ("" if ok else " {} {}".format(w, t)))

# 놓친 질문 기록(log_miss) — 점수 0 미만 · 관련 근거 없음만, 옵션 끄면 안 남김, 읽을 때 같은 질문은 횟수로
from simplerag.retrieve import synonym_suggest as ss  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    root, config.ROOT = config.ROOT, d
    config.SYNONYM_LOG = True
    r1 = ss.log_miss("할아버지 돌아가시면", {"rerank_top": -1.2})
    r2 = ss.log_miss("조부모 사망 시 휴가", {"rerank_top": 3.5})
    r3 = ss.log_miss("비트코인 숙박비", {"no_relevant": True})
    r4 = ss.log_miss("할아버지 돌아가시면", {"rerank_top": -0.5})
    config.SYNONYM_LOG = False
    r5 = ss.log_miss("와이프 출산", {"rerank_top": -2.0})
    got = ss.read_misses(os.path.join(d, ss.MISSES_NAME))
    config.ROOT = root
ok = (r1, r2, r3, r4, r5) == (True, False, True, True, False) and got == [("할아버지 돌아가시면", 2), ("비트코인 숙박비", 1)]
fail += not ok
print(("✅ " if ok else "🔴 ") + "놓친 질문 기록" + ("" if ok else " {} {}".format((r1, r2, r3, r4, r5), got)))
print("실패 %d건" % fail)
raise SystemExit(1 if fail else 0)
