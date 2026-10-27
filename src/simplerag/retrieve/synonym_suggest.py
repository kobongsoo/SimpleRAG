#------------------------------------------------------------------
# 동의어 후보 제안 — 놓친 질문을 모아 두었다가 "이 일상어 → 이 문서 용어" 후보를 뽑는다 (동의어 설계서 3단계 §7)
#=> 사전을 미리 다 알 수 없으니, 실제로 근거를 못 찾은 질문에서 사전에 넣을 짝을 찾는다.
#   판단(같은 뜻인가)은 사람이 승인 화면에서 한다 — 여기서는 고를 후보만 만든다. 질문 속도와 무관한 일괄 작업.
#
#   1) 기록: 답할 때 "관련 근거 없음"이었거나 리랭커 최고 점수가 0 미만인 질문을 synonym_misses.jsonl 에 한 줄씩
#      (바꿔 말한 질문 42문항에서 놓친 18문항 중 14개가 걸리고, 골든 100문항은 5개만 걸린다 — 2026-10-27 실측)
#   2) 제안(suggest-synonyms 명령):
#      · 질문을 다시 검색해 리랭커 후보 10건을 얻는다 — 놓친 질문도 정답 조항은 대개 이 안에 있다(42문항 중 32/34 짝)
#      · 질문의 핵심 낱말 중 그 10건에 글자로 없는 것 = 문서가 쓰지 않는 말 → 넓힐 대상
#      · 10건에 나오는 명사꼴 문서 용어를 e5 유사도로 줄 세우고, 상위 10개를 1.7B 에게 보여 같은 뜻 하나를 고르게 한다
#        (정답 짝 1위 적중: e5 만 9/34 → 1.7B 고르기 14/34 · 상위 3 적중 15 → 19)
#      · 1.7B 가 "같은 뜻 없음"이라 한 낱말 · 사전에 이미 있는 일상어 · [아님] 한 짝(rejected)은 뺀다. 자주 놓친 낱말부터.
#   결과: <데이터 폴더>/synonym_candidates.json (승인 화면이 읽는다 — 4단계)
#------------------------------------------------------------------

import datetime
import io
import json
import os
import re

from .. import config
from . import synonyms
from .hybrid import keywords

MISSES_NAME = "synonym_misses.jsonl"
CANDIDATES_NAME = "synonym_candidates.json"
# 이 점수 미만이면 놓친 질문으로 본다(리랭커 bge 점수) — 0 이면 일상어로 놓친 질문 14/18 을 잡고 골든은 5/100 만 걸린다
MISS_BELOW = 0.0
# 기록 파일이 이보다 커지면 .old 로 넘기고 새로 쓴다 — 질문 수천 개쯤
MISSES_MAX_BYTES = 1_000_000
# 제안할 때 볼 최근 질문 수 · 낱말마다 보여 줄 후보 수 · LLM 에게 보일 후보 수 · 파일에 담을 낱말 수(화면은 앞의 몇 개만)
RECENT = 200
SHOW = 3
LLM_CHOICES = 10
# chat 워커(/suggest-synonyms)에서 볼 최근 질문 수 — 1.7B 고르기로 질문당 약 3초, RAGSearchBox 명령 시간 제한 300초 안에
WORKER_RECENT = 60
KEEP_WORDS = 100
# 문서 용어로 치지 않는 끝 글자 — 동사·형용사·관형사꼴("받아", "따른", "하여")을 걸러 명사꼴만 남긴다
_VERB_END = set("다고며여어아서면게지한할된될는은을던든록라려니요야냐도의")
# 일상 질문에 흔한 말 — 문서에 없어도 넓힐 대상이 아니다("며칠", "얼마야", "나와")
_QUESTION_WORDS = {
    "며칠", "몇일", "얼마", "얼마야", "얼마나", "얼마까지", "나와", "나오", "나오는", "나오나", "하면", "하는", "해야", "해도",
    "되나", "될까", "되는", "되면", "있어", "있나", "있는", "없어", "없나", "안에", "달에", "언제", "어디", "누구", "누구한테",
    "뭐야", "무슨", "받아", "받을", "받나", "받으면", "받는", "줘야", "주나", "주는", "주면", "그럼", "그냥", "근데", "이거", "그거",
    "내가", "제가", "우리", "회사", "회사에서", "어떻게", "어떡해", "알려줘", "궁금해", "쳐야", "치나", "쓰면", "쓰나", "가면",
    "갈때", "할때", "몇개", "몇번", "몇명", "최대", "아니면", "같은", "다른", "중간에", "끼면", "얼만데", "보통", "따로", "총",
    "잇나", "잇어", "있나요", "할수", "될수", "쓸수", "사용할수", "있을까", "없을까", "얼마까지인지",
}
# 짧은 동사꼴 질문 말의 끝 글자 — 세 글자 이하로 이 글자로 끝나면("되고", "걸로", "오래", "쳐줘") 넓힐 대상이 아니다
_SHORT_VERB_END = set("고로야서지게요줘래니면데까")
# 문서 용어로 치지 않는 문법 말 — 명사꼴 거르기를 지나도 남는 것("따른", "으로", "위와")
_GRAMMAR_TERMS = {"따른", "따라", "으로", "위와", "같이", "이에", "이내", "내에", "통해", "의해", "경우", "경우에", "해당",
                  "다음", "이후", "이전", "또는", "및", "중에", "있음", "없음", "이상", "이하", "미만", "초과", "각호", "제외"}
