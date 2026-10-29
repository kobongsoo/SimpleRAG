#------------------------------------------------------------------
# 메타 질의 골든셋 시험 (메타데이터 검색 설계서 §16-1 ④)
#=> "기억나는 문서 하나 찾기" 질의 30개(bench/meta_golden.jsonl)를 탐색기 모드 찾기(/find)로 돌려
#   정답 문서가 몇 번째에 나오는지 잰다. 메타데이터 조건을 켠 것과 끈 것(SIMPLERAG_META_QUERY=0)을 나란히.
#    1) simplerag-rs chat 워커를 띄우고 리랭커 적재가 끝날 때까지 기다린다(탐색기 화면과 같은 조건)
#    2) 질의마다 "/find {json}" → "@find" 문서 줄 순서에서 정답(경로에 정답 글이 든 문서)의 순위
#    3) Top-1 · Top-5 · Top-10 포함률 · MRR · 질의별 순위를 출력하고 JSON 으로 남긴다
#   정답은 문서 이름 일부로 적는다(같은 문서의 사본 · 버전도 정답으로 본다).
#
#   사용: .venv\Scripts\python.exe bench\test_meta_golden.py [--exe dist-rust\simplerag-rs.exe] [--only on|off]
#   ⚠️ 그 배포 폴더의 RAGSearchBox 가 떠 있으면 인덱스 잠금 때문에 워커가 뜨지 않는다 — 먼저 끈다.
#------------------------------------------------------------------

import argparse
import json
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
GOLDEN = os.path.join(_HERE, "meta_golden.jsonl")
OUT_DIR = os.path.join(_ROOT, "results", "meta_golden")


