#------------------------------------------------------------------
# 자동 인덱싱 진행 시험 (자동 인덱싱 설계서 §8 — AI4)
#=> 워커는 가짜(같은 프로세스 안), 워커가 내려가 있을 때의 index 명령은
#   tests/fake_index_cli.py 를 진짜 하위 프로세스로 돌려 확인한다.
#------------------------------------------------------------------
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import settings as rsb_settings  # noqa: E402
from index_jobs import IndexJobs  # noqa: E402
from watcher import Batch  # noqa: E402

ROOT = r"D:\문서함"


#------------------------------------------------------------------
# 가짜 워커 — 명령을 받으면 handler 가 만든 결과를 곧바로 돌려준다
#
# -필드: sent  = 받은 명령 줄들
# -필드: avail = availability() 가 돌려줄 값
# -필드: idle  = 마지막 질문 뒤 지난 초
#------------------------------------------------------------------
class FakeWorker:
    def __init__(self, plan=None):
        self.sent = []
        self.avail = "up"
        self.idle = 999
        self.holds = []
        self.plan = plan or {"ok": True, "add": [], "modify": [], "remove": [],
                             "delete_blocked": "", "bm25_stale": False}
        self.doc_result = {"ok": True, "result": "modified", "chunks": 3}
        self.outside = {"ok": True, "outside": [], "total": 10, "delete_blocked": ""}

    def availability(self):
        return self.avail

    def seconds_since_ask(self):
        return self.idle

    def hold(self, reason):
        self.holds.append(reason)

    def command(self, line, cb):
        self.sent.append(line)
        cmd = line.split(" ", 1)[0]
        if cmd == "/index-plan":
            cb(dict(self.plan))
        elif cmd == "/index-outside":
            cb(dict(self.outside))
        elif cmd == "/remove-doc":
            cb({"ok": True, "result": "removed", "removed": 2})
        elif cmd == "/bm25":
            cb({"ok": True, "chunks": 10})
        else:
            cb(dict(self.doc_result))

    def ops(self):
        return [ln.split(" ", 1)[0] for ln in self.sent]


def make(worker, **kw):
    s = rsb_settings.Settings()
    s.autoindex_ask_above = kw.pop("ask_above", 50)
    s.autoindex_bm25_idle_s = kw.pop("bm25_idle", 10)
    notes = []
    jobs = IndexJobs(s, worker, [ROOT], kw.pop("base_cmd", []), post=lambda fn: fn(),
                     notify=lambda k, m: notes.append((k, m)))
    return jobs, notes


def spin(jobs, n=30):
    for _ in range(n):
        jobs.tick()


