#------------------------------------------------------------------
# 동의어 승인 — 후보 읽기 · 사용자 사전(synonyms.yaml) 고치기 (동의어 설계서 4단계 §8)
#=> SimpleRAG 의 suggest-synonyms 가 만든 후보(synonym_candidates.json)를 설정 창에 보여 주고,
#   사용자가 [맞음]·[아님]·[직접 추가] 한 것을 워커의 데이터 폴더 synonyms.yaml 에 적는다.
#   워커는 이 파일의 수정 시각이 바뀌면 다음 질문 때 다시 읽는다 — 앱을 다시 켤 필요가 없다.
#
#   파일 모양(Rust 판 synonyms.rs 와 글자까지 같게 쓴다 — 낱말은 JSON 꼴 따옴표 글자, YAML 로도 맞다):
#     synonyms:
#       "조부모": ["할아버지", "외할머니"]
#     rejected:
#       - ["와이프", "여비"]
#   다시 쓰기 전 원래 파일은 synonyms.yaml.bak 으로 남긴다(손으로 적은 주석은 다시 쓰면 사라지므로).
#------------------------------------------------------------------

import io
import json
import os

USER_NAME = "synonyms.yaml"
CANDIDATES_NAME = "synonym_candidates.json"
MISSES_NAME = "synonym_misses.jsonl"
# 설정 창 동의어 탭에 한 번에 보여 줄 후보 줄 수 — 승인하면 다음 후보가 올라온다(많으면 보지 않는다, 설계서 §7)
SHOW_ROWS = 5

HEADER = (
    "# 내 동의어 사전 — RAGSearchBox 설정 창의 [맞음]·[직접 추가]·[아님] 이 적는다\n"
    "#   synonyms: 문서 용어: [일상어, ...]   질문에 일상어가 있으면 키워드 검색에 문서 용어를 덧붙인다\n"
    "#   rejected: [일상어, 문서 용어]        [아님] 한 짝 — 다시 제안하지 않고, 기본 사전에 있어도 쓰지 않는다\n"
    "#   손으로 고쳐도 되지만 설정 창에서 다시 쓰면 주석은 사라진다(직전 파일은 synonyms.yaml.bak)\n"
)


#------------------------------------------------------------------
# 사용자 사전 읽기
#=> 없으면 빈 사전. 모양이 틀린 항목은 건너뛴다.
#
# -in: home = 워커 데이터 폴더
#
# -out: (synonyms: {문서 용어: [일상어...]} 순서 보존, rejected: [[일상어, 문서 용어]...])
# -out: error = 파일이 YAML 로 읽히지 않으면 ValueError (덮어써서 지우지 않게 — 호출자가 안내한다)
#------------------------------------------------------------------
def load_user(home):
    path = os.path.join(home, USER_NAME)
    if not os.path.isfile(path):
        return {}, []
    import yaml

    try:
        with io.open(path, encoding="utf-8") as f:
            y = yaml.safe_load(f) or {}
    except Exception as e:
        raise ValueError("synonyms.yaml 을 읽지 못했습니다: {}".format(e))
    syn = {}
    for term, words in (y.get("synonyms") or {}).items():
        ws = [str(w).strip() for w in (words or []) if str(w).strip()]
        if str(term).strip() and ws:
            syn[str(term).strip()] = ws
    rej = []
    for pair in y.get("rejected") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            rej.append([str(pair[0]).strip(), str(pair[1]).strip()])
    return syn, rej


#------------------------------------------------------------------
# 사전 글 만들기 (두 판이 같은 글을 쓴다)
#
# -in: syn = {문서 용어: [일상어...]} / rej = [[일상어, 문서 용어]...]
#
# -out: 파일에 쓸 글
# -out: error = 없음
#------------------------------------------------------------------
def dump(syn, rej):
    q = lambda s: json.dumps(s, ensure_ascii=False)  # noqa: E731 — JSON 따옴표 글자는 YAML 에서도 그대로 읽힌다
    lines = [HEADER, "synonyms:"]
    for term, words in syn.items():
        lines.append("  {}: [{}]".format(q(term), ", ".join(q(w) for w in words)))
    lines.append("rejected:")
    for w, t in rej:
        lines.append("  - [{}, {}]".format(q(w), q(t)))
    return "\n".join(lines) + "\n"


