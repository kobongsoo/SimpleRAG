#------------------------------------------------------------------
# 오류 상황 재현 확인 (설계서 §10)
#=> 잘 될 때가 아니라 고장났을 때 사용자에게 무엇이 보이는지를 본다.
#   상주 프로그램은 고장이 나도 조용히 계속 돌기 때문에, 알려 주지 않으면
#   사용자는 "왜 안 뜨지?" 만 하다 만다.
#
#   여기서 보는 것 (탐색기와 모델은 쓰지 않는다)
#    A) SimpleRAG 실행 파일을 못 찾음 → 감시는 계속, 질문하면 창에 안내
#    B) 범위 폴더 미설정          → 워커를 올리지 않음, 트레이에 안내
#    C) 워커가 답변 중에 죽음      → 창에 "다시 올립니다", 자동 재시작
#    D) 이벤트 처리 중 예외        → 프로그램이 죽지 않고 다음 이벤트를 계속 처리
#    E) 트레이 "모델 내리기"       → 워커가 내려가 인덱스 잠금이 풀림
#
#   워커 자체의 고장(시간 초과·설정 오류·잠금·재시작 한도)은 tests/test_rag_worker.py 가 본다.
#   진짜 exe 로 하는 잠금 충돌 확인은 따로 한다.
#
#   사용: .venv\\Scripts\\python.exe RAGSearchBox\\tests\\manual_errors.py
#------------------------------------------------------------------
import os
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as rsb_app  # noqa: E402
import log as rsb_log  # noqa: E402
import settings as rsb_settings  # noqa: E402
from rag_worker import STOPPED, RagWorker  # noqa: E402

FAKE = [sys.executable, os.path.join(HERE, "fake_chat.py")]
RESULTS = []


#------------------------------------------------------------------
# 결과 한 줄 적기
#
# -in: name = 확인한 것
# -in: ok   = 통과 여부
# -in: detail = 실패했을 때 보여 줄 값
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  -> " + str(detail)[:200]))


#------------------------------------------------------------------
# 임시 설정 파일 만들기
#=> 진짜 RAGSearchBox.ini 를 건드리지 않는다.
#
# -in: folders = [Scope] Folders 에 적을 값
# -in: exe     = [SimpleRAG] SimpleRagExe 에 적을 값
#
# -out: (INI 경로, 범위 폴더 경로)
# -out: error = 없음
#------------------------------------------------------------------
def make_ini(folders=None, exe=""):
    d = tempfile.mkdtemp(prefix="rsb_err_")
    scope_dir = os.path.join(d, "범위폴더")
    os.makedirs(scope_dir, exist_ok=True)
    path = os.path.join(d, "RAGSearchBox.ini")
    body = ("[SimpleRAG]\nSimpleRagExe = {}\n"
            "[Worker]\nStartMode = lazy\nIdleUnloadMin = 0\n"
            "[Trigger]\nPrefix = ?\nMinChars = 2\nDedupSec = 0\n"
            "[Scope]\nFolders = {}\n"
            "[Log]\nLevel = DIAG\n").format(
        exe, scope_dir if folders is None else folders)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path, scope_dir


#------------------------------------------------------------------
# App 한 벌 만들기 (감시 스레드는 띄우지 않는다)
#=> 탐색기 감지는 S5 에서 따로 확인했다. 여기서는 질문이 들어온 뒤부터를 본다.
#
# -in: ini      = 설정 파일 경로
# -in: fake_env = 가짜 워커에 줄 환경변수(없으면 진짜 탐색 결과를 그대로 쓴다)
#
# -out: (root, app)
# -out: error = 없음
#------------------------------------------------------------------
def make_app(ini, fake_env=None):
    s = rsb_settings.load(ini)
    root = tk.Tk()
    root.withdraw()
    a = rsb_app.App(root, s)
    a.monitor.start = lambda: None
    a.monitor.stop = lambda: None
    if fake_env is not None:
        for k, v in fake_env.items():
            os.environ[k] = v
        a.worker = RagWorker(FAKE, lambda ev: a.q.put(ev), idle_unload_min=0, tick_s=0.05)
        a.worker_error = None
    return root, a


#------------------------------------------------------------------
# 잠깐 돌리기
#=> tkinter 이벤트를 실제로 돌려야 큐가 비워지고 창이 갱신된다.
#
# -in: root    = tk 루트
# -in: seconds = 돌릴 시간
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def spin(root, seconds):
    root.after(int(seconds * 1000), root.quit)
    root.mainloop()