class TestWithWorker(unittest.TestCase):
    #--------------------------------------------------------------
    # 감시 묶음 → 대조 → 문서마다 명령 → 한가하면 키워드 색인
    #--------------------------------------------------------------
    def test_flow(self):
        w = FakeWorker()
        jobs, notes = make(w)
        spin(jobs)                                # 워커가 떴다 → 밖 문서 확인 + 한 번 대조(바뀐 것 없음)
        self.assertEqual(w.ops(), ["/index-outside", "/index-plan"])
        w.sent.clear()
        w.plan = {"ok": True, "add": [ROOT + r"\새.docx"], "modify": [ROOT + r"\고친.docx"],
                  "remove": [ROOT + r"\지운.docx"], "delete_blocked": "", "bm25_stale": False}
        jobs.on_batch(Batch(ROOT, changed=[ROOT + r"\고친.docx"]))
        spin(jobs)
        self.assertEqual(w.ops(), ["/index-plan", "/remove-doc", "/index-doc", "/index-doc", "/bm25"])
        # 경로는 ASCII JSON 으로 간다(cp949 stdin 에서 깨지지 않게)
        self.assertTrue(all(ln.isascii() for ln in w.sent), w.sent)
        self.assertEqual(json.loads(w.sent[1].split(" ", 1)[1])["path"], ROOT + r"\지운.docx")
        self.assertIn(("doc", "방금 반영(수정): 새.docx"), notes)
        self.assertEqual(jobs.status_text(), "")
        # 3건 반영 → 한 차례 끝에 트레이로 한 번 알린다
        self.assertTrue(any(k == "info" and "3건" in m for k, m in notes), notes)

    #--------------------------------------------------------------
    # 질문이 최근에 있었으면 키워드 색인은 미룬다
    #--------------------------------------------------------------
    def test_bm25_waits_for_idle(self):
        w = FakeWorker({"ok": True, "add": [], "modify": [ROOT + r"\a.docx"], "remove": [],
                        "delete_blocked": "", "bm25_stale": False})
        jobs, _ = make(w)
        w.idle = 2
        spin(jobs)
        self.assertNotIn("/bm25", w.ops())
        w.idle = 30
        spin(jobs)
        self.assertEqual(w.ops().count("/bm25"), 1)

    #--------------------------------------------------------------
    # 새 문서가 한도를 넘으면 넣지 않고 기다린다 → "지금 인덱싱" 이면 넣는다
    #--------------------------------------------------------------
    def test_ask_above(self):
        adds = [ROOT + r"\n%d.docx" % i for i in range(5)]
        w = FakeWorker({"ok": True, "add": adds, "modify": [], "remove": [],
                        "delete_blocked": "", "bm25_stale": False})
        jobs, notes = make(w, ask_above=3)
        spin(jobs)
        self.assertNotIn("/index-doc", w.ops())
        self.assertEqual(jobs.waiting_new, {ROOT: 5})
        self.assertIn("새 문서 5건", jobs.status_text())
        self.assertEqual(sum(1 for k, m in notes if k == "warn"), 1)
        jobs.mark_dirty(ROOT)                     # 또 대조해도 알림은 한 번만
        spin(jobs)
        self.assertEqual(sum(1 for k, m in notes if k == "warn"), 1)
        jobs.run_now()
        spin(jobs)
        self.assertEqual(w.ops().count("/index-doc"), 5)
        self.assertEqual(jobs.waiting_new, {})

    #--------------------------------------------------------------
    # 삭제가 멈췄으면 알리고, "지금 인덱싱" 이면 allow_delete 로 다시 대조
    #--------------------------------------------------------------
    def test_delete_blocked(self):
        w = FakeWorker({"ok": True, "add": [], "modify": [], "remove": [],
                        "delete_blocked": "문서 10건 중 6건이 사라짐", "bm25_stale": False})
        jobs, notes = make(w)
        spin(jobs)
        self.assertIn(ROOT, jobs.waiting_delete)
        self.assertTrue(any(k == "warn" and "6건" in m for k, m in notes))
        w.sent.clear()
        jobs.run_now()
        spin(jobs)
        plan = [ln for ln in w.sent if ln.startswith("/index-plan")][0]
        self.assertTrue(json.loads(plan.split(" ", 1)[1]).get("allow_delete"))

    #--------------------------------------------------------------
    # 옛 워커(명령을 모름)면 멈추고 알린다
    #--------------------------------------------------------------
    def test_old_worker_disables(self):
        w = FakeWorker()
        w.command = lambda line, cb: (w.sent.append(line), cb({"ok": False, "unknown": True}))
        jobs, notes = make(w)
        spin(jobs)
        self.assertEqual(len(w.sent), 1)
        self.assertIn("새로 빌드", jobs.status_text())
        self.assertTrue(any(k == "warn" for k, _ in notes))

    #--------------------------------------------------------------
    # 워커가 도중에 내려가면 그 폴더를 다시 대조하게 둔다
    #--------------------------------------------------------------
    def test_stopped_redirty(self):
        w = FakeWorker({"ok": True, "add": [], "modify": [ROOT + r"\a.docx"], "remove": [],
                        "delete_blocked": "", "bm25_stale": False})
        w.doc_result = {"ok": False, "stopped": True}
        jobs, _ = make(w)
        jobs.tick()          # 지정 폴더 밖 확인
        jobs.tick()          # 대조
        jobs.tick()          # 문서 → stopped
        self.assertIn(ROOT, jobs._dirty)

    #--------------------------------------------------------------
    # 꺼져 있으면 아무것도 보내지 않는다
    #--------------------------------------------------------------
    def test_disabled(self):
        w = FakeWorker()
        jobs, _ = make(w)
        jobs.set_enabled(False)
        jobs.on_batch(Batch(ROOT, rescan=True, reason="시작"))
        spin(jobs)
        self.assertEqual(w.sent, [])
        self.assertEqual(jobs.status_text(), "자동 인덱싱 꺼짐")

    #--------------------------------------------------------------
    # 준비 중이면 기다린다(곧 뜬다)
    #--------------------------------------------------------------
    def test_starting_waits(self):
        w = FakeWorker()
        w.avail = "starting"
        jobs, _ = make(w)
        jobs.on_batch(Batch(ROOT, rescan=True))
        spin(jobs)
        self.assertEqual(w.sent, [])
        self.assertEqual(w.holds, [])


