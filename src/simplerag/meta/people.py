#------------------------------------------------------------------
# 사람 별칭 사전 — 자동 제안 + 승인 (메타데이터 검색 설계서 §7-2 · §17 3단계 · 결정 D2)
#=> 계정 ID · 영문 이름(bong9, Cho Kang Lae)을 대표 이름(고봉수, 조강래)으로 잇는 사전을 만든다.
#   자동으로 적용하지 않는다 — 후보를 보여 주고 사람이 승인한 것만 쓴다(오연결 위험, 동의어 사전과 같은 흐름).
#
#   후보 근거 세 가지
#    ① 같은 문서 동시 출현 — 한 문서의 만든 이 / 마지막 저장자가 한글 이름 / 계정·영문이고 그 짝이 2번 이상
#    ② 로마자 일치 — Cho Kang Lae · kim-sung-su ↔ 조강래 · 김성수 (성 표기 표 + 음절 표기 변형)
#    ③ 경로 단서 — 파일 이름에 한글 이름(…_고봉수.xlsx · (고봉수))이 있고 그 문서 작성 · 저장자가 계정
#
#   파일(데이터 루트, synonyms.yaml 과 같은 자리)
#    people.default.yaml — 배포 기본값(배포 때 덮어씀)
#    people.yaml        — 사용자 사전: people: {대표: [별칭…]} · rejected: [[별칭, 대표]…] · me: 대표
#    people_candidates.json — 마지막 후보 목록(승인 화면 · 명령이 읽는다)
#------------------------------------------------------------------

import json
import os
import re

from . import normalize as N
from .rules import load_rules

HEADER = """# ─────────────────────────────────────────────────────────────────────
# 사람 별칭 사전 — 계정 ID · 영문 이름을 대표 이름으로 잇는다 (plan/메타데이터검색_설계서_20261029.html §7-2)
#
#   people:   대표 이름: [별칭, 별칭, ...]     예) 고봉수: [bong9, bong49]
#   rejected: [[별칭, 대표 이름], ...]          후보로 다시 올리지 않는 짝
#   me:       대표 이름                          "내가 만든 문서" 의 '나'
#   후보는 simplerag meta people suggest 로 만들고 approve · reject 로 고른다.
#   고친 뒤에는 simplerag meta rebuild 로 다시 정규화한다(approve · reject 는 저절로 한다).
# ─────────────────────────────────────────────────────────────────────
"""

# ── 로마자 (②) ───────────────────────────────────────────

# 성 — 실제로 쓰는 표기들(국어 로마자 표기법 + 관용 표기)
SURNAMES = {
    "김": "kim gim", "이": "lee yi rhee lie i", "박": "park pak bak", "최": "choi choe chey", "정": "jung jeong chung joung",
    "조": "cho jo joe", "강": "kang gang", "윤": "yoon yun", "장": "jang chang", "임": "lim im rim", "한": "han",
    "오": "oh o", "서": "seo suh so", "신": "shin sin", "권": "kwon gwon", "황": "hwang", "안": "ahn an",
    "송": "song", "류": "ryu yoo yu", "유": "yoo yu you", "홍": "hong", "전": "jeon jun chun", "고": "ko go koh",
    "문": "moon mun", "양": "yang", "손": "son sohn", "배": "bae bai", "백": "baek paek baik", "허": "heo huh hur",
    "남": "nam", "노": "noh no roh", "하": "ha", "곽": "kwak gwak", "성": "sung seong", "차": "cha",
    "주": "joo ju chu", "우": "woo u", "구": "koo gu ku", "민": "min", "나": "na ra", "진": "jin chin",
    "지": "ji chi", "엄": "um eom", "채": "chae", "원": "won", "천": "chun cheon", "방": "bang", "공": "kong gong",
    "현": "hyun hyeon", "함": "ham", "변": "byun byeon", "염": "yeom yum", "여": "yeo yuh", "추": "choo chu",
    "도": "do doh", "소": "so", "석": "seok suk", "선": "sun seon", "설": "seol sul", "마": "ma", "길": "gil kil",
    "연": "yeon yun", "위": "wi wee", "표": "pyo", "명": "myung myeong", "기": "ki gi", "반": "ban", "왕": "wang",
    "금": "keum geum", "옥": "ok", "육": "yook yuk", "인": "in", "맹": "maeng", "제": "je", "모": "mo",
    "탁": "tak", "국": "kook guk", "은": "eun", "편": "pyun pyeon", "용": "yong", "예": "ye", "경": "kyung gyeong",
    "봉": "bong", "사": "sa", "부": "boo bu", "태": "tae", "목": "mok", "형": "hyung hyeong", "두": "doo du",
    "동": "dong", "호": "ho", "승": "seung", "상": "sang", "시": "si", "단": "dan",
}
_INIT = ["g k", "kk", "n", "d t", "tt", "r l", "m", "b p", "pp", "s", "ss", "", "j ch", "jj", "ch", "k", "t", "p", "h"]
_MED = ["a", "ae", "ya", "yae", "eo u o", "e", "yeo yu yo", "ye", "o oh", "wa", "wae", "oe oi", "yo", "u oo", "wo", "we",
        "wi", "yu yoo", "eu u", "ui eui", "i ee y"]
