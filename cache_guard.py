#!/usr/bin/env python3
"""
cache-guard: keep your Claude Code prompt cache warm and your token use low.

Commands
  statusline          Two-line status bar: cache, session cost and usage-limit stats, and a tip.
  doctor              Scan your setup for things that silently break the cache or waste tokens.
  report [SESSION]    Cache stats for a session: totals, cost, real cache misses, and its subagents.
                      SESSION = session id or .jsonl path; default = latest session in this folder.
  report --all [--days N]
                      Every session from the last N days (default 30): cost split between main
                      conversations and subagents, rebuilds by cause, and whether a 5m or 1h
                      subagent cache would have been cheaper.
  install [flags]     Copy to ~/.claude/cache-guard/ and wire into ~/.claude/settings.json.
                        --trim     also trim huge Bash output before Claude reads it (opt-in)
                        --notify   desktop alert when a warm cache is about to expire
                        --lean     make the bundled "Lean" output style your default
                        --all      all three
                        --no-trim / --no-notify   turn those back off
  uninstall           Remove cache-guard from settings.json.

Hook entry points (wired by install; you never call these yourself)
  hook-model-switch   PreModelSwitch: ask before a switch that throws away a warm cache.
  hook-compact        PreCompact: a manual /compact on a COLD cache must be repeated to run.
  hook-prompt         UserPromptSubmit: a typed message into a big COLD session is stopped once,
                      with what it will cost. Automated prompts are never touched.
  hook-session-start  SessionStart: warn when a resume re-sends a big, expired history.
  hook-trim           PostToolUse (Bash): trim huge output, keep errors, save the full log.

Design rule: hooks only ever show messages to YOU (systemMessage / stderr).
They never inject text into Claude's context, so they can't break the cache themselves.

Needs Python 3.8+ and Claude Code v2.1.251+ (v2.1.260+ for miss causes). No dependencies.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "2.1.0"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
GUARD_DIR = CLAUDE_DIR / "cache-guard"
STATE_DIR = GUARD_DIR / "state"
LOG_DIR = GUARD_DIR / "logs"
CONFIG_PATH = GUARD_DIR / "config.json"
SETTINGS = CLAUDE_DIR / "settings.json"
STYLE_DIR = CLAUDE_DIR / "output-styles"
SCRIPT_NAME = "cache_guard.py"

DEFAULT_CONFIG = {
    "notify": False,             # desktop alert when a warm cache is about to expire
    "notify_minutes": 3,
    "expiry_warn_minutes": 3,    # status-line warning threshold
    "context_warn_pct": 75,      # suggest /compact at a break above this
    "limit_warn_pct": 80,        # 5-hour / 7-day usage warning threshold
    "resume_warn_tokens": 50000, # warn on resume when re-sending more than this, uncached
    "compact_confirm_seconds": 120,
    "cold_prompt": "block",      # block = stop a cold message once; warn = only tell you; off
    "cold_prompt_warn_tokens": 100000,  # only when the message would re-read at least this much
    "cold_prompt_confirm_seconds": 120,
    "pricing": {},               # override or add list prices: {"model-id-prefix": [in, 5m, 1h, read, out]}
    "statusline_cost": True,     # show session cost, avoidable cost and cache savings on the status line
    "avoidable_warn_pct": 15,    # colour the avoidable figure from this share of the session's cost
    "cause_tip_seconds": 600,
    "trim": {
        "max_chars": 12000,      # only trim output longer than this
        "head_lines": 30,
        "tail_lines": 60,
        "max_error_lines": 80,
        "keep_logs_days": 3,
    },
}

# API list prices in USD per million tokens: (input, 5m cache write, 1h cache write, cache read, output).
# Taken from Anthropic's pricing page in November 2026. Prices change: override or add models
# under "pricing" in config.json. On a subscription these show relative size, not a bill.
PRICES = {
    "claude-fable-5-2": (8, 10, 16, 0.20, 40),
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50),
    "claude-opus-5-5": (4, 5, 8, 0.20, 20),
    "claude-opus-5": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-8": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-7": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-6": (5, 6.25, 10, 0.50, 25),
    "claude-opus-4-5": (5, 6.25, 10, 0.50, 25),
    "claude-sonnet-5-5": (2, 2.5, 4, 0.10, 10),
    "claude-sonnet-5": (2, 2.5, 4, 0.20, 10),
    "claude-sonnet-4-6": (3, 3.75, 6, 0.30, 15),
    "claude-sonnet-4-5": (3, 3.75, 6, 0.30, 15),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5": (1, 1.25, 2, 0.10, 5),
}
LARGE_PROMPT = {"claude-haiku-5-5": (100_000, 5)}  # prompts over this many tokens cost this many times more

RED, YEL, GRN, CYN, DIM, BOLD, RST = (
    "\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[2m", "\033[1m", "\033[0m")

# ------------------------------------------------------------------ tips
TIPS = [
    "Pick your model before you start: /model mid-task re-reads the whole history uncached.",
    "Avoid the opusplan model setting: every plan-mode toggle is a model switch and a fresh cache.",
    "Don't connect or remove MCP servers mid-task. Edit MCP config, then restart between tasks.",
    "Want fast mode? Turn it on at the start of a session, not deep into a long one.",
    "Went down a dead end? /rewind is cache-safe; /compact rebuilds the cache.",
    "Run /compact at natural breaks between tasks, not mid-task. Cold-cache compaction costs the most.",
    "Just want a summary? /recap keeps the cache; /compact replaces your history.",
    "CLAUDE.md edits only load after /clear, /compact or a restart, so batch them.",
    "Block risky commands with scoped deny rules like Bash(rm *), not a bare tool name.",
    "A skill whose frontmatter sets a different model is a model switch for that turn.",
    "Lots of screenshots? Old images get dropped in batches and the history re-reads. Crop or share fewer.",
    "Upgrades apply on restart and the first turn after is uncached. DISABLE_AUTOUPDATER=1 lets you choose when.",
    "Parallel sessions in the same folder share a cache; different folders don't.",
    "Subagents get a 5-minute cache by default. Set subagentPromptCacheTtl to 1h if they idle.",
    "Plugin skills, commands and hooks are cache-safe. Plugins that add MCP servers are not.",
    "Changing permission mode or output style is cache-safe. Go ahead.",
    "Resuming? Use the same --append-system-prompt text, or the system prompt layer rebuilds.",
    "Check /usage: the 'Prompt cache (main)' line names the likely cause of your last miss.",
    "Name exact files in your prompt. Vague asks make Claude scan the repo.",
    "Routine edit? /effort low is cache-safe on Opus 5.5 and burns fewer tokens.",
    "Noisy search or log digging? Ask for a subagent; only its summary lands in your history.",
    "Run `cache_guard.py doctor` to find settings that silently break your cache.",
    "Run `cache_guard.py report` after a session to see which moments rebuilt the cache.",
    "Hooks that inject text on every tool call can break the cache. doctor flags them.",
]

CAUSE_TIPS = {
    "tools_changed": "Last miss: tool definitions changed. Leave MCP servers and plugins alone until a break.",
    "system_prompt_changed": "Last miss: system prompt changed. Keep --append-system-prompt identical on resume.",
    "ttl_expired_5m": "Last miss: idle over 5 min. Set promptCacheTtl to 1h if you pause often.",
    "ttl_expired_1h": "Last miss: idle over an hour. The first turn back is always a rebuild.",
    "likely_server_side": "Last miss looks server-side. Nothing you did, so carry on.",
}


# ------------------------------------------------------------------ helpers
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_PATH.read_text())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def read_stdin_json():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def state_path(session_id):
    safe = "".join(c for c in str(session_id or "default") if c.isalnum() or c in "-_")
    return STATE_DIR / f"{safe or 'default'}.json"


def load_state(session_id):
    try:
        return json.loads(state_path(session_id).read_text())
    except Exception:
        return {}


def save_state(session_id, data):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = state_path(session_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)  # a hook reading at the same moment sees the old file or the new one, never half
    except Exception:
        pass


def fmt_tokens(n):
    try:
        n = int(n)
    except Exception:
        return "?"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def fmt_usd(x):
    if x is None:
        return "?"
    return f"${x:,.0f}" if x >= 100 else f"${x:.2f}"


def fmt_signed_usd(x):
    s = fmt_usd(abs(x))
    return f"-{s}" if x < 0 and s != "$0.00" else s


def fmt_gap(seconds):
    if seconds >= 2 * 86400:
        return f"{seconds / 86400:.1f}d"
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{max(1, round(seconds / 60))}m"


def price_table(cfg):
    """Built-in list prices plus any overrides from config.json, longest model prefix first."""
    table = dict(PRICES)
    for k, v in (cfg.get("pricing") or {}).items():
        try:
            if len(v) == 5:
                table[str(k)] = tuple(float(x) for x in v)
        except Exception:
            pass
    return sorted(table.items(), key=lambda kv: -len(kv[0]))


def price_for(table, model, prompt_tokens=0):
    m = str(model or "")
    for prefix, p in table:
        if m.startswith(prefix):
            big = LARGE_PROMPT.get(prefix)
            return tuple(x * big[1] for x in p) if (big and prompt_tokens > big[0]) else p
    return None


def cold_cost(cfg, model, tokens, ttl):
    """List-price cost of re-caching `tokens` on a cold cache, and of reading them warm. (None, None) if unpriced."""
    try:
        p = price_for(price_table(cfg), model, int(tokens))
        if not p:
            return None, None
        return int(tokens) * (p[2] if ttl == "1h" else p[1]) / 1e6, int(tokens) * p[3] / 1e6
    except Exception:
        return None, None


def fmt_clock(epoch):
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")
    except Exception:
        return "?"


def cache_is_warm(state, now=None):
    now = now or time.time()
    exp = state.get("expires_at")
    if exp:
        try:
            return float(exp) > now
        except Exception:
            pass
    return bool(state.get("warm"))


def context_pct(data):
    cw = data.get("context_window") or {}
    pct = cw.get("used_percentage")
    if isinstance(pct, (int, float)):
        return float(pct)
    usage = cw.get("current_usage") or {}
    size = cw.get("context_window_size")
    if usage and size:
        used = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return 100.0 * used / float(size)
    return None


def limit_pct(data, window):
    w = ((data.get("rate_limits") or {}).get(window)) or {}
    pct = w.get("used_percentage")
    return (float(pct), w.get("resets_at")) if isinstance(pct, (int, float)) else (None, None)


def emit(obj):
    print(json.dumps(obj))


def notify(title, body):
    try:
        if sys.platform == "darwin":
            script = f'display notification {json.dumps(body)} with title {json.dumps(title)}'
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            ps = ("[reflection.assembly]::loadwithpartialname('System.Windows.Forms')|Out-Null;"
                  "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                  f"$n.Visible=$true;$n.ShowBalloonTip(8000,{json.dumps(title)},{json.dumps(body)},'Info');"
                  "Start-Sleep 9")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ------------------------------------------------------------------ status line
def cmd_statusline():
    data = read_stdin_json()
    cfg = load_config()
    now = time.time()
    sid = data.get("session_id")
    model = (data.get("model") or {}).get("display_name") or "?"
    pc = data.get("prompt_cache")
    state = load_state(sid)
    rotating = TIPS[int(now // 60) % len(TIPS)]

    fh, fh_reset = limit_pct(data, "five_hour")
    sd, sd_reset = limit_pct(data, "seven_day")
    limits = []
    for label, pct in (("5h", fh), ("7d", sd)):
        if pct is not None:
            colour = RED if pct >= 90 else YEL if pct >= cfg["limit_warn_pct"] else ""
            limits.append(f"{colour}{label} {pct:.0f}%{RST if colour else ''}")
    limits_txt = f" | {' '.join(limits)}" if limits else ""

    if data.get("transcript_path"):
        state["transcript_path"] = data["transcript_path"]

    if not pc:
        save_state(sid, state)
        print(f"{model} {DIM}| cache: waiting for first response{RST}{limits_txt}")
        print(f"{DIM}tip: {rotating}{RST}")
        return

    ratio = pc.get("hit_ratio")
    if isinstance(ratio, (int, float)):
        pct = f"{ratio * 100:.0f}%" if ratio <= 1 else f"{ratio:.0f}%"
    else:
        pct = "--"
    ttl = pc.get("ttl") or "?"
    misses = int(pc.get("misses") or 0)
    exp = pc.get("expires_at")
    recache = pc.get("recache_tokens_if_cold")
    warm = bool(pc.get("warm"))
    if exp:
        try:
            warm = float(exp) > now
        except Exception:
            pass
    mins_left = max(0, int((float(exp) - now) // 60)) if (warm and exp) else None

    if misses > int(state.get("misses_seen", 0)):
        state["miss_seen_at"] = now
    model_id = (data.get("model") or {}).get("id")
    state.update({"misses_seen": misses, "warm": warm, "expires_at": exp, "ttl": ttl,
                  "recache_tokens_if_cold": recache, "model": model, "model_id": model_id,
                  "caching_observed": bool(pc.get("caching_observed", True)), "updated": now})

    cost_txt = cost_block(cfg, state)

    # optional desktop alert, once per expiry time
    if (cfg.get("notify") and mins_left is not None and mins_left < cfg["notify_minutes"]
            and state.get("notified_for") != exp):
        state["notified_for"] = exp
        size = f" (~{fmt_tokens(recache)} tokens)" if recache else ""
        notify("Claude Code cache expiring",
               f"Cache expires in {mins_left + 1} min{size}. Reply now to keep it warm.")
    save_state(sid, state)

    if warm:
        left = f", {mins_left}m left" if mins_left is not None else ""
        colour = YEL if (mins_left is not None and mins_left < cfg["expiry_warn_minutes"]) else GRN
        cache_txt = f"{colour}warm{left}{RST}"
    else:
        cache_txt = f"{RED}{BOLD}COLD{RST}"
    line1 = f"{model} | cache {pct} ({cache_txt}, ttl {ttl}){cost_txt} | misses {misses}{limits_txt}"
    if not warm and recache:
        usd, _ = cold_cost(cfg, model_id, recache, ttl)
        line1 += f" | next msg re-reads ~{fmt_tokens(recache)}" + (f" (~{fmt_usd(usd)})" if usd else "")
    print(line1)

    # tip line, most urgent first
    causes = ((pc.get("last_miss_cause") or {}).get("causes")) or []
    recent_miss = now - float(state.get("miss_seen_at", 0)) < cfg["cause_tip_seconds"]
    cpct = context_pct(data)
    hot_limit = None
    for label, p, reset in (("5-hour", fh, fh_reset), ("7-day", sd, sd_reset)):
        if p is not None and p >= cfg["limit_warn_pct"]:
            hot_limit = (label, p, reset)
            break

    if not warm and pc.get("caching_observed", True):
        if int(now // 30) % 2 == 0:
            tip = (f"{RED}Cache is cold.{RST} Switching tasks? /clear is cheaper than "
                   f"continuing this long history or running /compact.")
        else:
            tip = (f"{YEL}Cold cache = free moment:{RST} change model, MCP servers or fast mode "
                   f"NOW, since you'll rebuild anyway.")
    elif mins_left is not None and mins_left < cfg["expiry_warn_minutes"]:
        tip = f"{YEL}Cache expires in {mins_left + 1}m.{RST} Reply soon or the next turn rebuilds it."
    elif hot_limit:
        label, p, reset = hot_limit
        when = f" (resets {fmt_clock(reset)})" if reset else ""
        tip = (f"{YEL}{label} limit at {p:.0f}%{when}.{RST} Avoid resumes, model switches "
               f"and cold /compact until then.")
    elif recent_miss and causes:
        c = causes[0]
        if c in CAUSE_TIPS:
            tip = CAUSE_TIPS[c]
        elif c.startswith("ttl_expired"):
            tip = "Last miss: you were idle past the cache TTL. The first turn back is a rebuild."
        elif "thinking" in c:
            tip = "Last miss: thinking settings changed. Set thinking at session start, not mid-task."
        else:
            tip = f"Last miss cause: {c}. Run /usage for details."
    elif cpct is not None and cpct >= cfg["context_warn_pct"]:
        tip = (f"{YEL}Context {cpct:.0f}% full.{RST} /compact at your next natural break, "
               f"before auto-compact fires mid-task.")
    else:
        tip = rotating
    print(f"{DIM}tip:{RST} {tip}")


# ------------------------------------------------------------------ hooks
def hook_model_switch():
    data = read_stdin_json()
    state = load_state(data.get("session_id"))
    to_model = data.get("to_model") or data.get("model") or "the new model"
    warm = data.get("prompt_cache_warm")  # newer Claude Code tells the hook directly
    if warm is None:
        warm = bool(state) and cache_is_warm(state)
    if not warm:
        return  # cold or unknown: the switch costs nothing extra
    est = data.get("context_tokens") or state.get("recache_tokens_if_cold")
    size = f"~{fmt_tokens(est)} tokens" if est else "your whole history"
    usd = data.get("estimated_cache_write_usd")
    if isinstance(usd, (int, float)) and usd > 0:
        size += f" (about {fmt_usd(usd)} at list price)"
    emit({
        "systemMessage": (f"cache-guard: your cache is warm. Switching to {to_model} re-reads "
                          f"{size} uncached on the next turn. Better at a break between tasks."),
        "hookSpecificOutput": {"hookEventName": "PreModelSwitch", "permissionDecision": "ask"},
    })


def hook_compact():
    data = read_stdin_json()
    cfg = load_config()
    if (data.get("trigger") or "") != "manual":
        return  # never interfere with auto-compaction
    sid = data.get("session_id")
    state = load_state(sid)
    if not state or cache_is_warm(state):
        return  # warm: compaction reads your prefix from cache, so it's fairly cheap
    now = time.time()
    window = int(cfg["compact_confirm_seconds"])
    if now - float(state.get("compact_confirm_at", 0)) < window:
        state.pop("compact_confirm_at", None)
        save_state(sid, state)
        return
    state["compact_confirm_at"] = now
    save_state(sid, state)
    est = state.get("recache_tokens_if_cold")
    size = f"~{fmt_tokens(est)} tokens" if est else "your full history"
    sys.stderr.write(
        f"cache-guard: cache is COLD, so /compact would re-read {size} uncached just to summarise. "
        f"Starting something new? /clear is cheaper. To compact anyway, run /compact again within "
        f"{max(1, window // 60)} minutes.\n")
    sys.exit(2)


def hook_prompt():
    """Stop a typed message into a big cold session once, and say what it will cost."""
    data = read_stdin_json()
    cfg = load_config()
    mode = str(cfg.get("cold_prompt", "block")).lower()
    source = data.get("source")
    if mode == "off" or source not in (None, "user"):
        return  # loops, scheduled tasks, SDK and system turns are never touched
    if str(data.get("prompt") or "").lstrip().startswith("/"):
        return  # /clear and /compact are the way out, so never stand in front of a command
    sid = data.get("session_id")
    state = load_state(sid)
    exp, est = state.get("expires_at"), state.get("recache_tokens_if_cold")
    if not state or not exp or not est or not state.get("caching_observed", True) or cache_is_warm(state):
        return
    try:
        if int(est) < int(cfg["cold_prompt_warn_tokens"]) or state.get("cold_ack_for") == exp:
            return
        ago = fmt_gap(max(0.0, time.time() - float(exp)))
    except Exception:
        return
    usd, warm_usd = cold_cost(cfg, state.get("model_id"), est, state.get("ttl"))
    price = f", about {fmt_usd(usd)} at API list price (about {fmt_usd(warm_usd)} if it were warm)" if usd else ""
    what = f"cache-guard: the cache went cold {ago} ago. This message re-reads ~{fmt_tokens(est)} tokens uncached{price}."
    now = time.time()
    window = int(cfg["cold_prompt_confirm_seconds"])
    asked = state.get("cold_confirm") or [None, 0]
    if mode != "block" or source != "user":
        # warn mode, or a Claude Code build that doesn't say who sent the prompt: tell, don't stop
        state["cold_ack_for"] = exp
        save_state(sid, state)
        emit({"systemMessage": f"{what} For a new task, /clear first is far cheaper."})
        return
    if asked[0] == exp and now - float(asked[1]) < window:
        state["cold_ack_for"] = exp  # confirmed: stay quiet until the cache has warmed and gone cold again
        state.pop("cold_confirm", None)
        save_state(sid, state)
        return
    state["cold_confirm"] = [exp, now]
    save_state(sid, state)
    emit({"decision": "block",
          "reason": (f"{what} New task? /clear first is far cheaper. Same task? Send it again within "
                     f"{max(1, window // 60)} minutes to go ahead.")})


def last_context_tokens(transcript_path):
    """Estimate prompt size from the last main-conversation usage record in a transcript."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - 400_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("isSidechain"):
                continue
            u = ((rec.get("message") or {}).get("usage")) or {}
            if u:
                return sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    except Exception:
        pass
    return None


