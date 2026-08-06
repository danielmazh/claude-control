# Claude Control

A local, zero-dependency dashboard for [Claude Code](https://claude.com/claude-code).
It **layers over your existing `~/.claude`** — nothing personal ships with the tool;
everything you see (profiles, skills, rules, settings, usage) is read live from the
machine it runs on.

## Features

- **Session launcher** — generic presets + your own (created in the UI), each with an
  estimated session-start token cost. Sessions open as auto-numbered windows inside a
  tmux session of your choice (reachable from any terminal via `tmux attach`), or in a
  new Terminal window.
- **Custom session builder** — pick MCP profiles, skills/bare tool set, rule packs,
  working directory (auto-listed from your code folder) and model; the exact `claude`
  command is built live.
- **Rules** — short instruction files in `~/.claude/rules/`, injected into a session
  via `--append-system-prompt`.
- **Management** — default model, plugins, notification sounds, `CLAUDE.md` editing,
  MCP profile and hooks overview, skill activate/archive (applies live), account
  login (`claude /login` via browser), and diagnostics using only Claude's own
  built-in tools (`claude doctor`, `claude --version`).
- **Monitor** — quota bars, a 7-day chart (output / input / messages / cache) and a
  project × model breakdown, built exclusively from Claude Code's own records (the
  `/usage` payload and transcript usage fields). Diagnostics run only Claude's
  built-in CLI commands (`doctor`, `--version`, `config/mcp/plugin list`).
- **Bilingual** — full Hebrew / English UI (flag toggle, RTL/LTR aware, static header).
- **One-click alias install** — adds `cl-dash` to your `~/.zshrc` from the UI.

## Requirements

- macOS, Python 3.9+ (stdlib only — no pip installs)
- [Claude Code](https://claude.com/claude-code) CLI signed in
- `tmux` (recommended): `brew install tmux`

## Install & run

```bash
mkdir -p ~/.claude/dashboard
cp server.py index.html README.md ~/.claude/dashboard/
python3 ~/.claude/dashboard/server.py
```

Suggested alias:

```bash
alias cl-dash='python3 ~/.claude/dashboard/server.py'
```

Running it again while already up just reopens the browser. The ⏻ button shuts the
server down. Serves on `127.0.0.1:7799` only.

Install as a Mac web app: Chrome → ⋮ → *Install page as app*, or Safari → *File →
Add to Dock*.

## What it writes

Only files under your own `~/.claude`: `settings.json` (model/plugins), `CLAUDE.md`
(if you edit it), `rules/*.md`, skill moves between `skills/` and `skills-archive/`,
and its own state in `~/.claude/dashboard/` (`presets.json`, `config.json`, caches).

## Sharing / publishing this tool

Ship only `server.py`, `index.html`, `README.md`. The runtime files
(`presets.json`, `config.json`, `.monitor-cache.json`, `.sounds-off`,
`.session-rules.md`) are the user's personal state — do not include them.

## Chrome extension (quick actions)

A minimal MV3 extension lives in `extension/`: quotas at a glance, one-click
session launch into your chosen tmux target, sounds toggle, and an expand
button that opens the full dashboard in a new tab. It talks only to
`127.0.0.1:7799` (the local server must be running).

Install: `chrome://extensions` → enable **Developer mode** → **Load unpacked**
→ select the `extension/` folder.
