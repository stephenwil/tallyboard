import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from tallyboard.server import make_handler
from tallyboard.store import Store


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store, token="s3cret"))
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close(); self.tmp.cleanup()

    def req(self, path, payload=None, token="s3cret", raw=None, header="X-Tallyboard-Token"):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        r = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET")
        if token:
            r.add_header(header, token)
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read())

    def test_remote_flow(self):
        rid = self.req("/api/runs", {"title": "remote", "items": ["r1", "r2"], "host": "RENDER-NODE"})["id"]
        self.req(f"/api/runs/{rid}/item", {"id": "r1", "status": "done", "note": "8.2s"})
        self.req(f"/api/runs/{rid}/log", {"msg": "hello"})
        self.req(f"/api/runs/{rid}/handoff", {"target": "GPU-NODE", "what": "renders"})
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
        self.req(f"/api/runs/{rid}/upload?item=r2&name=r2.png", raw=png)
        d = self.req(f"/api/runs/{rid}")
        self.assertEqual(d["summary"]["counts"]["done"], 1)
        self.assertEqual(d["handoffs"][0]["target"], "GPU-NODE")
        it = [i for i in d["items"] if i["id"] == "r2"][0]
        self.assertEqual(len(it["images"]), 1)
        with urllib.request.urlopen(f"{self.url}/api/runs/{rid}/file?item=r2&name={it['images'][0]}") as resp:
            self.assertEqual(resp.read(), png)
        runs = self.req("/api/runs")["runs"]
        self.assertEqual(runs[0]["id"], rid)

    def test_token_required_and_legacy_header_accepted(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("/api/runs", {"title": "x"}, token=None)
        self.assertEqual(cm.exception.code, 401)
        res = self.req("/api/runs", {"title": "legacy"}, header="X-Jobboard-Token")
        self.assertIn("id", res)

    def test_arbitrary_files_not_served(self):
        rid = self.req("/api/runs", {"title": "x", "items": ["a"]})["id"]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"{self.url}/api/runs/{rid}/file?item=a&name=/etc/passwd")
        self.assertEqual(cm.exception.code, 404)

    def test_index(self):
        with urllib.request.urlopen(self.url + "/") as resp:
            self.assertIn(b"Tallyboard", resp.read())


if __name__ == "__main__":
    unittest.main()