def cleanup_old_files(cfg):
    cutoff = time.time() - float(cfg["trim"]["keep_logs_days"]) * 86400
    for d, age in ((LOG_DIR, cutoff), (STATE_DIR, time.time() - 14 * 86400)):
        try:
            for p in d.iterdir():
                if p.stat().st_mtime < age:
                    p.unlink()
        except Exception:
            pass


def hook_session_start():
    data = read_stdin_json()
    cfg = load_config()
    cleanup_old_files(cfg)
    if data.get("source") != "resume":
        return
    tokens = data.get("context_tokens")
    expired = data.get("prompt_cache_likely_expired")
    tpath = data.get("transcript_path")
    if tokens is None and tpath:
        tokens = last_context_tokens(tpath)
    if expired is None and tpath:
        try:  # fall back to transcript age vs a 1-hour TTL
            expired = time.time() - os.path.getmtime(tpath) > 3600
        except Exception:
            expired = None
    if not expired or not tokens or int(tokens) < int(cfg["resume_warn_tokens"]):
        return
    usd = data.get("estimated_cache_write_usd")
    price = f" (about {fmt_usd(usd)} at list price)" if isinstance(usd, (int, float)) and usd > 0 else ""
    emit({"systemMessage": (
        f"cache-guard: this resume re-sends ~{fmt_tokens(tokens)} tokens{price} and the cache has likely "
        f"expired, so your first message pays full price for all of it. If you're starting "
        f"something new, /clear (or a fresh session) is much cheaper.")})


