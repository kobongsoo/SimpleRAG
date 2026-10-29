#------------------------------------------------------------------
# 유형 · 양식 판별 시험 (메타데이터 검색 설계서 §16-1 ③)
#=> 사람이 정답을 단 문서 100개(bench/meta_type_golden.jsonl — 13_샘플_템플릿 · 08_사내규정_인사)와
#   meta.sqlite 의 판정(doc_type · is_form)을 비교해 정밀도 · 재현율을 낸다(합격: 정밀도 85% · 재현율 70%).
#    1) 유형: 판정이 있는 문서 중 정답과 같은 비율(정밀도), 정답 유형이 있는 문서 중 맞힌 비율(재현율)
#    2) 양식: 참으로 판정한 문서 중 진짜 양식 비율(정밀도), 진짜 양식 중 찾은 비율(재현율)
#    3) 틀린 문서 목록 — 규칙을 고칠 때 본다
#   메뉴얼 · 매뉴얼은 같은 유형으로 본다.
#
#   사용: .venv\Scripts\python.exe bench\test_meta_type.py [--db dist-rust\meta.sqlite] [--html 보고서.html]
#------------------------------------------------------------------

import argparse
import html
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
GOLDEN = os.path.join(_HERE, "meta_type_golden.jsonl")


# 유형 이름 맞추기 — 메뉴얼은 매뉴얼로
def _norm(t):
    return "매뉴얼" if t == "메뉴얼" else (t or None)


#------------------------------------------------------------------
# 정답과 판정 비교
#
# -in: db = meta.sqlite 경로
#
# -out: (요약 dict, 문서별 행 목록)
# -out: error = DB 를 못 열면 sqlite3 예외 전파
#------------------------------------------------------------------
def evaluate(db):
    gold = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    c = sqlite3.connect(db)
    rows = []
    for g in gold:
        r = c.execute("SELECT doc_type, doc_type_score, is_form, form_score, title_display FROM doc_meta WHERE doc_path = ?",
                      (g["path"],)).fetchone()
        pt, ps, pf, fs, title = r if r else (None, None, 0, None, None)
        rows.append({"no": g["no"], "path": g["path"], "name": os.path.basename(g["path"]), "title": title,
                     "gold_type": _norm(g["type"]), "gold_form": bool(g["form"]), "note": g.get("note", ""),
                     "pred_type": _norm(pt), "type_score": ps, "pred_form": bool(pf), "form_score": fs})
    # 유형 — 판정이 있는 것 중 맞은 것 / 정답 유형이 있는 것 중 맞은 것
    pred = [r for r in rows if r["pred_type"]]
    has = [r for r in rows if r["gold_type"]]
    ok = [r for r in rows if r["pred_type"] and r["pred_type"] == r["gold_type"]]
    # 양식
    pf = [r for r in rows if r["pred_form"]]
    gf = [r for r in rows if r["gold_form"]]
    tp = [r for r in rows if r["pred_form"] and r["gold_form"]]
    pct = lambda a, b: round(100.0 * a / b, 1) if b else None  # noqa: E731 — 짧은 비율
    s = {"n": len(rows),
         "type_precision": pct(len(ok), len(pred)), "type_recall": pct(len(ok), len(has)),
         "type_pred": len(pred), "type_gold": len(has), "type_ok": len(ok),
         "form_precision": pct(len(tp), len(pf)), "form_recall": pct(len(tp), len(gf)),
         "form_pred": len(pf), "form_gold": len(gf), "form_ok": len(tp)}
    return s, rows