class TestOutside(unittest.TestCase):
    #--------------------------------------------------------------
    # 지정 폴더 밖 문서는 워커가 뜰 때 인덱스에서 뺀다 (인덱싱 폴더 = 패널 폴더)
    #--------------------------------------------------------------
    def test_outside_removed(self):
        w = FakeWorker()
        w.outside = {"ok": True, "outside": [r"D:\다른폴더\a.docx"], "total": 20, "delete_blocked": ""}
        jobs, _ = make(w)
        spin(jobs)
        rm = [ln for ln in w.sent if ln.startswith("/remove-doc")]
        self.assertEqual(len(rm), 1, w.sent)
        arg = json.loads(rm[0].split(" ", 1)[1])
        self.assertEqual(arg["path"], r"D:\다른폴더\a.docx")
        self.assertEqual(arg["outside_of"], [os.path.abspath(ROOT)])

    #--------------------------------------------------------------
    # 많으면 멈추고 묻는다 → "지금 인덱싱" 이면 뺀다
    #--------------------------------------------------------------
    def test_outside_blocked_then_approved(self):
        w = FakeWorker()
        many = [r"D:\다른폴더\%d.docx" % i for i in range(60)]
        w.outside = {"ok": True, "outside": many, "total": 80, "delete_blocked": "60건이 한꺼번에 사라졌습니다"}
        jobs, notes = make(w)
        spin(jobs)
        self.assertFalse([ln for ln in w.sent if ln.startswith("/remove-doc")])
        self.assertIn("(지정 폴더 밖)", jobs.waiting_delete)
        self.assertTrue(any(k == "warn" and "지정 폴더 밖" in m for k, m in notes), notes)
        jobs.run_now()
        spin(jobs, 200)
        self.assertEqual(len([ln for ln in w.sent if ln.startswith("/remove-doc")]), 60)
        self.assertNotIn("(지정 폴더 밖)", jobs.waiting_delete)

    #--------------------------------------------------------------
    # 워커가 다시 뜰 때마다 한 번씩 확인한다
    #--------------------------------------------------------------
    def test_checked_each_time_up(self):
        w = FakeWorker()
        jobs, _ = make(w)
        spin(jobs)
        w.avail = "down"
        jobs.tick()
        w.avail = "up"
        spin(jobs)
        self.assertEqual(w.ops().count("/index-outside"), 2)