ERROR_RE = re.compile(
    r"(error|fail|failed|failure|exception|traceback|panic|fatal|assert|warning|warn\b|"
    r"✗|✘|FAIL|ERR!|E\s{2,}|^\s+at\s)", re.IGNORECASE)


def trim_text(text, cfg, log_path):
    lines = text.splitlines()
    t = cfg["trim"]
    head, tail = int(t["head_lines"]), int(t["tail_lines"])
    if len(lines) <= head + tail:
        # few but very long lines: cut by characters instead
        keep = int(t["max_chars"]) // 2
        return (text[:keep] + f"\n\n[cache-guard: trimmed {len(text) - 2 * keep} chars. "
                f"Full output: {log_path}]\n\n" + text[-keep:])
    middle = lines[head:len(lines) - tail]
    errors, seen = [], set()
    for i, ln in enumerate(middle, start=head + 1):
        if ERROR_RE.search(ln) and ln.strip() not in seen:
            seen.add(ln.strip())
            errors.append(f"{i}: {ln[:400]}")
            if len(errors) >= int(t["max_error_lines"]):
                break
    parts = lines[:head]
    parts.append(f"\n[cache-guard: trimmed {len(middle)} of {len(lines)} lines to save tokens. "
                 f"Kept the first {head}, the last {tail}"
                 + (f", and {len(errors)} error/warning lines (numbered below)" if errors else "")
                 + f". Full output saved to {log_path}. To see more, grep that file "
                   f"rather than re-running the command.]")
    if errors:
        parts.append("--- matching lines from the trimmed middle ---")
        parts.extend(errors)
        parts.append("--- end of matching lines ---\n")
    parts.extend(lines[-tail:])
    return "\n".join(parts)


def hook_trim():
    data = read_stdin_json()
    cfg = load_config()
    if data.get("tool_name") not in (None, "Bash"):
        return
    resp = data.get("tool_response", data.get("tool_output"))
    if resp is None:
        return
    max_chars = int(cfg["trim"]["max_chars"])
    if isinstance(resp, str):
        fields = {"_text": resp}
    elif isinstance(resp, dict):
        fields = {k: v for k, v in resp.items() if k in ("stdout", "stderr") and isinstance(v, str)}
    else:
        return
    if sum(len(v) for v in fields.values()) <= max_chars:
        return
    cmd = ((data.get("tool_input") or {}).get("command") or "")
    if "no-trim" in cmd:
        return  # escape hatch: append `# no-trim` to a command to get full output
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sid = str(data.get("session_id") or "s")[:8]
    log_path = LOG_DIR / f"{stamp}-{sid}.log"
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"$ {cmd}\n")
            for k, v in fields.items():
                f.write(f"\n===== {k.strip('_')} =====\n{v}")
    except Exception:
        return  # never trim if we couldn't save the full output
    new = {k: (trim_text(v, cfg, log_path) if len(v) > max_chars // (2 if len(fields) > 1 else 1) else v)
           for k, v in fields.items()}
    if isinstance(resp, str):
        updated = new["_text"]
    else:
        updated = dict(resp)
        updated.update(new)
    emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated}})


# ------------------------------------------------------------------ doctor
def read_json(path):
    try:
        return json.loads(Path(path).read_text() or "{}")
    except Exception:
        return None


