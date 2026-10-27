#------------------------------------------------------------------
# 동의어 사전 — 일상어 질문을 규정 용어로 넓혀 키워드 검색(BM25)만 돕는다
#=> 사용자는 "할아버지 돌아가시면 휴가 며칠" 처럼 묻는데 문서에는 "조부모 사망" 으로 적혀 있다.
#   키워드 검색은 글자가 같아야 맞으므로, 사전에 있는 일상어가 질문에 있으면 문서 용어를 BM25 질의에만
#   덧붙인다. 의미 검색·리랭커는 원래 질문을 그대로 쓴다(리랭커가 질문 뜻으로 최종 판정).
#   판단(비슷한 말인가)은 사전을 만드는 사람이 한다 — 질문할 때는 글자 그대로 찾기만 한다.
#   (plan/동의어사전_설계서_20261027.html §5 · 바꿔 말한 질문 세트 42문항: 일상어 근거 적중 24 → 문서 용어 35)
#
#   사전 파일(같은 모양, 뒤가 앞선다):
#    1) <프로그램 폴더>/synonyms.default.yaml — 배포에 동봉하는 기본 사전(배포 때 덮어씀)
#    2) <데이터 폴더>/synonyms.yaml (없으면 <프로그램 폴더>/synonyms.yaml) — 사용자 사전
#   파일이 바뀌면(수정 시각) 다음 질문 때 다시 읽는다 — 앱을 다시 켤 필요가 없다.
#------------------------------------------------------------------

import os
import re

from .. import config

DEFAULT_NAME = "synonyms.default.yaml"
USER_NAME = "synonyms.yaml"
# 일상어 앞자리 — 글 처음이거나 공백·괄호·문장부호 뒤(낱말 중간 "할아버지" 의 "아버지" 는 맞지 않게)
_BOUNDARY = r"(?:^|(?<=[\s(\[{,.!?·\"'“‘/]))"

_cache = {"key": None, "rules": []}


#------------------------------------------------------------------
# 사전 파일 경로들
#=> 기본 사전(프로그램 폴더)과 사용자 사전(데이터 폴더 우선, 없으면 프로그램 폴더)을 순서대로.
#
# -in: 없음
#
# -out: 있는 파일 경로 목록(앞 → 뒤 순서로 덮어쓴다)
# -out: error = 없음
#------------------------------------------------------------------
def _paths():
    out = []
    d = os.path.join(config.APP_DIR, DEFAULT_NAME)
    if os.path.isfile(d):
        out.append(d)
    for base in (config.ROOT, config.APP_DIR):
        u = os.path.join(base, USER_NAME)
        if os.path.isfile(u):
            out.append(u)
            break
    return out


#------------------------------------------------------------------
# 사전 읽기
#=> 파일들을 합쳐 (일상어 정규식, 일상어, 문서 용어) 규칙 목록을 만든다. 긴 일상어부터 맞춰 본다
#   ("애기 낳" 이 "애기" 보다 먼저). rejected 의 [일상어, 문서 용어] 짝은 뺀다.
#   일상어 안의 공백은 공백 여러 개에도 맞게 한다.
#
# -in: paths = 사전 파일 목록
#
# -out: [(compiled, 일상어, 문서 용어)]
# -out: error = 없음 (읽지 못한 파일은 건너뛴다 — 검색은 사전 없이도 돈다)
#------------------------------------------------------------------
def _load(paths):
    import yaml

    table, rejected = {}, set()
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
        except Exception:
            continue
        for term, words in (y.get("synonyms") or {}).items():
            for w in words or []:
                if isinstance(w, str) and w.strip():
                    table[w.strip()] = str(term).strip()
        for pair in y.get("rejected") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                rejected.add((str(pair[0]).strip(), str(pair[1]).strip()))
    rules = []
    for w in sorted(table, key=lambda s: (-len(s), s)):
        t = table[w]
        if (w, t) in rejected:
            continue
        pat = re.compile(_BOUNDARY + r"\s+".join(re.escape(part) for part in w.split()))
        rules.append((pat, w, t))
    return rules


#------------------------------------------------------------------
# 지금 쓸 규칙 (바뀌었으면 다시 읽기)
#=> 파일 경로와 수정 시각이 같으면 앞서 읽은 것을 쓴다.
#
# -in: 없음
#
# -out: 규칙 목록
# -out: error = 없음
#------------------------------------------------------------------
def rules():
    paths = _paths()
    key = tuple((p, os.path.getmtime(p)) for p in paths)
    if key != _cache["key"]:
        _cache["rules"] = _load(paths)
        _cache["key"] = key
    return _cache["rules"]


#------------------------------------------------------------------
# 사용자가 [아님] 한 짝
#=> 후보 제안(synonym_suggest)이 다시 묻지 않게 사전 파일들의 rejected 를 모은다.
#
# -in: 없음
#
# -out: {(일상어, 문서 용어)}
# -out: error = 없음 (읽지 못한 파일은 건너뛴다)
#------------------------------------------------------------------
def rejected():
    import yaml

    out = set()
    for p in _paths():
        try:
            with open(p, encoding="utf-8") as f:
                y = yaml.safe_load(f) or {}
        except Exception:
            continue
        for pair in y.get("rejected") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                out.add((str(pair[0]).strip(), str(pair[1]).strip()))
    return out


#------------------------------------------------------------------
# 질문 넓히기
#=> 질문에 있는 일상어마다 문서 용어를 덧붙인 BM25 질의를 만든다.
#    · 문서 용어가 이미 질문에 있으면 덧붙이지 않는다(공백 무시 비교)
#    · 같은 문서 용어는 한 번만
#    · 긴 일상어가 먼저 맞은 자리는 짧은 일상어가 다시 쓰지 않는다("애기 낳" 뒤의 "애기")
#
# -in: query = 사용자 질문
#
# -out: (BM25 질의, [(일상어, 문서 용어)]) — 넓힐 것이 없으면 (query, [])
# -out: error = 없음
#------------------------------------------------------------------
def expand(query):
    if not config.SYNONYMS or not query:
        return query, []
    return expand_with(rules(), query)


#------------------------------------------------------------------
# 주어진 규칙으로 질문 넓히기 (expand 의 본체 — 시험에서 사전 파일 없이 쓴다)
#
# -in: rule_list = _load 결과 / query = 질문
#
# -out: (BM25 질의, [(일상어, 문서 용어)])
# -out: error = 없음
#------------------------------------------------------------------
def expand_with(rule_list, query):
    nq = re.sub(r"\s+", "", query)
    used, added, pairs = [], [], []
    for pat, word, term in rule_list:
        m = pat.search(query)
        if not m or any(a < m.end() and m.start() < b for a, b in used):
            continue
        used.append((m.start(), m.end()))
        if re.sub(r"\s+", "", term) in nq or term in added:
            continue
        added.append(term)
        pairs.append((word, term))
    if not added:
        return query, []
    return query + " " + " ".join(added), pairs
