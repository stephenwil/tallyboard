"""tally command line. Run `tally -h` or `tally <cmd> -h`."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import api, hosts, watch
from .spec import load_spec
from .store import (HANDOFF_STATES, ITEM_STATES, RUN_STATES, WORKER_STATES,
                    Store, StoreError, hostname, summarize)

DEFAULT_PORT = int(os.environ.get("TALLYBOARD_PORT") or os.environ.get("JOBBOARD_PORT") or 8790)


def board_url(port=None):
    env = os.environ.get("TALLYBOARD_URL") or os.environ.get("JOBBOARD_URL")
    if env:
        return env
    p = port or DEFAULT_PORT
    # Check if tailscale serve is actively proxying tallyboard
    serve_url = hosts.tailscale_serve_url(p)
    if serve_url:
        return serve_url
    host = hosts.self_dns() or hostname()
    return f"http://{host}:{p}/"


def parse_time(text):
    """epoch seconds | ISO 8601 | `ps -o lstart` text | pid:<N> (its start time)."""
    text = str(text).strip()
    if text.startswith("pid:"):
        t = watch.proc_start_time(int(text[4:]))
        if t is None:
            raise StoreError(f"no such process: {text[4:]}")
        return t
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(" ".join(text.split()), fmt))
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text)
        return dt.timestamp()
    except ValueError:
        raise StoreError(f"can't parse time: {text!r}") from None


def git_root(path):
    try:
        return subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def fmt_dur(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


class Backend:
    """Local store, or a remote server when --server/TALLYBOARD_SERVER is given."""

    def __init__(self, args):
        self.remote = api.Remote(args.server, args.token) if args.server else None
        self.store = None if self.remote else Store(args.home)

    def start(self, payload):
        if self.remote:
            return self.remote.post("/api/runs", payload)
        return api.op_start(self.store, payload)

    def op(self, run_id, op, payload):
        if self.remote:
            return self.remote.post(f"/api/runs/{run_id}/{op}", payload)
        if not self.store.exists(run_id):
            raise StoreError(f"no such run: {run_id}")
        return api.RUN_OPS[op](self.store, run_id, payload)

    def runs(self):
        if self.remote:
            return self.remote.get("/api/runs")["runs"]
        from .server import sort_runs
        return sort_runs([summarize(r) for r in self.store.list_runs()])

    def detail(self, run_id):
        if self.remote:
            return self.remote.get(f"/api/runs/{run_id}")
        return api.run_detail(self.store, run_id)

    def resolve(self, run_id):
        """`last` (or empty) = newest running run, preferring this thread's."""
        if run_id and run_id not in ("last", "-"):
            return run_id
        runs = self.runs()
        thread = default_thread()
        for pool in ([r for r in runs if r["status"] == "running" and thread and r.get("thread") == thread],
                     [r for r in runs if r["status"] == "running"], runs):
            if pool:
                return max(pool, key=lambda r: r["started_at"] or 0)["id"]
        raise StoreError("no runs yet")


def default_thread():
    return (os.environ.get("TALLYBOARD_THREAD") or os.environ.get("JOBBOARD_THREAD")
            or os.environ.get("T3CODE_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID"))


# ---- commands -----------------------------------------------------------------
def cmd_start(args, be):
    payload = {}
    if args.spec:
        overrides = dict(kv.split("=", 1) for kv in args.var or [])
        payload.update(load_spec(args.spec, base_dir=args.base_dir, overrides=overrides))
    cwd = os.path.abspath(args.cwd or os.getcwd())
    payload.setdefault("base_dir", os.path.abspath(args.base_dir) if args.base_dir else cwd)
    items = payload.get("items", [])
    for i in args.item or []:
        items.append({"id": i, "group": args.group})
    for path in args.expect or []:
        items.append({"id": path, "group": args.group, "output": path,
                      "label": os.path.basename(path)})
    if args.items_json:
        with open(args.items_json) as fh:
            items.extend(json.load(fh))
    workers = payload.get("workers", [])
    for spec in args.pid or []:
        name, _, pid = spec.rpartition("=")
        workers.append({"name": name or f"pid {pid}", "pid": int(pid)})
    for spec in args.pgrep or []:
        name, _, pat = spec.partition("=")
        workers.append({"name": name, "pgrep": pat} if pat else {"name": spec, "pgrep": spec})
    resolve_workers_now(workers)
    started = None
    if args.since:
        started = parse_time(args.since)
    elif args.since_workers:
        times = [w["started_at"] for w in workers if w.get("started_at")]
        if not times:
            raise StoreError("--since-workers: no live worker found to take a start time from")
        started = min(times)
    payload.update({
        "title": args.title or payload.get("title") or os.path.basename(cwd),
        "repo": args.repo or payload.get("repo") or git_root(payload["base_dir"]) or git_root(cwd) or cwd,
        "cwd": cwd, "thread": args.thread or default_thread(), "items": items,
        "workers": workers, "started_at": started, "host": hostname(),
    })
    if args.no_auto_finish:
        payload["auto_finish"] = False
    res = be.start(payload)
    run = res["run"]
    print(res["id"])
    print(f"  {run['title']}: {run['total']} items, attempt {run.get('attempt') or 1}"
          + (f" (prev {run['prev_run']})" if run.get("prev_run") else "")
          + f"\n  board: {(args.server or board_url()).rstrip('/')}/#{res['id']}", file=sys.stderr)