def cmd_doctor():
    cwd = Path.cwd()
    files = [SETTINGS, cwd / ".claude" / "settings.json", cwd / ".claude" / "settings.local.json"]
    merged_env, model, hooks_by_event, found = {}, None, {}, []
    ttl, sub_ttl, style, status_cmd = None, None, None, None
    for f in files:
        s = read_json(f) if f.exists() else None
        if s is None:
            if f.exists():
                print(f"{RED}!{RST} Could not parse {f}")
            continue
        found.append(str(f))
        merged_env.update({k: str(v) for k, v in (s.get("env") or {}).items()})
        model = s.get("model", model)
        ttl = s.get("promptCacheTtl", ttl)
        sub_ttl = s.get("subagentPromptCacheTtl", sub_ttl)
        style = s.get("outputStyle", style)
        status_cmd = ((s.get("statusLine") or {}).get("command")) or status_cmd
        for ev, groups in (s.get("hooks") or {}).items():
            for g in groups or []:
                for h in g.get("hooks", []):
                    hooks_by_event.setdefault(ev, []).append((h.get("command") or "", str(f)))
    env = dict(merged_env)
    for k, v in os.environ.items():
        if k.startswith(("DISABLE_PROMPT", "FORCE_PROMPT", "ENABLE_PROMPT", "CLAUDE_CODE_", "ANTHROPIC_",
                         "ENABLE_TOOL", "BASH_MAX", "MAX_MCP", "DISABLE_AUTO")):
            env.setdefault(k, v)

    warns, notes, oks = [], [], []
    print(f"{BOLD}cache-guard doctor{RST} v{VERSION}")
    print(f"{DIM}settings read: {', '.join(found) or 'none'}{RST}\n")

    # cache breakers
    if str(model or "").lower() == "opusplan":
        warns.append("model is 'opusplan': every plan-mode toggle switches model and starts a fresh cache.")
    for k in [k for k in env if k.startswith("DISABLE_PROMPT_CACHING")]:
        if env[k] not in ("0", "false", ""):
            warns.append(f"{k}={env[k]} turns prompt caching OFF. Every turn pays full price.")
    if env.get("FORCE_PROMPT_CACHING_5M") not in (None, "", "0", "false"):
        warns.append("FORCE_PROMPT_CACHING_5M is set: your main conversation loses the 1-hour cache.")
    if str(env.get("ENABLE_TOOL_SEARCH", "")).lower() in ("false", "0"):
        warns.append("ENABLE_TOOL_SEARCH is off: connecting MCP servers or deny rules mid-session will break the cache.")
    if env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") not in (None, "", "0", "false"):
        notes.append("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS is set: effort changes break the cache, "
                     "and a mid-conversation context block is sent uncached.")
    if env.get("ANTHROPIC_BASE_URL") and "api.anthropic.com" not in env["ANTHROPIC_BASE_URL"]:
        notes.append(f"ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']}: make sure the gateway forwards "
                     "cache_control markers and the anthropic-beta header, or caching silently stops.")

    # hooks that inject context
    for ev in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart"):
        for cmd, src in hooks_by_event.get(ev, []):
            if SCRIPT_NAME in cmd:
                continue
            injects = None
            for tok in re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', cmd):
                p = Path(os.path.expandvars(os.path.expanduser(next(t for t in tok if t))))
                if p.is_file() and p.stat().st_size < 500_000:
                    try:
                        injects = "additionalContext" in p.read_text(errors="ignore")
                        break
                    except Exception:
                        pass
            if "additionalContext" in cmd:
                injects = True
            short = cmd if len(cmd) < 70 else cmd[:67] + "..."
            if ev in ("PreToolUse", "PostToolUse") and injects:
                warns.append(f"{ev} hook returns additionalContext ({short}). Tool-level injected context "
                             "has been reported to break the cache every turn. Prefer systemMessage.")
            elif ev == "UserPromptSubmit" and injects:
                notes.append(f"UserPromptSubmit hook injects context every turn ({short}). It piles up "
                             "in history; keep it tiny or move it to CLAUDE.md.")
            elif ev in ("PreToolUse", "PostToolUse") and injects is None:
                notes.append(f"{ev} hook ({short}): couldn't read its script. Check it doesn't return additionalContext.")
    notes.append("Hooks from plugins aren't scanned. Run /hooks inside Claude Code to see them.")

    # context size
    for md in (CLAUDE_DIR / "CLAUDE.md", cwd / "CLAUDE.md", cwd / ".claude" / "CLAUDE.md", cwd / "CLAUDE.local.md"):
        if md.exists():
            tok = len(md.read_text(errors="ignore")) // 4
            msg = f"{md} is ~{fmt_tokens(tok)} tokens"
            if tok > 5000:
                warns.append(msg + ": loaded every session. Move rarely used parts into skills or path-scoped rules.")
            elif tok > 2000:
                notes.append(msg + ". Consider trimming or moving parts into skills.")
            else:
                oks.append(msg + ".")
    mcp_count = 0
    for p in (HOME / ".claude.json", cwd / ".mcp.json"):
        j = read_json(p) if p.exists() else None
        if isinstance(j, dict):
            mcp_count += len(j.get("mcpServers") or {})
            proj = ((j.get("projects") or {}).get(str(cwd)) or {})
            mcp_count += len(proj.get("mcpServers") or {})
    if mcp_count:
        notes.append(f"{mcp_count} MCP server(s) configured. Fine with tool search on; don't toggle them mid-task.")

    # token caps
    if "BASH_MAX_OUTPUT_LENGTH" not in env:
        notes.append("BASH_MAX_OUTPUT_LENGTH not set (default 30,000 chars). Try 15000 to cut noisy logs, "
                     "or install with --trim for smarter trimming.")
    if "MAX_MCP_OUTPUT_TOKENS" not in env:
        notes.append("MAX_MCP_OUTPUT_TOKENS not set (default cap 25,000). Lower it if your MCP tools are chatty.")

    # what's in place
    oks.append(f"promptCacheTtl: {ttl or 'default (1h on subscription, 5m on API key)'}; "
               f"subagentPromptCacheTtl: {sub_ttl or 'default (5m)'}.")
    if status_cmd and SCRIPT_NAME in status_cmd:
        oks.append("cache-guard status line installed.")
    else:
        notes.append("cache-guard status line not installed (run install).")
    installed = [ev for ev in ("PreModelSwitch", "PreCompact", "UserPromptSubmit", "SessionStart", "PostToolUse")
                 if any(SCRIPT_NAME in c for c, _ in hooks_by_event.get(ev, []))]
    (oks if installed else notes).append(f"cache-guard hooks: {', '.join(installed) or 'none (run install)'}.")
    oks.append(f"output style: {style or 'Default'}.")

    for sym, colour, items in (("!", RED, warns), ("i", YEL, notes), ("✓", GRN, oks)):
        for it in items:
            print(f"{colour}{sym}{RST} {it}")
    print(f"\n{len(warns)} problem(s), {len(notes)} suggestion(s).")


# ------------------------------------------------------------------ report
MISS_MIN_TOKENS = 2000       # same floor Claude Code uses for its own miss counter
MISS_MIN_FRACTION = 0.05     # ...and the same 5% shortfall
SIGNALS_RESTART = ("session_context", "skill_listing")
SIGNALS_TOOLS = ("deferred_tools_delta", "mcp_instructions_delta")
SIGNALS = SIGNALS_RESTART + SIGNALS_TOOLS
EXPECTED = ("start", "compaction")
KIND_LABELS = {
    "idle": "idle past the cache TTL",
    "model": "model switch",
    "restart": "session resumed or restarted",
    "tools": "tools or MCP servers changed (likely)",
    "unknown": "prefix changed, cause not in transcript",
    "compaction": "after compaction (expected)",
    "start": "session start (expected)",
}


def project_dir_for(cwd):
    return CLAUDE_DIR / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def find_transcript(arg):
    if arg:
        p = Path(os.path.expanduser(arg))
        if p.is_file():
            return p
        root = CLAUDE_DIR / "projects"
        hits = list(root.glob(f"*/{arg}.jsonl")) or list(root.glob(f"*/{arg}*.jsonl"))  # full id, or its start
        return max(hits, key=lambda p: p.stat().st_mtime) if hits else None
    d = project_dir_for(Path.cwd())
    cands = list(d.glob("*.jsonl")) if d.is_dir() else []
    if not cands:
        cands = list((CLAUDE_DIR / "projects").glob("*/*.jsonl"))
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def digest(data, size=5):
    return hashlib.blake2b(data, digest_size=size).hexdigest()


def key_digest(key):
    return digest(json.dumps(key).encode())


