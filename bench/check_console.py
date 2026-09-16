#------------------------------------------------------------------
# 콘솔 색 깨짐 확인 (실제 콘솔에서 직접 실행할 것)
#=> llama.cpp 가 Windows 콘솔 텍스트 속성을 바꿔 놓으면 이후 출력이 검은
#   글자로 나와 보이지 않는다. `simplerag.console` 방어가 동작하는지 확인한다.
#
#   ⚠️ 반드시 PowerShell/cmd 창에서 **리다이렉트 없이** 실행해야 한다.
#      파이프(`| more`)나 파일 리다이렉트를 걸면 콘솔 핸들이 아니라서
#      아무것도 측정되지 않는다.
#
#   사용:  .venv\Scripts\python.exe bench\check_console.py
#------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "src")))

from simplerag import console          # noqa: E402


def show(label):
    got = console._read_attrs()
    if got is None:
        print("  {:<26} 콘솔 아님(리다이렉트) — 이 스크립트는 콘솔에서 직접 실행하세요"
              .format(label))
        return None
    _, a = got
    print("  {:<26} 0x{:04X}  전경={:X} 배경={:X}".format(
        label, a, a & 0x0F, (a >> 4) & 0x0F))
    return a


def main():
    print("콘솔 텍스트 속성 추적\n")
    start = show("① 시작")
    if start is None:
        return 2

    console.install()

    from simplerag.warmup import App
    app = App()
    try:
        app.warmup(wait=True)
        after_load = show("② 모델 적재 후")

        for ev in app.pipeline.answer("테스트 질문입니다"):
            pass
        after_gen = show("③ 답변 생성 후")

        console.restore()
        after_fix = show("④ console.restore() 후")
    finally:
        app.close()

    print()
    changed = (after_load != start) or (after_gen != start)
    if not changed:
        print("  ✅ 콘솔 속성이 바뀌지 않았습니다 — 이 환경에서는 문제가 없습니다.")
    elif after_fix == start:
        print("  ✅ 중간에 속성이 바뀌었지만 restore() 로 원복됐습니다.")
        print("     (전경 0 = 검은 글자. 방어가 없으면 다음 출력이 안 보입니다)")
    else:
        print("  ❌ 원복되지 않았습니다. 시작={:#06X} 최종={:#06X}".format(
            start, after_fix if after_fix is not None else 0))
        return 1

    print("\n이 줄이 정상 색으로 보이면 콘솔이 정상 상태입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
