#------------------------------------------------------------------
# exe 빌드 스크립트 (PyInstaller)
#=> CLI 를 단일 실행파일 폴더로 묶는다.
#
#   왜 onefile 이 아니라 onedir 인가
#     onefile 은 실행할 때마다 수백 MB 의 DLL(onnxruntime, llama.cpp)을 임시
#     폴더에 풀어야 해서 기동이 5~10초 더 늘어난다. 이미 모델 적재에 4.7초가
#     드는데 거기에 얹히면 체감이 크게 나빠진다. onedir 은 그 비용이 없다.
#
#   ⚠️ 모델은 exe 에 넣지 않는다
#     GGUF 378MB + ONNX 118MB + 토크나이저 17MB = 약 0.5GB 다. exe 에 넣으면
#     빌드·배포·갱신이 모두 무거워지고, 모델만 바꿔 끼우지도 못한다.
#     빌드 후 dist 폴더에 복사해 넣는다(아래에서 자동 처리).
#     리랭커(bge-reranker-base int8, 283MB)도 같은 방식이다.
#
#   iGPU(Vulkan) 런타임 — runtime/llama_vulkan/ (약 59MB)
#     CPU 판 llama.dll 은 llama_cpp 패키지에 들어 있어 --collect-all 로 담기고,
#     Vulkan 판은 이 폴더를 exe 옆 같은 위치로 복사한다. 앱이 첫 실행 때 두 판의
#     속도를 재서 고른다(generate/backend.py, REPORT §32).
#
#   사용:
#     .venv\Scripts\python.exe build_exe.py            # 빌드 + 모델 복사
#     .venv\Scripts\python.exe build_exe.py --no-models  # 모델 복사 생략
#
#   배포 폴더의 데이터는 지킨다 — 인덱스·config.yaml·RAGSearchBox·모델 등 빌드 산출물이 아닌 것은
#   빌드 동안 build/keep_<시각>/ 로 옮겼다가 되돌리고, config.yaml 은 없을 때만 복사한다.
#------------------------------------------------------------------

import argparse
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
APP = "simplerag"

# 네이티브 확장을 가진 패키지들 — PyInstaller 가 자동으로 못 찾는 것이 많아
# collect-all 로 통째로 담는다(DLL·데이터파일·메타데이터 포함).
COLLECT_ALL = [
    "llama_cpp",      # GGUF 추론 — llama.dll 등
    "onnxruntime",    # 임베딩 추론 — onnxruntime DLL 일체
    "tokenizers",     # Rust 확장
    "qdrant_client",  # 데이터파일/버전 메타데이터
    "pypdfium2",      # PDF — pdfium DLL
    "pypdfium2_raw",
]

# 지연 import 되어 정적 분석에 안 잡히는 모듈들.
HIDDEN = [
    "olefile", "xlrd", "yaml",
    "csoclassify", "csoclassify.extract",
    "simplerag.extract", "simplerag.console",
    "simplerag.generate.backend", "simplerag.retrieve.reranker",
]

# 하위 모듈을 통째로 담을 패키지 — 구조화 추출기(docmodel)는 포맷별 파서를 함수 안에서
# 필요할 때 가져온다(extract._parser_for). 하나라도 빠지면 그 포맷만 조용히 legacy 로
# 폴백해 겉으로는 동작하는 것처럼 보이므로, 통째로 담아 빠질 여지를 없앤다.
COLLECT_SUBMODULES = [
    "simplerag.docmodel",
]


#------------------------------------------------------------------
# 형제 프로젝트 src 경로 확인 (csoclassify 패키지가 들어 있는 곳)
#=> extract 모듈을 exe 에 번들하려면 빌드 시점에 소스가 있어야 한다.
#   config.py 와 같은 후보 순서로 찾는다 — 프로젝트 이름이 바뀌어도
#   (CSOClassify → MpowerClassify) 빌드가 조용히 "추출 기능 제한"으로
#   빠지지 않게 한다.
#
# -out: 경로 또는 None
#------------------------------------------------------------------
def find_cso_src():
    override = os.environ.get("SIMPLERAG_CSO_SRC")
    cands = [override] if override else [r"D:\Project\MpowerClassify\src",
                                         r"D:\Project\CSOClassify\src"]
    for cand in cands:
        if os.path.isdir(os.path.join(cand, "csoclassify")):
            return cand
    return None