_FIN = ["", "k g", "kk k", "k", "n", "n", "n", "t d", "l r", "k", "m", "l", "l", "l", "p", "l", "m", "p b", "p", "t s",
        "t", "ng", "t", "t", "k", "t", "p", "t"]


# 한글 음절 하나 → 로마자 표기 후보 정규식
def _syl(ch):
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return re.escape(ch)
    i, m, f = code // 588, (code % 588) // 28, code % 28
    alt = lambda s: "(?:" + "|".join(re.escape(x) for x in s.split(" ")) + ")" if " " in s else re.escape(s)  # noqa: E731
    return alt(_INIT[i]) + alt(_MED[m]) + alt(_FIN[f])


#------------------------------------------------------------------
# 한글 이름 ↔ 영문 · 계정 이름이 같은 사람으로 읽히는가 (②)
#=> 성은 표, 이름 음절은 표기 변형을 허락한다. 순서(성 앞 · 뒤)와 띄어쓰기 · 하이픈은 따지지 않는다.
#   "Cho Kang Lae" · "kang-lae cho" · "kim-sung-su" · "choiwoosun" 이 각각 조강래 · 조강래 · 김성수 · 최우선.
#
# -in: kname = 한글 이름(2~4자) / ename = 영문 · 계정(소문자)
#
# -out: True / False
# -out: error = 없음
#------------------------------------------------------------------
def romanized_match(kname, ename):
    if not re.fullmatch(r"[가-힣]{2,4}", kname or ""):
        return False
    toks = [t for t in re.split(r"[\s\-_.,]+", ename.lower()) if t]
    if not toks or any(not re.fullmatch(r"[a-z]+", t) for t in toks):
        return False
    sur = SURNAMES.get(kname[0])
    sur_re = "(?:" + "|".join(sur.split()) + ")" if sur else _syl(kname[0])
    given = "".join(_syl(c) for c in kname[1:])
    # 성 + 이름(붙여 쓰기 · 띄어 쓰기 모두) 를 앞뒤 순서 둘 다 본다
    flat = "".join(toks)
    if re.fullmatch(sur_re + given, flat) or re.fullmatch(given + sur_re, flat):
        return len(toks) >= 2 or len(flat) >= 6     # 한 덩어리 짧은 계정(kim)은 너무 흔해 받지 않는다
    return False


#------------------------------------------------------------------
# 계정 ID 가 이름 로마자의 일부인가 — bong9 ↔ 고봉수(봉), kobong ↔ 고봉수(고봉), bongsu ↔ 고봉수(봉수)
#=> 계정 글자(숫자 · 기호 뺀 것)가 [성+이름 · 이름 · 성+이름 첫 음절 · 이름 첫 음절] 중 하나와 통째로 같으면 참.
#   성만 같은 것(kim · lee · yoon)은 너무 흔해 받지 않는다. 글자가 3자 미만이면 받지 않는다.
#
# -in: kname = 한글 이름 / letters = 계정 글자(소문자 영문만)
#
# -out: True / False
# -out: error = 없음
#------------------------------------------------------------------
def partial_match(kname, letters):
    if len(letters) < 3 or not re.fullmatch(r"[가-힣]{2,4}", kname or ""):
        return False
    # 흔한 성 표기만으로 된 계정(yoon · sung · lee)은 누구라도 될 수 있다 — 외자 이름(허성)에도 맞추지 않는다
    if letters in COMMON_SURNAME_ROMAN:
        return False
    sur = SURNAMES.get(kname[0])
    sur_re = "(?:" + "|".join(sur.split()) + ")" if sur else _syl(kname[0])
    given = "".join(_syl(c) for c in kname[1:])
    first = _syl(kname[1])
    pats = [sur_re + given, given + sur_re, given, sur_re + first, first + sur_re, first]
    # 회사 계정에 흔한 "이름 이니셜 + 성"(skchoi · dhkim · jhrho) · "성 + 이름 이니셜"(kimsj)
    gi = _given_initials(kname)
    if gi:
        pats += [gi + sur_re, sur_re + gi]
    for pat in pats:
        if re.fullmatch(pat, letters):
            return True
    return False


