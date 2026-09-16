#------------------------------------------------------------------
# 골든셋 RAGAS 최종 결과 합치기·출력
#=> bench/ragas_golden.py score 결과 두 벌을 합쳐 최종 4지표를 만든다(REPORT §36 기록용).
#    - F / AR / CR : summary_nothink (thinking 끔, NaN 재채점 후)
#    - CP          : summary_think_cp (thinking 켬) — thinking 끔 CP 는 비교용으로 함께 보인다
#   합친 결과는 results/ragas_golden/final_items.json · final_summary.json 에 저장한다.
#
#   사용: python bench/ragas_golden_report.py
#------------------------------------------------------------------

import io
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import ragas_golden as rg  # noqa: E402

CP = "llm_context_precision_with_reference"
SHORT = {"faithfulness": "F", "answer_relevancy": "AR", CP: "CP", "context_recall": "CR"}


#------------------------------------------------------------------
# JSON 읽기 (없으면 None)
#=> 한쪽 채점만 끝났어도 있는 것으로 진행 여부를 판단하게 한다.
#
# -in: name = results/ragas_golden 안 파일명
#
# -out: 객체 또는 None
# -out: error = JSON 이 깨졌으면 ValueError 전파
#------------------------------------------------------------------
def load(name):
    p = os.path.join(rg.OUT_DIR, name)
    return json.load(io.open(p, encoding="utf-8")) if os.path.exists(p) else None


#------------------------------------------------------------------
# 점수 칸 문자열
#=> None 은 '-', 유효 개수가 전체보다 적으면 (유효/전체) 를 붙인다.
#
# -in: row = summarize 결과 한 행, col = 지표 열 이름
#
# -out: 표에 넣을 문자열
# -out: error = 예외 없음
#------------------------------------------------------------------
def cell(row, col):
    v = row.get(col)
    if v is None:
        return "-"
    s = "%.3f" % v
    if row.get(col + "_valid", row["n"]) < row["n"]:
        s += " (%d/%d)" % (row[col + "_valid"], row["n"])
    return s


#------------------------------------------------------------------
# 요약 표 출력
#=> 최종 요약의 그룹별 행에 thinking 끔 CP(비교용) 칸을 덧붙여 마크다운 표로 찍는다.
#
# -in: title = 표 제목, final = 최종 요약 그룹 dict, off = thinking 끔 요약 그룹 dict
#
# -out: 없음 (stdout)
# -out: error = 예외 없음
#------------------------------------------------------------------
def table(title, final, off):
    print("\n### " + title)
    print("| 그룹 | n | F | AR | CP | CR | 4지표 평균 | (참고) CP thinking 끔 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for g, row in final.items():
        print("| %s | %d | %s | %s | %s | %s | %s | %s |" % (
            g, row["n"], cell(row, "faithfulness"), cell(row, "answer_relevancy"), cell(row, CP),
            cell(row, "context_recall"), "-" if row["mean4"] is None else "%.3f" % row["mean4"],
            cell(off.get(g, {"n": 0}), CP)))


#------------------------------------------------------------------
# 최종 결과 합치기 본체
#=> thinking 끔 결과(F·AR·CR)에 thinking 켬 CP 를 끼워 최종 파일을 만들고 표를 찍는다.
#
# -in: 없음
#
# -out: 0 = 성공, 1 = 필요한 파일 없음
# -out: error = JSON 이 깨졌으면 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    off_items, cp_items = load("scores_items_nothink.json"), load("scores_items_think_cp.json")
    off_sum, cp_sum = load("summary_nothink.json"), load("summary_think_cp.json")
    if not (off_items and cp_items):
        print("필요 파일 없음 — scores_items_nothink.json / scores_items_think_cp.json")
        return 1

    cp_by_id = {r["id"]: r["scores"][CP] for r in cp_items}
    final = []
    for r in off_items:
        sc = dict(r["scores"])
        sc["cp_nothink"] = sc[CP]
        # CP 만 thinking 켬 판정으로 바꾼다
        sc[CP] = cp_by_id.get(r["id"])
        final.append(dict(r, scores=sc))

    summary = {
        "judge": {"model": off_sum["judge"]["model"], "url": off_sum["judge"]["url"],
                  "thinking": {"faithfulness": False, "answer_relevancy": False, "context_recall": False, CP: True}},
        "embeddings": off_sum["embeddings"], "ragas_version": off_sum["ragas_version"], "n": len(final),
        "score_sec": {"nothink_f_ar_cp_cr": off_sum["score_sec"], "think_cp": cp_sum["score_sec"]},
        "overall": rg.summarize(final)["all"],
        "by_type": rg.summarize(final, "question_type"),
        "by_difficulty": rg.summarize(final, "difficulty"),
        "by_doc": rg.summarize(final, "doc_id"),
    }
    rg.save_json(os.path.join(rg.OUT_DIR, "final_items.json"), final)
    rg.save_json(os.path.join(rg.OUT_DIR, "final_summary.json"), summary)

    print("판정 %s / 임베딩 %s / ragas %s / %d문항 / 채점 %.0f초 + %.0f초" % (
        summary["judge"]["model"], summary["embeddings"]["model"], summary["ragas_version"], len(final),
        off_sum["score_sec"], cp_sum["score_sec"]))
    table("전체", {"전체": summary["overall"]}, {"전체": off_sum["overall"]})
    table("질문 유형별", summary["by_type"], off_sum["by_type"])
    table("난이도별", summary["by_difficulty"], off_sum["by_difficulty"])
    table("문서별", summary["by_doc"], off_sum["by_doc"])

    print("\n### 지표별 최저 문항")
    for col, lab in SHORT.items():
        worst = sorted((r for r in final if r["scores"][col] is not None), key=lambda r: r["scores"][col])[:6]
        print("- %s: " % lab + ", ".join("%s(%s) %.2f" % (r["id"], r["question_type"], r["scores"][col]) for r in worst))
        nan = [r["id"] for r in final if r["scores"][col] is None]
        if nan:
            print("  - 채점 실패(NaN): " + ", ".join(nan))

    print("\n### CP thinking 켬/끔 차이 큰 문항")
    diff = sorted(final, key=lambda r: -abs((r["scores"][CP] or 0) - (r["scores"]["cp_nothink"] or 0)))[:8]
    for r in diff:
        print("- %s (%s): 켬 %s / 끔 %s" % (r["id"], r["question_type"], r["scores"][CP], r["scores"]["cp_nothink"]))
    zero = lambda k: sum(1 for r in final if r["scores"][k] == 0)  # noqa: E731
    print("- CP 0점 문항 수: 켬 %d / 끔 %d" % (zero(CP), zero("cp_nothink")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
