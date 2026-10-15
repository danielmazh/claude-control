#!/bin/bash
# Install Claude Control into ~/.claude/dashboard and run it with launchd:
# starts at login, restarts after a crash, stays down after the in-app shutdown
# button, never runs twice. Safe to run again (updates the files, keeps your state).
#
#   ./install.sh                  dashboard only
#   ./install.sh --with-restore   also the session restore scripts and their agents
#   ./install.sh --with-restore --force   also replace restore scripts you changed
#
# Environment: CC_LABEL (dashboard agent label), CC_LABEL_PREFIX (restore agent
# prefix, default local), CC_BIN_DIR (restore scripts, default ~/.local/bin),
# CC_NO_LAUNCHCTL=1 (write files only, do not load agents: for tests).
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/dashboard"
LABEL="${CC_LABEL:-local.claude-control}"
UID_NUM=$(id -u)
AGENTS="$HOME/Library/LaunchAgents"

WITH_RESTORE=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --with-restore) WITH_RESTORE=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

lctl() {
  if [ -n "${CC_NO_LAUNCHCTL:-}" ]; then
    echo "  (CC_NO_LAUNCHCTL set: skipped launchctl $*)"
    return 0
  fi
  launchctl "$@"
}

mkdir -p "$DEST" "$AGENTS" "$HOME/Library/Logs"
cp "$SRC/server.py" "$SRC/index.html" "$SRC/README.md" "$SRC/cl-dash" "$DEST/"
chmod +x "$DEST/cl-dash"
echo "files installed in $DEST"

# Reuse an existing agent (any label ending in .claude-control); create one otherwise.
PLIST=$(ls "$AGENTS"/*.claude-control.plist 2>/dev/null | head -1 || true)
if [ -z "$PLIST" ]; then
  PLIST="$AGENTS/$LABEL.plist"
  PY=$(command -v python3)
  NODEBIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DEST/server.py</string></array>
  <key>WorkingDirectory</key><string>$DEST</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CC_NO_BROWSER</key><string>1</string>
    <key>PATH</key><string>${NODEBIN:+$NODEBIN:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/claude-control.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/claude-control.log</string>
</dict>
</plist>
EOF
  echo "launchd agent written: $PLIST"
fi
LABEL=$(basename "$PLIST" .plist)

# Restart the agent so it serves the new files.
if [ -n "${CC_NO_LAUNCHCTL:-}" ]; then
  echo "  (CC_NO_LAUNCHCTL set: agent $LABEL not loaded)"
else
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$PLIST"
  echo "launchd agent running: $LABEL"
fi

if ! grep -q "alias cl-dash=" "$HOME/.zshrc" 2>/dev/null; then
  printf '\n# [claude-control] open the dashboard (launchd keeps the server up)\nalias cl-dash=%s\n' "'$DEST/cl-dash'" >> "$HOME/.zshrc"
  echo "alias cl-dash added to ~/.zshrc (open a new terminal)"
fi

# ── optional: session restore (--with-restore) ──────────────────────────────
if [ "$WITH_RESTORE" = 1 ]; then
  echo
  echo "session restore:"
  BIN="${CC_BIN_DIR:-$HOME/.local/bin}"
  PREFIX="${CC_LABEL_PREFIX:-local}"
  CONF="$HOME/.config/claude-rc/sessions.conf"
  mkdir -p "$BIN" "$(dirname "$CONF")"
  command -v tmux >/dev/null 2>&1 || echo "  note: tmux not found. Install it first: brew install tmux"

  # Scripts: a copy you changed is kept unless --force.
  for s in claude-rc-watchdog.sh claude-rc-resume.sh; do
    if [ ! -e "$BIN/$s" ]; then
      cp "$SRC/scripts/$s" "$BIN/$s"; chmod +x "$BIN/$s"
      echo "  script installed: $BIN/$s"
    elif cmp -s "$SRC/scripts/$s" "$BIN/$s"; then
      echo "  script up to date: $BIN/$s"
    elif [ "$FORCE" = 1 ]; then
      cp "$BIN/$s" "$BIN/$s.bak"
      cp "$SRC/scripts/$s" "$BIN/$s"; chmod +x "$BIN/$s"
      echo "  script replaced (--force, old copy in $s.bak): $BIN/$s"
    else
      echo "  script kept (yours differs, use --force to replace): $BIN/$s"
    fi
  done

  if [ ! -e "$CONF" ]; then
    cat > "$CONF" <<'EOF'
# Claude Control: Claude sessions to bring back after a reboot or login.
# One session per line:  name|session-id|folder
#   name        tmux window name and Remote Control name (A-Z, 0-9, . _ -)
#   session-id  the id that `claude --resume` takes
#   folder      the folder the session runs in (empty = your home folder)
# Add and remove sessions in the dashboard (Sessions and Restore, Session search).
# Lines that start with # are ignored. Removed sessions are commented out, not deleted.
EOF
    echo "  sessions file created: $CONF"
  else
    echo "  sessions file kept: $CONF"
  fi

  # Agents: reuse any agent whose label ends in the same suffix.
  # write_agent <suffix> <script> <schedule xml>
  write_agent() {
    local suffix="$1" script="$2" sched="$3" found plist label
    found=$(ls "$AGENTS"/*."$suffix".plist 2>/dev/null | head -1 || true)
    if [ -n "$found" ]; then
      echo "  launchd agent kept: $found"
      return 0
    fi
    label="$PREFIX.$suffix"
    plist="$AGENTS/$label.plist"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>/bin/zsh</string><string>$BIN/$script</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
$sched
  <key>StandardOutPath</key><string>$HOME/Library/Logs/$suffix.launchd.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/$suffix.launchd.log</string>
</dict>
</plist>
EOF
    echo "  launchd agent written: $plist"
    lctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    lctl bootstrap "gui/$UID_NUM" "$plist"
    [ -z "${CC_NO_LAUNCHCTL:-}" ] && echo "  launchd agent running: $label"
    return 0
  }
  write_agent claude-rc-watchdog claude-rc-watchdog.sh \
    '  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>'
  write_agent claude-rc-resume claude-rc-resume.sh \
    '  <key>RunAtLoad</key><true/>'
  echo "  done. Add sessions on the Sessions and Restore page."
fi

echo "open: http://127.0.0.1:7799"
