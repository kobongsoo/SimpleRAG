#------------------------------------------------------------------
# 답변 출력 관리 — 반복 루프 감지 · 상한에서 완결 문장까지 (Rust 판 generator.rs 의 OutGuard 와 같다)
#=> 온도 0(탐욕 선택)은 같은 문장을 되풀이하는 루프에 빠질 수 있다. 반복 벌점은 재현성 때문에
#   넣지 않고, 대신 출력 쪽에서 막는다.
#    1) 앞에 나온 글과 같은 꼬리가 HOLD_MIN_CHARS 이상이면 반복일 수 있어 내보내지 않고 붙든다
#    2) 그 꼬리가 LOOP_MIN_CHARS 에 이르면 반복으로 보고 멈춘다 — 반복이 시작된 문장 경계에서 자른다
#    3) 상한(max_tokens)까지 TAIL_WINDOW 토큰 안쪽이면 덜 끝난 문장을 붙든다
#    4) 상한에서 멈추면 마지막 완결 문장까지만 남기고 NOTE_CAPPED 표시를 단다
#   화면에 나간 글은 되돌릴 수 없으므로, 나중에 잘라 낼 수 있는 꼬리만 붙들어 둔다.
#   중단 문자열은 llama-cpp-python 이 이미 처리하므로 여기서는 보지 않는다.
#------------------------------------------------------------------

import re

# 앞에 나온 글과 같은 꼬리가 이만큼(글자) 이어지면 반복 루프로 본다.
#   표 줄·"[1] 근거에 따르면," 같은 정상적인 짧은 되풀이는 이보다 짧다.
LOOP_MIN_CHARS = 40
# 앞에 나온 글과 같은 꼬리가 이만큼(글자) 넘으면 반복일 수 있어 붙든다
HOLD_MIN_CHARS = 8
# 상한까지 이만큼(토큰) 남으면 덜 끝난 문장을 붙든다
TAIL_WINDOW = 48
# 쓰는 중인 문장의 머리 OPEN_HEAD_MAX 자(인용 번호 "[3] " 등)만 빼고 나머지 OPEN_REP_MIN 자 이상이
#   앞에 나온 글과 같으면 반복의 첫머리로 보고 문장째 붙든다
OPEN_HEAD_MAX = 6
OPEN_REP_MIN = 3
# 한 번 반복의 첫머리로 보고 붙든 문장은, 같은 문장 안에서 앞 글과 같은 꼬리가 이만큼(글자) 이상
#   이어지는 동안 계속 붙든다. 반복은 앞의 다른 곳으로 갈아타며 이어지기도 한다 — 골든셋 q096:
#   "근거 [1]에 따르면, 2021년 " (둘째 문장 머리) → " 대비 2026년 …" (첫 문장 중간)으로 갈아타는 순간
#   같은 꼬리가 "2021년 대" 7자로 짧아져 위 조건이 풀리고 문장 머리가 화면에 나갔다
STICKY_REP_MIN = 5
NOTE_CAPPED = "(답변이 길어 일부 생략)"
NOTE_LOOP = "(같은 내용이 반복되어 이하 생략)"

# 문장 끝 바로 뒤에 붙은 인용 번호 하나 " [1]"
_CITE_AFTER = re.compile(r"[ \t]*\[\d+\]")


#------------------------------------------------------------------
# 문장 끝 뒤의 인용 번호를 그 문장으로 넣기
#=> " [1][2]" 처럼 이어진 번호를 하나씩 보며, 뒤에 공백·글 끝이 오는 번호까지만 넣는다.
#   "[3]에 따르면" 처럼 낱말이 바로 붙은 번호는 다음 문장의 머리라 넣지 않는다.
#
# -in: s = 글 / p = 문장부호 바로 뒤 위치
#
# -out: p = 인용 번호를 넣은 뒤의 위치(넣을 것이 없으면 그대로)
# -out: error = 없음
#------------------------------------------------------------------
def _absorb_cites(s, p):
    best = p
    q = p
    while True:
        m = _CITE_AFTER.match(s, q)
        if not m:
            return best
        q = m.end()
        # 번호 뒤가 공백·글 끝이면 여기까지는 앞 문장의 인용이다
        if q >= len(s) or s[q].isspace():
            best = q


