#------------------------------------------------------------------
# 폴더 패널 눈으로 확인하기 (설계서 §18) — 탐색기 없이 화면만
#=> 가짜 워커에 질문을 던져 패널이 어떻게 그려지는지 본다.
#   탐색기를 따라다니는 부분은 tests 밖의 확인 스크립트에서 진짜 창으로 본다.
#
#   확인할 것
#    1) 위에 폴더, 가운데 대화, 아래 입력 칸이 있는가
#    2) 질문을 보내면 근거 → 답변 → 소요 순서로 쌓이는가
#    3) 여러 번 물으면 아래로 쌓이고 자동으로 맨 아래를 보여 주는가
#    4) 뜰 때 포커스를 뺏지 않는가 (사용자가 누르면 그때 들어간다)
#
#   사용: .venv\\Scripts\\python.exe RAGSearchBox\\tests\\manual_panel.py [--seconds 20]
#------------------------------------------------------------------
import argparse
import os
import queue
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chat_panel  # noqa: E402
import log as rsb_log  # noqa: E402
import settings as rsb_settings  # noqa: E402
from rag_worker import RagWorker  # noqa: E402


#------------------------------------------------------------------
# 확인 실행
#
# -in: seconds = 몇 초 뒤에 끝낼지
#
# -out: 통과하면 0, 아니면 1
# -out: error = 없음 (예외는 그대로 보이게 둔다)
#------------------------------------------------------------------
def main(seconds):
    rsb_log.setup("DIAG")
    s = rsb_settings.Settings()

    root = tk.Tk()
    root.geometry("420x160+60+60")
    root.title("가짜 다른 창 (여기에 글을 쳐 보세요)")
    tk.Text(root, height=6).pack(fill="both", expand=True)
    root.update()

    events = queue.Queue()
    fake = [sys.executable, os.path.join(HERE, "fake_chat.py")]
    os.environ["FAKE_ANSWER_DELAY"] = "0.6"
    worker = RagWorker(fake, events.put, idle_unload_min=0, tick_s=0.05)
    worker.start()

    panel = chat_panel.ChatPanel(root, s, on_ask=worker.ask)
    # 탐색기 대신 이 시험 창에 붙인다(따라다니기·자리 만들기는 여기서 보지 않는다)
    s.shrink_explorer = False
    panel.show_for(chat_panel.hwnd_of(root), r"D:\Project\SimpleRAG\sample")

    results = []

    #--------------------------------------------------------------
    # 큐에서 꺼내 패널에 반영
    #--------------------------------------------------------------
    def pump():
        while True:
            try:
                ev = events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "state":
                panel.set_state({"starting": "모델 준비 중", "busy": "검색 중"}.get(ev[1], ""))
            elif kind == "evidence":
                panel.set_evidence(ev[1], ev[2])
            elif kind == "token":
                panel.append_token(ev[1])
            elif kind == "done":
                panel.finish(ev[1])
            elif kind == "raw":
                panel.show_raw(ev[1])
            elif kind == "error":
                panel.show_error(ev[1])
        root.after(50, pump)

    #--------------------------------------------------------------
    # 입력 칸에 글을 넣고 보내기 (사람이 친 것처럼)
    #--------------------------------------------------------------
    def ask(text):
        panel.txt_input.insert("1.0", text)
        panel._send()

    #--------------------------------------------------------------
    # 자체 점검
    #--------------------------------------------------------------
    def check():
        import ctypes
        user32 = ctypes.windll.user32
        h = chat_panel.hwnd_of(panel.win)
        results.append(("패널이 보인다", bool(panel.win.winfo_ismapped())))
        results.append(("폴더 이름이 보인다", "sample" in panel.lbl_folder.cget("text")))
        results.append(("대화가 두 마디 쌓였다", len(panel.turns) == 2))
        t = panel.turns[-1] if panel.turns else None
        results.append(("마지막 마디에 근거가 있다", bool(t and t.docs)))
        answer = t.txt_answer.get("1.0", "end").strip() if (t and t.txt_answer) else ""
        results.append(("마지막 마디에 답변이 들어왔다", len(answer) > 0))
        results.append(("입력 칸이 비워졌다", not panel.txt_input.get("1.0", "end").strip()))
        results.append(("근거 파일 보기 단추가 열렸다",
                        str(panel.btn_files.cget("state")) == "normal"))
        results.append(("패널이 전경 창을 뺏지 않았다", user32.GetForegroundWindow() != h))

        print("")
        print("=== 패널 자체 점검 ===")
        for name, ok in results:
            print(("  PASS  " if ok else "  FAIL  ") + name)
        print("결과: {}/{} 통과".format(sum(1 for _, ok in results if ok), len(results)))

    ms = int(seconds * 1000)
    root.after(600, lambda: ask("연차 이월 기준은 어떻게 되나요"))
    root.after(ms // 2, lambda: ask("경조사 지원 금액은 얼마인가요"))
    root.after(ms - 600, check)
    root.after(ms, root.quit)
    root.after(50, pump)

    print("패널을 띄웁니다. {}초 뒤 닫힙니다. 왼쪽 창에 글을 쳐 보세요(포커스를 뺏지 않아야 합니다).".format(seconds))
    try:
        root.mainloop()
    finally:
        panel.close()
        worker.shutdown()
        os.environ.pop("FAKE_ANSWER_DELAY", None)
    return 0 if results and all(ok for _, ok in results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20)
    sys.exit(main(ap.parse_args().seconds))
