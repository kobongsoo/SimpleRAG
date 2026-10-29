#------------------------------------------------------------------
# 메타데이터 질의 해석 · 필터 · 가산 · 완화 (메타데이터 검색 설계서 §12 · §13 · 결정 D3 · D4)
#=> "작년에 고봉수님이 만든 절차서 양식을 찾아줘" 를
#     조건(사람 · 기간 · 형식 · 출처 · 스캔 · 유형 · 양식) + 본문 검색어("절차서 양식") 로 나누고,
#   meta.sqlite 로 문서 범위(필터)와 점수 보정(가산)을 만든다. 규칙만 쓴다(LLM 없음, 같은 질의엔 같은 해석).
#
#   parse(query)  → 조건 · 본문 검색어 · 화면 칩
#   plan(query)   → parse + 필터할 문서 집합 · 가산 점수 · 완화 내역 (0건이면 조건을 하나씩 푼다)
#
#   ⚠️ 사람 이름은 색인에 사람으로 나온 이름(doc_people) · 별칭 사전에 있는 것만 사람으로 본다
#      ("규정" · "연차" 같은 낱말이 이름으로 잡히는 사고를 막는다, §12-2).
#   ⚠️ 유형어 · 양식어는 조건으로도 쓰고 본문 검색어에도 남긴다. 사람 · 시간 · 형식 · 명령어는 뗀다.
#------------------------------------------------------------------

import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

from . import normalize as N
from . import store as S
from .rules import load_rules

KST = timezone(timedelta(hours=9))

# 형식 낱말 → 형식 (§12-2)
FORMAT_WORDS = [
    (r"엑셀\s*파일|엑셀|xlsx|xls", "excel"),
    (r"피피티|파워\s*포인트|ppt|pptx|발표\s*자료", "ppt"),
    (r"한글\s*파일|한글\s*문서|hwpx|hwp", "hwp"),
    (r"pdf|피디에프", "pdf"),
    (r"워드\s*파일|워드\s*문서|워드|docx", "word"),
]
# 받은 경로
RECV_WORDS = [
    (r"(?:이?메일|첨부)(?:로|에서|으로)?\s*(?:받은|온|왔던|받았던)", "mail"),
    (r"(?:구글\s*)?드라이브(?:에서|로)?\s*(?:받은|내려받은)", "drive"),
    (r"(?:클로드|챗\s*gpt|chatgpt|claude)(?:에서|로)?\s*(?:받은|만든)", "ai"),
]
SCAN_WORDS = r"스캔한|스캔된|스캔본|스캔"
# 동사 → (역할, 날짜 칸)
VERB_AUTHOR = r"만든|만드신|작성한|작성하신|작성된|써\s*둔|쓴"
VERB_EDITOR = r"고친|수정한|수정하신|수정된|업데이트한|최근에\s*본|손본"
VERB_RECV = r"받은|받았던|내려\s*받은|다운\s*받은|다운로드한|다운로드\s*받은|들어온"
# 사람 이름 뒤에 붙는 말
NAME_TAIL = r"(?:님|씨|이가|께서|이|가|의|과|와|랑|이랑)?"
# 본문 검색어에서 뗄 명령 · 군말
FILLER = r"(?:을|를)?\s*(?:찾아\s*줘|찾아\s*주세요|찾아|보여\s*줘|알려\s*줘|있어\??|있나요\??|어디\s*있어\??|뭐야\??|주세요|줘)"
GENERIC = {"문서", "파일", "자료", "것", "거", "중", "중에", "중에서", "그중에", "관련", "관련된"}


# ── 이름 목록 (색인에 사람으로 나온 이름 + 별칭) ─────────────

_names_cache = {}       # meta.sqlite 경로 → (mtime, names, aliases, me)


