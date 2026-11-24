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
                        --all      --notify and --lean (never the trimmer: ask for that with --trim)
                        --keepwarm keep a long session's cache warm while you're away (wakes Claude briefly)
                        --no-trim / --no-notify / --no-keepwarm   turn those back off
  uninstall           Remove cache-guard from settings.json.

Hook entry points (wired by install; you never call these yourself)
  hook-model-switch   PreModelSwitch: ask before a switch that throws away a warm cache.
  hook-compact        PreCompact: a manual /compact on a COLD cache must be repeated to run.
  hook-prompt         UserPromptSubmit: a typed message into a big COLD session gets a warning with
                      what it will cost (stopped once instead with cold_prompt=block). Automated
                      prompts are never touched.
  hook-session-start  SessionStart: warn when a resume re-sends a big, expired history.
  hook-trim           PostToolUse (Bash): trim huge output, keep errors, save the full log.
  hook-keepwarm       Stop, in the background (opt-in): while you're away, wake Claude for a one-word reply just
                      before the cache expires, as long as that costs less than a rebuild.

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

VERSION = "2.2.0"
HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
GUARD_DIR = CLAUDE_DIR / "cache-guard"
STATE_DIR = GUARD_DIR / "state"
LOG_DIR = GUARD_DIR / "logs"
CONFIG_PATH = GUARD_DIR / "config.json"
PACE_PATH = GUARD_DIR / "pace.json"                  # recent usage-limit readings, for the pace forecast
TOTALS_PATH = GUARD_DIR / "totals.json"              # all-projects figures the status line shows
TOTALS_FILES_PATH = GUARD_DIR / "totals-files.json"  # per-transcript figures, so a scan re-reads only what changed
TOTALS_LOCK = GUARD_DIR / "totals.lock"              # held while a background scan runs
SETTINGS = CLAUDE_DIR / "settings.json"
STYLE_DIR = CLAUDE_DIR / "output-styles"
SCRIPT_NAME = "cache_guard.py"

