#------------------------------------------------------------------
# 콘솔 상태 보호
#=> llama.cpp 는 Windows 에서 콘솔 텍스트 속성을 Win32 API 로 직접 바꾼다
#   (SetConsoleTextAttribute). 그 값을 되돌리지 않고 프로세스가 끝나면
#   **다음 명령의 출력이 검은 글자로 나와 안 보이는** 현상이 생긴다.
#   실제로 관찰됐다 — 드래그해서 선택해야 글자가 보였다.
#
#   ANSI 이스케이프가 아니라 API 호출이라 출력 스트림에는 흔적이 남지 않는다
#   (리다이렉트해서 봐도 0x1B 가 0개다). 그래서 '무엇이 바꿨는지' 를 쫓는 대신
#   시작 시점의 속성을 저장해 두었다가 되돌리는 방어 방식을 쓴다.
#
#   적용 지점
#    - 프로그램 시작 시 install() → 종료 시 자동 복구(atexit)
#    - 대화형 모드에서는 답변 1건이 끝날 때마다 restore() — 세션 중간에
#      깨진 색이 다음 질문까지 이어지지 않게 한다.
#------------------------------------------------------------------

import atexit
import os
import sys

_saved_attrs = None          # 시작 시점의 콘솔 텍스트 속성
_std_handle = None           # 표준 출력 콘솔 핸들
_installed = False

_STD_OUTPUT_HANDLE = -11


#------------------------------------------------------------------
# 현재 콘솔 텍스트 속성 읽기 (Windows 전용)
#=> 리다이렉트되어 있거나 Windows 가 아니면 None 을 돌려준다.
#
# -out: (handle, attributes) 또는 None
#------------------------------------------------------------------
def _read_attrs():
    if os.name != "nt":
        return None

    try:
        import ctypes
        import ctypes.wintypes as wt

        class COORD(ctypes.Structure):
            _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT),
                        ("Right", wt.SHORT), ("Bottom", wt.SHORT)]

        class CSBI(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                        ("wAttributes", wt.WORD), ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(_STD_OUTPUT_HANDLE)
        info = CSBI()
        if not k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None          # 파이프/파일로 리다이렉트된 상태
        return handle, info.wAttributes
    except Exception:
        return None


#------------------------------------------------------------------
# 콘솔 상태 보호 설치
#=> 시작 시점의 속성을 저장하고, 종료 시 자동 복구되도록 등록한다.
#   콘솔이 아니면(리다이렉트) 아무것도 하지 않는다.
#
# -out: bool = 보호가 설치됐으면 True
#------------------------------------------------------------------
def install():
    global _saved_attrs, _std_handle, _installed

    if _installed:
        return _saved_attrs is not None

    _installed = True
    got = _read_attrs()
    if got is None:
        return False

    _std_handle, _saved_attrs = got
    atexit.register(restore)
    return True


#------------------------------------------------------------------
# 콘솔 상태 복구
#=> 저장해 둔 속성으로 되돌린다. 저장된 값이 없으면 아무것도 하지 않는다
#   (임의의 색으로 덮어써서 사용자 테마를 망가뜨리지 않기 위함).
#------------------------------------------------------------------
def restore():
    if _saved_attrs is None or _std_handle is None:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTextAttribute(
            _std_handle, _saved_attrs)
    except Exception:
        pass


#------------------------------------------------------------------
# POSIX 계열 색 초기화
#=> Windows 가 아닌 환경에서 색이 남는 경우를 위한 최소 대응.
#   Windows 에서는 VT 처리가 꺼져 있으면 이스케이프가 글자로 찍히므로 쓰지 않는다.
#------------------------------------------------------------------
def reset_ansi():
    if os.name != "nt" and sys.stdout.isatty():
        sys.stdout.write("\x1b[0m")
        sys.stdout.flush()
