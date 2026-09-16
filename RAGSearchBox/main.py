#------------------------------------------------------------------
# 진입점 (설계서 §8·§10, 원본 MDriveSearchBox.cpp 자리)
#=> 하는 일은 넷뿐이다.
#    1) 명령줄 처리 (--autorun on|off|status 는 상주하지 않고 바로 끝난다)
#    2) 중복 실행 막기 — 뮤텍스. 두 번 뜨면 워커(모델)가 둘이 되고 인덱스 잠금이 충돌한다
#    3) 로그·설정 준비
#    4) tkinter 루트를 숨긴 채 mainloop
#
#   창이 없는 프로그램이지만 tkinter 루트는 필요하다. 답변 창(Toplevel)의 부모이면서
#   50ms 큐 처리(root.after)를 도는 시계 역할을 한다.
#------------------------------------------------------------------

import sys
import tkinter as tk

import app as rsb_app
import autorun
import log as rsb_log
import settings as rsb_settings

MUTEX_NAME = "Local\\RAGSearchBox"
ERROR_ALREADY_EXISTS = 183


#------------------------------------------------------------------
# 중복 실행 막기
#=> 이름 있는 뮤텍스를 만들어 본다. 이미 있으면 먼저 뜬 것이 있다는 뜻이다.
#   Local\ 이라 사용자마다 따로 계산된다 — 한 PC 에 여러 사람이 로그인해도 각자 하나씩 뜬다.
#   ⚠️ 돌려받은 핸들을 붙잡고 있어야 한다. 수집되면 뮤텍스가 사라져 막는 뜻이 없어진다.
#
# -in: 없음
#
# -out: (첫 실행이면 핸들, 아니면 None)
# -out: error = 없음 (뮤텍스를 못 만들면 막지 않고 진행한다 — 상주를 아예 못 하는 것보다 낫다)
#------------------------------------------------------------------
def acquire_single_instance():
    try:
        import win32api
        import win32event
        import winerror
        h = win32event.CreateMutex(None, False, MUTEX_NAME)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return None
        return h
    except Exception:
        return True


#------------------------------------------------------------------
# 시작하기
#
# -in: argv = 명령줄 인자(프로그램 이름 제외)
#
# -out: 종료 코드 (0=정상, 1=이미 실행 중, 2=--autorun 실패)
# -out: error = 예상 못 한 예외는 로그에 남기고 3 을 돌려준다
#------------------------------------------------------------------
def main(argv):
    # --autorun 은 상주하지 않는다. 설치 스크립트나 사용자가 한 번 부르고 끝내는 명령이다.
    if argv and argv[0] == "--autorun":
        return autorun.run_cli(argv[1] if len(argv) > 1 else "")

    s = rsb_settings.load()
    log = rsb_log.setup(s.log_level)
    log = rsb_log.get("main")

    handle = acquire_single_instance()
    if handle is None:
        log.info("이미 실행 중이라 종료한다 (뮤텍스 %s)", MUTEX_NAME)
        return 1

    log.info("RAGSearchBox 시작 — 설정 %s", s.ini_path)

    root = tk.Tk()
    root.withdraw()                    # 본체 창은 쓰지 않는다. 답변 창의 부모이자 시계 역할만 한다
    application = rsb_app.App(root, s)
    try:
        application.start()
        root.mainloop()
    except KeyboardInterrupt:
        log.info("Ctrl+C 로 종료한다")
    except Exception:
        log.exception("예상 못 한 예외로 종료한다")
        try:
            application.quit()
        except Exception:
            pass
        return 3
    finally:
        application.quit()
        # handle 을 여기까지 살려 두어야 실행 내내 뮤텍스가 유지된다
        del handle
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