#------------------------------------------------------------------
# 질의에서 사람으로 볼 이름들 읽기 (meta.sqlite 가 바뀔 때만 다시)
#
# -in: db = meta.sqlite 경로 / root = 사전 폴더
#
# -out: (한글 이름 집합, {별칭: 대표}, me 대표 이름)
# -out: error = 없음 (파일이 없으면 빈 값)
#------------------------------------------------------------------
def _names(db, root):
    try:
        mt = os.path.getmtime(db)
    except OSError:
        return set(), {}, None
    pp = os.path.join(root, "people.yaml")
    key = (mt, os.path.getmtime(pp) if os.path.isfile(pp) else 0)
    hit = _names_cache.get(db)
    if hit and hit[0] == key:
        return hit[1:]
    conn = sqlite3.connect(db)
    try:
        names = {n for (n,) in conn.execute("SELECT DISTINCT name FROM doc_people") if re.fullmatch(r"[가-힣]{2,4}", n)}
    finally:
        conn.close()
    from .people import load_user
    aliases = N.load_aliases(root)
    me = load_user(root).get("me")
    names |= {v for v in aliases.values() if re.fullmatch(r"[가-힣]{2,4}", v)}
    _names_cache[db] = (key, names, aliases, me)
    return names, aliases, me


# ── 시간 (§12-1) ──────────────────────────────────────────

def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=KST).timestamp())


def _month_range(y, m):
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


#------------------------------------------------------------------
# 시간 표현 찾기
#=> 오늘을 기준으로 [시작, 끝) 범위(KST)를 만든다. 찾은 글자 범위도 돌려줘 본문 검색어에서 뗀다.
#    올해 · 작년 · 재작년 · 2023년 · 23년 · 이번 달 · 지난달 · 3월(쯤) · 상반기 · 하반기 · 지난주 ·
#    최근 · 요즘 · N년 전 · N개월 전. 연도 + 월("2023년 3월") · 연도 + 반기도 묶는다.
#
# -in: q = 질의 / today = 기준 날짜
#
# -out: ({from, to, label}, [(시작, 끝) 글자 범위]) / 없으면 (None, [])
# -out: error = 없음
#------------------------------------------------------------------
def parse_time(q, today):
    spans = []
    y = None
    label = []
    m = re.search(r"재작년|작년|지난\s*해|전년|올해|금년|((?:19|20)\d{2})\s*년(?:도)?|(?<![\d.])(\d{2})\s*년(?:도)?(?!\s*(?:전|간|차))", q)
    if m:
        t = m.group(0)
        if "재작년" in t:
            y = today.year - 2
        elif re.search(r"작년|지난\s*해|전년", t):
            y = today.year - 1
        elif re.search(r"올해|금년", t):
            y = today.year
        elif m.group(1):
            y = int(m.group(1))
        else:
            y = 2000 + int(m.group(2))
        spans.append(m.span())
        label.append("{}년".format(y))
    mm = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*월(\s*쯤|\s*경)?", q)
    half = re.search(r"상반기|하반기", q)
    rel = re.search(r"이번\s*달|지난\s*달|저번\s*달|지난\s*주|저번\s*주|이번\s*주|최근|요즘|(\d+)\s*년\s*전|(\d+)\s*개월\s*전", q)
    start = end = None
    if mm:
        mon = int(mm.group(1))
        yy = y if y is not None else (today.year if mon <= today.month else today.year - 1)
        s0, e0 = _month_range(yy, mon)
        if mm.group(2):
            # "3월쯤" — 앞뒤 한 달 더
            s0 = (s0.replace(day=1) - timedelta(days=1)).replace(day=1)
            e0 = _month_range(e0.year, e0.month)[1]
        start, end = s0, e0
        spans.append(mm.span())
        label = ["{}년 {}월{}".format(yy, mon, "쯤" if mm.group(2) else "")]
    elif half:
        yy = y if y is not None else today.year
        start, end = (date(yy, 1, 1), date(yy, 7, 1)) if half.group(0) == "상반기" else (date(yy, 7, 1), date(yy + 1, 1, 1))
        spans.append(half.span())
        label = ["{}년 {}".format(yy, half.group(0))]
    elif y is not None:
        start, end = date(y, 1, 1), date(y + 1, 1, 1)
    elif rel:
        t = rel.group(0)
        if re.match(r"이번\s*달", t):
            start, end = _month_range(today.year, today.month)
        elif re.match(r"(지난|저번)\s*달", t):
            p = today.replace(day=1) - timedelta(days=1)
            start, end = _month_range(p.year, p.month)
        elif re.match(r"(지난|저번)\s*주", t):
            start, end = today - timedelta(days=7), today + timedelta(days=1)
        elif re.match(r"이번\s*주", t):
            start, end = today - timedelta(days=today.weekday()), today + timedelta(days=1)
        elif t in ("최근", "요즘"):
            start, end = today - timedelta(days=90), today + timedelta(days=1)
        elif rel.group(1):
            yy = today.year - int(rel.group(1))
            start, end = date(yy, 1, 1), date(yy + 1, 1, 1)
        elif rel.group(2):
            p = today.replace(day=1)
            for _ in range(int(rel.group(2))):
                p = (p - timedelta(days=1)).replace(day=1)
            start, end = _month_range(p.year, p.month)
        spans.append(rel.span())
        label = [re.sub(r"\s+", " ", t)]
    if start is None:
        return None, []
    return {"from": _ts(start), "to": _ts(end), "label": " ".join(label),
            "from_date": start.isoformat(), "to_date": (end - timedelta(days=1)).isoformat()}, spans