#------------------------------------------------------------------
# A) SimpleRAG 실행 파일을 못 찾았을 때
#=> 프로그램은 계속 살아 있어야 하고, 질문했을 때 창에 이유가 보여야 한다.
#   여기서 조용히 아무 일도 안 하면 사용자는 원인을 알 길이 없다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def case_a_no_exe():
    print("\n[A] SimpleRAG 실행 파일 없음")
    ini, scope_dir = make_ini(exe=r"D:\없는폴더\simplerag.exe")
    root, a = make_app(ini)
    record("A1 워커를 만들지 않았다", a.worker is None)
    record("A2 이유를 안내 문구로 들고 있다",
           a.worker_error and "찾지" in a.worker_error or "없습니다" in (a.worker_error or ""),
           a.worker_error)
    a.start()
    a.q.put(("query", "?연차 이월 기준", [scope_dir], None))
    spin(root, 2.0)
    w = a.window
    record("A3 질문하면 창이 뜬다", w.visible)
    status = w.lbl_status.cget("text") if w.win else "창 없음"
    record("A4 창에 원인이 보인다",
           "simplerag" in status.lower() and "없습니다" in status, status)
    record("A5 프로그램은 계속 살아 있다", not a._quitting)
    a.quit()
    root.destroy()


#------------------------------------------------------------------
# B) 범위 폴더가 비었을 때
#=> 아무 데서도 동작하지 않아야 하고, 모델도 올리지 않아야 한다(메모리 2.5GB).
#   트레이 글에 무엇을 고쳐야 하는지가 나와야 한다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def case_b_no_scope():
    print("\n[B] 범위 폴더 미설정")
    ini, _ = make_ini(folders="")
    root, a = make_app(ini, fake_env={})
    started = []
    a.worker.ensure_started = lambda: started.append(1)
    a.start()
    spin(root, 2.5)
    record("B1 범위가 쓸 수 없는 상태로 판정된다", not a.scope.usable())
    record("B2 모델을 올리지 않았다", not started)
    record("B3 트레이 글에 고칠 방법이 나온다",
           "Folders" in a._last_tooltip, a._last_tooltip)
    a.quit()
    root.destroy()


#------------------------------------------------------------------
# C) 답변 중에 워커가 죽었을 때
#=> 창에 "다시 올립니다" 가 보이고, 워커는 스스로 다시 떠야 한다.
#   죽게 만든 질문은 다시 보내지 않는다(같은 고장을 반복하지 않게).
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def case_c_worker_dies():
    print("\n[C] 답변 중 워커가 죽음")
    ini, scope_dir = make_ini()
    root, a = make_app(ini, fake_env={"FAKE_DIE_ON": "죽는 질문"})
    a.start()
    a.q.put(("query", "?죽는 질문", [scope_dir], None))
    spin(root, 6.0)
    w = a.window
    status = w.lbl_status.cget("text") if w.win else ""
    record("C1 창에 종료 사실이 보인다", "종료" in status or "다시" in status, status)
    record("C2 워커가 다시 떴다", a.worker.state != STOPPED, a.worker.status_text())
    os.environ.pop("FAKE_DIE_ON", None)
    a.quit()
    root.destroy()


#------------------------------------------------------------------
# D) 이벤트 처리 중 예외가 났을 때
#=> 큐 처리에서 예외가 새어 나가면 50ms 루프가 끊겨 프로그램이 벙어리가 된다.
#   일부러 깨진 이벤트를 넣고, 그 뒤 정상 이벤트가 처리되는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def case_d_bad_event():
    print("\n[D] 이벤트 처리 중 예외")
    ini, scope_dir = make_ini()
    root, a = make_app(ini, fake_env={})
    a.start()
    a.q.put(("query",))                     # 인자가 모자란 이벤트 — 처리 중 예외가 난다
    a.q.put(("evidence", None, None))       # 창이 없을 때의 근거 이벤트
    a.q.put(("query", "?정상 질문", [scope_dir], None))
    spin(root, 3.0)
    record("D1 예외 뒤에도 다음 질문이 처리된다",
           a.window.visible and a.window.question == "정상 질문",
           a.window.question)
    a.quit()
    root.destroy()


#------------------------------------------------------------------
# E) 트레이 "모델 내리기"
#=> 인덱싱하려면 인덱스 잠금을 놓아야 한다. 메뉴가 실제로 워커를 내리는지 본다.
#
# -in: 없음
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def case_e_unload():
    print("\n[E] 트레이 모델 내리기")
    ini, scope_dir = make_ini()
    root, a = make_app(ini, fake_env={})
    a.start()
    a.q.put(("query", "?연차", [scope_dir], None))
    spin(root, 3.0)
    record("E1 먼저 워커가 올라와 있다", a.worker.state != STOPPED, a.worker.status_text())
    a.q.put(("tray", "unload"))
    spin(root, 2.0)
    record("E2 메뉴로 워커가 내려갔다", a.worker.state == STOPPED, a.worker.status_text())
    a.quit()
    root.destroy()


def main():
    rsb_log.setup("DIAG")
    for fn in (case_a_no_exe, case_b_no_scope, case_c_worker_dies,
               case_d_bad_event, case_e_unload):
        try:
            fn()
        except Exception as ex:
            record("{} 실행 자체가 실패".format(fn.__name__), False, ex)
    n = sum(1 for _, ok in RESULTS if ok)
    print("")
    print("=== 오류 상황 재현: {}/{} 통과 ===".format(n, len(RESULTS)))
    return 0 if n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
