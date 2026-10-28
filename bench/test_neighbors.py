#------------------------------------------------------------------
# 정밀 모드 이웃 청크 시험 — hybrid.attach_neighbors (코드수정 설계서 §7 ④)
#=> 근거마다 리랭커 후보 안의 앞뒤(±1) 청크만 붙는지, 이미 근거인 청크·다른 문서·꺼진 설정이면 안 붙는지,
#   앞 이웃은 머리 줄 뒤에·뒤 이웃은 본문 뒤에 머리 줄을 떼고 붙는지 본다.
#   골든 100문항에서 실험(C4)과 근거가 같은지는 스크래치 prec_check.py 로 쟀다(100/100).
#
#   사용: .venv\Scripts\python bench/test_neighbors.py
#------------------------------------------------------------------
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config  # noqa: E402
from simplerag.retrieve.hybrid import attach_neighbors  # noqa: E402


# 시험용 청크 — 머리 줄 "[문서 > 절]" 을 단다
def c(path, idx, body):
    return {"id": hash((path, idx)), "doc_path": path, "chunk_idx": idx, "text": "[{} > 절]\n{}".format(path, body)}


pool = [c("a", 5, "가운데"), c("a", 4, "앞"), c("a", 6, "뒤"), c("b", 1, "다른 문서"), c("a", 9, "먼 곳"), c("a", 8, "먼 앞")]
fail = 0
config.PRECISION = True
CASES = [
    ("앞뒤 둘 다 후보에 있음 → 앞은 머리 뒤, 뒤는 본문 뒤", [pool[0]], ["[a > 절]\n앞\n가운데\n뒤"], 2),
    ("앞 이웃이 이미 근거 → 그 청크는 안 붙임", [pool[0], pool[1]], ["[a > 절]\n가운데\n뒤", "[a > 절]\n앞"], 1),
    ("다른 문서 · 이웃이 후보에 없음 → 그대로", [pool[3]], ["[b > 절]\n다른 문서"], 0),
    ("뒤 이웃 없고 앞만 후보에", [pool[4]], ["[a > 절]\n먼 앞\n먼 곳"], 1),
]
for name, picked, want, want_n in CASES:
    got, n = attach_neighbors(picked, pool)
    ok = [x["text"] for x in got] == want and n == want_n
    fail += not ok
    print("{} {}".format("OK  " if ok else "FAIL", name))
config.PRECISION = False
got, n = attach_neighbors([pool[0]], pool)
ok = got[0] is pool[0] and n == 0
fail += not ok
print("{} 설정 끔 → 그대로".format("OK  " if ok else "FAIL"))
sys.exit(1 if fail else 0)