class RequestReader:
    """Turns transcript lines into one row per API response, in order. load_requests feeds it a whole
    file; the status line feeds it only the lines appended since its last run."""

    def __init__(self, sidechain=False):
        self.sidechain = sidechain
        self.reqs, self.keys, self.index = [], [], {}
        self.compact_pending, self.signals = False, set()
        self.folded = set()  # status line: digests of requests already added to its running totals
        self.stale = False   # status line: a line re-logged one of those, so the file must be read again

    def feed(self, line):
        if ('"usage"' not in line and '"compact_boundary"' not in line
                and '"isCompactSummary"' not in line and not any(s in line for s in SIGNALS)):
            return  # skip the bulk of the file without parsing it
        try:
            rec = json.loads(line)
        except Exception:
            return
        if not isinstance(rec, dict) or (rec.get("isSidechain") and not self.sidechain):
            return
        typ = rec.get("type")
        if (typ == "system" and rec.get("subtype") == "compact_boundary") or rec.get("isCompactSummary"):
            self.compact_pending = True
            return
        if typ == "attachment":
            kind = (rec.get("attachment") or {}).get("type")
            if kind in SIGNALS:
                self.signals.add(kind)
            return
        msg = rec.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if typ != "assistant" or not isinstance(u, dict) or msg.get("model") == "<synthetic>":
            return
        cc = u.get("cache_creation") or {}
        row = {
            "ts": parse_ts(rec.get("timestamp")), "model": msg.get("model") or "?",
            "inp": int(u.get("input_tokens") or 0),
            "write": int(u.get("cache_creation_input_tokens") or 0),
            "w5": int(cc.get("ephemeral_5m_input_tokens") or 0),
            "w1": int(cc.get("ephemeral_1h_input_tokens") or 0),
            "read": int(u.get("cache_read_input_tokens") or 0),
            "out": int(u.get("output_tokens") or 0),
            "after_compact": self.compact_pending, "signals": self.signals,
        }
        key = msg.get("id") or rec.get("requestId") or rec.get("uuid")
        if key in self.index:  # a streamed response is logged more than once; the last line has the full usage
            old = self.reqs[self.index[key]]
            row["after_compact"], row["signals"] = old["after_compact"], old["signals"]
            self.reqs[self.index[key]] = row
            return
        if self.folded and key_digest(key) in self.folded:
            self.stale = True
            return
        self.index[key] = len(self.reqs)
        self.reqs.append(row)
        self.keys.append(key)
        self.compact_pending, self.signals = False, set()


def load_requests(path, sidechain=False):
    """One row per API response in a transcript, in order. sidechain=True reads a subagent's own file."""
    reader = RequestReader(sidechain)
    try:
        f = open(path, encoding="utf-8", errors="ignore")
    except Exception:
        return reader.reqs
    with f:
        for line in f:
            reader.feed(line)
    return reader.reqs


def subagent_files(path):
    """Subagent and workflow transcripts live in a folder named after the session."""
    d = path.parent / path.stem
    try:
        return sorted(d.rglob("*.jsonl")) if d.is_dir() else []
    except Exception:
        return []


def agent_label(path):
    meta = read_json(path.with_name(path.stem + ".meta.json")) or {}
    kind, desc = meta.get("agentType"), meta.get("description")
    return " ".join(x for x in (kind, f'"{desc[:50]}"' if desc else None) if x) or path.stem


def req_cost(r, table):
    """Cost of one request at list price, split by part. None when the model has no price."""
    p = price_for(table, r["model"], r["inp"] + r["write"] + r["read"])
    if not p:
        return None
    w5, w1 = r["w5"], r["w1"]
    if w5 + w1 == 0:
        w5 = r["write"]  # older transcripts don't record the split; assume the cheaper 5m rate
    return {"read": r["read"] * p[3] / 1e6, "write": (w5 * p[1] + w1 * p[2]) / 1e6,
            "input": r["inp"] * p[0] / 1e6, "output": r["out"] * p[4] / 1e6}


def new_summary():
    return {"reqs": 0, "inp": 0, "write": 0, "w5": 0, "w1": 0, "read": 0, "out": 0, "unpriced": 0,
            "cost": {"read": 0.0, "write": 0.0, "input": 0.0, "output": 0.0}}


def add_summary(a, b):
    for k in ("reqs", "inp", "write", "w5", "w1", "read", "out", "unpriced"):
        a[k] += b[k]
    for k in a["cost"]:
        a["cost"][k] += b["cost"][k]
    return a


def summarise(reqs, table):
    s = new_summary()
    for r in reqs:
        s["reqs"] += 1
        for k in ("inp", "write", "w5", "w1", "read", "out"):
            s[k] += r[k]
        c = req_cost(r, table)
        if c is None:
            s["unpriced"] += 1
        else:
            for k in c:
                s["cost"][k] += c[k]
    return s


def total_in(s):
    return s["inp"] + s["write"] + s["read"]


def hit_pct(s):
    return 100.0 * s["read"] / total_in(s) if total_in(s) else 0.0


def total_cost(s):
    return sum(s["cost"].values())


def rebuild_step(prev, r, ttl):
    """find_rebuilds for one request: (the rebuild it is, or None; the cache TTL carried to the next one)."""
    if prev is None:
        return ({"req": r, "kind": "start", "tokens": r["write"], "cause": "session start"}
                if r["write"] else None), ttl
    ttl = 3600 if prev["w1"] else 300 if prev["w5"] else ttl
    if r["after_compact"]:
        return {"req": r, "kind": "compaction", "tokens": r["write"], "cause": "after compaction"}, ttl
    expected = prev["read"] + prev["write"]
    tokens = min(expected - r["read"], r["write"] + r["inp"])
    if tokens < max(MISS_MIN_TOKENS, MISS_MIN_FRACTION * expected):
        return None, ttl
    gap = (r["ts"] - prev["ts"]) if (r["ts"] and prev["ts"]) else None
    if r["model"] != prev["model"]:
        kind, cause = "model", f"model switch ({prev['model']} → {r['model']})"
    elif gap and ttl and gap > ttl:
        kind, cause = "idle", f"idle {fmt_gap(gap)} (cache expired, ttl {'1h' if ttl == 3600 else '5m'})"
    elif gap and not ttl and gap > 3600:
        kind, cause = "idle", f"idle {fmt_gap(gap)} (cache expired)"
    elif gap and not ttl and gap > 300:
        kind, cause = "idle", f"idle {fmt_gap(gap)} (expired if on 5m TTL)"
    elif r["signals"] & set(SIGNALS_RESTART):
        kind, cause = "restart", KIND_LABELS["restart"]
    elif r["signals"] & set(SIGNALS_TOOLS):
        kind, cause = "tools", KIND_LABELS["tools"]
    else:
        kind, cause = "unknown", "prefix changed (cause not in transcript; /usage may know)"
    return {"req": r, "kind": kind, "tokens": tokens, "cause": cause}, ttl


def rebuild_usd(x, table):
    r = x["req"]
    p = price_for(table, r["model"], r["inp"] + r["write"] + r["read"])
    return x["tokens"] * (p[2] if r["w1"] > r["w5"] else p[1]) / 1e6 if p else None


def find_rebuilds(reqs, table):
    """Requests that re-wrote cache they should have read. A big write alone is not a rebuild:
    the cached prefix has to shrink, the same test Claude Code's own miss counter uses."""
    out, ttl, prev = [], None, None
    for r in reqs:
        x, ttl = rebuild_step(prev, r, ttl)
        if x:
            out.append(x)
        prev = r
    for x in out:
        x["usd"] = rebuild_usd(x, table)
    return out


def subagent_ttl_whatif(runs, table):
    """Price the other subagent cache length against what these runs actually did."""
    w5 = w1 = 0
    dearer_writes = saved_rebuilds = cheaper_writes = added_rebuilds = 0.0
    for reqs in runs:
        for i, r in enumerate(reqs):
            p = price_for(table, r["model"], r["inp"] + r["write"] + r["read"])
            if not p:
                continue
            w5 += r["w5"]
            w1 += r["w1"]
            dearer_writes += r["w5"] * (p[2] - p[1]) / 1e6   # 5m -> 1h: every write costs the 1h rate
            cheaper_writes += r["w1"] * (p[2] - p[1]) / 1e6  # 1h -> 5m: every write costs the 5m rate
            prev = reqs[i - 1] if i else None
            if not prev or not r["ts"] or not prev["ts"] or r["model"] != prev["model"]:
                continue
            if not 300 < r["ts"] - prev["ts"] <= 3600:
                continue  # only a 5-60 minute pause is treated differently by the two lengths
            if r["w5"] >= r["w1"]:  # on 5m: what was re-written after the pause would have been a read on 1h
                expected = prev["read"] + prev["write"]
                tokens = min(expected - r["read"], r["write"])
                if tokens >= max(MISS_MIN_TOKENS, MISS_MIN_FRACTION * expected):
                    saved_rebuilds += tokens * (p[1] - p[3]) / 1e6
            else:                   # on 1h: what was read after the pause would have been re-written on 5m
                added_rebuilds += r["read"] * (p[1] - p[3]) / 1e6
    if not (w5 or w1):
        return None
    if w5 >= w1:
        return {"current": "5m", "other": "1h", "writes": dearer_writes, "rebuilds": saved_rebuilds,
                "net_saving": saved_rebuilds - dearer_writes}
    return {"current": "1h", "other": "5m", "writes": cheaper_writes, "rebuilds": added_rebuilds,
            "net_saving": cheaper_writes - added_rebuilds}


