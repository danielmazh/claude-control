# Claude Control

A local, zero-dependency dashboard for [Claude Code](https://claude.com/claude-code).
It **layers over your existing `~/.claude`**. Nothing personal ships with the tool:
everything you see (sessions, profiles, skills, rules, settings, usage) is read live
from the machine it runs on.

## Pages

- **Home** - one health bar (network, VPN, Claude login, Remote Control daemon,
  watchdog, restore, sessions) with a "fix everything" button, and cards to every page.
- **Sessions and Restore** - every session in your auto-restore list with its state,
  phone (Remote Control) state and last activity. One-click actions: reconnect, start,
  open on phone, copy the tmux attach command, add, remove. System checks with an
  automatic fix where possible, and a yellow "needs you" banner with a command to copy
  when a step cannot be automated (for example `claude auth login`). A "recently
  fallen" panel lists sessions that stopped and are not in the restore list.
- **Session search** - find any session by title, first message, id, folder, or the
  full conversation text. Shows where it runs now (tmux window or Claude Desktop) and
  brings a stopped session back in one click (tmux + Remote Control + auto-restore).
  A search box in the top bar of every page jumps here.
- **Logs** - live tail of the watchdog, restore, Remote Control daemon and FortiClient
  logs, with source tabs, filter and pause.
- **New session** - generic presets and your own, each with an estimated start token
  cost, plus a custom builder (MCP profiles, skills, rule packs, folder, model). Opens
  in a tmux window of your choice or a new Terminal window. "Work from phone" reuses
  the running Remote Control daemon and shows its link and QR code, or a QR code with
  an `ssh ... tmux attach` command for a phone SSH app.
- **Manage** - account, update, diagnostics (`claude doctor`), model, plugins, sounds,
  `CLAUDE.md`, MCP profiles (add a server), hooks, rules, skills (create, activate,
  archive), new files, Claude Desktop config, and a one-click `cl-dash` alias install.
- **Monitor** - quota bars, a 7-day chart and a project x model breakdown, built only
  from Claude Code's own records.
- **Guide** - what each part does.

Hebrew and English UI (RTL and LTR).

## Requirements

- macOS, Python 3.9+ (stdlib only, no pip installs)
- [Claude Code](https://claude.com/claude-code) CLI signed in
- `tmux` (recommended): `brew install tmux`

## Install and run

```bash
./install.sh
```

This copies the files to `~/.claude/dashboard`, adds a launchd agent and a `cl-dash`
alias. The agent:

- starts the dashboard at login,
- restarts it after a crash,
- leaves it off after you press the in-app shutdown button,
- never runs a second copy (a second start exits when port 7799 is taken).

Open it with `cl-dash` or `http://127.0.0.1:7799`. Log: `~/Library/Logs/claude-control.log`.
Run `./install.sh` again to update; your state files are kept.

Without launchd: `python3 ~/.claude/dashboard/server.py`.

### Session restore (optional)

The Sessions and Restore page works with two small launchd scripts if you have them:
a watchdog that keeps `claude remote-control` alive in tmux session
`claude-RC-general` (window `rc-daemon`), and a resume script that restarts the
sessions listed in `~/.config/claude-rc/sessions.conf` (`name|session-id|folder`) in
tmux session `rc-resume`. Their launchd labels are found by suffix
(`*.claude-rc-watchdog`, `*.claude-rc-resume`). The daemon log tab and the daemon's
phone link read `~/Library/Logs/claude-rc-daemon.log`, which the watchdog fills with
`tmux pipe-pane`.

## Settings (environment)

| Variable | Default | Use |
|---|---|---|
| `CC_PORT` | `7799` | Port. Use another one for a second, development copy. |
| `CC_DATA` | the script folder | Where state files live (presets, config, caches). |
| `CC_NO_BROWSER` | unset | `1` = do not open a browser on start. |
| `CC_LABEL` | `local.claude-control` | launchd label used by `install.sh` for a new agent. |
| `CC_LABEL_CLAUDE_RC_WATCHDOG`, `CC_LABEL_CLAUDE_RC_RESUME` | found by suffix | launchd labels of the restore scripts, if the suffix search does not fit. |
| `CC_RC_CONF` | `~/.config/claude-rc/sessions.conf` | Restore list. |
| `CC_RC_SESSION` | `rc-resume` | tmux session of the restored sessions. |
| `CC_RC_CMD` | `ANTHROPIC_BASE_URL= claude` | Command used to resume a session (for tests). |

Development copy next to the live one:

```bash
CC_PORT=7800 CC_DATA=$PWD/.devdata CC_NO_BROWSER=1 python3 server.py
```

## What it writes

Only files under your own `~/.claude`: `settings.json` (model, plugins), `CLAUDE.md`
(if you edit it), `rules/*.md`, skill moves between `skills/` and `skills-archive/`,
its own state in `~/.claude/dashboard/` (`presets.json`, `config.json`, caches), and
the restore list `~/.config/claude-rc/sessions.conf` (lines are commented out, never
deleted). `install.sh` also writes the launchd plist and one alias line in `~/.zshrc`.

## API

Read (GET): `/api/state`, `/api/monitor` (`?force` rebuilds the cache), `/api/doctor`,
`/api/rc/status`, `/api/logs?src=all|watchdog|resume|daemon|forti&n=&q=`,
`/api/sessions/search?q=&days=&limit=&content=1`, `/api/qr?text=`.

Restore and search actions (POST, with `"dry": true` for a plan only):
`/api/rc/action` (`fix_all`, `restart_session`, `resume_missing`, `restart_daemon`,
`keep`, `drop`), `/api/sessions/resume`, and `/api/launch` (dry shows the command).

Other POST actions used by the pages: `/api/model`, `/api/plugin`, `/api/sounds`,
`/api/skill`, `/api/skill/create`, `/api/preset`, `/api/preset/delete`, `/api/rule`,
`/api/rule/get`, `/api/rule/delete`, `/api/claude-md/get`, `/api/claude-md/save`,
`/api/config`, `/api/login`, `/api/install-alias`, `/api/update`,
`/api/remote-control`, `/api/mcp-server/add`, `/api/file/create`, `/api/open`,
`/api/shutdown`.

## Sharing this tool

Ship the repo files only. The runtime files (`presets.json`, `config.json`,
`.monitor-cache.json`, `.sounds-off`, `.session-rules.md`, `.devdata/`) are personal
state and are ignored by git.

## Chrome extension (quick actions)

A minimal MV3 extension lives in `extension/`: quotas at a glance, one-click session
launch into your chosen tmux target, sounds toggle, and a button that opens the full
dashboard. It talks only to `127.0.0.1:7799`.

Install: `chrome://extensions` -> enable **Developer mode** -> **Load unpacked** ->
select the `extension/` folder.
