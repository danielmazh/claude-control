#!/usr/bin/env python3
"""Claude Control — a local dashboard that layers over an existing ~/.claude.

Generic by design: no personal data ships with the tool. Everything it shows is
read live from the machine it runs on:

  ~/.claude/settings.json            plugins / model / hooks   (next session)
  ~/.claude/CLAUDE.md                global instructions       (editable here)
  ~/.claude/skills[-archive]/        skill activation          (live)
  ~/.claude/registry/mcp/*.json      MCP launch profiles       (at launch)
  ~/.claude/rules/*.md[c]            rule packs → --append-system-prompt
  ~/.claude/dashboard/presets.json   user presets (starts empty — you build them)
  ~/.claude/dashboard/config.json    dashboard config (e.g. your code directory)

Sessions open inside tmux (chosen session, auto-numbered window) or Terminal.
Monitor uses ONLY Claude Code's own records. Diagnostics run ONLY Claude's
built-in tools (claude doctor / --version). Re-running while up reopens the
browser; /api/shutdown stops it.

Run: python3 server.py   (suggested alias: cl-dash)
"""
import json, os, re, shutil, socket, subprocess, threading, time, webbrowser
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOME = Path.home()
CL = HOME / ".claude"
DASH = Path(__file__).resolve().parent
SETTINGS = CL / "settings.json"
CLAUDE_MD = CL / "CLAUDE.md"
SKILLS = CL / "skills"
ARCHIVE = CL / "skills-archive"
MCP_DIR = CL / "registry" / "mcp"
RULES_DIR = CL / "rules"
CLAUDE_JSON = HOME / ".claude.json"
INSTALLED = CL / "plugins" / "installed_plugins.json"
RATE_LIMITS = CL / "statusline-cache" / "rate-limits.json"
SKILLS_TOK = CL / "statusline-cache" / "skills.tok"
USER_PRESETS = DASH / "presets.json"
DASH_CONFIG = DASH / "config.json"
MONITOR_CACHE = DASH / ".monitor-cache.json"
SOUNDS_OFF_FLAG = DASH / ".sounds-off"
SESSION_RULES = DASH / ".session-rules.md"
DESKTOP_CFG = HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
PROJECTS = CL / "projects"
PORT = 7799

CLT_FULL = ("Bash,Read,Edit,Write,TaskCreate,TaskUpdate,TaskStop,TaskOutput,"
            "WebFetch,WebSearch,Agent,AskUserQuestion,Skill")
CLT_NOSKILL = CLT_FULL.replace(",Skill", "")
CLT_BARE = "Bash,Read,Write,Edit,Grep,Glob"
# mirrors the /model picker; actual availability depends on the signed-in account
MODELS = ("opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku", "fable")

BUILTIN_PRESETS = [
    {"id": "bare",  "kind": "generic", "name": "נקי לגמרי",
     "desc": "בלי סקילים, בלי MCP, בלי sub-agents — מינימום מוחלט",
     "profiles": [], "skills": False, "bare": True},
    {"id": "cl",    "kind": "generic", "name": "קלאסי + סקילים",
     "desc": "רזה עם כל הסקילים — ברירת מחדל יומיומית",
     "profiles": [], "skills": True, "bare": False},
    {"id": "mcp",   "kind": "generic", "name": "קלאסי + MCP מקומי",
     "desc": "סקילים + כל פרופילי ה-MCP המקומיים",
     "profiles": ["all-local.json"], "skills": True, "bare": False},
    {"id": "full",  "kind": "generic", "name": "הכול",
     "desc": "פלאגינים + ‎.mcp.json של הפרויקט — כמו claude רגיל",
     "profiles": None, "skills": True, "bare": False},
]

# rough per-server preload estimates (K tokens) for the session-start badge;
# unknown servers fall back to 5k. Deferred tool loading may lower real cost.
SERVER_TOK = {"jira": 20, "supabase": 10, "playwright": 4, "cloudflare": 8,
              "cloudflare-bindings": 9, "drawio": 2, "resend": 5}

