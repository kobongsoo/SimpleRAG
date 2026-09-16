#------------------------------------------------------------------
# 로그인 시 자동 시작 (설계서 §12, 결정 D5)
#=> HKCU 의 Run 키에 등록한다. 사용자 단위라 관리자 권한이 필요 없고 다른 사용자에게 영향이 없다.
#   기본은 등록하지 않는다 — 설치할 때 --autorun on 으로 켜거나 트레이 메뉴로 켠다.
#
#   경로가 바뀌어도(폴더를 옮겨도) 이미 등록돼 있으면 시작할 때 조용히 고쳐 준다.
#   등록이 아예 없으면 새로 만들지 않는다 — 사용자가 끈 설정을 되살리면 안 되기 때문이다.
#------------------------------------------------------------------

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RAGSearchBox"
BOX_WAIT_S = 20          # 알림 창을 최대 이만큼만 기다린다(그 뒤엔 끝내고 창도 닫는다)


#------------------------------------------------------------------
# 결과 알리기 (창 없는 exe 에서도 보이게)
#=> RAGSearchBox.exe 는 --windowed 로 묶여 콘솔이 없다. 그냥 print 하면 아무 데도
#   보이지 않는다. 그래서 순서대로 시도한다.
#    1) 항상 로그에 남긴다 — 나중에 확인할 수 있는 유일한 기록이다
#    2) 나를 실행한 콘솔(cmd 창)에 붙어서 거기에 쓴다 — 사람이 명령을 친 경우
#    3) 붙을 콘솔이 없고 상태를 바꾸는 명령(on/off)이면 알림 창을 띄운다
#
#   status 는 붙을 콘솔이 없어도 창을 띄우지 않는다. status 의 답은 종료 코드
#   (0=등록됨, 1=안 됨)로 이미 전해지기 때문이다.
#
#   ⚠️ 알림 창은 사람이 닫을 때까지 프로그램을 멈춰 세운다. 콘솔 없이 스크립트에서
#      부르면 영원히 걸릴 수 있어(실제로 겪었다), 딸림 스레드에서 띄우고 최대
#      BOX_WAIT_S 초만 기다린다. 그 시간이 지나면 프로그램이 끝나면서 창도 같이 닫힌다.
#
# -in: text  = 보여 줄 글
# -in: modal = 콘솔이 없을 때 알림 창까지 띄울지(on/off 는 True, status 는 False)
#
# -out: 없음
# -out: error = 없음 (알리지 못해도 종료 코드는 그대로다)
#------------------------------------------------------------------
def _out(text, modal=False):
    try:
        import log as rsb_log
        rsb_log.get("autorun").info("%s", text)
    except Exception:
        pass

    if not getattr(sys, "frozen", False):
        print(text)
        return
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            with open("CONOUT$", "w", encoding="utf-8") as con:
                con.write(text)
                con.write(os.linesep)
            return
        if modal:
            import threading
            # MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST
            t = threading.Thread(
                target=lambda: ctypes.windll.user32.MessageBoxW(
                    0, text, "RAGSearchBox", 0x40 | 0x10000 | 0x40000),
                daemon=True)
            t.start()
            t.join(timeout=BOX_WAIT_S)
    except Exception:
        pass


#------------------------------------------------------------------
# 지금 실행 파일 경로
#=> exe 로 빌드했으면 그 exe, 소스로 돌리면 "python main.py" 형태가 된다.
#
# -in: 없음
#
# -out: 레지스트리에 넣을 명령 문자열(따옴표로 감싼 절대 경로)
# -out: error = 없음
#------------------------------------------------------------------
def command_value():
    if getattr(sys, "frozen", False):
        return '"{}"'.format(os.path.abspath(sys.executable))
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return '"{}" "{}"'.format(os.path.abspath(sys.executable), main_py)


#------------------------------------------------------------------
# 등록된 값 읽기
#
# -in: 없음
#
# -out: 등록된 명령 문자열 또는 None(등록 안 됨)
# -out: error = 없음 (읽기 실패도 None)
#------------------------------------------------------------------
def current_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return v
    except FileNotFoundError:
        return None
    except OSError:
        return None


#------------------------------------------------------------------
# 지금 자동 시작이 켜져 있는가
#
# -in: 없음
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def is_enabled():
    return current_value() is not None


#------------------------------------------------------------------
# 자동 시작 켜기
#
# -in: 없음
#
# -out: (True, 등록한 값)
# -out: error = 실패하면 (False, 사유). 그룹 정책으로 막힌 경우가 있다
#------------------------------------------------------------------
def enable():
    value = command_value()
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, value)
        return True, value
    except OSError as e:
        return False, "자동 시작을 등록하지 못했습니다: {}".format(e)


#------------------------------------------------------------------
# 자동 시작 끄기
#
# -in: 없음
#
# -out: (True, 안내) — 원래 없었어도 성공으로 본다
# -out: error = 실패하면 (False, 사유)
#------------------------------------------------------------------
def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        return True, "자동 시작을 껐습니다"
    except FileNotFoundError:
        return True, "자동 시작이 등록돼 있지 않았습니다"
    except OSError as e:
        return False, "자동 시작을 끄지 못했습니다: {}".format(e)


#------------------------------------------------------------------
# 옮긴 폴더 보정
#=> 이미 등록돼 있는데 경로가 지금 실행 파일과 다르면 현재 경로로 고친다.
#   등록이 없으면 아무것도 하지 않는다(사용자가 끈 것을 되살리지 않는다).
#
# -in: 없음
#
# -out: 고쳤으면 새 값, 아니면 None
# -out: error = 없음 (쓰기 실패는 None)
#------------------------------------------------------------------
def fix_path_if_moved():
    cur = current_value()
    if cur is None:
        return None
    want = command_value()
    if cur.strip().lower() == want.strip().lower():
        return None
    ok, _ = enable()
    return want if ok else None


#------------------------------------------------------------------
# 명령줄에서 쓰기 (--autorun on|off|status)
#=> main.py 가 인자를 보고 이 함수를 부른다. 상주하지 않고 바로 끝난다.
#
# -in: action = "on" | "off" | "status"
#
# -out: 종료 코드 (status 는 등록됨 0 / 안 됨 1, 실패는 2)
# -out: error = 알 수 없는 동작이면 2
#------------------------------------------------------------------
def run_cli(action):
    action = (action or "").lower()
    if action == "on":
        ok, msg = enable()
        _out("자동 시작 등록: {}".format(msg) if ok else msg, modal=True)
        return 0 if ok else 2
    if action == "off":
        ok, msg = disable()
        _out(msg, modal=True)
        return 0 if ok else 2
    if action == "status":
        v = current_value()
        _out("등록됨: {}".format(v) if v else "등록되어 있지 않습니다")
        return 0 if v else 1
    _out("사용법: RAGSearchBox.exe --autorun on|off|status")
    return 2