#------------------------------------------------------------------
# 사용자 사전 쓰기
#=> 원래 파일은 .bak 으로 남기고, 임시 파일에 다 쓴 뒤 바꿔 넣는다(워커가 반쯤 쓴 파일을 읽지 않게).
#
# -in: home = 데이터 폴더 / syn, rej = 쓸 내용
#
# -out: 없음
# -out: error = 쓰기 실패는 OSError 전파
#------------------------------------------------------------------
def save_user(home, syn, rej):
    path = os.path.join(home, USER_NAME)
    if os.path.isfile(path):
        with io.open(path, "rb") as f:
            old = f.read()
        with io.open(path + ".bak", "wb") as f:
            f.write(old)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump(syn, rej))
    os.replace(tmp, path)


#------------------------------------------------------------------
# 이미 승인한 일상어인가
#=> 사전의 어느 문서 용어 아래에든 이 일상어가 있으면 승인한 것으로 본다.
#
# -in: syn = {문서 용어: [일상어...]}
# -in: word = 일상어
#
# -out: True = 이미 있음
# -out: error = 없음
#------------------------------------------------------------------
def _approved(syn, word):
    return any(word in ws for ws in syn.values())


#------------------------------------------------------------------
# 보여 줄 후보 줄
#=> 후보 파일의 낱말 중 아직 승인하지 않았고, [아님] 하지 않은 문서 용어가 남은 것만.
#
# -in: home = 데이터 폴더 / limit = 최대 줄 수
#
# -out: (줄 목록 [{word, terms:[...], example, docs:{용어: [문서...]}}], 남은 후보 전체 수)
# -out: error = 없음 (파일이 없거나 깨졌으면 빈 목록, 사전을 못 읽으면 후보 그대로)
#------------------------------------------------------------------
def pending(home, limit=SHOW_ROWS):
    try:
        with io.open(os.path.join(home, CANDIDATES_NAME), encoding="utf-8") as f:
            cands = json.load(f).get("candidates") or []
    except Exception:
        return [], 0
    try:
        syn, rej = load_user(home)
    except ValueError:
        syn, rej = {}, []
    rejected = {(w, t) for w, t in rej}
    rows = []
    for c in cands:
        w = c.get("word") or ""
        if not w or _approved(syn, w):
            continue
        terms = [t for t in c.get("terms") or [] if t.get("term") and (w, t["term"]) not in rejected]
        if not terms:
            continue
        rows.append({"word": w, "terms": [t["term"] for t in terms],
                     "example": (c.get("examples") or [""])[0],
                     "docs": {t["term"]: t.get("docs") or [] for t in terms}})
    return rows[:limit], len(rows)


#------------------------------------------------------------------
# 짝 확인 — 승인·직접 추가 전에
#=> 빈 칸, 한 글자 일상어(다른 낱말 앞에도 걸린다: "애" → "애플"), 같은 말은 받지 않는다.
#
# -in: word = 일상어 / term = 문서 용어
#
# -out: 문제 설명 글, 괜찮으면 None
# -out: error = 없음
#------------------------------------------------------------------
def check_pair(word, term):
    if not word or not term:
        return "일상어와 문서 용어를 모두 적으세요"
    if len(word.replace(" ", "")) < 2:
        return "한 글자 일상어는 다른 낱말에도 걸려 넣지 않습니다(예: \"애\" → \"애플\")"
    if word.replace(" ", "") == term.replace(" ", ""):
        return "일상어와 문서 용어가 같습니다"
    return None