SAFE_NAME = re.compile(r"[A-Za-z0-9._֐-׿ -]{1,40}")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/~ -]+")
SAFE_FILE = re.compile(r"[A-Za-z0-9._-]+\.(md|mdc)")


def jread(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {} if default is None else default


def jwrite(p, data):
    Path(p).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def dash_config():
    return {"code_dir": "", **jread(DASH_CONFIG, {})}


def repos():
    """Sub-directories of the configured code directory — used as cwd choices."""
    cd = os.path.expanduser(dash_config().get("code_dir") or "")
    if not cd or not os.path.isdir(cd):
        return []
    out = []
    try:
        for e in sorted(os.scandir(cd), key=lambda x: x.name.lower()):
            if e.is_dir() and not e.name.startswith("."):
                out.append(os.path.join(dash_config()["code_dir"], e.name))
    except Exception:
        pass
    return out[:60]


# ── presets (user presets start EMPTY — the user builds their own) ───────────
def user_presets():
    return jread(USER_PRESETS, [])


def all_presets():
    out = [{**p, "custom": False, "cmd": build_cmd(p), "est": estimate(p)}
           for p in BUILTIN_PRESETS]
    out += [{**p, "kind": "custom", "custom": True, "cmd": build_cmd(p), "est": estimate(p)}
            for p in user_presets()]
    return out


def save_user_preset(p):
    name = (p.get("name") or "").strip()
    if not SAFE_NAME.fullmatch(name):
        raise ValueError("שם לא תקין")
    cwd = (p.get("cwd") or "").strip()
    if cwd and not SAFE_PATH.fullmatch(cwd):
        raise ValueError("נתיב לא תקין")
    entry = {"id": p.get("id") or f"u{int(time.time())}", "name": name,
             "desc": (p.get("desc") or "").strip()[:80],
             "profiles": [x for x in p.get("profiles", [])
                          if re.fullmatch(r"[A-Za-z0-9._-]+\.json", x) and (MCP_DIR / x).exists()],
             "skills": bool(p.get("skills", True)), "bare": bool(p.get("bare", False)),
             "cwd": cwd, "model": p.get("model") if p.get("model") in MODELS else "",
             "rules": [r for r in p.get("rules", []) if SAFE_FILE.fullmatch(r) and (RULES_DIR / r).exists()]}
    ps = [x for x in user_presets() if x.get("id") != entry["id"]]
    ps.append(entry)
    jwrite(USER_PRESETS, ps)
    return entry["id"]


def delete_user_preset(pid):
    jwrite(USER_PRESETS, [x for x in user_presets() if x.get("id") != pid])


def build_cmd(p):
    if p.get("profiles") is None:
        cmd = "claude"
    else:
        tools = CLT_BARE if p.get("bare") else (CLT_FULL if p.get("skills", True) else CLT_NOSKILL)
        cfgs = " ".join(f"~/.claude/registry/mcp/{x}" for x in (p.get("profiles") or ["none.json"]))
        cmd = f'claude --strict-mcp-config --mcp-config {cfgs} --tools "{tools}"'
        if p.get("bare"):
            cmd += ' --agents "{}"'
    if p.get("model"):
        cmd += f' --model "{p["model"]}"'
    if p.get("rules"):
        cmd += f' --append-system-prompt "$(cat {SESSION_RULES})"'
    return cmd


def estimate(p):
    """Rough session-start context (K tokens), anchored to /context measurements."""
    if p.get("profiles") is None:
        return 46.5
    t = 3.5 + 1.0
    t += 4.5 if p.get("bare") else 5.0
    if p.get("skills", True) and not p.get("bare"):
        try:
            t += int(SKILLS_TOK.read_text().strip()) / 1000
        except Exception:
            t += 2.5
    for prof in (p.get("profiles") or []):
        for server in jread(MCP_DIR / prof).get("mcpServers", {}):
            t += SERVER_TOK.get(server, 5)
    for r in (p.get("rules") or []):
        try:
            t += (RULES_DIR / r).stat().st_size / 4000
        except Exception:
            pass
    return round(t, 1)


# ── rules ────────────────────────────────────────────────────────────────────
def rules_list():
    out = []
    if RULES_DIR.is_dir():
        for f in sorted(RULES_DIR.iterdir()):
            if f.is_file() and f.suffix in (".md", ".mdc"):
                head = f.read_text(errors="ignore").strip().splitlines()
                title = next((l.lstrip("# ").strip() for l in head if l.strip()), f.name)
                out.append({"file": f.name, "title": title[:60],
                            "tokens": round(f.stat().st_size / 4 / 1000, 1)})
    return out


def rule_save(name, content, old=None):
    slug = re.sub(r"[^A-Za-z0-9א-ת_-]+", "-", name.strip()).strip("-")[:40]
    if not slug:
        raise ValueError("שם לא תקין")
    RULES_DIR.mkdir(exist_ok=True)
    fname = old if (old and SAFE_FILE.fullmatch(old)) else f"{slug}.md"
    (RULES_DIR / fname).write_text(content)
    return fname


def rule_delete(fname):
    if not SAFE_FILE.fullmatch(fname):
        raise ValueError("bad file")
    (RULES_DIR / fname).unlink(missing_ok=True)


def prepare_rules(rule_files):
    parts = [(RULES_DIR / r).read_text(errors="ignore").strip()
             for r in rule_files if SAFE_FILE.fullmatch(r) and (RULES_DIR / r).exists()]
    SESSION_RULES.write_text("\n\n---\n\n".join(parts) + "\n")


# ── tmux / launching ─────────────────────────────────────────────────────────
def tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)
    return r.returncode, r.stdout.strip()