#------------------------------------------------------------------
# PyInstaller 실행
#=> onedir 모드로 빌드한다. 콘솔 앱이므로 --console.
#------------------------------------------------------------------
def build(cso_src):
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--console",
           # 파이프로 실행될 때 stdout 버퍼링을 끈다(python -u 와 같음).
           # 동결 exe 는 PYTHONUNBUFFERED 환경변수를 무시하므로(RAGSearchBox P0-4 실측)
           # 빌드 시점에 박아 넣어야 근거를 답변보다 먼저 내보낼 수 있다.
           "--python-option", "u",
           "--name", APP,
           "--distpath", DIST, "--workpath", BUILD,
           "--specpath", BUILD,
           "--paths", SRC]

    if cso_src:
        cmd += ["--paths", cso_src]
    for pkg in COLLECT_ALL:
        cmd += ["--collect-all", pkg]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    for pkg in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", pkg]

    # 진입점: CLI. src 를 paths 에 넣었으므로 simplerag 패키지가 잡힌다.
    cmd.append(os.path.join(SRC, "simplerag", "cli.py"))

    print("[build] PyInstaller 실행 (수 분 소요)...")
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        print("[build] 실패 (exit={})".format(r.returncode), file=sys.stderr)
        return False
    print("[build] 완료 {:.0f}초".format(time.perf_counter() - t0))
    return True


#------------------------------------------------------------------
# 모델 배치
#=> exe 옆에 모델을 복사한다. config 가 이 위치를 자동으로 찾는다.
#     dist/simplerag/models/Qwen3-0.6B-Q4_K_M.gguf
#     dist/simplerag/models/e5-small-ko/{model.onnx, tokenizer.json, ...}
#------------------------------------------------------------------
def copy_models():
    out = os.path.join(DIST, APP, "models")
    os.makedirs(out, exist_ok=True)

    # 생성 모델(GGUF)
    src_models = os.path.join(ROOT, "models")
    n = 0
    if os.path.isdir(src_models):
        for f in os.listdir(src_models):
            if f.lower().endswith(".gguf"):
                dst = os.path.join(out, f)
                if not os.path.isfile(dst):
                    print("  GGUF 복사: {} ({:.0f}MB)".format(
                        f, os.path.getsize(os.path.join(src_models, f)) / 1024 / 1024))
                    shutil.copy2(os.path.join(src_models, f), dst)
                n += 1
    if not n:
        print("  ⚠️ GGUF 모델을 찾지 못했습니다 — bench/download_models.py 를 먼저 실행하세요.")

    # 임베딩 모델(ONNX + 토크나이저). 런타임에 필요한 파일만 고른다.
    from simplerag import config
    src_embed = config.MODEL_DIR
    dst_embed = os.path.join(out, "e5-small-ko")
    need = ["model.onnx", "tokenizer.json"]
    if os.path.isdir(src_embed):
        os.makedirs(dst_embed, exist_ok=True)
        for f in need + ["config.json", "special_tokens_map.json",
                         "tokenizer_config.json"]:
            s = os.path.join(src_embed, f)
            d = os.path.join(dst_embed, f)
            if os.path.isfile(s) and not os.path.isfile(d):
                print("  임베딩 복사: {} ({:.0f}MB)".format(
                    f, os.path.getsize(s) / 1024 / 1024))
                shutil.copy2(s, d)
    else:
        print("  ⚠️ 임베딩 모델 폴더 없음: {}".format(src_embed))

    # 리랭커(ONNX + 토크나이저). 없으면 리랭킹이 자동으로 꺼질 뿐 앱은 돈다.
    src_rr = config.RERANK_DIR
    dst_rr = os.path.join(out, os.path.basename(os.path.normpath(src_rr)))
    if os.path.isfile(os.path.join(src_rr, "model_int8.onnx")):
        os.makedirs(dst_rr, exist_ok=True)
        for f in ("model_int8.onnx", "tokenizer.json", "config.json",
                  "special_tokens_map.json", "tokenizer_config.json"):
            s = os.path.join(src_rr, f)
            d = os.path.join(dst_rr, f)
            if os.path.isfile(s) and not os.path.isfile(d):
                print("  리랭커 복사: {} ({:.0f}MB)".format(
                    f, os.path.getsize(s) / 1024 / 1024))
                shutil.copy2(s, d)
    else:
        print("  ⚠️ 리랭커 모델 없음(리랭킹 비활성으로 배포): {}".format(src_rr))


