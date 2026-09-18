#------------------------------------------------------------------
# 폴더 감시 시험 (자동 인덱싱 설계서 §4 — AI3)
#=> 판정 규칙(settle)은 가짜 시계·가짜 파일 상태로, 실제 알림은 임시 폴더에서
#   파일을 만들고·고치고·지우고·이름을 바꿔 확인한다.
#------------------------------------------------------------------
import ast
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import watcher  # noqa: E402
from watcher import FolderWatcher, Pending, is_doc_name, settle  # noqa: E402


#------------------------------------------------------------------
# simplerag 의 TEXT_EXTS 를 소스에서 읽기
#=> RAGSearchBox 는 simplerag 를 import 하지 않으므로 파일을 읽어 값만 꺼낸다.
#
# -in: 없음
#
# -out: 확장자 집합
# -out: error = 못 찾으면 AssertionError
#------------------------------------------------------------------
def simplerag_exts():
    src = os.path.join(os.path.dirname(HERE), "src", "simplerag", "index", "indexer.py")
    with open(src, encoding="utf-8-sig") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TEXT_EXTS" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("TEXT_EXTS 를 찾지 못함")


class TestNames(unittest.TestCase):
    #--------------------------------------------------------------
    # 인덱싱 대상 확장자가 simplerag 와 같다
    #=> 다르면 감시가 알린 문서를 워커가 거절하거나, 워커가 다루는 문서를 감시가 놓친다.
    #--------------------------------------------------------------
    def test_exts_match_simplerag(self):
        self.assertEqual(watcher.TEXT_EXTS, simplerag_exts())

    #--------------------------------------------------------------
    # 문서 이름 거르기
    #--------------------------------------------------------------
    def test_doc_names(self):
        for ok in (r"D:\a\규정.docx", r"D:\a\b.HWP", r"D:\a\c.txt"):
            self.assertTrue(is_doc_name(ok), ok)
        for no in (r"D:\a\~$규정.docx", r"D:\a\.~lock.b.docx#", r"D:\a\~WRL0001.tmp",
                   r"D:\a\사진.jpg", r"D:\a\b.docx.tmp", r"D:\a\Thumbs.db", r"D:\a\폴더"):
            self.assertFalse(is_doc_name(no), no)


class TestSettle(unittest.TestCase):
    #--------------------------------------------------------------
    # 가짜 파일 — stat 과 open 결과를 차례로 돌려준다
    #--------------------------------------------------------------
    def fake(self, stats, opens=None):
        stats, opens = list(stats), list(opens or [True])
        return (lambda p: stats.pop(0) if len(stats) > 1 else stats[0],
                lambda p: opens.pop(0) if len(opens) > 1 else opens[0])

    #--------------------------------------------------------------
    # 조용해지기 전에는 보지 않는다
    #--------------------------------------------------------------
    def test_waits_for_quiet(self):
        p = Pending("x.docx", now=100.0)
        st, op = self.fake([(1, 1)])
        self.assertEqual(settle(p, 103.0, 5, stat_fn=st, open_fn=op), "wait")

    #--------------------------------------------------------------
    # 크기·시각이 두 번 같고 열리면 "바뀜"
    #--------------------------------------------------------------
    def test_stable_then_changed(self):
        p = Pending("x.docx", now=100.0)
        st, op = self.fake([(10, 1), (10, 1)])
        self.assertEqual(settle(p, 106.0, 5, 1.0, st, op), "wait")      # 첫 번째 재기
        self.assertEqual(settle(p, 106.5, 5, 1.0, st, op), "wait")      # 아직 1초 안 됨
        self.assertEqual(settle(p, 107.1, 5, 1.0, st, op), "changed")

    #--------------------------------------------------------------
    # 복사 중(크기가 계속 늘어남)이면 계속 기다린다
    #--------------------------------------------------------------
    def test_growing_file_waits(self):
        p = Pending("big.pdf", now=100.0)
        st, op = self.fake([(10, 1), (20, 2), (30, 3), (30, 3)])
        t = 106.0
        verdicts = []
        for _ in range(4):
            verdicts.append(settle(p, t, 5, 1.0, st, op))
            t += 1.1
        self.assertEqual(verdicts, ["wait", "wait", "wait", "changed"])

    #--------------------------------------------------------------
    # 없어졌으면 "사라짐"
    #--------------------------------------------------------------
    def test_removed(self):
        p = Pending("x.docx", now=100.0)
        self.assertEqual(settle(p, 106.0, 5, stat_fn=lambda _: None), "removed")

    #--------------------------------------------------------------
    # 계속 못 열면 간격을 늘려 다시 보다가 버린다
    #--------------------------------------------------------------
    def test_locked_backoff_then_drop(self):
        p = Pending("locked.docx", now=0.0)
        st, op = self.fake([(1, 1)], [False])
        t, seen = 6.0, []
        for _ in range(200):
            v = settle(p, t, 5, 1.0, st, op)
            if v != "wait":
                seen.append((v, round(t)))
                break
            t += 1.0
        self.assertEqual(seen[0][0], "drop")
        # 5s → 15s → 60s 를 다 기다린 뒤라야 버린다
        self.assertGreater(seen[0][1], 80)

    #--------------------------------------------------------------
    # 못 열다가 열리면 "바뀜"
    #--------------------------------------------------------------
    def test_locked_then_opens(self):
        p = Pending("x.docx", now=0.0)
        st, op = self.fake([(1, 1)], [False, True])
        t, v = 6.0, "wait"
        while v == "wait" and t < 100:
            v = settle(p, t, 5, 1.0, st, op)
            t += 1.0
        self.assertEqual(v, "changed")


