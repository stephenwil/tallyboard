import os
import subprocess
import sys
import tempfile
import time
import unittest

from tallyboard import watch
from tallyboard.store import Store


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(self.base, "out"))
        self.store = Store(os.path.join(self.tmp.name, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, rel, mtime=None):
        p = os.path.join(self.base, rel)
        with open(p, "w") as fh:
            fh.write("x")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_output_newer_than_start_marks_done(self):
        start = time.time() - 100
        self.touch("out/old.stl", mtime=start - 50)
        run = self.store.create("w", base_dir=self.base, started_at=start, items=[
            {"id": "old", "output": "out/old.stl"}, {"id": "new", "output": "out/new.stl"},
            {"id": "glob", "output": "out/frame_*.png", "min_files": 2}])
        watch.poll(self.store, run["id"])
        r = self.store.load(run["id"])
        self.assertEqual([i["status"] for i in r["items"]], ["queued"] * 3)
        self.assertEqual(r["items"][0]["stale_files"], 1)

        self.touch("out/new.stl")
        self.touch("out/frame_1.png")
        watch.poll(self.store, run["id"])
        r = self.store.load(run["id"])
        self.assertEqual([i["status"] for i in r["items"]], ["queued", "done", "queued"])

        self.touch("out/old.stl")  # rebuilt now
        self.touch("out/frame_2.png")
        watch.poll(self.store, run["id"])
        r = self.store.load(run["id"])
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["items"][2]["files"], ["out/frame_1.png", "out/frame_2.png"])

    def test_no_change_does_not_rewrite(self):
        run = self.store.create("w", base_dir=self.base, items=[{"id": "a", "output": "out/a"}])
        watch.poll(self.store, run["id"])
        before = self.store.load(run["id"])["updated_at"]
        self.assertFalse(watch.poll(self.store, run["id"]))
        self.assertEqual(self.store.load(run["id"])["updated_at"], before)

    def test_pid_exit_and_log_failure_and_inferred_running(self):
        log = os.path.join(self.tmp.name, "w.log")
        with open(log, "w") as fh:
            fh.write("OK item a\nError: script failed, file: x.py\n")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            run = self.store.create("w", base_dir=self.base, items=[
                {"id": "a", "group": "G"}, {"id": "b", "group": "G"}],
                workers=[{"name": "w1", "pid": proc.pid, "log": log, "groups": ["G"]}])
            watch.poll(self.store, run["id"])
            r = self.store.load(run["id"])
            self.assertEqual(r["workers"][0]["status"], "running")
            self.assertEqual(r["workers"][0]["log_tail"][-1], "Error: script failed, file: x.py")
            self.assertEqual(r["items"][0]["status"], "running")   # inferred
            self.assertEqual(r["items"][0]["inferred"], "w1")
        finally:
            proc.kill(); proc.wait()
        watch.poll(self.store, run["id"])
        r = self.store.load(run["id"])
        self.assertEqual(r["workers"][0]["status"], "failed")
        self.assertEqual(r["items"][0]["status"], "failed")
        self.assertEqual(r["items"][1]["status"], "queued")

    def test_pgrep_finds_process_and_skips_shells(self):
        marker = "tallyboard_test_marker_%d" % os.getpid()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
        try:
            found = watch.pgrep(marker)
            self.assertEqual([p for p, _ in found], [proc.pid])
            st = watch.proc_start_time(proc.pid)
            self.assertLess(abs(st - time.time()), 30)
        finally:
            proc.kill(); proc.wait()
        self.assertEqual(watch.pgrep(marker), [])


if __name__ == "__main__":
    unittest.main()
