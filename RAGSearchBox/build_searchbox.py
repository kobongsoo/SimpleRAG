#------------------------------------------------------------------
# RAGSearchBox.exe 빌드 스크립트 (설계서 §12)
#=> 감시·UI 프로그램만 묶는다. 무거운 것(llama.cpp·onnxruntime·모델)은 전부
#   워커인 simplerag.exe 쪽에 있으므로 이 exe 는 작아야 한다.
#
#   왜 onefile 인가
#     SimpleRAG 본체와 달리 여기엔 수백 MB DLL 이 없다. 파일 하나면 배포가 쉽고,
#     자동 시작 등록도 경로 하나만 가리키면 된다.
#
#   ⚠️ 공유 .venv 라서 빼내는 일이 중요하다 (D4)
#     같은 .venv 에 llama_cpp·onnxruntime·모델 라이브러리가 들어 있어, 가만두면
#     PyInstaller 가 그것까지 끌어와 수백 MB 짜리 exe 가 된다. RAGSearchBox 코드는
#     simplerag 를 import 하지 않으므로(경계는 프로세스뿐) 통째로 제외한다.
#     빌드 뒤 크기를 재서 LIMIT_MB 를 넘으면 실패로 본다 — 조용히 부풀지 않게.
#
#   ⚠️ comtypes.gen 은 빌드 시점에 미리 만들어 넣는다
#     UIA 타입 라이브러리 래퍼는 처음 쓸 때 comtypes/gen 폴더에 .py 를 써서 만든다.
#     exe 안에는 쓸 수 없으므로, 여기서 한 번 만들어 hidden-import 로 함께 묶는다.
#
#   ⚠️ 워커 전제
#     함께 쓸 simplerag.exe 는 --python-option u 로 빌드된 것이어야 한다(D8).
#     그 옵션이 없으면 근거가 답변과 함께 늦게 뜬다(P0-4 실측).
#
#   사용:
#     .venv\Scripts\python.exe RAGSearchBox\build_searchbox.py
#     .venv\Scripts\python.exe RAGSearchBox\build_searchbox.py --deploy   # dist\simplerag 옆으로 복사까지
#------------------------------------------------------------------

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
APP = "RAGSearchBox"
ICON = os.path.join(HERE, APP + ".ico")
DEPLOY_DIR = os.path.join(ROOT, "dist", "simplerag")
LIMIT_MB = 40                       # 이보다 크면 뭔가 딸려 들어온 것이다

# 공유 .venv 에 있지만 이 프로그램은 쓰지 않는 것들. 넣으면 exe 가 수백 MB 가 된다.
EXCLUDE = [
    "simplerag", "csoclassify",
    "llama_cpp", "onnxruntime", "onnx", "qdrant_client", "tokenizers",
    "numpy", "scipy", "pandas", "torch", "transformers", "matplotlib",
    "PIL", "yaml", "pypdfium2", "pypdfium2_raw", "xlrd", "olefile",
    "IPython", "pytest", "PyInstaller",
]

# 정적 분석에 안 잡히는 것들 — UIA 래퍼는 위 설명대로 미리 만들어 둔 모듈이다.
HIDDEN = [
    "comtypes.gen",
    "comtypes.gen.UIAutomationClient",
    "comtypes.gen.stdole",
    "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
    "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
    "win32com.client",
]


#------------------------------------------------------------------
# UIA 타입 라이브러리 래퍼 미리 만들기
#=> comtypes 가 UIAutomationCore.dll 을 읽어 comtypes/gen/*.py 를 만들어 둔다.
#   이미 있으면 아무 일도 하지 않는다.
#
# -in: 없음
#
# -out: 만들어진 모듈 이름 목록
# -out: error = 실패하면 빈 목록 + 경고 출력(빌드는 계속하지만 실행이 안 될 수 있다)
#------------------------------------------------------------------
def prepare_comtypes_gen():
    try:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        import comtypes.gen
        d = os.path.dirname(comtypes.gen.__file__)
        mods = [f[:-3] for f in os.listdir(d) if f.endswith(".py") and f != "__init__.py"]
        print("[gen] comtypes.gen 준비됨: {}".format(", ".join(mods)))
        return mods
    except Exception as e:
        print("  ⚠️ comtypes.gen 을 만들지 못했습니다: {}".format(e), file=sys.stderr)
        return []


