#------------------------------------------------------------------
# 생성 백엔드 선택 + 리랭킹 회귀 테스트 (REPORT §32)
#=> 운영 반영에서 새로 생긴 두 갈래 — "CPU 냐 iGPU 냐" 와 "리랭킹을 켜냐" —
#   가 설계대로 갈라지는지 본다. 모델·인덱스를 올리지 않아 몇 초면 끝난다.
#   (실제 측정 자식 프로세스 1건만 띄운다 — 깨진 DLL 로 폴백하는 경로.)
#
#   왜 필요한가
#    - 폴백 경로는 이 노트북에서는 평소에 절대 안 탄다(iGPU 가 멀쩡하므로).
#      안 타는 경로는 조용히 썩는다. 일부러 깨진 조건을 만들어 탄다.
#    - 리랭킹은 실험 스크립트와 **같은 절차**여야 실험 수치가 운영에서
#      재현된다. 정렬 방향·동점 처리·풀 크기가 바뀌면 여기서 걸린다.
#
#   실행: .venv/Scripts/python.exe bench/test_backend_rerank.py
#------------------------------------------------------------------

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                          # noqa: E402
from simplerag.generate import backend as B           # noqa: E402
from simplerag.retrieve.hybrid import HybridRetriever  # noqa: E402

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
    if cond:
        print("  OK   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, note))
        FAILED.append(name)


#------------------------------------------------------------------
# 설정값 임시 교체
#=> 테스트마다 config 를 바꿨다가 원래대로 돌린다. 선택 결과 캐시(_selected)
#   도 비워 매 테스트가 처음부터 고르게 한다.
#
# -in: **kw = 바꿀 config 이름=값
#
# -out: restore = 호출하면 원래 값으로 되돌리는 함수
# -out: error = 없음
#------------------------------------------------------------------
def override(**kw):
    old = {k: getattr(config, k) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    B._selected = None

    def restore():
        for k, v in old.items():
            setattr(config, k, v)
        B._selected = None
    return restore


def test_decide():
    print("\n[decide — 측정 결과로 고르기]")
    cpu = {"ok": True, "prefill_tps": 230.0}
    check("iGPU 3배 → vulkan", B.decide(cpu, {"ok": True, "prefill_tps": 700.0})[0] == "vulkan")
    check("iGPU 1.2배 → cpu (기준 1.3배 미달)",
          B.decide(cpu, {"ok": True, "prefill_tps": 276.0})[0] == "cpu")
    check("정확히 1.3배 → vulkan (이상이면 채택)",
          B.decide(cpu, {"ok": True, "prefill_tps": 299.0})[0] == "vulkan")
    check("구형 iGPU 가 CPU 보다 느림 → cpu",
          B.decide(cpu, {"ok": True, "prefill_tps": 150.0})[0] == "cpu")
    b, why = B.decide(cpu, {"ok": False, "error": "DLL 적재 실패"})
    check("iGPU 측정 실패 → cpu, 이유에 원인 포함", b == "cpu" and "DLL 적재 실패" in why, why)
    check("iGPU 결과 없음 → cpu", B.decide(cpu, None)[0] == "cpu")
    check("CPU 측정만 실패 → vulkan",
          B.decide({"ok": False, "error": "x"}, {"ok": True, "prefill_tps": 700.0})[0] == "vulkan")


def test_rerank_enabled():
    print("\n[rerank_enabled — 리랭킹 켤지]")
    for mode, backend, want in (("auto", "vulkan", True), ("auto", "cpu", False),
                                ("1", "cpu", True), ("0", "vulkan", False),
                                ("on", "cpu", True), ("off", "vulkan", False)):
        restore = override(RERANK=mode)
        try:
            check("RERANK=%s, %s → %s" % (mode, backend, want),
                  B.rerank_enabled(backend) is want)
        finally:
            restore()


def test_vulkan_available(tmp):
    print("\n[vulkan_available — 사전 점검]")
    ok, why = B.vulkan_available(os.path.join(tmp, "없는폴더"))
    check("폴더 없음 → 불가", not ok and "폴더 없음" in why, why)

    part = os.path.join(tmp, "part")
    os.makedirs(part)
    open(os.path.join(part, "llama.dll"), "wb").close()
    ok, why = B.vulkan_available(part)
    check("DLL 일부 누락 → 불가, 누락 목록 표시", not ok and "ggml-vulkan.dll" in why, why)

    wrong = os.path.join(tmp, "wrong")
    os.makedirs(wrong)
    for f in B.VULKAN_DLLS:
        open(os.path.join(wrong, f), "wb").close()
    with open(os.path.join(wrong, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write("0.0.1\n")
    ok, why = B.vulkan_available(wrong)
    check("DLL 버전 불일치 → 불가", not ok and "버전" in why, why)

    ok, why = B.vulkan_available()
    check("이 PC 의 실제 런타임 → 가능", ok, why)


def test_cache(tmp):
    print("\n[peek / cache_key — 저장된 측정]")
    path = os.path.join(tmp, "gen_backend.json")
    restore = override(BACKEND_CACHE_PATH=path)
    try:
        check("파일 없음 → None", B.peek() is None)

        data = {"key": B.cache_key(), "backend": "vulkan", "reason": "테스트"}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        got = B.peek()
        check("키 일치 → 저장 내용 반환", got and got["backend"] == "vulkan")

        data["key"] = dict(data["key"], host="다른PC")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        check("다른 PC 의 측정 → None(다시 잰다)", B.peek() is None)

        with open(path, "w", encoding="utf-8") as f:
            f.write("{깨진 json")
        check("손상된 파일 → None(죽지 않음)", B.peek() is None)
    finally:
        restore()


def test_select(tmp):
    print("\n[select — 선택과 환경변수 반영]")
    check("테스트 프로세스에 llama_cpp 미적재(선택 테스트 전제)", "llama_cpp" not in sys.modules)
    saved_env = os.environ.pop("LLAMA_CPP_LIB_PATH", None)
    path = os.path.join(tmp, "sel.json")
    try:
        restore = override(GEN_BACKEND="cpu", BACKEND_CACHE_PATH=path)
        try:
            info = B.select()
            check("SIMPLERAG_GEN_BACKEND=cpu → cpu(환경변수)",
                  info["backend"] == "cpu" and info["source"] == "config")
            check("cpu 선택 시 LLAMA_CPP_LIB_PATH 미설정", "LLAMA_CPP_LIB_PATH" not in os.environ)
            check("두 번째 호출은 같은 결과를 재사용", B.select() is info)
        finally:
            restore()

        restore = override(GEN_BACKEND="auto", BACKEND_CACHE_PATH=path,
                           VULKAN_LIB_DIR=os.path.join(tmp, "없는런타임"))
        try:
            info = B.select()
            check("런타임 폴더 없음 → cpu(사전 점검)",
                  info["backend"] == "cpu" and info["source"] == "check", info)
        finally:
            restore()

        # 저장된 측정이 iGPU 면 측정 없이 즉시 iGPU + 환경변수 설정
        restore = override(GEN_BACKEND="auto", BACKEND_CACHE_PATH=path)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"key": B.cache_key(), "backend": "vulkan", "reason": "저장"}, f)
            info = B.select()
            check("저장된 측정(vulkan) → 측정 없이 vulkan",
                  info["backend"] == "vulkan" and info["source"] == "cache", info)
            check("vulkan 선택 시 LLAMA_CPP_LIB_PATH = 런타임 폴더",
                  os.environ.get("LLAMA_CPP_LIB_PATH") == config.VULKAN_LIB_DIR)
            B._apply({"backend": "cpu", "source": "config"})
            check("cpu 로 바꾸면 우리 런타임을 가리키던 환경변수를 지운다",
                  "LLAMA_CPP_LIB_PATH" not in os.environ)
            os.environ["LLAMA_CPP_LIB_PATH"] = r"D:\사용자가\지정한\경로"
            B._apply({"backend": "cpu", "source": "config"})
            check("사용자가 따로 지정한 값은 건드리지 않는다",
                  os.environ.get("LLAMA_CPP_LIB_PATH") == r"D:\사용자가\지정한\경로")
        finally:
            os.environ.pop("LLAMA_CPP_LIB_PATH", None)
            restore()
    finally:
        if saved_env is not None:
            os.environ["LLAMA_CPP_LIB_PATH"] = saved_env


def test_broken_dll_probe(tmp):
    print("\n[run_probe — 깨진 Vulkan DLL 로 실제 자식 프로세스 (폴백 경로)]")
    broken = os.path.join(tmp, "broken_vulkan")
    shutil.copytree(config.VULKAN_LIB_DIR, broken)
    # ggml.dll 이 직접 링크하는 DLL 을 빼 적재 실패를 일부러 만든다
    os.remove(os.path.join(broken, "ggml-vulkan.dll"))

    restore = override(VULKAN_LIB_DIR=broken)
    try:
        r = B.run_probe("vulkan", config.gen_model_path(config.DEFAULT_GEN_MODEL), timeout=120)
        check("깨진 DLL → 측정 실패로 보고(본 프로세스는 무사)", r.get("ok") is False, r)
        check("실패 이유가 담긴다", bool(r.get("error")), r)
        backend, why = B.decide({"ok": True, "prefill_tps": 230.0}, r)
        check("그 결과로 고르면 cpu", backend == "cpu", why)
    finally:
        restore()


#------------------------------------------------------------------
# 가짜 리랭커 — 미리 정한 점수를 돌려주거나 일부러 실패한다
#
# -in: scores = 돌려줄 점수 목록(None 이면 예외)
#
# -out: 없음
# -out: error = scores 가 None 이면 score() 가 RuntimeError
#------------------------------------------------------------------
class FakeReranker:
    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    def score(self, query, passages):
        self.calls.append(list(passages))
        if self.scores is None:
            raise RuntimeError("모델 손상")
        return list(self.scores)


#------------------------------------------------------------------
# 1단계 검색을 흉내 내는 리트리버 만들기
#=> _search 만 바꿔 끼워 Qdrant·임베더 없이 search() 의 리랭킹 절차만 본다.
#
# -in: n        = 1단계가 돌려줄 후보 수(요청 top_k 가 더 작으면 그만큼만)
# -in: reranker = 붙일 리랭커(None 이면 리랭킹 끔)
#
# -out: (retriever, asked) = 리트리버, 1단계에 요청된 top_k 기록 리스트
# -out: error = 없음
#------------------------------------------------------------------
def make_retriever(n, reranker):
    r = HybridRetriever(None, None, None, reranker=reranker)
    asked = []

    # 1단계 흉내 — RRF 순위대로 c0, c1, ... 를 돌려준다
    def fake_search(query, top_k=None, first_stage=None):
        asked.append(top_k)
        k = min(n, top_k)
        return ([{"id": i, "text": "c%d" % i} for i in range(k)],
                {"embed_ms": 1.0, "dense_ms": 1.0, "bm25_ms": 1.0, "fuse_ms": 1.0,
                 "total_ms": 100.0})

    r._search = fake_search
    return r, asked


def test_rerank_search():
    print("\n[HybridRetriever.search — 리랭킹 절차]")
    # 절차 검사는 풀 5 기준으로 짰다(가짜 리랭커 점수 5개). 기본값(§35.9, 10)과 무관하게 고정한다
    saved_pool = config.RERANK_POOL
    config.RERANK_POOL = 5
    r, asked = make_retriever(10, None)
    chunks, t = r.search("q", top_k=3)
    check("리랭커 없음 → 1단계에 top_k 그대로 요청", asked == [3], asked)
    check("리랭커 없음 → 순서 그대로, rerank_ms 없음",
          [c["id"] for c in chunks] == [0, 1, 2] and "rerank_ms" not in t)

    fake = FakeReranker([0.1, 0.9, -1.0, 0.5, 0.3])
    r, asked = make_retriever(10, fake)
    chunks, t = r.search("q", top_k=3)
    check("리랭킹 → 1단계에 RERANK_POOL(%d) 요청" % config.RERANK_POOL,
          asked == [config.RERANK_POOL], asked)
    check("리랭커에 후보 5건 전부 전달", fake.calls and len(fake.calls[0]) == 5)
    check("점수 내림차순 상위 3 → [1, 3, 4]", [c["id"] for c in chunks] == [1, 3, 4],
          [c["id"] for c in chunks])
    check("rerank_ms 기록 + total_ms 에 합산",
          "rerank_ms" in t and abs(t["total_ms"] - (100.0 + t["rerank_ms"])) < 0.2, t)

    r, _ = make_retriever(10, FakeReranker([0.5, 0.5, 0.5, 0.9, 0.5]))
    chunks, _ = r.search("q", top_k=3)
    check("동점은 RRF 순위 유지(안정 정렬) → [3, 0, 1]", [c["id"] for c in chunks] == [3, 0, 1],
          [c["id"] for c in chunks])

    r, _ = make_retriever(10, FakeReranker(None))
    chunks, t = r.search("q", top_k=3)
    check("리랭커 예외 → 죽지 않고 RRF 순서 [0, 1, 2]", [c["id"] for c in chunks] == [0, 1, 2])
    check("리랭커 예외 → rerank_error 기록", "모델 손상" in t.get("rerank_error", ""), t)

    # 관련도 문턱을 끄면(예전 동작) 후보가 적을 때 리랭커를 부르지 않는다
    os.environ["SIMPLERAG_RELEVANCE_MIN"] = "off"
    fake = FakeReranker([1.0, 2.0])
    r, _ = make_retriever(2, fake)
    chunks, t = r.search("q", top_k=3)
    check("문턱 끔 + 후보가 top_k 이하 → 리랭커 호출 안 함", fake.calls == [] and len(chunks) == 2)

    # 문턱을 켜면 후보가 적어도 점수를 매긴다 — 작은 폴더에서 문턱이 빠지지 않게
    os.environ["SIMPLERAG_RELEVANCE_MIN"] = "-5"
    fake = FakeReranker([1.0, 2.0])
    r, _ = make_retriever(2, fake)
    chunks, t = r.search("q", top_k=3)
    check("문턱 켬 + 후보가 적어도 → 점수를 매긴다", len(fake.calls) == 1 and len(chunks) == 2, t)
    check("관련도 최고 점수를 남긴다", t.get("rerank_top") == 2.0, t)

    # 가장 관련 있는 후보도 문턱 아래 → 근거 없이 no_relevant
    fake = FakeReranker([-7.0, -6.0, -8.0, -9.0, -6.5])
    r, _ = make_retriever(10, fake)
    chunks, t = r.search("q", top_k=3)
    check("모든 후보가 -5 아래 → 근거 없음(no_relevant)", chunks == [] and t.get("no_relevant"), t)
    fake = FakeReranker([-7.0, -4.0, -8.0, -9.0, -6.5])
    r, _ = make_retriever(10, fake)
    chunks, t = r.search("q", top_k=3)
    check("하나라도 문턱 위면 근거를 낸다", len(chunks) == 3 and not t.get("no_relevant"), t)
    os.environ.pop("SIMPLERAG_RELEVANCE_MIN", None)
    config.RERANK_POOL = saved_pool


def main():
    tmp = tempfile.mkdtemp(prefix="simplerag_test_")
    try:
        test_decide()
        test_rerank_enabled()
        test_vulkan_available(tmp)
        test_cache(tmp)
        test_select(tmp)
        test_rerank_search()
        test_broken_dll_probe(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d개 중 %d개 실패" % (TOTAL[0], len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
