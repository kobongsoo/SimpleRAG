#------------------------------------------------------------------
# SimpleRAG chat 출력 해석 (설계서 §6, 결정 D3)
#=> 워커(simplerag.exe chat)가 사람 보라고 찍는 글자를 이벤트로 바꾼다.
#   SimpleRAG 를 고치지 않기로 했으므로(D3) 구분선 문구에 기댈 수밖에 없다.
#   그래서 기대는 문구를 여기 한곳에 상수로 모아 둔다 — CLI 가 바뀌면 여기만 고친다.
#
#   내는 이벤트 (SimpleRAG pipeline.answer 의 이벤트 이름과 맞춘다)
#     ("ready",)                     첫 프롬프트 = 모델 적재 끝
#     ("evidence", [근거...], ms)    근거 블록
#     ("token", "글자")              답변 조각
#     ("done", {...})                답변 끝(다음 프롬프트)
#     ("raw", "원문")                해석 실패 — 창에 원문을 그대로 보여 준다
#
#   왜 줄 단위로 읽지 않는가
#     프롬프트 "질문> " 는 줄바꿈 없이 끝나고 답변 토큰도 줄바꿈 없이 흘러온다.
#     줄 단위로 기다리면 영원히 오지 않는다.
#------------------------------------------------------------------

import re

PROMPT = "질문> "
EVIDENCE_MARK = "── 근거 "
ANSWER_MARK = "── 답변 "
TIMING_MARK = "── 소요"

# "── 근거 3건 (845ms) ────"
RE_EV_HEAD = re.compile(r"── 근거 (\d+)건 \((\d+)ms\)")
# "  [1] 문서이름.doc"
RE_EV_ITEM = re.compile(r"^\s{2}\[(\d+)\]\s+(.+?)\s*$")
# "  첫 글자 1.24s / 완료 2.86s"
RE_TTFT = re.compile(r"첫 글자 ([\d.]+)s / 완료 ([\d.]+)s")
# "  검색 845ms (임베딩 6 + dense 81 + BM25 1 + 리랭킹 120)"
RE_SEARCH = re.compile(r"검색 (\d+)ms")
# "  인용 근거: [1], [3]"
RE_CITED = re.compile(r"인용 근거: (.+)")

_STATE_STARTING = "starting"   # 모델 적재 중 — 첫 프롬프트를 기다린다
_STATE_IDLE = "idle"           # 질문을 받을 수 있음
_STATE_EVIDENCE = "evidence"   # 근거 블록을 모으는 중
_STATE_ANSWER = "answer"       # 답변이 흘러오는 중
_STATE_TIMING = "timing"       # 소요 블록을 모으는 중