#------------------------------------------------------------------
# 앞에 나온 글과 같은 가장 긴 꼬리(글자 수)
#=> 뒤집은 글의 Z-함수: z[i] = 맨 앞(= 원래의 꼬리)과 i 에서 시작하는 부분이 같은 길이.
#   i ≥ 1 의 최댓값이 "앞쪽 어딘가에 한 번 더 나온" 가장 긴 꼬리다(겹쳐도 된다 — 주기 반복).
#
# -in: s = 지금까지 생성된 글
#
# -out: n = 길이(글자)
# -out: error = 없음
#------------------------------------------------------------------
def repeated_suffix_len(s):
    r = s[::-1]
    n = len(r)
    z = [0] * n
    l = rr = best = 0
    for i in range(1, n):
        if i < rr:
            z[i] = min(rr - i, z[i - l])
        # 같은 글자가 이어지는 동안 늘린다
        while i + z[i] < n and r[z[i]] == r[i + z[i]]:
            z[i] += 1
        if i + z[i] > rr:
            l, rr = i, i + z[i]
        best = max(best, z[i])
    return best


#------------------------------------------------------------------
# 문장이 끝나는 자리(글자 위치, 오름차순)
#=> 줄바꿈 뒤, 또는 '.' '!' '?' '。' 뒤에 공백·'['·글 끝이 오는 자리.
#    1) 바로 뒤에 붙은 인용 번호 " [1][2]" 는 그 문장에 넣는다
#    2) 글 끝의 "6." 처럼 숫자 뒤 마침표는 소수점이 이어질 수 있어 문장 끝으로 보지 않는다
#
# -in: s = 글
#
# -out: ends = 위치 목록
# -out: error = 없음
#------------------------------------------------------------------
def sentence_ends(s):
    out = []
    n = len(s)
    for k, c in enumerate(s):
        if c == "\n":
            end = True
        elif c in ".!?。":
            if k + 1 >= n:
                # 글 끝의 "숫자." 는 소수점일 수 있다
                end = not (c == "." and k > 0 and s[k - 1].isascii() and s[k - 1].isdigit())
            else:
                nc = s[k + 1]
                end = nc.isspace() or nc == "["
        else:
            end = False
        if not end:
            continue
        p = k + 1
        if c != "\n":
            p = _absorb_cites(s, p)
        if not out or p > out[-1]:
            out.append(p)
    return out


