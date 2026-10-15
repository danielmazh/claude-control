#!/bin/zsh
# Brings back the Claude sessions listed in the sessions file after a reboot or
# login, each in its own tmux window with Remote Control on. Run once at login by
# launchd (install.sh --with-restore). Safe to run by hand. It never touches a
# session that is already running, and never touches a window that runs something
# other than an idle shell.
#
# Sessions file: one session per line, `name|session-id|folder`. Lines that start
# with # are ignored. An empty folder means $RC_DIR.
#
# Settings (environment):
#   RC_MANIFEST  sessions file      (default ~/.config/claude-rc/sessions.conf)
#   RC_SESSION   tmux session       (default rc-resume)
#   RC_DIR       default folder     (default $HOME)
#   RC_CMD       resume command     (default: ANTHROPIC_BASE_URL= <claude binary>)
#   RC_WAIT=0    do not wait for tmux and the network first (default 1)
#   RC_GAP       seconds between two starts (default 10)
#   RC_LOG_DIR   folder for the log (default ~/Library/Logs)
#   CLAUDE_BIN   claude binary (default: newest nvm, then homebrew, then PATH)
#   TMUX_BIN     tmux binary (default: from PATH)
#   DRY_RUN=1    print what it would do, change nothing

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

MANIFEST="${RC_MANIFEST:-$HOME/.config/claude-rc/sessions.conf}"
SESSION="${RC_SESSION:-rc-resume}"
DEFAULT_DIR="${RC_DIR:-$HOME}"
WAIT="${RC_WAIT:-1}"
LOG_DIR="${RC_LOG_DIR:-$HOME/Library/Logs}"
LOG="$LOG_DIR/claude-rc-resume.log"
TMUX_BIN="${TMUX_BIN:-$(command -v tmux)}"
LOCK="${TMPDIR:-/tmp}/claude-rc-resume.${SESSION}.lock"

[[ -z "$DRY_RUN" ]] && mkdir -p "$LOG_DIR"
# A dry run prints only: it writes nothing, not even the log.
log() {
  local line="$(date '+%F %T') $*"
  if [[ -n "$DRY_RUN" ]]; then print -r -- "$line"; else print -r -- "$line" | tee -a "$LOG"; fi
}
run() { if [[ -n "$DRY_RUN" ]]; then log "DRY: $*"; else "$@"; fi; }

[[ -r "$MANIFEST" ]] || { log "ERROR: no sessions file at $MANIFEST"; exit 1; }
[[ -n "$TMUX_BIN" && -x "$TMUX_BIN" ]] || { log "ERROR: tmux not found (brew install tmux)"; exit 1; }

# Claude binary: CLAUDE_BIN, else the newest nvm install, else homebrew, else PATH.
if [[ -z "$RC_CMD" ]]; then
  if [[ -z "$CLAUDE_BIN" ]]; then
    for c in "$HOME"/.nvm/versions/node/*/bin/claude(NnOn) /opt/homebrew/bin/claude \
             /usr/local/bin/claude "$HOME/.local/bin/claude" "$(command -v claude 2>/dev/null)"; do
      [[ -n "$c" && -x "$c" ]] && { CLAUDE_BIN="$c"; break; }
    done
  fi
  [[ -z "$CLAUDE_BIN" ]] && { log "ERROR: claude binary not found"; exit 1; }
  # ANTHROPIC_BASE_URL is cleared so Remote Control talks to the Anthropic API directly.
  RC_CMD="ANTHROPIC_BASE_URL= ${(q)CLAUDE_BIN}"
fi
CMD="$RC_CMD"

# One copy at a time. A lock older than 30 minutes is left over from a killed run.
if [[ -d "$LOCK" ]] && (( $(date +%s) - $(stat -f %m "$LOCK") > 1800 )); then
  run rmdir "$LOCK" 2>/dev/null
fi
if [[ -z "$DRY_RUN" ]]; then
  mkdir "$LOCK" 2>/dev/null || { log "another run holds $LOCK, exit"; exit 0; }
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
fi

if [[ "$WAIT" == 1 ]]; then
  # Wait up to 3 min for the tmux server (the watchdog starts it after a reboot).
  for i in {1..36}; do "$TMUX_BIN" ls >/dev/null 2>&1 && break; sleep 5; done
  # Let a tmux session-restore plugin (for example tmux-continuum) finish first:
  # the server must be up 60s.
  if start=$("$TMUX_BIN" display -p '#{start_time}' 2>/dev/null); then
    age=$(( $(date +%s) - start )); (( age < 60 )) && sleep $(( 60 - age ))
  fi
  # Wait up to 5 min for the network, then go on anyway.
  for i in {1..30}; do curl -s -o /dev/null -m 8 https://api.anthropic.com/ && break; sleep 10; done
fi

started=0; skipped=0
while IFS='|' read -r name id dir; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  [[ -z "$id" ]] && { log "WARN: no session id for '$name', skip"; continue; }
  dir="${dir:-$DEFAULT_DIR}"

  if pgrep -f -- "--resume $id" >/dev/null 2>&1; then
    skipped=$((skipped+1)); continue
  fi
  # Resume works only from the folder the transcript is stored under.
  if [[ ! -e "$HOME/.claude/projects/${dir//[\/.]/-}/$id.jsonl" ]]; then
    log "WARN: no transcript for $name ($id) in $dir, skip"; continue
  fi

  line="cd ${(q)dir} && $CMD --resume $id --remote-control $name"

  if ! "$TMUX_BIN" has-session -t "=$SESSION" 2>/dev/null; then
    run "$TMUX_BIN" new-session -d -s "$SESSION" -n "$name" -c "$dir"
  elif ! "$TMUX_BIN" list-windows -t "=$SESSION" -F '#{window_name}' | grep -qxF -- "$name"; then
    run "$TMUX_BIN" new-window -d -t "=$SESSION:" -n "$name" -c "$dir"
  else
    # The window exists (for example restored by a tmux plugin). Use it only if it is
    # an idle shell. pane_current_command is not reliable alone, so: a shell with no
    # child process is idle.
    cur=$("$TMUX_BIN" display -p -t "=$SESSION:=$name" '#{pane_current_command}' 2>/dev/null)
    ppid=$("$TMUX_BIN" display -p -t "=$SESSION:=$name" '#{pane_pid}' 2>/dev/null)
    case "$cur" in
      zsh|bash|sh|fish|-zsh|-bash) ;;
      *) log "SKIP $name: window busy with '$cur'"; continue ;;
    esac
    if [[ -z "$ppid" ]] || pgrep -P "$ppid" >/dev/null 2>&1; then
      log "SKIP $name: window shell is busy (pane pid ${ppid:-?})"; continue
    fi
  fi
  [[ -z "$DRY_RUN" ]] && sleep 1
  run "$TMUX_BIN" set-option -w -t "=$SESSION:=$name" automatic-rename off
  run "$TMUX_BIN" send-keys -t "=$SESSION:=$name" "$line" Enter
  log "started $name ($id)"
  started=$((started+1))
  # Space out the starts: each session starts its MCP servers and hooks.
  [[ -z "$DRY_RUN" ]] && sleep "${RC_GAP:-10}"
done < "$MANIFEST"

log "done: started=$started already-running=$skipped${DRY_RUN:+ (dry run)}"
