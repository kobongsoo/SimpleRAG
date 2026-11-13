# -*- coding: utf-8 -*-
"""
모델별 설치 파일 만들기 + 목록 파일(models.json) 채우기 (모델 받기 설계서 §7)

사용(저장소 루트에서):
  .venv\\Scripts\\python rust\\build_model_installers.py                 # 내놓는 모델(ready) 모두
  .venv\\Scripts\\python rust\\build_model_installers.py --only qwen3.5-4b-q4 --rev 2
  .venv\\Scripts\\python rust\\build_model_installers.py --dry-run       # 무엇을 만들지만 보기

결과:
  dist-rust\\models-release\\MoAISearch_Model_<별칭>_r<판>.exe (+ -1.bin, -2.bin …)  → GitHub 릴리스 models-1 에 올린다
  dist-rust\\models-release\\models.json                                          → MoAI-S 저장소 docs\\models.json(Pages)
  rust\\ragsearchbox\\ui\\models.json 의 parts(크기 · SHA256) · file_bytes · updated 를 고친다(앱에 든 목록)
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys

RUST = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(RUST)
CATALOG = os.path.join(RUST, "ragsearchbox", "ui", "models.json")
LICENSES = os.path.join(ROOT, "licenses", "models")
# GitHub 릴리스 첨부 한도는 2GB — 이보다 크면 1.9GB 조각으로 나눈다(installer_model.iss DiskSliceSize 와 같은 값)
SPLIT_OVER = 1_900_000_000


#------------------------------------------------------------------
# Inno Setup 컴파일러(ISCC.exe) 찾기
#=> 사용자 설치 · 32비트 Program Files · 64비트 Program Files 순서로 본다(build_installer.ps1 과 같다).
#
# -in: 없음
#
# -out: path = ISCC.exe 경로
# -out: error = 없으면 SystemExit (설치 안내 글)
#------------------------------------------------------------------
def find_iscc():
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
    ]
    for c in cands:
        # 처음 찾은 것을 쓴다
        if os.path.isfile(c):
            return c
    raise SystemExit("Inno Setup 6 이 없습니다 — winget install JRSoftware.InnoSetup")


#------------------------------------------------------------------
# 파일 SHA256 (소문자 16진)
#=> 앱이 받은 조각을 이 값과 비교한다(코드 서명 전이라 유일한 확인 수단).
#    1) 1MB 씩 읽어 해시에 넣는다(수 GB 파일도 메모리를 적게 쓴다)
#
# -in: path = 파일 경로
#
# -out: hex = 64자 16진 글
# -out: error = 파일을 못 읽으면 OSError 전파
#------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


#------------------------------------------------------------------
# 모델 GGUF 원본 찾기
#=> 저장소 models → Python 배포본 dist\simplerag\models → dist-rust\models 순서.
#
# -in: file      = GGUF 파일 이름
# -in: extra_dir = --models-dir 로 준 폴더(없으면 None) — 가장 먼저 본다
#
# -out: path = 찾은 폴더 경로(파일이 아니라 폴더)
# -out: error = 못 찾으면 None
#------------------------------------------------------------------
def find_source(file, extra_dir=None):
    dirs = [extra_dir] if extra_dir else []
    dirs += [os.path.join(ROOT, "models"), os.path.join(ROOT, "dist", "simplerag", "models"), os.path.join(ROOT, "dist-rust", "models")]
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, file)):
            return d
    return None


#------------------------------------------------------------------
# 이번에 만들 모델 고르기
#=> 기본은 내놓는 모델(status ready)에서 내장 모델을 뺀 것.
#    --only 로 별칭을 주면 그것만 — 측정 전(hidden) 모델도 시험 삼아 만들 수 있다.
#
# -in: catalog = 목록 파일 내용(dict)
# -in: only    = 별칭 목록(빈 목록이면 기본 규칙)
#
# -out: models = 만들 모델 dict 목록(목록 파일의 항목 그대로 — 고치면 목록에도 반영된다)
# -out: error = --only 에 목록에 없는 별칭이 있으면 SystemExit
#------------------------------------------------------------------
def pick_models(catalog, only):
    all_models = catalog["models"]
    if only:
        known = {m["alias"] for m in all_models}
        bad = [a for a in only if a not in known]
        if bad:
            raise SystemExit("목록에 없는 별칭: " + ", ".join(bad))
        return [m for m in all_models if m["alias"] in only and not m.get("builtin")]
    # 내장 모델은 기본 설치 파일에 들어 있고, 내놓지 않는 모델(retired · hidden)은 만들지 않는다
    return [m for m in all_models if m.get("status", "ready") == "ready" and not m.get("builtin")]


#------------------------------------------------------------------
# 모델 하나의 설치 파일 만들기
#=> 1) 워커가 읽을 설명 파일(<별칭>.model.json)을 쓴다
#    2) ISCC 로 installer_model.iss 를 묶는다(1.9GB 를 넘으면 조각으로)
#    3) 나온 파일들(.exe · -N.bin)의 크기와 SHA256 을 목록 항목 parts 에 적는다
#
# -in: m     = 목록 파일의 모델 항목(dict) — parts · file_bytes 를 고친다
# -in: iscc  = ISCC.exe 경로
# -in: out   = 출력 폴더
# -in: rev   = 설치 파일 판 번호(파일 이름 _r<판>)
# -in: src   = GGUF 가 있는 폴더
#
# -out: 없음 (m 을 고친다)
# -out: error = 라이선스 글이 없거나 ISCC 가 실패하면 SystemExit
#------------------------------------------------------------------
def build_one(m, iscc, out, rev, src):
    alias, file = m["alias"], m["file"]
    gguf = os.path.join(src, file)
    size = os.path.getsize(gguf)
    if m.get("file_bytes") and m["file_bytes"] != size:
        print(f"  ! file_bytes {m['file_bytes']} → 실제 {size} 로 고침")
    m["file_bytes"] = size
    lic = os.path.join(LICENSES, m.get("license_file", ""))
    if not m.get("license_file") or not os.path.isfile(lic):
        raise SystemExit(f"{alias}: 라이선스 글이 없습니다 — 목록의 license_file 과 licenses\\models 를 확인하세요")
    # 1) 워커가 읽는 설명 파일 — 대화 형식 · 이웃 청크 여부 · 최소 앱 판
    side_dir = os.path.join(out, "_sidecars")
    os.makedirs(side_dir, exist_ok=True)
    side = os.path.join(side_dir, f"{alias}.model.json")
    with open(side, "w", encoding="utf-8") as f:
        json.dump({k: m.get(k) for k in ("alias", "file", "format", "tier", "neighbors", "min_app")}, f, ensure_ascii=False, indent=2)
    # 지난 판 파일이 남아 있으면 조각 수가 섞인다 — 같은 이름은 먼저 지운다
    base = f"MoAISearch_Model_{alias}_r{rev}"
    for n in os.listdir(out):
        if n == base + ".exe" or (n.startswith(base + "-") and n.endswith(".bin")):
            os.remove(os.path.join(out, n))
    split = "1" if size > SPLIT_OVER else "0"
    args = [iscc, f"/DAlias={alias}", f"/DModelFile={file}", f"/DModelName={m['name']}", f"/DRev={rev}",
            f"/DSrc={src}", f"/DSidecar={side}", f"/DLicense={lic}", f"/DOut={out}", f"/DSplit={split}", "installer_model.iss"]
    print(f"  ISCC {alias} ({size / 1e9:.2f}GB{', 조각' if split == '1' else ''})")
    # 아이콘 · 언어 파일 경로가 rust 폴더 기준이라 거기서 돌린다
    r = subprocess.run(args, cwd=RUST, capture_output=True)
    if r.returncode != 0:
        # ISCC 는 시스템 코드 페이지(한국어 Windows 는 cp949)로 찍는다
        print(r.stdout.decode("mbcs", errors="replace")[-3000:])
        raise SystemExit(f"{alias}: Inno Setup 실패({r.returncode})")
    # 3) 나온 파일 — .exe 가 맨 앞, 조각은 번호 순
    names = [base + ".exe"] + sorted((n for n in os.listdir(out) if n.startswith(base + "-") and n.endswith(".bin")),
                                     key=lambda n: int(n[len(base) + 1:-4]))
    m["parts"] = [{"name": n, "bytes": os.path.getsize(os.path.join(out, n)), "sha256": sha256_file(os.path.join(out, n))} for n in names]
    for p in m["parts"]:
        print(f"    {p['name']}  {p['bytes']:,}  {p['sha256'][:16]}…")


#------------------------------------------------------------------
# 진입점 — 인자 읽기 → 모델마다 설치 파일 → 목록 파일 저장
#=> 목록 파일은 앱에 드는 사본(rust\ragsearchbox\ui\models.json)과 Pages 에 올릴 사본(출력 폴더)을 함께 쓴다.
#
# -in: 없음 (명령줄 인자)
#
# -out: 없음
# -out: error = 준비가 안 됐으면 SystemExit(사유 글)
#------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="모델별 설치 파일 만들기")
    ap.add_argument("--only", default="", help="별칭(쉼표로 여럿) — 없으면 ready 모델 모두")
    ap.add_argument("--rev", type=int, default=1, help="설치 파일 판 번호(기본 1)")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist-rust", "models-release"), help="출력 폴더")
    ap.add_argument("--models-dir", default=None, help="GGUF 원본 폴더(먼저 본다)")
    ap.add_argument("--dry-run", action="store_true", help="무엇을 만들지만 보이고 끝낸다")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    # ISCC 는 rust 폴더에서 돌므로 상대 경로는 지금 폴더 기준 절대 경로로 바꿔 둔다
    a.out = os.path.abspath(a.out)
    if a.models_dir:
        a.models_dir = os.path.abspath(a.models_dir)

    with open(CATALOG, encoding="utf-8") as f:
        catalog = json.load(f)
    only = [s.strip() for s in a.only.split(",") if s.strip()]
    todo = pick_models(catalog, only)
    if not todo:
        raise SystemExit("만들 모델이 없습니다")
    # 원본 GGUF 가 다 있는지 먼저 본다 — 중간에 멈추면 목록 파일이 반쯤 고쳐진다
    srcs = {}
    for m in todo:
        s = find_source(m["file"], a.models_dir)
        if not s:
            raise SystemExit(f"{m['alias']}: {m['file']} 를 찾지 못했습니다 — --models-dir 로 폴더를 주세요")
        srcs[m["alias"]] = s
        print(f"- {m['alias']}: {os.path.join(s, m['file'])}")
    if a.dry_run:
        return
    iscc = find_iscc()
    os.makedirs(a.out, exist_ok=True)
    for m in todo:
        build_one(m, iscc, a.out, a.rev, srcs[m["alias"]])
    # 목록 파일 저장 — 날짜가 바뀌어야 앱이 캐시보다 새것으로 본다
    catalog["updated"] = datetime.date.today().isoformat()
    text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    for p in (CATALOG, os.path.join(a.out, "models.json")):
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    shutil.rmtree(os.path.join(a.out, "_sidecars"), ignore_errors=True)
    print(f"완료 — {a.out}")
    print("다음: 출력 폴더의 설치 파일을 GitHub 릴리스 models-1 에, models.json 을 MoAI-S docs\\ 에 올린다")


if __name__ == "__main__":
    main()