def tmux_sessions():
    try:
        rc, out = tmux("list-sessions", "-F", "#{session_name}|#{session_windows}|#{session_attached}")
        if rc != 0:
            return []
        return [{"name": n, "windows": int(w), "attached": a == "1"}
                for n, w, a in (l.split("|") for l in out.splitlines() if l)]
    except Exception:
        return []


def next_window_name(session, base):
    _, out = tmux("list-windows", "-t", session, "-F", "#{window_name}")
    nums = [int(m.group(1)) for w in out.splitlines()
            if (m := re.fullmatch(re.escape(base) + r"-(\d+)", w))]
    return f"{base}-{(max(nums) + 1) if nums else 1:02d}"


def run_in_terminal(command):
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{esc}"',
                    "-e", 'tell application "Terminal" to activate'],
                   capture_output=True, timeout=10)


def launch(preset, target, dry=False):
    rules = preset.get("rules") or []
    if rules and not dry:
        prepare_rules(rules)
    cmd = build_cmd(preset)
    cwd = os.path.expanduser(preset.get("cwd") or "")
    if cwd and not os.path.isdir(cwd):
        raise ValueError(f"תיקייה לא קיימת: {cwd}")
    base = re.sub(r"[^A-Za-z0-9-]", "", preset.get("id", "cl")) or "cl"
    if target.get("type") == "tmux":
        if shutil.which("tmux") is None:
            raise ValueError("tmux לא מותקן — ראה טאב הוראות")
        ses = target.get("session", "")
        if ses == "__new__":
            ses = "claude"
            if not any(s["name"] == ses for s in tmux_sessions()) and not dry:
                tmux("new-session", "-d", "-s", ses, "-c", cwd or str(HOME))
        elif ses not in [s["name"] for s in tmux_sessions()]:
            raise ValueError("סשן tmux לא קיים")
        win = next_window_name(ses, base) if not dry else f"{base}-XX"
        if not dry:
            tmux("new-window", "-t", f"{ses}:", "-n", win, "-c", cwd or str(HOME))
            tmux("send-keys", "-t", f"{ses}:{win}", cmd, "C-m")
        return {"cmd": cmd, "tmux": f"{ses}:{win}"}
    full = f"cd {cwd} && {cmd}" if cwd else cmd
    if not dry:
        run_in_terminal(full)
    return {"cmd": full, "terminal": True}


# ── state ────────────────────────────────────────────────────────────────────
def skill_entries(root):
    out = []
    if not root.is_dir():
        return out
    for e in sorted(root.iterdir()):
        if e.is_dir() and (e / "SKILL.md").exists():
            out.append(e.name)
        elif e.is_file() and e.suffix == ".md" and e.name != "INDEX.md":
            if e.read_text(errors="ignore")[:200].lstrip().startswith("---"):
                out.append(e.name)
    return out