#------------------------------------------------------------------
# iGPU(Vulkan) 런타임 배치
#=> runtime/llama_vulkan 의 DLL 을 exe 옆 같은 상대 위치로 복사한다.
#   config.VULKAN_LIB_DIR 이 <exe 폴더>/runtime/llama_vulkan 을 가리킨다.
#   없으면 경고만 한다 — 앱은 CPU 로 동작한다(backend.vulkan_available).
#
#   ⚠️ DLL 은 파이썬 llama-cpp-python 과 **같은 버전**이어야 한다(VERSION.txt).
#      다르면 앱이 쓰지 않고 CPU 로 간다. 패키지를 올리면 이 폴더도 같이 바꿀 것.
#      모델과 달리 항상 덮어쓴다 — 옛 DLL 이 남으면 버전이 어긋난다.
#
# -in: 없음
#
# -out: ok = 복사했으면 True
# -out: error = 없음 (복사 중 파일 오류는 예외 전파)
#------------------------------------------------------------------
def copy_runtime():
    src = os.path.join(ROOT, "runtime", "llama_vulkan")
    dst = os.path.join(DIST, APP, "runtime", "llama_vulkan")
    if not os.path.isfile(os.path.join(src, "ggml-vulkan.dll")):
        print("  ⚠️ Vulkan 런타임 없음 — iGPU 미지원으로 배포: {}".format(src))
        return False
    os.makedirs(dst, exist_ok=True)
    for f in sorted(os.listdir(src)):
        if f.lower().endswith((".dll", ".txt")):
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    size = sum(os.path.getsize(os.path.join(dst, f)) for f in os.listdir(dst))
    print("  Vulkan 런타임 복사: {} ({:.0f}MB)".format(dst, size / 1024 / 1024))
    return True


#------------------------------------------------------------------
# 배포 폴더가 잠겨 있지 않은지 미리 확인
#=> PyInstaller 는 dist/simplerag 를 지우고 다시 만든다. 그 폴더를 누가 쥐고 있으면
#   (그 안에서 연 셸, 실행 중인 simplerag.exe · RAGSearchBox.exe) 파일만 지우다가 멈춰
#   반쯤 빈 폴더가 남는다(2026-10-26 실제로 겪음). 그래서 빌드 전에 폴더 이름을 잠깐
#   바꿔 보고, 안 바뀌면 아무것도 건드리지 않고 멈춘다.
#
# -in: app_dir = 배포 폴더
#
# -out: None = 괜찮음 / 문자열 = 멈춰야 하는 이유
# -out: error = 없음 (이름 바꾸기 실패는 이유 문자열로 돌려준다)
#------------------------------------------------------------------
def check_unlocked(app_dir):
    if not os.path.isdir(app_dir):
        return None
    here = os.path.normcase(os.path.abspath(os.getcwd()))
    if here == os.path.normcase(app_dir) or here.startswith(os.path.normcase(app_dir) + os.sep):
        return "빌드 스크립트의 작업 폴더가 배포 폴더 안입니다: " + here
    probe = app_dir + "_locktest"
    try:
        os.rename(app_dir, probe)
    except OSError as e:
        return "배포 폴더를 다른 프로세스가 쓰고 있습니다({}). simplerag.exe · RAGSearchBox.exe 를 끄고, " \
               "그 폴더 안에 있는 셸·탐색기 창을 닫은 뒤 다시 실행하세요.".format(e.strerror or e)
    os.rename(probe, app_dir)
    return None


