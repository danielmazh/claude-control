# Claude Control

A local, zero-dependency dashboard for [Claude Code](https://claude.com/claude-code).
It **layers over your existing `~/.claude`**. Nothing personal ships with the tool:
everything you see (sessions, profiles, skills, rules, settings, usage) is read live
from the machine it runs on.

## Pages

- **Home** - one health bar (network, VPN, Claude login, Remote Control daemon,
  watchdog, restore, sessions) with a "fix everything" button, and cards to every page.
  Checks for parts this machine does not have are left out (see "Optional parts").
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

Hebrew and English UI (RTL and LTR). Server messages, check titles and issue texts
follow the UI language.

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

```bash
./install.sh --with-restore          # dashboard + restore scripts + their agents
./install.sh --with-restore --force  # also replace restore scripts you changed
```

This adds two small scripts from `scripts/`:

- `claude-rc-watchdog.sh` keeps one `claude remote-control` (the phone link) alive in
  tmux session `claude-RC-general`, window `rc-daemon`. launchd runs it every 60s. It
  never starts a second copy, does not restart while `api.anthropic.com` is
  unreachable, keeps the last screen of a stopped daemon in its log (remain-on-exit),
  and copies the daemon output to `~/Library/Logs/claude-rc-daemon.log`
  (`tmux pipe-pane`), where the dashboard reads the phone link.
- `claude-rc-resume.sh` runs once at login. It brings back the sessions listed in
  `~/.config/claude-rc/sessions.conf` (`name|session-id|folder`, empty folder = your
  home folder) in tmux session `rc-resume`, each with Remote Control on. It runs one
  copy at a time, skips sessions that already run, never touches a busy window, skips
  a session with no transcript, waits between starts, and supports `DRY_RUN=1`.

What `--with-restore` does, and prints:

- copies the scripts to `~/.local/bin` (or `$CC_BIN_DIR`). A script you already have
  and changed is kept, unless you pass `--force` (the old copy is saved as `.bak`);
- creates `~/.config/claude-rc/sessions.conf` with a commented header, if missing;
- writes two launchd agents, `<prefix>.claude-rc-watchdog` (every 60s) and
  `<prefix>.claude-rc-resume` (at load), prefix `$CC_LABEL_PREFIX` or `local`. If an
  agent with the same suffix already exists, it is reused and not rewritten.

Without `--with-restore` the install is the same as before. The dashboard finds the
agents by suffix (`*.claude-rc-watchdog`, `*.claude-rc-resume`) and the scripts from the
agent's `ProgramArguments`, so existing setups with other labels keep working.

Script settings (environment):

| Variable | Default | Script |
|---|---|---|
| `CC_RC_DAEMON_TARGET` | `claude-RC-general:rc-daemon` | watchdog (tmux `session:window`) |
| `RC_DAEMON_SESSION`, `RC_DAEMON_WINDOW` | from the target | watchdog |
| `RC_DAEMON_DIR` | `$HOME` | watchdog (working folder) |
| `RC_MANIFEST` | `~/.config/claude-rc/sessions.conf` | resume |
| `RC_SESSION` | `rc-resume` | resume (tmux session) |
| `RC_DIR` | `$HOME` | resume (folder for lines without one) |
| `RC_CMD` | `ANTHROPIC_BASE_URL= <claude>` | resume |
| `RC_WAIT` | `1` | resume (`0` = do not wait for tmux and the network) |
| `RC_GAP` | `10` | resume (seconds between starts) |
| `RC_LOG_DIR` | `~/Library/Logs` | both |
| `CLAUDE_BIN` | newest nvm, then Homebrew, then `PATH` | both |
| `TMUX_BIN` | `tmux` from `PATH` | both |
| `DRY_RUN` | unset | both (`1` = print the plan, change nothing, write no log) |

Try a script safely: `DRY_RUN=1 RC_WAIT=0 zsh ~/.local/bin/claude-rc-resume.sh`.

### Optional parts

The dashboard detects what this machine has and shows the result as `features` in
`/api/state` and `/api/rc/status`:

| Feature | True when | Without it |
|---|---|---|
| `restore` | `sessions.conf` exists or a `*.claude-rc-resume` agent exists | Sessions and Restore shows a setup panel with `./install.sh --with-restore`; the health bar shows only network and login checks and one info line. Search offers the resume command to copy. |
| `watchdog` | the watchdog script or a `*.claude-rc-watchdog` agent exists | - |
| `forticlient` | the FortiClient app or its `Logs` folder exists | no VPN check, no FortiClient log tab |
| `mcp_registry` | `~/.claude/registry/mcp` exists | the MCP profile parts of New session and Manage explain what profiles are |
| `rate_limits` | `~/.claude/statusline-cache/rate-limits.json` exists (or Claude Code's own cached quota) | Monitor hides the quota bars and shows a one-line note |

FortiClient, the MCP registry and the quota bars are optional. `CC_FEATURE_<NAME>=0|1`
(for example `CC_FEATURE_FORTICLIENT=0`) overrides one detection, for tests.

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
| `CC_RC_DAEMON_TARGET` | `claude-RC-general:rc-daemon` | tmux `session:window` of the Remote Control daemon. |
| `CC_BIN_DIR` | `~/.local/bin` | Where `install.sh --with-restore` puts the restore scripts, and where the dashboard looks for them when no agent names them. |
| `CC_LABEL_PREFIX` | `local` | Label prefix of new restore agents. |
| `CC_NO_LAUNCHCTL` | unset | `1` = `install.sh` writes files but does not load agents (for tests). |
| `CC_FEATURE_<NAME>` | detected | `0` or `1` overrides one feature (see "Optional parts"). |

Folder defaults: a restore line without a folder, and a session added without one,
use `code_dir` from `config.json` (Manage, Dashboard configuration), else your home
folder.

Development copy next to the live one:

```bash
CC_PORT=7800 CC_DATA=$PWD/.devdata CC_NO_BROWSER=1 python3 server.py
```

## What it writes

Only files under your own `~/.claude`: `settings.json` (model, plugins), `CLAUDE.md`
(if you edit it), `rules/*.md`, skill moves between `skills/` and `skills-archive/`,
its own state in `~/.claude/dashboard/` (`presets.json`, `config.json`, caches), and
the restore list `~/.config/claude-rc/sessions.conf` (lines are commented out, never
deleted). `install.sh` also writes the launchd plist and one alias line in `~/.zshrc`;
with `--with-restore` also the two scripts, `sessions.conf` and two more plists.

## API

All calls take the UI language: `?lang=en` (GET) or `"lang": "en"` in the JSON body
(POST). The default is Hebrew.

Read (GET): `/api/state`, `/api/monitor` (`?force` rebuilds the cache), `/api/doctor`,
`/api/rc/status`, `/api/logs?src=all|watchdog|resume|daemon|forti&n=&q=` (`forti` is empty
without FortiClient),
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