DEFAULT_CONFIG = {
    "notify": False,             # desktop alert when a warm cache is about to expire
    "notify_minutes": 3,
    "expiry_warn_minutes": 3,    # status-line warning threshold
    "context_warn_pct": 75,      # suggest /compact at a break above this
    "limit_warn_pct": 80,        # 5-hour / 7-day usage warning threshold
    "keepwarm": False,           # keep the cache warm while you're away (install --keepwarm wires the hook)
    "keepwarm_lead_minutes": 5,  # on a 1h cache, ping this long before it expires (a 5m cache: 1 minute)
    "keepwarm_max_hours": 12,    # stop keeping a session warm after this long away
    "pace_forecast": True,       # say when the recent pace uses a limit up before it resets
    "pace_window_minutes": 30,   # how far back the pace is measured
    "resume_warn_tokens": 50000, # warn on resume when re-sending more than this, uncached
    "compact_confirm_seconds": 120,
    "cold_prompt": "warn",       # warn = only tell you; block = stop a cold message once; off
    "cold_prompt_warn_tokens": 100000,  # only when the message would re-read at least this much
    "cold_prompt_confirm_seconds": 120,
    "cold_prompt_warn_usd": 0.5,  # on a priced model, act when the re-read costs at least this (0 = token floor only)
    "session_budget_usd": 0,     # warn when a session's list-price cost passes this, and each multiple (0 = off)
    "session_budget_action": "warn",  # warn = only tell you; block = stop the message once
    "session_budget_confirm_seconds": 120,
    "pricing": {},               # override or add list prices: {"model-id-prefix": [in, 5m, 1h, read, out]}
    "statusline_cost": True,     # show session cost, avoidable cost and cache savings on the status line
    "avoidable_warn_pct": 15,    # colour the avoidable figure from this share of the session's cost
    "statusline_totals": True,   # show the all-projects row on the status line
    "totals_days": 30,           # window for the all-projects row, as report --all --days
    "totals_refresh_minutes": 10,  # how old the all-projects figures may get before a background scan
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
    "Stepping away from a long session? install --keepwarm keeps its cache warm for less than one rebuild.",
    "cache cut $A → $B: $A is what re-sending your history every turn would cost at full price. "
    "The cache bills those repeats at about 5%.",
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


def fmt_compact_usd(x):
    return f"${x / 1000:.1f}k" if x >= 1000 else fmt_usd(x)


def cut_text(cost, saved):
    """'cache cut $before → $after (-N%)': what the requests would have cost without the cache, then what they cost.
    While the cache's writes haven't paid for themselves yet, say so; with no caching at all, just the cost."""
    if saved < 0:
        return f"cost {fmt_usd(cost)} · cache not paid back yet"
    if saved == 0:
        return f"cost {fmt_usd(cost)}"
    return f"cache cut {fmt_usd(cost + saved)} → {fmt_usd(cost)} ({cut_pct(cost, saved)})"


def cut_pct(cost, saved):
    pct = round(100 * saved / (cost + saved))
    return f"-{pct}%" if pct >= 1 else "<1%"


def api_price_text(cost, saved):
    """'$4,638 (would be $41,147 without cache, -89%)': the cost at API list price first, then the same calls uncached."""
    if saved < 0:
        return f"{fmt_usd(cost)} (cache not paid back yet)"
    if saved == 0:
        return fmt_usd(cost)
    return f"{fmt_usd(cost)} (would be {fmt_usd(cost + saved)} without cache, {cut_pct(cost, saved)})"


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


LIMIT_WINDOWS = (("five_hour", "5h", "5-hour"), ("seven_day", "week", "weekly"))  # key, on the line, in the tip
SPEND_PERIODS = {"daily": ("day", "daily"), "weekly": ("week", "weekly"), "monthly": ("month", "monthly")}


def usage_limits(data):
    """Every usage limit Claude Code passed, in display order: limits for one model first, then all models, then a
    gateway spend limit. five_hour and seven_day cover all models. Any other key with a used_percentage is shown too,
    named from its key: seven_day_<name> as that model's weekly limit, five_hour_<name> as its 5-hour one."""
    rl = data.get("rate_limits")
    found = []
    for key, w in (rl.items() if isinstance(rl, dict) else ()):
        used = w.get("used_percentage") if isinstance(w, dict) else None
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        short = words = None
        if key == "spend_limit":
            scope, group = "Spend limit", 2
            short, words = SPEND_PERIODS.get(str(w.get("period")), (None, None))
        else:
            rest = str(key)
            for prefix, s, wd in LIMIT_WINDOWS:
                if rest == prefix or rest.startswith(prefix + "_"):
                    short, words, rest = s, wd, rest[len(prefix) + 1:]
                    break
            name = rest.replace("_", " ").strip().title()
            scope, group = (name, 0) if name else ("All models", 1)
        found.append({"scope": scope, "group": group, "short": short, "words": words, "used": float(used),
                      "left": min(100.0, max(0.0, 100.0 - float(used))), "reset": w.get("resets_at")})
    first = {}
    for x in found:
        first.setdefault(x["scope"], len(first))
    windows = {"5h": 0, "week": 1}
    return sorted(found, key=lambda x: (x["group"], first[x["scope"]], windows.get(x["short"], 2)))


PACE_MIN_SPAN = 300   # seconds of readings needed before forecasting
PACE_MIN_RISE = 1.0   # percentage points the limit must have risen over them


def pace_key(x):
    return f"{x['scope']}|{x['short'] or ''}"


def pace_forecast(limits, cfg, now):
    """{pace_key: seconds until used up} for every limit the recent pace uses up before it resets. Keeps one reading per
    limit per minute in PACE_PATH; a new reset time starts that limit's readings over. Never raises."""
    out = {}
    try:
        on = cfg.get("pace_forecast", True)
        if on is False or str(on).strip().lower() in ("false", "0", "off", "no", "none") or not limits:
            return out
        window = float(cfg.get("pace_window_minutes", 30)) * 60
        data = read_json(PACE_PATH) if PACE_PATH.exists() else None
        data = data if isinstance(data, dict) else {}
        for x in limits:
            if x["short"] != "5h":
                continue  # a weekly limit isn't used around the clock, so its recent pace would cry wolf
            k, reset = pace_key(x), x["reset"]
            e = data.get(k) if isinstance(data.get(k), dict) else {}
            try:
                shift = float(reset) - float(e["reset"])
            except Exception:
                shift = 0.0 if e and reset == e.get("reset") else None
            if shift is not None and shift < -60:
                continue  # an idle session still passing the previous window's reading
            same = shift is not None and abs(shift) < 60
            samples = [r for r in (e.get("samples") or []) if isinstance(r, list) and len(r) == 2
                       and now - float(r[0]) <= window] if same else []
            if samples and x["used"] < float(samples[-1][1]):
                continue  # lower than the newest reading: stale, from a session that hasn't refreshed
            if not samples or now - float(samples[-1][0]) >= 60 or float(samples[-1][1]) != x["used"]:
                samples.append([now, x["used"]])
            data[k] = {"reset": reset, "samples": samples[-200:]}
            span, rise = now - float(samples[0][0]), x["used"] - float(samples[0][1])
            if reset and x["left"] > 0 and span >= PACE_MIN_SPAN and rise >= PACE_MIN_RISE:
                secs = x["left"] / (rise / span)
                if now + secs < float(reset):
                    out[k] = secs
        write_json_atomic(PACE_PATH, data)
    except Exception:
        pass
    return out


def limits_text(limits, cfg, pace=None):
    """'Fable: 61% left · All models: 58% left (5h, out in ~40m) · 82% left (week)', coloured by how much is used, and
    yellow at least when the recent pace uses it up before it resets."""
    scopes = {}
    for x in limits:
        scopes.setdefault(x["scope"], []).append(x)
    parts = []
    for scope, items in scopes.items():
        bits = []
        for x in items:
            secs = (pace or {}).get(pace_key(x))
            colour = (RED if x["used"] >= 90 else YEL if x["used"] >= cfg["limit_warn_pct"] or secs is not None
                      else "")
            notes = [x["short"]] if len(items) > 1 and x["short"] else []
            if secs is not None:
                notes.append(f"out in ~{fmt_gap(secs)}")
            window = f" ({', '.join(notes)})" if notes else ""
            bits.append(f"{colour}{x['left']:.0f}% left{window}{RST if colour else ''}")
        parts.append(f"{scope}: {' · '.join(bits)}")
    return " · ".join(parts)


def limit_name(x):
    """'Weekly Fable limit', '5-hour limit for all models', 'Monthly spend limit'."""
    words = x["words"] or ""
    if x["scope"] == "All models":
        name = f"{words} limit for all models"
    elif x["scope"] == "Spend limit":
        name = f"{words} spend limit"
    else:
        name = f"{words} {x['scope']} limit"
    name = name.strip()
    return name[0].upper() + name[1:]


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

    limits = usage_limits(data)
    pace = pace_forecast(limits, cfg, now)
    limits_txt = f" | {limits_text(limits, cfg, pace)}" if limits else ""

    if data.get("transcript_path"):
        state["transcript_path"] = data["transcript_path"]

    totals = totals_line(cfg, now)
    maybe_spawn_refresh(cfg, now)

    if not pc:
        save_state(sid, state)
        print(f"{model} {DIM}| cache: waiting for first response{RST}{limits_txt}")
        if totals:
            print(totals)
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
    kw = load_keepwarm(sid)
    kept = int(kw.get("keepwarm_pings") or 0) if (float(kw.get("keepwarm_fired_at") or 0)
                                                   > float(kw.get("last_prompt_at") or 0)) else 0
    kept_txt = f", kept warm ×{kept}" if kept else ""
    line1 = f"{model} | cache {pct} ({cache_txt}, ttl {ttl}{kept_txt}){cost_txt} | misses {misses}{limits_txt}"
    if not warm and recache:
        usd, _ = cold_cost(cfg, model_id, recache, ttl)
        line1 += f" | next msg re-reads ~{fmt_tokens(recache)}" + (f" (~{fmt_usd(usd)})" if usd else "")
    print(line1)
    if totals:
        print(totals)

    # tip line, most urgent first
    causes = ((pc.get("last_miss_cause") or {}).get("causes")) or []
    recent_miss = now - float(state.get("miss_seen_at", 0)) < cfg["cause_tip_seconds"]
    cpct = context_pct(data)
    hot = [x for x in limits if x["used"] >= cfg["limit_warn_pct"]]
    hot_limit = max(hot, key=lambda x: x["used"]) if hot else None  # the one nearest to used up

    if not warm and pc.get("caching_observed", True):
        if int(now // 30) % 2 == 0:
            tip = (f"{RED}Cache is cold.{RST} Switching tasks? /clear is cheaper than "
                   f"continuing this long history or running /compact.")
        else:
            tip = (f"{YEL}Cold cache = free moment:{RST} change model, MCP servers or fast mode "
                   f"NOW, since you'll rebuild anyway.")
    elif mins_left is not None and mins_left < cfg["expiry_warn_minutes"]:
        tip = f"{YEL}Cache expires in {mins_left + 1}m.{RST} Reply soon or the next turn rebuilds it."
    elif pace:
        x = min((x for x in limits if pace_key(x) in pace), key=lambda x: pace[pace_key(x)])
        secs, name = pace[pace_key(x)], limit_name(x)
        tip = (f"{YEL}At this pace the {name[0].lower() + name[1:]} runs out in ~{fmt_gap(secs)}{RST}, "
               f"{fmt_gap(float(x['reset']) - now - secs)} before it resets ({fmt_clock(x['reset'])}). "
               f"/effort low and fewer subagents stretch it.")
    elif hot_limit:
        left = f"{hot_limit['left']:.0f}"
        when = f" (resets {fmt_clock(hot_limit['reset'])})" if hot_limit["reset"] else ""
        tip = (f"{YEL}{limit_name(hot_limit)} {'used up' if left == '0' else 'nearly used up'}: {left}% left{when}.{RST} "
               f"Avoid resumes, model switches and cold /compact until then.")
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


TTL_SECONDS = {"1h": 3600.0, "5m": 300.0}
KEEPWARM_POLL = 30.0          # seconds between checks while waiting
KEEPWARM_ALIVE = 120          # the status line ran this recently: the session is still open
KEEPWARM_OWN_TURN = 90        # a prompt this soon after a ping is the ping's own turn, not the user coming back
KEEPWARM_PING_TOKENS = (200, 20)  # rough tokens a ping adds to the cache, and its reply
KEEPWARM_MSG = ("cache-guard keep-warm ping (automatic: the user is away and this only keeps the prompt cache warm). "
                "Reply with only: ok. Do not use tools and do not continue any task.")


def keepwarm_path(session_id):
    p = state_path(session_id)
    return p.with_name(p.stem + ".keepwarm.json")


def load_keepwarm(session_id):
    j = read_json(keepwarm_path(session_id))
    return j if isinstance(j, dict) else {}


def save_keepwarm(session_id, data):
    try:
        write_json_atomic(keepwarm_path(session_id), data)
    except Exception:
        pass


def keepwarm_on(cfg):
    v = cfg.get("keepwarm", False)
    return v is True or str(v).strip().lower() in ("true", "1", "on", "yes")


def keepwarm_lead(ttl, cfg):
    """Seconds before expiry to ping: keepwarm_lead_minutes on a 1h cache, 1 minute on a 5m one."""
    secs = TTL_SECONDS.get(ttl, TTL_SECONDS["5m"])
    try:
        lead = float(cfg.get("keepwarm_lead_minutes", 5)) * 60 if ttl == "1h" else 60.0
    except Exception:
        lead = 300.0
    return float(min(max(lead, 0.0), secs * 0.95))


def keepwarm_allowance(cfg, model_id, tokens, ttl):
    """How many pings together cost less than one rebuild of `tokens`; None when the model has no price."""
    p = price_for(price_table(cfg), model_id, int(tokens))
    if not p:
        return None
    write = p[2] if ttl == "1h" else p[1]
    ping = int(tokens) * p[3] + KEEPWARM_PING_TOKENS[0] * write + KEEPWARM_PING_TOKENS[1] * p[4]
    return int(int(tokens) * write // ping) if ping > 0 else None


def hook_keepwarm():
    """Stop hook run in the background (async + asyncRewake). While the user stays away, exit 2 shortly before the
    cache expires so Claude Code wakes Claude with KEEPWARM_MSG; its one-word reply reads the cache and resets the TTL.
    Stands down when a newer turn re-arms it, the user types, the session closes, the Mac slept past the expiry, the
    history is too small to matter, the pings would cost more than one rebuild, or keepwarm_max_hours have passed."""
    data = read_stdin_json()
    cfg = load_config()
    sid = data.get("session_id")
    if not keepwarm_on(cfg) or not sid:
        return
    ttl = load_state(sid).get("ttl")
    if ttl not in TTL_SECONDS:
        return  # the status line hasn't seen this session's cache yet
    armed, token = time.time(), os.urandom(6).hex()
    kw = load_keepwarm(sid)
    if float(kw.get("keepwarm_fired_at") or 0) <= float(kw.get("last_prompt_at") or 0):
        kw.update(keepwarm_pings=0, keepwarm_since=armed)  # the user was here since the last ping: a new stretch
    kw["keepwarm_armed"] = token
    save_keepwarm(sid, kw)
    expiry = armed + TTL_SECONDS[ttl]
    target = expiry - keepwarm_lead(ttl, cfg)

    def stood_down():
        k = load_keepwarm(sid)
        return k.get("keepwarm_armed") != token or float(k.get("last_prompt_at") or 0) > armed

    while time.time() < target:  # wall clock, so a Mac that slept is noticed
        time.sleep(max(0.0, min(KEEPWARM_POLL, target - time.time())))
        if stood_down() or not keepwarm_on(load_config()):
            return
    now = time.time()
    st, kw = load_state(sid), load_keepwarm(sid)
    if stood_down() or now >= expiry or now - float(st.get("updated") or 0) > KEEPWARM_ALIVE:
        return  # newer turn, user back, already cold, or the session is closed
    tokens, model = st.get("recache_tokens_if_cold"), st.get("model_id")
    if not tokens:
        return
    usd, _ = cold_cost(cfg, model, tokens, ttl)
    if not over_cold_floor(cfg, tokens, usd):
        return
    pings = int(kw.get("keepwarm_pings") or 0)
    allowed = keepwarm_allowance(cfg, model, tokens, ttl)
    if allowed is not None and pings >= allowed:
        return
    if now - float(kw.get("keepwarm_since") or armed) > float(cfg.get("keepwarm_max_hours", 12)) * 3600:
        return
    kw.update(keepwarm_pings=pings + 1, keepwarm_fired_at=now)
    save_keepwarm(sid, kw)
    sys.stderr.write(KEEPWARM_MSG + "\n")
    sys.exit(2)


def over_cold_floor(cfg, tokens, usd):
    """Priced model with a dollar floor set: the re-read must cost at least that. Otherwise the token floor."""
    try:
        floor = float(cfg.get("cold_prompt_warn_usd") or 0)
    except Exception:
        floor = 0.0
    if usd is not None and floor > 0:
        return usd >= floor
    return int(tokens) >= int(cfg["cold_prompt_warn_tokens"])


def cold_prompt_check(cfg, state, source, now):
    """('warn' | 'block' | None, text) for a typed message into a big cold session. Updates state."""
    mode = str(cfg.get("cold_prompt", "warn")).lower()
    exp, est = state.get("expires_at"), state.get("recache_tokens_if_cold")
    if mode == "off" or not exp or not est or not state.get("caching_observed", True) or cache_is_warm(state):
        return None, None
    try:
        if state.get("cold_ack_for") == exp:
            return None, None
        ago = fmt_gap(max(0.0, now - float(exp)))
        usd, warm_usd = cold_cost(cfg, state.get("model_id"), est, state.get("ttl"))
        if not over_cold_floor(cfg, est, usd):
            return None, None
    except Exception:
        return None, None
    price = f", about {fmt_usd(usd)} at API list price (about {fmt_usd(warm_usd)} if it were warm)" if usd else ""
    what = f"the cache went cold {ago} ago. This message re-reads ~{fmt_tokens(est)} tokens uncached{price}."
    window = int(cfg["cold_prompt_confirm_seconds"])
    asked = state.get("cold_confirm") or [None, 0]
    if mode != "block" or source != "user":
        # warn mode, or a Claude Code build that doesn't say who sent the prompt: tell, don't stop
        state["cold_ack_for"] = exp
        return "warn", f"{what} For a new task, /clear first is far cheaper."
    if asked[0] == exp and now - float(asked[1]) < window:
        state["cold_ack_for"] = exp  # confirmed: stay quiet until the cache has warmed and gone cold again
        state.pop("cold_confirm", None)
        return None, None
    state["cold_confirm"] = [exp, now]
    return "block", (f"{what} New task? /clear first is far cheaper. Same task? Send it again within "
                     f"{max(1, window // 60)} minutes to go ahead.")


def session_budget(cfg):
    try:
        b = float(cfg.get("session_budget_usd") or 0)
    except Exception:
        return 0.0
    return b if b > 0 else 0.0


def fmt_budget(x):
    return f"${x:,.0f}" if float(x).is_integer() else fmt_usd(x)


def budget_check(cfg, state, source, now):
    """('warn' | 'block' | None, text) when the session's cost first passes the budget, and each further multiple."""
    budget = session_budget(cfg)
    try:
        cost = float(((state.get("costs") or {}).get("shown") or {}).get("cost"))
    except Exception:
        return None, None
    if not budget:
        return None, None
    level = int(cost // budget)
    if level < 1 or level <= int(state.get("budget_ack") or 0):
        return None, None
    times = f"{level}× " if level > 1 else ""
    what = f"this session has cost {fmt_usd(cost)} at API list price, past {times}your {fmt_budget(budget)} budget."
    window = int(cfg.get("session_budget_confirm_seconds", 120))
    if str(cfg.get("session_budget_action", "warn")).lower() != "block" or source != "user":
        state["budget_ack"] = level
        return "warn", f"{what} For the next task, /clear starts a fresh, cheaper session."
    asked = state.get("budget_confirm") or [None, 0]
    if asked[0] == level and now - float(asked[1]) < window:
        state["budget_ack"] = level
        state.pop("budget_confirm", None)
        return None, None
    state["budget_confirm"] = [level, now]
    return "block", f"{what} Send it again within {max(1, window // 60)} minutes to go ahead, or /clear to start fresh."


def hook_prompt():
    """Typed messages only: warn (or, if asked for, stop once) when a big cold session re-reads its history, and when
    the session passes its budget. Both messages go out as one."""
    data = read_stdin_json()
    cfg = load_config()
    source = data.get("source")
    if source not in (None, "user"):
        return  # loops, scheduled tasks, SDK and system turns are never touched
    sid = data.get("session_id")
    state = load_state(sid)
    if not state:
        return
    now = time.time()
    kw = load_keepwarm(sid)
    if kw and now - float(kw.get("keepwarm_fired_at") or 0) > KEEPWARM_OWN_TURN:
        kw["last_prompt_at"] = now  # the user is here: any keep-warm timer stands down
        save_keepwarm(sid, kw)
    if str(data.get("prompt") or "").lstrip().startswith("/"):
        return  # /clear and /compact are the way out, so never stand in front of a command
    before = json.dumps(state, sort_keys=True)
    found = [r for r in (cold_prompt_check(cfg, state, source, now), budget_check(cfg, state, source, now)) if r[0]]
    if json.dumps(state, sort_keys=True) != before:
        save_state(sid, state)
    if not found:
        return
    text = "cache-guard: " + " Also, ".join(t for _, t in found)
    if any(kind == "block" for kind, _ in found):
        emit({"decision": "block", "reason": text})
    else:
        emit({"systemMessage": text})


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


IMPORT_RE = re.compile(r"(?<![\w`/])@((?:~|\.{1,2})?/?[\w.\-/]+\.[A-Za-z0-9]+)")
NOT_A_REFERENCE = r"(?!\$|\{\{|<|%|process\.env|os\.environ|os\.getenv|getenv|env\.|ENV\[)"  # ${VAR} etc. aren't secrets
SECRET_PATTERNS = [  # first match per line wins; values are never printed
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a password", re.compile(r"(?i)\b(?:password|passwd)\b\s*[:=]\s*['\"]?" + NOT_A_REFERENCE + r"[^\s'\"]{8,}")),
    ("a secret or token", re.compile(r"(?i)\b(?:secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\b"
                                     r"\s*[:=]\s*['\"]?" + NOT_A_REFERENCE + r"[^\s'\"]{12,}")),
]


def mask_url(url):
    """http://user:pass@host → http://***@host, so doctor output can be pasted safely."""
    return re.sub(r"(//)[^/@\s]*@", r"\1***@", str(url))


def always_loaded(cwd):
    """({kind: tokens}, [(path, text)]) for what Claude Code loads every session in `cwd`: CLAUDE.md files here and
    above, their @imports (5 deep, outside code), this folder's auto-memory MEMORY.md, and rules with no paths: scope."""
    sizes, files, seen = {"CLAUDE.md": 0, "@imports": 0, "auto memory": 0, "rules": 0}, [], set()

    def add(kind, path, depth=0):
        try:
            path = path.expanduser().resolve()
            if path in seen or not path.is_file() or path.stat().st_size > 2_000_000:
                return
            seen.add(path)
            text = path.read_text(errors="ignore")
        except Exception:
            return
        front = re.match(r"---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", text, re.S)
        if kind == "rules" and front and re.search(r"(?m)^paths\s*:", front.group(1)):
            return  # scoped to matching files: loaded only when Claude touches them
        sizes[kind] += len(text) // 4
        files.append((path, text))
        if depth < 5 and kind in ("CLAUDE.md", "@imports"):
            prose = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.S))
            for ref in IMPORT_RE.findall(prose):
                add("@imports", Path(ref) if ref.startswith(("~", "/")) else path.parent / ref, depth + 1)

    for d in [cwd, *cwd.parents]:
        for name in ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"):
            add("CLAUDE.md", d / name)
    add("CLAUDE.md", CLAUDE_DIR / "CLAUDE.md")
    add("auto memory", project_dir_for(cwd) / "memory" / "MEMORY.md")
    for d in (cwd / ".claude" / "rules", CLAUDE_DIR / "rules"):
        try:
            for f in sorted(d.rglob("*.md")) if d.is_dir() else []:
                add("rules", f)
        except Exception:
            pass
    return sizes, files


def secret_lines(path, text):
    for n, ln in enumerate(text.splitlines(), 1):
        for what, rx in SECRET_PATTERNS:
            if rx.search(ln):
                yield n, what
                break


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
                         "ENABLE_TOOL", "BASH_MAX", "MAX_MCP", "DISABLE_AUTO", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS",
                         "HTTPS_PROXY", "https_proxy")):
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
    base = env.get("ANTHROPIC_BASE_URL") or ""
    node_opts = env.get("NODE_OPTIONS") or ""
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    if re.search(r"(--import|--require|--loader|--experimental-loader)\b|(^|\s)-r\s", node_opts):
        warns.append(f"NODE_OPTIONS preloads code into Claude Code ({node_opts[:60]}). Request rewriters loaded this way "
                     "can change cache TTLs and markers, so what reaches Anthropic may not be what cache-guard reports.")
    if proxy and env.get("NODE_EXTRA_CA_CERTS"):
        warns.append(f"HTTPS_PROXY={mask_url(proxy)} with NODE_EXTRA_CA_CERTS: Claude Code's traffic goes through a proxy that holds "
                     "its own certificate. It can read and rewrite every request, your conversation included.")
    if re.search(r"//(localhost|127\.|\[::1\]|0\.0\.0\.0)", base):
        warns.append(f"ANTHROPIC_BASE_URL={mask_url(base)} points at a local proxy. If it rewrites requests (TTLs, cache markers, "
                     "system prompt), cache-guard's figures describe what Claude Code sent, not what Anthropic billed.")
    elif base and "api.anthropic.com" not in base:
        notes.append(f"ANTHROPIC_BASE_URL={mask_url(base)}: make sure the gateway forwards "
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
    sizes, loaded = always_loaded(cwd)
    total = sum(sizes.values())
    if total:
        msg = (f"Always loaded every session: ~{fmt_tokens(total)} tokens ("
               + ", ".join(f"{k} ~{fmt_tokens(v)}" for k, v in sizes.items() if v) + ")")
        if total > 15000:
            warns.append(msg + ". Every turn re-reads it; move rarely used parts into skills or path-scoped rules.")
        elif total > 6000:
            notes.append(msg + ". Consider moving rarely used parts into skills or path-scoped rules.")
        else:
            oks.append(msg + ".")
    mem_dir = project_dir_for(cwd) / "memory"
    scanned = {p for p, _ in loaded}
    extra = []
    try:
        extra = [(f, f.read_text(errors="ignore")) for f in sorted(mem_dir.glob("*.md")) if f.resolve() not in scanned]
    except Exception:
        pass
    for path, text in loaded + extra:
        for n, what in secret_lines(path, text):
            warns.append(f"{path}:{n} looks like {what}. This file is sent to the model, so the value is too: "
                         "keep it in an environment variable or your keychain instead.")
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
    installed = [ev for ev in ("PreModelSwitch", "PreCompact", "UserPromptSubmit", "SessionStart", "PostToolUse", "Stop")
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
    shown = not (on is False or str(on).strip().lower() in ("false", "0", "off", "no", "none"))
    if not shown and not session_budget(cfg):
        state.pop("costs", None)
        return ""
    try:
        tp = state.get("transcript_path")
        figs = session_costs(state, Path(tp), cfg) if tp else None
    except Exception:
        state.pop("costs", None)  # start afresh next run
        return ""
    if not shown:
        return ""  # still counted, so the session budget keeps working
    try:
        if not figs:
            return ""
        cost, avoid = float(figs["cost"]), float(figs["avoidable"])
        parts = [cut_text(cost, float(figs["saved"]))]
        if fmt_usd(avoid) != "$0.00":
            try:
                warn = float(cfg.get("avoidable_warn_pct", 15))
            except Exception:
                warn = 15.0
            txt = f"avoidable {fmt_usd(avoid)}"
            parts.append(f"{YEL}{txt}{RST}" if cost > 0 and 100 * avoid / cost >= warn else txt)
        budget = session_budget(cfg)
        if budget and cost >= budget:
            parts.append(f"{RED}over {fmt_budget(budget)} budget{RST}")
        return " | " + " · ".join(parts)
    except Exception:
        return ""


# ------------------------------------------------------------------ all-projects totals
def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def transcript_cwd(path):
    """The folder a session ran in: the first string "cwd" in the transcript's first 500 lines, or None."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i >= 500:
                    break
                if '"cwd"' not in ln:
                    continue
                try:
                    cwd = json.loads(ln).get("cwd")
                except Exception:
                    continue
                if isinstance(cwd, str) and cwd:
                    return cwd
    except Exception:
        pass
    return None


def file_figures(path, sidechain, table):
    """(cost, saved) of one transcript at list price, as report counts them; unpriced requests add nothing."""
    cost = saved = 0.0
    for r in load_requests(path, sidechain=sidechain):
        c = req_cost(r, table)
        if c is not None:
            cost += sum(c.values())
        x = cache_saving(r, table)
        if x is not None:
            saved += x
    return cost, saved


def scan_totals(cfg, now, beat=None):
    """Cost and cache savings of every session report --all would cover, grouped by project folder. Figures per
    transcript are kept in TOTALS_FILES_PATH, so a file whose size and mtime are unchanged is not read again."""
    days = float(cfg.get("totals_days", 30))
    cutoff = now - days * 86400
    table = price_table(cfg)
    prices = digest(json.dumps([VERSION, table, sorted(LARGE_PROMPT.items())]).encode(), 16)
    cache = read_json(TOTALS_FILES_PATH) if TOTALS_FILES_PATH.exists() else None
    old = cache.get("files") if isinstance(cache, dict) and cache.get("prices") == prices else None
    old = old if isinstance(old, dict) else {}
    files, projects = {}, {}

    def figures(path, sidechain):
        st = path.stat()
        e = old.get(str(path))
        if not (isinstance(e, dict) and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns):
            if beat:
                beat()
            cost, saved = file_figures(path, sidechain, table)
            e = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "cost": cost, "saved": saved,
                 "cwd": None if sidechain else transcript_cwd(path)}
        files[str(path)] = e
        return e

    root = CLAUDE_DIR / "projects"
    for path in sorted(root.glob("*/*.jsonl")) if root.is_dir() else []:
        try:
            mtime = path.stat().st_mtime
            if mtime < cutoff:
                continue
            runs = [figures(path, False)] + [figures(f, True) for f in subagent_files(path)]
        except OSError:  # deleted or unreadable while scanning
            continue
        key = path.parent.name
        pr = projects.setdefault(key, {"key": key, "cost": 0.0, "saved": 0.0, "cwd": None, "cwd_mtime": -1.0})
        pr["cost"] += sum(float(e["cost"]) for e in runs)
        pr["saved"] += sum(float(e["saved"]) for e in runs)
        if runs[0].get("cwd") and mtime > pr["cwd_mtime"]:  # the newest session's folder names the project
            pr["cwd"], pr["cwd_mtime"] = runs[0]["cwd"], mtime

    out = []
    for pr in projects.values():
        name = re.split(r"[\\/]", pr["cwd"].rstrip("/\\"))[-1] if pr["cwd"] else ""
        out.append({"key": pr["key"], "name": name or pr["key"].lstrip("-") or pr["key"],
                    "cost": pr["cost"], "saved": pr["saved"]})
    out.sort(key=lambda x: (-x["saved"], x["name"]))
    write_json_atomic(TOTALS_FILES_PATH, {"prices": prices, "files": files})
    return {"computed_at": now, "days": days, "cost": sum(x["cost"] for x in out),
            "saved": sum(x["saved"] for x in out), "projects": out}


def own_lock(token):
    try:
        return bool(token) and TOTALS_LOCK.read_text() == token
    except Exception:
        return False


def cmd_totals_refresh(args):
    """Scan and write TOTALS_PATH. With --lock TOKEN (as the status line starts it): keep that lock fresh while
    reading, so no one takes it over from a live scan, and remove it on success only. A failed scan leaves it to go
    stale, so the next attempt waits TOTALS_LOCK_STALE_SECONDS instead of re-reading everything every 30s."""
    token = args[args.index("--lock") + 1] if "--lock" in args[:-1] else None
    last = [0.0]

    def beat():
        if time.time() - last[0] >= 2 and own_lock(token):
            last[0] = time.time()
            os.utime(TOTALS_LOCK)

    write_json_atomic(TOTALS_PATH, scan_totals(load_config(), time.time(), beat if token else None))
    if own_lock(token):
        try:
            TOTALS_LOCK.unlink()
        except Exception:
            pass


TOTALS_LOCK_STALE_SECONDS = 300  # a lock this old belongs to a scan that died; take it over
TOTALS_NAME_MAX = 24


def totals_on(cfg):
    on = cfg.get("statusline_totals", True)
    return not (on is False or str(on).strip().lower() in ("false", "0", "off", "no", "none"))


def read_totals():
    t = read_json(TOTALS_PATH) if TOTALS_PATH.exists() else None
    return t if isinstance(t, dict) else None


def totals_fresh(t, cfg, now):
    try:
        return (float(t["days"]) == float(cfg.get("totals_days", 30))
                and now - float(t["computed_at"]) <= float(cfg.get("totals_refresh_minutes", 10)) * 60)
    except Exception:
        return False


def short_name(name):
    name = str(name or "?")
    return name if len(name) <= TOTALS_NAME_MAX else name[:TOTALS_NAME_MAX - 1] + "…"


def totals_line(cfg, now):
    """'30d at API prices: $B (would be $A without cache, -N%) | most saved: x $1.2k · y $340 · z $12.50' from the last scan,
    'counting…' before the first one, or None when it is off, nothing is priced or anything goes wrong."""
    try:
        if not totals_on(cfg):
            return None
        t = read_totals()
        if t is None:
            return f"{DIM}{float(cfg.get('totals_days', 30)):g}d at API prices:{RST} counting…"
        cost, saved = float(t["cost"]), float(t["saved"])
        if cost == 0 and saved == 0:
            return None
        text = f"{DIM}{float(t['days']):g}d at API prices:{RST} {api_price_text(cost, saved)}"
        top = sorted((x for x in t.get("projects") or [] if float(x.get("saved", 0)) > 0),
                     key=lambda x: -float(x["saved"]))[:3]
        if top:
            text += " | most saved: " + " · ".join(
                f"{short_name(x.get('name'))} {fmt_compact_usd(float(x['saved']))}" for x in top)
        return text
    except Exception:
        return None


def maybe_spawn_refresh(cfg, now):
    """Start one detached totals-refresh when the last scan is missing or stale. The lock, created with O_EXCL, lets
    only one of several status lines start it; the scan removes it when done. Never raises, never waits."""
    try:
        if not totals_on(cfg) or os.environ.get("CACHE_GUARD_NO_SPAWN"):
            return
        t = read_totals()
        if t is not None and totals_fresh(t, cfg, now):
            return
        GUARD_DIR.mkdir(parents=True, exist_ok=True)
        token = os.urandom(8).hex()
        for attempt in (0, 1):
            try:
                fd = os.open(str(TOTALS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, token.encode())
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                if attempt or now - TOTALS_LOCK.stat().st_mtime <= TOTALS_LOCK_STALE_SECONDS:
                    return
                TOTALS_LOCK.unlink()
        # no pipe is inherited, so Claude Code never waits on the child for the status line's output
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "totals-refresh", "--lock", token], **kw)
        except Exception:
            TOTALS_LOCK.unlink()
    except Exception:
        pass


# ------------------------------------------------------------------ install
def cmd_install(flags):
    want_all = "--all" in flags
    existing = read_json(SETTINGS) if SETTINGS.exists() else {}
    existing = existing or {}
    had_trim = any(SCRIPT_NAME in (h.get("command") or "")
                   for g in ((existing.get("hooks") or {}).get("PostToolUse") or [])
                   for h in g.get("hooks", []))
    # only --trim turns the trimmer on (never --all); re-running install keeps it if it was on, unless --no-trim
    trim = ("--trim" in flags or had_trim) and "--no-trim" not in flags
    had_keepwarm = any("hook-keepwarm" in (h.get("command") or "")
                       for g in ((existing.get("hooks") or {}).get("Stop") or []) for h in g.get("hooks", []))
    keepwarm = ("--keepwarm" in flags or had_keepwarm) and "--no-keepwarm" not in flags
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
    if "--keepwarm" in flags:
        cfg["keepwarm"] = True
    if "--no-keepwarm" in flags:
        cfg["keepwarm"] = False
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

    def set_hook(event, matcher, command, **extra):
        groups = hooks.setdefault(event, [])
        for g in groups:
            for h in g.get("hooks", []):
                if SCRIPT_NAME in (h.get("command") or ""):
                    h["command"] = command
                    h.update(extra)
                    g["matcher"] = matcher
                    return
        groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": command, **extra}]})

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
    if keepwarm:  # runs in the background and may wake Claude; the timeout only has to outlast a long absence
        set_hook("Stop", "", f"{cmd} hook-keepwarm", **{"async": True, "asyncRewake": True, "timeout": 46800})
    else:
        drop_hook("Stop")
    if lean:
        settings["outputStyle"] = "Lean"

    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"Installed cache-guard v{VERSION} to {GUARD_DIR}")
    print("  ✓ status line (refreshes every 30s)")
    print("  ✓ hooks: model-switch guard, cold /compact guard, cold-message guard, resume warning")
    print(f"  {'✓' if trim else '·'} Bash output trimmer {'' if trim else '(off; add --trim)'}")
    print(f"  {'✓' if keepwarm else '·'} keep-warm while you're away {'' if keepwarm else '(off; add --keepwarm)'}")
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
        "totals-refresh": lambda: cmd_totals_refresh(sys.argv[2:]),
        "hook-model-switch": hook_model_switch,
        "hook-compact": hook_compact,
        "hook-prompt": hook_prompt,
        "hook-session-start": hook_session_start,
        "hook-trim": hook_trim,
        "hook-keepwarm": hook_keepwarm,
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
