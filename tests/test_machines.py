import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from tallyboard import api, cli, hosts
from tallyboard.server import make_handler
from tallyboard.store import Store

FAKE = {"at": 1e18, "by_ip": {"100.64.0.42": "GPU-NODE"},
        "by_name": {"gpu-node": "GPU-NODE", "gpu-win11": "GPU-NODE",
                    "workstation": "workstation"},
        "online": {"GPU-NODE": True}, "self_ips": set(),
        "self_dns": "workstation.example.ts.net"}


class MachinesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.p = mock.patch.dict(hosts._cache, FAKE)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_canonical_uses_tailnet_names(self):
        self.assertEqual(hosts.canonical("gpu-win11"), "GPU-NODE")
        self.assertEqual(hosts.canonical("Gpu-Node.example.ts.net"), "GPU-NODE")
        self.assertEqual(hosts.canonical("elsewhere"), "elsewhere")

    def test_board_url_uses_tailscale_magic_dns(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TALLYBOARD_URL", None)
            os.environ.pop("JOBBOARD_URL", None)
            self.assertEqual(cli.board_url(8790), "http://workstation.example.ts.net:8790/")
            with mock.patch.dict(hosts._cache, {"self_dns": None}):
                with mock.patch.object(cli, "hostname", return_value="workstation"):
                    self.assertEqual(cli.board_url(8790), "http://workstation:8790/")

    def test_machines_split_items_workers_and_handoffs(self):
        run = self.store.create("render", host="workstation", items=[
            {"id": "a"}, {"id": "r1", "host": "GPU-NODE"}, {"id": "r2", "host": "gpu-win11"}],
            workers=[{"name": "blender", "pid": 1, "status": "running"}])
        self.store.set_item(run["id"], "r1", "done")
        self.store.handoff(run["id"], "GPU-NODE", what="Cycles renders")
        old = self.store.create("old", host="elsewhere", items=["z"])
        self.store.finish(old["id"])  # finished runs are ignored
        ms = {m["name"]: m for m in api.machines(self.store.list_runs(), server_host="workstation",
                                                 online=hosts.online_map())}
        ws, gpu = ms["workstation"], ms["GPU-NODE"]
        self.assertNotIn("elsewhere", ms)
        self.assertTrue(ws["self"])
        self.assertEqual(ws["items"]["total"], 1)
        self.assertEqual([w["name"] for w in ws["workers"]], ["blender"])
        self.assertEqual(ws["handoffs_out"], 1)
        self.assertEqual((gpu["items"]["done"], gpu["items"]["total"]), (1, 2))
        self.assertTrue(gpu["online"])
        h = gpu["handoffs_in"][0]
        self.assertEqual((h["from"], h["done"], h["total"], h["what"]), ("workstation", 1, 2, "Cycles renders"))
        self.assertEqual(gpu["runs"][0]["via"], "workstation")

    def test_remote_posts_are_attributed_to_the_sender(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d" % httpd.server_address[1]

        def post(path, payload):
            r = urllib.request.Request(url + path, json.dumps(payload).encode(), method="POST")
            with urllib.request.urlopen(r) as resp:
                return json.loads(resp.read())
        try:
            with mock.patch.object(hosts, "host_for_ip", return_value="GPU-NODE"):
                rid = post("/api/runs", {"title": "win job", "items": ["x"]})["id"]
                post(f"/api/runs/{rid}/item", {"id": "x", "status": "running"})
            run = self.store.load(rid)
            self.assertEqual(run["host"], "GPU-NODE")
            self.assertEqual(run["items"][0]["host"], "GPU-NODE")
            with mock.patch.object(hosts, "host_for_ip", return_value=None):  # local caller
                post(f"/api/runs/{rid}/item", {"id": "y", "status": "queued"})
            self.assertIsNone(self.store.load(rid)["items"][1]["host"])
            with urllib.request.urlopen(url + "/api/runs") as resp:
                self.assertIn("machines", json.loads(resp.read()))
        finally:
            httpd.shutdown(); httpd.server_close()


if __name__ == "__main__":
    unittest.main()