# 빌드가 새로 만드는 것 — 이것만 빼고 배포 폴더의 나머지는 빌드 동안 옮겨 두었다 되돌린다
BUILD_OUTPUTS = {"_internal", APP + ".exe"}


#------------------------------------------------------------------
# 배포 데이터 잠시 치워 두기
#=> 빌드 산출물(_internal · simplerag.exe)이 아닌 것은 전부 build/keep_<시각>/ 로 옮겨 둔다 —
#   운영 인덱스(qdrant_data · bm25_index.npz · index_state.json), gen_backend.json, config.yaml,
#   RAGSearchBox.exe · .ini, 모델, 런타임, 그 밖에 사용자가 둔 파일. 목록을 따로 두지 않아
#   새 파일이 생겨도 빠지지 않는다. 같은 드라이브 안의 이동이라 모델(수 GB)도 바로 끝난다.
#
# -in: app_dir = 배포 폴더
#
# -out: (보관 폴더, 옮긴 이름 목록) — 배포 폴더가 없으면 (None, [])
# -out: error = 옮기다 실패하면 이미 옮긴 것을 되돌린 뒤 OSError 전파
#------------------------------------------------------------------
def stash_dist(app_dir):
    if not os.path.isdir(app_dir):
        return None, []
    keep = os.path.join(BUILD, "keep_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(keep, exist_ok=True)
    moved = []
    try:
        for name in sorted(os.listdir(app_dir)):
            if name in BUILD_OUTPUTS:
                continue
            shutil.move(os.path.join(app_dir, name), os.path.join(keep, name))
            moved.append(name)
    except OSError:
        # 반만 옮긴 채로 빌드하지 않는다 — 옮긴 것을 되돌리고 멈춘다
        restore_dist(app_dir, keep, moved)
        raise
    print("[keep] 배포 데이터 {}개를 잠시 옮겼습니다: {}".format(len(moved), keep))
    return keep, moved


#------------------------------------------------------------------
# 치워 둔 배포 데이터 되돌리기
#=> 빌드가 성공하든 실패하든 반드시 부른다(main 의 finally). 빌드가 같은 이름을 새로
#   만들었으면(그럴 일은 없지만) 덮어쓰지 않고 보관 폴더에 남긴 채 알린다.
#
# -in: app_dir = 배포 폴더 / keep = 보관 폴더 / moved = 옮긴 이름 목록
#
# -out: 없음
# -out: error = 없음 (되돌리지 못한 것은 보관 폴더 경로와 함께 알린다)
#------------------------------------------------------------------
def restore_dist(app_dir, keep, moved):
    if not keep:
        return
    os.makedirs(app_dir, exist_ok=True)
    left = []
    for name in moved:
        src, dst = os.path.join(keep, name), os.path.join(app_dir, name)
        if os.path.exists(dst):
            left.append(name)
            continue
        shutil.move(src, dst)
    if left:
        print("  ⚠️ 빌드가 같은 이름을 만들어 되돌리지 못했습니다: {} — 보관 위치 {}".format(left, keep))
    else:
        os.rmdir(keep)
        print("[keep] 배포 데이터 {}개를 되돌렸습니다".format(len(moved)))


#------------------------------------------------------------------
# config.yaml 배치 — 배포 폴더에 없을 때만 복사
#=> 배포본에서만 바꾼 설정(예: parent: section)을 빌드가 덮어쓰지 않게 한다.
#   이미 있으면 그대로 두고, 프로젝트 config.yaml 에만 있는 키(새로 생긴 옵션)를 알린다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음 (YAML 을 못 읽으면 키 비교만 건너뛴다)
#------------------------------------------------------------------
def place_config():
    src_cfg = os.path.join(ROOT, "config.yaml")
    dst_cfg = os.path.join(DIST, APP, "config.yaml")
    if not os.path.isfile(src_cfg):
        return
    if not os.path.isfile(dst_cfg):
        shutil.copy2(src_cfg, dst_cfg)
        print("  config.yaml 복사")
        return
    try:
        import yaml
        keys = lambda p: {"{}.{}".format(s, k) for s, v in (yaml.safe_load(open(p, encoding="utf-8")) or {}).items()
                          if isinstance(v, dict) for k in v}
        new = sorted(keys(src_cfg) - keys(dst_cfg))
    except Exception:
        new = []
    print("  config.yaml 은 배포본 것을 그대로 둡니다(덮어쓰지 않음)")
    if new:
        print("  ℹ️ 프로젝트 config.yaml 에만 있는 키: {} — 필요하면 배포본에 옮겨 적으세요".format(", ".join(new)))


#------------------------------------------------------------------
# 배포 폴더 요약
#------------------------------------------------------------------
def summarize():
    app_dir = os.path.join(DIST, APP)
    if not os.path.isdir(app_dir):
        return
    total = 0
    for r, _, fs in os.walk(app_dir):
        for f in fs:
            total += os.path.getsize(os.path.join(r, f))
    exe = os.path.join(app_dir, APP + ".exe")
    print("\n배포 폴더 : {}".format(app_dir))
    print("실행 파일 : {}  ({})".format(
        exe, "있음" if os.path.isfile(exe) else "❌ 없음"))
    print("전체 크기 : {:.0f} MB".format(total / 1024 / 1024))


def main():
    p = argparse.ArgumentParser(description="SimpleRAG exe 빌드")
    p.add_argument("--no-models", action="store_true", help="모델 복사 생략")
    p.add_argument("--models-only", action="store_true", help="빌드 없이 모델만 복사")
    args = p.parse_args()

    sys.path.insert(0, SRC)

    if not args.models_only:
        app_dir = os.path.join(DIST, APP)
        # 잠긴 폴더를 지우다 멈추면 반쯤 빈 폴더가 남는다 — 아무것도 건드리기 전에 확인
        why = check_unlocked(app_dir)
        if why:
            print("[build] 멈춤: " + why, file=sys.stderr)
            return 1
        cso = find_cso_src()
        print("[build] CSOClassify src: {}".format(cso or "없음(추출 기능 제한)"))
        # PyInstaller 가 배포 폴더를 통째로 지우므로 인덱스·설정·트레이 앱 등은 옮겨 두었다가 되돌린다
        keep, moved = stash_dist(app_dir)
        try:
            ok = build(cso)
        finally:
            restore_dist(app_dir, keep, moved)
        if not ok:
            return 1

    print("\n[runtime] iGPU(Vulkan) 런타임 복사")
    copy_runtime()

    # 검색·청킹 계수 파일(REPORT §35) — exe 옆에 두면 코드 수정 없이 바꿀 수 있다.
    #   배포본에서 바꾼 설정을 지키려고 없을 때만 복사한다
    place_config()
    # 기본 동의어 사전은 배포 때마다 새것으로 — 사용자 사전(synonyms.yaml)은 건드리지 않는다
    src_syn = os.path.join(ROOT, "synonyms.default.yaml")
    if os.path.isfile(src_syn):
        shutil.copy2(src_syn, os.path.join(DIST, APP, "synonyms.default.yaml"))
        print("  synonyms.default.yaml 복사")

    if not args.no_models:
        print("\n[models] exe 옆으로 모델 복사")
        copy_models()

    summarize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