# 구어 조사 — hybrid.keywords 가 떼지 않는 것("공무원한테" → "공무원")
_SPOKEN_JOSA = ("한테서", "한테", "께서", "에서는", "이랑", "랑", "하고")
_HANGUL_TERM = re.compile(r"[가-힣]{2,8}")
_HANGUL_WORD = re.compile(r"[가-힣]{2,}")
_WS = re.compile(r"\s+")

LLM_SYSTEM = "당신은 한국어 낱말 뜻을 판정하는 도우미입니다."
LLM_USER = ("질문: {q}\n\n질문 속 낱말 \"{w}\" 를 규정 문서에서는 어떤 용어로 쓸까요? 같은 뜻인 용어를 아래 후보에서 하나만 고르세요. "
            "같은 뜻인 것이 없으면 0.\n\n후보:\n{cands}\n\n번호만 답하세요.")


#------------------------------------------------------------------
# 놓친 질문 기록
#=> 답변 한 번이 끝날 때 부른다. 근거를 못 찾았거나(관련 근거 없음) 리랭커가 확신하지 못한 질문만
#   데이터 폴더의 synonym_misses.jsonl 에 한 줄로 남긴다. 기록은 이 PC 에만 남는다.
#    1) 설정(retrieval.synonym_log)이 꺼졌으면 아무것도 하지 않는다
#    2) 리랭커 점수가 없으면(리랭커 끔) 관련 근거 없음일 때만 기록한다
#    3) 파일이 너무 커지면 .old 로 넘긴다
#
# -in: query  = 사용자 질문
# -in: timing = 검색 timing (rerank_top · no_relevant · synonyms)
#
# -out: True = 기록함
# -out: error = 없음 (쓰기 실패는 삼킨다 — 기록 때문에 답이 죽으면 안 된다)
#------------------------------------------------------------------
def log_miss(query, timing):
    if not config.SYNONYM_LOG or not query or not timing:
        return False
    top = timing.get("rerank_top")
    no_rel = bool(timing.get("no_relevant"))
    if not no_rel and (top is None or top >= MISS_BELOW):
        return False
    rec = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "q": query,
           "top": top, "no_relevant": no_rel, "expanded": timing.get("synonyms") or []}
    path = os.path.join(config.ROOT, MISSES_NAME)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > MISSES_MAX_BYTES:
            os.replace(path, path + ".old")
        with io.open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