#------------------------------------------------------------------
# 질의 해석 (§12) — 규칙만
#=> 1) 사람(색인의 이름 · 별칭 · "내가") 2) 시간 3) 동사(만든 · 고친 · 받은 → 역할 · 날짜 칸)
#   4) 형식 · 받은 경로 · 스캔 · 유형 · 양식 사전 5) 남은 말 = 본문 검색어
#
# -in: q = 질의 / names = 사람 이름 집합 / aliases = 별칭 사전 / me = "나" 대표 이름
# -in: rules = load_rules() / today = 기준 날짜(None 이면 오늘)
#
# -out: {people, date, date_field, format, recv, scanned, doc_type, is_form, query, chips, conditions}
#        conditions 가 False 면 조건 없는 보통 질의(지금 검색과 같게 돈다)
# -out: error = 없음
#------------------------------------------------------------------
def parse(q, names, aliases, me, rules, today=None):
    today = today or datetime.now(KST).date()
    text = q.strip()
    cut = []            # 본문 검색어에서 뗄 글자 범위
    out = {"people": [], "date": None, "date_field": "modified", "format": None, "recv": None,
           "scanned": False, "doc_type": None, "is_form": False}
    low = text.lower()

    # 1) 사람 — 긴 이름부터, 뒤에 붙은 조사까지
    for n in sorted(names, key=len, reverse=True):
        for m in re.finditer(re.escape(n) + NAME_TAIL, text):
            if any(a <= m.start() < b for a, b in cut):
                continue
            # 이름이 더 긴 한글 낱말의 일부면(고봉수지원팀) 사람으로 보지 않는다
            end_n = m.start() + len(n)
            if m.start() > 0 and re.match(r"[가-힣]", text[m.start() - 1]):
                continue
            if m.end() == end_n and end_n < len(text) and re.match(r"[가-힣]", text[end_n]):
                continue
            out["people"].append({"name": n, "raw": m.group(0)})
            cut.append(m.span())
    for al, canon in aliases.items():
        if re.fullmatch(r"[a-z0-9._\-]{3,}", al):
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(al) + r"(?![a-z0-9])" + NAME_TAIL, low):
                if canon not in [p["name"] for p in out["people"]]:
                    out["people"].append({"name": canon, "raw": text[m.start():m.end()]})
                cut.append(m.span())
    m = re.search(r"(?<![가-힣])(내가|제가|나의|저의|내|제)(?=\s|$|[가-힣])", text)
    if me and m and m.group(1) in ("내가", "제가", "나의", "저의") or (me and m and re.match(r"\s*(?:" + VERB_AUTHOR + "|" + VERB_EDITOR + ")", text[m.end():])):
        if me not in [p["name"] for p in out["people"]]:
            out["people"].append({"name": me, "raw": m.group(0), "me": True})
        cut.append(m.span())

    # 2) 시간
    tm, spans = parse_time(text, today)
    if tm:
        out["date"] = tm
        cut += spans

    # 3) 동사 — 역할과 날짜 칸
    role = None
    for pat, r, field in ((VERB_AUTHOR, "author", "created"), (VERB_EDITOR, "editor", "modified"), (VERB_RECV, None, "added")):
        mv = re.search(pat, text)
        if mv:
            role = role or r
            out["date_field"] = field if out["date_field"] == "modified" else out["date_field"]
            cut.append(mv.span())
            break
    for p in out["people"]:
        p["role"] = role

    # 4) 사전 — 받은 경로 · 형식 · 스캔 · 유형 · 양식
    for pat, kind in RECV_WORDS:
        mr = re.search(pat, low)
        if mr:
            out["recv"] = kind
            out["date_field"] = "added" if out["date"] else out["date_field"]
            cut.append(mr.span())
            break
    for pat, fmt in FORMAT_WORDS:
        mf = re.search(r"(?<![a-z가-힣])(?:" + pat + r")(?![a-z])", low)
        if mf:
            out["format"] = fmt
            cut.append(mf.span())
            break
    ms_ = re.search(SCAN_WORDS, text)
    if ms_:
        out["scanned"] = True
        cut.append(ms_.span())
    for w in rules["doc_types"]:
        if w in text:
            out["doc_type"] = "매뉴얼" if w == "메뉴얼" else w
            break
    if any(w in text for w in ("양식", "서식", "템플릿")):
        out["is_form"] = True

    # 5) 본문 검색어 — 뗄 범위를 지우고 명령 · 군말을 뺀다(유형어 · 양식어는 남는다)
    keep = []
    for i, ch in enumerate(text):
        keep.append(" " if any(a <= i < b for a, b in cut) else ch)
    rest = "".join(keep)
    rest = re.sub(FILLER + r"\s*[.?!]*\s*$", " ", rest)
    rest = re.sub(r"(?<![가-힣])(에|에서|중|중에|의|을|를|은|는|이|가|로|으로)(?=\s|$)", " ", rest)
    words = [w for w in re.split(r"\s+", rest) if w and w not in GENERIC and not re.fullmatch(r"[\W_]+", w)]
    out["query"] = " ".join(words)
    # 유형어 · 양식어만 있는 질의("휴가 규정에서 연차 이월")는 보통 본문 질의다 — 지금 검색과 같게 돌려야 한다
    #   (시험 ④ "본문 질의 하락 없음"). 다른 조건(사람 · 시간 · 형식 · 출처 · 스캔)과 함께일 때만 조건으로 쓴다
    out["conditions"] = bool(out["people"] or out["date"] or out["format"] or out["recv"] or out["scanned"])
    # 내용을 묻는 질문("2016년 한국의 점수는?" · "내가 결혼하면 휴가 며칠 줘?")의 연도 · 나 · 형식은 문서 속성이
    #   아니라 질문 내용이다(골든 본문 질문 362개 중 21개가 여기서 걸렸다). 질문 꼴이면 "만든 · 받은 … + 문서 낱말"
    #   처럼 문서를 가리키는 말이 붙어 있을 때만 조건으로 쓴다.
    if out["conditions"] and is_question(text) and not doc_context(text, rules):
        out["conditions"] = False
        out["skipped"] = "내용 질문"
    return out