def print_ttl_whatif(runs, table, sub_cost):
    w = subagent_ttl_whatif(runs, table)
    if not w:
        return
    print(f"\n{BOLD}Subagent cache length{RST} {DIM}(measured on {len(runs)} subagent runs, API list prices){RST}")
    if w["current"] == "5m":
        print(f"  Now 5m. A 1h cache would add {fmt_usd(w['writes'])} in dearer writes and save "
              f"{fmt_usd(w['rebuilds'])} in rebuilds after 5-60 minute pauses.")
    else:
        print(f"  Now 1h. A 5m cache would save {fmt_usd(w['writes'])} in cheaper writes and add "
              f"{fmt_usd(w['rebuilds'])} in rebuilds after 5-60 minute pauses.")
    net, floor = w["net_saving"], 0.02 * sub_cost
    pct = f", {100 * abs(net) / sub_cost:.0f}% of subagent spend" if sub_cost else ""
    if net > floor:
        print(f"  {GRN}Verdict: switch to {w['other']}{RST} (saves about {fmt_usd(net)}{pct}). "
              f'Set "subagentPromptCacheTtl": "{w["other"]}" in ~/.claude/settings.json.')
    elif net < -floor:
        print(f"  {GRN}Verdict: keep {w['current']}{RST} ({w['other']} would cost about {fmt_usd(-net)} more{pct}).")
    else:
        print(f"  {GRN}Verdict: keep {w['current']}{RST} (the difference is under 2% of subagent spend).")
    print(f"  {DIM}Not counted: subagents that start cold. The transcript can't show whether a longer "
          f"cache would have warmed them.{RST}")


def print_totals(s, indent=""):
    print(f"{indent}Input tokens   {fmt_tokens(total_in(s)):>7}  (read from cache {fmt_tokens(s['read'])}, "
          f"written {fmt_tokens(s['write'])}, uncached {fmt_tokens(s['inp'])})")
    print(f"{indent}Output tokens  {fmt_tokens(s['out']):>7}")
    hit = hit_pct(s)
    colour = GRN if hit >= 85 else YEL if hit >= 70 else RED
    print(f"{indent}Cache hit      {colour}{hit:>6.1f}%{RST}")
    if s["unpriced"] < s["reqs"]:
        c = s["cost"]
        extra = f"; {s['unpriced']} requests on unpriced models left out" if s["unpriced"] else ""
        print(f"{indent}Cost           {fmt_usd(total_cost(s)):>7}  at API list price (reads {fmt_usd(c['read'])}, "
              f"writes {fmt_usd(c['write'])}, output {fmt_usd(c['output'])}{extra})")


def usd_note(items):
    priced = [x["usd"] for x in items if x["usd"] is not None]
    return f", about {fmt_usd(sum(priced))}" if priced else ""


def report_session(arg):
    path = find_transcript(arg)
    if not path:
        sys.exit("No transcript found. Pass a session id or a .jsonl path.")
    table = price_table(load_config())
    reqs = load_requests(path)
    if not reqs:
        sys.exit(f"No usage records in {path}.")
    s = summarise(reqs, table)
    print(f"{BOLD}cache-guard report{RST}  {path.name}")
    start, end = reqs[0]["ts"], reqs[-1]["ts"]
    clock = "%H:%M"
    if start and end:
        if datetime.fromtimestamp(start).date() != datetime.fromtimestamp(end).date():
            clock = "%m-%d %H:%M"  # a session that spans days needs the date on every time
        end_txt = datetime.fromtimestamp(end).strftime("%H:%M" if clock == "%H:%M" else "%Y-%m-%d %H:%M")
        print(f"{DIM}{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} → {end_txt}, "
              f"{len(reqs)} requests, models: {', '.join(sorted({r['model'] for r in reqs}))}{RST}\n")
    print_totals(s)

    rebuilds = find_rebuilds(reqs, table)
    misses = [x for x in rebuilds if x["kind"] not in EXPECTED]
    head = f"{fmt_tokens(sum(x['tokens'] for x in misses))} tokens re-cached{usd_note(misses)}"
    print(f"\n{BOLD}Cache misses:{RST} {len(misses)}" + (f" ({head})" if misses else ""))
    for x in sorted(misses, key=lambda x: -x["tokens"])[:8]:
        when = datetime.fromtimestamp(x["req"]["ts"]).strftime(clock) if x["req"]["ts"] else "?"
        usd = fmt_usd(x["usd"]) if x["usd"] is not None else ""
        print(f"  {when}  {fmt_tokens(x['tokens']):>6} re-cached  {usd:>7}  {YEL}{x['cause']}{RST}")
    if not misses:
        print(f"  {GRN}None. Nice.{RST}")
    expected = []
    for kind, label in (("start", "session start"), ("compaction", "after compaction")):
        hits = [x for x in rebuilds if x["kind"] == kind]
        if hits:
            times = f" ({len(hits)}×)" if len(hits) > 1 else ""
            expected.append(f"{label} {fmt_tokens(sum(x['tokens'] for x in hits))}{times}")
    if expected:
        print(f"{DIM}Expected cache writes: {'; '.join(expected)}.{RST}")

    runs = [(f, load_requests(f, sidechain=True)) for f in subagent_files(path)]
    runs = [(f, rr) for f, rr in runs if rr]
    if runs:
        sub = new_summary()
        per_run = []
        for f, rr in runs:
            one = summarise(rr, table)
            add_summary(sub, one)
            per_run.append((total_cost(one), total_in(one), f))
        both = total_cost(s) + total_cost(sub)
        share = f", {100 * total_cost(sub) / both:.0f}% of this session's cost" if both else ""
        print(f"\n{BOLD}Subagents:{RST} {len(runs)} runs, {sub['reqs']} requests{share}")
        print_totals(sub, "  ")
        print(f"  Cache writes   5m {fmt_tokens(sub['w5'])}, 1h {fmt_tokens(sub['w1'])}")
        for cost, tokens, f in sorted(per_run, key=lambda x: (-x[0], -x[1]))[:3]:
            size = fmt_usd(cost) if cost else f"{fmt_tokens(tokens)} tokens"
            print(f"  {DIM}{size:>8}  {agent_label(f)}{RST}")
    print(f"\n{DIM}A miss is a request whose cached prefix shrank; a big write on its own is not one. Causes "
          f"are inferred from timing, model and transcript markers. /usage shows Claude Code's own diagnosis.{RST}")


def report_all(days):
    cutoff = time.time() - days * 86400
    root = CLAUDE_DIR / "projects"
    files = [p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff] if root.is_dir() else []
    if not files:
        sys.exit(f"No transcripts from the last {days:g} days under {root}.")
    table = price_table(load_config())
    main, sub, runs, by_kind, sessions = new_summary(), new_summary(), [], {}, []
    for p in files:
        reqs = load_requests(p)
        add_summary(main, summarise(reqs, table))
        avoid_tokens, avoid_usd, kinds = 0, 0.0, {}
        for x in find_rebuilds(reqs, table):
            row = by_kind.setdefault(x["kind"], [0, 0, 0.0])
            row[0] += 1
            row[1] += x["tokens"]
            row[2] += x["usd"] or 0.0
            if x["kind"] not in EXPECTED:
                avoid_tokens += x["tokens"]
                avoid_usd += x["usd"] or 0.0
                kinds[x["kind"]] = kinds.get(x["kind"], 0) + 1
        if avoid_tokens:
            sessions.append((avoid_usd, avoid_tokens, p, kinds, reqs[0]["ts"]))
        for f in subagent_files(p):
            rr = load_requests(f, sidechain=True)
            if rr:
                runs.append(rr)
                add_summary(sub, summarise(rr, table))

    print(f"{BOLD}cache-guard report{RST}  last {days:g} days: {len(files)} sessions, {len(runs)} subagent runs")
    print(f"{DIM}Costs are API list prices. On a subscription they show relative size, not a bill.{RST}\n")
    both = total_cost(main) + total_cost(sub)
    print(f"  {'':24} {'tokens in':>9}  {'cache hit':>9}  {'cost':>9}")
    for label, s in (("Main conversations", main), ("Subagents and workflows", sub)):
        share = f"  ({100 * total_cost(s) / both:.0f}%)" if both else ""
        print(f"  {label:24} {fmt_tokens(total_in(s)):>9}  {hit_pct(s):>8.1f}%  {fmt_usd(total_cost(s)):>9}{share}")
    unpriced = main["unpriced"] + sub["unpriced"]
    if unpriced:
        print(f"  {DIM}{unpriced} requests on unpriced models are left out of the cost column.{RST}")

    print(f"\n{BOLD}Main-conversation cache rebuilds by cause{RST}")
    main_cost = total_cost(main)
    for kind, (n, tokens, usd) in sorted(by_kind.items(), key=lambda kv: (kv[0] in EXPECTED, -kv[1][1])):
        share = f"  {100 * usd / main_cost:>4.1f}% of main cost" if main_cost else ""
        mark = DIM if kind in EXPECTED else YEL
        print(f"  {mark}{KIND_LABELS[kind]:42}{RST} {n:>5}×  {fmt_tokens(tokens):>6}  {fmt_usd(usd):>8}{share}")
    if not by_kind:
        print(f"  {GRN}None.{RST}")

    if sessions:
        print(f"\n{BOLD}Sessions with the most avoidable rebuilds{RST}")
        for usd, tokens, p, kinds, ts in sorted(sessions, key=lambda x: (-x[0], -x[1]))[:5]:
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
            why = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
            print(f"  {day}  {p.stem[:8]}  {fmt_tokens(tokens):>6}  {fmt_usd(usd):>8}  {DIM}{why}{RST}")
        print(f"  {DIM}Details: cache_guard.py report <session id>{RST}")

    print_ttl_whatif(runs, table, total_cost(sub))