def mcp_profiles():
    return [{"file": f.name, "servers": list(jread(f).get("mcpServers", {}).keys())}
            for f in sorted(MCP_DIR.glob("*.json"))] if MCP_DIR.is_dir() else []


def hooks_summary():
    hooks = jread(SETTINGS).get("hooks") or {}
    out = []
    for ev, matchers in hooks.items():
        scripts = []
        for m in matchers:
            for h in m.get("hooks", []):
                c = h.get("command", "")
                found = re.findall(r"([A-Za-z0-9_.-]+\.(?:py|sh|js))", c)
                scripts += found or ([c.split()[0].split("/")[-1]] if c.strip() else [])
        out.append({"event": ev,
                    "count": sum(len(m.get("hooks", [])) for m in matchers),
                    "scripts": sorted(set(s for s in scripts if s))[:6]})
    return out


def local_ip():
    for iface in ("en0", "en1", "en2"):
        try:
            r = subprocess.run(["ipconfig", "getifaddr", iface],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return ""


_VER = {"v": None}


def claude_version():
    if _VER["v"] is None:
        try:
            r = subprocess.run(["claude", "--version"], capture_output=True,
                               text=True, timeout=15)
            _VER["v"] = (r.stdout or "").strip().split()[0] or "?"
        except Exception:
            _VER["v"] = "?"
    return _VER["v"]


def claude_update():
    """Upgrade via Claude's own built-in updater; on the known npm-global
    failure (stale .claude-code-* temp dir blocks the rename), clean it and
    retry with the npm that lives next to the claude binary itself."""
    env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}

    def run(args, timeout=300):
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
        txt = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
        return r.returncode, txt.strip()

    rc, out = run(["claude", "update"])
    log = [f"$ claude update\n{out or '(no output)'}"]

    if rc != 0 or "Failed to install" in out:
        claude_bin = shutil.which("claude") or ""
        real = os.path.realpath(claude_bin)
        m = re.search(r"(.+/lib/node_modules)/@anthropic-ai/claude-code/", real)
        if m:
            scope = Path(m.group(1)) / "@anthropic-ai"
            for d in scope.glob(".claude-code-*"):
                shutil.rmtree(d, ignore_errors=True)
                log.append(f"(removed stale npm leftover: {d.name})")
            npm = Path(claude_bin).parent / "npm"
            npm_cmd = str(npm) if npm.exists() else "npm"
            rc2, out2 = run([npm_cmd, "install", "-g", "@anthropic-ai/claude-code@latest"])
            log.append(f"$ {npm_cmd} install -g @anthropic-ai/claude-code@latest\n{out2}")

    _VER["v"] = None                                  # re-read version next time
    return "\n\n".join(log)[:6000]


def remote_control(target):
    """Open Claude's official phone bridge (`claude remote-control`) in tmux/Terminal.
    The QR it prints is scanned with the Claude mobile app."""
    cmd = "claude remote-control"
    if target.get("type") == "tmux":
        if shutil.which("tmux") is None:
            raise ValueError("tmux לא מותקן — ראה טאב הוראות")
        ses = target.get("session", "")
        if ses == "__new__":
            ses = "claude"
            if not any(s["name"] == ses for s in tmux_sessions()):
                tmux("new-session", "-d", "-s", ses)
        elif ses not in [s["name"] for s in tmux_sessions()]:
            raise ValueError("סשן tmux לא קיים")
        win = next_window_name(ses, "rc")
        tmux("new-window", "-t", f"{ses}:", "-n", win)
        tmux("send-keys", "-t", f"{ses}:{win}", cmd, "C-m")
        return {"tmux": f"{ses}:{win}"}
    run_in_terminal(cmd)
    return {"terminal": True}


def qr_svg(text):
    if shutil.which("qrencode") is None:
        raise ValueError("qrencode not installed — brew install qrencode")
    r = subprocess.run(["qrencode", "-t", "SVG", "-o", "-", text[:200]],
                       capture_output=True, timeout=10)
    return r.stdout


