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
import json, os, re, shlex, shutil, signal, socket, subprocess, threading, time, urllib.error, urllib.request, webbrowser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
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
# runtime data dir: CC_DATA lets a dev copy run next to the live one without
# sharing presets/config/cache (default: next to this file, as before)
DATA = Path(os.environ.get("CC_DATA") or DASH).expanduser().resolve()
DATA.mkdir(parents=True, exist_ok=True)
USER_PRESETS = DATA / "presets.json"
DASH_CONFIG = DATA / "config.json"
MONITOR_CACHE = DATA / ".monitor-cache.json"
SOUNDS_OFF_FLAG = DATA / ".sounds-off"
SESSION_RULES = DATA / ".session-rules.md"
DESKTOP_CFG = HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
PROJECTS = CL / "projects"
PORT = int(os.environ.get("CC_PORT") or 7799)

# ── language: messages, check titles and issue texts in Hebrew (default) or English.
# A request sets it with ?lang=en or a JSON field "lang"; it is kept per thread, so
# a background action thread sets its own copy.
_LANG = threading.local()


def set_lang(lang):
    _LANG.v = "en" if str(lang or "").lower().startswith("en") else "he"


def cur_lang():
    return getattr(_LANG, "v", "he")


def tr(he, en):
    return en if cur_lang() == "en" else he

CLT_FULL = ("Bash,Read,Edit,Write,TaskCreate,TaskUpdate,TaskStop,TaskOutput,"
            "WebFetch,WebSearch,Agent,AskUserQuestion,Skill")
CLT_NOSKILL = CLT_FULL.replace(",Skill", "")
CLT_BARE = "Bash,Read,Write,Edit,Grep,Glob"
# mirrors the /model picker; actual availability depends on the signed-in account
MODELS = ("opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku", "fable")

BUILTIN_PRESETS = [
    {"id": "bare",  "kind": "generic", "name": "נקי לגמרי",
     "desc": "בלי סקילים, בלי MCP, בלי sub-agents — מינימום מוחלט",
     "name_en": "Completely clean",
     "desc_en": "No skills, no MCP, no sub-agents - the absolute minimum",
     "profiles": [], "skills": False, "bare": True},
    {"id": "cl",    "kind": "generic", "name": "קלאסי + סקילים",
     "desc": "רזה עם כל הסקילים — ברירת מחדל יומיומית",
     "name_en": "Classic + skills",
     "desc_en": "Lean, with all skills - the everyday default",
     "profiles": [], "skills": True, "bare": False},
    {"id": "mcp",   "kind": "generic", "name": "קלאסי + MCP מקומי",
     "desc": "סקילים + כל פרופילי ה-MCP המקומיים",
     "name_en": "Classic + local MCP",
     "desc_en": "Skills + all local MCP profiles",
     "profiles": ["all-local.json"], "skills": True, "bare": False},
    {"id": "full",  "kind": "generic", "name": "הכול",
     "desc": "פלאגינים + ‎.mcp.json של הפרויקט — כמו claude רגיל",
     "name_en": "Everything",
     "desc_en": "Plugins + the project .mcp.json - like plain claude",
     "profiles": None, "skills": True, "bare": False},
]


def builtin_preset(p):
    """A built-in preset with name and desc in the request language."""
    out = {k: v for k, v in p.items() if not k.endswith("_en")}
    if cur_lang() == "en":
        out["name"], out["desc"] = p["name_en"], p["desc_en"]
    return out

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


def default_dir():
    """Fallback folder for sessions: config.json code_dir if it exists, else HOME."""
    cd = os.path.expanduser(dash_config().get("code_dir") or "")
    return cd if cd and os.path.isdir(cd) else str(HOME)


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
    # a built-in preset that names a missing MCP profile would fail to start: hide it
    out = [{**builtin_preset(p), "custom": False, "cmd": build_cmd(p), "est": estimate(p)}
           for p in BUILTIN_PRESETS
           if not p["profiles"] or all((MCP_DIR / x).exists() for x in p["profiles"])]
    out += [{**p, "kind": "custom", "custom": True, "cmd": build_cmd(p), "est": estimate(p)}
            for p in user_presets()]
    return out


def save_user_preset(p):
    name = (p.get("name") or "").strip()
    if not SAFE_NAME.fullmatch(name):
        raise ValueError(tr("שם לא תקין", "Invalid name"))
    cwd = (p.get("cwd") or "").strip()
    if cwd and not SAFE_PATH.fullmatch(cwd):
        raise ValueError(tr("נתיב לא תקין", "Invalid path"))
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
        profs = p.get("profiles") or []
        if profs:
            cfgs = " ".join(f"~/.claude/registry/mcp/{x}" for x in profs)
        elif (MCP_DIR / "none.json").exists():
            cfgs = "~/.claude/registry/mcp/none.json"
        else:                          # no MCP registry on this machine: an empty inline config
            cfgs = """'{"mcpServers":{}}'"""
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
        raise ValueError(tr("שם לא תקין", "Invalid name"))
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
        raise ValueError(tr(f"תיקייה לא קיימת: {cwd}", f"Folder does not exist: {cwd}"))
    base = re.sub(r"[^A-Za-z0-9-]", "", preset.get("id", "cl")) or "cl"
    if target.get("type") == "tmux":
        if shutil.which("tmux") is None:
            raise ValueError(tr("tmux לא מותקן — ראה טאב הוראות", "tmux is not installed - see the Guide page"))
        ses = target.get("session", "")
        if ses == "__new__":
            ses = "claude"
            if not any(s["name"] == ses for s in tmux_sessions()) and not dry:
                tmux("new-session", "-d", "-s", ses, "-c", cwd or str(HOME))
        elif ses not in [s["name"] for s in tmux_sessions()]:
            raise ValueError(tr("סשן tmux לא קיים", "The tmux session does not exist"))
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


# shell words that are never the "real" command of a hook: tests, flow, wrappers
HOOK_SKIP = {"[", "[[", "]", "]]", "test", "true", "false", "!", "if", "then", "else",
             "fi", "exit", "cd", "export", "source", ".", "command", "exec", "env",
             "nohup", "time", "sudo", "echo", "printf", "{", "}", "(", ")"}


def hook_commands(c):
    """Real command names in a hook line, e.g. `[ -f x ] || afplay y &` -> [afplay].
    Splits on && || ; | & and skips tests/builtins/VAR=val assignments."""
    out = []
    for seg in re.split(r"&&|\|\||[;|&\n]", c):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        if not words or words[0] in ("[", "[[", "test"):
            continue
        for w in words:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w) or w in HOOK_SKIP:
                continue
            name = w.split("/")[-1]
            if name and not name.startswith("-"):
                out.append(name)
            break
    return out[:2]