#------------------------------------------------------------------
# 검토용 HTML — 정답 초안과 지금 판정을 나란히(다른 칸은 강조)
#
# -in: path = 저장 경로 / s · rows = evaluate() 결과
#
# -out: 없음
# -out: error = 쓰기 실패는 예외 전파
#------------------------------------------------------------------
def write_html(path, s, rows):
    tr = []
    for r in rows:
        tdiff = (r["pred_type"] or "") != (r["gold_type"] or "")
        fdiff = r["pred_form"] != r["gold_form"]
        tr.append("<tr><td>{}</td><td title='{}'>{}</td><td class='g'>{}</td><td class='g'>{}</td><td{}>{}</td><td{}>{}</td><td class='m'>{}</td></tr>".format(
            r["no"], html.escape(r["path"]), html.escape(r["name"]), html.escape(r["gold_type"] or "—"),
            "양식" if r["gold_form"] else "—",
            " class='d'" if tdiff else "", html.escape("{} ({})".format(r["pred_type"], r["type_score"]) if r["pred_type"] else "—"),
            " class='d'" if fdiff else "", "양식" if r["pred_form"] else "—", html.escape(r["note"])))
    doc = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>유형 · 양식 정답 초안 검토</title>
<style>body{{font-family:"Malgun Gothic",sans-serif;margin:24px;color:#1f2937}} table{{border-collapse:collapse;font-size:13px}}
td,th{{border:1px solid #d7dbe3;padding:4px 8px;text-align:left}} th{{background:#f3f4f6}} .g{{background:#f0fdf4;font-weight:bold}}
.d{{background:#fee2e2}} .m{{color:#5b6475}} .sum{{margin:12px 0;line-height:1.7}}</style></head><body>
<h2>시험 ③ 유형 · 양식 — 정답 초안 검토 (문서 {n}개)</h2>
<div class="sum">초록 칸 = <b>정답 초안</b>(Claude 가 문서 이름 · 본문 첫머리를 읽고 단 것 — 검토해 주세요) · 빨간 칸 = 지금 규칙 판정이 초안과 다름<br>
양식 = 빈칸을 채워 쓰는 <b>빈 서식</b>(채워 쓴 작성본은 양식 아님) · 유형 칸의 괄호 = 규칙 점수<br>
지금 규칙: 유형 정밀도 <b>{tp}%</b> ({tok}/{tpred}) · 재현율 <b>{tr}%</b> ({tok}/{tgold}) / 양식 정밀도 <b>{fp}%</b> ({fok}/{fpred}) · 재현율 <b>{fr}%</b> ({fok}/{fgold}) — 합격 정밀도 85% · 재현율 70%</div>
<table><tr><th>#</th><th>문서</th><th>정답 유형</th><th>정답 양식</th><th>지금 판정 유형</th><th>지금 판정 양식</th><th>메모</th></tr>
{rows}</table></body></html>""".format(n=s["n"], tp=s["type_precision"], tr=s["type_recall"], tok=s["type_ok"], tpred=s["type_pred"],
                                        tgold=s["type_gold"], fp=s["form_precision"], fr=s["form_recall"], fok=s["form_ok"],
                                        fpred=s["form_pred"], fgold=s["form_gold"], rows="\n".join(tr))
    open(path, "w", encoding="utf-8").write(doc)


#------------------------------------------------------------------
# 실행
#
# -in: 없음(명령줄 인자)
#
# -out: 0
# -out: error = 없음
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_ROOT, "dist-rust", "meta.sqlite"))
    ap.add_argument("--html")
    a = ap.parse_args()
    s, rows = evaluate(a.db)
    print("유형: 정밀도 {}% ({}/{}) · 재현율 {}% ({}/{})".format(s["type_precision"], s["type_ok"], s["type_pred"],
                                                            s["type_recall"], s["type_ok"], s["type_gold"]))
    print("양식: 정밀도 {}% ({}/{}) · 재현율 {}% ({}/{})".format(s["form_precision"], s["form_ok"], s["form_pred"],
                                                            s["form_recall"], s["form_ok"], s["form_gold"]))
    for r in rows:
        if (r["pred_type"] or "") != (r["gold_type"] or "") or r["pred_form"] != r["gold_form"]:
            print("  {:>3} {}  정답 {}/{}  판정 {}({})/{}".format(r["no"], r["name"][:40], r["gold_type"], int(r["gold_form"]),
                                                          r["pred_type"], r["type_score"], int(r["pred_form"])))
    if a.html:
        write_html(a.html, s, rows)
        print("검토 화면:", a.html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
