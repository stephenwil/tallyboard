"""Operations shared by the local CLI and the HTTP API.

Each op takes a JSON-able payload dict, so `tally --server URL item ...`
just POSTs the same payload to /api/runs/<id>/<op>.
"""
import json
import os
import urllib.error
import urllib.request

from .store import StoreError, find_item, hostname, summarize
from .watch import is_image


def _pick(payload, *keys):
    return {k: payload[k] for k in keys if payload.get(k) is not None}


def op_start(store, payload):
    run = store.create(**_pick(payload, "title", "repo", "cwd", "thread", "items",
                               "started_at", "spec", "base_dir", "host", "workers",
                               "meta", "auto_finish"))
    return {"id": run["id"], "run": summarize(run)}


def op_item(store, run_id, p):
    ids = p.get("ids") or [p["id"]]
    out = [store.set_item(run_id, i, **_pick(p, "status", "note", "group", "label",
                                             "output", "host", "source")) for i in ids]
    return {"items": out}


def op_log(store, run_id, p):
    store.log(run_id, p["msg"], **_pick(p, "level", "source"))
    return {"ok": True}


def op_handoff(store, run_id, p):
    return {"handoff": store.handoff(
        run_id, p.get("target"), handoff_id=p.get("handoff_id"),
        **_pick(p, "what", "thread", "link", "status", "note", "source", "from_host"))}


def op_worker(store, run_id, p):
    return {"worker": store.worker(run_id, p["name"], **_pick(
        p, "pid", "status", "note", "log", "host", "pgrep", "groups", "source"))}


def op_finish(store, run_id, p):
    return {"run": summarize(store.finish(run_id, **_pick(p, "status", "note", "source")))}


RUN_OPS = {"item": op_item, "log": op_log, "note": op_log, "handoff": op_handoff,
           "worker": op_worker, "finish": op_finish}


def run_detail(store, run_id, events=60):
    run = store.load(run_id)
    run["summary"] = summarize(run)
    run["events"] = store.events(run_id, limit=events)[::-1]
    for it in run["items"]:
        imgs = [f for f in it.get("files") or [] if is_image(f)]
        if it.get("uploaded") and is_image(it["uploaded"]):
            imgs.append("@" + it["uploaded"])
        it["images"] = imgs[-1:]
    if run.get("spec"):
        key = run["spec"].get("key")
        hist = [summarize(r) for r in store.list_runs()
                if (r.get("spec") or {}).get("key") == key]
        hist.sort(key=lambda s: s["started_at"] or 0)
        run["spec_history"] = [{"id": h["id"], "status": h["status"],
                                "duration": h["duration"], "attempt": h["attempt"],
                                "started_at": h["started_at"],
                                "done": h["counts"]["done"], "total": h["total"]}
                               for h in hist]
    return run


def item_file_path(store, run_id, item_id, name):
    """Map a (validated) item file reference to a real path. Only files the
    watcher recorded for that item, or its upload, can be served."""
    run = store.load(run_id)
    it = find_item(run, item_id)
    if it is None:
        raise StoreError("no such item")
    if name.startswith("@"):
        if name[1:] != it.get("uploaded"):
            raise StoreError("not an upload of this item")
        return os.path.join(store.run_dir(run_id), "files", name[1:])
    if name not in (it.get("files") or []):
        raise StoreError("not an output of this item")
    return os.path.join(run.get("base_dir") or "/", name)


# ---- remote client ------------------------------------------------------------
class Remote:
    def __init__(self, url, token=None):
        self.url = url.rstrip("/")
        self.token = token

    def post(self, path, payload):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     **({"X-Tallyboard-Token": self.token} if self.token else {})})
        return self._send(req)

    def get(self, path):
        return self._send(urllib.request.Request(self.url + path))

    def _send(self, req):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except ValueError:
                msg = str(e)
            raise StoreError(f"server: {msg}") from None
        except urllib.error.URLError as e:
            raise StoreError(f"cannot reach {self.url}: {e.reason}") from None


# ---- machines -----------------------------------------------------------------
def machines(runs, server_host=None, online=None):
    """Who is doing what, per machine, across active runs.

    A run's items count toward their own `host`, else the run's host. Live
    workers count toward their host. Open handoffs (sent/running) count toward
    their target, with progress from the items assigned to that target."""
    from .hosts import canonical
    online = online or {}
    server_host = server_host or hostname()
    out = {}

    def m(name):
        name = canonical(name) or "unknown"
        key = name.lower()
        if key not in out:
            out[key] = {"name": name, "online": online.get(name), "self": key == server_host.lower(),
                        "runs": [], "workers": [], "handoffs_in": [], "handoffs_out": 0,
                        "items": {"done": 0, "running": 0, "failed": 0, "queued": 0, "total": 0},
                        "running_items": []}
        return out[key]

    m(server_host)
    for run in runs:
        if run.get("status") != "running":
            continue
        rhost = run.get("host")
        m(rhost)["runs"].append({"id": run["id"], "title": run.get("title")})
        for it in run.get("items", []):
            mm = m(it.get("host") or rhost)
            st = it["status"] if it["status"] in mm["items"] else "done"
            mm["items"][st] += 1
            mm["items"]["total"] += 1
            if it["status"] == "running" and len(mm["running_items"]) < 6:
                mm["running_items"].append({"run": run["id"], "id": it["id"],
                                            "label": it.get("label") or it["id"]})
            if not any(r["id"] == run["id"] for r in mm["runs"]):
                mm["runs"].append({"id": run["id"], "title": run.get("title"), "via": rhost})
        for w in run.get("workers", []):
            if w.get("status") == "running":
                m(w.get("host") or rhost)["workers"].append(
                    {"run": run["id"], "name": w["name"], "pid": w.get("pid"),
                     "started_at": w.get("started_at")})
        for h in run.get("handoffs", []):
            if h.get("status") in ("sent", "running"):
                tgt = canonical(h.get("target"))
                mine = [i for i in run.get("items", []) if canonical(i.get("host")) == tgt]
                m(tgt)["handoffs_in"].append({
                    "run": run["id"], "id": h["id"], "from": canonical(h.get("from")),
                    "what": h.get("what"), "status": h["status"], "thread": h.get("thread"),
                    "link": h.get("link"), "updated_at": h.get("updated_at"),
                    "done": sum(1 for i in mine if i["status"] == "done"), "total": len(mine)})
                m(h.get("from") or rhost)["handoffs_out"] += 1
    order = sorted(out.values(), key=lambda x: (not x["self"], not (x["runs"] or x["workers"] or x["handoffs_in"]), x["name"].lower()))
    return order
