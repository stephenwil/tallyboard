"""On-disk state: one folder per run holding run.json + append-only events.jsonl.

All writers (CLI processes, the server's watcher, remote POSTs applied by the
server) go through `Store.mutate`, which takes an exclusive flock on the run's
lock file, so concurrent updates never clobber each other.
"""
import fcntl
import json
import os
import re
import secrets
import socket
import time
from contextlib import contextmanager

ITEM_STATES = ("queued", "running", "done", "failed", "skipped")
RUN_STATES = ("running", "done", "failed", "cancelled")
WORKER_STATES = ("running", "exited", "failed", "unknown")
HANDOFF_STATES = ("sent", "running", "done", "failed")


def default_root():
    env = os.environ.get("TALLYBOARD_HOME") or os.environ.get("JOBBOARD_HOME")
    if env:
        return env
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg, "tallyboard")


def hostname():
    return socket.gethostname()


def slugify(text, maxlen=32):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "run").strip("-").lower()
    return (s or "run")[:maxlen].strip("-")


class StoreError(Exception):
    pass


class Store:
    def __init__(self, root=None):
        self.root = root or default_root()
        self.runs_dir = os.path.join(self.root, "runs")
        os.makedirs(self.runs_dir, exist_ok=True)

    # ---- paths -----------------------------------------------------------
    def run_dir(self, run_id):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
            raise StoreError(f"bad run id: {run_id!r}")
        return os.path.join(self.runs_dir, run_id)

    def exists(self, run_id):
        return os.path.isfile(os.path.join(self.run_dir(run_id), "run.json"))

    # ---- low level -------------------------------------------------------
    @contextmanager
    def _lock(self, run_id):
        d = self.run_dir(run_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _write_run(self, run):
        d = self.run_dir(run["id"])
        tmp = os.path.join(d, f".run.json.{os.getpid()}.tmp")
        with open(tmp, "w") as fh:
            json.dump(run, fh, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(d, "run.json"))

    def _append_event(self, run_id, event):
        with open(os.path.join(self.run_dir(run_id), "events.jsonl"), "a") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")

    def load(self, run_id):
        try:
            with open(os.path.join(self.run_dir(run_id), "run.json")) as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise StoreError(f"no such run: {run_id}") from None

    def events(self, run_id, limit=None):
        path = os.path.join(self.run_dir(run_id), "events.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out[-limit:] if limit else out

    def list_runs(self):
        runs = []
        for name in os.listdir(self.runs_dir):
            try:
                runs.append(self.load(name))
            except (StoreError, ValueError, OSError):
                continue
        return runs

    @contextmanager
    def mutate(self, run_id):
        """Yield the run dict under lock; it is saved on exit. Set
        run['_events'] = [..] inside to append events atomically with it."""
        with self._lock(run_id):
            run = self.load(run_id)
            yield run
            events = run.pop("_events", [])
            if run.pop("_skip", False) and not events:
                return
            run["updated_at"] = time.time()
            self._write_run(run)
            for ev in events:
                self._append_event(run_id, ev)

    # ---- operations ------------------------------------------------------
    def create(self, title, repo=None, cwd=None, thread=None, items=None,
               started_at=None, spec=None, base_dir=None, host=None,
               workers=None, meta=None, auto_finish=True):
        now = time.time()
        started_at = float(started_at) if started_at else now
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        run_id = f"{stamp}-{slugify(title)}-{secrets.token_hex(2)}"
        run = {
            "id": run_id,
            "title": title,
            "repo": repo,
            "cwd": cwd,
            "thread": thread,
            "host": host or hostname(),
            "base_dir": base_dir or cwd,
            "created_at": now,
            "updated_at": now,
            "started_at": started_at,
            "finished_at": None,
            "status": "running",
            "auto_finish": bool(auto_finish),
            "items": [],
            "workers": [],
            "handoffs": [],
            "spec": spec,  # {"key","name","path"} when started from a spec
            "prev_run": None,
            "attempt": 1,
            "meta": meta or {},
        }
        if spec and spec.get("key"):
            prev = [r for r in self.list_runs()
                    if (r.get("spec") or {}).get("key") == spec["key"]]
            if prev:
                prev.sort(key=lambda r: r.get("created_at", 0))
                run["prev_run"] = prev[-1]["id"]
                run["attempt"] = len(prev) + 1
        for it in items or []:
            run["items"].append(_new_item(it, now))
        _check_unique_items(run["items"])
        for w in workers or []:
            run["workers"].append(_new_worker(w, run["host"], now))
        with self._lock(run_id):
            self._write_run(run)
            self._append_event(run_id, {
                "ts": now, "type": "start", "msg": f"run started: {title}",
                "items": len(run["items"]), "host": run["host"]})
        return run

    def set_item(self, run_id, item_id, status=None, note=None, group=None,
                 label=None, output=None, host=None, source="cli"):
        if status and status not in ITEM_STATES:
            raise StoreError(f"item status must be one of {ITEM_STATES}")
        now = time.time()
        with self.mutate(run_id) as run:
            it = find_item(run, item_id)
            if it is None:
                it = _new_item({"id": item_id, "group": group, "label": label,
                                "output": output}, now)
                run["items"].append(it)
            for k, v in (("group", group), ("label", label), ("output", output),
                         ("host", host)):
                if v is not None:
                    it[k] = v
            if status:
                _apply_item_status(it, status, now)
            if note is not None:
                it["note"] = note
            it["updated_at"] = now
            run["_events"] = [{"ts": now, "type": "item", "item": item_id,
                               "status": it["status"], "msg": note or "",
                               "source": source, **({"host": host} if host else {})}]
            _maybe_autofinish(run, now)
            return it

    def log(self, run_id, msg, level="info", source="cli"):
        now = time.time()
        with self.mutate(run_id) as run:
            run["_events"] = [{"ts": now, "type": "log", "level": level,
                               "msg": msg, "source": source}]

    def handoff(self, run_id, target, what=None, thread=None, link=None,
                status="sent", note=None, handoff_id=None, source="cli",
                from_host=None):
        if status and status not in HANDOFF_STATES:
            raise StoreError(f"handoff status must be one of {HANDOFF_STATES}")
        now = time.time()
        with self.mutate(run_id) as run:
            h = None
            if handoff_id:
                h = next((x for x in run["handoffs"] if x["id"] == handoff_id), None)
                if h is None:
                    raise StoreError(f"no such handoff: {handoff_id}")
            else:
                h = {"id": f"h{len(run['handoffs']) + 1}", "created_at": now,
                     "from": from_host or run.get("host"), "target": target,
                     "what": what, "thread": thread, "link": link,
                     "status": "sent", "note": None}
                run["handoffs"].append(h)
            for k, v in (("target", target), ("what", what), ("thread", thread),
                         ("link", link), ("note", note), ("status", status)):
                if v is not None:
                    h[k] = v
            h["updated_at"] = now
            run["_events"] = [{"ts": now, "type": "handoff", "handoff": h["id"],
                               "status": h["status"], "source": source,
                               "msg": f"{h['from']} -> {h['target']}: {h.get('what') or ''}"
                                      + (f" ({note})" if note else "")}]
            return h

    def worker(self, run_id, name, pid=None, status=None, note=None, log=None,
               host=None, pgrep=None, groups=None, source="cli"):
        if status and status not in WORKER_STATES:
            raise StoreError(f"worker status must be one of {WORKER_STATES}")
        now = time.time()
        with self.mutate(run_id) as run:
            w = next((x for x in run["workers"] if x["name"] == name), None)
            if w is None:
                w = _new_worker({"name": name}, host or run.get("host"), now)
                run["workers"].append(w)
            if pid is not None and pid != w.get("pid"):
                w["pid_start"] = None
                status = status or "running"
            for k, v in (("pid", pid), ("note", note), ("log", log),
                         ("host", host), ("pgrep", pgrep), ("groups", groups)):
                if v is not None:
                    w[k] = v
            if status:
                if status != w.get("status") and status != "running":
                    w["ended_at"] = w.get("ended_at") or now
                w["status"] = status
            w["updated_at"] = now
            run["_events"] = [{"ts": now, "type": "worker", "worker": name,
                               "status": w["status"], "msg": note or "",
                               "source": source}]
            return w

    def finish(self, run_id, status="done", note=None, source="cli"):
        if status not in RUN_STATES:
            raise StoreError(f"run status must be one of {RUN_STATES}")
        now = time.time()
        with self.mutate(run_id) as run:
            run["status"] = status
            run["finished_at"] = now if status != "running" else None
            run["_events"] = [{"ts": now, "type": "finish", "status": status,
                               "msg": note or f"run {status}", "source": source}]
            return run

    def attach_file(self, run_id, item_id, filename, data):
        """Store an uploaded file (e.g. a render PNG from a remote machine)."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "upload"))
        d = os.path.join(self.run_dir(run_id), "files")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{slugify(item_id, 60)}__{safe}")
        with open(path, "wb") as fh:
            fh.write(data)
        now = time.time()
        with self.mutate(run_id) as run:
            it = find_item(run, item_id)
            if it is None:
                it = _new_item({"id": item_id}, now)
                run["items"].append(it)
            it["uploaded"] = os.path.basename(path)
            it["updated_at"] = now
            run["_events"] = [{"ts": now, "type": "upload", "item": item_id,
                               "msg": f"uploaded {safe} ({len(data):,} B)",
                               "source": "http"}]
        return path


# ---- helpers ----------------------------------------------------------------
def find_item(run, item_id):
    return next((i for i in run["items"] if i["id"] == item_id), None)


def _new_item(spec, now):
    if isinstance(spec, str):
        spec = {"id": spec}
    if not spec.get("id"):
        raise StoreError(f"item needs an id: {spec!r}")
    it = {
        "id": str(spec["id"]),
        "label": spec.get("label"),
        "group": spec.get("group"),
        "host": spec.get("host"),       # machine doing it; None = the run's host
        "output": spec.get("output"),   # path or glob, relative to base_dir
        "min_files": int(spec.get("min_files") or 1),
        "cmd": spec.get("cmd"),
        "status": spec.get("status") or "queued",
        "note": spec.get("note"),
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "files": [],
    }
    if it["status"] not in ITEM_STATES:
        raise StoreError(f"bad item status {it['status']!r}")
    return it


def _check_unique_items(items):
    seen = set()
    for it in items:
        if it["id"] in seen:
            raise StoreError(f"duplicate item id: {it['id']}")
        seen.add(it["id"])


def _new_worker(spec, host, now):
    return {
        "name": spec["name"], "pid": spec.get("pid"), "pid_start": spec.get("pid_start"),
        "host": spec.get("host") or host, "pgrep": spec.get("pgrep"),
        "log": spec.get("log"), "cmd": spec.get("cmd"),
        "groups": spec.get("groups"), "fail_pattern": spec.get("fail_pattern"),
        "status": spec.get("status") or ("running" if spec.get("pid") else "unknown"),
        "note": spec.get("note"), "started_at": spec.get("started_at"),
        "ended_at": None, "updated_at": now, "log_tail": [],
    }


def _apply_item_status(it, status, now):
    if status == "running" and not it.get("started_at"):
        it["started_at"] = now
    if status in ("done", "failed", "skipped"):
        it["finished_at"] = now
    elif status in ("queued", "running"):
        it["finished_at"] = None
    it["status"] = status


def _maybe_autofinish(run, now):
    if run.get("status") != "running" or not run.get("auto_finish") or not run["items"]:
        return
    if all(i["status"] in ("done", "skipped") for i in run["items"]):
        run["status"] = "done"
        # end at the last item's finish (e.g. the newest output file), not poll time
        run["finished_at"] = max([i.get("finished_at") or 0 for i in run["items"]] + [0]) or now
        run.setdefault("_events", []).append(
            {"ts": now, "type": "finish", "status": "done",
             "msg": "all items done", "source": "auto"})


def summarize(run):
    """Counts used by the list view and CLI."""
    counts = {s: 0 for s in ITEM_STATES}
    for it in run.get("items", []):
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    total = len(run.get("items", []))
    end = run.get("finished_at") or time.time()
    return {
        "id": run["id"], "title": run.get("title"), "status": run.get("status"),
        "repo": run.get("repo"), "thread": run.get("thread"), "host": run.get("host"),
        "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
        "duration": max(0.0, end - (run.get("started_at") or end)),
        "total": total, "counts": counts,
        "spec": run.get("spec"), "attempt": run.get("attempt"),
        "prev_run": run.get("prev_run"),
        "workers_alive": sum(1 for w in run.get("workers", []) if w.get("status") == "running"),
        "handoffs": len(run.get("handoffs", [])),
        "updated_at": run.get("updated_at"),
    }