#------------------------------------------------------------------
# PyInstaller 실행
#=> onefile + windowed(콘솔 창 없음) + 아이콘.
#   --windowed 라 print 는 아무 데도 안 나온다. 그래서 로그 파일이 유일한 창구다(log.py).
#
# -in: gen_mods = 함께 묶을 comtypes.gen 모듈 이름 목록
#
# -out: True = 성공
# -out: error = 실패하면 False (PyInstaller 출력이 그대로 보인다)
#------------------------------------------------------------------
def build(gen_mods):
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", APP,
           "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
           "--paths", HERE]

    if os.path.isfile(ICON):
        cmd += ["--icon", ICON]
        # 트레이가 파일에서 읽을 수 있게 exe 안에도 넣는다(tray.py 가 옆/내장을 찾는다)
        cmd += ["--add-data", "{}{}.".format(ICON, os.pathsep)]
    else:
        print("  ⚠️ 아이콘이 없습니다. make_icon.py 를 먼저 실행하세요: {}".format(ICON))

    for m in HIDDEN + ["comtypes.gen." + g for g in gen_mods]:
        cmd += ["--hidden-import", m]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]

    cmd.append(os.path.join(HERE, "main.py"))

    print("[build] PyInstaller 실행...")
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("[build] 실패 (exit={})".format(r.returncode), file=sys.stderr)
        return False
    print("[build] 완료 {:.0f}초".format(time.perf_counter() - t0))
    return True


#------------------------------------------------------------------
# 크기 확인
#=> 제외가 제대로 먹었는지 보는 가장 확실한 신호다. 넘으면 빌드를 실패로 본다.
#
# -in: 없음
#
# -out: (ok, MB)
# -out: error = exe 가 없으면 (False, 0)
#------------------------------------------------------------------
def check_size():
    exe = os.path.join(DIST, APP + ".exe")
    if not os.path.isfile(exe):
        print("  ❌ exe 가 만들어지지 않았습니다: {}".format(exe), file=sys.stderr)
        return False, 0
    mb = os.path.getsize(exe) / 1024 / 1024
    ok = mb <= LIMIT_MB
    print("  실행 파일 : {} ({:.1f} MB, 한도 {} MB) {}".format(
        exe, mb, LIMIT_MB, "OK" if ok else "❌ 너무 큽니다 — 제외 목록을 확인하세요"))
    return ok, mb


#------------------------------------------------------------------
# 배치 — 워커 옆으로 복사
#=> dist\simplerag 에 두면 RAGSearchBox 가 simplerag.exe 를 자동으로 찾는다(§6).
#   INI 는 이미 있으면 덮어쓰지 않는다 — 사용자가 적어 둔 범위 폴더를 지우면 안 된다.
#
# -in: 없음
#
# -out: 복사한 폴더 또는 None
# -out: error = 없음 (대상 폴더가 없으면 알리고 건너뛴다)
#------------------------------------------------------------------
def deploy():
    if not os.path.isdir(DEPLOY_DIR):
        print("  ⚠️ 배포 폴더가 없습니다(먼저 build_exe.py 로 워커를 빌드하세요): {}"
              .format(DEPLOY_DIR))
        return None
    shutil.copy2(os.path.join(DIST, APP + ".exe"), os.path.join(DEPLOY_DIR, APP + ".exe"))
    ini_dst = os.path.join(DEPLOY_DIR, APP + ".ini")
    if os.path.isfile(ini_dst):
        print("  INI 는 이미 있어 그대로 둡니다: {}".format(ini_dst))
    else:
        shutil.copy2(os.path.join(HERE, APP + ".ini"), ini_dst)
        print("  INI 복사: {} — [Scope] Folders 를 지정해야 동작합니다".format(ini_dst))
    print("  배포: {}".format(DEPLOY_DIR))
    return DEPLOY_DIR


def main():
    p = argparse.ArgumentParser(description="RAGSearchBox exe 빌드")
    p.add_argument("--deploy", action="store_true",
                   help="빌드 뒤 dist\\simplerag 옆으로 복사")
    args = p.parse_args()

    if not os.path.isfile(ICON):
        print("[icon] 아이콘을 먼저 만듭니다")
        subprocess.run([sys.executable, os.path.join(HERE, "make_icon.py")])

    gen_mods = prepare_comtypes_gen()
    if not build(gen_mods):
        return 1

    print("\n[확인]")
    ok, _mb = check_size()
    if not ok:
        return 2

    if args.deploy:
        print("\n[배포]")
        deploy()

    print("\n다음 단계")
    print("  1) RAGSearchBox.ini 의 [Scope] Folders 에 동작할 폴더를 적습니다(비면 동작하지 않습니다)")
    print("  2) RAGSearchBox.exe 를 실행하면 트레이에 상주합니다")
    print("  3) 로그인할 때 자동 시작하려면: RAGSearchBox.exe --autorun on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
