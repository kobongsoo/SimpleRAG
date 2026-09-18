#------------------------------------------------------------------
# 가짜 SimpleRAG chat — 워커 수명 시험용 (모델 없이 돈다)
#=> 진짜 워커는 준비에 6초, 답변에 10초쯤 걸려 재시작·시간 초과 같은 경우를 시험하기 어렵다.
#   이 스크립트는 같은 형식으로 즉시 답하고, 시험이 원하는 고장을 흉내 낸다.
#
#   진짜와 같게 맞춘 것
#    - 첫 프롬프트 "질문> " 를 줄바꿈 없이 낸다
#    - stdout 은 UTF-8, stdin 은 cp949 로 읽는다(동결 exe 와 같은 상황)
#    - 근거 → 답변 → 소요 → 다음 프롬프트 순서와 구분선 문구
#
#   시험용 고장 (환경변수로 켠다)
#    FAKE_READY_DELAY  : 준비까지 늦추는 시간(초)
#    FAKE_ANSWER_DELAY : 답변을 시작하기까지 늦추는 시간(초)
#    FAKE_HANG         : 이 질문을 받으면 답하지 않고 멈춘다(답변 시간 초과 시험)
#    FAKE_DIE_ON       : 이 질문을 받으면 그냥 죽는다(재시작 시험)
#    FAKE_EXIT_CODE    : 시작하자마자 이 코드로 끝난다(설정 오류 시험)
#    FAKE_LOCK         : 인덱스가 잠겨 있을 때처럼 stderr 에 알리고 죽는다(잠금 충돌 시험)
#    FAKE_NO_EVIDENCE  : 근거를 못 찾은 답변을 낸다(인덱스가 비었을 때)
#    FAKE_CMD_DELAY    : 인덱싱 명령(/index-…) 하나에 걸리는 시간(초)
#    FAKE_OLD_WORKER   : 인덱싱 명령을 모르는 옛 워커처럼 "알 수 없는 명령" 을 낸다
#------------------------------------------------------------------
import json
import os
import sys
import time


#------------------------------------------------------------------
# 인덱싱 명령 흉내 (자동 인덱싱 §9)
#=> 진짜 워커처럼 "@index {json}" 한 줄을 내고 프롬프트로 돌아간다. 받은 명령과 경로를
#   그대로 돌려줘 시험이 차례를 확인할 수 있게 한다.
#
# -in: q = 받은 줄("/index-doc {...}")
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def index_command(q):
    time.sleep(float(os.environ.get("FAKE_CMD_DELAY", "0")))
    if os.environ.get("FAKE_OLD_WORKER"):
        out("  알 수 없는 명령입니다. /help 를 입력하세요.\n\n")
        out("질문> ")
        return
    cmd, _, arg = q.partition(" ")
    path = ""
    if arg.strip().startswith("{"):
        path = json.loads(arg).get("path", "")
    res = {"op": cmd.lstrip("/"), "ok": True, "path": path, "result": "added", "ms": 1}
    out("@index " + json.dumps(res, ensure_ascii=True) + "\n")
    out("질문> ")


#------------------------------------------------------------------
# UTF-8 로 내보내기
#=> 진짜 CLI 도 stdout 을 UTF-8 로 고정한다(cli._force_utf8_output).
#
# -in: text = 내보낼 글자
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def out(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


#------------------------------------------------------------------
# 답변 한 건 내보내기
#=> 근거 3건 → 답변 → 소요 → 프롬프트. 진짜 출력과 같은 모양으로 만든다.
#
# -in: q = 받은 질문(답변 글에 그대로 넣는다)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def answer(q):
    # 시험에서 BUSY 상태를 관찰할 수 있도록 일부러 늦출 수 있게 한다
    time.sleep(float(os.environ.get("FAKE_ANSWER_DELAY", "0")))
    if os.environ.get("FAKE_NO_EVIDENCE"):
        # 인덱스가 비었을 때 진짜 CLI 가 내는 모양
        out("\n검색된 근거가 없습니다.\n\n")
        out("질문> ")
        return
    out("\n── 근거 3건 (845ms) ─────────────────────\n")
    for i, doc in enumerate(("규정_A.doc", "규정_B.doc", "규정_C.docx"), 1):
        out("  [{}] {}\n".format(i, doc))
        out("      {} 에 대한 근거 본문 {}\n".format(q, i))
    out("\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──\n")
    for part in ("{} 에 대한 ".format(q), "요약 답변입니다. ", "[1]"):
        out(part)
        time.sleep(0.01)
    out("\n\n── 소요 ─────────────────────────────────\n")
    out("  검색 845ms (임베딩 6 + dense 81 + BM25 1)\n")
    out("  첫 글자 1.24s / 완료 2.86s\n")
    out("  인용 근거: [1]\n\n")
    out("질문> ")


def main():
    if os.environ.get("FAKE_LOCK"):
        # 진짜 Qdrant 가 내는 문구 그대로 — rag_worker 가 이 글귀로 잠금 충돌을 알아챈다
        sys.stderr.write("RuntimeError: Storage folder qdrant_data is already accessed "
                         "by another instance of Qdrant client\n")
        sys.stderr.flush()
        return 1

    code = os.environ.get("FAKE_EXIT_CODE")
    if code:
        # 설정 오류 흉내: 진짜 CLI 도 stderr 로 알리고 코드 2 로 끝낸다
        sys.stderr.write("설정 오류: chunk.tokens 값이 범위 밖입니다\n")
        sys.stderr.flush()
        return int(code)

    delay = float(os.environ.get("FAKE_READY_DELAY", "0"))
    sys.stderr.write("ggml_vulkan: Found 1 Vulkan devices:\n")
    sys.stderr.flush()
    out("모델 적재 중...\n")
    time.sleep(delay)
    out("준비 완료 (0.1초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf\n")
    out("생성 iGPU(Vulkan) / 리랭킹 켬\n")
    out("질문을 입력하세요.  종료: exit 또는 Ctrl+D   도움말: /help\n\n")
    out("질문> ")

    hang_on = os.environ.get("FAKE_HANG")
    die_on = os.environ.get("FAKE_DIE_ON")

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        q = raw.decode("cp949", errors="replace").strip()
        if not q:
            continue
        if q.lower() in ("exit", "quit", "종료"):
            out("종료합니다.\n")
            break
        if die_on and q == die_on:
            sys.stderr.write("가짜 고장: 질문을 받고 죽습니다\n")
            sys.stderr.flush()
            os._exit(9)
        if hang_on and q == hang_on:
            while True:
                time.sleep(1)       # 답하지 않고 버틴다
        if q.startswith("/index-") or q == "/bm25":
            index_command(q)
            continue
        answer(q)
    return 0


if __name__ == "__main__":
    sys.exit(main())