def add_mcp_profile(req):
    name = (req.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", name):
        raise ValueError("invalid name (a-z, 0-9, -, _)")
    fn = MCP_DIR / f"{name}.json"
    if fn.exists():
        raise ValueError("profile already exists")
    if req.get("kind") == "http":
        url = (req.get("url") or "").strip()
        if not re.fullmatch(r"https://[A-Za-z0-9./_?&=%~-]+", url):
            raise ValueError("invalid https URL")
        server = {"type": "http", "url": url}
    else:
        parts = (req.get("command") or "").strip().split()
        if not parts:
            raise ValueError("empty command")
        server = {"command": parts[0], "args": parts[1:]}
    MCP_DIR.mkdir(parents=True, exist_ok=True)
    jwrite(fn, {"mcpServers": {name: server}})
    return fn.name


def create_skill(req):
    slug = re.sub(r"[^a-z0-9-]+", "-", (req.get("name") or "").lower()).strip("-")[:40]
    if not slug:
        raise ValueError("invalid name")
    d = SKILLS / slug
    if d.exists():
        raise ValueError("skill already exists")
    desc = (req.get("description") or "").strip().replace("\n", " ")[:300]
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: {desc}\n---\n\n{req.get('content', '')}\n")
    return slug


def create_file(req):
    rel = (req.get("path") or "").strip().lstrip("/")
    if ".." in rel or not re.fullmatch(r"[A-Za-z0-9._/ -]{1,120}", rel):
        raise ValueError("invalid path")
    p = (CL / rel).resolve()
    if not str(p).startswith(str(CL.resolve()) + os.sep):
        raise ValueError("path must be under ~/.claude")
    if p.exists():
        raise ValueError("file already exists")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.get("content", ""))
    return rel


def account_info():
    a = jread(CLAUDE_JSON).get("oauthAccount") or {}
    return {"email": a.get("emailAddress", ""),
            "org": (a.get("organizationName") or "")[:40]}


def desktop_info():
    cfg = jread(DESKTOP_CFG)
    return {"exists": DESKTOP_CFG.exists(),
            "mcp_servers": [{"name": k} for k in (cfg.get("mcpServers") or {})]}


def usage_limits():
    limits = jread(RATE_LIMITS, {})
    if not limits:
        limits = (jread(CLAUDE_JSON).get("cachedUsageUtilization") or {}).get("utilization") or {}
    return limits


def state():
    s = jread(SETTINGS)
    installed = jread(INSTALLED).get("plugins") or {}
    enabled = s.get("enabledPlugins", {})
    return {
        "model": s.get("model", ""),
        "models": list(MODELS),
        "plugins": [{"name": n, "enabled": bool(enabled.get(n, False))}
                    for n in sorted(set(installed) | set(enabled))],
        "sounds": not SOUNDS_OFF_FLAG.exists(),
        "skills": {"active": skill_entries(SKILLS), "archive": skill_entries(ARCHIVE)},
        "mcp_profiles": mcp_profiles(),
        "hooks": hooks_summary(),
        "account": account_info(),
        "presets": all_presets(),
        "rules": rules_list(),
        "tmux": tmux_sessions(),
        "tmux_available": shutil.which("tmux") is not None,
        "desktop": desktop_info(),
        "config": dash_config(),
        "alias_installed": alias_installed(),
        "remote": {"user": os.environ.get("USER", ""), "ip": local_ip(),
                   "qr": shutil.which("qrencode") is not None},
        "claude_version": claude_version(),
        "repos": repos(),
        "claude_md": {"exists": CLAUDE_MD.exists(),
                      "tokens": round(CLAUDE_MD.stat().st_size / 4 / 1000, 1) if CLAUDE_MD.exists() else 0},
        "limits": usage_limits(),
    }


def toggle_plugin(name, on):
    s = jread(SETTINGS)
    s.setdefault("enabledPlugins", {})[name] = bool(on)
    jwrite(SETTINGS, s)