def resolve_workers_now(workers):
    """Fill pid/start time for workers given by pgrep, so --since-workers works
    and the board shows them immediately."""
    for w in workers:
        if w.get("pid") and not w.get("started_at"):
            w["started_at"] = watch.proc_start_time(w["pid"])
            w["pid_start"] = watch.proc_start_ticks(w["pid"])
            w["status"] = "running" if w["started_at"] else "exited"
        elif w.get("pgrep") and not w.get("pid"):
            found = watch.pgrep(w["pgrep"])
            if found:
                pid, cmd = found[0]
                w.update(pid=pid, cmd=cmd, started_at=watch.proc_start_time(pid),
                         pid_start=watch.proc_start_ticks(pid), status="running")


def cmd_item(args, be):
    *ids, status = args.args
    if not ids:
        raise StoreError("usage: tally item RUN ITEM [ITEM...] STATUS")
    if status not in ITEM_STATES:
        raise StoreError(f"status must be one of {', '.join(ITEM_STATES)}")
    be.op(be.resolve(args.run), "item", {"ids": ids, "status": status, "note": args.note,
                                         "group": args.group, "label": args.label,
                                         "output": args.output, "host": args.host})


def cmd_log(args, be):
    be.op(be.resolve(args.run), "log", {"msg": " ".join(args.msg), "level": args.level})


def cmd_handoff(args, be):
    if not args.id and not args.target:
        raise StoreError("--target is required for a new handoff (or --id to update one)")
    res = be.op(be.resolve(args.run), "handoff", {
        "target": args.target, "what": args.what, "thread": args.thread, "link": args.link,
        "status": args.status, "note": args.note, "handoff_id": args.id})
    print(res["handoff"]["id"])


def cmd_worker(args, be):
    be.op(be.resolve(args.run), "worker", {
        "name": args.name, "pid": args.pid, "status": args.status, "note": args.note,
        "log": os.path.abspath(args.log) if args.log else None, "pgrep": args.pgrep,
        "groups": args.groups, "host": args.host})


def cmd_finish(args, be):
    be.op(be.resolve(args.run), "finish", {"status": args.status, "note": args.note})


def cmd_watch(args, be):
    if be.remote:
        raise StoreError("watch runs where the files are; the server already watches its runs")
    run_id = be.resolve(args.run)
    while True:
        watch.poll(be.store, run_id)
        s = summarize(be.store.load(run_id))
        print(f"\r{s['counts']['done']}/{s['total']} done, {s['workers_alive']} workers alive, "
              f"{fmt_dur(s['duration'])}  ", end="", file=sys.stderr, flush=True)
        if args.once or s["status"] != "running":
            print(file=sys.stderr)
            return
        time.sleep(args.interval)


def _exec_item(be, run_id, item_id, cmd, cwd, shell):
    be.op(run_id, "item", {"ids": [item_id], "status": "running", "host": hostname(),
                           "note": cmd if isinstance(cmd, str) else shlex.join(cmd)})
    t0 = time.time()
    try:
        rc = subprocess.run(cmd, shell=shell, cwd=cwd).returncode
    except OSError as e:
        rc, err = 127, str(e)
    else:
        err = None
    status = "done" if rc == 0 else "failed"
    be.op(run_id, "item", {"ids": [item_id], "status": status,
                           "note": err or f"exit {rc} in {fmt_dur(time.time() - t0)}"})
    return rc


