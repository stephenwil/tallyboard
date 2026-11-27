import os
import tempfile
import threading
import unittest

from tallyboard.store import Store, StoreError, summarize


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_writes_run_and_event(self):
        run = self.store.create("Build things", items=["a", {"id": "b", "group": "G"}])
        d = self.store.run_dir(run["id"])
        self.assertTrue(os.path.isfile(os.path.join(d, "run.json")))
        evs = self.store.events(run["id"])
        self.assertEqual(evs[0]["type"], "start")
        self.assertEqual([i["id"] for i in run["items"]], ["a", "b"])
        self.assertEqual(run["items"][1]["group"], "G")

    def test_duplicate_items_rejected(self):
        with self.assertRaises(StoreError):
            self.store.create("x", items=["a", "a"])

    def test_item_status_and_autofinish(self):
        run = self.store.create("x", items=["a", "b"])
        self.store.set_item(run["id"], "a", "done", note="ok")
        self.assertEqual(self.store.load(run["id"])["status"], "running")
        self.store.set_item(run["id"], "b", "done")
        r = self.store.load(run["id"])
        self.assertEqual(r["status"], "done")
        self.assertIsNotNone(r["finished_at"])
        self.assertEqual(summarize(r)["counts"]["done"], 2)

    def test_unknown_item_is_added(self):
        run = self.store.create("x")
        self.store.set_item(run["id"], "new", "running", group="later")
        it = self.store.load(run["id"])["items"][0]
        self.assertEqual((it["id"], it["status"], it["group"]), ("new", "running", "later"))

    def test_bad_status(self):
        run = self.store.create("x", items=["a"])
        with self.assertRaises(StoreError):
            self.store.set_item(run["id"], "a", "exploded")

    def test_handoff_create_and_update(self):
        run = self.store.create("x")
        h = self.store.handoff(run["id"], "GPU-NODE", what="renders", thread="t-1")
        self.assertEqual(h["id"], "h1")
        self.store.handoff(run["id"], None, handoff_id="h1", status="done")
        h = self.store.load(run["id"])["handoffs"][0]
        self.assertEqual((h["target"], h["status"], h["thread"]), ("GPU-NODE", "done", "t-1"))

    def test_repeat_spec_links_previous(self):
        spec = {"key": "nightly", "name": "nightly"}
        a = self.store.create("x", spec=spec)
        b = self.store.create("x", spec=spec)
        self.assertEqual(b["prev_run"], a["id"])
        self.assertEqual(b["attempt"], 2)

    def test_concurrent_updates_do_not_lose_items(self):
        run = self.store.create("x", auto_finish=False)
        ids = [f"i{n}" for n in range(40)]
        threads = [threading.Thread(target=self.store.set_item, args=(run["id"], i, "done")) for i in ids]
        for t in threads: t.start()
        for t in threads: t.join()
        got = {i["id"] for i in self.store.load(run["id"])["items"]}
        self.assertEqual(got, set(ids))
        self.assertEqual(len(self.store.events(run["id"])), 41)

    def test_bad_run_id(self):
        with self.assertRaises(StoreError):
            self.store.load("../etc")


if __name__ == "__main__":
    unittest.main()