#------------------------------------------------------------------
# [맞음] · [직접 추가] — 일상어 → 문서 용어 를 사전에 넣는다
#=> 같은 짝이 [아님] 목록에 있으면 거기서 뺀다(마음이 바뀐 것).
#
# -in: home = 데이터 폴더 / word = 일상어 / term = 문서 용어
#
# -out: (ok, 안내 글)
# -out: error = 없음 (읽기·쓰기 실패는 False 와 이유 글)
#------------------------------------------------------------------
def approve(home, word, term):
    word, term = (word or "").strip(), (term or "").strip()
    bad = check_pair(word, term)
    if bad:
        return False, bad
    try:
        syn, rej = load_user(home)
        if word in syn.get(term, []):
            return True, "이미 들어 있습니다: {} → {}".format(word, term)
        syn.setdefault(term, []).append(word)
        rej = [p for p in rej if p != [word, term]]
        save_user(home, syn, rej)
    except (ValueError, OSError) as e:
        return False, "사전에 적지 못했습니다: {}".format(e)
    return True, "넣었습니다: {} → {} (다음 질문부터)".format(word, term)


#------------------------------------------------------------------
# [아님] — 이 짝은 다시 제안하지 않는다
#
# -in: home = 데이터 폴더 / word = 일상어 / term = 문서 용어
#
# -out: (ok, 안내 글)
# -out: error = 없음 (읽기·쓰기 실패는 False 와 이유 글)
#------------------------------------------------------------------
def reject(home, word, term):
    word, term = (word or "").strip(), (term or "").strip()
    if not word or not term:
        return False, "고른 짝이 없습니다"
    try:
        syn, rej = load_user(home)
        if [word, term] not in rej:
            rej.append([word, term])
        # 앞서 승인했던 짝이면 사전에서도 뺀다
        if word in syn.get(term, []):
            syn[term] = [w for w in syn[term] if w != word]
            if not syn[term]:
                del syn[term]
        save_user(home, syn, rej)
    except (ValueError, OSError) as e:
        return False, "사전에 적지 못했습니다: {}".format(e)
    return True, "다시 묻지 않습니다: {} → {}".format(word, term)


#------------------------------------------------------------------
# 승인한 짝 목록 (동의어 탭 아래 목록)
#
# -in: home = 데이터 폴더
#
# -out: [(일상어, 문서 용어)] 사전에 적힌 순서
# -out: error = 없음 (못 읽으면 빈 목록)
#------------------------------------------------------------------
def approved_pairs(home):
    try:
        syn, _ = load_user(home)
    except ValueError:
        return []
    return [(w, t) for t, ws in syn.items() for w in ws]


#------------------------------------------------------------------
# [빼기] — 승인한 짝을 사전에서 뺀다 ([아님] 과 달리 다시 제안될 수 있다)
#
# -in: home = 데이터 폴더 / word = 일상어 / term = 문서 용어
#
# -out: (ok, 안내 글)
# -out: error = 없음 (읽기·쓰기 실패는 False 와 이유 글)
#------------------------------------------------------------------
def remove(home, word, term):
    try:
        syn, rej = load_user(home)
        if word not in syn.get(term, []):
            return False, "사전에 없는 짝입니다: {} → {}".format(word, term)
        syn[term] = [w for w in syn[term] if w != word]
        if not syn[term]:
            del syn[term]
        save_user(home, syn, rej)
    except (ValueError, OSError) as e:
        return False, "사전에 적지 못했습니다: {}".format(e)
    return True, "뺐습니다: {} → {} (다음 질문부터)".format(word, term)


#------------------------------------------------------------------
# 안내 줄에 쓸 수 — 놓친 질문 수(같은 질문은 하나로) · 승인한 일상어 수
#
# -in: home = 데이터 폴더
#
# -out: (놓친 질문 수, 승인한 일상어 수)
# -out: error = 없음 (못 읽으면 0)
#------------------------------------------------------------------
def counts(home):
    qs = set()
    try:
        with io.open(os.path.join(home, MISSES_NAME), encoding="utf-8") as f:
            for line in f:
                try:
                    qs.add(json.loads(line).get("q") or "")
                except ValueError:
                    pass
    except OSError:
        pass
    qs.discard("")
    try:
        syn, _ = load_user(home)
    except ValueError:
        syn = {}
    return len(qs), sum(len(ws) for ws in syn.values())