def cmd_exec(args, be):
    run_id = be.resolve(args.run)
    if not args.cmd:
        raise StoreError("usage: tally exec RUN ITEM -- command ...")
    cmd = args.cmd[1:] if args.cmd[0] == "--" else args.cmd
    sys.exit(_exec_item(be, run_id, args.item, cmd, None, False))


def cmd_run(args, be):
    run_id = be.resolve(args.run)
    run = be.detail(run_id)
    todo = [i for i in run["items"] if i.get("cmd")
            and (not args.item or i["id"] in args.item)
            and (args.all or i["status"] in ("queued", "failed"))]
    if not todo:
        print("nothing to run (no queued/failed items with a cmd)", file=sys.stderr)
        return
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        rcs = list(pool.map(lambda i: _exec_item(be, run_id, i["id"], i["cmd"],
                                                  run.get("base_dir"), True), todo))
    failed = sum(1 for rc in rcs if rc)
    print(f"{len(rcs) - failed} ok, {failed} failed", file=sys.stderr)
    sys.exit(1 if failed else 0)


def cmd_status(args, be):
    if args.run:
        d = be.detail(be.resolve(args.run))
        if args.json:
            return print(json.dumps(d, indent=1, default=str))
        s = d["summary"]
        print(f"{d['id']}  [{s['status']}]  {d['title']}")
        print(f"  {s['counts']['done']}/{s['total']} done, {s['counts']['failed']} failed, "
              f"{s['counts']['running']} running · {fmt_dur(s['duration'])} · {d.get('repo')}")
        for w in d.get("workers", []):
            print(f"  worker {w['name']}: {w['status']} pid={w.get('pid')}"
                  + (f" — {w['log_tail'][-1]}" if w.get("log_tail") else ""))
        for h in d.get("handoffs", []):
            print(f"  handoff {h['id']}: {h['from']} → {h['target']} [{h['status']}] {h.get('what') or ''}")
        for it in d["items"]:
            if it["status"] != "done" or args.verbose:
                print(f"    {it['status']:8} {it['id']}" + (f"  ({it['note']})" if it.get("note") else ""))
        return
    runs = be.runs()[: args.limit]
    if args.json:
        return print(json.dumps(runs, indent=1, default=str))
    for r in runs:
        print(f"{r['id']:48} {r['status']:9} {r['counts']['done']:>3}/{r['total']:<3} "
              f"{fmt_dur(r['duration']):>7}  {r['title']}")


def cmd_machines(args, be):
    if be.remote:
        ms = be.remote.get("/api/machines")["machines"]
    else:
        from .hosts import online_map
        ms = api.machines(be.store.list_runs(), online=online_map())
    if args.json:
        return print(json.dumps(ms, indent=1, default=str))
    for m in ms:
        state = {True: "online", False: "offline", None: "?"}[m["online"]]
        it = m["items"]
        print(f"{m['name']}{' (board)' if m['self'] else ''} [{state}]"
              + (f"  items {it['done']}/{it['total']} done, {it['running']} running" if it["total"] else ""))
        for r in m["runs"]:
            print(f"  run     {r['id']}  {r['title']}")
        for w in m["workers"]:
            print(f"  worker  {w['name']} pid={w['pid']} up {fmt_dur(time.time() - (w['started_at'] or time.time()))}")
        for h in m["handoffs_in"]:
            print(f"  handoff {h['from']} → here [{h['status']}] {h.get('what') or ''}"
                  + (f" ({h['done']}/{h['total']})" if h["total"] else ""))


def cmd_serve(args, be):
    from .server import serve
    serve(host=args.host, port=args.port, root=args.home, interval=args.interval)


def cmd_url(args, be):
    print(args.server or board_url())


