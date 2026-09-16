#------------------------------------------------------------------
# 검색어 거르기 (설계서 §5 T4)
#=> 탐색기 검색창에서 읽은 글자를 SimpleRAG 워커에 넘길 질문으로 바꾼다.
#   원본 MDriveSearchBox 에는 없던 부분이다(원본은 검색어를 읽지 않는다).
#
#   여기서 하는 일은 네 가지다.
#    1) '?' 로 시작하는 것만 질문으로 본다 — 일반 파일 검색까지 답변 창이 뜨면 성가시다
#    2) 워커는 input() 으로 한 줄씩 읽으므로 줄바꿈을 공백으로 바꾼다
#    3) chat 의 슬래시 명령·종료어로 읽힐 글자를 피한다 (질문이 워커를 조작하면 안 된다)
#    4) 같은 질문이 연달아 들어오면 무시한다
#------------------------------------------------------------------

import time

# 전각 물음표도 같은 접두어로 본다 — 한글 입력 상태에서 그대로 쳐지는 일이 잦다
FULLWIDTH = "？"
# chat 이 종료로 읽는 낱말 (cli.cmd_chat 의 목록과 같아야 한다)
EXIT_WORDS = ("exit", "quit", "종료", "/exit", "/quit")


#------------------------------------------------------------------
# 검색어를 질문으로 바꾸기 (핵심)
#=> 접두어를 떼고 한 줄로 만든 뒤, 워커가 명령으로 오해할 여지를 없앤다.
#
#   예) "?연차 이월 기준"   -> "연차 이월 기준"
#       "？ 연차"            -> "연차"
#       "?? 연차"            -> "연차"      (앞쪽 물음표를 모두 뗀다)
#       "?/topk 5"           -> "topk 5"    (슬래시 명령 회피)
#       "?종료"              -> "종료 ?"    (워커 종료 회피)
#       "보고서"             -> None        (접두어가 없으니 파일 검색으로 둔다)
#
# -in: text      = 검색창에서 읽은 원본 글자(None 가능)
# -in: prefix    = 질문으로 볼 접두어(기본 "?")
# -in: min_chars = 접두어를 뗀 질문의 최소 길이
#
# -out: (질문, None)        = 통과
# -out: (None, 사유 문자열) = 거름 (사유는 DIAG 로그용)
# -out: error = 없음 (예외를 내지 않는다 — 감시 루프가 멈추면 안 된다)
#------------------------------------------------------------------
def to_question(text, prefix="?", min_chars=2):
    if not text:
        return None, "빈 값"

    s = text.strip()
    if not s:
        return None, "공백뿐"

    # 접두어 판정 — 설정한 접두어, 기본값일 때는 전각 물음표도 인정
    heads = (prefix, FULLWIDTH) if prefix == "?" else (prefix,)
    if not s.startswith(heads):
        return None, "접두어 없음"

    # 앞쪽 접두어와 공백을 모두 뗀다 ("?? 연차" 처럼 두 번 친 경우 포함)
    while s and (s.startswith(heads) or s[0].isspace()):
        s = s[len(prefix):] if s.startswith(prefix) else s[1:]
        s = s.lstrip()
    if not s:
        return None, "접두어뿐"

    # 워커는 한 줄만 읽는다 — 줄바꿈·탭을 공백으로 바꾸고 연속 공백을 접는다
    s = " ".join(s.split())

    # chat 의 슬래시 명령으로 읽히지 않게 앞의 '/' 를 뗀다
    s = s.lstrip("/").strip()
    if not s:
        return None, "슬래시뿐"

    if len(s) < min_chars:
        return None, "{}자 미만".format(min_chars)

    # 종료어와 똑같으면 워커가 꺼진다 — 뒤에 물음표를 붙여 질문으로 만든다
    if s.lower() in EXIT_WORDS:
        s = s + " ?"

    return s, None


#------------------------------------------------------------------
# 같은 질문 거르기
#=> 한 번의 Enter 가 두 비트(누름·눌렸음)로 두 번 읽히거나 사용자가 연달아
#   Enter 를 눌러도 워커를 두 번 돌리지 않는다.
#
# -필드: window_sec = 이 시간 안의 같은 질문은 무시
#------------------------------------------------------------------
class Dedup:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: window_sec = 무시할 시간(초). 0 이면 중복을 거르지 않는다
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, window_sec=3):
        self.window_sec = window_sec
        self._last_q = None
        self._last_t = 0.0

    #--------------------------------------------------------------
    # 중복인지 보고 기록하기
    #=> 중복이 아니면 이번 질문을 기록하고 False 를 돌려준다.
    #
    # -in: question = 거를 질문
    # -in: now      = 현재 시각(초). 시험에서 시간을 넣기 위해 인자로 받는다
    #
    # -out: True = 중복이라 무시해야 함
    # -out: error = 없음
    #--------------------------------------------------------------
    def is_duplicate(self, question, now=None):
        now = time.monotonic() if now is None else now
        if (self.window_sec > 0 and question == self._last_q
                and (now - self._last_t) < self.window_sec):
            return True
        self._last_q = question
        self._last_t = now
        return False