# 흔한 성의 로마자 — 이 글자만으로 된 계정(yoon · sung · lee)은 이름 일부로 보지 않는다
COMMON_SURNAME_ROMAN = {r for s in "김이박최정조강윤장임한오서신권황안송류유홍전고문양손배백허남노하곽성차주우구민나진지"
                        for r in SURNAMES[s].split()}


# 음절 첫 소리 한 글자 정규식 — ㄱ=g/k, ㄹ=r/l, 받침 없는 ㅇ 은 모음 첫 글자
def _ini_re(ch):
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return None
    ini = _INIT[code // 588]
    firsts = {x[0] for x in ini.split(" ") if x} or {x[0] for x in _MED[(code % 588) // 28].split(" ")}
    return "[" + "".join(sorted(firsts)) + "]"


# 이름(성 뺀) 음절들의 첫 소리 — 강래 → [gk][rl]
def _given_initials(kname):
    parts = [_ini_re(c) for c in kname[1:]]
    return "".join(parts) if all(parts) else None


#------------------------------------------------------------------
# 이니셜 일치 — ckl ↔ 조강래 (성 첫 글자 + 이름 음절 첫 소리, 표기 변형 허락)
#
# -in: kname = 한글 이름 / letters = 계정 글자
#
# -out: True / False
#------------------------------------------------------------------
def initials_match(kname, letters):
    sur = SURNAMES.get(kname[0])
    if not sur or len(letters) < 3:
        return False
    sur_first = "[" + "".join(sorted({x[0] for x in sur.split()})) + "]"
    gi = _given_initials(kname)
    return bool(gi) and re.fullmatch(sur_first + gi, letters) is not None



# ── 사전 파일 ─────────────────────────────────────────────

#------------------------------------------------------------------
# 사용자 사전 읽기 — {people: {대표: [별칭]}, rejected: [[별칭, 대표]], me: 대표}
#
# -in: root = 데이터 루트
#
# -out: dict (없으면 빈 모양)
# -out: error = 없음 (깨졌으면 빈 모양)
#------------------------------------------------------------------
def load_user(root):
    out = {"people": {}, "rejected": [], "me": None}
    p = os.path.join(root, "people.yaml")
    if not os.path.isfile(p):
        return out
    try:
        import yaml
        with open(p, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — 깨진 사전은 빈 사전처럼
        return out
    out["people"] = {str(k): [str(a) for a in (v or [])] for k, v in (d.get("people") or {}).items()}
    out["rejected"] = [[str(a), str(b)] for a, b in (d.get("rejected") or []) if a and b]
    out["me"] = d.get("me")
    return out


#------------------------------------------------------------------
# 사용자 사전 쓰기 — 머리 설명을 붙여 people.yaml 로
#
# -in: root = 데이터 루트 / data = load_user() 모양
#
# -out: 파일 경로
# -out: error = 쓰기 실패는 OSError 전파
#------------------------------------------------------------------
def save_user(root, data):
    import yaml
    p = os.path.join(root, "people.yaml")
    body = {"people": {k: sorted(set(v)) for k, v in sorted(data["people"].items()) if v},
            "rejected": data["rejected"]}
    if data.get("me"):
        body["me"] = data["me"]
    with open(p, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(body, f, allow_unicode=True, sort_keys=False, default_flow_style=None)
    return p


#------------------------------------------------------------------
# 별칭 승인 · 직접 추가 — 대표 이름 아래에 별칭을 넣는다(다른 대표에 있던 같은 별칭은 옮긴다)
#
# -in: root / alias = 별칭(계정 · 영문) / name = 대표 이름
#
# -out: 안내 글
# -out: error = 쓰기 실패는 OSError 전파
#------------------------------------------------------------------
def approve(root, alias, name):
    alias, name = alias.strip().lower(), name.strip()
    if not alias or not name:
        return "별칭과 대표 이름을 모두 적으세요"
    d = load_user(root)
    for k in list(d["people"]):
        d["people"][k] = [a for a in d["people"][k] if a.lower() != alias]
    d["people"].setdefault(name, []).append(alias)
    d["rejected"] = [r for r in d["rejected"] if not (r[0].lower() == alias and r[1] == name)]
    save_user(root, d)
    return "승인: {} → {}".format(alias, name)


#------------------------------------------------------------------
# 후보 무시 — 그 짝은 다시 후보로 올리지 않는다
#------------------------------------------------------------------
def reject(root, alias, name):
    alias, name = alias.strip().lower(), name.strip()
    d = load_user(root)
    if [alias, name] not in d["rejected"]:
        d["rejected"].append([alias, name])
    d["people"][name] = [a for a in d["people"].get(name, []) if a.lower() != alias]
    save_user(root, d)
    return "무시: {} → {} (다시 제안하지 않음)".format(alias, name)


#------------------------------------------------------------------
# 승인한 별칭 빼기
#------------------------------------------------------------------
def remove(root, alias):
    alias = alias.strip().lower()
    d = load_user(root)
    hit = [k for k, v in d["people"].items() if alias in [a.lower() for a in v]]
    for k in hit:
        d["people"][k] = [a for a in d["people"][k] if a.lower() != alias]
    save_user(root, d)
    return "뺌: {}".format(alias) if hit else "사전에 없는 별칭입니다: {}".format(alias)


#------------------------------------------------------------------
# 이 PC 사용자의 계정 이름들 — Windows 계정명 · Office 사용자 이름 (§7-2 "나")
#
# -in: 없음
#
# -out: [이름, …] (소문자 · 중복 없음)
# -out: error = 없음 (레지스트리를 못 읽으면 Windows 계정명만)
#------------------------------------------------------------------
def my_accounts():
    out = []
    u = os.environ.get("USERNAME")
    if u:
        out.append(u)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Office\Common\UserInfo") as k:
            for v in ("UserName", "UserInitials"):
                try:
                    val = winreg.QueryValueEx(k, v)[0]
                    if val and v == "UserName":
                        out.append(val)
                except OSError:
                    pass
    except (OSError, ImportError):
        pass
    seen, res = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            res.append(x)
    return res


#------------------------------------------------------------------
# "나" 정하기 — 대표 이름을 me 로 적고, 이 PC 계정 이름들을 그 별칭으로 승인한다
#
# -in: root / name = 대표 이름
#
# -out: 안내 글
#------------------------------------------------------------------
def set_me(root, name):
    d = load_user(root)
    d["me"] = name.strip()
    save_user(root, d)
    added = []
    rules = load_rules(root)
    for a in my_accounts():
        got = N.norm_person(a, rules)
        # 한글 이름이면 그 자체가 대표 이름 — 별칭으로 넣지 않는다
        if got and got[0] != name and got[1] in ("account", "english"):
            approve(root, got[0], name)
            added.append(got[0])
    return "나 = {}{}".format(name, " · 이 PC 계정 " + ", ".join(added) + " 를 별칭으로 승인" if added else "")


# ── 후보 만들기 ───────────────────────────────────────────

#------------------------------------------------------------------
# 별칭 후보 만들기 (§7-2)
#=> meta.sqlite 의 원본 작성자 · 저장자 값을 (별칭 없이) 정규화해 근거를 모은다.
#   상속 문서(회사 양식)의 만든 이는 쓰지 않는다 — 양식을 만든 사람과 실제 작성자가 섞인다.
#   ⚠️ 분류함 실측(2,295개)에서 "같은 문서 동시 출현" 짝은 대부분 남이 만든 문서를 다른 사람이
#      저장한 경우였다(김영민 작성 ← youngho yoon 저장, 나문규 서식 ← bong9 저장). 그래서
#      이름 모양 근거(로마자 · 이름 일부 · 이니셜)가 있어야만 후보로 올리고, 동시 출현 · 파일명은
#      점수를 더하는 데만 쓴다.
#    이름 모양: 영문 이름 = 로마자 일치(5) / 계정 = 로마자 일치(5) · 이름 일부(3) · 이니셜(2)
#    점수 = 이름 모양 + 동시 출현 문서 × 2 + 파일명 단서 문서 + 그 이름의 문서 수가 많으면 +1
#    한 별칭이 이름 일부 · 이니셜로 여러 이름에 똑같이 맞고 다른 근거로 갈리지 않으면 모호해 올리지 않는다.
#   이미 승인했거나 무시한 짝은 뺀다.
#
# -in: conn = meta.sqlite / root = 데이터 루트
#
# -out: [{alias, name, score, kind, cooc, path, docs, examples}] (점수 순)
# -out: error = 없음
#------------------------------------------------------------------
def suggest(conn, root):
    rules = load_rules(root)
    user = load_user(root)
    approved = {a.lower() for v in user["people"].values() for a in v}
    rejected = {(a.lower(), b) for a, b in user["rejected"]}
    ev = {}            # (alias, name) → {cooc: set, path: set}
    names = {}         # 한글 이름 → 문서 수
    others = {}        # 계정 · 영문 → 문서 수
    for path, raw_json, inh in conn.execute("SELECT doc_path, raw_json, inherited FROM doc_meta"):
        doc = (json.loads(raw_json) or {}).get("doc") or {}
        got = []
        for field in ("author", "last_saved_by"):
            if field == "author" and inh:
                continue
            g = N.norm_person(doc.get(field), rules)
            if g:
                got.append(g)
        kor = {n for n, k in got if k == "korean"}
        oth = {n for n, k in got if k in ("account", "english")}
        for n in kor:
            names[n] = names.get(n, 0) + 1
        for o in oth:
            others[o] = others.get(o, 0) + 1
        for o in oth:
            for n in kor:
                ev.setdefault((o, n), {"cooc": set(), "path": set()})["cooc"].add(path)
            # ③ 경로 단서 — 파일 이름 속 한글 이름(2~4자)
            stem = os.path.splitext(os.path.basename(path))[0]
            for n in set(re.findall(r"(?<![가-힣])([가-힣]{3})(?![가-힣])", stem)):
                ev.setdefault((o, n), {"cooc": set(), "path": set()})["path"].add(path)
    # 사람으로 나온 한글 이름만 대표 후보다(파일명 속 세 글자는 사람 이름이 아닐 수도 있다)
    known = set(names)
    empty = {"cooc": set(), "path": set()}
    res = []
    for o in sorted(others):
        if o in approved:
            continue
        is_acct = " " not in o
        letters = re.sub(r"[^a-z]", "", o)
        cands = []
        for n in known:
            if (o, n) in rejected:
                continue
            # 이름 모양 근거 — 이것이 없으면 후보가 아니다
            if romanized_match(n, o):
                kind, shape = "로마자", 5
            elif is_acct and partial_match(n, letters):
                kind, shape = "이름 일부", 3
            elif is_acct and initials_match(n, letters):
                kind, shape = "이니셜", 2
            else:
                continue
            e = ev.get((o, n), empty)
            c, p = len(e["cooc"]), len(e["path"] - e["cooc"])
            score = shape + c * 2 + p + (1 if names.get(n, 0) >= 10 else 0)
            ex = sorted(e["cooc"] | e["path"])[:3]
            cands.append({"alias": o, "name": n, "score": score, "kind": kind, "cooc": c, "path": p,
                          "docs": others.get(o, 0), "name_docs": names.get(n, 0),
                          "examples": [os.path.basename(x) for x in ex]})
        if not cands:
            continue
        cands.sort(key=lambda x: -x["score"])
        # 같은 점수로 여러 이름에 맞으면 모호하다 — 로마자 일치가 아니면 올리지 않는다
        if len(cands) > 1 and cands[0]["score"] == cands[1]["score"] and cands[0]["kind"] != "로마자":
            continue
        res.append(cands[0])
    res.sort(key=lambda x: (-x["score"], x["alias"]))
    try:
        with open(os.path.join(root, "people_candidates.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return res
