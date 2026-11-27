<p align="center">
  <img src="assets/icon.svg" width="72" height="72" alt="Tallyboard logo">
</p>

<h1 align="center">Tallyboard</h1>

<p align="center">
  A tiny, zero-dependency live dashboard for long-running agent work: batch builds, renders, file-watched outputs, and cross-machine handoffs over Tailscale.
</p>

---

**Tallyboard** gives every repo, shell script, and AI coding agent on your network one shared live board. It is written in **Python 3 stdlib only**, with no build step, no database, and no external packages: one CLI (`tally`), one HTTP server, and one single-file live web page.

## Highlights

- **Shared board, any repo:** One state directory (`~/.local/share/tallyboard/runs/`) stores every run as `run.json`, an append-only `events.jsonl`, and uploaded thumbnails. Active runs appear first; completed runs stay in searchable history.
- **File & PID watching for silent tools:** Tools like Blender buffer stdout. Declare expected outputs (paths or globs) and worker PIDs/`pgrep` regexes, and Tallyboard infers progress every 3 seconds as fresh files land.
- **Repeatable job specs:** Commit a `.tallyboard/<name>.json` file in any repo. Re-running it chains attempts (`#1`, `#2`, …) so you can compare durations side by side.
- **Automatic multi-machine & Tailscale tracking:** A **Machines** strip shows what is running on every host, with live online/offline status from `tailscale status --json`. Any new machine added to your tailnet is discovered automatically — remote HTTP reports are credited to the sending machine by IP with zero configuration.
- **Works from Windows, Linux, and macOS:** Remote workers can report via `tally --server URL`, `curl`, or a one-line PowerShell `Invoke-RestMethod` — including raw PNG uploads that render as thumbnails on the board.
- **Agent-ready:** Includes a Claude Code skill template (`skill/SKILL.md`) and reads `$T3CODE_THREAD_ID` / `$CLAUDE_CODE_SESSION_ID` automatically.

## Quick Start

```bash
git clone https://github.com/stephenwil/tallyboard.git
cd tallyboard
./install.sh
```

`install.sh` puts `tally` (and `tallyboard`) in `~/.local/bin/`, installs the Claude Code skill with your auto-detected board URL, and enables the `tallyboard.service` systemd user unit on port `8790` (override with `TALLYBOARD_PORT`).

On macOS or systems without `systemd --user`:

```bash
./bin/tally serve --host 0.0.0.0 --port 8790
```

Print your board's URL (automatically uses Tailscale MagicDNS when available):

```bash
tally url
```

## 30-Second Example

```bash
# Start a run with 3 items
RUN=$(tally start --title "Render hero shots" --item shot-1 --item shot-2 --item shot-3 --group Heroes)

# Update progress (or wrap a command with `tally exec`)
tally item $RUN shot-1 running
tally item $RUN shot-1 done -m "41 s"
tally exec $RUN shot-2 -- ./render.sh shot-2

# Hand off an item to another machine on your tailnet
H=$(tally handoff $RUN --target gpu-node --what "Cycles render shot-3")
tally item $RUN shot-3 queued --host gpu-node
```

On the remote Windows machine (PowerShell):

```powershell
$B='http://workstation.your-tailnet.ts.net:8790'; $R='<run-id>'
Invoke-RestMethod -Method Post "$B/api/runs/$R/item" -ContentType 'application/json' -Body (@{id='shot-3';status='done';note='18 s'}|ConvertTo-Json)
Invoke-RestMethod -Method Post "$B/api/runs/$R/upload?item=shot-3&name=shot-3.png" -InFile .\shot-3.png -ContentType 'image/png'
```

## Documentation

- **[How-To Manual](docs/MANUAL.md)** — complete guide covering installation, CLI commands, file & PID watching, job specs, multi-machine Tailscale workflows, AI agent integration, security, and backups.
- **[Example spec](examples/batch-render.json)** — repeatable batch render spec with `item_sources`, `vars`, and worker watching.
- **[Claude Code skill](skill/SKILL.md)** — drop-in skill template installed automatically by `install.sh`.

## Tests

```bash
python3 -m unittest -v
```

## License

[MIT](LICENSE)
