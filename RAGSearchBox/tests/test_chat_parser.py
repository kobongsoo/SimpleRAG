#------------------------------------------------------------------
# chat_parser 시험 — SimpleRAG chat 출력 해석 (설계서 §6)
#=> 실제 출력과 같은 글을 여러 크기로 잘라 넣어도 같은 이벤트가 나오는지 본다.
#   파이프는 아무 데서나 잘려 들어오므로 이 성질이 중요하다.
#------------------------------------------------------------------
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_parser import ChatParser, meta_line, setup_bits  # noqa: E402

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
    # 근거 전체 본문 줄(@ev)을 같은 번호 근거에 붙인다 — [...] 팝업이 쓴다
    #=> 줄바꿈·한글이 든 본문도 ASCII JSON 한 줄로 오므로 조각 크기와 무관하게 같아야 한다.
    #--------------------------------------------------------------
    def test_evidence_full(self):
        full_line = '  @ev {"no": 2, "path": "D:\\\\docs\\\\B.doc", "text": "\\uc804\\uccb4\\n\\ubcf8\\ubb38"}\n'
        text = ANSWER.replace("      두 번째 근거 본문\n", "      두 번째 근거 본문\n" + full_line)
        for size in (None, 7):
            _, p = feed_chunks(STARTUP)
            p.begin_question()
            events, _ = feed_chunks(text, size=size, parser=p)
            items = events[0][1]
            self.assertEqual(items[1]["full"], "전체\n본문")
            self.assertEqual(items[1]["path"], "D:\\docs\\B.doc")
            self.assertNotIn("full", items[0])            # 줄이 없는 근거는 그대로
            self.assertEqual(items[1]["snippet"], "두 번째 근거 본문")

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

    #--------------------------------------------------------------
    # 소요 블록의 '답변모델 · parent' 줄을 읽는다 — 어디서 잘려 들어와도 같다
    #--------------------------------------------------------------
    def test_setup_line(self):
        answer = ANSWER.replace(
            "  첫 글자 1.24s / 완료 2.86s\n",
            "  첫 글자 1.24s / 완료 2.86s\n"
            "  답변모델 midm-2.0-mini-q4 (한국어) · parent section(상한 400토큰)\n")
        for size in (None, 1, 7):
            _, p = feed_chunks(STARTUP)
            p.begin_question()
            events, _ = feed_chunks(answer, size=size, parser=p)
            done = [e for e in events if e[0] == "done"][-1][1]
            self.assertEqual((done["model"], done["model_mode"], done["parent"]),
                             ("midm-2.0-mini-q4", "한국어", "section(상한 400토큰)"))
            self.assertEqual(done["ttft_s"], 1.24)
            self.assertEqual(setup_bits(done),
                             ["midm-2.0-mini-q4(한국어)", "parent section(상한 400토큰)"])

    #--------------------------------------------------------------
    # 예전 simplerag.exe 처럼 그 줄이 없으면 비워 두고 아무것도 붙이지 않는다
    #--------------------------------------------------------------
    def test_setup_line_missing(self):
        _, p = feed_chunks(STARTUP)
        p.begin_question()
        events, _ = feed_chunks(ANSWER, parser=p)
        done = [e for e in events if e[0] == "done"][-1][1]
        self.assertIsNone(done["model"])
        self.assertIsNone(done["parent"])
        self.assertEqual(setup_bits(done), [])

    #--------------------------------------------------------------
    # 답 아래 소요 줄 — 검색/첫 글자/완료 모두 초 · 모델(정밀) · parent 는 빼고 · 안내는 AI 답이 있을 때만
    #   (Rust ui/panel.html metaLine 과 같은 글)
    #--------------------------------------------------------------
    def test_meta_line(self):
        done = {"search_ms": 513, "ttft_s": 6.85, "total_s": 9.64, "model": "qwen3-4b-q4",
                "model_mode": "정밀", "parent": "section(상한 400토큰)"}
        self.assertEqual(meta_line(done, True),
                         "정확한 내용은 아래 출처로 확인하세요.  0.51s/6.85s/9.64s , qwen3-4b-q4(정밀)")
        self.assertEqual(meta_line(dict(done, model_mode=None, model="qwen3-1.7b-q4"), False),
                         "0.51s/6.85s/9.64s , qwen3-1.7b-q4")
        self.assertEqual(meta_line({}, True), "정확한 내용은 아래 출처로 확인하세요.")
        self.assertEqual(meta_line({}, False), "")


if __name__ == "__main__":
    unittest.main()
