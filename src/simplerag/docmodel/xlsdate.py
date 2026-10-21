# 이식 출처: MpowerClassify csoclassify/extract/xlsdate.py (2026-10-21 복사, SimpleRAG 전용 추출기용 — 공유하지 않음)
# -*- coding: utf-8 -*-
#------------------------------------------------------------------
# 엑셀 날짜 서식 해석 — Rust 판 Rust/src/xlsdate.rs 와 같은 규칙
#=> 엑셀은 날짜를 **숫자**로 저장한다. 2012-12-31 은 파일 안에 41274 로 들어
#   있고, "날짜처럼 보여라"는 **서식**이 따로 붙어 있을 뿐이다. 서식을 읽지
#   않으면 본문에 41274 가 그대로 나온다 — 사람이 표에서 보는 글자와 다르다.
#
#   [왜 두 판이 같아야 하나] 같은 문서를 두 구현으로 돌렸을 때 본문이 갈리면
#   등급도 갈린다. 그래서 판단 규칙(내장 서식 번호표·서식 문자열 해석·1900년
#   윤년 착각 보정)과 출력 모양(YYYY-MM-DD)을 Rust 판과 1:1 로 맞춘다.
#   한쪽만 고치면 두 판이 조용히 어긋난다.
#------------------------------------------------------------------
import datetime

# 서식의 종류 — Rust 판 DateKind 와 같은 뜻.
KIND_NONE = 0      # 날짜·시각과 무관(숫자를 그대로 둔다)
KIND_DATE = 1      # 날짜만 (2012-12-31)
KIND_TIME = 2      # 시각만 (13:34:00)
KIND_DATETIME = 3  # 둘 다 (2012-12-31 13:34:00)


#------------------------------------------------------------------
# 내장 서식 번호가 날짜/시각인가
#=> 엑셀에는 번호만으로 뜻이 정해진 '내장 서식'이 있다(0~163). 이 번호는 파일
#   안에 서식 문자열이 없으므로 표를 보고 판단해야 한다. xlsx 와 xls 가 같은
#   번호 체계를 쓴다.
#    1) 14~17 날짜 · 18~21 시각 · 22 날짜+시각
#    2) 27~36, 50~58 은 한중일 지역 날짜 서식이다
#    3) 45~47 은 경과시간(분:초 등)이라 시각 쪽이다
#
# -in: fmt_id = 서식 번호(numFmtId)
#
# -out: KIND_* 중 하나
# -out: error = 없음(모르는 번호는 KIND_NONE)
#------------------------------------------------------------------
def builtin_kind(fmt_id):
    if 14 <= fmt_id <= 17:
        return KIND_DATE
    if 18 <= fmt_id <= 21:
        return KIND_TIME
    if fmt_id == 22:
        return KIND_DATETIME
    # 한중일 지역 날짜 서식 묶음.
    if 27 <= fmt_id <= 36 or 50 <= fmt_id <= 58:
        return KIND_DATE
    if 45 <= fmt_id <= 47:
        return KIND_TIME
    return KIND_NONE