def cmd_report(args):
    args = list(args)
    days = 30.0
    if "--days" in args:
        i = args.index("--days")
        try:
            days = float(args[i + 1])
        except Exception:
            sys.exit("--days needs a number, for example: report --all --days 7")
        del args[i:i + 2]
    if "--all" in args:
        report_all(days)
    else:
        report_session(args[0] if args else None)


# ------------------------------------------------------------------ session cost (status line)
COST_BUDGET_SECONDS = 0.5   # most time one status-line run spends reading transcripts; the rest waits for the next
COST_READ_BLOCK = 4 << 20   # bytes read at a time
COST_TAIL = 8               # newest requests kept open, because a streamed response is logged again under its id
HEAD_BYTES = 4096           # start of a transcript remembered, to notice the file was replaced
NEWLINES = re.compile(r"\r\n|\r|\n")


def cache_saving(r, table):
    """What caching saved on one request at list price: its cache reads at the plain input price less what they
    cost, minus the premium its cache writes paid over the input price. None when the model has no price."""
    p = price_for(table, r["model"], r["inp"] + r["write"] + r["read"])
    if not p:
        return None
    w5, w1 = r["w5"], r["w1"]
    if w5 + w1 == 0:
        w5 = r["write"]  # as req_cost: no split recorded means the 5m rate
    return (r["read"] * (p[0] - p[3]) - w5 * (p[1] - p[0]) - w1 * (p[2] - p[0])) / 1e6


def new_tally():
    return {"reqs": 0, "unpriced": 0, "cost": {"read": 0.0, "write": 0.0, "input": 0.0, "output": 0.0},
            "saved": 0.0, "avoidable": 0.0, "prev": None, "ttl": None}


def tally_add(t, r, table, main):
    """Add one request to running totals, with the same arithmetic, in the same order, as summarise and find_rebuilds."""
    t["reqs"] += 1
    c = req_cost(r, table)
    if c is None:
        t["unpriced"] += 1
    else:
        for k in c:
            t["cost"][k] += c[k]
        t["saved"] += cache_saving(r, table)
    if main:  # rebuilds are judged in the main conversation only, as report lists them
        x, t["ttl"] = rebuild_step(t["prev"], r, t["ttl"])
        if x and x["kind"] not in EXPECTED:
            usd = rebuild_usd(x, table)
            if usd is not None:
                t["avoidable"] += usd
        t["prev"] = r


def row_out(r):
    return dict(r, signals=sorted(r["signals"])) if r else None


def row_in(d):
    return dict(d, signals=set(d["signals"])) if d else None


def new_track():
    return {"off": 0, "ino": None, "stamp": None, "hl": 0, "head": digest(b"", 16), "rows": [], "keys": [],
            "cp": False, "sig": [], "folded": "", "tally": new_tally(), "fig": None}


def text_lines(data):
    """Bytes to lines split as a text-mode file splits them, so this reader and load_requests see the same lines."""
    text = data.decode("utf-8", "ignore")
    return NEWLINES.split(text) if "\r" in text else text.split("\n")


def track_continues(st, path, s):
    """True when this is the file read last time, with lines only appended to it since."""
    if st.get("ino") != s.st_ino or s.st_size < st["off"]:
        return False  # replaced, or shrank
    if st["stamp"] and s.st_size == st["stamp"][1]:
        return False  # same size but touched since: rewritten in place
    if not st["off"]:
        return True
    with open(path, "rb") as f:
        head = f.read(st["hl"])
        f.seek(st["off"] - 1)
        return f.read(1) == b"\n" and digest(head, 16) == st["head"]


def read_appended(path, off, reader, deadline, ran):
    """Feed the reader every complete line after byte `off` until end of file or the time budget. Returns the offset
    after the last complete line and the bytes after it at end of file, or None for those when time ran out first.
    The first block of a run is always read, so a run out of time still makes progress."""
    with open(path, "rb") as f:
        f.seek(off)
        partial = []
        while not reader.stale:
            if ran[0] and time.monotonic() > deadline:
                return off, None
            block = f.read(COST_READ_BLOCK)
            if not block:
                return off, b"".join(partial)
            cut = block.rfind(b"\n") + 1
            if not cut:
                partial.append(block)  # a line longer than a block
                continue
            partial.append(block[:cut])
            data, partial = b"".join(partial), [block[cut:]]
            for line in text_lines(data):
                reader.feed(line)
            off += len(data)
            ran[0] = True
    return off, None


def advance_track(st, path, main, table, deadline, ran):
    """Bring one transcript's running totals up to date. Returns (its state, whether it reached the end)."""
    try:
        s = os.stat(path)
    except OSError:
        return None, True  # not there (yet): nothing to count
    stamp = [s.st_ino, s.st_size, s.st_mtime_ns]
    if st and st["stamp"] == stamp:
        return st, True  # unchanged since the last run
    if not (st and track_continues(st, path, s)):
        st = new_track()
    while True:
        reader = RequestReader(sidechain=not main)
        reader.reqs = [row_in(x) for x in st["rows"]]
        reader.keys = list(st["keys"])
        reader.index = {k: i for i, k in enumerate(reader.keys)}
        reader.compact_pending, reader.signals = bool(st["cp"]), set(st["sig"])
        folded = st["folded"]
        reader.folded = {folded[i:i + 10] for i in range(0, len(folded), 10)}
        tally = dict(st["tally"], cost=dict(st["tally"]["cost"]), prev=row_in(st["tally"]["prev"]))
        off, rest = read_appended(path, st["off"], reader, deadline, ran)
        if reader.stale:  # a request already counted was logged again: count this file from the start
            st = new_track()
            continue
        # all but the newest requests join the running totals; those stay open to a line that re-logs them
        n = max(0, len(reader.reqs) - COST_TAIL)
        done_keys = [key_digest(k) for k in reader.keys[:n]]
        for r in reader.reqs[:n]:
            tally_add(tally, r, table, main)
        reader.reqs, reader.keys = reader.reqs[n:], reader.keys[n:]
        reader.index = {k: i for i, k in enumerate(reader.keys)}
        reader.folded.update(done_keys)
        hl, head = st["hl"], st["head"]
        if hl < HEAD_BYTES and off > hl:
            hl = min(off, HEAD_BYTES)
            with open(path, "rb") as f:
                head = digest(f.read(hl), 16)
        kept = {"off": off, "ino": s.st_ino, "stamp": None, "hl": hl, "head": head,
                "rows": [row_out(r) for r in reader.reqs], "keys": list(reader.keys),
                "cp": reader.compact_pending, "sig": sorted(reader.signals), "folded": folded + "".join(done_keys),
                "tally": dict(tally, prev=row_out(tally["prev"])), "fig": None}
        if rest is None:
            return kept, False  # out of time: carry on from `off` next run
        for line in text_lines(rest) if rest else ():
            reader.feed(line)  # a last line still without its newline counts now, and is read again next run
        if reader.stale:
            st = new_track()
            continue
        fig = dict(tally, cost=dict(tally["cost"]))
        for r in reader.reqs:
            tally_add(fig, r, table, main)
        kept["fig"] = {k: fig[k] for k in ("reqs", "unpriced", "cost", "saved", "avoidable")}
        kept["stamp"] = stamp
        return kept, True


def combine_tracks(main, subs):
    """Session figures from the per-file totals, added up in the order report adds them. None when nothing is priced."""
    figs = [t["fig"] for t in [main] + subs if t]
    if not sum(f["reqs"] - f["unpriced"] for f in figs):
        return None
    sub = {"read": 0.0, "write": 0.0, "input": 0.0, "output": 0.0}
    for t in subs:
        if t:
            for k in sub:
                sub[k] += t["fig"]["cost"][k]
    m = main["fig"] if main else new_tally()
    return {"cost": sum(m["cost"].values()) + sum(sub.values()), "avoidable": m["avoidable"],
            "saved": m["saved"] + sum(t["fig"]["saved"] for t in subs if t)}