class TestTick(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rsb_watch_")
        self.got = []
        self.w = FolderWatcher([self.dir], self.got.append, quiet_s=5, rescan_min=0, stable_s=1.0)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text="x"):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    #--------------------------------------------------------------
    # 조용해진 뒤 두 번 재어 같으면 한 묶음으로 넘긴다
    #--------------------------------------------------------------
    def test_changed_after_quiet(self):
        a = self.write("a.docx")
        self.w.note(self.dir, a)
        now = time.monotonic()
        self.assertEqual(self.w.tick(now + 1), [])
        self.w.tick(now + 6)                     # 첫 재기
        out = self.w.tick(now + 7.2)             # 두 번째 — 같다
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].changed, [a])
        self.assertFalse(out[0].rescan)
        self.assertEqual(self.w.tick(now + 20), [])   # 한 번만 넘긴다

    #--------------------------------------------------------------
    # 알림이 또 오면 다시 조용해질 때까지 미룬다
    #--------------------------------------------------------------
    def test_new_event_postpones(self):
        a = self.write("a.docx")
        self.w.note(self.dir, a)
        now = time.monotonic()
        self.w.tick(now + 6)
        self.w._pending[self.dir][os.path.normcase(a)].last = now + 6.5   # 새 알림처럼
        self.assertEqual(self.w.tick(now + 7.2), [])

    #--------------------------------------------------------------
    # 사라진 문서는 removed 로
    #--------------------------------------------------------------
    def test_removed(self):
        a = self.write("a.docx")
        self.w.note(self.dir, a)
        os.remove(a)
        out = self.w.tick(time.monotonic() + 6)
        self.assertEqual(out[0].removed, [a])

    #--------------------------------------------------------------
    # 문서가 아닌 이름은 아예 받지 않는다
    #--------------------------------------------------------------
    def test_ignores_non_docs(self):
        self.w.note(self.dir, os.path.join(self.dir, "~$a.docx"))
        self.w.note(self.dir, os.path.join(self.dir, "~WRL0001.tmp"))
        self.assertEqual(self.w._pending[self.dir], {})

    #--------------------------------------------------------------
    # 숨김 파일은 알리지 않는다
    #--------------------------------------------------------------
    def test_hidden_skipped(self):
        import ctypes
        a = self.write("h.txt")
        ctypes.windll.kernel32.SetFileAttributesW(a, 0x2)
        self.w.note(self.dir, a)
        now = time.monotonic()
        self.w.tick(now + 6)
        self.assertEqual(self.w.tick(now + 7.2), [])

    #--------------------------------------------------------------
    # 전체 대조: 부탁은 조용해진 뒤, 시작·순찰은 바로
    #--------------------------------------------------------------
    def test_rescan(self):
        now = time.monotonic()
        self.w.request_rescan(self.dir, "하위 폴더 변화")
        self.assertEqual(self.w.tick(now + 1), [])
        out = self.w.tick(now + 6)
        self.assertTrue(out[0].rescan)
        self.assertIn("하위 폴더", out[0].reason)
        self.w.request_rescan(self.dir, "시작", immediate=True)
        self.assertTrue(self.w.tick(now + 6.1)[0].rescan)

    #--------------------------------------------------------------
    # 순찰 주기
    #--------------------------------------------------------------
    def test_periodic(self):
        w = FolderWatcher([self.dir], self.got.append, quiet_s=5, rescan_min=10)
        now = time.monotonic()
        w._last_rescan[self.dir] = now
        self.assertEqual(w.tick(now + 60), [])
        out = w.tick(now + 601)
        self.assertTrue(out[0].rescan and out[0].reason == "순찰")