#------------------------------------------------------------------
# 기록된 질문 읽기
#=> 줄마다 JSON 의 "q"(없으면 "user_input")를 읽어, 같은 질문은 한 번만 최근 순서로 남긴다.
#   평가할 때는 바꿔 말한 질문 세트(jsonl)를 그대로 넣을 수 있다.
#
# -in: path = jsonl 경로
#
# -out: [(질문, 기록된 횟수)] 최근 RECENT 개
# -out: error = 파일이 없으면 빈 목록
#------------------------------------------------------------------
def read_misses(path):
    if not os.path.isfile(path):
        return []
    counts, order = {}, []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            q = (r.get("q") or r.get("user_input") or "").strip()
            if not q:
                continue
            if q not in counts:
                order.append(q)
            counts[q] = counts.get(q, 0) + 1
    return [(q, counts[q]) for q in order[-RECENT:]]


#------------------------------------------------------------------
# 공백 지운 글
#=> 청크 줄바꿈·띄어쓰기와 상관없이 낱말이 들었는지 보려고 공백을 모두 지운다.
#
# -in: s = 글
#
# -out: 공백 없는 글
# -out: error = 없음
#------------------------------------------------------------------
def _flat(s):
    return _WS.sub("", s or "")


#------------------------------------------------------------------
# 구어 조사 떼기
#=> "공무원한테" → "공무원" 처럼 hybrid.keywords 가 떼지 않는 구어 조사를 뗀다. 떼고도 두 글자 이상 남을 때만.
#
# -in: w = 낱말
#
# -out: 조사를 뗀 낱말
# -out: error = 없음
#------------------------------------------------------------------
def _strip_spoken(w):
    for j in _SPOKEN_JOSA:
        if w.endswith(j) and len(w) >= len(j) + 2:
            return w[: -len(j)]
    return w


#------------------------------------------------------------------
# 질문 하나에서 넓힐 낱말과 문서 용어 후보 뽑기
#=> 넓힐 낱말 = 질문의 한글 핵심 낱말 중 검색 후보 글에 글자로 없고, 사전이 아직 넓히지 않는 것.
#   문서 용어 = 검색 후보 글의 한글 핵심 낱말(2~8자, 명사꼴) 중 넓힐 낱말이 아닌 것.
#
# -in: query = 질문 / texts = 검색 후보 청크 본문들
#
# -out: (넓힐 낱말 목록, 문서 용어 목록)
# -out: error = 없음
#------------------------------------------------------------------
def words_and_terms(query, texts):
    body = " ".join(texts)
    flat = _flat(body)
    # 넓히기를 끈 상태(평가 등)면 사전도 없는 것으로 본다
    rules = synonyms.rules() if config.SYNONYMS else []
    words = []
    for w in dict.fromkeys(_strip_spoken(w) for w in keywords(query)):
        # 질문에 흔한 말, 문서에 이미 있는 말, 한글이 아닌 말, 사전이 이미 넓히는 말은 넓힐 필요가 없다
        if (w in _QUESTION_WORDS or w in flat or not _HANGUL_WORD.fullmatch(w)
                or (len(w) <= 3 and w[-1] in _SHORT_VERB_END) or synonyms.expand_with(rules, w)[1]):
            continue
        words.append(w)
    terms = [t for t in dict.fromkeys(keywords(body))
             if _HANGUL_TERM.fullmatch(t) and t[-1] not in _VERB_END and t not in _GRAMMAR_TERMS and t not in words]
    return words, terms


