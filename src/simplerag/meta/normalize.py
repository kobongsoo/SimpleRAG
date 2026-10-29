#------------------------------------------------------------------
# 메타데이터 정규화 (메타데이터 검색 설계서 §7 사람 · §8 날짜 · §9 제목 · §10 형식 · 출처 · 유형)
#=> extract.py 의 원본 값(RawMeta)을 검색에 쓸 값(DocMeta)으로 바꾼다.
#   속성 값은 지저분하다(실측: 작성자 16% 쓰레기 · 19% 템플릿 상속 · 쓸 만한 제목 29%).
#   이 파일의 규칙이 그것을 바로잡는다. 파일을 다시 읽지 않으므로 규칙을 바꾸면
#   meta rebuild 한 번으로 모두 다시 계산된다.
#------------------------------------------------------------------

import ipaddress
import os
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
MIN_TS = datetime(1980, 1, 1, tzinfo=timezone.utc).timestamp()
HANGUL = re.compile(r"[가-힣]")


# ── 사람 (§7) ─────────────────────────────────────────────

#------------------------------------------------------------------
# 사람 값 하나 정규화
#=> 원본 작성자 칸 값을 "대표 이름"으로 바꾼다. 사람이 아니면 None.
#    1) 정리 — NUL · 앞뒤 공백, 괄호 안 소속, 직함, 이메일 도메인(앞부분은 계정 ID)
#    2) 쓰레기 — 목록 일치 · 한 글자 영문 · 숫자만 · 깨진 문자열(라틴 확장 30% 넘음)
#    3) 한글 이름 — "봉수 고" → 고봉수, "고 봉수" → 고봉수, 그 밖은 공백만 뺀다
#    4) 영문 — 소문자 · 공백 하나로. 공백 없는 영숫자는 계정 ID
#    5) 별칭 사전 — bong9 → 고봉수
#
# -in: value   = 원본 값
# -in: rules   = load_rules() 결과
# -in: aliases = {별칭 소문자: 대표 이름} (people.yaml)
#
# -out: (이름, 종류) — 종류 = korean|english|account|alias. 사람이 아니면 None
# -out: error = 없음
#------------------------------------------------------------------
def norm_person(value, rules, aliases=None):
    if not isinstance(value, str):
        return None
    s = value.split("\x00", 1)[0].strip()
    # 여러 사람을 ; 로 이어 쓴 칸 — 사람으로 읽히는 첫 값을 쓴다
    if ";" in s:
        for part in s.split(";"):
            got = norm_person(part, rules, aliases)
            if got:
                return got
        return None
    s = _unmangle(s)
    if not s:
        return None
    # 괄호 안 소속 · 메모(고봉수(품질팀)) 를 뗀다
    s = re.sub(r"\s*[\(\[（][^)\]）]*[\)\]）]\s*", " ", s).strip()
    # 이메일은 앞부분만(계정 ID 로 본다)
    if "@" in s:
        s = s.split("@", 1)[0].strip()
    if not s:
        return None
    low = s.lower()
    if low in rules["junk_people"] or any(re.search(p, low) for p in rules["junk_people_patterns"]):
        return None
    # 깨진 문자열 — 라틴 확장(À~ɏ) 비율이 30% 넘으면 복구할 수 없는 모지바케다
    ext = sum(1 for c in s if "\u00c0" <= c <= "\u024f")
    if ext and ext / len(s) > 0.3:
        return None
    if re.fullmatch(r"[\d\s\-_.]+", s) or re.fullmatch(r"[A-Za-z]", s):
        return None
    if HANGUL.search(s):
        # 부서 낱말("정보화전략팀")을 뗀다 — 이름 뒤에 소속을 붙여 쓴 칸
        words = [w for w in s.split() if not any(w.endswith(o) and len(w) > len(o) + 1 for o in rules["org_suffix"])]
        s = " ".join(words) if words else s
        # 직함을 뗀다(앞뒤 어디든, 공백으로 떨어져 있거나 붙어 있거나)
        for t in sorted(rules["titles"], key=len, reverse=True):
            s = re.sub(r"(?<=[가-힣])\s*" + re.escape(t) + r"$", "", s)
            s = re.sub(r"^" + re.escape(t) + r"\s+", "", s)
        s = s.strip()
        m = re.fullmatch(r"([가-힣]{1,3})\s+([가-힣])", s)
        if m:
            name = m.group(2) + m.group(1)         # "봉수 고" → 고봉수
        else:
            name = re.sub(r"\s+", "", s)            # "고 봉수" · "고봉수" → 고봉수
        if not re.fullmatch(r"[가-힣]{2,5}", name):
            # 한글이 섞였지만 이름 모양이 아니다(회사명 · 문장) — 사람으로 보지 않는다
            return None
        if name in rules["junk_people"]:
            return None
        return _alias(name, "korean", aliases)
    # 영문 · 계정 ID
    if re.fullmatch(r"[A-Za-z0-9._\-]+", s):
        return _alias(low, "account", aliases)
    # "Han, Hyun Bae" 처럼 성 뒤에 쉼표 — 쉼표를 공백으로(낱말 순서는 별칭에서 무시한다)
    if re.fullmatch(r"[A-Za-z][A-Za-z .,'\-]+", s) and s.count(",") <= 1:
        name = re.sub(r"\s+", " ", low.replace(",", " ")).strip()
        if name in rules["junk_people"]:
            return None
        return _alias(name, "english", aliases)
    return None