QUESTION_END = (r"[?？]\s*$|(?:는가|은가|인가|나요|니까|까요|일까|을까|ㄹ까|습니까|입니까|었나|했나|되나|하나|있나|없나|"
                r"어때|뭐야|뭔가|되지|하지|돼|줘|해|야)\s*[.?？]*\s*$")
QUESTION_WORD = r"얼마|몇\s*(?:개|명|일|도|배|번|년|월|시간|퍼센트|%)?|어떻게|어떤|무엇|무슨|뭐|언제|누가|누구|왜|어디|어느"


#------------------------------------------------------------------
# 내용을 묻는 질문 꼴인가 — 물음표 · 의문 어미 · 의문사
#=> "찾아줘 · 보여줘" 로 끝나는 찾기 요청은 질문으로 보지 않는다.
#
# -in: text = 질의
#
# -out: True / False
# -out: error = 없음
#------------------------------------------------------------------
def is_question(text):
    t = text.strip()
    if re.search(r"(?:찾아|보여|알려)\s*(?:줘|주세요|줄래|봐)\s*[.?？!]*\s*$", t):
        return False
    return bool(re.search(QUESTION_END, t) or re.search(QUESTION_WORD, t))


#------------------------------------------------------------------
# 문서를 가리키는 말이 있는가 — "만든 보고서에서", "메일로 받은 한글 파일 중", "내가 작성한 문서"
#=> 문서 동작(만든 · 작성한 · 고친 · 받은 …) 바로 뒤 두 낱말 안에 문서 낱말(문서 · 파일 · 자료 · 유형어 · 형식어)이 있으면 참.
#
# -in: text = 질의 / rules
#
# -out: True / False
# -out: error = 없음
#------------------------------------------------------------------
def doc_context(text, rules):
    nouns = ["문서", "파일", "자료", "양식", "서식", "엑셀", "피피티", "ppt", "pdf", "한글", "워드", "hwp", "슬라이드"] + rules["doc_types"]
    noun_re = "|".join(re.escape(n) for n in nouns)
    verb_re = "|".join((VERB_AUTHOR, VERB_EDITOR, VERB_RECV))
    return bool(re.search(r"(?:" + verb_re + r")\s*(?:\S+\s+){0,2}?\S*(?:" + noun_re + r")", text, re.I))


