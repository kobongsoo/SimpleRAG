#------------------------------------------------------------------
# chat_parser 시험 — SimpleRAG chat 출력 해석 (설계서 §6)
#=> 실제 출력과 같은 글을 여러 크기로 잘라 넣어도 같은 이벤트가 나오는지 본다.
#   파이프는 아무 데서나 잘려 들어오므로 이 성질이 중요하다.
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_parser import ChatParser  # noqa: E402

STARTUP = (
    "모델 적재 중...\n"
    "준비 완료 (4.5초) — 498청크 / Qwen3-0.6B-Q4_K_M.gguf\n"
    "생성 iGPU(Vulkan) / 리랭킹 켬(백그라운드 적재 중)\n"
    "질문을 입력하세요.  종료: exit 또는 Ctrl+D   도움말: /help\n\n"
    "질문> "
)

ANSWER = (
    "\n── 근거 3건 (845ms) ─────────────────────\n"
    "  [1] 규정_A.doc\n"
    "      표 조각 예시 본문입니다\n"
    "  [2] 규정_B.doc\n"
    "      두 번째 근거 본문\n"
    "  [3] 규정_C.docx\n"
    "      세 번째 근거 본문\n"
    "\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──\n"
    "요약한 답변 문장입니다. [1]"
    "\n\n── 소요 ─────────────────────────────────\n"
    "  검색 845ms (임베딩 6 + dense 81 + BM25 1 + 리랭킹 120)\n"
    "  첫 글자 1.24s / 완료 2.86s\n"
    "  인용 근거: [1]\n\n"
    "질문> "
)


#------------------------------------------------------------------
# 글자를 여러 조각으로 잘라 넣고 이벤트를 모으기
#=> 파이프가 어디서 자르든 결과가 같아야 한다.
#
# -in: text   = 넣을 글자
# -in: size   = 조각 크기(None 이면 통째로)
# -in: parser = 쓰던 해석기(없으면 새로 만든다)
#
# -out: (이벤트 목록, 해석기)
# -out: error = 없음
#------------------------------------------------------------------
def feed_chunks(text, size=None, parser=None):
    p = parser or ChatParser()
    events = []
    if size is None:
        events.extend(p.feed(text))
    else:
        for i in range(0, len(text), size):
            events.extend(p.feed(text[i:i + size]))
    return events, p


class TestChatParser(unittest.TestCase):
    #--------------------------------------------------------------
    # 준비 완료(첫 프롬프트) 판정
    #--------------------------------------------------------------
    def test_ready(self):
        events, _ = feed_chunks(STARTUP)
        self.assertEqual(events, [("ready",)])

    #--------------------------------------------------------------
    # 답변 한 건의 이벤트 순서와 내용
    #--------------------------------------------------------------
    def test_answer_flow(self):
        _, p = feed_chunks(STARTUP)
        p.begin_question()
        events, _ = feed_chunks(ANSWER, parser=p)

        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "evidence")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("token", kinds)

        ev = events[0]
        self.assertEqual(ev[2], 845)
        self.assertEqual(len(ev[1]), 3)
        self.assertEqual(ev[1][0]["no"], 1)
        self.assertEqual(ev[1][0]["doc"], "규정_A.doc")
        self.assertIn("표 조각", ev[1][0]["snippet"])

        done = events[-1][1]
        self.assertEqual(done["answer"], "요약한 답변 문장입니다. [1]")
        self.assertEqual(done["search_ms"], 845)
        self.assertEqual(done["ttft_s"], 1.24)
        self.assertEqual(done["total_s"], 2.86)
        self.assertEqual(done["cited"], "[1]")
        self.assertTrue(done["parsed"])

    #--------------------------------------------------------------
    # 조각 크기가 달라도 같은 결과 (핵심 — 파이프는 아무 데서나 잘린다)
    #--------------------------------------------------------------
    def test_chunk_independence(self):
        base = None
        for size in (1, 3, 7, 50, 500, None):
            _, p = feed_chunks(STARTUP, size=size)
            p.begin_question()
            events, _ = feed_chunks(ANSWER, size=size, parser=p)
            answer = "".join(e[1] for e in events if e[0] == "token")
            done = [e for e in events if e[0] == "done"][-1][1]
            got = (answer.strip(), done["ttft_s"], len(done["evidence"]))
            if base is None:
                base = got
            self.assertEqual(got, base, "조각 크기 {} 에서 결과가 다르다".format(size))

    #--------------------------------------------------------------
    # 답변 글자에 구분선·프롬프트가 새어 나오지 않는다
    #--------------------------------------------------------------
    def test_markers_not_leaked(self):
        _, p = feed_chunks(STARTUP)
        p.begin_question()
        events, _ = feed_chunks(ANSWER, size=2, parser=p)
        answer = "".join(e[1] for e in events if e[0] == "token")
        self.assertNotIn("── 소요", answer)
        self.assertNotIn("질문>", answer)

    #--------------------------------------------------------------
    # 근거 없이 끝나는 경우(검색 결과 없음)는 원문을 그대로 넘긴다
    #--------------------------------------------------------------
    def test_no_evidence_raw(self):
        _, p = feed_chunks(STARTUP)
        p.begin_question()
        events, _ = feed_chunks("검색된 근거가 없습니다.\n\n질문> ", parser=p)
        kinds = [e[0] for e in events]
        self.assertIn("raw", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertFalse(events[-1][1]["parsed"])

    #--------------------------------------------------------------
    # 질문 두 건을 잇달아 처리해도 상태가 남지 않는다
    #--------------------------------------------------------------
    def test_two_questions(self):
        _, p = feed_chunks(STARTUP)
        p.begin_question()
        feed_chunks(ANSWER, parser=p)
        p.begin_question()
        events, _ = feed_chunks(ANSWER, size=5, parser=p)
        done = [e for e in events if e[0] == "done"][-1][1]
        self.assertEqual(done["answer"], "요약한 답변 문장입니다. [1]")
        self.assertEqual(len(done["evidence"]), 3)


if __name__ == "__main__":
    unittest.main()
