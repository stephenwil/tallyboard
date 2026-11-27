---
name: tallyboard
description: Use when starting or tracking any long-running or batch job (Blender builds, renders, exports, test sweeps, anything with many outputs or background workers), or when handing work off to another machine or thread (delegate_task to a remote host or GPU workstation). Registers the job on the shared Tallyboard live web dashboard so the user can watch progress over Tailscale, and lets the job be repeated from a committed spec.
---

# Tallyboard

A shared, live dashboard for long-running agent work — one board for every repo and
thread. Board URL (share this with the user when registering a run):

**@BOARD_URL@** (a run's own card is at `@BOARD_BASE@/#<run-id>`)

The server runs on **@HOST@** as the user service `tallyboard.service`. History lives in
`~/.local/share/tallyboard/runs/<run-id>/` (`run.json` + `events.jsonl` + `files/`).
Source & full manual: `@REPO@` (`docs/MANUAL.md`).

## When to use it

- Any batch build, render, export, or test sweep that takes more than a minute or produces many outputs.
- Background workers whose progress you'd otherwise check by hand (`ls`, `pgrep`).
- Every handoff to another machine or thread (e.g. `delegate_task` to a remote GPU host).
- Tell the user the board URL once you've registered the run.

## CLI (`tally`, on PATH on @HOST@)

```bash
# Ad-hoc run. Prints the run id; --thread defaults to $T3CODE_THREAD_ID
RUN=$(tally start --title "Render hero shots" --item hero1 --item hero2 --group Heroes)

# Or: items done automatically when their output file appears (newer than run start)
RUN=$(tally start --title "Export STLs" --expect 'out/a.stl' --expect 'out/b.stl' \
        --pgrep 'blender=blender -b .*export_stl')

tally item   $RUN hero1 running
tally item   $RUN hero1 done -m "42 s" --output "renders/hero1.png" # queued | running | done | failed | skipped
tally log    $RUN "switched to 64 samples"         # alias: note; --level warn|error
tally exec   $RUN hero2 -- blender -b …            # running -> done/failed by exit code
tally worker $RUN "W1" --pid 12345 --log /tmp/build.log
tally finish $RUN                                  # or --status failed|cancelled -m why
tally status [$RUN]                                # text summary; `last` = newest active run
tally machines                                     # what is running on which machine
```

`RUN` can be `last` (the newest active run, preferring this thread's).

## Critical Rule: Visual Outputs & Images (Renders, Slices, Contact Sheets)

The live board shows image thumbnails directly on item tiles and in an expanded **Images** gallery, but **only if the output image path is declared or uploaded**:

1. **When starting with `--expect` (preferred for local builds/renders):**
   - Provide the relative path or glob: `tally start --title "..." --expect "renders/*.png" --expect "previews/*.jpg"`
   - Each matching file automatically marks its item `done` and renders as a live thumbnail on the board.
2. **When updating items dynamically via `tally item`:**
   - Always supply `--output <path>` if the item produced an image:
     `tally item $RUN preview done --output "previews/contact_sheet.png"`
3. **In repeatable specs (`.tallyboard/*.json`):**
   - Always set `"output": "renders/{stem}.png"` on `item_sources` or `"output": "previews/sheet.png"` on `items`.

**Tools that can't report** (e.g. Blender buffering stdout): give items an `output`
(path or glob relative to the run's `base_dir`). The server polls every 3 s. An item
is done when ≥ `min_files` matching files exist with mtime ≥ the run's start.
Files older than the run show as "old output". Worker PIDs on `@HOST@` show
running/exited/failed (a worker counts as failed if its `log` matches `Traceback` /
`Error:`). Use `--since "<ps -o lstart text>" | ISO | epoch | pid:N` or
`--since-workers` to backdate the start when registering an already-running job.

## Repeatable jobs: spec files

Commit `tallyboard.json` or `.tallyboard/<name>.json` in the repo. Its `base_dir`
defaults to the repo root (the parent of `.tallyboard/`):

```json
{
  "name": "scene-renders",
  "title": "Cycles renders, Batch {batch}",
  "vars": {"batch": "1"},
  "item_sources": [
    {
      "glob": "src/Batch {batch}/*.blend",
      "id": "{stem}",
      "group": "Batch {batch}",
      "label_sub": {"_final$": ""},
      "output": "renders/Batch {batch}/{stem}.png",
      "cmd": "blender -b '{path}' -P tools/render.py"
    }
  ],
  "items": [{"id": "contact-sheet", "output": "renders/contact_{batch}.png"}],
  "workers": [{"name": "blender", "pgrep": "blender -b .*render\\.py", "groups": ["Batch {batch}"]}]
}
```

Template fields per matched file: `{path} {name} {stem} {ext} {parent} {dir}`. Vars
(`--var batch=2`) fill `{batch}` anywhere. A worker with `groups` marks the next
queued item in those groups as running (inferred) while it is alive.

```bash
tally start --spec .tallyboard/scene-renders.json --var batch=2   # fresh run
tally run last -j 2       # optional: execute per-item "cmd"s (queued/failed ones)
```

Re-running the same spec `name` creates a new run linked to earlier ones (`attempt`,
`prev_run`), so durations are compared side by side on the board.

## Handoffs to another machine/thread

```bash
H=$(tally handoff $RUN --target GPU-NODE --what "Cycles GPU renders (50 PNGs)")
# ... delegate_task ... then record the child thread:
tally handoff $RUN --id $H --thread <childThreadId> --link <thread-url> --status running
tally handoff $RUN --id $H --status done -m "50/50 rendered"
```

Give the child the run id and the remote-reporting lines below so its items tick over
live. Any machine on the Tailscale tailnet is discovered automatically (no config
needed) and credited by its Tailscale IP without sending a `host` field. To pre-assign
items ahead of time: `tally item $RUN <ids…> queued --host GPU-NODE`.

## Remote reporting (other machines, over Tailscale / HTTP)

Plain JSON POSTs to `@BOARD_BASE@`:
`POST /api/runs`, `POST /api/runs/<id>/{item|log|handoff|worker|finish}`,
`POST /api/runs/<id>/upload?item=<id>&name=<file.png>` (raw body, shown as a thumbnail).

```bash
# Linux/macOS: CLI or curl
tally --server @BOARD_BASE@ item $RUN "shot-01" done -m "41 s"
curl -s -XPOST -H 'Content-Type: application/json' \
  -d '{"id":"shot-01","status":"done","note":"41 s"}' \
  @BOARD_BASE@/api/runs/$RUN/item
```

```powershell
# Windows PowerShell
$B='@BOARD_BASE@'; $R='<run-id>'
Invoke-RestMethod -Method Post "$B/api/runs/$R/item" -ContentType 'application/json' -Body (@{id='shot-01';status='done';note='41 s'}|ConvertTo-Json)
# upload an image so it appears as a thumbnail:
Invoke-RestMethod -Method Post "$B/api/runs/$R/upload?item=$([uri]::EscapeDataString('shot-01'))&name=shot-01.png" -InFile .\shot-01.png -ContentType 'image/png'
```

Notes: the server only watches local files and PIDs on `@HOST@`. Remote machines
report explicitly via HTTP (`item`/`worker`/`log`/`upload`). If `TALLYBOARD_TOKEN` is
set on the server, include header `X-Tallyboard-Token` (or `--token`).