# ── 필터 · 가산 · 완화 (§13) ──────────────────────────────

# 가산 크기 — 리랭커 점수 척도(대략 -10 ~ +10) 기준. §16 에서 조정
BOOST_ROLE = {"author": 1.5, "editor": 0.8, "template": 0.3}
BOOST_DATE = 1.0
BOOST_TYPE = 1.0
BOOST_FORM = 1.0
DATE_CONF_MIN = 0.7
TYPE_FILTER_MIN = 5.0


#------------------------------------------------------------------
# 필터 · 가산 계획 (진입점)
#=> 1) 질의 해석 2) 조건마다 문서 집합(필터) · 가산 점수 3) 교집합이 0 이면 완화
#      (사람 필터 → 가산, 기간 앞뒤 1년, 유형 · 양식 필터 → 가산 순)
#   필터: 폴더 · 형식 · 스캔 · 받은 경로 = 항상 / 기간 = 날짜를 믿을 수 있는 문서에만(믿을 수 없으면 탈락 안 함)
#         사람 = 색인 · 별칭에 확정된 이름이고 문서가 있을 때 / 유형 · 양식 = 점수 5 이상인 문서가 있을 때
#   가산: 사람(작성 > 수정 > 원본양식) · 기간 안 · 유형 · 양식 점수
#   "작성일"은 신뢰도 0.7 이상일 때만 쓰고, 아니면 수정일로 대신한다(결정 D3).
#
# -in: q = 질의 / db = meta.sqlite 경로(None 이면 기본) / folder = 폴더 한정 범위
# -in: today = 기준 날짜(시험용)
#
# -out: None(메타 없음 · 조건 없음) 또는
#       {parsed, query(본문 검색어), docs(필터 집합 또는 None=전체), boosts{경로: 점수},
#        chips[{kind, label, mode}], relaxed[글], meta_only, ms}
# -out: error = 없음 (meta.sqlite 를 못 읽으면 None)
#------------------------------------------------------------------
def plan(q, db=None, folder=None, today=None, root=None):
    t0 = time.perf_counter()
    db = db or S.default_path(root)
    if not os.path.isfile(db):
        return None
    root = root or os.path.dirname(os.path.abspath(db))
    names, aliases, me = _names(db, root)
    p = parse(q, names, aliases, me, load_rules(root), today)
    if not p["conditions"]:
        return None
    try:
        rows, people_rows = _rows(db)
        res = _plan(rows, people_rows, p, folder)
    except sqlite3.Error:
        return None
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res


_rows_cache = {}        # meta.sqlite 경로 → (mtime, rows, {이름: [(경로, 역할)]})