#------------------------------------------------------------------
# 출력 해석기
#=> 파이프에서 읽은 덩어리를 feed() 로 넣으면 이벤트 목록을 돌려준다.
#   덩어리가 어디서 잘려 들어와도 같은 결과가 나와야 한다(시험으로 지킨다).
#
# -필드: state      = 지금 어느 구간을 읽는 중인지
# -필드: buf        = 아직 해석하지 못한 글자
# -필드: n_evidence = 이번 답변의 근거 개수
#------------------------------------------------------------------
class ChatParser:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self):
        self.state = _STATE_STARTING
        self.buf = ""
        self.n_evidence = 0
        self._evidence = []
        self._ev_ms = 0
        self._answer = []
        self._timing_text = ""
        self._saw_evidence = False

    #--------------------------------------------------------------
    # 질문을 보냈다고 알리기
    #=> 답변 하나가 시작되므로 모아 둔 것을 비운다.
    #
    # -in: 없음
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def begin_question(self):
        self.state = _STATE_IDLE
        self._evidence = []
        self._ev_ms = 0
        self._answer = []
        self._timing_text = ""
        self._saw_evidence = False
        self.n_evidence = 0

    #--------------------------------------------------------------
    # 파이프에서 읽은 글자 넣기 (핵심)
    #=> 버퍼에 붙이고, 지금 상태에서 뽑아낼 수 있는 이벤트를 모두 뽑는다.
    #
    # -in: text = 새로 도착한 글자(빈 문자열 가능)
    #
    # -out: 이벤트 목록(없으면 빈 목록)
    # -out: error = 없음 (알 수 없는 출력은 ("raw", ...) 로 돌려준다)
    #--------------------------------------------------------------
    def feed(self, text):
        if text:
            self.buf += text
        out = []
        while self._step(out):
            pass
        return out

    #--------------------------------------------------------------
    # 한 걸음 해석
    #=> 상태별로 버퍼 앞부분을 잘라 이벤트를 만든다.
    #
    # -in: out = 이벤트를 담을 목록
    #
    # -out: True = 뭔가 처리했으니 한 번 더 돌 것
    # -out: error = 없음
    #--------------------------------------------------------------
    def _step(self, out):
        if self.state == _STATE_STARTING:
            i = self.buf.find(PROMPT)
            if i < 0:
                return False
            self.buf = self.buf[i + len(PROMPT):]
            self.state = _STATE_IDLE
            out.append(("ready",))
            return True

        if self.state == _STATE_IDLE:
            i = self.buf.find(EVIDENCE_MARK)
            if i >= 0:
                self.buf = self.buf[i:]
                self.state = _STATE_EVIDENCE
                self._saw_evidence = True
                return True
            # 근거 없이 프롬프트가 오면(예: "검색된 근거가 없습니다") 원문을 그대로 넘긴다
            j = self.buf.find(PROMPT)
            if j >= 0:
                raw = self.buf[:j].strip()
                self.buf = self.buf[j + len(PROMPT):]
                if raw:
                    out.append(("raw", raw))
                out.append(("done", self._done_info()))
                return True
            return False

        if self.state == _STATE_EVIDENCE:
            k = self.buf.find(ANSWER_MARK)
            if k < 0:
                return False
            block = self.buf[:k]
            rest = self.buf[k:]
            # 답변 구분선 줄이 끝나야(줄바꿈) 본문이 시작된다 — 아직이면 더 기다린다
            nl = rest.find("\n")
            if nl < 0:
                return False
            self.buf = rest[nl + 1:]
            self._parse_evidence(block)
            self.state = _STATE_ANSWER
            out.append(("evidence", list(self._evidence), self._ev_ms))
            return True

        if self.state == _STATE_ANSWER:
            t = self.buf.find(TIMING_MARK)
            if t >= 0:
                body = self.buf[:t]
                self.buf = self.buf[t:]
                if body:
                    self._answer.append(body)
                    out.append(("token", body))
                self.state = _STATE_TIMING
                return True
            # 구분선이 잘려 들어올 수 있으니 끝부분을 그만큼 남겨 둔다
            hold = len(TIMING_MARK) + 2
            if len(self.buf) > hold:
                send = self.buf[:-hold]
                self.buf = self.buf[-hold:]
                self._answer.append(send)
                out.append(("token", send))
                return True
            return False

        if self.state == _STATE_TIMING:
            p = self.buf.find(PROMPT)
            if p < 0:
                return False
            self._timing_text = self.buf[:p]
            self.buf = self.buf[p + len(PROMPT):]
            self.state = _STATE_IDLE
            out.append(("done", self._done_info()))
            return True

        return False

    #--------------------------------------------------------------
    # 근거 블록 해석
    #=> "── 근거 3건 (845ms)" 머리와 "  [1] 문서" / "      본문" 줄을 나눈다.
    #
    # -in: block = 근거 구분선부터 답변 구분선 직전까지의 글자
    #
    # -out: 없음 (self._evidence, self._ev_ms 를 채운다)
    # -out: error = 없음 (형식이 어긋난 줄은 건너뛴다)
    #--------------------------------------------------------------
    def _parse_evidence(self, block):
        m = RE_EV_HEAD.search(block)
        if m:
            self.n_evidence = int(m.group(1))
            self._ev_ms = int(m.group(2))
        items = []
        for line in block.splitlines():
            hit = RE_EV_ITEM.match(line)
            if hit:
                items.append({"no": int(hit.group(1)), "doc": hit.group(2), "snippet": ""})
            elif items and line.startswith("      "):
                items[-1]["snippet"] = (items[-1]["snippet"] + " " + line.strip()).strip()
        self._evidence = items

    #--------------------------------------------------------------
    # 답변 끝 정보 만들기
    #=> 소요 블록에서 숫자를 뽑아 창 아래에 보여 줄 값으로 만든다.
    #
    # -in: 없음
    #
    # -out: dict = {answer, evidence, search_ms, ttft_s, total_s, cited, warnings, parsed}
    # -out: error = 없음 (못 뽑은 값은 None)
    #--------------------------------------------------------------
    def _done_info(self):
        t = self._timing_text
        ttft = RE_TTFT.search(t)
        search = RE_SEARCH.search(t)
        cited = RE_CITED.search(t)
        warns = [ln.strip() for ln in t.splitlines() if "⚠" in ln]
        return {
            "answer": "".join(self._answer).strip(),
            "evidence": list(self._evidence),
            "search_ms": int(search.group(1)) if search else None,
            "ttft_s": float(ttft.group(1)) if ttft else None,
            "total_s": float(ttft.group(2)) if ttft else None,
            "cited": cited.group(1).strip() if cited else "",
            "warnings": warns,
            "parsed": self._saw_evidence,
        }
