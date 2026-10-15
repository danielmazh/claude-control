#!/bin/bash
# Install Claude Control into ~/.claude/dashboard and run it with launchd:
# starts at login, restarts after a crash, stays down after the in-app shutdown
# button, never runs twice. Safe to run again (updates the files, keeps your state).
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/dashboard"
LABEL="${CC_LABEL:-local.claude-control}"
UID_NUM=$(id -u)

mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/server.py" "$SRC/index.html" "$SRC/README.md" "$SRC/cl-dash" "$DEST/"
chmod +x "$DEST/cl-dash"
echo "files installed in $DEST"

# Reuse an existing agent (any label ending in .claude-control); create one otherwise.
PLIST=$(ls "$HOME"/Library/LaunchAgents/*.claude-control.plist 2>/dev/null | head -1 || true)
if [ -z "$PLIST" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
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
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
echo "launchd agent running: $LABEL"

if ! grep -q "alias cl-dash=" "$HOME/.zshrc" 2>/dev/null; then
  printf '\n# [claude-control] open the dashboard (launchd keeps the server up)\nalias cl-dash=%s\n' "'$DEST/cl-dash'" >> "$HOME/.zshrc"
  echo "alias cl-dash added to ~/.zshrc (open a new terminal)"
fi
echo "open: http://127.0.0.1:7799"