#------------------------------------------------------------------
# meta.sqlite 의 필터용 칸을 메모리에 (파일이 바뀔 때만 다시 읽는다)
#=> 질의마다 2천여 줄을 다시 읽으면 20ms 남짓 — 설계 목표(+10ms)를 넘는다. 한 번 읽어 두면 1~3ms.
#
# -in: db = meta.sqlite 경로
#
# -out: ({경로: 행}, {이름: [(경로, 역할)]})
# -out: error = sqlite 예외 전파
#------------------------------------------------------------------
def _rows(db):
    mt = os.path.getmtime(db)
    wal = db + "-wal"
    if os.path.isfile(wal):
        mt = max(mt, os.path.getmtime(wal))
    hit = _rows_cache.get(db)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    conn = sqlite3.connect(db)
    try:
        rows = {r[0]: r for r in conn.execute(
            "SELECT doc_path, format, scanned, recv_kind, modified_ts, created_ts, created_conf, added_ts, "
            "doc_type, doc_type_score, is_form, form_score FROM doc_meta")}
        people = {}
        for path, name, role in conn.execute("SELECT doc_path, name, role FROM doc_people"):
            people.setdefault(name, []).append((path, role))
    finally:
        conn.close()
    _rows_cache[db] = (mt, rows, people)
    return rows, people


# 조건별 문서 집합 · 가산 → 교집합 · 완화
def _plan(rows, people_rows, p, folder):
    if folder:
        f = os.path.normcase(os.path.abspath(folder)).rstrip("\\/") + os.sep
        rows = {k: v for k, v in rows.items() if os.path.normcase(k).startswith(f)}
    allp = set(rows)
    hard = {}           # 조건 이름 → 문서 집합(필터)
    soft = {}           # 조건 이름 → {경로: 가산}
    chips = []
    if p["format"]:
        hard["format"] = {k for k, r in rows.items() if r[1] == p["format"]}
        chips.append({"kind": "format", "label": {"excel": "엑셀", "ppt": "PPT", "hwp": "한글", "pdf": "PDF", "word": "워드"}[p["format"]], "mode": "filter"})
    if p["scanned"]:
        hard["scanned"] = {k for k, r in rows.items() if r[2] == 1}
        chips.append({"kind": "scanned", "label": "스캔본", "mode": "filter"})
    if p["recv"]:
        hard["recv"] = {k for k, r in rows.items() if r[3] == p["recv"]}
        chips.append({"kind": "recv", "label": {"mail": "메일로 받은", "drive": "드라이브에서 받은", "ai": "AI 도구에서 받은"}[p["recv"]], "mode": "filter"})
    if p["date"]:
        hard["date"], soft["date"] = _date_sets(rows, p["date"], p["date_field"])
        chips.append({"kind": "date", "label": "{} · {}".format(p["date"]["label"], {"created": "작성일", "modified": "수정일", "added": "받은 날"}[p["date_field"]]), "mode": "filter"})
    if p["people"]:
        hard["people"], soft["people"] = _people_sets(people_rows, p["people"], allp)
        for pp in p["people"]:
            role = {"author": "작성", "editor": "수정"}.get(pp.get("role"), "관련")
            chips.append({"kind": "person", "label": "{} ({})".format(pp["name"] + (" · 나" if pp.get("me") else ""), role),
                          "mode": "filter" if hard["people"] else "boost"})
        if not hard["people"]:
            del hard["people"]
    if p["doc_type"]:
        strong = {k for k, r in rows.items() if r[8] == p["doc_type"] and (r[9] or 0) >= TYPE_FILTER_MIN}
        soft["type"] = {k: BOOST_TYPE * min(1.0, (r[9] or 0) / TYPE_FILTER_MIN) for k, r in rows.items()
                        if r[8] == p["doc_type"] and (r[9] or 0) >= 3}
        if strong:
            hard["type"] = {k for k, r in rows.items() if r[8] == p["doc_type"] and (r[9] or 0) >= 3}
        chips.append({"kind": "type", "label": p["doc_type"], "mode": "filter" if strong else "boost"})
    if p["is_form"]:
        strong = {k for k, r in rows.items() if r[10] == 1 and (r[11] or 0) >= TYPE_FILTER_MIN}
        soft["form"] = {k: BOOST_FORM * min(1.0, (r[11] or 0) / TYPE_FILTER_MIN) for k, r in rows.items() if r[10] == 1}
        if strong:
            hard["form"] = {k for k, r in rows.items() if r[10] == 1}
        chips.append({"kind": "form", "label": "양식", "mode": "filter" if strong else "boost"})

    # 교집합 — 0 이면 하나씩 푼다(§13-3)
    relaxed = []
    docs = _inter(allp, hard)
    if hard and not docs and "people" in hard:
        del hard["people"]
        relaxed.append("작성자 조건 해제")
        _mode(chips, "person", "boost")
        docs = _inter(allp, hard)
    if hard and not docs and "date" in hard:
        wide = dict(p["date"], **{"from": p["date"]["from"] - 365 * 86400, "to": p["date"]["to"] + 365 * 86400})
        hard["date"], soft["date"] = _date_sets(rows, wide, p["date_field"])
        relaxed.append("기간을 앞뒤 1년 넓힘")
        docs = _inter(allp, hard)
    for k in ("type", "form"):
        if hard and not docs and k in hard:
            del hard[k]
            relaxed.append({"type": "유형 조건 해제", "form": "양식 조건 해제"}[k])
            _mode(chips, k, "boost")
            docs = _inter(allp, hard)
    boosts = {}
    for part in soft.values():
        for k, v in part.items():
            boosts[k] = boosts.get(k, 0.0) + v
    return {"parsed": p, "query": p["query"], "docs": docs if hard else None, "boosts": boosts, "chips": chips,
            "relaxed": relaxed, "meta_only": not p["query"].strip()}


