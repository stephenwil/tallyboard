"""HTTP server: the live page, a JSON API, and a background watcher thread.

GET  /                              the board
GET  /api/runs                      run summaries (active first)
GET  /api/runs/<id>                 full run + recent events + spec history
GET  /api/runs/<id>/file?item=&name=  an image output (only files recorded for that item)
POST /api/runs                      start a run (same payload as `tally start`)
POST /api/runs/<id>/<op>            op = item | log | note | handoff | worker | finish
POST /api/runs/<id>/upload?item=&name=  raw body = file (e.g. a render PNG)

If TALLYBOARD_TOKEN is set, POSTs need header `X-Tallyboard-Token` (or ?token=).
"""
import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import api, hosts, watch
from .store import Store, StoreError, summarize

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 64 * 1024 * 1024


def sort_runs(summaries):
    active = sorted((s for s in summaries if s["status"] == "running"),
                    key=lambda s: -(s["started_at"] or 0))
    rest = sorted((s for s in summaries if s["status"] != "running"),
                  key=lambda s: -(s["finished_at"] or s["started_at"] or 0))
    return active + rest


def make_handler(store, token=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tallyboard/1"

        def log_message(self, fmt, *args):  # keep journald quiet for polling GETs
            if self.command != "GET":
                super().log_message(fmt, *args)

        def _json(self, obj, code=200):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path, ctype=None, cache="no-cache"):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0]
                             or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def _parts(self):
            u = urlparse(self.path)
            return [p for p in u.path.split("/") if p], {k: v[-1] for k, v in parse_qs(u.query).items()}

        def do_GET(self):
            parts, q = self._parts()
            try:
                if not parts or parts == ["index.html"]:
                    return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
                if parts == ["favicon.svg"]:
                    return self._file(os.path.join(STATIC, "favicon.svg"), "image/svg+xml", "max-age=86400")
                if parts == ["api", "health"]:
                    return self._json({"ok": True, "host": watch.hostname(), "time": time.time()})
                if parts == ["api", "runs"]:
                    runs = store.list_runs()
                    return self._json({"host": watch.hostname(), "time": time.time(),
                                       "runs": sort_runs([summarize(r) for r in runs]),
                                       "machines": api.machines(runs, online=hosts.online_map())})
                if parts == ["api", "machines"]:
                    return self._json({"machines": api.machines(store.list_runs(),
                                                                online=hosts.online_map())})
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self._json(api.run_detail(store, parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "file":
                    path = api.item_file_path(store, parts[2], q.get("item", ""), q.get("name", ""))
                    if not watch.is_image(path):
                        return self._json({"error": "only images are served"}, 403)
                    return self._file(path, cache="max-age=30")
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "events":
                    return self._json({"events": store.events(parts[2])})
            except StoreError as e:
                return self._json({"error": str(e)}, 404)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            parts, q = self._parts()
            got_token = (self.headers.get("X-Tallyboard-Token")
                         or self.headers.get("X-Jobboard-Token")
                         or q.get("token"))
            if token and got_token != token:
                return self._json({"error": "bad or missing token"}, 401)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json({"error": "body too large"}, 413)
            body = self.rfile.read(length) if length else b""
            try:
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "upload":
                    path = store.attach_file(parts[2], q.get("item") or "upload",
                                             q.get("name") or "upload.bin", body)
                    return self._json({"ok": True, "stored": os.path.basename(path)})
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return self._json({"error": "body must be JSON"}, 400)
                if not isinstance(payload, dict):
                    return self._json({"error": "body must be a JSON object"}, 400)
                client = hosts.host_for_ip(self.client_address[0])  # None = this machine
                payload.setdefault("source", "http:" + (client or self.client_address[0]))
                if parts == ["api", "runs"]:
                    payload.pop("source", None)
                    payload["host"] = hosts.canonical(payload.get("host") or client or watch.hostname())
                    if not payload.get("title"):
                        return self._json({"error": "title required"}, 400)
                    return self._json(api.op_start(store, payload), 201)
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in api.RUN_OPS:
                    if not store.exists(parts[2]):
                        return self._json({"error": f"no such run: {parts[2]}"}, 404)
                    if client and parts[3] in ("item", "worker") and not payload.get("host"):
                        payload["host"] = client  # whoever reports the work is doing it
                    if payload.get("host"):
                        payload["host"] = hosts.canonical(payload["host"])
                    return self._json(api.RUN_OPS[parts[3]](store, parts[2], payload))
            except (StoreError, KeyError, TypeError, ValueError) as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
            self._json({"error": "not found"}, 404)

    return Handler


def watcher_loop(store, interval, stop):
    while not stop.is_set():
        for run in store.list_runs():
            if run.get("status") == "running":
                try:
                    watch.poll(store, run["id"])
                except Exception:  # never let one bad run kill the watcher
                    traceback.print_exc()
        stop.wait(interval)


def serve(host="0.0.0.0", port=8790, root=None, interval=3.0, token=None):
    store = Store(root)
    token = token or os.environ.get("TALLYBOARD_TOKEN") or os.environ.get("JOBBOARD_TOKEN") or None
    stop = threading.Event()
    t = threading.Thread(target=watcher_loop, args=(store, interval, stop), daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(store, token))
    httpd.daemon_threads = True
    print(f"tallyboard: serving {store.root} on http://{host}:{port}/ "
          f"(watch every {interval}s{', token required for POST' if token else ''})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
