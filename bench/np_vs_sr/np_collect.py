"""Newploy AI Desktop 의 AI 채팅에 골든셋 문항을 실제로 질의해 답변·검색 근거를 JSON 으로 기록한다 (채점 안 함).

앱은 --remote-debugging-port=9333 으로 떠 있어야 한다. 화면 조작은 Playwright(CDP),
답변·근거 원문은 앱이 저장하는 대화 기록(poc_chat/convos/*.json)에서 읽는다.
사용: python np_collect.py <golden.jsonl> <out.json> [N|a-b]
"""
import glob
import io
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

BASE = r"C:\Users\bong9\AppData\Local\Newploy\AI Desktop"
CONVOS = os.path.join(BASE, "poc_chat", "convos")
CDP = "http://127.0.0.1:9333"
STATUS = ("관련 근거를 검색 중", "생각 중입니다", "답변 내용을 정리 중")
JS_LAST = """() => { const a=[...document.querySelectorAll('article.np-poc-chat__msg--assistant')]; const l=a[a.length-1];
    return {n:a.length, text:l? l.innerText:'', done: !!(l && l.querySelector('.np-poc-chat__membtn'))}; }"""


#------------------------------------------------------------------
# JSON 저장 (중간 결과 보존)
#=> 도중에 끊겨도 그때까지 문항이 남도록 임시 파일에 쓴 뒤 바꿔치기한다.
#
# -in: path = 저장 경로
# -in: obj  = 저장할 객체
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def save_json(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


#------------------------------------------------------------------
# 앱 라이브러리 파일 id → 원본 파일명
#=> 근거 키(ocr:<파일id>:<쪽 index>)를 사람이 읽는 파일명으로 바꾸려고 library.json 을 읽는다.
#
# -in: 없음
#
# -out: {파일id: 파일명}
# -out: error = 파일 없으면 예외 전파
#------------------------------------------------------------------
def library_names():
    lib = json.load(io.open(os.path.join(BASE, "library", "library.json"), encoding="utf-8"))
    return {f["id"]: os.path.basename(f["sourcePath"]) for f in lib["files"]}


#------------------------------------------------------------------
# 새 대화방 열기 + 웹 검색 끄기
#=> 문항끼리 대화 이력이 섞이지 않게 매번 새 방을 만들고, 질문이 외부로 나가지 않게 웹 검색을 끈다.
#
# -in: page = 앱 페이지
#
# -out: 없음
# -out: error = 버튼을 못 찾으면 playwright 예외
#------------------------------------------------------------------
def new_room(page):
    page.locator("button[title='새 대화']").first.click()
    page.wait_for_timeout(800)
    sw = page.locator("input.np-poc-chat__webswitch-input").first
    if sw.count() and sw.is_checked():
        sw.evaluate("e => e.click()")
        page.wait_for_timeout(400)


#------------------------------------------------------------------
# 질문 1건 보내고 완료까지 기다리기
#=> Enter 후 0.1초마다 마지막 답변 말풍선을 읽어, '기억 저장' 버튼이 붙으면 완료로 본다.
#
# -in: page    = 앱 페이지
# -in: q       = 질문
# -in: limit_s = 최대 대기 초 (기본 300)
#
# -out: dict = sent_at(epoch), done_s, ui_text, timeout
# -out: error = 입력창을 못 찾으면 playwright 예외
#------------------------------------------------------------------
def ask(page, q, limit_s=300):
    n0 = page.evaluate(JS_LAST)["n"]
    ta = page.locator("textarea").first
    ta.fill(q)
    sent = time.time()
    t0 = time.perf_counter()
    ta.press("Enter")
    search_end_s = first_s = None
    while True:
        s = page.evaluate(JS_LAST)
        now = time.perf_counter() - t0
        txt = s["text"].strip() if s["n"] > n0 else ""
        # 화면은 '검색 중 → 생각 중 → 정리 중' 상태문구를 먼저 보여 준다 — 검색 문구가 사라진 때 = 검색 끝
        if txt and search_end_s is None and "검색 중" not in txt:
            search_end_s = now
        # 상태문구가 아닌 본문 글자가 처음 보인 때 = 화면 기준 첫 글자(SimpleRAG ttft_s 와 같은 정의)
        if txt and first_s is None and not txt.startswith(STATUS):
            first_s = now
        if s["n"] > n0 and s["done"]:
            return {"sent_at": sent, "done_s": round(now, 2), "ui_text": s["text"], "timeout": False,
                    "screen_ttft_s": None if first_s is None else round(first_s, 2),
                    "search_end_s": None if search_end_s is None else round(search_end_s, 2)}
        if now > limit_s:
            return {"sent_at": sent, "done_s": round(now, 2), "ui_text": s["text"], "timeout": True,
                    "screen_ttft_s": None if first_s is None else round(first_s, 2),
                    "search_end_s": None if search_end_s is None else round(search_end_s, 2)}
        time.sleep(0.05)


#------------------------------------------------------------------
# 방금 질문한 대화 기록 찾기
#=> 보낸 시각 이후 만들어진 대화방 중 첫 질문이 같은 것을 고른다(앱이 저장을 마칠 때까지 잠깐 재시도).
#
# -in: q    = 질문
# -in: sent = 보낸 시각(epoch)
#
# -out: (user, assistant) 메시지 dict / 못 찾으면 (None, None)
# -out: error = 예외 없음
#------------------------------------------------------------------
def find_convo(q, sent):
    for _ in range(30):
        best = None
        for f in glob.glob(os.path.join(CONVOS, "*.json")):
            if os.path.getmtime(f) < sent - 5:
                continue
            try:
                j = json.load(io.open(f, encoding="utf-8"))
            except ValueError:
                continue
            user = next((m for m in j["messages"] if m["role"] == "user"), None)
            bot = next((m for m in j["messages"] if m["role"] == "assistant"), None)
            if user and bot and user["content"].strip() == q.strip():
                if best is None or j["createdAt"] > best[0]:
                    best = (j["createdAt"], user, bot)
        if best:
            return best[1], best[2]
        time.sleep(0.5)
    return None, None


#------------------------------------------------------------------
# 답변 본문 정리
#=> 화면/기록의 답변 끝에 붙는 "[참조 원문: …]"·"출처:" 꼬리는 답변 내용이 아니라서 떼어 낸다
#   (원문은 response_raw 로 따로 남긴다).
#
# -in: text = 답변 원문
#
# -out: 정리된 답변
# -out: error = 없음
#------------------------------------------------------------------
def strip_tail(text):
    t = re.split(r"\n+>?\s*출처\s*:", text)[0]
    t = re.sub(r"\n*\[참조 원문[^\]]*\]\s*$", "", t.strip())
    return t.strip()


#------------------------------------------------------------------
# 화면 꼬리 정보 파싱
#=> 답변 아래 앱이 찍는 "참조 원문 N페이지 · 검색어 "…"" 와 "TTFT …s · … tok/s · … tokens" 를 꺼낸다.
#
# -in: ui = 답변 말풍선 innerText
#
# -out: dict = search_query, ui_ttft_s, tok_s, tokens (없으면 None)
# -out: error = 없음
#------------------------------------------------------------------
def ui_meta(ui):
    m_q = re.search(r'검색어\s*"([^"]*)"', ui)
    m_t = re.search(r"TTFT\s*([\d.]+)s\s*·\s*([\d.]+)\s*tok/s\s*·\s*(\d+)\s*tokens", ui)
    return {"search_query": m_q.group(1) if m_q else None,
            "ui_ttft_s": float(m_t.group(1)) if m_t else None,
            "tok_s": float(m_t.group(2)) if m_t else None,
            "tokens": int(m_t.group(3)) if m_t else None}


#------------------------------------------------------------------
# 수집 진입점
#=> AI 채팅 화면으로 가서 문항마다 새 방 → 질문 → 완료 대기 → 대화 기록에서 답변·근거를 꺼내 저장한다.
#    1) 첫 질문은 모델 예열이 섞이므로 '안녕하세요' 로 예열만 하고 기록하지 않는다
#    2) 근거 키(ocr:<파일id>:<N>)의 N 을 그대로 PDF 쪽번호(page)로 남긴다(1부터 셈)
#    3) 이미 저장된 id 는 건너뛴다(이어하기)
#
# -in: 없음 (argv: 골든셋, 출력, 범위)
#
# -out: 없음 (출력 JSON)
# -out: error = 앱 연결 실패 시 playwright 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    golden = [json.loads(l) for l in io.open(sys.argv[1], encoding="utf-8") if l.strip()]
    out_path = sys.argv[2]
    rng = sys.argv[3] if len(sys.argv) > 3 else "all"
    if rng != "all":
        a, b = (rng.split("-") + [None])[:2]
        golden = golden[int(a) - 1:int(b)] if b else golden[:int(a)]
    doc = json.load(io.open(out_path, encoding="utf-8")) if os.path.exists(out_path) else None
    names = library_names()
    with sync_playwright() as pw:
        page = pw.chromium.connect_over_cdp(CDP).contexts[0].pages[0]
        page.locator("button:has-text('AI 채팅')").first.click()
        page.wait_for_timeout(1000)
        if doc is None:
            new_room(page)
            warm = ask(page, "안녕하세요")
            tier = json.load(io.open(os.path.join(BASE, "config", "ai_chat_model.json"), encoding="utf-8"))
            doc = {"product": "Newploy AI Desktop", "settings": {"tier": tier.get("tier"), "web_search": False,
                                                                 "new_room_per_question": True},
                   "warmup_s": warm["done_s"], "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": []}
        done_ids = {r["id"] for r in doc["items"]}
        for i, g in enumerate(golden, 1):
            if g["id"] in done_ids:
                continue
            new_room(page)
            r = ask(page, g["user_input"])
            user, bot = find_convo(g["user_input"], r["sent_at"])
            raw = bot["content"] if bot else r["ui_text"]
            ev = (bot or {}).get("evidence") or []
            meta = []
            for e in ev:
                parts = str(e.get("key", "")).split(":")
                fid = parts[1] if len(parts) == 3 else None
                pidx = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
                # 키의 숫자는 이미 1부터 센 쪽번호다(1쪽짜리 문서가 ':1') — 화면 라벨 p.N 은 1 크게 찍힌다
                meta.append({"doc": names.get(fid, fid), "page": pidx,
                             "key": e.get("key"), "label": e.get("label")})
            docs = [m["doc"] for m in meta]
            doc["items"].append({
                **{k: g[k] for k in ("id", "user_input", "reference", "reference_contexts", "reference_pages",
                                     "doc_ids", "source_files", "question_type", "difficulty", "must")},
                "response": strip_tail(raw),
                "response_raw": raw,
                "retrieved_contexts": [e.get("text", "") for e in ev],
                "retrieved_meta": meta,
                "doc_hit": any(d in g["source_files"] for d in docs),
                "page_hit": any(m["doc"] in g["source_files"] and m["page"] in
                                {p["page"] for p in g["reference_pages"]} for m in meta),
                # ttft_s = 질문→화면 첫 본문 글자(SimpleRAG ttft_s 와 같은 정의) / ui_ttft_s·tok_s·tokens = 앱이 찍는 LLM 기준 값
                "timing": {"ttft_s": r["screen_ttft_s"], "search_end_s": r["search_end_s"],
                           "total_s": r["done_s"], **ui_meta(r["ui_text"])},
                "timeout": r["timeout"], "convo_found": bot is not None,
            })
            save_json(out_path, doc)
            it = doc["items"][-1]
            tm = it["timing"]
            print("  [{:>3}] {}{} TTFT {} tok/s {} total {:.1f}s ctx{} {}".format(
                i, "R" if it["doc_hit"] else "-", "P" if it["page_hit"] else "-", tm["ttft_s"], tm["tok_s"],
                r["done_s"], len(ev), g["user_input"][:30]), flush=True)
            time.sleep(1)
    doc["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(out_path, doc)
    print("[NP] 저장:", out_path)


if __name__ == "__main__":
    main()