def set_model(model):
    if model not in MODELS:
        raise ValueError("unknown model")
    s = jread(SETTINGS)
    s["model"] = model
    jwrite(SETTINGS, s)


def move_skill(name, action):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("bad name")
    src, dst = (ARCHIVE, SKILLS) if action == "activate" else (SKILLS, ARCHIVE)
    if not (src / name).exists():
        raise ValueError("not found")
    if (dst / name).exists():
        raise ValueError("already exists at destination")
    ARCHIVE.mkdir(exist_ok=True)
    shutil.move(str(src / name), str(dst / name))


def open_target(what):
    targets = {
        "connectors": lambda: webbrowser.open("https://claude.ai/settings/connectors"),
        "settings": lambda: subprocess.run(["open", "-t", str(SETTINGS)], timeout=5),
        "desktop-config": lambda: subprocess.run(["open", "-t", str(DESKTOP_CFG)], timeout=5),
        "plugins-dir": lambda: subprocess.run(["open", str(CL / "plugins")], timeout=5),
        "rules-dir": lambda: subprocess.run(["open", str(RULES_DIR)], timeout=5),
        "mcp-dir": lambda: subprocess.run(["open", str(MCP_DIR)], timeout=5),
        "claude-dir": lambda: subprocess.run(["open", str(CL)], timeout=5),
    }
    if what not in targets:
        raise ValueError("unknown target")
    targets[what]()


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(\x07|\x1b\\)|\r")


def doctor():
    """Diagnostics via Claude's OWN built-in CLI commands only — no extra logic."""
    out = []
    cmds = (("claude --version", ["claude", "--version"]),
            ("claude doctor", ["claude", "doctor"]),
            ("claude config list", ["claude", "config", "list"]),
            ("claude mcp list", ["claude", "mcp", "list"]),
            ("claude plugin list", ["claude", "plugin", "list"]))
    for label, args in cmds:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=25,
                               stdin=subprocess.DEVNULL,
                               env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"})
            txt = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
            txt = ANSI_RE.sub("", txt)
            lines = [l.rstrip() for l in txt.splitlines()]
            txt = "\n".join(l for i, l in enumerate(lines)
                            if l.strip() or (i > 0 and lines[i - 1].strip()))
            out.append({"cmd": label, "output": txt.strip()[:6000] or "(no output)"})
        except FileNotFoundError:
            out.append({"cmd": label, "output": "claude CLI not found in PATH"})
        except subprocess.TimeoutExpired:
            out.append({"cmd": label, "output": "(timed out — command may be interactive in this version)"})
    return out


ALIAS_LINE = "alias cl-dash='python3 ~/.claude/dashboard/server.py'"


def alias_installed():
    z = HOME / ".zshrc"
    return z.exists() and "cl-dash=" in z.read_text(errors="ignore")


def install_alias():
    if alias_installed():
        return
    with open(HOME / ".zshrc", "a") as f:
        f.write(f"\n# [claude-control] dashboard launcher\n{ALIAS_LINE}\n")


