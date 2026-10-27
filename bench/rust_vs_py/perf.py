"""Rust 판(simplerag-rs.exe) vs Python 판(simplerag.exe) 실측 — 인덱싱 · 예열 · 골든셋 질의 · 메모리 · 용량.

두 배포본을 같은 조건(같은 config.yaml · 같은 모델 파일 · 새 데이터 폴더)으로 차례로 돌린다. 동시에 돌리지 않는다.
사용: python bench/rust_vs_py/perf.py <작업 폴더> [index|warmup|chat|size|all]
결과: results/rust_vs_py/perf.json (단계마다 갱신), chat_<판>.json (문항별 기록)
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import psutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(ROOT, "results", "rust_vs_py")
GOLDEN = os.path.join(ROOT, "dataset", "golden_pdf30.jsonl")
DOCS = os.path.join(ROOT, "sample_pdf")
CONFIG = os.path.join(ROOT, "config.yaml")      # 10/23 기준선(수정 전 수집)과 같은 설정
PRODUCTS = {
    "python": {"dist": os.path.join(ROOT, "dist", "simplerag"), "exe": "simplerag.exe", "stdin": "cp949"},
    "rust": {"dist": os.path.join(ROOT, "dist-rust"), "exe": "simplerag-rs.exe", "stdin": "utf-8"},
}
PROMPT = "질문> "


#------------------------------------------------------------------
# JSON 저장 (중간 결과 보존)
#=> 단계가 오래 걸리므로 끊겨도 앞 단계 결과가 남게 임시 파일에 쓴 뒤 바꿔치기한다.
#
# -in: path = 저장 경로 / obj = 객체
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
# 판별 실행 환경 만들기
#=> 데이터 폴더(SIMPLERAG_HOME)는 작업 폴더 아래 새로 두고, 모델은 각 배포본의 것을 가리킨다.
#    1) config.yaml 은 프로젝트 것을 복사 — 두 판이 같은 설정, 수정 전 기준선과도 같은 설정
#    2) gen_backend.json(CPU/iGPU 측정 결과)을 복사 — 첫 실행에 백엔드 재측정이 끼지 않게
#
# -in: work = 작업 폴더 / prod = "python" | "rust"
#
# -out: (exe 경로, 환경변수 dict, 데이터 폴더)
# -out: error = 배포본이 없으면 FileNotFoundError
#------------------------------------------------------------------
def prepare(work, prod):
    p = PRODUCTS[prod]
    exe = os.path.join(p["dist"], p["exe"])
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    home = os.path.join(work, prod + "_home")
    os.makedirs(home, exist_ok=True)
    shutil.copy(CONFIG, os.path.join(home, "config.yaml"))
    gb = os.path.join(p["dist"], "gen_backend.json")
    if os.path.exists(gb):
        shutil.copy(gb, os.path.join(home, "gen_backend.json"))
    models = os.path.join(p["dist"], "models")
    env = dict(os.environ, SIMPLERAG_HOME=home, SIMPLERAG_MODELS_DIR=models,
               SIMPLERAG_EMBED_DIR=os.path.join(models, "e5-small-ko"), SIMPLERAG_EVIDENCE_FULL="1")
    return exe, env, home


#------------------------------------------------------------------
# 프로세스 묶음 메모리 샘플러
#=> 실행한 프로세스와 그 자식들의 RSS 합을 0.2초마다 재서 최대값을 남긴다.
#
# -필드: peak_mb = 지금까지 최대 RSS 합(MB) / last_mb = 마지막 측정값
#------------------------------------------------------------------
class MemSampler(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid, self.peak_mb, self.last_mb, self.stop = pid, 0.0, 0.0, threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                p = psutil.Process(self.pid)
                procs = [p] + p.children(recursive=True)
                mb = sum(x.memory_info().rss for x in procs if x.is_running()) / 2 ** 20
                self.last_mb = mb
                self.peak_mb = max(self.peak_mb, mb)
            except psutil.Error:
                pass
            time.sleep(0.2)


#------------------------------------------------------------------
# 명령 한 번 실행하고 시간·메모리 재기
#=> 끝날 때까지 기다리며 벽시계 시간과 최대 메모리, 출력 꼬리를 돌려준다.
#
# -in: args = 명령 인자 / env = 환경변수 / timeout = 초
#
# -out: dict = wall_s, peak_mb, rc, out(마지막 3000자)
# -out: error = 시간 초과 시 subprocess.TimeoutExpired 전파
#------------------------------------------------------------------
def run_once(args, env, timeout=1800):
    t0 = time.perf_counter()
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    ms = MemSampler(proc.pid)
    ms.start()
    out, _ = proc.communicate(timeout=timeout)
    wall = time.perf_counter() - t0
    ms.stop.set()
    ms.join()
    return {"wall_s": round(wall, 2), "peak_mb": round(ms.peak_mb, 1), "rc": proc.returncode,
            "out": out.decode("utf-8", "replace")[-3000:]}


#------------------------------------------------------------------
# 인덱싱 실측
#=> sample_pdf 30건을 새로(--rebuild) 인덱싱한다. 두 판을 번갈아 2회씩 — 디스크 캐시 영향을 고르게 한다.
#
# -in: work = 작업 폴더 / res = 결과 dict(갱신)
#
# -out: 없음
# -out: error = 실행 실패는 rc·출력으로 남긴다
#------------------------------------------------------------------
def stage_index(work, res):
    res["index"] = {"python": [], "rust": []}
    for rnd in range(2):
        for prod in ("python", "rust"):
            exe, env, _ = prepare(work, prod)
            r = run_once([exe, "index", "--dir", DOCS, "--rebuild"], env)
            m = re.search(r"청크 ([\d,]+)개 / ([\d.]+)s", r["out"])
            r["chunks"] = int(m.group(1).replace(",", "")) if m else None
            r["index_s"] = float(m.group(2)) if m else None
            res["index"][prod].append(r)
            print("[index] {} #{} wall {}s 인덱싱 {}s 청크 {} peak {}MB rc {}".format(
                prod, rnd + 1, r["wall_s"], r["index_s"], r["chunks"], r["peak_mb"], r["rc"]), flush=True)


#------------------------------------------------------------------
# 예열(콜드스타트) 실측
#=> warmup 명령으로 임베더·인덱스·BM25·LLM 을 올리는 시간을 잰다. 두 판을 번갈아 3회씩.
#
# -in: work, res
#
# -out: 없음
# -out: error = 실행 실패는 rc·출력으로 남긴다
#------------------------------------------------------------------
def stage_warmup(work, res):
    res["warmup"] = {"python": [], "rust": []}
    for rnd in range(3):
        for prod in ("python", "rust"):
            exe, env, _ = prepare(work, prod)
            r = run_once([exe, "warmup"], env, timeout=600)
            m = re.search(r"합계\s+([\d.]+)ms", r["out"])
            r["ready_ms"] = float(m.group(1)) if m else None
            res["warmup"][prod].append(r)
            print("[warmup] {} #{} wall {}s 준비 {}ms peak {}MB".format(
                prod, rnd + 1, r["wall_s"], r["ready_ms"], r["peak_mb"]), flush=True)


#------------------------------------------------------------------
# chat 워커 드라이버
#=> RAGSearchBox 와 같은 방식으로 chat 을 띄워 "질문> " 이 보일 때만 질문을 보내고,
#   다음 프롬프트까지의 출력을 한 문항으로 잘라 근거(@ev)·답변·소요를 꺼낸다.
#
# -필드: proc = 워커 프로세스 / buf = 지금까지 받은 출력 / stamps = (받은 시각, buf 길이)
#------------------------------------------------------------------
class ChatDriver:
    def __init__(self, exe, env, stdin_enc):
        self.enc = stdin_enc
        self.t_start = time.perf_counter()
        self.proc = subprocess.Popen([exe, "chat"], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buf, self.stamps, self.lock = "", [], threading.Lock()
        self.err = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # 표준출력을 조각 단위로 읽어 받은 시각과 함께 쌓는다(줄바꿈 없는 프롬프트도 잡으려고)
    def _read(self):
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.proc.stdout.fileno()
        while True:
            b = os.read(fd, 4096)
            if not b:
                break
            s = dec.decode(b)
            with self.lock:
                self.buf += s
                self.stamps.append((time.perf_counter(), len(self.buf)))

    # 표준에러(리랭커 적재 완료 알림 등)는 따로 모은다 — 막히지 않게 계속 비운다
    def _read_err(self):
        for line in iter(self.proc.stderr.readline, b""):
            self.err.append((time.perf_counter(), line.decode("utf-8", "replace").rstrip()))

    #--------------------------------------------------------------
    # 프롬프트 기다리기
    #=> pos 이후에 "질문> " 이 나타날 때까지 기다린다.
    #
    # -in: pos = 찾기 시작할 위치 / limit = 초
    #
    # -out: 프롬프트가 끝나는 위치
    # -out: error = 시간 초과나 워커 종료 시 RuntimeError
    #--------------------------------------------------------------
    def wait_prompt(self, pos, limit=600):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < limit:
            with self.lock:
                i = self.buf.find(PROMPT, pos)
            if i >= 0:
                return i + len(PROMPT)
            if self.proc.poll() is not None:
                raise RuntimeError("워커 종료: " + self.buf[-500:])
            time.sleep(0.02)
        raise RuntimeError("프롬프트 시간 초과")

    #--------------------------------------------------------------
    # 질문 1건
    #=> 보낸 시각부터 출력에서 근거 머리·답변 머리·첫 답변 글자·다음 프롬프트 시각을 찾는다.
    #
    # -in: q = 질문 / pos = 이전 프롬프트 끝 위치
    #
    # -out: (dict, 다음 위치) — dict 에 근거·답변·소요·외부 시각
    # -out: error = wait_prompt 의 예외 전파
    #--------------------------------------------------------------
    def ask(self, q, pos):
        self.proc.stdin.write((q + "\n").encode(self.enc, errors="replace"))
        self.proc.stdin.flush()
        t_send = time.perf_counter()
        end = self.wait_prompt(pos)
        t_done = time.perf_counter()
        with self.lock:
            seg = self.buf[pos:end - len(PROMPT)]
            stamps = [(t, n) for t, n in self.stamps if pos < n]
        # 답변 머리 다음 글자가 처음 들어온 시각 = 화면 기준 첫 글자
        ans_at = self.buf.find("── 답변", pos)
        ans_body = self.buf.find("\n", ans_at) + 1 if ans_at >= 0 else -1
        t_first = next((t for t, n in stamps if ans_body > 0 and n > ans_body + 1), None)
        return parse_block(seg, t_send, t_first, t_done), end

    def close(self):
        try:
            self.proc.stdin.write(b"exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()


#------------------------------------------------------------------
# 출력 블록 해석
#=> 한 문항의 출력에서 근거(@ev JSON) · 답변 본문 · 소요 줄을 꺼낸다.
#
# -in: seg = 출력 / t_send·t_first·t_done = 외부에서 잰 시각
#
# -out: dict = evidence, answer, ttft_s·total_s(워커가 찍은 값), ext_*(밖에서 잰 값), raw(근거 없을 때)
# -out: error = 없음 (못 찾은 칸은 None)
#------------------------------------------------------------------
def parse_block(seg, t_send, t_first, t_done):
    ev = [json.loads(l.split("@ev ", 1)[1]) for l in seg.splitlines() if l.strip().startswith("@ev ")]
    m_ans = re.search(r"── 답변[^\n]*\n(.*?)\n\s*\n── 소요", seg, re.S)
    m_t = re.search(r"첫 글자 ([\d.]+)s / 완료 ([\d.]+)s", seg)
    m_r = re.search(r"검색 (\d+)ms", seg)
    return {"evidence": [{"path": e.get("path"), "text": e.get("text")} for e in ev],
            "answer": m_ans.group(1).strip() if m_ans else None,
            "ttft_s": float(m_t.group(1)) if m_t else None,
            "total_s": float(m_t.group(2)) if m_t else None,
            "retrieval_ms": int(m_r.group(1)) if m_r else None,
            "ext_first_s": None if t_first is None else round(t_first - t_send, 3),
            "ext_total_s": round(t_done - t_send, 3),
            "raw": None if m_ans else seg.strip()[-500:]}


#------------------------------------------------------------------
# 골든셋 질의 실측
#=> 판마다 chat 워커를 한 번 띄워 100문항을 차례로 묻고, 문항별 결과와 메모리를 남긴다.
#    1) 기동~첫 프롬프트 시간 = 사용자가 체감하는 chat 준비 시간
#    2) 리랭커는 준비 뒤 백그라운드로 올라오므로, stderr 의 적재 완료 알림까지 기다린 뒤 질문한다
#       (첫 문항들만 리랭킹 없이 답하는 일이 없게 — 두 판 조건을 같게)
#    3) 문항마다 결과 파일을 갱신한다
#
# -in: work, res, prods = 돌릴 판 목록
#
# -out: 없음 (chat_<판>.json, res["chat"])
# -out: error = 워커가 죽으면 RuntimeError 전파
#------------------------------------------------------------------
def stage_chat(work, res, prods=("python", "rust")):
    golden = [json.loads(l) for l in io.open(GOLDEN, encoding="utf-8") if l.strip()]
    # 시험 실행용: PERF_LIMIT=N 이면 앞 N문항만
    golden = golden[:int(os.environ.get("PERF_LIMIT", "0")) or None]
    res.setdefault("chat", {})
    for prod in prods:
        exe, env, _ = prepare(work, prod)
        drv = ChatDriver(exe, env, PRODUCTS[prod]["stdin"])
        ms = MemSampler(drv.proc.pid)
        ms.start()
        pos = drv.wait_prompt(0)
        ready_s = time.perf_counter() - drv.t_start
        # 리랭커 백그라운드 적재가 끝날 때까지(최대 60초) — 알림 줄은 stderr 로 온다
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 60 and not any("리랭커" in l for _, l in drv.err):
            time.sleep(0.2)
        rerank_wait = round(time.perf_counter() - t0, 2)
        idle_mb = ms.last_mb
        items = []
        out_path = os.path.join(OUT_DIR, "chat_{}.json".format(prod))
        for i, g in enumerate(golden, 1):
            r, pos = drv.ask(g["user_input"], pos)
            r.update({"id": g["id"], "question_type": g["question_type"], "user_input": g["user_input"],
                      "reference": g["reference"], "must": g["must"], "source_files": g["source_files"]})
            items.append(r)
            save_json(out_path, {"product": prod, "ready_s": round(ready_s, 2), "items": items})
            print("  [{}] {:>3} TTFT {} 완료 {} {}".format(prod, i, r["ttft_s"], r["total_s"],
                                                         (r["answer"] or r["raw"] or "")[:30]), flush=True)
        drv.close()
        ms.stop.set()
        ms.join()
        res["chat"][prod] = {"ready_s": round(ready_s, 2), "rerank_wait_s": rerank_wait,
                             "idle_mb": round(idle_mb, 1), "peak_mb": round(ms.peak_mb, 1),
                             "stderr": [l for _, l in drv.err][-20:]}
        print("[chat] {} 준비 {}s 리랭커 대기 {}s 대기 메모리 {}MB 최대 {}MB".format(
            prod, round(ready_s, 2), rerank_wait, round(idle_mb), round(ms.peak_mb)), flush=True)


#------------------------------------------------------------------
# 배포 용량
#=> 배포 폴더에서 모델·인덱스·설정을 뺀 '프로그램' 부분의 크기를 잰다.
#
# -in: res
#
# -out: 없음 (res["size"])
# -out: error = 없음
#------------------------------------------------------------------
def stage_size(res):
    skip = {"models", "qdrant_data", "rag_index", "bm25_index.npz", "bm25_index.bin", "index_state.json"}
    res["size"] = {}
    for prod, p in PRODUCTS.items():
        tot, n = 0, 0
        for name in os.listdir(p["dist"]):
            if name in skip or name.lower().startswith("ragsearchbox") or name.endswith((".json", ".yaml", ".ini", ".bak")):
                continue
            full = os.path.join(p["dist"], name)
            if os.path.isdir(full):
                for r, _, fs in os.walk(full):
                    for f in fs:
                        tot += os.path.getsize(os.path.join(r, f)); n += 1
            else:
                tot += os.path.getsize(full); n += 1
        res["size"][prod] = {"mb": round(tot / 2 ** 20, 1), "files": n,
                             "exe_mb": round(os.path.getsize(os.path.join(p["dist"], p["exe"])) / 2 ** 20, 1)}
    print("[size]", res["size"], flush=True)


#------------------------------------------------------------------
# 진입점
#=> 고른 단계를 차례로 돌리고 단계마다 perf.json 을 갱신한다.
#
# -in: 없음 (argv: 작업 폴더, 단계)
#
# -out: 없음
# -out: error = 단계 예외 전파
#------------------------------------------------------------------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    work, stage = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "all")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "perf.json")
    res = json.load(io.open(path, encoding="utf-8")) if os.path.exists(path) else {}
    res["date"] = time.strftime("%Y-%m-%d %H:%M")
    steps = ["size", "index", "warmup", "chat"] if stage == "all" else [stage]
    for s in steps:
        if s == "size":
            stage_size(res)
        elif s == "index":
            stage_index(work, res)
        elif s == "warmup":
            stage_warmup(work, res)
        elif s == "chat":
            stage_chat(work, res)
        save_json(path, res)


if __name__ == "__main__":
    main()
