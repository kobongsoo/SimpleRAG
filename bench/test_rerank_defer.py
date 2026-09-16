#------------------------------------------------------------------
# 리랭커 백그라운드 적재 회귀 테스트 (REPORT §34)
#=> 준비 시간을 줄이려고 리랭커를 병렬 예열에서 빼 "준비 완료 뒤 백그라운드"로 올리게
#   바꿨다. 이 파일은 그 순서와 실패 처리가 설계대로인지 가짜 적재 함수로 확인한다.
#   모델·인덱스를 전혀 올리지 않아 몇 초면 끝난다.
#
#   확인하는 것
#    - 리랭커 적재는 핵심 구성요소(임베더·Qdrant·BM25)가 **다 끝난 뒤** 시작한다
#    - warmup(wait=True) 는 리랭커를 기다리지 않고 돌아온다
#    - 실패하면 리트리버에서 떼어 이후 질의는 RRF 로 간다
#    - wait=False 에서도 미룬다 / 설정으로 끄면 종전처럼 함께 올린다 / 리랭킹 끔이면 안 올린다
#
#   실행: .venv/Scripts/python.exe bench/test_rerank_defer.py
#------------------------------------------------------------------

import sys
import threading
import time

sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from simplerag import config                   # noqa: E402
from simplerag.retrieve.reranker import RerankError  # noqa: E402
from simplerag.warmup import App               # noqa: E402

FAILED = []
TOTAL = [0]
CORE_S = 0.30        # 가짜 핵심 구성요소 적재 시간
RERANK_S = 0.50      # 가짜 리랭커 적재 시간


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
# 가짜 적재 함수를 끼운 App 만들기
#=> 실제 모델 대신 잠깐 자고 시각을 기록하는 함수를 넣는다. 스레드가 언제 시작·끝났는지
#   남겨 순서를 검사한다.
#
# -in: rerank_fails = True 면 리랭커 적재가 RerankError 를 낸다
#
# -out: (app, log) = App, {이름: (시작, 끝)} 기록 dict
# -out: error = 없음
#------------------------------------------------------------------
def make_app(rerank_fails=False):
    app = App()
    log = {}
    lock = threading.Lock()
    t0 = time.perf_counter()

    # 이름·소요·실패 여부를 받아 가짜 ensure_loaded 를 만든다
    def fake(name, sec, fail=False):
        def run():
            s = time.perf_counter() - t0
            time.sleep(sec)
            e = time.perf_counter() - t0
            with lock:
                log[name] = (s, e)
            if fail:
                raise RerankError("가짜 적재 실패")
            return sec * 1000.0
        return run

    app.embedder.ensure_loaded = fake("embedder", CORE_S)
    app.store.ensure_loaded = fake("qdrant", CORE_S)
    app.bm25.ensure_loaded = fake("bm25", CORE_S / 3)
    app.reranker.ensure_loaded = fake("reranker", RERANK_S, rerank_fails)
    return app, log


def core_end(log):
    return max(log[k][1] for k in ("embedder", "qdrant", "bm25"))


def test_defer_default():
    print("\n[기본 — 준비 완료 뒤 백그라운드]")
    old = config.RERANK_DEFER_LOAD
    config.RERANK_DEFER_LOAD = True
    try:
        app, log = make_app()
        r = app.warmup(wait=True, skip_llm=True, rerank=True)
        # 벽시계 문턱으로 보지 않는다 — 첫 호출의 부수 비용(메타데이터 조회 등)에 흔들린다.
        # 돌아온 순간 리랭커가 **아직 적재 중**인지로 본다(이것이 확인하려는 성질이다).
        with_rr_done = "reranker" in log
        alive = app._rerank_thread is not None and app._rerank_thread.is_alive()
        check("warmup 이 리랭커를 기다리지 않고 돌아옴(돌아온 순간 적재 중)",
              not with_rr_done and alive, (with_rr_done, alive))
        check("결과에 reranker 적재 시간 없음 + _rerank_deferred=True",
              "reranker" not in r and r.get("_rerank_deferred") is True, r)
        check("리랭킹은 켜진 상태(리트리버에 붙어 있음)", app.retriever.reranker is app.reranker)
        v = app.wait_rerank(timeout=5)
        check("wait_rerank → 적재 ms", isinstance(v, float) and abs(v - RERANK_S * 1000) < 1, v)
        check("리랭커는 핵심 구성요소가 다 끝난 뒤 시작",
              log["reranker"][0] >= core_end(log) - 0.005, log)
    finally:
        config.RERANK_DEFER_LOAD = old


def test_defer_failure():
    print("\n[백그라운드 적재 실패]")
    old = config.RERANK_DEFER_LOAD
    config.RERANK_DEFER_LOAD = True
    try:
        app, _ = make_app(rerank_fails=True)
        app.warmup(wait=True, skip_llm=True, rerank=True)
        v = app.wait_rerank(timeout=5)
        check("rerank_load 에 에러 문자열", isinstance(v, str) and "가짜 적재 실패" in v, v)
        check("리트리버에서 리랭커를 뗌(이후 질의는 RRF)", app.retriever.reranker is None)
    finally:
        config.RERANK_DEFER_LOAD = old


def test_defer_nowait():
    print("\n[wait=False 에서도 미룬다]")
    old = config.RERANK_DEFER_LOAD
    config.RERANK_DEFER_LOAD = True
    try:
        app, log = make_app()
        t = time.perf_counter()
        r = app.warmup(wait=False, skip_llm=True, rerank=True)
        check("즉시 돌아옴", time.perf_counter() - t < 0.1 and r == {})
        v = app.wait_rerank(timeout=5)
        check("리랭커 적재 완료", isinstance(v, float), v)
        check("리랭커는 핵심 구성요소가 다 끝난 뒤 시작",
              log["reranker"][0] >= core_end(log) - 0.005, log)
    finally:
        config.RERANK_DEFER_LOAD = old


def test_parallel_when_disabled():
    print("\n[SIMPLERAG_RERANK_DEFER=0 — 종전 병렬 적재]")
    old = config.RERANK_DEFER_LOAD
    config.RERANK_DEFER_LOAD = False
    try:
        app, log = make_app()
        r = app.warmup(wait=True, skip_llm=True, rerank=True)
        check("결과에 reranker 적재 시간 있음 + _rerank_deferred=False",
              "reranker" in r and r.get("_rerank_deferred") is False, r)
        check("핵심 구성요소와 동시에 시작", log["reranker"][0] < core_end(log), log)
        check("wait_rerank → None(백그라운드 적재 안 함)", app.wait_rerank(timeout=1) is None)
    finally:
        config.RERANK_DEFER_LOAD = old


def test_rerank_off():
    print("\n[리랭킹 끔 — 올리지 않음]")
    app, log = make_app()
    r = app.warmup(wait=True, skip_llm=True, rerank=False)
    time.sleep(RERANK_S + 0.1)
    check("리랭커 적재 호출 없음", "reranker" not in log and "reranker" not in r, log)
    check("리트리버에 리랭커 없음 / wait_rerank None",
          app.retriever.reranker is None and app.wait_rerank(timeout=1) is None)


def main():
    test_defer_default()
    test_defer_failure()
    test_defer_nowait()
    test_parallel_when_disabled()
    test_rerank_off()
    print("\n%d개 중 %d개 실패" % (TOTAL[0], len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
