#!/bin/zsh
# Keeps one `claude remote-control` (the phone bridge) alive in a tmux window.
# Run every 60s by launchd (install.sh --with-restore). Safe to run by hand.
# Never starts a second copy, and never restarts while the network is down.
#
# Settings (environment):
#   CC_RC_DAEMON_TARGET  tmux session:window       (default claude-RC-general:rc-daemon)
#   RC_DAEMON_SESSION    tmux session, overrides the target part
#   RC_DAEMON_WINDOW     tmux window, overrides the target part
#   RC_DAEMON_DIR        working folder of the daemon       (default $HOME)
#   RC_LOG_DIR           folder for the logs                (default ~/Library/Logs)
#   CLAUDE_BIN           claude binary (default: newest nvm, then homebrew, then PATH)
#   TMUX_BIN             tmux binary                        (default: from PATH)
#   DRY_RUN=1            print what it would do, change nothing

# launchd starts jobs with PATH=/usr/bin:/bin:/usr/sbin:/sbin. If this script starts
# the tmux server (for example after a reboot), tmux plugins need `tmux` in PATH.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

TARGET="${CC_RC_DAEMON_TARGET:-claude-RC-general:rc-daemon}"
SESSION="${RC_DAEMON_SESSION:-${TARGET%%:*}}"
WINDOW="${RC_DAEMON_WINDOW:-${TARGET#*:}}"
DIR="${RC_DAEMON_DIR:-$HOME}"
LOG_DIR="${RC_LOG_DIR:-$HOME/Library/Logs}"
LOG="$LOG_DIR/claude-rc-watchdog.log"
DLOG="$LOG_DIR/claude-rc-daemon.log"
TMUX_BIN="${TMUX_BIN:-$(command -v tmux)}"
LOCK="${TMPDIR:-/tmp}/claude-rc-watchdog.${SESSION}.lock"

[[ -z "$DRY_RUN" ]] && mkdir -p "$LOG_DIR"
# A dry run prints only: it writes nothing, not even the log.
log() {
  local line="$(date '+%F %T') $*"
  if [[ -n "$DRY_RUN" ]]; then print -r -- "$line"; else print -r -- "$line" >> "$LOG"; fi
}
run() { if [[ -n "$DRY_RUN" ]]; then log "DRY: $*"; else "$@"; fi; }

[[ -n "$TMUX_BIN" && -x "$TMUX_BIN" ]] || { log "ERROR: tmux not found (brew install tmux)"; exit 1; }
[[ -d "$DIR" ]] || DIR="$HOME"

# Claude binary: CLAUDE_BIN, else the newest nvm install, else homebrew, else PATH.
if [[ -z "$CLAUDE_BIN" ]]; then
  for c in "$HOME"/.nvm/versions/node/*/bin/claude(NnOn) /opt/homebrew/bin/claude \
           /usr/local/bin/claude "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null)"; do
    [[ -n "$c" && -x "$c" ]] && { CLAUDE_BIN="$c"; break; }
  done
fi
[[ -z "$CLAUDE_BIN" ]] && { log "ERROR: claude binary not found"; exit 1; }

# One run at a time. A lock older than 10 minutes is left over from a killed run.
if [[ -z "$DRY_RUN" ]]; then
  if [[ -d "$LOCK" ]] && (( $(date +%s) - $(stat -f %m "$LOCK") > 600 )); then
    rmdir "$LOCK" 2>/dev/null
  fi
  mkdir "$LOCK" 2>/dev/null || exit 0
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
fi

# Anchored at line end, so a shell or editor that only mentions the words does not count.
running() { pgrep -f "(^|/)claude remote-contro[l]\$" >/dev/null 2>&1; }

# Session: reuse if it exists, create if not.
if ! "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; then
  run "$TMUX_BIN" new-session -d -s "$SESSION" -c "$DIR" && log "created tmux session $SESSION"
fi

# Two checks 10s apart, so a manual restart loop in its short gap is not doubled.
running && { [[ -n "$DRY_RUN" ]] && log "remote-control is running, nothing to do"; exit 0; }
[[ -z "$DRY_RUN" ]] && sleep 10
running && exit 0

# No restart while api.anthropic.com is unreachable: a daemon started now would fail
# again and again. The next run (60s) tries again. Any HTTP answer, even 404, means
# the network is up.
if ! curl -s -o /dev/null -m 8 https://api.anthropic.com/; then
  log "network down (api.anthropic.com unreachable), remote-control not restarted"
  exit 0
fi

# Drop the old window(s) from the last run, then start a fresh one. First copy the
# last screen of a dead daemon into the log, so the reason it stopped is kept (the
# window stays open after exit, see remain-on-exit below).
# Window ids (@N), not indexes: renumber-windows shifts indexes after each kill.
for w in $("$TMUX_BIN" list-windows -t "=$SESSION" -F '#{window_id}|#{window_name}|#{pane_dead}' 2>/dev/null); do
  [[ "${${w#*|}%|*}" == "$WINDOW" ]] || continue
  if [[ "${w##*|}" == 1 ]]; then
    log "last output of the stopped remote-control:"
    [[ -z "$DRY_RUN" ]] && "$TMUX_BIN" capture-pane -p -t "${w%%|*}" -S -40 2>/dev/null | grep -v '^[[:space:]]*$' | tail -15 | sed 's/^/    | /' >> "$LOG"
  fi
  run "$TMUX_BIN" kill-window -t "${w%%|*}" 2>/dev/null
done

if [[ -n "$DRY_RUN" ]]; then
  log "DRY: would start '$CLAUDE_BIN remote-control' in $SESSION:$WINDOW (cwd $DIR), log to $DLOG"
  exit 0
fi

# ANTHROPIC_BASE_URL is cleared so remote-control talks to the Anthropic API directly.
# Keep the window name (so the next run finds it) and keep the window after exit
# (for the log above). Targeted by window id; claude takes seconds to start, so the
# options are set long before it could exit.
if wid=$("$TMUX_BIN" new-window -d -P -F '#{window_id}' -t "=$SESSION:" -n "$WINDOW" -c "$DIR" \
     "ANTHROPIC_BASE_URL= PATH=${CLAUDE_BIN:h}:/opt/homebrew/bin:/usr/local/bin:\$PATH /usr/bin/caffeinate -is ${(q)CLAUDE_BIN} remote-control"); then
  for opt in "remain-on-exit on" "automatic-rename off" "allow-rename off"; do
    "$TMUX_BIN" set-option -w -t "$wid" ${=opt} 2>/dev/null
  done
  # Copy the daemon output to a log. Its environment link scrolls out of the pane,
  # and the Claude Control dashboard reads the link from this file. Kept under 5 MB.
  [[ -f "$DLOG" && $(stat -f %z "$DLOG") -gt 5000000 ]] && tail -c 1000000 "$DLOG" > "$DLOG.tmp" && mv -f "$DLOG.tmp" "$DLOG"
  "$TMUX_BIN" pipe-pane -o -t "$wid" "cat >> ${(q)DLOG}" 2>/dev/null
  log "started remote-control in $SESSION:$WINDOW ($CLAUDE_BIN)"
else
  log "ERROR: could not open tmux window"
  exit 1
fi