# ── monitor: ONLY Claude Code's own records ──────────────────────────────────
def monitor():
    cached = jread(MONITOR_CACHE)
    if cached and cached.get("v") == 2 and time.time() - cached.get("_ts", 0) < 600:
        cached["limits"] = usage_limits()
        return cached

    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    per_day = {d: {"output": 0, "input": 0, "cache": 0, "messages": 0} for d in days}
    per_model, per_project, per_pm = {}, {}, {}
    cutoff_mtime = time.time() - 8 * 86400
    home_slug = re.sub(r"[^A-Za-z0-9]", "-", str(HOME))

    def local_date(ts):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone() \
                           .strftime("%Y-%m-%d")
        except Exception:
            return ts[:10]

    def proj_name(dirname):
        n = dirname[len(home_slug):] if dirname.startswith(home_slug) else dirname
        return n.strip("-") or "~"

    if PROJECTS.is_dir():
        for f in PROJECTS.glob("**/*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff_mtime:
                    continue
                proj = proj_name(f.parent.name)
                with open(f, errors="ignore") as fh:
                    for line in fh:
                        if '"usage"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        raw_ts = d.get("timestamp", "")
                        if not raw_ts:
                            continue
                        ts = local_date(raw_ts)
                        if ts not in per_day:
                            continue
                        msg = d.get("message") or {}
                        u = msg.get("usage") or {}
                        if not u:
                            continue
                        out = u.get("output_tokens", 0) or 0
                        per_day[ts]["output"] += out
                        per_day[ts]["input"] += u.get("input_tokens", 0) or 0
                        per_day[ts]["cache"] += (u.get("cache_read_input_tokens", 0) or 0) + \
                                                (u.get("cache_creation_input_tokens", 0) or 0)
                        if out:
                            per_day[ts]["messages"] += 1
                            model = (msg.get("model") or "?").replace("claude-", "")
                            per_model[model] = per_model.get(model, 0) + 1
                            per_project[proj] = per_project.get(proj, 0) + out
                            per_pm.setdefault(proj, {})[model] = \
                                per_pm.get(proj, {}).get(model, 0) + out
            except Exception:
                continue

    week = {k: sum(d[k] for d in per_day.values()) for k in ("output", "input", "cache", "messages")}
    top_projects = [p for p, _ in sorted(per_project.items(), key=lambda x: -x[1])[:6]]
    top_models = [m for m, _ in sorted(per_model.items(), key=lambda x: -x[1])[:4]]
    result = {"_ts": time.time(), "v": 2,
              "days": [{"date": d, **per_day[d]} for d in days],
              "week": week,
              "models": sorted(per_model.items(), key=lambda x: -x[1])[:6],
              "projects": sorted(per_project.items(), key=lambda x: -x[1])[:8],
              "matrix": {"projects": top_projects, "models": top_models,
                         "cells": {p: {m: per_pm.get(p, {}).get(m, 0) for m in top_models}
                                   for p in top_projects}}}
    jwrite(MONITOR_CACHE, result)
    result["limits"] = usage_limits()
    return result


# ── static assets ────────────────────────────────────────────────────────────
# control-sliders icon — deliberately different from Claude's own starburst so
# the tab reads as "dashboard", not the Claude app itself
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="22" fill="#CC785C"/>
<g stroke="#fff" stroke-width="7" stroke-linecap="round">
<line x1="22" y1="32" x2="78" y2="32"/><line x1="22" y1="52" x2="78" y2="52"/>
<line x1="22" y1="72" x2="78" y2="72"/></g>
<circle cx="62" cy="32" r="7.5" fill="#CC785C" stroke="#fff" stroke-width="5"/>
<circle cx="36" cy="52" r="7.5" fill="#CC785C" stroke="#fff" stroke-width="5"/>
<circle cx="54" cy="72" r="7.5" fill="#CC785C" stroke="#fff" stroke-width="5"/></svg>"""

MANIFEST = {"name": "Claude Control", "short_name": "Claude Ctl",
            "start_url": "/", "display": "standalone",
            "background_color": "#f7f5f1", "theme_color": "#CC785C",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}


# ── http ─────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    server_ref = None

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", (DASH / "index.html").read_bytes())
        elif self.path == "/icon.svg":
            self._send(200, "image/svg+xml", ICON_SVG.encode())
        elif self.path == "/manifest.json":
            self._json(200, MANIFEST)
        elif self.path == "/api/state":
            self._json(200, state())
        elif self.path.startswith("/api/monitor"):
            if "force" in self.path:
                MONITOR_CACHE.unlink(missing_ok=True)
            self._json(200, monitor())
        elif self.path == "/api/doctor":
            self._json(200, {"checks": doctor()})
        elif self.path.startswith("/api/qr"):
            from urllib.parse import parse_qs, urlparse
            text = (parse_qs(urlparse(self.path).query).get("text") or [""])[0]
            try:
                self._send(200, "image/svg+xml", qr_svg(text))
            except Exception as e:
                self._json(400, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            out = {}
            if self.path == "/api/plugin":
                toggle_plugin(req["name"], req["enabled"])
            elif self.path == "/api/model":
                set_model(req["model"])
            elif self.path == "/api/sounds":
                (SOUNDS_OFF_FLAG.unlink(missing_ok=True) if req.get("enabled")
                 else SOUNDS_OFF_FLAG.touch())
            elif self.path == "/api/skill":
                move_skill(req["name"], req["action"])
            elif self.path == "/api/preset":
                out["id"] = save_user_preset(req)
            elif self.path == "/api/preset/delete":
                delete_user_preset(req["id"])
            elif self.path == "/api/rule":
                out["file"] = rule_save(req.get("name", ""), req.get("content", ""), req.get("file"))
            elif self.path == "/api/rule/get":
                f = req["file"]
                if not SAFE_FILE.fullmatch(f):
                    raise ValueError("bad file")
                return self._json(200, {"ok": True,
                                        "content": (RULES_DIR / f).read_text(errors="ignore")})
            elif self.path == "/api/rule/delete":
                rule_delete(req["file"])
            elif self.path == "/api/claude-md/get":
                return self._json(200, {"ok": True,
                                        "content": CLAUDE_MD.read_text(errors="ignore") if CLAUDE_MD.exists() else ""})
            elif self.path == "/api/claude-md/save":
                CLAUDE_MD.write_text(req.get("content", ""))
            elif self.path == "/api/config":
                cd = (req.get("code_dir") or "").strip()
                if cd and not SAFE_PATH.fullmatch(cd):
                    raise ValueError("נתיב לא תקין")
                if cd and not os.path.isdir(os.path.expanduser(cd)):
                    raise ValueError("תיקייה לא קיימת")
                jwrite(DASH_CONFIG, {**dash_config(), "code_dir": cd})
            elif self.path == "/api/login":
                run_in_terminal("claude /login")
            elif self.path == "/api/install-alias":
                install_alias()
            elif self.path == "/api/update":
                out["output"] = claude_update()
            elif self.path == "/api/remote-control":
                out.update(remote_control(req.get("target") or {"type": "terminal"}))
            elif self.path == "/api/mcp-server/add":
                out["file"] = add_mcp_profile(req)
            elif self.path == "/api/skill/create":
                out["slug"] = create_skill(req)
            elif self.path == "/api/file/create":
                out["path"] = create_file(req)
            elif self.path == "/api/open":
                open_target(req["what"])
            elif self.path == "/api/shutdown":
                self._json(200, {"ok": True, "bye": True})
                threading.Thread(target=H.server_ref.shutdown, daemon=True).start()
                return
            elif self.path == "/api/launch":
                pid = req.get("preset")
                preset = next((p for p in all_presets() if p["id"] == pid), None)
                if preset is None and req.get("custom") is not None:
                    c = req["custom"]
                    preset = {"id": "cl", "profiles": c.get("profiles", []),
                              "skills": bool(c.get("skills", True)),
                              "bare": bool(c.get("bare", False)),
                              "cwd": c.get("cwd", ""),
                              "model": c.get("model") if c.get("model") in MODELS else "",
                              "rules": [r for r in c.get("rules", [])
                                        if SAFE_FILE.fullmatch(r) and (RULES_DIR / r).exists()]}
                    if preset["cwd"] and not SAFE_PATH.fullmatch(preset["cwd"]):
                        raise ValueError("נתיב לא תקין")
                    preset["profiles"] = [x for x in preset["profiles"]
                                          if re.fullmatch(r"[A-Za-z0-9._-]+\.json", x)
                                          and (MCP_DIR / x).exists()]
                    out["est"] = estimate(preset)
                if preset is None:
                    raise ValueError("no preset")
                out.update(launch(preset, req.get("target") or {"type": "terminal"},
                                  dry=bool(req.get("dry"))))
            else:
                return self._json(404, {"error": "not found"})
            self._json(200, {"ok": True, "state": state(), **out})
        except Exception as e:
            self._json(400, {"error": str(e)})


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    if port_in_use():
        print(f"Claude Control already running · {url} — opening browser")
        webbrowser.open(url)
        raise SystemExit(0)
    srv = HTTPServer(("127.0.0.1", PORT), H)
    H.server_ref = srv
    print(f"Claude Control · {url}  (Ctrl-C or the ⏻ button to stop)")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
