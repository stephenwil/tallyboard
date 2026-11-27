#!/usr/bin/env bash
# Install Tallyboard for this user: CLI on PATH, Claude Code skill, and user systemd service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
PORT="${TALLYBOARD_PORT:-${JOBBOARD_PORT:-8790}}"
HOST="$(hostname)"

chmod +x "$REPO/bin/tally"

mkdir -p ~/.local/bin ~/.claude/skills/tallyboard ~/.config/systemd/user

# CLI symlinks
ln -sfn "$REPO/bin/tally" ~/.local/bin/tally
ln -sfn "$REPO/bin/tally" ~/.local/bin/tallyboard

# If upgrading from the earlier `jobboard` name, migrate state and keep a compat alias
if [ -L ~/.local/bin/jobboard ]; then
  ln -sfn "$REPO/bin/tally" ~/.local/bin/jobboard
fi
if [ -d ~/.local/share/jobboard ] && [ ! -e ~/.local/share/tallyboard ]; then
  mv ~/.local/share/jobboard ~/.local/share/tallyboard
  echo "Migrated history: ~/.local/share/jobboard -> ~/.local/share/tallyboard"
fi
if [ -d ~/.claude/skills/jobboard ]; then
  rm -rf ~/.claude/skills/jobboard
fi

# Detect the board URL (prefers Tailscale MagicDNS when available)
BOARD_URL="$(TALLYBOARD_PORT="$PORT" "$REPO/bin/tally" url)"
BOARD_BASE="${BOARD_URL%/}"

# Install Claude Code skill with this host's detected URL
sed -e "s#@BOARD_URL@#$BOARD_URL#g" \
    -e "s#@BOARD_BASE@#$BOARD_BASE#g" \
    -e "s#@HOST@#$HOST#g" \
    -e "s#@REPO@#$REPO#g" \
    "$REPO/skill/SKILL.md" > ~/.claude/skills/tallyboard/SKILL.md

# Install user systemd service (if systemd user session is available)
if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
  if systemctl --user is-active --quiet jobboard.service 2>/dev/null || \
     systemctl --user is-enabled --quiet jobboard.service 2>/dev/null; then
    systemctl --user disable --now jobboard.service || true
    rm -f ~/.config/systemd/user/jobboard.service
  fi
  sed -e "s#@REPO@#$REPO#g" -e "s#@PORT@#$PORT#g" "$REPO/systemd/tallyboard.service" \
    > ~/.config/systemd/user/tallyboard.service
  systemctl --user daemon-reload
  systemctl --user enable --now tallyboard.service
  systemctl --user restart tallyboard.service
else
  echo "Note: systemd --user not available; start manually with: tally serve --host 0.0.0.0 --port $PORT"
fi

echo "Tallyboard installed: $BOARD_URL"