def hooks_summary():
    hooks = jread(SETTINGS).get("hooks") or {}
    out = []
    for ev, matchers in hooks.items():
        scripts = []
        for m in matchers:
            for h in m.get("hooks", []):
                c = h.get("command", "")
                found = re.findall(r"([A-Za-z0-9_.-]+\.(?:py|sh|js))", c)
                scripts += found or hook_commands(c)
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
    """Claude's official phone bridge (`claude remote-control`). Never starts a
    second daemon: if one runs, return its environment link from the daemon pane;
    else ask the watchdog script (which never doubles it) to start it."""
    d = rc_daemon_proc()
    if d:
        info = rc_daemon_pane()
        return {"running": True, "pid": d["pid"], "env_link": info["env_link"],
                "tmux": RC_DAEMON_TARGET}
    if WATCHDOG_SH.exists():
        subprocess.Popen(["/bin/zsh", str(WATCHDOG_SH)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"starting": True, "tmux": RC_DAEMON_TARGET}
    # no watchdog on this machine: open it the old way (tmux/Terminal)
    cmd = "claude remote-control"
    if target.get("type") == "tmux":
        if shutil.which("tmux") is None:
            raise ValueError(tr("tmux לא מותקן — ראה טאב הוראות", "tmux is not installed - see the Guide page"))
        ses = target.get("session", "")
        if ses == "__new__":
            ses = "claude"
            if not any(s["name"] == ses for s in tmux_sessions()):
                tmux("new-session", "-d", "-s", ses)
        elif ses not in [s["name"] for s in tmux_sessions()]:
            raise ValueError(tr("סשן tmux לא קיים", "The tmux session does not exist"))
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
        "features": features(),
        "lang": cur_lang(),
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


def _cli_check(label, args, timeout=25):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"})
        txt = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
        txt = ANSI_RE.sub("", txt)
        lines = [l.rstrip() for l in txt.splitlines()]
        txt = "\n".join(l for i, l in enumerate(lines)
                        if l.strip() or (i > 0 and lines[i - 1].strip()))
        return {"cmd": label, "output": txt.strip()[:6000] or "(no output)"}
    except FileNotFoundError:
        return {"cmd": label, "output": "claude CLI not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"cmd": label, "output": "(timed out — command may be interactive in this version)"}


# `claude mcp list` health-checks every server and can take 30s+: run it in a
# background thread and serve the last result with its age
_MCP_LIST = {"res": None, "ts": 0, "running": False}
_MCP_LOCK = threading.Lock()


def _mcp_list_refresh():
    try:
        res = _cli_check("claude mcp list", ["claude", "mcp", "list"], timeout=40)
        with _MCP_LOCK:
            _MCP_LIST.update(res=res, ts=time.time())
    finally:
        _MCP_LIST["running"] = False


def mcp_list_cached():
    with _MCP_LOCK:
        stale = time.time() - _MCP_LIST["ts"] > 120
        if stale and not _MCP_LIST["running"]:
            _MCP_LIST["running"] = True
            threading.Thread(target=_mcp_list_refresh, daemon=True).start()
        res = _MCP_LIST["res"]
    if res is None:
        return {"cmd": "claude mcp list", "output": "(pending: running in background, refresh in ~40s)",
                "pending": True}
    return {**res, "age": int(time.time() - _MCP_LIST["ts"]), "pending": _MCP_LIST["running"]}


def doctor():
    """Diagnostics via Claude's OWN built-in CLI commands only — no extra logic.
    [20261015] `claude config list` removed: the subcommand no longer exists and
    the CLI treated it as a prompt (a silent model call)."""
    out = [_cli_check("claude --version", ["claude", "--version"]),
           _cli_check("claude doctor", ["claude", "doctor"]),
           mcp_list_cached(),
           _cli_check("claude plugin list", ["claude", "plugin", "list"])]
    return out


# cl-dash opens the dashboard and starts it through launchd when needed (never a second server)
ALIAS_LINE = "alias cl-dash='~/.claude/dashboard/cl-dash'"


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


# ── Remote Control health: resumed sessions + phone daemon ───────────────────
# Reads the machine's own tooling: sessions.conf, claude-rc-resume.sh,
# claude-rc-watchdog.sh, their launchd agents and logs, tmux panes. The CC_RC_*
# env overrides exist only so a test can point actions at a throwaway tmux session.
RC_CONF = Path(os.environ.get("CC_RC_CONF") or HOME / ".config" / "claude-rc" / "sessions.conf")
RC_SESSION = os.environ.get("CC_RC_SESSION") or "rc-resume"
RC_DAEMON_TARGET = os.environ.get("CC_RC_DAEMON_TARGET") or "claude-RC-general:rc-daemon"
LOGS_DIR = HOME / "Library" / "Logs"
RC_DAEMON_LOG = LOGS_DIR / "claude-rc-daemon.log"   # written by tmux pipe-pane in the watchdog
BIN_DIR = Path(os.environ.get("CC_BIN_DIR") or HOME / ".local" / "bin").expanduser()
WATCHDOG_LOG = LOGS_DIR / "claude-rc-watchdog.log"
RESUME_LOG = LOGS_DIR / "claude-rc-resume.log"
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
FORTI_APP = Path("/Applications/FortiClient.app")
FORTI_LOGS = Path("/Library/Application Support/Fortinet/FortiClient/Logs")


def _agent_plist(suffix):
    hits = sorted(LAUNCH_AGENTS_DIR.glob(f"*.{suffix}.plist"))
    return hits[0] if hits else None


def _agent_label(suffix):
    """launchd label of a user agent named '<anything>.<suffix>' (labels differ per user)."""
    env = os.environ.get("CC_LABEL_" + suffix.replace("-", "_").upper())
    if env:
        return env
    hit = _agent_plist(suffix)
    return hit.stem if hit else f"{os.environ.get('CC_LABEL_PREFIX') or 'local'}.{suffix}"


def launch_agents():
    # read live: install.sh --with-restore may add the agents while the server runs
    return {"watchdog": _agent_label("claude-rc-watchdog"),
            "resume": _agent_label("claude-rc-resume")}


def _script(name):
    """Restore script path: the one the launchd agent runs, else CC_BIN_DIR/<name>."""
    plist = _agent_plist(name.replace(".sh", ""))
    if plist:
        try:
            import plistlib
            with open(plist, "rb") as f:
                for a in plistlib.load(f).get("ProgramArguments") or []:
                    if str(a).endswith("/" + name):
                        return Path(a).expanduser()
        except Exception:
            pass
    return BIN_DIR / name


class _ScriptPath:
    """Path-like lookup that is resolved on each use (agents can change at runtime)."""
    def __init__(self, name):
        self.name = name

    def path(self):
        return _script(self.name)

    def exists(self):
        return self.path().exists()

    def __str__(self):
        return str(self.path())

    __fspath__ = __str__


RESUME_SH = _ScriptPath("claude-rc-resume.sh")
WATCHDOG_SH = _ScriptPath("claude-rc-watchdog.sh")


def features():
    """What this machine has. The pages hide the parts that do not apply.
    CC_FEATURE_<NAME>=0|1 (for example CC_FEATURE_FORTICLIENT=0) overrides one detection."""
    found = {
        "restore": RC_CONF.exists() or _agent_plist("claude-rc-resume") is not None,
        "watchdog": WATCHDOG_SH.exists() or _agent_plist("claude-rc-watchdog") is not None,
        "forticlient": FORTI_APP.exists() or FORTI_LOGS.exists(),
        "mcp_registry": MCP_DIR.is_dir(),
        # the quota bars read the status-line cache, else Claude's own cached copy
        "rate_limits": RATE_LIMITS.exists() or bool(
            (jread(CLAUDE_JSON).get("cachedUsageUtilization") or {}).get("utilization")),
    }
    for k in found:
        v = os.environ.get("CC_FEATURE_" + k.upper())
        if v in ("0", "1"):
            found[k] = v == "1"
    return found


def forti_on():
    return features()["forticlient"]


RESTORE_MISSING = ("שחזור הסשנים לא מוגדר. התקן אותו עם ‎./install.sh --with-restore",
                   "Session restore is not set up. Install it with ./install.sh --with-restore")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RC_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
QR_CHARS = re.compile(r"[█▀▄]")


def sh(args, timeout=5, env=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def claude_bin():
    """Same choice as the watchdog: newest nvm claude, else homebrew, else PATH."""
    c = sorted((HOME / ".nvm" / "versions" / "node").glob("*/bin/claude"),
               key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for p in c + [Path("/opt/homebrew/bin/claude")]:
        if os.access(p, os.X_OK):
            return str(p)
    return shutil.which("claude") or "claude"


def rc_conf_entries():
    out = []
    try:
        for line in RC_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[0] and parts[1]:
                out.append({"name": parts[0], "uuid": parts[1],
                            "dir": parts[2] if len(parts) > 2 and parts[2] else default_dir()})
    except Exception:
        pass
    return out


def ps_list():
    """[{pid, started(epoch), cmd}] for every process."""
    rc, out = sh(["ps", "-axww", "-o", "pid=,lstart=,command="])
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        try:
            started = datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y").timestamp()
            procs.append({"pid": int(parts[0]), "started": started, "cmd": parts[6]})
        except ValueError:
            continue
    return procs


def resume_pids(procs):
    out = {}
    for p in procs:
        m = re.search(r"--resume[ =](" + UUID_RE.pattern + ")", p["cmd"])   # Claude Desktop uses --resume=
        if m and "claude" in p["cmd"] and not p["cmd"].startswith(("grep", "pgrep")):
            out.setdefault(m.group(1), p["pid"])
    return out


def rc_daemon_proc(procs=None):
    for p in procs or ps_list():
        first = p["cmd"].split()[0] if p["cmd"] else ""
        if re.search(r"(^|/)claude remote-control$", p["cmd"]) and \
                os.path.basename(first) not in ("caffeinate", "pgrep", "grep"):
            return p
    return None


def pane(target, lines=400):
    rc, out = sh(["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", f"-{lines}"])
    return out if rc == 0 else ""


def session_pane_state(text):
    """rc + link + waiting from a resumed session's pane."""
    lines = text.splitlines()
    rc, link = "unknown", None
    for l in lines:
        if "/remote-control is active" in l:
            rc = "connected"
        elif "Remote Control disconnected" in l:
            rc = "disconnected"
        m = re.search(r"https://claude\.ai/code/session_[A-Za-z0-9]+", l)
        if m:
            link = m.group(0)
    tail = [l for l in lines if l.strip()][-25:]
    waiting = any(re.match(r"\s*❯\s*1\.", l) for l in tail)
    busy_dialog = any("Background work is running" in l for l in tail)
    return rc, link, waiting, busy_dialog


def transcript_path(d, uuid):
    return PROJECTS / re.sub(r"[/.]", "-", d) / f"{uuid}.jsonl"


def rc_window(name):
    return f"={RC_SESSION}:={name}"


def rc_sessions(procs):
    pids = resume_pids(procs)
    _, wins = tmux("list-windows", "-t", f"={RC_SESSION}", "-F", "#{window_name}") \
        if shutil.which("tmux") else (1, "")
    wins = set(wins.splitlines())
    out = []
    for e in rc_conf_entries():
        pid = pids.get(e["uuid"])
        has_win = e["name"] in wins
        text = pane(rc_window(e["name"])) if has_win else ""
        rc, link, waiting, _ = session_pane_state(text) if pid else ("unknown", None, False, False)
        tp = transcript_path(e["dir"], e["uuid"])
        out.append({"name": e["name"], "uuid": e["uuid"], "dir": e["dir"], "in_list": True,
                    "running": bool(pid), "pid": pid,
                    "state": ("waiting" if waiting else "active") if pid else "stopped",
                    "rc": rc, "link": link,
                    "last_activity": tp.stat().st_mtime if tp.exists() else None,
                    "window": f"{RC_SESSION}:{e['name']}" if has_win else None})
    return out


def rc_daemon_pane():
    text = pane(RC_DAEMON_TARGET, 400)
    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not QR_CHARS.search(l)]
    status, env_link = "", None
    for l in lines:
        m = re.search(r"·✔︎?·\s*(\w+)", l)
        if m:
            status = m.group(1)
        m = re.search(r"https://claude\.ai/code\?environment=env_[A-Za-z0-9]+", l)
        if m:
            env_link = m.group(0)
    if not env_link:                   # link scrolled away: the watchdog pipes daemon output to a log
        try:
            with open(RC_DAEMON_LOG, "rb") as f:
                f.seek(max(0, f.seek(0, 2) - 200_000))
                found = re.findall(rb"https://claude\.ai/code\?environment=env_[A-Za-z0-9]+", f.read())
            env_link = found[-1].decode() if found else None
        except OSError:
            pass
    events = [l.strip() for l in lines if re.match(r"\s*\[\d\d:\d\d:\d\d\]", l)]
    if not status and events:          # status line scrolled away: infer from last event
        last = events[-1].lower()
        status = "Error" if ("giving up" in last or "error" in last) else \
                 "Connected" if ("reconnected" in last or "connected" in last) else ""
    return {"status": status, "env_link": env_link, "last_events": events[-8:], "lines": lines}


def check_network():
    t0 = time.time()
    try:
        urllib.request.urlopen(urllib.request.Request("https://api.anthropic.com/", method="GET"),
                               timeout=6)
        up = True
    except urllib.error.HTTPError:
        up = True                        # any HTTP answer means the network is up
    except Exception:
        up = False
    return {"up": up, "ms": int((time.time() - t0) * 1000)}


def check_vpn():
    rc, out = sh(["scutil", "--nc", "list"])
    for l in out.splitlines():
        if "com.fortinet" in l:
            return {"present": True, "connected": "(Connected)" in l}
    return {"present": False, "connected": False}


_AUTH = {"v": None, "ts": 0}


def check_auth():
    if _AUTH["v"] and time.time() - _AUTH["ts"] < 60:
        return _AUTH["v"]
    rc, out = sh([claude_bin(), "auth", "status"], timeout=8,
                 env={**os.environ, "ANTHROPIC_BASE_URL": "", "NO_COLOR": "1"})
    try:
        d = json.loads(out[out.index("{"):out.rindex("}") + 1])
        v = {"ok": True, "loggedIn": bool(d.get("loggedIn")),
             "subscriptionType": d.get("subscriptionType") or "", "email": d.get("email") or ""}
    except Exception:
        v = {"ok": False, "loggedIn": False, "subscriptionType": "", "email": ""}
    _AUTH.update(v=v, ts=time.time())
    return v


def check_launchd(label):
    rc, out = sh(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    if rc != 0:
        return {"loaded": False, "state": "", "last_exit": None, "runs": 0}
    st = re.search(r"^\s*state = (.+)$", out, re.M)
    ex = re.search(r"last exit code = (-?\d+)", out)
    runs = re.search(r"runs = (\d+)", out)
    return {"loaded": True, "state": st.group(1).strip() if st else "",
            "last_exit": int(ex.group(1)) if ex else None,
            "runs": int(runs.group(1)) if runs else 0}


def _fix(kind, label, action=None, command=None, link=None):
    f = {"kind": kind, "label": label}
    if action:
        f["action"] = action
    if command:
        f["command"] = command
    if link:
        f["link"] = link
    return f


ACTION = {"state": None}          # the running / last background action
ACTION_LOCK = threading.Lock()
_RC_CACHE = {}                    # lang -> (status, time)
_RC_LOCK = threading.Lock()


def rc_status(force=False):
    lang = cur_lang()
    with _RC_LOCK:
        hit = _RC_CACHE.get(lang)
        if not force and hit and time.time() - hit[1] < 3:
            return {**hit[0], "running_action": action_state()}
        feats = features()
        restore = feats["restore"]
        agents = launch_agents()
        res = {}
        jobs = {"network": check_network, "auth": check_auth, "procs": ps_list}
        if feats["forticlient"]:
            jobs["vpn"] = check_vpn
        if restore:
            jobs.update({k: (lambda l=l: check_launchd(l)) for k, l in agents.items()})

        def run(k, fn):
            try:
                res[k] = fn()
            except Exception as e:
                res[k] = {"error": str(e)}
        ts = [threading.Thread(target=run, args=(k, fn), daemon=True) for k, fn in jobs.items()]
        for t in ts:
            t.start()
        deadline = time.time() + 3.5
        for t in ts:
            t.join(max(0.0, deadline - time.time()))
        net = res.get("network") or {"up": False, "ms": 6000, "timeout": True}
        vpn = res.get("vpn") or {"present": False, "connected": False}
        auth = res.get("auth") or {"ok": False, "loggedIn": False}
        procs = res.get("procs") or []
        sessions = rc_sessions(procs) if restore else []
        dproc = rc_daemon_proc(procs)
        dpane = rc_daemon_pane() if (restore or dproc) else {"status": "", "env_link": None, "last_events": []}
        daemon = {"running": bool(dproc), "pid": dproc["pid"] if dproc else None,
                  "started": dproc["started"] if dproc else None,
                  "status": dpane["status"] if dproc else "",
                  "env_link": dpane["env_link"], "last_events": dpane["last_events"]}

        checks, issues = [], []
        # network
        if net.get("up"):
            checks.append({"id": "network", "title": tr("רשת", "Network"),
                           "level": "warn" if net["ms"] > 3000 else "ok",
                           "detail": tr(f"api.anthropic.com עונה ({net['ms']}ms)",
                                        f"api.anthropic.com answers ({net['ms']}ms)")})
        else:
            detail = tr("api.anthropic.com לא עונה.", "api.anthropic.com does not answer.")
            if vpn.get("connected"):
                detail += tr(" ה-VPN של FortiClient מחובר, ייתכן שהוא הגורם.",
                             " The FortiClient VPN is connected, it may be the cause.")
            checks.append({"id": "network", "title": tr("רשת", "Network"), "level": "bad", "detail": detail})
            issues.append({"id": "network", "level": "bad",
                           "title": tr("אין חיבור ל-Anthropic", "No connection to Anthropic"),
                           "detail": detail,
                           "fix": _fix("manual", tr("בדוק VPN או Wi-Fi", "Check the VPN or Wi-Fi") +
                                       (tr(" (נסה לנתק את FortiClient)", " (try to disconnect FortiClient)")
                                        if vpn.get("connected") else ""))})
        # vpn: only on a machine with FortiClient
        if feats["forticlient"]:
            if vpn.get("present"):
                checks.append({"id": "vpn", "title": "VPN (FortiClient)", "level": "ok",
                               "detail": tr("מחובר", "connected") if vpn.get("connected")
                               else tr("מנותק", "disconnected")})
            else:
                checks.append({"id": "vpn", "title": "VPN", "level": "ok",
                               "detail": tr("לא מוגדר", "not set up")})
        # auth
        if auth.get("loggedIn"):
            checks.append({"id": "auth", "title": tr("התחברות", "Sign-in"), "level": "ok",
                           "detail": tr(f"מחובר ({auth.get('subscriptionType') or '?'})",
                                        f"signed in ({auth.get('subscriptionType') or '?'})")})
        else:
            d = tr("לא מחובר לחשבון Claude", "Not signed in to a Claude account") if auth.get("ok") \
                else tr("לא ניתן לבדוק התחברות", "Cannot check the sign-in")
            fx = _fix("manual", tr("התחבר מחדש בטרמינל", "Sign in again in a terminal"),
                      command="claude auth login")
            checks.append({"id": "auth", "title": tr("התחברות", "Sign-in"), "level": "bad",
                           "detail": d, "action": fx})
            issues.append({"id": "auth", "level": "bad",
                           "title": tr("לא מחובר ל-Claude", "Not signed in to Claude"),
                           "detail": d, "fix": fx})
        if not restore:
            # restore is optional: one info line with the setup command, no agent issues
            checks.append({"id": "restore_setup", "title": tr("שחזור סשנים", "Session restore"),
                           "level": "info",
                           "detail": tr("לא מוגדר. התקנה: ‎./install.sh --with-restore",
                                        "Not set up. Install: ./install.sh --with-restore"),
                           "action": _fix("manual", tr("העתק פקודת התקנה", "Copy the install command"),
                                          command="./install.sh --with-restore")})
        else:
            # daemon
            if dproc:
                checks.append({"id": "daemon", "title": tr("שרת Remote Control", "Remote Control server"),
                               "level": "warn" if daemon["status"] == "Error" else "ok",
                               "detail": tr(f"רץ (pid {dproc['pid']}) {daemon['status']}",
                                            f"running (pid {dproc['pid']}) {daemon['status']}").strip()})
            else:
                fx = _fix("auto", tr("הפעל מחדש", "Restart"), action="restart_daemon") if net.get("up") else \
                    _fix("manual", tr("יופעל מחדש כשהרשת תחזור", "Restarts when the network is back"))
                checks.append({"id": "daemon", "title": tr("שרת Remote Control", "Remote Control server"),
                               "level": "warn", "detail": tr("לא רץ", "not running"), "action": fx})
                issues.append({"id": "daemon", "level": "warn",
                               "title": tr("שרת Remote Control לא רץ", "The Remote Control server is not running"),
                               "detail": tr("הטלפון לא יראה סשנים חדשים", "The phone will not see new sessions"),
                               "fix": fx})
            # launchd agents
            for key, title in (("watchdog", "Watchdog"), ("resume", tr("שחזור סשנים", "Session restore"))):
                ld = res.get(key) or {"loaded": False}
                label = agents[key]
                cmd = f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist"
                bad_exit = ld.get("last_exit") not in (None, 0)
                if ld.get("loaded") and not bad_exit:
                    checks.append({"id": key, "title": title, "level": "ok",
                                   "detail": tr(f"טעון, {ld.get('runs', 0)} הרצות, יציאה אחרונה 0",
                                                f"loaded, {ld.get('runs', 0)} runs, last exit 0")})
                else:
                    d = tr("לא טעון ב-launchd", "not loaded in launchd") if not ld.get("loaded") \
                        else tr(f"יציאה אחרונה {ld.get('last_exit')}", f"last exit {ld.get('last_exit')}")
                    fx = _fix("manual", tr("טען מחדש את הסוכן", "Load the agent again"), command=cmd)
                    checks.append({"id": key, "title": title, "level": "warn", "detail": d, "action": fx})
                    issues.append({"id": key, "level": "warn", "title": f"{title}: {d}", "detail": label,
                                   "fix": fx})
            # sessions
            for s in sessions:
                if not s["running"]:
                    issues.append({"id": f"stopped:{s['name']}", "level": "warn",
                                   "title": tr(f"{s['name']} לא רץ", f"{s['name']} is not running"),
                                   "detail": tr("הסשן ברשימה אבל לא פעיל", "The session is in the list but not active"),
                                   "fix": _fix("auto", tr("הפעל סשנים חסרים", "Start missing sessions"),
                                               action="resume_missing")})
                elif s["state"] == "waiting":
                    issues.append({"id": f"waiting:{s['name']}", "level": "warn",
                                   "title": tr(f"{s['name']} מחכה לתשובה", f"{s['name']} waits for an answer"),
                                   "detail": tr("יש בסשן תפריט שמחכה לבחירה שלך",
                                                "A menu in the session waits for your choice"),
                                   "fix": _fix("manual", tr("פתח את הסשן וענה", "Open the session and answer"),
                                               command=f"tmux attach -t {RC_SESSION} \\; select-window -t {s['name']}",
                                               link=s["link"])})
                elif s["rc"] == "disconnected":
                    issues.append({"id": f"disconnected:{s['name']}", "level": "warn",
                                   "title": tr(f"{s['name']}: Remote Control מנותק",
                                               f"{s['name']}: Remote Control disconnected"),
                                   "detail": tr("הסשן רץ אבל לא נראה בטלפון",
                                                "The session runs but the phone does not see it"),
                                   "fix": _fix("auto", tr("הפעל מחדש את הסשן", "Restart the session"),
                                               action="restart_session")})
            n_run = sum(1 for s in sessions if s["running"])
            checks.append({"id": "resume_sessions", "title": tr("סשנים", "Sessions"), "level":
                           "ok" if n_run == len(sessions) else "warn",
                           "detail": tr(f"{n_run} מתוך {len(sessions)} רצים",
                                        f"{n_run} of {len(sessions)} running")})
        overall = "bad" if any(i["level"] == "bad" for i in issues) else \
                  "warn" if issues else "ok"
        v = {"ts": int(time.time()), "overall": overall, "issues": issues, "checks": checks,
             "sessions": sessions, "daemon": daemon, "network_up": bool(net.get("up")),
             "features": feats, "rc_session": RC_SESSION, "lang": lang}
        _RC_CACHE[lang] = (v, time.time())
        return {**v, "running_action": action_state()}


# ── RC logs ──────────────────────────────────────────────────────────────────
def log_level(msg):
    m = msg.lower()
    if re.search(r"error|eror|fail|unreachable|giving up|dead", m):
        return "bad"
    if re.search(r"network down|disconnected|warn|skip", m):
        return "warn"
    if re.search(r"started|reconnected|connected|\bdone\b|\bready\b", m):
        return "ok"
    return "info"


def tail_lines(path, n=400, max_bytes=512 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        if size > max_bytes:
            lines = lines[1:]           # first line is probably cut
        return lines[-n:]
    except Exception:
        return []


def _script_log(path, src, n):
    out, last = [], ""
    for l in tail_lines(path, n * 2):
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)", l)
        if m:
            last = m.group(1)
            out.append({"ts": last, "src": src, "msg": m.group(2)})
        elif l.strip():
            out.append({"ts": last, "src": src, "msg": l.rstrip(), "cont": True})
    return out


def _daemon_log():
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for l in rc_daemon_pane()["lines"]:
        m = re.match(r"\s*\[(\d\d:\d\d:\d\d)\]\s*(.*)", l)
        out.append({"ts": m.group(1) if m else "", "src": "daemon",
                    "msg": (m.group(2) if m else l.strip()), "_k": f"{today} {m.group(1)}" if m else ""})
    return out


FORTI_KEEP = re.compile(r"EROR|WARN|Start|Stop|tunnel|DNS|resolve", re.I)


def _forti_log(n):
    out = []
    for f in ("fctproxy.log", "vpn-provider.log", "epctrl.log"):
        for l in tail_lines(FORTI_LOGS / f, 600):
            m = re.match(r"(\d{4})(\d\d)(\d\d) (\d\d:\d\d:\d\d)\.\d+ \S+ \[([^\]]*)\]\s*(.*)", l)
            if not m or not FORTI_KEEP.search(l):
                continue
            msg = f"[{m.group(5)}] {m.group(6)}"
            out.append({"ts": f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}",
                        "src": "forti", "msg": msg[:400]})
    out.sort(key=lambda x: x["ts"])
    return out[-n:]


def rc_logs(src="all", n=200, q=""):
    n = max(1, min(int(n or 200), 2000))
    forti = forti_on()
    if src not in ("all", "watchdog", "resume", "daemon", "forti"):
        raise ValueError("unknown src")
    srcs = (["watchdog", "resume", "daemon"] + (["forti"] if forti else [])) if src == "all" else [src]
    if src == "forti" and not forti:      # no FortiClient on this machine: nothing to show
        srcs = []
    lines = []
    for s in srcs:
        if s == "watchdog":
            lines += _script_log(WATCHDOG_LOG, "watchdog", n)
        elif s == "resume":
            lines += _script_log(RESUME_LOG, "resume", n)
        elif s == "daemon":
            lines += _daemon_log()
        elif s == "forti":
            lines += _forti_log(n)
    if len(srcs) > 1:                 # merge by time; untimed lines follow the line above
        k = ""
        for l in lines:
            k = l.get("_k") or (l["ts"] if len(l["ts"]) > 8 else k)
            l["_k"] = k
        lines.sort(key=lambda l: l["_k"])
    for l in lines:
        l.pop("_k", None)
        l["level"] = log_level(l["msg"])
        l.pop("cont", None)
    if q:
        ql = q.lower()
        lines = [l for l in lines if ql in l["msg"].lower()]
    return {"lines": lines[-n:]}


# ── RC actions ───────────────────────────────────────────────────────────────
def action_state():
    a = ACTION["state"]
    if not a:
        return None
    if a.get("done") and time.time() - a.get("finished", 0) > 300:
        return None
    return a


def _step(results, step, ok, detail=""):
    results.append({"step": step, "ok": bool(ok), "detail": str(detail)[:600]})
    return ok


def _resume_env():
    env = {**os.environ, "RC_WAIT": "0", "RC_GAP": "3"}
    if os.environ.get("CC_RC_CONF"):
        env["RC_MANIFEST"] = str(RC_CONF)
    if os.environ.get("CC_RC_SESSION"):
        env["RC_SESSION"] = RC_SESSION
    if os.environ.get("CC_RC_CMD"):
        env["RC_CMD"] = os.environ["CC_RC_CMD"]
    env.setdefault("RC_DIR", default_dir())      # folder for lines without one, same as the server
    return env


def do_resume_missing(results, dry=False):
    if not RESUME_SH.exists():
        return _step(results, "resume_missing", False, tr(f"{RESUME_SH} חסר", f"{RESUME_SH} is missing"))
    env = _resume_env()
    if dry:
        env["DRY_RUN"] = "1"
    rc, out = sh(["/bin/zsh", str(RESUME_SH)], timeout=180, env=env)
    lines = [l for l in out.splitlines() if l.strip()]
    return _step(results, "resume_missing" + (" (dry)" if dry else ""), rc == 0,
                 "\n".join(lines[-12:]) or f"exit {rc}")


def _find_entry(name, uuid=None):
    for e in rc_conf_entries():
        if e["name"] == name and (not uuid or e["uuid"] == uuid):
            return e
    return None


def do_restart_session(results, name, dry=False):
    e = _find_entry(name)
    if not e:
        return _step(results, f"restart {name}", False, tr("לא נמצא ברשימת הסשנים", "Not found in the session list"))
    win = rc_window(name)
    pid = resume_pids(ps_list()).get(e["uuid"])
    if dry:
        _step(results, f"exit {name}", True,
              f"would send /exit to {RC_SESSION}:{name} (pid {pid or '-'}) and wait up to 25s")
        return _step(results, f"resume {name}", True,
                     f"would run {RESUME_SH.name} with RC_WAIT=0 RC_GAP=3")
    if pid:
        tmux("send-keys", "-t", win, "/exit", "Enter")
        gone = False
        for _ in range(25):
            time.sleep(1)
            if e["uuid"] not in resume_pids(ps_list()):
                gone = True
                break
            if session_pane_state(pane(win, 60))[3]:
                tmux("send-keys", "-t", win, "Escape")
                return _step(results, f"exit {name}", False,
                             tr("needs_user: יש עבודת רקע בסשן. לא עצרתי אותה. סגור אותה ידנית ונסה שוב",
                                "needs_user: background work runs in the session. I did not stop it. Close it by hand and try again"))
        if not gone:
            return _step(results, f"exit {name}", False, tr("הסשן לא נסגר תוך 25 שניות", "The session did not close within 25 seconds"))
        _step(results, f"exit {name}", True, tr(f"pid {pid} יצא", f"pid {pid} exited"))
        time.sleep(1)                  # let the shell prompt come back
    else:
        _step(results, f"exit {name}", True, tr("לא רץ, מדלג ליציאה", "not running, skipping the exit"))
    ok = do_resume_missing(results)
    for _ in range(10):
        if e["uuid"] in resume_pids(ps_list()):
            return _step(results, f"verify {name}", True, tr("רץ שוב", "running again"))
        time.sleep(1)
    return _step(results, f"verify {name}", False, tr("התהליך לא עלה תוך 10 שניות", "The process did not start within 10 seconds")) and ok


def do_restart_daemon(results, dry=False):
    if not check_network()["up"]:
        return _step(results, "restart_daemon", False, tr("הרשת למטה, לא מפעיל מחדש", "The network is down, not restarting"))
    d = rc_daemon_proc()
    if dry:
        return _step(results, "restart_daemon (dry)", True,
                     f"would kill pid {d['pid']} and run the watchdog" if d else
                     "not running: would run the watchdog")
    if d:
        try:
            os.kill(d["pid"], signal.SIGTERM)
            _step(results, "kill daemon", True, f"pid {d['pid']}")
        except Exception as ex:
            return _step(results, "kill daemon", False, ex)
    if WATCHDOG_SH.exists():
        subprocess.Popen(["/bin/zsh", str(WATCHDOG_SH)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return _step(results, "watchdog", True, tr("ה-watchdog יפעיל אותו מחדש (עד ~70 שניות)",
                                                    "The watchdog restarts it (up to ~70 seconds)"))
    return _step(results, "watchdog", False, tr(f"{WATCHDOG_SH} חסר", f"{WATCHDOG_SH} is missing"))


def fix_all_plan():
    st = rc_status(force=True)
    if not st["network_up"]:
        return st, []
    autos = {i["fix"]["action"]: True for i in st["issues"] if i["fix"].get("kind") == "auto"}
    plan = []
    if autos.get("resume_missing"):
        plan.append(("resume_missing", None))
    for i in st["issues"]:
        if i["fix"].get("action") == "restart_session":
            plan.append(("restart_session", i["id"].split(":", 1)[1]))
    if autos.get("restart_daemon"):
        plan.append(("restart_daemon", None))
    return st, plan


def do_fix_all(results, dry=False):
    st, plan = fix_all_plan()
    if not st["network_up"]:
        return _step(results, "fix_all", False, tr("הרשת למטה, לא מתקן כלום", "The network is down, fixing nothing"))
    if not plan:
        return _step(results, "fix_all", True, tr("אין מה לתקן אוטומטית", "Nothing to fix automatically"))
    ok = True
    for act, name in plan:
        if act == "resume_missing":
            ok = (do_resume_missing(results) if not dry else
                  _step(results, "resume_missing (dry)", True, "would run the resume script")) and ok
        elif act == "restart_session":
            ok = do_restart_session(results, name, dry) and ok
        elif act == "restart_daemon":
            ok = do_restart_daemon(results, dry) and ok
    return ok


def rc_keep(req, dry=False):
    name = (req.get("name") or "").strip()
    uuid = (req.get("uuid") or "").strip().lower()
    d = os.path.expanduser((req.get("dir") or "").strip() or default_dir())
    if not RC_NAME_RE.fullmatch(name):
        raise ValueError(tr("שם לא תקין (בלי רווחים או |)", "Invalid name (no spaces or |)"))
    if not UUID_RE.fullmatch(uuid):
        raise ValueError(tr("מזהה סשן לא תקין", "Invalid session id"))
    if not os.path.isdir(d) or "|" in d:
        raise ValueError(tr("תיקייה לא קיימת", "Folder does not exist"))
    for e in rc_conf_entries():
        if e["name"] == name or e["uuid"] == uuid:
            raise ValueError(tr(f"כבר ברשימה ({e['name']})", f"Already in the list ({e['name']})"))
    line = f"{name}|{uuid}|{d}"
    if not dry and not features()["restore"]:
        raise ValueError(tr(*RESTORE_MISSING))
    if not dry:
        txt = RC_CONF.read_text() if RC_CONF.exists() else ""
        with open(RC_CONF, "a") as f:
            f.write(("" if not txt or txt.endswith("\n") else "\n") + line + "\n")
    return line


def rc_drop(req, dry=False):
    name = (req.get("name") or "").strip()
    if not RC_NAME_RE.fullmatch(name):
        raise ValueError(tr("שם לא תקין", "Invalid name"))
    lines = RC_CONF.read_text().splitlines(keepends=True)
    stamp = datetime.now().strftime("%Y%m%d")
    hit = None
    for i, l in enumerate(lines):
        s = l.strip()
        if s and not s.startswith("#") and s.split("|")[0] == name:
            hit = i
            break
    if hit is None:
        raise ValueError(tr("לא נמצא ברשימה", "Not found in the list"))
    new = f"# [{stamp}] dropped from dashboard. {lines[hit].rstrip(chr(10))}\n"
    if not dry:
        lines[hit] = new
        RC_CONF.write_text("".join(lines))
    return new.strip()


# ---------------------------------------------------------------------------
# Session search: find any Claude Code session by title, first message, id,
# folder or content, see if it runs and where (tmux), and bring it back.
# ---------------------------------------------------------------------------
_SEARCH_IDX = {}            # path -> (mtime, size, meta)
_SEARCH_LOCK = threading.Lock()


def _session_meta(p):
    """title, first user message and folder of one transcript (cached by mtime+size)."""
    st = p.stat()
    hit = _SEARCH_IDX.get(str(p))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    title, first, cwds = None, None, []
    rc, out = sh(["grep", "-a", "-o", r'"customTitle":"[^"]*"', str(p)], timeout=5)
    if out.strip():
        title = json.loads("{" + out.strip().splitlines()[-1] + "}")["customTitle"]
    try:
        with open(p, "rb") as f:
            for i, raw in enumerate(f):
                if i > 400 or (first and cwds):
                    break
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("cwd") and d["cwd"] not in cwds:
                    cwds.append(d["cwd"])
                if first is None and d.get("type") == "user":
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        first = " ".join(c.split())[:160]
    except OSError:
        pass
    folder = p.parent.name
    # resume only works from the folder the transcript is stored under
    run_dir = next((c for c in cwds if re.sub(r"[/.]", "-", c) == folder), cwds[0] if cwds else None)
    meta = {"uuid": p.stem, "title": title, "first": first, "dir": run_dir, "folder": folder}
    _SEARCH_IDX[str(p)] = (st.st_mtime, st.st_size, meta)
    return meta


def _tmux_locations(pids):
    """pid -> 'session:window' for processes that run inside a tmux pane."""
    if not pids:
        return {}
    rc, out = sh(["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}:#{window_name}"])
    panes = dict(l.split(" ", 1) for l in out.splitlines() if " " in l)
    rc, out = sh(["ps", "-axo", "pid=,ppid="])
    parent = {}
    for l in out.split("\n"):
        a = l.split()
        if len(a) == 2:
            parent[a[0]] = a[1]
    res = {}
    for pid in pids:
        cur, hops = str(pid), 0
        while cur and cur != "1" and hops < 12:
            if cur in panes:
                res[pid] = panes[cur]
                break
            cur, hops = parent.get(cur), hops + 1
    return res


def session_search(q="", days=14, limit=60, content=False):
    q = (q or "").strip()
    days = max(1, min(int(days or 14), 365))
    limit = max(1, min(int(limit or 60), 300))
    cutoff = time.time() - days * 86400
    files = [p for p in PROJECTS.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff and p.stat().st_size > 0]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    with _SEARCH_LOCK:
        metas = [(p, _session_meta(p)) for p in files]
    content_hits = set()
    if q and content:
        for i in range(0, len(files), 200):           # grep -F is much faster than parsing JSON
            rc, out = sh(["grep", "-l", "-i", "-F", "--", q] + [str(p) for p in files[i:i + 200]], timeout=20)
            content_hits.update(out.split())
    procs = ps_list()
    running = resume_pids(procs)
    locs = _tmux_locations(list(running.values()))
    cmd_of = {p["pid"]: p["cmd"] for p in procs}
    for pid in running.values():
        if pid not in locs and "Application Support/Claude/claude-code" in cmd_of.get(pid, ""):
            locs[pid] = "Claude Desktop"
    listed = {e["uuid"]: e["name"] for e in rc_conf_entries()}
    ql = q.lower()
    out = []
    for p, m in metas:
        hay = " ".join(str(x or "") for x in (m["title"], m["first"], m["uuid"], m["dir"])).lower()
        if ql and ql not in hay and str(p) not in content_hits:
            continue
        pid = running.get(m["uuid"])
        mtime = p.stat().st_mtime
        size = p.stat().st_size
        out.append({**m, "last_activity": mtime, "size": size,
                    "running": bool(pid), "pid": pid, "where": locs.get(pid),
                    "in_list": m["uuid"] in listed, "list_name": listed.get(m["uuid"]),
                    "match": "content" if ql and ql not in hay else ("meta" if ql else None),
                    # fell: active in the last 24 h, big enough to be real work, not running, not restored
                    # (idle 10+ min: a session still writing is alive somewhere we cannot see)
                    "fallen": (not pid and m["uuid"] not in listed and size > 20_000
                               and 600 < time.time() - mtime < 86400)})
        if len(out) >= limit:
            break
    return {"q": q, "days": days, "scanned": len(files), "results": out}


def session_resume(req, dry=False):
    """Add a found session to the auto-restore list (if needed) and start it."""
    uuid = (req.get("uuid") or "").strip().lower()
    if not UUID_RE.fullmatch(uuid):
        raise ValueError(tr("מזהה סשן לא תקין", "Invalid session id"))
    hits = list(PROJECTS.glob(f"*/{uuid}.jsonl"))
    if not hits:
        raise ValueError(tr("לא נמצא קובץ שיחה לסשן הזה", "No transcript file found for this session"))
    meta = _session_meta(hits[0])
    if not meta["dir"] or not os.path.isdir(meta["dir"]):
        raise ValueError(tr("התיקייה של הסשן לא קיימת יותר", "The session folder no longer exists"))
    if not dry and not features()["restore"]:
        raise ValueError(tr(*RESTORE_MISSING))
    results = []
    listed = {e["uuid"]: e["name"] for e in rc_conf_entries()}
    name = listed.get(uuid)
    if not name:
        base = (req.get("name") or meta["title"] or "session-" + uuid[:8]).strip()
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")[:40] or "session-" + uuid[:8]
        taken = {e["name"] for e in rc_conf_entries()}
        if name in taken:
            name = (name[:31] + "-" + uuid[:8])[:40]
        line = rc_keep({"name": name, "uuid": uuid, "dir": meta["dir"]}, dry=dry)
        _step(results, "keep", True, line)
    if uuid in resume_pids(ps_list()):
        _step(results, "start", True, tr("כבר רץ", "already running"))
    else:
        do_resume_missing(results, dry=dry)
    return name, results


RC_ACTIONS = ("fix_all", "restart_session", "resume_missing", "restart_daemon", "keep", "drop")
RC_LABELS = {"fix_all": ("תיקון הכול", "Fix everything"),
             "restart_session": ("הפעלה מחדש של סשן", "Restart a session"),
             "resume_missing": ("הפעלת סשנים חסרים", "Start missing sessions"),
             "restart_daemon": ("הפעלה מחדש של השרת", "Restart the server")}


def rc_label(act):
    he, en = RC_LABELS.get(act, (act, act))
    return tr(he, en)


def rc_action(req):
    act = req.get("action")
    dry = bool(req.get("dry"))
    if act not in RC_ACTIONS:
        raise ValueError(tr("פעולה לא מוכרת", "Unknown action"))
    results = []
    if act == "keep":
        line = rc_keep(req, dry)
        _step(results, "keep" + (" (dry)" if dry else ""), True, line)
        _RC_CACHE.clear()
        return {"ok": True, "message": (tr("יתווסף לרשימה", "Will be added to the list") if dry
                                                else tr("נוסף לרשימה", "Added to the list")), "results": results}
    if act == "drop":
        line = rc_drop(req, dry)
        _step(results, "drop" + (" (dry)" if dry else ""), True, line)
        _RC_CACHE.clear()
        return {"ok": True, "message": (tr("יוסר מהרשימה (הסשן עצמו ממשיך לרוץ)",
                                           "Will be removed from the list (the session itself keeps running)") if dry
                                        else tr("הוסר מהרשימה. הסשן עצמו ממשיך לרוץ",
                                                "Removed from the list. The session itself keeps running")), "results": results}
    if act == "restart_session" and not _find_entry(req.get("name") or ""):
        raise ValueError(tr("סשן לא נמצא ברשימה", "Session not found in the list"))

    def body(res):
        if act == "fix_all":
            return do_fix_all(res, dry)
        if act == "restart_session":
            return do_restart_session(res, req["name"], dry)
        if act == "resume_missing":
            return do_resume_missing(res, dry)
        return do_restart_daemon(res, dry)

    if dry:                            # dry runs are quick (no waits): answer inline
        ok = body(results)
        return {"ok": bool(ok), "message": tr(f"תוכנית בלבד: {rc_label(act)}", f"Plan only: {rc_label(act)}"), "results": results,
                "dry": True}
    with ACTION_LOCK:
        cur = ACTION["state"]
        if cur and not cur.get("done"):
            return {"ok": False, "message": tr(f"פעולה אחרת רצה: {rc_label(cur['action'])}",
                                                   f"Another action is running: {rc_label(cur['action'])}"),
                    "results": []}
        state_ = {"action": act, "name": req.get("name"), "started": time.time(),
                  "done": False, "ok": None, "results": []}
        ACTION["state"] = state_

    lang = cur_lang()

    def worker():
        set_lang(lang)                 # thread-local: the action thread answers in the request language
        try:
            ok = body(state_["results"])
        except Exception as ex:
            ok = _step(state_["results"], act, False, ex)
        state_.update(done=True, ok=bool(ok), finished=time.time(),
                      message=f"{rc_label(act)}: " + (tr("הסתיים", "done") if ok else tr("נכשל", "failed")))
        _RC_CACHE.clear()
    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "started": True, "message": tr(f"התחיל: {rc_label(act)}", f"Started: {rc_label(act)}"), "results": []}


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

    def _lang_from_query(self):
        from urllib.parse import parse_qs, urlparse
        set_lang((parse_qs(urlparse(self.path).query).get("lang") or ["he"])[0])

    def do_GET(self):
        self._lang_from_query()
        route = self.path.split("?", 1)[0]
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", (DASH / "index.html").read_bytes())
        elif route == "/icon.svg":
            self._send(200, "image/svg+xml", ICON_SVG.encode())
        elif route == "/manifest.json":
            self._json(200, MANIFEST)
        elif route == "/api/state":
            self._json(200, state())
        elif self.path.startswith("/api/monitor"):
            if "force" in self.path:
                MONITOR_CACHE.unlink(missing_ok=True)
            self._json(200, monitor())
        elif route == "/api/doctor":
            self._json(200, {"checks": doctor()})
        elif self.path.startswith("/api/rc/status"):
            self._json(200, rc_status(force="force" in self.path))
        elif self.path.startswith("/api/sessions/search"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            g = lambda k, d: (qs.get(k) or [d])[0]
            try:
                self._json(200, session_search(g("q", ""), g("days", 14), g("limit", 60),
                                               g("content", "0") in ("1", "true")))
            except ValueError as e:
                self._json(400, {"error": str(e)})
        elif self.path.startswith("/api/logs"):
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            try:
                self._json(200, rc_logs((qs.get("src") or ["all"])[0], (qs.get("n") or [200])[0],
                                        (qs.get("q") or [""])[0]))
            except ValueError as e:
                self._json(400, {"error": str(e)})
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
        self._lang_from_query()
        self.path = self.path.split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if isinstance(req, dict) and req.get("lang"):
                set_lang(req["lang"])
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
                    raise ValueError(tr("נתיב לא תקין", "Invalid path"))
                if cd and not os.path.isdir(os.path.expanduser(cd)):
                    raise ValueError(tr("תיקייה לא קיימת", "Folder does not exist"))
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
            elif self.path == "/api/rc/action":
                try:
                    return self._json(200, rc_action(req))
                except ValueError as e:      # validation: same shape, ok false
                    return self._json(200, {"ok": False, "message": str(e), "error": str(e),
                                            "results": []})
            elif self.path == "/api/sessions/resume":
                try:
                    name, results = session_resume(req, dry=bool(req.get("dry")))
                    ok = all(r["ok"] for r in results)
                    msg = (tr(f"תוכנית בלבד: {name}", f"Plan only: {name}") if req.get("dry") else
                           tr(f"{name} הופעל ונוסף לשחזור האוטומטי", f"{name} started and added to auto-restore")
                           if ok else tr(f"{name}: חלק מהשלבים נכשלו", f"{name}: some steps failed"))
                    return self._json(200, {"ok": ok, "name": name, "message": msg, "results": results,
                                            "dry": bool(req.get("dry"))})
                except ValueError as e:
                    return self._json(200, {"ok": False, "message": str(e), "error": str(e), "results": []})
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
                        raise ValueError(tr("נתיב לא תקין", "Invalid path"))
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
        if not os.environ.get("CC_NO_BROWSER"):
            webbrowser.open(url)
        raise SystemExit(0)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    H.server_ref = srv
    print(f"Claude Control · {url}  (Ctrl-C or the ⏻ button to stop)")
    if not os.environ.get("CC_NO_BROWSER"):            # CC_NO_BROWSER=1: dev/headless run
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