#------------------------------------------------------------------
# 깨진 글자 되살리기
#=> 실측에서 본 두 가지를 되돌린다(되돌린 결과에 한글이 있을 때만 받아들인다).
#    1) <C0AFBFACC1A4> — CP949 바이트를 hex 로 적은 값(PDF)
#    2) °ø·æ · ê³€ë´›… — CP949 / UTF-8 바이트를 Latin-1 · CP1252 로 잘못 읽은 값
#   못 되돌리면 원래 글자를 그대로 돌려준다(뒤 단계가 깨진 값으로 버린다).
#
# -in: s = 원본 글자
#
# -out: 되살린 글자(또는 원래 글자)
# -out: error = 없음
#------------------------------------------------------------------
def _unmangle(s):
    m = re.fullmatch(r"<([0-9A-Fa-f]{4,})>", s)
    if m:
        try:
            t = bytes.fromhex(m.group(1)).decode("cp949").strip()
            return t if HANGUL.search(t) else s
        except (ValueError, UnicodeDecodeError):
            return s
    if not HANGUL.search(s) and any("\u0080" <= c <= "ÿ" or c in "€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ" for c in s):
        for enc_from, enc_to in (("latin-1", "cp949"), ("cp1252", "utf-8"), ("cp1252", "cp949")):
            try:
                t = s.encode(enc_from).decode(enc_to).strip()
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            # 흔한 한글 2,350자(EUC-KR) 밖의 음절이 나오면 잘못 되살린 것이다(실측: ê³€ë´›… → 곀봛싟)
            if HANGUL.search(t) and not any("À" <= c <= "ɏ" for c in t) and _common_hangul(t):
                return t
    return s


# 한글 음절이 모두 흔한 2,350자(EUC-KR)에 드는가
def _common_hangul(t):
    # 파이썬 euc-kr 은 드문 음절을 8바이트 조합형으로도 적어 주므로, 2바이트(완성형)인지로 본다
    try:
        return all(len(c.encode("euc-kr")) == 2 for c in t if HANGUL.match(c))
    except UnicodeEncodeError:
        return False


# 별칭 사전 적용 — 영문 이름은 낱말 순서가 달라도 같은 사람(youngho yoon = yoon youngho)
def _alias(name, kind, aliases):
    if aliases:
        key = name.lower()
        if key in aliases:
            return aliases[key], "alias"
        if kind == "english":
            k2 = " ".join(sorted(key.split()))
            if k2 in aliases:
                return aliases[k2], "alias"
    return name, kind


