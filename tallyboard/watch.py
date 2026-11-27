"""Infer progress for tools that can't report themselves.

* Items with an `output` (path or glob, relative to the run's base_dir) become
  done once at least `min_files` matching files exist with mtime >= run start.
* Workers with a pid (or a `pgrep` regex) on this host are checked for life;
  /proc start time guards against pid reuse. A worker's `log` is tailed, and an
  exited worker whose log matches `fail_pattern` is marked failed.
* A live worker with `groups` marks the first queued item in those groups as
  running (Blender-style sequential builds), shown as inferred.
"""
import glob
import os
import re
import time

from .store import _apply_item_status, _maybe_autofinish, hostname

SHELLS = {"bash", "sh", "zsh", "dash", "fish", "pgrep", "grep", "timeout", "sudo"}
DEFAULT_FAIL = r"Traceback \(most recent call last\)|Error: script failed|^Error:"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


# ---- /proc helpers ----------------------------------------------------------
def _clk_tck():
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return 100


def _boot_time():
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("btime"):
                return float(line.split()[1])
    return 0.0


def proc_start_ticks(pid):
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            data = fh.read()
    except (OSError, ValueError):
        return None
    # comm may contain spaces/parens; fields resume after the last ')'
    rest = data[data.rfind(")") + 2:].split()
    return int(rest[19])  # field 22 overall = starttime


def proc_start_time(pid):
    """Process start as epoch seconds, or None if it is gone."""
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return None
    return _boot_time() + ticks / _clk_tck()


def proc_alive(pid, start_ticks=None):
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return False
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False
    except OSError:
        return False
    return start_ticks is None or ticks == start_ticks


def pgrep(pattern, exclude_shells=True):
    """[(pid, cmdline)] whose full command line matches the regex."""
    rx = re.compile(pattern)
    me = os.getpid()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            with open(f"/proc/{name}/comm") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if not cmd or (exclude_shells and comm in SHELLS):
            continue
        if rx.search(cmd):
            out.append((int(name), cmd))
    return sorted(out)


def tail(path, n=3, max_bytes=8192):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            text = fh.read().decode(errors="replace")
    except OSError:
        return None
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-n:], text


# ---- output files -----------------------------------------------------------
def resolve_output(base_dir, output):
    pattern = os.path.join(base_dir or "", os.path.expanduser(output))
    if glob.has_magic(pattern):
        return sorted(glob.glob(pattern))
    return [pattern] if os.path.exists(pattern) else []


def check_outputs(run, now=None, events=None):
    """Update item status from output files. Returns True if anything changed."""
    now = now or time.time()
    started = run.get("started_at") or 0
    changed = False
    for it in run["items"]:
        if not it.get("output"):
            continue
        fresh, stale = [], 0
        for p in resolve_output(run.get("base_dir"), it["output"]):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= started:
                fresh.append((st.st_mtime, p))
            else:
                stale += 1
        files = [os.path.relpath(p, run.get("base_dir") or "/") for _, p in sorted(fresh)][:12]
        if files != it.get("files") or stale != it.get("stale_files", 0):
            it["files"], it["stale_files"] = files, stale
            changed = True
        if len(fresh) >= it.get("min_files", 1) and it["status"] in ("queued", "running"):
            _apply_item_status(it, "done", now)
            it["finished_at"] = max(m for m, _ in fresh)
            it.pop("inferred", None)
            it["updated_at"] = now
            changed = True
            if events is not None:
                events.append({"ts": now, "type": "item", "item": it["id"],
                               "status": "done", "source": "watch",
                               "msg": f"output appeared: {files[-1] if files else it['output']}"})
    return changed


# ---- workers ----------------------------------------------------------------
def check_workers(run, now=None, events=None, host=None):
    now = now or time.time()
    host = host or hostname()
    changed = False
    for w in run.get("workers", []):
        if w.get("host") and w["host"] != host:
            continue  # can only inspect our own processes
        if w.get("pid") and w.get("status") == "running" and not w.get("pid_start"):
            w["pid_start"] = proc_start_ticks(w["pid"])
            w["started_at"] = w.get("started_at") or proc_start_time(w["pid"])
            changed = True
        if w.get("pgrep") and w.get("status") != "running":
            # pick up a worker that (re)started after the last one ended
            since = w.get("ended_at") or 0
            for pid, cmd in pgrep(w["pgrep"]):
                st = proc_start_time(pid) or 0
                if st >= since - 1 and pid != w.get("pid"):
                    w.update(pid=pid, cmd=cmd, pid_start=proc_start_ticks(pid),
                             started_at=st, status="running", ended_at=None)
                    changed = True
                    if events is not None:
                        events.append({"ts": now, "type": "worker", "worker": w["name"],
                                       "status": "running", "source": "watch",
                                       "msg": f"found pid {pid}"})
                    break
        if w.get("log"):
            res = tail(w["log"])
            if res is not None:
                lines, text = res
                if lines != w.get("log_tail"):
                    w["log_tail"] = lines
                    changed = True
                w["_log_text"] = text
        if w.get("pid") and w.get("status") == "running":
            if not proc_alive(w["pid"], w.get("pid_start")):
                failed = bool(w.get("_log_text") and re.search(
                    w.get("fail_pattern") or DEFAULT_FAIL, w["_log_text"], re.M))
                w["status"] = "failed" if failed else "exited"
                w["ended_at"] = now
                changed = True
                if events is not None:
                    events.append({"ts": now, "type": "worker", "worker": w["name"],
                                   "status": w["status"], "source": "watch",
                                   "msg": f"pid {w['pid']} {w['status']}"})
        w.pop("_log_text", None)
    changed |= infer_running(run, now)
    return changed


def infer_running(run, now):
    changed = False
    for w in run.get("workers", []):
        groups = w.get("groups")
        if not groups:
            continue
        mine = [i for i in run["items"] if i.get("group") in groups]
        alive = w.get("status") == "running"
        for it in mine:
            if it.get("inferred") == w["name"] and it["status"] == "running" and not alive:
                it["status"] = "failed" if w.get("status") == "failed" else "queued"
                it["note"] = f"worker {w['name']} {w.get('status')}"
                it.pop("inferred", None)
                changed = True
        if alive and not any(i["status"] == "running" for i in mine):
            nxt = next((i for i in mine if i["status"] == "queued"), None)
            if nxt:
                _apply_item_status(nxt, "running", now)
                nxt["inferred"] = w["name"]
                changed = True
    return changed


def poll(store, run_id, host=None):
    """One watch pass over a run. Returns True if it changed."""
    result = {"changed": False}
    with store.mutate(run_id) as run:
        if run.get("status") != "running":
            run["_skip"] = True
            return False
        now = time.time()
        events = []
        changed = check_outputs(run, now, events)
        changed |= check_workers(run, now, events, host)
        _maybe_autofinish(run, now)
        events.extend(run.pop("_events", []))
        if events:
            run["_events"] = events
        if not changed and not events:
            run["_skip"] = True
        result["changed"] = changed or bool(events)
    return result["changed"]


def is_image(path):
    return path.lower().endswith(IMAGE_EXT)
