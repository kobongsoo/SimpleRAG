# -*- coding: utf-8 -*-
"""
답변 모델 비교 — 규칙 지표 채점 (판정 서버 없이)

ragas_golden.py collect 가 모은 responses.json 들을 같은 규칙으로 센다.
  · 핵심어 정답 : 문항의 must 핵심어가 답 안에 **모두** 들었는가(NFKC · 소문자 · 영숫자/한글만 남겨 비교)
  · 회피 답     : "근거에 없어 답할 수 없다" 류로 답을 피했는가
  · 첫 글자 · 답 완료 : 문항별 시간의 중앙값

사용:
  .venv\\Scripts\\python bench\\score_rule.py pdf100_q3_17 pdf100_lfm25 [...]
  (폴더 이름은 results\\ragas_golden\\<이름>\\responses.json)
"""

import json
import os
import re
import statistics
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "results", "ragas_golden")
_KEEP = re.compile(r"[0-9A-Za-z가-힣]+")
# 답을 피한 표현 — 근거가 있어도 모델이 물러선 경우를 센다
_EVADE = re.compile(r"(근거에\s*(없|명시되어 있지 않|나와 있지 않)|답할\s*수\s*없|알\s*수\s*없|확인할\s*수\s*없|찾을\s*수\s*없|제공되지\s*않)")


#------------------------------------------------------------------
# 글 정규화 — 비교용
#=> 전각·반각을 맞추고(NFKC) 소문자로, 영숫자·한글만 이어 붙인다(띄어쓰기·문장부호 차이 무시).
#
# -in: s = 원문(None 가능)
#
# -out: text = 정규화한 글
# -out: error = 없음
#------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(_KEEP.findall(s))


#------------------------------------------------------------------
# 실험 하나 채점
#=> 1) responses.json 을 읽는다
#    2) 문항마다 핵심어 전부 포함 · 회피 표현 여부를 본다
#    3) 시간은 중앙값으로 모은다
#
# -in: name = 실험 폴더 이름
#
# -out: (요약 dict, 문항별 정답 dict{id: bool})
# -out: error = 파일이 없으면 FileNotFoundError 전파
#------------------------------------------------------------------
def score(name):
    with open(os.path.join(RUNS, name, "responses.json"), encoding="utf-8") as f:
        rows = json.load(f)
    ok, ev = {}, 0
    for r in rows:
        ans = norm(r.get("response"))
        must = [norm(m) for m in r.get("must") or [] if norm(m)]
        # 핵심어 목록이 없는 문항(일반어 세트)은 정답을 세지 않는다
        if must:
            ok[r["id"]] = all(m in ans for m in must)
        if _EVADE.search(r.get("response") or ""):
            ev += 1
    med = lambda k: statistics.median([r[k] for r in rows if isinstance(r.get(k), (int, float))] or [0])
    return ({"name": name, "n": len(rows), "correct": sum(ok.values()) if ok else None, "evasive": ev,
             "ttft": round(med("ttft_s"), 2), "llm_ttft": round(med("llm_ttft_s"), 2), "total": round(med("total_s"), 2),
             "model": rows[0].get("model") if rows else ""}, ok)


#------------------------------------------------------------------
# 진입점 — 실험마다 한 줄, 첫 실험과 짝지은 승/패
#
# -in: 없음 (명령줄의 실험 폴더 이름들)
#
# -out: 없음 (표준 출력)
# -out: error = 인자가 없으면 SystemExit
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    names = sys.argv[1:]
    if not names:
        raise SystemExit(__doc__)
    base, base_ok = score(names[0])
    print("실험 | 모델 | 문항 | 핵심어 정답 | 회피 표현 | 모델 첫 글자(중앙) | 화면 첫 글자(중앙) | 답 완료(중앙) | 첫 실험 대비 얻음/잃음")
    for n in names:
        s, ok = score(n)
        # 같은 문항끼리 — 이 실험만 맞힌 수 / 첫 실험만 맞힌 수
        win = sum(1 for k, v in ok.items() if v and not base_ok.get(k, False))
        lose = sum(1 for k, v in base_ok.items() if v and not ok.get(k, False))
        # 핵심어 목록이 없는 세트(일반어)는 정답 · 얻음/잃음을 비운다
        wl = f"+{win}/-{lose}" if ok and base_ok else "—"
        print(f"{n} | {s['model']} | {s['n']} | {s['correct'] if s['correct'] is not None else '—'} | {s['evasive']} | "
              f"{s['llm_ttft']}초 | {s['ttft']}초 | {s['total']}초 | {wl}")


if __name__ == "__main__":
    main()