#------------------------------------------------------------------
# 1.7B 에게 같은 뜻인 문서 용어 하나 고르게 하기
#=> 후보에 번호를 매겨 보여 주고 번호 하나만 받는다. 고르지 못하면(0·엉뚱한 답) None — 그 낱말은 후보에서 뺀다.
#   (고른 짝을 예/아니오로 다시 묻는 확인도 해 보았으나 1.7B 가 맞는 짝까지 거의 다 "아니오"라 해 쓰지 않는다 —
#    바꿔 말한 질문 42문항에서 남은 낱말 3개, 정답 짝 0)
#
# -in: llm_complete = (프롬프트, 최대 토큰) → 글 함수
# -in: query, word = 질문 · 넓힐 낱말 / cands = 후보 용어(e5 순서)
#
# -out: 고른 용어 또는 None
# -out: error = 생성 실패는 None (e5 순서로 대신한다)
#------------------------------------------------------------------
def llm_pick(llm_complete, query, word, cands):
    from ..generate.prompts import build_prompt

    user = LLM_USER.format(q=query, w=word, cands="\n".join("{}. {}".format(i + 1, c) for i, c in enumerate(cands)))
    try:
        out = llm_complete(build_prompt(LLM_SYSTEM, user), 6)
    except Exception:
        return None
    m = re.search(r"\d+", out or "")
    k = int(m.group()) if m else 0
    return cands[k - 1] if 1 <= k <= len(cands) else None


#------------------------------------------------------------------
# 후보 제안 (본체)
#=> 놓친 질문마다 검색 후보 10건 → 넓힐 낱말·문서 용어 → e5 유사도 → (있으면) 1.7B 고르기 → 낱말별로 모은다.
#
# -in: retriever    = HybridRetriever (_search · embedder 사용)
# -in: questions    = [(질문, 횟수)]
# -in: llm_complete = (프롬프트, 최대 토큰) → 글. None 이면 e5 순서만 쓴다
# -in: log          = 진행 출력 함수(선택)
#
# -out: dict {updated, n_questions, llm, candidates:[{word, count, examples, terms:[{term, score, llm, docs}]}]}
# -out: error = 검색·임베딩 실패는 예외 전파
#------------------------------------------------------------------
def suggest(retriever, questions, llm_complete=None, log=None):
    import numpy as np

    emb = retriever.embedder
    rejected = synonyms.rejected()
    cache = {}

    # 낱말 벡터(질문 접두) — 같은 낱말은 한 번만 임베딩한다
    def vecs(ws):
        need = [w for w in ws if w not in cache]
        if need:
            for w, v in zip(need, emb.embed(need, emb.spec.query_prefix)):
                cache[w] = np.asarray(v, dtype=np.float32)
        return np.stack([cache[w] for w in ws])

    by_word = {}
    for i, (q, n) in enumerate(questions, 1):
        pool, _ = retriever._search(q, config.RERANK_POOL)
        texts = [c["text"] for c in pool]
        words, terms = words_and_terms(q, texts)
        if log:
            log("[{}/{}] {} — 넓힐 낱말 {}".format(i, len(questions), q[:40], ", ".join(words) or "없음"))
        if not words or not terms:
            continue
        tv = vecs(terms)
        for w in words:
            s = tv @ vecs([w])[0]
            order = [int(k) for k in np.argsort(-s)]
            ranked = [(terms[k], float(s[k])) for k in order if (w, terms[k]) not in rejected]
            pick = llm_pick(llm_complete, q, w, [t for t, _ in ranked[:LLM_CHOICES]]) if llm_complete else None
            if llm_complete and pick is None:
                # 1.7B 가 "같은 뜻 없음"이라 한 낱말은 뺀다 — "받으려면"·"걸로" 같은 질문 말이 후보를 덮지 않게
                continue
            if pick:
                # LLM 이 고른 것을 맨 앞으로 — 나머지는 e5 순서
                ranked = [x for x in ranked if x[0] == pick] + [x for x in ranked if x[0] != pick]
            ent = by_word.setdefault(w, {"word": w, "count": 0, "examples": [], "terms": {}})
            ent["count"] += n
            if q not in ent["examples"] and len(ent["examples"]) < 3:
                ent["examples"].append(q)
            for rank, (t, sc) in enumerate(ranked[:SHOW]):
                docs = [os.path.basename(c.get("doc_name") or "") for c in pool if t in _flat(c["text"])]
                cur = ent["terms"].get(t)
                # 여러 질문에서 나온 같은 짝은 더 좋은 순위·점수를 남기고 표를 더한다
                if cur is None:
                    ent["terms"][t] = {"term": t, "score": round(sc, 3), "llm": t == pick, "votes": 1,
                                       "best_rank": rank, "docs": list(dict.fromkeys(docs))[:2]}
                else:
                    cur["votes"] += 1
                    cur["llm"] = cur["llm"] or t == pick
                    cur["best_rank"] = min(cur["best_rank"], rank)
                    cur["score"] = max(cur["score"], round(sc, 3))
    out = []
    for ent in by_word.values():
        ts = sorted(ent["terms"].values(), key=lambda x: (not x["llm"], x["best_rank"], -x["votes"], -x["score"]))
        out.append({"word": ent["word"], "count": ent["count"], "examples": ent["examples"],
                    "terms": [{k: x[k] for k in ("term", "score", "llm", "docs")} for x in ts[:SHOW]]})
    # 자주 놓친 낱말부터, 같으면 1위 후보 점수가 높은 것부터
    out.sort(key=lambda e: (-e["count"], -(e["terms"][0]["score"] if e["terms"] else 0)))
    return {"updated": datetime.datetime.now().isoformat(timespec="seconds"), "n_questions": len(questions),
            "llm": bool(llm_complete), "candidates": out[:KEEP_WORDS]}