# ---- parser -------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="tally", description="Tallyboard: live board for long-running agent jobs.")
    p.add_argument("--server", default=os.environ.get("TALLYBOARD_SERVER") or os.environ.get("JOBBOARD_SERVER"),
                   help="report to a remote board, e.g. http://host:8790 (env TALLYBOARD_SERVER)")
    p.add_argument("--token", default=os.environ.get("TALLYBOARD_TOKEN") or os.environ.get("JOBBOARD_TOKEN"),
                   help="POST token (env TALLYBOARD_TOKEN)")
    p.add_argument("--home", default=None, help="state dir (env TALLYBOARD_HOME, default ~/.local/share/tallyboard)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="register a run; prints its id")
    s.add_argument("--title")
    s.add_argument("--spec", help="job spec JSON (repeatable job)")
    s.add_argument("--var", action="append", metavar="K=V", help="override a spec var")
    s.add_argument("--base-dir", help="dir that item outputs are relative to (default: spec rule / cwd)")
    s.add_argument("--repo"); s.add_argument("--cwd")
    s.add_argument("--thread", help="thread id or name (default $T3CODE_THREAD_ID)")
    s.add_argument("--item", action="append", metavar="ID", help="add an item (repeatable)")
    s.add_argument("--expect", action="append", metavar="PATH_OR_GLOB",
                   help="add an item that is done when this output file appears")
    s.add_argument("--group", help="group for --item/--expect items")
    s.add_argument("--items-json", help="JSON list of item objects")
    s.add_argument("--pid", action="append", metavar="[NAME=]PID", help="watch a worker pid")
    s.add_argument("--pgrep", action="append", metavar="NAME=REGEX", help="watch a worker found by command line")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--since", metavar="TIME",
                   help="backdate start: epoch, ISO, `ps -o lstart` text, or pid:N")
    g.add_argument("--since-workers", action="store_true", help="backdate to earliest live worker start")
    s.add_argument("--no-auto-finish", action="store_true", help="don't mark done when all items are done")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("item", help="set item status: item RUN ITEM [ITEM...] STATUS")
    s.add_argument("run"); s.add_argument("args", nargs="+", metavar="ITEM... STATUS")
    s.add_argument("-m", "--note"); s.add_argument("--group"); s.add_argument("--label")
    s.add_argument("--output", help="path/glob to watch for this item")
    s.add_argument("--host", help="machine doing this item (default: the run's host)")
    s.set_defaults(fn=cmd_item)

    for name in ("log", "note"):
        s = sub.add_parser(name, help="add a log line to a run")
        s.add_argument("run"); s.add_argument("msg", nargs="+")
        s.add_argument("--level", default="info", choices=["info", "warn", "error"])
        s.set_defaults(fn=cmd_log)

    s = sub.add_parser("handoff", help="record work sent to another machine/thread; prints handoff id")
    s.add_argument("run")
    s.add_argument("--target", help="target env/machine/thread name, e.g. gpu-node")
    s.add_argument("--what", help="what was sent")
    s.add_argument("--thread", help="child thread id"); s.add_argument("--link", help="child thread link")
    s.add_argument("--status", choices=HANDOFF_STATES); s.add_argument("-m", "--note")
    s.add_argument("--id", help="update an existing handoff (h1, h2, ...)")
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("worker", help="add/update a worker (pid, log, status)")
    s.add_argument("run"); s.add_argument("name")
    s.add_argument("--pid", type=int); s.add_argument("--pgrep", help="regex to (re)find the process")
    s.add_argument("--log", help="log file to tail"); s.add_argument("--groups", nargs="+")
    s.add_argument("--status", choices=WORKER_STATES); s.add_argument("-m", "--note")
    s.add_argument("--host", help="machine the worker runs on (default: the run's host)")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("finish", help="mark a run finished")
    s.add_argument("run"); s.add_argument("--status", default="done", choices=RUN_STATES)
    s.add_argument("-m", "--note")
    s.set_defaults(fn=cmd_finish)

    s = sub.add_parser("watch", help="poll output files/pids for a run (the server does this too)")
    s.add_argument("run", nargs="?"); s.add_argument("--interval", type=float, default=3.0)
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("exec", help="run a command as an item: exec RUN ITEM -- cmd ...")
    s.add_argument("run"); s.add_argument("item"); s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_exec)

    s = sub.add_parser("run", help="run the spec's per-item commands")
    s.add_argument("run"); s.add_argument("--item", action="append")
    s.add_argument("-j", "--jobs", type=int, default=1)
    s.add_argument("--all", action="store_true", help="also re-run done items")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="list runs, or show one run")
    s.add_argument("run", nargs="?"); s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("machines", help="what is running on which machine")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_machines)

    s = sub.add_parser("serve", help="run the web board + watcher")
    s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--interval", type=float, default=3.0)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("url", help="print the board URL")
    s.set_defaults(fn=cmd_url)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        be = None if args.cmd == "serve" else Backend(args)
        args.fn(args, be)
    except StoreError as e:
        print(f"tally: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