def _inter(allp, hard):
    out = set(allp)
    for s in hard.values():
        out &= s
    return out


def _mode(chips, kind, mode):
    for c in chips:
        if c["kind"] == kind:
            c["mode"] = mode


#------------------------------------------------------------------
# 기간 조건 — (필터 집합, 가산)
#=> 작성일은 신뢰도 0.7 이상인 문서만 작성일로, 나머지는 수정일로 본다(결정 D3).
#   날짜를 믿을 수 없는 문서(상속 · 없음)는 필터로 떨어뜨리지 않고 가산도 주지 않는다 —
#   단, 수정일은 항상 있으므로 작성일이 없으면 수정일로 대신하는 데서 끝난다.
#------------------------------------------------------------------
def _date_sets(rows, d, field):
    lo, hi = d["from"], d["to"]
    keep, boost = set(), {}
    for k, r in rows.items():
        mod, cre, conf, add = r[4], r[5], r[6], r[7]
        if field == "created":
            ts = cre if (cre and (conf or 0) >= DATE_CONF_MIN) else mod
        elif field == "added":
            ts = add
        else:
            ts = mod
        if ts is None:
            keep.add(k)            # 날짜를 모르는 문서는 떨어뜨리지 않는다
            continue
        if lo <= ts < hi:
            keep.add(k)
            boost[k] = BOOST_DATE
    return keep, boost


#------------------------------------------------------------------
# 사람 조건 — (필터 집합, 가산)
#=> 그 이름이 관련인(작성 · 수정 · 원본양식)인 문서. 질의 동사가 "만든"이어도 작성 칸은 틀린 값이
#   많으므로(§3) 역할로 떨어뜨리지 않고 역할별로 가산만 다르게 준다.
#   여러 사람이면 모두가 관련된 문서(교집합).
#------------------------------------------------------------------
def _people_sets(people_rows, people, allp):
    keep = None
    boost = {}
    for pp in people:
        got = {}
        for path, role in people_rows.get(pp["name"], []):
            b = BOOST_ROLE.get(role, 0.3)
            # 질의가 "만든" 이면 작성 역할을, "고친" 이면 수정 역할을 조금 더 올린다
            if pp.get("role") and pp["role"] == role:
                b += 0.5
            got[path] = max(got.get(path, 0.0), b)
        s = set(got) & allp
        keep = s if keep is None else keep & s
        for k, v in got.items():
            boost[k] = boost.get(k, 0.0) + v
    return (keep or set()), boost
