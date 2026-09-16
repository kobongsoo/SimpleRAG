#------------------------------------------------------------------
# 답변 창·트레이 눈으로 확인하기 (자동 시험이 아니라 수동 확인용)
#=> 가짜 워커에 질문을 던져 실제 이벤트로 창을 그려 본다. 모델은 쓰지 않는다.
#   확인할 것
#    1) 근거가 답변보다 먼저 뜨는가
#    2) 답변이 한 조각씩 흘러 들어오는가
#    3) 창이 떠도 포커스를 뺏지 않는가 (다른 창에 계속 글자를 칠 수 있어야 한다)
#    4) 트레이 아이콘의 상태 글과 메뉴가 나오는가
#
#   사용: .venv\Scripts\python.exe RAGSearchBox\tests\manual_ui.py [--seconds 20]
#------------------------------------------------------------------
import argparse
import os
import queue
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import log as rsb_log  # noqa: E402
import settings as rsb_settings  # noqa: E402
from answer_window import AnswerWindow  # noqa: E402
from rag_worker import RagWorker  # noqa: E402
from tray import Tray  # noqa: E402


#------------------------------------------------------------------
# 수동 확인 실행
#=> 가짜 워커를 띄우고 질문 두 건을 던진 뒤, 정해진 시간이 지나면 스스로 닫는다.
#
# -in: seconds = 몇 초 뒤에 자동으로 끝낼지
#
# -out: 0
# -out: error = 없음 (예외는 그대로 보이게 둔다 — 수동 확인용이다)
#------------------------------------------------------------------
def main(seconds):
    rsb_log.setup("INFO")
    s = rsb_settings.Settings()

    root = tk.Tk()
    root.withdraw()
    win = AnswerWindow(root, s)
    events = queue.Queue()

    fake = [sys.executable, os.path.join(HERE, "fake_chat.py")]
    os.environ["FAKE_ANSWER_DELAY"] = "1.0"      # 근거가 먼저 뜨는지 보이도록 늦춘다
    worker = RagWorker(fake, events.put, idle_unload_min=0, tick_s=0.05)
    worker.start()

    tray = Tray(lambda name: events.put(("tray", name)))
    tray.start()
    tray.wait_ready()

    #--------------------------------------------------------------
    # 큐에서 이벤트를 꺼내 화면에 반영 (메인 스레드)
    #--------------------------------------------------------------
    def pump():
        while True:
            try:
                ev = events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "state":
                tray.set_tooltip("RAGSearchBox — " + worker.status_text())
                if ev[1] == "starting":
                    win.set_status("모델 준비 중 (첫 질문)")
                elif ev[1] == "busy":
                    win.set_status("검색 중")
            elif kind == "evidence":
                import time as _t
                checks["evidence_t"] = checks["evidence_t"] or _t.monotonic()
                win.set_evidence(ev[1], ev[2])
            elif kind == "token":
                import time as _t
                checks["first_token_t"] = checks["first_token_t"] or _t.monotonic()
                win.append_token(ev[1])
            elif kind == "done":
                win.finish(ev[1])
            elif kind == "raw":
                win.show_raw(ev[1])
            elif kind == "error":
                win.show_error(ev[1])
            elif kind == "tray":
                print("트레이 메뉴 선택:", ev[1])
                if ev[1] == "exit":
                    root.quit()
        root.after(50, pump)

    #--------------------------------------------------------------
    # 질문 한 건 던지기
    #--------------------------------------------------------------
    def ask(q, anchor):
        win.show(q, anchor=anchor, status="검색 중")
        worker.ask(q)

    checks = {"evidence_t": None, "first_token_t": None}

    #--------------------------------------------------------------
    # 자체 점검 — 사람이 못 보는 것(스타일·전경 창)을 프로그램이 확인한다
    #=> 창이 실제로 보이는지, 활성화되지 않는 창인지, 전경을 뺏지 않았는지,
    #   근거가 답변보다 먼저 왔는지를 보고 PASS/FAIL 로 찍는다.
    #
    # -in: 없음
    #
    # -out: 없음 (결과는 화면에 출력)
    # -out: error = 없음
    #--------------------------------------------------------------
    def selfcheck():
        import ctypes
        user32 = ctypes.windll.user32
        results = []
        w = win.win
        hwnd = user32.GetParent(w.winfo_id()) or w.winfo_id()
        ex = user32.GetWindowLongW(hwnd, -20)

        results.append(("창이 보인다", bool(w.winfo_ismapped())))
        results.append(("활성화되지 않는 창(WS_EX_NOACTIVATE)", bool(ex & 0x08000000)))
        results.append(("항상 위(topmost)", bool(ex & 0x00000008)))
        results.append(("전경 창을 뺏지 않았다", user32.GetForegroundWindow() != hwnd))
        results.append(("근거 위젯이 그려졌다", len(win.frm_evidence.winfo_children()) > 0))
        both = checks["evidence_t"] and checks["first_token_t"]
        results.append(("근거가 답변보다 먼저 왔다",
                        bool(both) and checks["evidence_t"] < checks["first_token_t"]))
        answer = win.txt_answer.get("1.0", "end").strip()
        results.append(("답변 글자가 들어왔다", len(answer) > 0))
        results.append(("소요 시간이 채워졌다", bool(win.lbl_timing.cget("text"))))
        results.append(("트레이 아이콘 등록됨", bool(tray._added)))

        print("")
        print("=== 자체 점검 ===")
        for name, ok in results:
            print(("  PASS  " if ok else "  FAIL  ") + name)
        print("결과: {}/{} 통과".format(sum(1 for _, ok in results if ok), len(results)))

    root.after(300, lambda: ask("연차 이월 기준", (900, 60, 1200, 90)))
    root.after(int(seconds * 500), lambda: ask("경조사 지원 금액", (900, 60, 1200, 90)))
    root.after(int(seconds * 1000) - 600, selfcheck)
    root.after(int(seconds * 1000), root.quit)
    root.after(50, pump)

    print("답변 창을 띄웁니다. {}초 뒤 자동으로 닫힙니다.".format(seconds))
    print("확인: 근거가 먼저 뜨는지 · 답변이 흘러오는지 · 다른 창에 계속 글을 쓸 수 있는지")
    try:
        root.mainloop()
    finally:
        tray.stop()
        worker.shutdown()
        os.environ.pop("FAKE_ANSWER_DELAY", None)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    sys.exit(main(ap.parse_args().seconds))