#------------------------------------------------------------------
# 출력 관리기
#=> 조각을 push() 로 넣으면 내보내도 되는 글을 돌려주고, 멈춰야 하면 halt 를 세운다.
#   끝나면 finish() 로 남은 꼬리와 표시를 받는다.
#
# -필드: text = 지금까지 생성된 글 / sent = 내보낸 글자 수
# -필드: halt = None | ("loop", 자를 위치) | ("capped", None)
# -필드: held_open = 반복 의심으로 붙든 문장의 시작 위치(None = 없음) — STICKY_REP_MIN 참고
#------------------------------------------------------------------
class OutGuard:
    #------------------------------------------------------------------
    # 생성자 — 빈 상태로 시작
    #
    # -in: 없음
    #------------------------------------------------------------------
    def __init__(self):
        self.text = ""
        self.sent = 0
        self.halt = None
        self.held_open = None

    #------------------------------------------------------------------
    # 조각 하나 받기
    #=> 1) 앞의 글과 같은 꼬리가 LOOP_MIN_CHARS 이상이면 halt=("loop", 그 꼬리 안의 첫 문장 경계)
    #    2) 아니면 붙들 꼬리(반복 의심 · 상한 직전의 덜 끝난 문장)를 뺀 앞부분을 내보낸다
    #
    # -in: piece = 새 조각 / left = 상한까지 남은 토큰 수
    #
    # -out: emit = 지금 내보낼 글("" 이면 없음). halt 가 서면 호출자는 생성을 멈춘다
    # -out: error = 없음
    #------------------------------------------------------------------
    def push(self, piece, left):
        self.text += piece
        n = len(self.text)
        rep = repeated_suffix_len(self.text)
        if rep >= LOOP_MIN_CHARS:
            # 겹친 꼬리가 앞 문장 끝("니다. ")부터 걸칠 수 있다 — 그 안의 첫 문장 경계에서 자른다
            start = n - rep
            cut = next((b for b in sentence_ends(self.text) if start <= b < n), start)
            # 이미 내보낸 곳보다 앞일 수도 있다 — 남길 답은 거기까지, 나간 꼬리는 출력기가 붙들고 있으면 버린다
            self.halt = ("loop", cut)
            return ""
        hold = rep if rep >= HOLD_MIN_CHARS else 0
        ends = sentence_ends(self.text)
        last = ends[-1] if ends else 0
        # 쓰는 중인 문장이 (머리 몇 글자만 빼고) 앞에 나온 글과 같다 — 반복의 첫머리일 수 있으니 문장째 붙든다
        #   (8자 문턱만으로는 반복 문장의 첫 몇 글자 "[1] 기내" 가 먼저 화면에 나가 조각으로 남았다.
        #    반복 문장은 머리의 인용 번호만 바뀌어 오기도 한다 — "[1] 청탁금지법…" → "[3] 청탁금지법")
        #   둘째 문장부터는 머리 OPEN_HEAD_MAX 자를 늘 붙든다 — 머리("[2" 등)만으로는 반복인지 알 수 없다
        open_len = len(self.text[last:].lstrip())
        if open_len > 0 and ((last > 0 and open_len <= OPEN_HEAD_MAX) or open_len <= rep
                             or (rep >= OPEN_REP_MIN and open_len - rep <= OPEN_HEAD_MAX)):
            hold = max(hold, n - max(last, self.sent))
            if last > 0 and open_len > OPEN_HEAD_MAX:
                # 머리 몇 글자가 아니라 문장 자체가 앞 글과 겹쳐 붙든 것 — 이 문장을 기억해 둔다
                self.held_open = last
        elif self.held_open == last and rep >= STICKY_REP_MIN:
            # 반복 의심 문장이 앞의 다른 곳으로 갈아타며 계속 겹친다 — 문장째 계속 붙든다(q096)
            hold = max(hold, n - max(last, self.sent))
        else:
            self.held_open = None
        if left <= TAIL_WINDOW:
            # 상한이 가깝다 — 마지막 완결 문장 뒤는 잘릴 수 있으니 붙든다
            hold = max(hold, n - max(last, self.sent))
        safe = n - hold
        if safe > self.sent:
            emit = self.text[self.sent:safe]
            self.sent = safe
            return emit
        return ""

    #------------------------------------------------------------------
    # 마무리 — 멈춘 까닭에 따라 끝을 정하고 남은 꼬리를 내보낸다
    #=> · 자연스럽게 끝남 / 중단 문자열: 그대로
    #    · 반복 루프: 반복이 시작된 곳 앞까지. 그곳이 문장 중간이면 마지막 완결 문장까지 + 표시
    #    · 상한: 마지막 완결 문장까지 + 표시
    #    이미 내보낸 글자보다 앞에서 끝날 수도 있다 — 남길 답(text)은 거기까지로 돌려주고,
    #    이미 나간 꼬리는 화면 출력기(StreamWriter.finish(final=…))가 아직 붙들고 있으면 버린다.
    #    완결 문장이 하나도 없으면 있는 그대로 둔다(빈 답보다 낫다).
    #
    # -in: capped = 상한에 닿아 멈췄나(호출자가 finish_reason 으로 안다)
    #
    # -out: (emit, text, note) = 마저 내보낼 글 · 남긴 답 전체 · 표시(없으면 None)
    # -out: error = 없음
    #------------------------------------------------------------------
    def finish(self, capped=False):
        if self.halt is None and capped:
            self.halt = ("capped", None)
        end = self.halt[1] if self.halt and self.halt[0] == "loop" else len(self.text)
        note = None
        if self.halt:
            head = self.text[:end]
            ends = sentence_ends(head)
            last = ends[-1] if ends else 0
            # 마지막 문장이 덜 끝났나
            open_ = last < len(head.rstrip())
            if self.halt[0] == "capped":
                note = NOTE_CAPPED
            elif open_:
                note = NOTE_LOOP
            if open_ and last > 0:
                end = last
        emit = self.text[self.sent:end] if end > self.sent else ""
        self.sent = max(self.sent, end)
        return emit, self.text[:end], note