def session_costs(state, path, cfg):
    """Cost, avoidable cost and cache savings of a session at list price, kept as running totals in its state and
    brought up to date from what its transcripts gained since the last run. Returns the last complete figures, so a
    run that runs out of time shows those; None before the first complete run, or when nothing can be priced."""
    deadline = time.monotonic() + COST_BUDGET_SECONDS
    table = price_table(cfg)
    prices = digest(json.dumps([VERSION, table, sorted(LARGE_PROMPT.items())]).encode(), 16)
    cs = state.get("costs")
    if not (isinstance(cs, dict) and cs.get("path") == str(path) and cs.get("prices") == prices):
        cs = state["costs"] = {"path": str(path), "prices": prices, "main": None, "subs": {}, "shown": None}
    ran = [False]
    cs["main"], done = advance_track(cs["main"], path, True, table, deadline, ran)
    base, old, subs = path.parent / path.stem, cs["subs"], []
    cs["subs"] = {}
    for f in subagent_files(path):
        rel = str(f.relative_to(base))
        st, ok = advance_track(old.get(rel), f, False, table, deadline, ran)
        done = done and ok
        if st:
            cs["subs"][rel] = st
        subs.append(st)
    if done:
        cs["shown"] = combine_tracks(cs["main"], subs)
    return cs["shown"]


def cost_block(cfg, state):
    """' | cost $x · avoidable $y · saved $z' for the first status line, or '' when it is turned off, nothing can be
    priced yet, or anything at all goes wrong: the status line must always print."""
    on = cfg.get("statusline_cost", True)
    if on is False or str(on).strip().lower() in ("false", "0", "off", "no", "none"):
        state.pop("costs", None)
        return ""
    try:
        tp = state.get("transcript_path")
        figs = session_costs(state, Path(tp), cfg) if tp else None
    except Exception:
        state.pop("costs", None)  # start afresh next run
        return ""
    try:
        if not figs:
            return ""
        cost, avoid = float(figs["cost"]), float(figs["avoidable"])
        parts = [f"cost {fmt_usd(cost)}"]
        if fmt_usd(avoid) != "$0.00":
            try:
                warn = float(cfg.get("avoidable_warn_pct", 15))
            except Exception:
                warn = 15.0
            txt = f"avoidable {fmt_usd(avoid)}"
            parts.append(f"{YEL}{txt}{RST}" if cost > 0 and 100 * avoid / cost >= warn else txt)
        parts.append(f"saved {fmt_signed_usd(float(figs['saved']))}")
        return " | " + " · ".join(parts)
    except Exception:
        return ""


# ------------------------------------------------------------------ install
def cmd_install(flags):
    want_all = "--all" in flags
    existing = read_json(SETTINGS) if SETTINGS.exists() else {}
    existing = existing or {}
    had_trim = any(SCRIPT_NAME in (h.get("command") or "")
                   for g in ((existing.get("hooks") or {}).get("PostToolUse") or [])
                   for h in g.get("hooks", []))
    # re-running install keeps the trimmer if it was on, unless you pass --no-trim
    trim = (want_all or "--trim" in flags or had_trim) and "--no-trim" not in flags
    want_notify = want_all or "--notify" in flags
    lean = want_all or "--lean" in flags

    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    src_dir = Path(__file__).resolve().parent
    target = GUARD_DIR / SCRIPT_NAME
    if Path(__file__).resolve() != target.resolve():
        shutil.copy2(Path(__file__).resolve(), target)
    target.chmod(0o755)

    style_src = src_dir / "lean.md"
    if style_src.exists():
        STYLE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(style_src, STYLE_DIR / "lean.md")
        shutil.copy2(style_src, GUARD_DIR / "lean.md")

    cfg = read_json(CONFIG_PATH) if CONFIG_PATH.exists() else {}
    cfg = cfg or {}
    if want_notify:
        cfg["notify"] = True
    if "--no-notify" in flags:
        cfg["notify"] = False
    CONFIG_PATH.write_text(json.dumps({**{k: v for k, v in DEFAULT_CONFIG.items()}, **cfg}, indent=2) + "\n")

    py = "python3" if os.name != "nt" else "python"
    cmd = f'{py} "{target}"'

    settings = {}
    if SETTINGS.exists():
        settings = read_json(SETTINGS)
        if settings is None:
            sys.exit(f"Could not parse {SETTINGS}. Fix it and re-run; nothing was changed.")
        backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"Backed up settings to {backup}")
    else:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    old_sl = settings.get("statusLine")
    settings["statusLine"] = {"type": "command", "command": f"{cmd} statusline", "refreshInterval": 30}
    if old_sl and SCRIPT_NAME not in (old_sl.get("command") or ""):
        print(f"Replaced your previous statusLine ({old_sl.get('command')}). It's in the backup.")

    hooks = settings.setdefault("hooks", {})

    def set_hook(event, matcher, command):
        groups = hooks.setdefault(event, [])
        for g in groups:
            for h in g.get("hooks", []):
                if SCRIPT_NAME in (h.get("command") or ""):
                    h["command"] = command
                    g["matcher"] = matcher
                    return
        groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})

    def drop_hook(event):
        groups = [g for g in hooks.get(event, []) if not any(
            SCRIPT_NAME in (h.get("command") or "") for h in g.get("hooks", []))]
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)

    # clean up v1 command names, then wire v2
    set_hook("PreModelSwitch", "", f"{cmd} hook-model-switch")
    set_hook("PreCompact", "manual", f"{cmd} hook-compact")
    set_hook("UserPromptSubmit", "", f"{cmd} hook-prompt")
    set_hook("SessionStart", "", f"{cmd} hook-session-start")
    if trim:
        set_hook("PostToolUse", "Bash", f"{cmd} hook-trim")
    else:
        drop_hook("PostToolUse")
    if lean:
        settings["outputStyle"] = "Lean"

    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Installed cache-guard v{VERSION} to {GUARD_DIR}")
    print("  ✓ status line (refreshes every 30s)")
    print("  ✓ hooks: model-switch guard, cold /compact guard, cold-message guard, resume warning")
    print(f"  {'✓' if trim else '·'} Bash output trimmer {'' if trim else '(off; add --trim)'}")
    print(f"  {'✓' if want_notify or cfg.get('notify') else '·'} expiry notifications "
          f"{'' if want_notify or cfg.get('notify') else '(off; add --notify)'}")
    print(f"  ✓ Lean output style saved to {STYLE_DIR / 'lean.md'}"
          + (" and set as default" if lean else " (pick it in /config, or add --lean)"))
    print(f"Settings: {SETTINGS}   Config: {CONFIG_PATH}")
    print("Restart Claude Code to load it. Then run:  python3 ~/.claude/cache-guard/cache_guard.py doctor")


def cmd_uninstall():
    settings = read_json(SETTINGS) if SETTINGS.exists() else None
    if not settings:
        print("Nothing to remove.")
        return
    backup = SETTINGS.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(SETTINGS, backup)
    if SCRIPT_NAME in ((settings.get("statusLine") or {}).get("command") or ""):
        settings.pop("statusLine")
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = [g for g in hooks[event] or [] if not any(
            SCRIPT_NAME in (h.get("command") or "") for h in g.get("hooks", []))]
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)
    if settings.get("outputStyle") == "Lean":
        settings.pop("outputStyle")
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Removed cache-guard from {SETTINGS} (backup: {backup}).")
    print(f"To delete the files too: rm -rf {GUARD_DIR} {STYLE_DIR / 'lean.md'}")


# ------------------------------------------------------------------ main
def main():
    jobs = {
        "statusline": cmd_statusline,
        "doctor": cmd_doctor,
        "report": lambda: cmd_report(sys.argv[2:]),
        "install": lambda: cmd_install(sys.argv[2:]),
        "uninstall": cmd_uninstall,
        "hook-model-switch": hook_model_switch,
        "hook-compact": hook_compact,
        "hook-prompt": hook_prompt,
        "hook-session-start": hook_session_start,
        "hook-trim": hook_trim,
        # v1 names, kept so an old settings.json keeps working until you re-run install
        "pre-model-switch": hook_model_switch,
        "pre-compact": hook_compact,
        "--version": lambda: print(VERSION),
    }
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job not in jobs:
        print(__doc__)
        sys.exit(0 if job in ("", "-h", "--help", "help") else 1)
    try:
        jobs[job]()
    except SystemExit:
        raise
    except Exception as e:  # a crashing hook must never block Claude Code
        if job.startswith(("hook-", "pre-", "statusline")):
            sys.stderr.write(f"cache-guard: {job} error: {e}\n")
            sys.exit(0)
        raise


if __name__ == "__main__":
    main()