#------------------------------------------------------------------
# 관련인 목록 (§4 #9 · §7-1 ⑥)
#=> 만든 이 = 작성, 마지막 저장자 = 수정. 템플릿 상속 문서의 만든 이는 원본양식(신뢰도 0.3).
#   같은 사람이 두 칸에 있으면 높은 역할 하나만 남긴다.
#
# -in: doc = raw["doc"] / kind = raw["kind"] / rules · aliases / inherited = 템플릿 상속 문서인가
#
# -out: [{name, role, raw, source, conf}]
# -out: error = 없음
#------------------------------------------------------------------
def people_of(doc, kind, rules, aliases, inherited):
    out = []
    for field, role in (("author", "author"), ("last_saved_by", "editor")):
        raw = doc.get(field)
        got = norm_person(raw, rules, aliases)
        if not got:
            continue
        name, how = got
        conf = 0.9 if how in ("korean", "alias", "english") else 0.6   # 별칭이 없는 계정 ID 는 덜 믿는다
        r = role
        if role == "author" and inherited:
            r, conf = "template", 0.3
        out.append({"name": name, "role": r, "raw": raw, "source": "{}.{}".format(kind, field), "conf": conf})
    # 같은 이름이면 높은 역할 하나만(작성 > 수정 > 원본양식)
    rank = {"author": 3, "editor": 2, "template": 1}
    best = {}
    for p in out:
        cur = best.get(p["name"])
        if cur is None or rank[p["role"]] > rank[cur["role"]]:
            best[p["name"]] = p
    return list(best.values())


# ── 날짜 (§8) ─────────────────────────────────────────────

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