#------------------------------------------------------------------
# 사용자 지정 서식 문자열이 날짜/시각인가
#=> 사용자가 만든 서식(번호 164 이상)은 "yyyy-mm-dd" 같은 **문자열**로 온다.
#   글자를 훑어 날짜 기호(y·d)와 시각 기호(h·s)가 있는지 본다.
#    1) 세미콜론 앞 첫 구역만 본다 — 뒤는 음수·0·문자열용이라 양수 표시가 기준
#    2) 따옴표 안 글자와 [빨강] 같은 색·조건 구역은 서식 기호가 아니라 건너뛴다
#    3) [h] [m] [s] 는 '경과 시간' 기호라 시각으로 센다
#
#   [왜 m 만으로는 날짜라 하지 않나] m 은 '월'도 되고 '분'도 된다(mm:ss 의 mm 은
#   분이다). 혼자 있으면 가릴 수 없어, y 나 d 가 함께 있을 때만 날짜로 본다.
#   잘못 날짜로 보면 멀쩡한 숫자가 엉뚱한 날짜 글자로 바뀐다 — 그 쪽이 더 나쁘다.
#
# -in: code = 서식 문자열(예: 'yyyy"년" m"월"')
#
# -out: KIND_* 중 하나
# -out: error = 없음(판단이 안 서면 KIND_NONE)
#------------------------------------------------------------------
def code_kind(code):
    head = (code or "").split(";")[0]
    has_date = False
    has_time = False
    i = 0
    n = len(head)
    while i < n:
        ch = head[i]
        if ch == '"':
            # "..." 안은 그대로 찍히는 글자다 — 서식 기호가 아니다.
            i += 1
            while i < n and head[i] != '"':
                i += 1
        elif ch == "\\":
            # \x 는 바로 뒤 한 글자를 글자 그대로 쓰라는 뜻이다.
            i += 1
        elif ch == "[":
            # [빨강]·[>100] 같은 구역은 건너뛴다. 다만 [h]·[m]·[s] 는 경과 시간이다.
            j = head.find("]", i + 1)
            inner = head[i + 1:j] if j != -1 else head[i + 1:]
            t = inner.strip().lower()
            if t and all(c in "hms" for c in t):
                has_time = True
            i = j if j != -1 else n
        elif ch in "yYdD":
            has_date = True
        elif ch in "hHsS":
            has_time = True
        i += 1
    if has_date and has_time:
        return KIND_DATETIME
    if has_date:
        return KIND_DATE
    if has_time:
        return KIND_TIME
    return KIND_NONE


#------------------------------------------------------------------
# 엑셀 일련번호를 날짜 글자로
#=> 엑셀은 1900-01-01 을 1 로 두고 하루에 1씩 센다. 소수 부분은 하루 중 시각이다.
#    1) 정수 부분으로 날짜를, 소수 부분으로 시각을 만든다
#    2) 종류에 따라 날짜만·시각만·둘 다를 돌려준다
#
#   [1900년 2월 29일 문제] 엑셀은 1900년을 윤년으로 잘못 알고 있어, 실제로는
#   없는 1900-02-29 를 60번으로 세어 둔다. 그래서 60 이후는 하루가 밀려 있다.
#   61 이상은 기준일을 1899-12-30 으로, 59 이하는 1899-12-31 로 잡으면 두 구간이
#   모두 맞는다. 60 은 있지도 않은 날이라 그대로 적어 준다(엑셀 화면과 같게).
#
# -in: v    = 셀에 들어 있는 숫자(일련번호)
# -in: kind = 서식의 종류(KIND_*)
#
# -out: 사람이 읽는 글자. 바꿀 수 없으면 None(=숫자를 그대로 두라는 뜻)
# -out: error = 없음(범위를 벗어나면 None)
#------------------------------------------------------------------
def serial_to_string(v, kind):
    if kind == KIND_NONE:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    # 9999-12-31 이 2958465 다. 그보다 크거나 음수면 날짜로 볼 수 없다.
    if v != v or v < 0.0 or v >= 2958466.0:
        return None

    days = int(v // 1)
    # 하루를 초로 나눈 나머지. 반올림해야 0.5 가 12:00:00 으로 떨어진다.
    secs = int(round((v - days) * 86400))
    # 23:59:59.7 처럼 반올림이 하루를 넘기면 날짜를 하루 올린다.
    if secs >= 86400:
        secs -= 86400
        days += 1
    time_s = "%02d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)

    if kind == KIND_TIME:
        return time_s

    if days == 60:
        # 엑셀에만 있는 가짜 날짜다 — 계산하지 말고 그대로 적는다.
        date_s = "1900-02-29"
    else:
        base = (datetime.date(1899, 12, 30) if days >= 61
                else datetime.date(1899, 12, 31))
        try:
            date_s = (base + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        except (OverflowError, ValueError):
            return None

    if kind == KIND_DATE:
        return date_s
    return "%s %s" % (date_s, time_s)
