#------------------------------------------------------------------
# rag_worker 시험 — 워커 수명 (설계서 §6 상태 기계)
#=> 진짜 모델 대신 tests/fake_chat.py 를 워커로 써서, 모델 없이 빠르게 확인한다.
#   확인하는 것: 준비 판정 · 질문/답변 · 한글 전달(cp949) · 대기 칸 교체 ·
#                답변 시간 초과 · 워커가 죽었을 때 재시작 · 설정 오류 시 재시작 안 함 · 유휴 해제
#------------------------------------------------------------------
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import log as rsb_log  # noqa: E402
import rag_worker  # noqa: E402
from rag_worker import BUSY, READY, STOPPED, RagWorker  # noqa: E402

rsb_log.setup("INFO")
FAKE = [sys.executable, os.path.join(HERE, "fake_chat.py")]


#------------------------------------------------------------------
# 이벤트를 모으는 도우미
#=> 워커는 다른 스레드에서 콜백을 부른다. 목록에 쌓아 두고 시험에서 들여다본다.
#
# -필드: events = 받은 이벤트 전부
#------------------------------------------------------------------
class Collector:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self):
        self.events = []

    #--------------------------------------------------------------
    # 콜백 (워커가 부른다)
    #
    # -in: ev = 이벤트 튜플
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __call__(self, ev):
        self.events.append(ev)

    #--------------------------------------------------------------
    # 어떤 종류의 이벤트만 고르기
    #
    # -in: kind = 이벤트 이름
    #
    # -out: 목록
    # -out: error = 없음
    #--------------------------------------------------------------
    def of(self, kind):
        return [e for e in self.events if e[0] == kind]

    #--------------------------------------------------------------
    # 답변 글자 모으기
    #
    # -in: 없음
    #
    # -out: 토큰을 이어 붙인 글자
    # -out: error = 없음
    #--------------------------------------------------------------
    def answer_text(self):
        return "".join(e[1] for e in self.events if e[0] == "token")


