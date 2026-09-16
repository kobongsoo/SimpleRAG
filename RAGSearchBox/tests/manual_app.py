#------------------------------------------------------------------
# app 통합 확인 (S6) — 탐색기도 모델도 없이, 엮인 것만 본다
#=> 진짜 탐색기 감지는 S5 에서 확인했고, 진짜 모델은 무겁다. 여기서는 그 사이,
#   즉 "질문이 들어오면 창이 뜨고 워커에 전해지고 이벤트가 창에 반영되는가"를 본다.
#    1) 임시 INI 를 만들어 범위 폴더를 지정한다
#    2) 워커를 가짜(tests/fake_chat.py)로 바꿔 끼운다
#    3) 감시 스레드가 보내는 것과 같은 모양의 질문 이벤트를 큐에 직접 넣는다
#    4) 창·트레이·거르기·종료가 기대대로 도는지 스스로 점검한다
#
#   사용: .venv\Scripts\python.exe RAGSearchBox\tests\manual_app.py [--seconds 14]
#------------------------------------------------------------------
import argparse
import os
import sys
import tempfile
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app as rsb_app  # noqa: E402
import log as rsb_log  # noqa: E402
import settings as rsb_settings  # noqa: E402
from rag_worker import RagWorker  # noqa: E402

INI = """[SimpleRAG]
NoStream = 0
[Worker]
StartMode = lazy
IdleUnloadMin = 0
[Trigger]
Prefix = ?
MinChars = 2
DedupSec = 3
[Scope]
Folders = {folders}
[Log]
Level = DIAG
"""


#------------------------------------------------------------------
# 임시 설정 파일 만들기
#=> 진짜 RAGSearchBox.ini 를 건드리지 않으려고 임시 폴더에 따로 만든다.
#
# -in: 없음
#
# -out: (INI 경로, 범위 폴더 경로)
# -out: error = 없음
#------------------------------------------------------------------
def make_ini():
    d = tempfile.mkdtemp(prefix="rsb_app_")
    scope_dir = os.path.join(d, "범위폴더")
    os.makedirs(scope_dir, exist_ok=True)
    path = os.path.join(d, "RAGSearchBox.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write(INI.format(folders=scope_dir))
    return path, scope_dir


#------------------------------------------------------------------
# 통합 확인 실행
#
# -in: seconds = 몇 초 뒤에 끝낼지
#
# -out: 통과하면 0, 하나라도 실패하면 1
# -out: error = 없음 (예외는 그대로 보이게 둔다)
#------------------------------------------------------------------
def main(seconds):
    ini, scope_dir = make_ini()
    s = rsb_settings.load(ini)
    rsb_log.setup("DIAG")

    root = tk.Tk()
    root.withdraw()
    application = rsb_app.App(root, s)

    # 워커를 가짜로 바꿔 끼운다 — 모델을 올리지 않고 같은 이벤트를 낸다
    fake = [sys.executable, os.path.join(HERE, "fake_chat.py")]
    os.environ["FAKE_ANSWER_DELAY"] = "0.8"
    application.worker = RagWorker(fake, lambda ev: application.q.put(ev),
                                   idle_unload_min=0, tick_s=0.05)
    application.worker_error = None

    # 감시 스레드는 띄우지 않는다(탐색기가 없으니 할 일이 없다). 대신 질문을 직접 넣는다.
    application.monitor.start = lambda: None
    application.monitor.stop = lambda: None

    application.start()

    anchor = (900, 60, 1200, 90)
    results = []

    #--------------------------------------------------------------
    # 감시 스레드가 보내는 것과 같은 모양으로 질문 넣기
    #--------------------------------------------------------------
    def send(text):
        application.q.put(("query", text, [scope_dir], anchor))

    #--------------------------------------------------------------
    # 자체 점검 — 창·거르기·트레이가 기대대로인지 본다
    #--------------------------------------------------------------
    def check():
        w = application.window
        answer = w.txt_answer.get("1.0", "end").strip() if w.win else ""
        results.append(("답변 창이 떠 있다", bool(w.visible)))
        results.append(("질문이 접두어를 뗀 채로 걸렸다", w.question == "연차 이월 기준"))
        results.append(("근거가 그려졌다", bool(w.win) and len(w.frm_evidence.winfo_children()) > 0))
        results.append(("답변 글자가 들어왔다", len(answer) > 0))
        results.append(("소요 시간이 채워졌다", bool(w.win) and bool(w.lbl_timing.cget("text"))))
        results.append(("트레이 상태 글에 워커 상태가 보인다",
                        "RAGSearchBox" in application._last_tooltip
                        and application._last_tooltip != "RAGSearchBox"))
        results.append(("범위 폴더를 읽었다", application.scope.usable()))

    #--------------------------------------------------------------
    # 너무 짧은 질문·중복 질문이 걸러지는지 본다
    #--------------------------------------------------------------
    def check_filters():
        w = application.window
        before = w.question
        send("?ㄱ")                      # MinChars 미만 → 무시되어야 한다
        root.after(400, lambda: results.append(("너무 짧은 질문은 무시", w.question == before)))
        root.after(500, lambda: send("?연차 이월 기준"))   # 방금 한 질문 → 중복
        root.after(900, lambda: results.append(
            ("DedupSec 안 같은 질문은 무시", w.question == before)))

    #--------------------------------------------------------------
    # 결과 출력 후 끝내기
    #--------------------------------------------------------------
    def finish():
        print("")
        print("=== app 통합 점검 ===")
        for name, ok in results:
            print(("  PASS  " if ok else "  FAIL  ") + name)
        n = sum(1 for _, ok in results if ok)
        print("결과: {}/{} 통과".format(n, len(results)))
        application.quit()
        root.quit()

    ms = int(seconds * 1000)
    root.after(600, lambda: send("? 연차 이월 기준"))   # 접두어 뒤 공백도 떼어져야 한다
    root.after(ms - 2600, check)
    root.after(ms - 2400, check_filters)
    root.after(ms - 300, finish)

    print("{}초 동안 확인합니다 (가짜 워커, 모델 없음)".format(seconds))
    try:
        root.mainloop()
    finally:
        os.environ.pop("FAKE_ANSWER_DELAY", None)
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=14)
    sys.exit(main(ap.parse_args().seconds))