class TestLive(unittest.TestCase):
    #--------------------------------------------------------------
    # 실제 알림 — 만들기·고치기·이름 바꾸기·지우기·하위 폴더
    #=> 조용히 0.4초로 줄여 빨리 돈다. 묶음이 올 때까지 기다린다.
    #--------------------------------------------------------------
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rsb_watch_live_")
        self.got = []
        self.lock = threading.Lock()

        def on_batch(b):
            with self.lock:
                self.got.append(b)

        self.w = FolderWatcher([self.dir], on_batch, quiet_s=0.4, rescan_min=0, stable_s=0.2,
                               tick_s=0.1)
        self.w.start()
        self.wait(lambda b: b.rescan and "시작" in b.reason)

    def tearDown(self):
        self.w.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def wait(self, pred, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for i, b in enumerate(self.got):
                    if pred(b):
                        return self.got.pop(i)
            time.sleep(0.05)
        self.fail("묶음이 오지 않았다: {}".format(self.got))

    def test_live(self):
        a = os.path.join(self.dir, "a.docx")
        with open(a, "w") as f:
            f.write("1")
        self.wait(lambda b: a in b.changed)

        with open(a, "a") as f:
            f.write("2")
        self.wait(lambda b: a in b.changed)

        b2 = os.path.join(self.dir, "b.docx")
        os.rename(a, b2)
        # 옛 이름(사라짐)은 바로, 새 이름은 두 번 재고 나서 — 다른 묶음으로 올 수 있다
        self.wait(lambda b: a in b.removed)
        self.wait(lambda b: b2 in b.changed)

        os.remove(b2)
        self.wait(lambda b: b2 in b.removed)

        # 하위 폴더를 통째로 넣으면(안의 파일 알림이 오지 않을 수 있다) 전체 대조
        src = tempfile.mkdtemp(prefix="rsb_src_")
        with open(os.path.join(src, "c.txt"), "w") as f:
            f.write("c")
        shutil.move(src, os.path.join(self.dir, "sub"))
        self.wait(lambda b: b.rescan and "하위 폴더" in b.reason)

        # 폴더가 그 자리에서 사라지면(이름 변경·분리) 옛 경로로 알리지 않고, 돌아오면 전체 대조
        import watcher as wmod
        old_retry, wmod.OFFLINE_RETRY_S = wmod.OFFLINE_RETRY_S, 1
        try:
            away = self.dir + "_away"
            os.rename(self.dir, away)
            time.sleep(2.5)
            with open(os.path.join(away, "옮긴뒤.docx"), "w") as f:
                f.write("x")
            time.sleep(1.5)
            os.rename(away, self.dir)
            self.wait(lambda b: b.rescan and "다시 연결" in b.reason, timeout=12)
            with self.lock:
                self.assertFalse([b for b in self.got if b.changed or b.removed], self.got)
        finally:
            wmod.OFFLINE_RETRY_S = old_retry

        # 문서 아닌 파일은 묶음을 만들지 않는다
        with open(os.path.join(self.dir, "x.jpg"), "w") as f:
            f.write("x")
        time.sleep(1.0)
        with self.lock:
            self.assertFalse([b for b in self.got if b.changed or b.removed], self.got)


class TestDispatch(unittest.TestCase):
    #--------------------------------------------------------------
    # 알림 처리 규칙 — 0바이트(버퍼 넘침)·하위 폴더·파일
    #=> 실제로 넘치게 만드는 시험은 읽는 속도에 달려 들쭉날쭉했다(따로 돌리면 넘치고
    #   전체 시험 안에서는 안 넘쳤다). 그래서 판단 부분을 직접 시험한다.
    #--------------------------------------------------------------
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rsb_watch_dp_")
        self.w = FolderWatcher([self.dir], lambda b: None, quiet_s=5, rescan_min=0)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_overflow_means_rescan(self):
        self.w.dispatch(self.dir, False, 0, [])
        out = self.w.tick(time.monotonic() + 6)
        self.assertTrue(out and out[0].rescan and "넘침" in out[0].reason, out)

    def test_dir_event_means_rescan(self):
        self.w.dispatch(self.dir, True, 40, [])
        out = self.w.tick(time.monotonic() + 6)
        self.assertTrue(out and out[0].rescan and "하위 폴더" in out[0].reason, out)

    def test_file_records_are_noted(self):
        self.w.dispatch(self.dir, False, 80, [(1, "a.docx"), (1, "~$a.docx"), (3, os.path.join("sub", "b.pdf"))])
        keys = sorted(os.path.basename(p.path) for p in self.w._pending[self.dir].values())
        self.assertEqual(keys, ["a.docx", "b.pdf"])


if __name__ == "__main__":
    unittest.main()