class TestWorkerDown(unittest.TestCase):
    #--------------------------------------------------------------
    # 워커가 내려가 있으면 index 명령(진짜 하위 프로세스)을 돌린다
    #=> 워커를 막았다가(hold) 끝나면 푼다. 새 문서 한도·삭제 허락이 인자로 넘어간다.
    #--------------------------------------------------------------
    def setUp(self):
        self.log = os.path.join(tempfile.mkdtemp(prefix="rsb_ij_"), "args.txt")
        os.environ["FAKE_ARGS_LOG"] = self.log
        self.addCleanup(os.environ.pop, "FAKE_ARGS_LOG", None)
        self.base = [sys.executable, os.path.join(HERE, "fake_index_cli.py")]

    def wait_idle(self, jobs, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            jobs.tick()
            if not jobs._inflight:
                return True
            time.sleep(0.05)
        return False

    def args(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f]

    def test_cli_when_down(self):
        w = FakeWorker()
        w.avail = "down"
        jobs, notes = make(w, base_cmd=self.base, ask_above=3)
        jobs.on_batch(Batch(ROOT, changed=[ROOT + r"\a.docx"]))
        jobs.tick()
        self.assertEqual(w.holds[:1], ["index 명령 실행 중"])
        self.assertTrue(self.wait_idle(jobs))
        self.assertEqual(w.holds[-1], None, "끝난 뒤 워커 막음을 풀지 않았다")
        a = self.args()[0]
        self.assertEqual(a[:3], ["index", "--dir", ROOT])
        self.assertIn("--max-add", a)
        self.assertEqual(w.sent, [], "워커에게 보내면 안 된다(내려가 있다)")

    def test_cli_held_new_then_approve(self):
        os.environ["FAKE_NEW"] = "7"
        self.addCleanup(os.environ.pop, "FAKE_NEW", None)
        w = FakeWorker()
        w.avail = "down"
        jobs, notes = make(w, base_cmd=self.base, ask_above=3)
        jobs.on_batch(Batch(ROOT, rescan=True))
        jobs.tick()
        self.assertTrue(self.wait_idle(jobs))
        self.assertEqual(jobs.waiting_new, {ROOT: 7})
        self.assertTrue(any(k == "warn" and "7건" in m for k, m in notes))
        jobs.run_now()
        jobs.tick()
        self.assertTrue(self.wait_idle(jobs))
        a = self.args()[-1]
        self.assertIn("--allow-delete", a)
        self.assertNotIn("--max-add", a)
        self.assertEqual(jobs.waiting_new, {})

    def test_cli_failure_retries_later(self):
        os.environ["FAKE_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_FAIL", None)
        w = FakeWorker()
        w.avail = "down"
        jobs, _ = make(w, base_cmd=self.base)
        jobs.on_batch(Batch(ROOT, rescan=True))
        jobs.tick()
        self.assertTrue(self.wait_idle(jobs))
        self.assertIn(ROOT, jobs._dirty)
        n = len(self.args())
        jobs.tick()
        self.assertEqual(len(self.args()), n, "곧바로 다시 돌리면 안 된다(재시도 간격)")

    #--------------------------------------------------------------
    # 막는 사이 워커가 뜨기 시작했으면 index 명령을 돌리지 않고 워커에게 맡긴다
    #--------------------------------------------------------------
    def test_race_worker_came_up(self):
        w = FakeWorker()
        w.avail = "down"
        orig_hold = w.hold

        def hold(reason):
            orig_hold(reason)
            if reason:
                w.avail = "starting"         # 막는 바로 그 순간 질문이 와서 뜨기 시작했다
        w.hold = hold
        jobs, _ = make(w, base_cmd=self.base)
        jobs.on_batch(Batch(ROOT, rescan=True))
        jobs.tick()
        self.assertEqual(w.holds, ["index 명령 실행 중", None])
        self.assertFalse(os.path.exists(self.log), "index 명령이 돌았다")
        self.assertIn(ROOT, jobs._dirty)

    #--------------------------------------------------------------
    # 워커가 다시 뜨면 한 번 대조한다(내려가 있던 동안의 키워드 색인을 맞추려고)
    #--------------------------------------------------------------
    def test_came_up_rescans(self):
        w = FakeWorker({"ok": True, "add": [], "modify": [], "remove": [],
                        "delete_blocked": "", "bm25_stale": True})
        w.avail = "down"
        jobs, _ = make(w, base_cmd=[])
        jobs.tick()
        w.avail = "up"
        spin(jobs)
        self.assertEqual(w.ops(), ["/index-outside", "/index-plan", "/bm25"])


if __name__ == "__main__":
    unittest.main()