#------------------------------------------------------------------
# 상주 워커 — simplerag-rs chat 을 띄워 /find 를 한 줄씩 보낸다
# -필드: p = 프로세스 / lines = stdout 줄 큐 / err = stderr 줄 목록
#------------------------------------------------------------------
class Worker:
    #------------------------------------------------------------------
    # 워커 띄우기 · 리랭커 적재 기다리기
    #
    # -in: exe = simplerag-rs.exe 경로 / meta = False 면 SIMPLERAG_META_QUERY=0
    #
    # -out: 없음
    # -out: error = 리랭커가 300초 안에 안 올라오면 RuntimeError
    #------------------------------------------------------------------
    def __init__(self, exe, meta):
        env = dict(os.environ)
        if not meta:
            env["SIMPLERAG_META_QUERY"] = "0"
        # 배포 폴더를 작업 폴더로 — config.yaml · 인덱스 · meta.sqlite 를 그 자리에서 읽는다
        self.p = subprocess.Popen([exe, "chat"], cwd=os.path.dirname(os.path.abspath(exe)), env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  encoding="utf-8", errors="replace", bufsize=1)
        self.lines, self.err = [], []
        self.cv = threading.Condition()
        threading.Thread(target=self._pump, args=(self.p.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.p.stderr, self.err), daemon=True).start()
        t0 = time.time()
        # 리랭커는 준비 뒤 백그라운드로 올라온다 — 그 전에 찾으면 리랭킹 없이 돌아 결과가 달라진다
        while not any("리랭커 적재" in l for l in self.err):
            if time.time() - t0 > 300 or self.p.poll() is not None:
                raise RuntimeError("워커 준비 실패: " + " / ".join(self.err[-5:]))
            time.sleep(0.2)

    # 줄 읽기 스레드 — 받은 줄을 목록에 넣고 기다리는 쪽을 깨운다
    def _pump(self, f, out):
        for line in f:
            line = line.rstrip("\r\n")
            # 워커는 입력 안내 "질문> " 뒤에 바로 결과 줄을 잇는다 — 안내를 떼야 첫 줄 · 마침 줄을 놓치지 않는다
            if "@find" in line:
                line = line[line.index("@find"):]
            with self.cv:
                out.append(line)
                self.cv.notify_all()

    #------------------------------------------------------------------
    # 찾기 한 번
    #
    # -in: q = 질의
    #
    # -out: (문서 줄 목록, 마침 줄 dict)
    # -out: error = 120초 안에 마침 줄이 없으면 RuntimeError
    #------------------------------------------------------------------
    def find(self, q):
        with self.cv:
            start = len(self.lines)
        self.p.stdin.write("/find " + json.dumps({"q": q}, ensure_ascii=True) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + 120
        with self.cv:
            while True:
                new = self.lines[start:]
                done = next((l for l in new if l.startswith("@find_done ")), None)
                if done:
                    break
                if time.time() > deadline:
                    raise RuntimeError("찾기 시간 초과: " + q)
                self.cv.wait(1)
        docs = [json.loads(l[6:]) for l in new if l.startswith("@find ")]
        return [d for d in docs if "page" not in d], json.loads(done[11:])

    # 워커 끝내기
    def close(self):
        try:
            self.p.stdin.write("exit\n")
            self.p.stdin.flush()
            self.p.wait(30)
        except Exception:  # noqa: BLE001 — 끝내기 실패는 강제로
            self.p.kill()


#------------------------------------------------------------------
# 정답 순위 — 탐색기 화면에 보이는 순서(1부터)에서 정답 글이 경로에 든 첫 문서
#=> 화면(browse.html)은 "조건 일치" → "파일 이름 일치" → "내용 일치" 묶음 순으로 그린다.
#   워커가 내보내는 줄 순서(내용 → 이름)와 다르므로 화면 순서로 다시 세운다.
#
# -in: docs = 문서 줄 목록 / answers = 정답 글 목록
#
# -out: 순위(int) / 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def rank_of(docs, answers):
    keys = [a.lower() for a in answers]
    by_meta = [d for d in docs if d.get("meta_hit") and not d.get("hits") and not d.get("name_hit")]
    by_name = [d for d in docs if d.get("name_hit")]
    content = [d for d in docs if d.get("hits") and not d.get("name_hit")]
    docs = by_meta + by_name + content
    for i, d in enumerate(docs, 1):
        p = (d.get("doc_path") or "").lower()
        if any(k in p for k in keys):
            return i
    return None


#------------------------------------------------------------------
# 한 판 돌리기 — 골든셋 전부를 한 워커로
#
# -in: exe = 워커 경로 / meta = 메타 조건 켬 여부 / items = 골든셋
#
# -out: [{id, kind, q, rank, chips, relaxed, ms}, …]
# -out: error = 워커 실패는 예외 전파
#------------------------------------------------------------------
def run(exe, meta, items):
    w = Worker(exe, meta)
    out = []
    try:
        for it in items:
            docs, done = w.find(it["q"])
            m = done.get("meta") or {}
            r = rank_of(docs, it["answers"])
            out.append({"id": it["id"], "kind": it["kind"], "q": it["q"], "rank": r, "ms": done.get("ms"),
                        "chips": [c["label"] for c in m.get("chips", [])], "relaxed": m.get("relaxed", []),
                        "top3": [d.get("doc_name") for d in docs[:3]]})
            print("  {:<4} {:>4}  {}  {}".format(it["id"], r or "-", it["q"], " · ".join(out[-1]["chips"])), flush=True)
    finally:
        w.close()
    return out


#------------------------------------------------------------------
# 요약 — Top-1 · 5 · 10 포함률과 MRR(10위 밖은 0)
#
# -in: res = run() 결과
#
# -out: dict
# -out: error = 없음
#------------------------------------------------------------------
def summary(res):
    n = len(res)
    hit = lambda k: sum(1 for r in res if r["rank"] and r["rank"] <= k)  # noqa: E731 — 짧은 셈
    mrr = sum(1.0 / r["rank"] for r in res if r["rank"] and r["rank"] <= 10) / n
    return {"n": n, "top1": hit(1), "top5": hit(5), "top10": hit(10), "mrr": round(mrr, 3),
            "ms_avg": round(sum(r["ms"] or 0 for r in res) / n)}


#------------------------------------------------------------------
# 실행
#
# -in: 없음(명령줄 인자)
#
# -out: 0
# -out: error = 워커 실패는 예외 전파
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=os.path.join(_ROOT, "dist-rust", "simplerag-rs.exe"))
    ap.add_argument("--only", choices=["on", "off"])
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    items = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    os.makedirs(OUT_DIR, exist_ok=True)
    result = {}
    for mode in ("on", "off"):
        if a.only and a.only != mode:
            continue
        print("── 메타 조건 {} ({}문항)".format("켬" if mode == "on" else "끔", len(items)), flush=True)
        res = run(a.exe, mode == "on", items)
        result[mode] = {"summary": summary(res), "items": res}
        print("   →", result[mode]["summary"], flush=True)
    path = os.path.join(OUT_DIR, "meta_golden{}_{}.json".format("_" + a.tag if a.tag else "", time.strftime("%Y%m%d_%H%M")))
    json.dump(result, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("저장:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