#------------------------------------------------------------------
# 조건이 참이 될 때까지 기다리기
#=> 워커는 스레드로 돌아가므로 시험에서는 기다려야 한다.
#
# -in: cond    = 참/거짓을 돌려주는 함수
# -in: timeout = 한도(초)
#
# -out: True = 조건이 참이 됨
# -out: error = 없음 (시간이 지나면 False)
#------------------------------------------------------------------
def wait_for(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class WorkerTestBase(unittest.TestCase):
    #--------------------------------------------------------------
    # 워커 만들기 (시험이 끝나면 정리한다)
    #
    # -in: **kw = RagWorker 에 넘길 값
    #
    # -out: (worker, collector)
    # -out: error = 없음
    #--------------------------------------------------------------
    def make(self, **kw):
        col = Collector()
        kw.setdefault("start_timeout_s", 10)
        kw.setdefault("answer_timeout_s", 3)
        kw.setdefault("tick_s", 0.05)
        w = RagWorker(FAKE, col, **kw)
        self.addCleanup(w.shutdown)
        w.start()
        return w, col


class TestLifecycle(WorkerTestBase):
    #--------------------------------------------------------------
    # 준비 → 질문 → 답변까지 한 바퀴
    #--------------------------------------------------------------
    def test_ask_and_answer(self):
        w, col = self.make()
        w.ensure_started()
        self.assertTrue(wait_for(lambda: w.state == READY), "준비되지 않았다")

        w.ask("연차 이월 기준")
        self.assertTrue(wait_for(lambda: col.of("done")), "답변이 오지 않았다")

        ev = col.of("evidence")[0]
        self.assertEqual(len(ev[1]), 3)
        self.assertEqual(ev[2], 845)
        done = col.of("done")[0][1]
        self.assertIn("요약 답변입니다", done["answer"])
        self.assertEqual(done["ttft_s"], 1.24)
        self.assertTrue(wait_for(lambda: w.state == READY))

    #--------------------------------------------------------------
    # 한글 질문이 깨지지 않고 워커까지 간다 (stdin cp949 — P0-4)
    #--------------------------------------------------------------
    def test_korean_roundtrip(self):
        w, col = self.make()
        w.ask("경조사 지원 금액은 얼마인가요?")
        self.assertTrue(wait_for(lambda: col.of("done")))
        # 가짜 워커는 받은 질문을 답변에 그대로 넣는다 — 깨졌다면 여기서 드러난다
        self.assertIn("경조사 지원 금액은 얼마인가요?", col.answer_text())

    #--------------------------------------------------------------
    # 답변 중 새 질문이 오면 대기 칸이 새 것으로 바뀐다
    #--------------------------------------------------------------
    def test_pending_replaced(self):
        os.environ["FAKE_ANSWER_DELAY"] = "1.5"   # 답변이 느려야 대기 칸을 시험할 수 있다
        self.addCleanup(os.environ.pop, "FAKE_ANSWER_DELAY", None)
        w, col = self.make(answer_timeout_s=20)
        w.ensure_started()
        self.assertTrue(wait_for(lambda: w.state == READY))

        w.ask("첫 번째 질문")
        self.assertTrue(wait_for(lambda: w.state == BUSY, timeout=5))
        w.ask("두 번째 질문")
        w.ask("세 번째 질문")

        self.assertTrue(wait_for(lambda: len(col.of("done")) >= 2, timeout=15))
        sent = [e[1] for e in col.of("sent")]
        self.assertIn("첫 번째 질문", sent)
        self.assertIn("세 번째 질문", sent)
        self.assertNotIn("두 번째 질문", sent)      # 갈아 끼워졌다

    #--------------------------------------------------------------
    # 아직 준비 전에 물어도 준비된 뒤에 처리된다
    #--------------------------------------------------------------
    def test_ask_before_ready(self):
        w, col = self.make(idle_unload_min=0)
        os.environ["FAKE_READY_DELAY"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_READY_DELAY", None)
        w.ask("준비 전에 던진 질문")
        self.assertTrue(wait_for(lambda: col.of("done"), timeout=20))
        self.assertIn("준비 전에 던진 질문", col.answer_text())


class TestFailures(WorkerTestBase):
    #--------------------------------------------------------------
    # 답변이 오지 않으면 죽이고 다시 올린다
    #--------------------------------------------------------------
    def test_answer_timeout(self):
        os.environ["FAKE_HANG"] = "멈추는 질문"
        self.addCleanup(os.environ.pop, "FAKE_HANG", None)
        w, col = self.make(answer_timeout_s=2)
        w.ask("멈추는 질문")
        self.assertTrue(wait_for(lambda: any("답변이 멈췄" in e[1] for e in col.of("error")),
                                 timeout=20), "시간 초과 처리가 없었다")

    #--------------------------------------------------------------
    # 워커가 죽으면 알리고 다시 올린다
    #--------------------------------------------------------------
    def test_restart_after_crash(self):
        os.environ["FAKE_DIE_ON"] = "죽는 질문"
        self.addCleanup(os.environ.pop, "FAKE_DIE_ON", None)
        w, col = self.make()
        w.ask("죽는 질문")
        self.assertTrue(wait_for(lambda: col.of("error"), timeout=20), "종료를 알리지 않았다")
        # 죽인 질문은 다시 보내지 않고, 워커는 다시 준비 상태가 된다
        self.assertTrue(wait_for(lambda: w.state == READY, timeout=20), "다시 올라오지 않았다")
        self.assertEqual(len([e for e in col.of("sent") if e[1] == "죽는 질문"]), 1)

    #--------------------------------------------------------------
    # 설정 오류(코드 2)면 자동 재시작하지 않는다
    #--------------------------------------------------------------
    def test_config_error_no_restart(self):
        os.environ["FAKE_EXIT_CODE"] = "2"
        self.addCleanup(os.environ.pop, "FAKE_EXIT_CODE", None)
        w, col = self.make()
        w.ensure_started()
        self.assertTrue(wait_for(lambda: any("설정 오류" in e[1] for e in col.of("error")),
                                 timeout=20))
        time.sleep(1.0)
        self.assertEqual(w.state, STOPPED)

    #--------------------------------------------------------------
    # 어느 모델이 올라갔는지 알아낸다
    #=> 설정 파일을 뒤지지 않고도 로그·트레이에서 확인할 수 있어야 한다.
    #   워커가 시작하며 내는 "준비 완료 … / Qwen3-0.6B-Q4_K_M.gguf" 줄에서 뽑는다.
    #--------------------------------------------------------------
    def test_model_name(self):
        w, col = self.make()
        w.ensure_started()
        self.assertTrue(wait_for(lambda: w.state == READY))
        self.assertEqual(w.model, "Qwen3-0.6B-Q4_K_M.gguf", w.startup_lines)
        self.assertIn("준비 완료", w.ready_line)
        # 트레이 툴팁에는 짧게 — "Qwen3-0.6B"
        self.assertIn("Qwen3-0.6B", w.status_text())

    #--------------------------------------------------------------
    # 진짜 CLI 가 내는 꼬리표까지 붙은 줄에서도 파일 이름만 집는다
    #=> 실제 줄: "준비 완료 (9.3초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf (빠름 모드)"
    #   경로가 통째로 나오는 경우도 대비한다.
    #--------------------------------------------------------------
    def test_model_name_parsing(self):
        cases = [
            ("준비 완료 (9.3초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf (빠름 모드)",
             "Qwen3-0.6B-Q4_K_M.gguf"),
            ("준비 완료 (0.1초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf",
             "Qwen3-0.6B-Q4_K_M.gguf"),
            ("준비 완료 (12초) — 498청크 / D:" + chr(92) + "models" + chr(92)
             + "Qwen3-1.7B-Q4_K_M.gguf (정밀 모드)", "Qwen3-1.7B-Q4_K_M.gguf"),
        ]
        for line, want in cases:
            m = rag_worker.MODEL_RE.search(line)
            self.assertIsNotNone(m, line)
            self.assertEqual(m.group(1), want, line)

    #--------------------------------------------------------------
    # 인덱스가 잠겨 있으면 자동 재시작하지 않는다 (설계서 §10)
    #=> chat 이나 index 가 이미 돌고 있으면 Qdrant 가 폴더를 내주지 않는다.
    #   계속 다시 띄워 봐야 같은 이유로 죽으므로, 사용자에게 알리고 멈춘다.
    #--------------------------------------------------------------
    def test_index_lock_no_restart(self):
        os.environ["FAKE_LOCK"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_LOCK", None)
        w, col = self.make()
        w.ensure_started()
        self.assertTrue(wait_for(lambda: any("chat/index 가 실행 중입니다" in e[1]
                                             for e in col.of("error")), timeout=20),
                        "잠금 안내가 오지 않았다: {}".format(col.of("error")))
        time.sleep(1.0)
        self.assertEqual(w.state, STOPPED)

    #--------------------------------------------------------------
    # 재시작 한도를 넘으면 멈추고 알린다 (설계서 §10)
    #=> 5분 안에 restart_max 번 넘게 죽으면 더 띄우지 않는다.
    #   계속 되살리면 무한히 프로세스를 띄우며 CPU 만 태운다.
    #--------------------------------------------------------------
    def test_restart_limit(self):
        os.environ["FAKE_EXIT_CODE"] = "9"      # 설정 오류(2)가 아닌 이유로 계속 죽는다
        self.addCleanup(os.environ.pop, "FAKE_EXIT_CODE", None)
        w, col = self.make(restart_max=2)
        w.ensure_started()
        self.assertTrue(wait_for(lambda: any("반복해서 종료됩니다" in e[1]
                                             for e in col.of("error")), timeout=30),
                        "한도 초과 안내가 오지 않았다: {}".format(col.of("error")))
        time.sleep(1.0)
        self.assertEqual(w.state, STOPPED)

    #--------------------------------------------------------------
    # 근거가 없어도 답변 창이 빈 채로 남지 않는다 (인덱스가 비었을 때)
    #=> 진짜 CLI 는 "검색된 근거가 없습니다." 만 내고 답변 구분선을 내지 않는다.
    #   해석기가 이것을 raw 로 넘겨 창이 그 문구를 그대로 보여 준다.
    #--------------------------------------------------------------
    def test_no_evidence(self):
        os.environ["FAKE_NO_EVIDENCE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_NO_EVIDENCE", None)
        w, col = self.make()
        w.ensure_started()
        w.ask("아무 질문")
        self.assertTrue(wait_for(lambda: any("근거가 없습니다" in e[1] for e in col.of("raw")),
                                 timeout=20),
                        "안내 문구가 오지 않았다: {}".format(col.events[-5:]))

    #--------------------------------------------------------------
    # 오래 안 쓰면 모델을 내린다 (인덱스 잠금·메모리 반납)
    #--------------------------------------------------------------
    def test_idle_unload(self):
        w, col = self.make()
        w.idle_unload_s = 1               # 분 단위 설정 대신 초로 바꿔 빠르게 확인
        w.ensure_started()
        self.assertTrue(wait_for(lambda: w.state == READY))
        self.assertTrue(wait_for(lambda: w.state == STOPPED, timeout=10), "내려가지 않았다")


if __name__ == "__main__":
    unittest.main()