#------------------------------------------------------------------
# 날짜 문자열 → UTC epoch 초
#=> 실측에서 본 형식을 모두 받는다.
#    W3CDTF / ISO(OOXML · HWPX · OLE · 파일 시스템) — 시간대 없으면 UTC(OLE FILETIME 은 UTC)
#    PDF D:20100823131638+09'00' — 오프셋 적용, 없으면 KST
#    PDF 자유 형식 "Mon Jan 12 08:51:48 2004" · "March 15, 2024 14:09"
#    HWP "2005년 10월 7일 금요일 오전 10:28:32" — KST
#   1980년 이전 · 내일 이후는 버린다(1900-12-31 같은 이상값).
#
# -in: s = 날짜 문자열 / now = 기준 시각(epoch, 미래 판정용)
#
# -out: epoch 초(int) / 못 읽거나 이상하면 None
# -out: error = 없음
#------------------------------------------------------------------
def parse_date(s, now=None):
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    dt = None
    m = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?\s*([Zz+\-])?\s*(\d{2})?'?(\d{2})?", s)
    if m:
        y, mo, d, h, mi, se = (int(x) if x else dflt for x, dflt in zip(m.groups()[:6], (0, 1, 1, 0, 0, 0)))
        sign, oh, om = m.group(7), m.group(8), m.group(9)
        if sign in ("+", "-") and oh:
            off = timedelta(hours=int(oh), minutes=int(om or 0))
            tz = timezone(off if sign == "+" else -off)
        elif sign in ("Z", "z"):
            tz = timezone.utc
        else:
            tz = KST              # 오프셋이 없는 PDF 날짜는 한국 시간으로 본다
        try:
            dt = datetime(y, mo, d, h, mi, se, tzinfo=tz)
        except ValueError:
            return None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    if dt is None:
        m = re.match(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일(?:\s*\S+요일)?(?:\s*(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
        if m:
            h = int(m.group(5) or 0)
            if m.group(4) == "오후" and h < 12:
                h += 12
            if m.group(4) == "오전" and h == 12:
                h = 0
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), h, int(m.group(6) or 0),
                              int(m.group(7) or 0), tzinfo=KST)
            except ValueError:
                return None
    if dt is None:
        dt = _free_date(s)
    if dt is None:
        return None
    ts = dt.timestamp()
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    if ts < MIN_TS or ts > now + 86400:
        return None
    return int(ts)


# 영문 자유 형식 — "Mon Jan 12 08:51:48 2004" · "March 15, 2024 14:09" (KST 로 본다)
def _free_date(s):
    low = s.lower()
    mon = None
    for word in re.findall(r"[a-z]+", low):
        if word[:3] in MONTHS and len(word) >= 3:
            mon = MONTHS[word[:3]]
            break
    year = re.search(r"\b(19|20)\d{2}\b", low)
    if not mon or not year:
        return None
    rest = low.replace(year.group(0), " ")
    day = re.search(r"\b(\d{1,2})\b(?!:)", re.sub(r"\d{1,2}:\d{2}(:\d{2})?", " ", rest))
    tm = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?", rest)
    try:
        return datetime(int(year.group(0)), mon, int(day.group(1)) if day else 1,
                        int(tm.group(1)) if tm else 0, int(tm.group(2)) if tm else 0,
                        int(tm.group(3) or 0) if tm else 0, tzinfo=KST)
    except ValueError:
        return None


#------------------------------------------------------------------
# 대표 날짜 세 가지 (§8-3)
#=> 수정일(대표) = 문서 내부 수정일 → 없으면 파일 시스템 수정일
#   작성일 = 문서 내부 생성일(상속이면 신뢰도 0.3). 수정일보다 1시간 넘게 늦으면 버린다
#   PC 에 들어온 날 = 파일 시스템 생성일(복사 · 내려받은 시점)
#   HWP 는 생성일이 없을 때 날짜 문자열(PIDSI 20)을 보조로 쓴다.
#
# -in: raw = RawMeta / inherited = 템플릿 상속 문서인가
#
# -out: {modified_ts, created_ts, created_conf, added_ts}
# -out: error = 없음
#------------------------------------------------------------------
def dates_of(raw, inherited):
    doc, fs = raw.get("doc") or {}, raw.get("fs") or {}
    mod = parse_date(doc.get("modified")) or parse_date(fs.get("modified"))
    cre = parse_date(doc.get("created")) or parse_date(doc.get("date_str"))
    if cre and mod and cre > mod + 3600:
        cre = None                     # 생성일이 수정일보다 늦다 — 생성일 쪽이 틀렸다
    conf = None
    if cre:
        conf = 0.3 if inherited else 0.9
    return {"modified_ts": mod, "created_ts": cre, "created_conf": conf,
            "added_ts": parse_date(fs.get("created"))}


# ── 제목 (§9) ─────────────────────────────────────────────

#------------------------------------------------------------------
# 파일명 정리본 (§9-1 ③) — 앞 번호 · 날짜 · 버전 · 확장자를 떼고 _ 를 공백으로
#=> "25.출장여비규정_16.04.01.pdf" → "출장여비규정", "07_ISMS-GU-005-자산관리지침.pdf" → "ISMS-GU-005-자산관리지침"
#
# -in: name     = 파일 이름
# -in: versions = True 면 버전(v2.0 · 최종)까지 뗀다 — 버전 그룹 키용. 제목 후보는 False
#                 (제품 버전 "M드라이브 V9" 는 제목의 일부라서)
#
# -out: 정리한 이름(다 지워지면 확장자만 뗀 이름)
# -out: error = 없음
#------------------------------------------------------------------
def clean_filename(name, versions=True):
    base = os.path.splitext(name)[0]
    # 확장자가 겹친 이름(a.pdf.pdf)
    while os.path.splitext(base)[1].lower() in (".pdf", ".doc", ".docx", ".hwp", ".xls", ".xlsx", ".ppt", ".pptx"):
        base = os.path.splitext(base)[0]
    s = base
    # 사본 표시 · 괄호 번호: (1) [1] - 복사본
    s = re.sub(r"\s*[\(\[]\d+[\)\]]\s*$", "", s)
    s = re.sub(r"\s*-\s*(복사본|사본|copy)(\s*\(\d+\))?$", "", s, flags=re.I)
    # 날짜: _20190824 · _2019.03.29 · _24.04.01 · -2019.03.29 · (2019.03.29) · _230406
    s = re.sub(r"[\s_\-]*[\(\[]?(19|20)\d{2}[.\-_]?\d{2}[.\-_]?\d{2}[\)\]]?", " ", s)
    s = re.sub(r"[\s_\-]+\d{2}\.\d{2}\.\d{2}(?=$|[\s_\-])", " ", s)
    s = re.sub(r"[\s_\-]+\d{6,7}(?=$|[\s_\-])", " ", s)
    if versions:
        # 버전: v2.0 · _v2.3.1 · ver1 · _최종 · 최종본 · _수정
        # _ 도 낱말 글자라 \b 로는 "_v1.1" 의 v 앞을 못 잡는다 — 영문자가 아닌 것 뒤로 본다
        s = re.sub(r"[\s_\-]*(?<![A-Za-z])(v|ver\.?)\s?\d+(\.\d+)*(?![\w.])", " ", s, flags=re.I)
        s = re.sub(r"[\s_\-]*(최종본|최종|수정본|수정|final)$", "", s, flags=re.I)
    # 앞 번호: 07_ · 25. · 4-5_ · 1_1_ · 01.
    s = re.sub(r"^(\d+[._\-])+\s*", "", s)
    s = re.sub(r"[_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    return s or base


#------------------------------------------------------------------
# 속성 제목 정리 (§9-1 ①) — 쓰레기면 None
#=> "Microsoft Word - 경조사지원규정.doc" → 괄호 속 파일명만 남기고 다시 판정.
#   <B8AEB4…> 는 CP949 hex 로 풀어 본다(풀어서 파일명이면 버림). 상속 문서의 제목도 버린다.
#
# -in: title = 원본 제목 / rules / inherited
#
# -out: 정리한 제목 / 못 쓰면 None
# -out: error = 없음
#------------------------------------------------------------------
def clean_prop_title(title, rules, inherited):
    if not isinstance(title, str) or inherited:
        return None
    t = title.strip()
    m = re.fullmatch(r"<([0-9A-Fa-f]{4,})>", t)
    if m:
        try:
            t = bytes.fromhex(m.group(1)).decode("cp949").strip()
        except (ValueError, UnicodeDecodeError):
            return None
    m = re.match(r"^microsoft (word|excel|powerpoint)\s*-\s*(.+)$", t, re.I)
    if m:
        t = m.group(2).strip()
    if not t or any(ch < " " for ch in t):
        return None
    # 깨진 글자(라틴 확장이 많음) · 파일 같은 이름(GabrickWeiss.book) · 너무 짧은 일반어(과정명)
    ext = sum(1 for c in t if "À" <= c <= "ɏ" or c in "˘´")
    if ext and ext / len(t) > 0.15:
        return None
    if re.search(r"\.[A-Za-z]{2,5}$", t):
        return None
    t = _join_spaced(t)
    for pat in rules["title_junk"]:
        if re.search(pat, t, re.I):
            return None
    if len(t) < 4:
        return None
    return t


# "취  업  규  칙" 처럼 한 글자씩 띄운 제목을 붙인다
def _join_spaced(t):
    parts = t.split()
    if len(parts) >= 3 and all(len(x) == 1 and HANGUL.match(x) for x in parts):
        return "".join(parts)
    return t


#------------------------------------------------------------------
# 본문 첫 제목 (§9-1 ② · §9-2) — 1단계는 원본 값에 있는 것만(PPT 1번 슬라이드 제목)
#   Word · HWP · PDF · Excel 의 본문 첫 제목은 7단계(추출기 구조 사용)에서 더한다.
#------------------------------------------------------------------
def body_title(raw, rules):
    titles = (raw.get("doc") or {}).get("slide_titles") or []
    for t in titles[:1]:
        t = (t or "").strip()
        if 4 <= len(t) <= 60 and t.lower() not in ("목차", "contents", "agenda") \
                and not any(re.search(p, t, re.I) for p in rules["title_junk"]):
            return t
    return None


# 비교용 — 공백 · 기호를 뺀 소문자
def _key(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


# 문자 3-gram 자카드
def _jaccard3(a, b):
    ga = {a[i:i + 3] for i in range(max(0, len(a) - 2))}
    gb = {b[i:i + 3] for i in range(max(0, len(b) - 2))}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


#------------------------------------------------------------------
# 대표 제목 고르기 (§9-3)
#=> 세 후보(① 속성 ② 본문 ③ 파일명 정리본) 중 둘이 비슷하면(포함 관계 · 3-gram 자카드 ≥ 0.6)
#   더 긴 쪽, 아니면 ② → ① → ③ 순서. 모두 없으면 원래 파일명.
#
# -in: prop · body · fname = 후보 / name = 원래 파일 이름
#
# -out: (대표 제목, 신뢰도 high|mid)
# -out: error = 없음
#------------------------------------------------------------------
def pick_title(prop, body, fname, name):
    cands = [c for c in (body, prop, fname) if c]
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = _key(cands[i]), _key(cands[j])
            if a and b and (a in b or b in a or _jaccard3(a, b) >= 0.6):
                return (cands[i] if len(cands[i]) >= len(cands[j]) else cands[j]), "high"
    if cands:
        return cands[0], "mid"
    return os.path.splitext(name)[0], "mid"


# ── 형식 · 출처 · 유형 (§10) ───────────────────────────────

#------------------------------------------------------------------
# 원본 프로그램 (§10-2) — Creator · Producer · Application 에 든 말로
#
# -in: doc = raw["doc"] / fmt = 형식 / rules
#
# -out: hwp|word|excel|ppt|scanner|print|web|other / 단서가 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def origin_app(doc, fmt, rules):
    text = " ".join(str(doc.get(k) or "") for k in ("creator", "producer", "app")).lower()
    if not text.strip():
        # PDF 가 아니면 형식이 곧 원본 프로그램
        return fmt if fmt in ("word", "excel", "ppt", "hwp") else None
    for value, words in rules["origin_apps"]:
        if any(w in text for w in words):
            return value
    return "other"


#------------------------------------------------------------------
# 받은 경로 (§10-3) — Zone.Identifier 의 호스트 → mail|drive|intranet|ai|web
#
# -in: host = 도메인(없으면 None) / rules
#
# -out: 종류 / 받은 정보가 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def recv_kind(host, rules):
    if not host:
        return None
    try:
        if ipaddress.ip_address(host).is_private:
            return "intranet"
    except ValueError:
        pass
    for kind, doms in rules["recv_domains"].items():
        if any(host == d or host.endswith("." + d) for d in doms):
            return kind
    if any(host.startswith(p) for p in rules["intranet_prefix"]):
        return "intranet"
    return "web"


#------------------------------------------------------------------
# 템플릿 이름(§4 #17) — 경로 · 확장자를 떼고, Normal 계열은 None
#------------------------------------------------------------------
def template_of(doc, rules):
    t = doc.get("template")
    if not isinstance(t, str) or not t.strip():
        return None
    t = re.split(r"[\\/]", t.strip())[-1]
    if t.lower() in rules["plain_templates"] or os.path.splitext(t)[0].lower() in rules["plain_templates"]:
        return None
    return t


#------------------------------------------------------------------
# 문서 유형 · 양식 점수 (§10-4) — 규칙 점수(LLM 없음)
#=> 유형: 제목 후보 · 파일명 +3, 폴더명 +2, 템플릿명 +2 → 가장 높고 3점 이상인 유형
#   양식: 양식어(제목 · 파일명 +3, 폴더 +2, 템플릿 +2) · 양식 시트명 +1 · 템플릿 확장자 +5 → 3점 이상
#   본문 빈칸 패턴(+2)은 본문을 보는 7단계에서 더한다.
#
# -in: texts = 제목 후보 · 파일명 글 / folders = 폴더 이름 목록 / template · sheets · ext / rules
#
# -out: (유형, 유형 점수, 양식인가, 양식 점수)
# -out: error = 없음
#------------------------------------------------------------------
def type_scores(texts, folders, template, sheets, ext, rules):
    title_txt = " ".join(t for t in texts if t).lower()
    folder_txt = " ".join(folders).lower()
    tmpl = (template or "").lower()
    scores = {}
    for w in rules["doc_types"]:
        s = (3 if w in title_txt else 0) + (2 if w in folder_txt else 0) + (2 if w in tmpl else 0)
        if s:
            # 메뉴얼 · 매뉴얼은 같은 유형
            key = "매뉴얼" if w == "메뉴얼" else w
            scores[key] = max(scores.get(key, 0), s)
    dtype, dscore = (None, 0)
    if scores:
        dtype, dscore = max(scores.items(), key=lambda kv: kv[1])
    form = 0
    if any(w in title_txt for w in rules["form_words"]):
        form += 3
    if any(w in folder_txt for w in rules["form_folder_words"]):
        form += 2
    if tmpl and any(w in tmpl for w in rules["form_words"] + rules["doc_types"]):
        form += 2
    if any(any(fs in (s or "") for fs in rules["form_sheets"]) for s in (sheets or [])):
        form += 1
    if ext in rules["template_exts"]:
        form += 5
    return (dtype if dscore >= 3 else None), float(dscore), form >= 3, float(form)


#------------------------------------------------------------------
# RawMeta → DocMeta (정규화 진입점)
#=> meta.sqlite 의 doc_meta 한 줄 + doc_people 여러 줄을 만든다.
#
# -in: path      = 문서 경로(index_state.json 키와 같은 절대경로)
# -in: raw       = extract_raw() 결과
# -in: rules     = load_rules() / aliases = 별칭 사전
# -in: inherited = 템플릿 상속 문서인가(store 가 전체를 보고 정해 준다)
# -in: base      = 인덱싱 폴더(폴더 토큰을 그 아래 경로로만 잡는다, 없으면 부모 폴더 하나)
#
# -out: (meta dict, people list)
# -out: error = 없음
#------------------------------------------------------------------
def normalize(path, raw, rules, aliases=None, inherited=False, base=None):
    doc, fs = raw.get("doc") or {}, raw.get("fs") or {}
    name = raw.get("name") or os.path.basename(path)
    fmt = raw.get("format") or "other"
    # 폴더 이름들 — 인덱싱 폴더 아래 경로만(위쪽 D:\Project 같은 이름은 분류가 아니다)
    parent = os.path.dirname(path)
    if base and os.path.normcase(parent).startswith(os.path.normcase(os.path.abspath(base))):
        rel = os.path.relpath(parent, base)
        folders = [] if rel == "." else rel.split(os.sep)
    else:
        folders = [os.path.basename(parent)] if parent else []
    prop = clean_prop_title(doc.get("title"), rules, inherited)
    body = body_title(raw, rules)
    fname = clean_filename(name, versions=False)
    display, _ = pick_title(prop, body, fname, name)
    tmpl = template_of(doc, rules)
    sheets = doc.get("sheets") or []
    dtype, dscore, is_form, fscore = type_scores([prop, body, fname], folders, tmpl, sheets, raw.get("ext"), rules)
    origin = origin_app(doc, fmt, rules)
    pages = doc.get("pages") or doc.get("slides") or (len(sheets) if sheets else None)
    try:
        pages = int(pages) if pages is not None else None
    except (TypeError, ValueError):
        pages = None
    scanned = None
    if fmt == "pdf":
        # 앞 3쪽 글자가 거의 없거나 스캐너가 만든 PDF 면 스캔본
        tc = doc.get("text_chars")
        scanned = int((tc is not None and pages and tc < 50) or origin == "scanner")
    meta = {
        "doc_path": path,
        "content_hash": fs.get("sha1"),
        "version_key": _key(clean_filename(name)) or None,
        "format": fmt,
        "size": fs.get("size"),
        "pages": pages,
        **dates_of(raw, inherited),
        "inherited": int(bool(inherited)),
        "title_display": display,
        "title_text": " | ".join(t for t in (prop, body, fname) if t),
        "folder_text": " / ".join(folders),
        "origin_app": origin,
        "scanned": scanned,
        "recv_kind": recv_kind(fs.get("host"), rules),
        "recv_host": fs.get("host"),
        "template": tmpl,
        "doc_type": dtype, "doc_type_score": dscore,
        "is_form": int(is_form), "form_score": fscore,
        "status": raw.get("status") or "ok",
    }
    return meta, people_of(doc, raw.get("kind"), rules, aliases, inherited)


#------------------------------------------------------------------
# 별칭 사전 읽기 (§7-2) — people.default.yaml(배포) + people.yaml(사용자)
#=> 형식: people: {대표이름: [별칭, …]} (people.py 가 쓰는 모양). 결과는 {별칭 소문자: 대표 이름}.
#   대표 이름 자신도 넣는다(영문 이름 순서 무시용 키도 함께).
#
# -in: root = 데이터 폴더
#
# -out: {별칭: 대표 이름}
# -out: error = 없음 (파일이 없거나 깨졌으면 빈 사전)
#------------------------------------------------------------------
def load_aliases(root):
    out = {}
    for fn in ("people.default.yaml", "people.yaml"):
        p = os.path.join(root, fn) if root else None
        if not p or not os.path.isfile(p):
            continue
        try:
            import yaml
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001 — 사전이 깨져도 정규화는 돈다
            continue
        # people.yaml 모양: people: {대표: [별칭]} (rejected · me 는 별칭이 아니다). 예전 평평한 모양도 받는다
        if isinstance(data.get("people"), dict):
            data = data["people"]
        else:
            data = {k: v for k, v in data.items() if k not in ("rejected", "me")}
        for canon, v in data.items():
            als = (v or {}).get("aliases") if isinstance(v, dict) else v
            for a in [canon] + list(als or []):
                a = str(a).strip().lower()
                if a:
                    out[a] = str(canon)
                    out[" ".join(sorted(a.split()))] = str(canon)
    return out