#------------------------------------------------------------------
# 후보 만들기 한 번 (suggest-synonyms 명령 · chat 워커 /suggest-synonyms 가 함께 쓴다)
#=> 기록을 읽어 후보를 만들고 파일로 쓴 뒤, 결과 요약을 돌려준다(요약은 "@index {json}" 한 줄로 내보낸다).
#    1) 기록된 질문이 없으면 실패 요약
#    2) 최근 recent 개만 본다 — chat 워커 안에서는 명령 시간 제한(300초) 안에 끝나게 줄여 부른다
#
# -in: retriever    = 검색기 / llm_complete = (프롬프트, 최대 토큰) → 글, None 이면 e5 순서만
# -in: src = 질문 jsonl(None 이면 데이터 폴더 기록) / out = 결과 경로(None 이면 데이터 폴더)
# -in: recent = 볼 최근 질문 수 / log = 진행 출력 함수(선택)
#
# -out: (요약 dict {op, ok, n_questions, n_words, llm, path | why}, 결과 dict 또는 None)
# -out: error = 없음 (검색·쓰기 실패도 요약의 why 로)
#------------------------------------------------------------------
def run(retriever, llm_complete=None, src=None, out=None, recent=RECENT, log=None):
    src = src or os.path.join(config.ROOT, MISSES_NAME)
    questions = read_misses(src)[-recent:]
    if not questions:
        return {"op": "suggest-synonyms", "ok": False, "why": "기록된 질문이 없습니다: {}".format(src)}, None
    try:
        result = suggest(retriever, questions, llm_complete=llm_complete, log=log)
        path = save(result, out)
    except Exception as e:
        return {"op": "suggest-synonyms", "ok": False, "why": "{}: {}".format(type(e).__name__, str(e)[:200])}, None
    return {"op": "suggest-synonyms", "ok": True, "n_questions": result["n_questions"],
            "n_words": len(result["candidates"]), "llm": result["llm"], "path": path}, result


#------------------------------------------------------------------
# 결과 파일로 쓰기
#
# -in: result = suggest 결과 / path = 쓸 경로(None 이면 <데이터 폴더>/synonym_candidates.json)
#
# -out: 쓴 경로
# -out: error = 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def save(result, path=None):
    path = path or os.path.join(config.ROOT, CANDIDATES_NAME)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    # 승인 화면이 쓰는 도중의 파일을 읽지 않게 다 쓴 뒤 바꿔 넣는다
    os.replace(tmp, path)
    return path
