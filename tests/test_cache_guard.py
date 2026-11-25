#!/usr/bin/env python3
"""Self-contained tests for cache_guard.py. Runs every command against a throwaway HOME.
Usage: python3 tests/test_cache_guard.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "cache_guard.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def fg(n):  # a 256-colour foreground code
    return f"\x1b[38;5;{n}m"


RED_, YEL_ = fg(196), fg(166)  # the default palette's red and yellow
HOME = Path(tempfile.mkdtemp(prefix="cg-home-"))
WORK = Path(tempfile.mkdtemp(prefix="cg-work-"))
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("BASH_MAX", "MAX_MCP", "NODE_OPTIONS", "NODE_EXTRA_CA",
                                                                    "HTTPS_PROXY", "https_proxy", "ANTHROPIC_BASE_URL",
                                                                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_",
                                                                    "CLAUDE_CONFIG_DIR", "NO_COLOR", "COLORFGBG", "CACHE_GUARD_THEME",
                                                                    "CLAUDE_CODE_SUBAGENT_MODEL"))},
       "HOME": str(HOME),
       "CACHE_GUARD_NO_SPAWN": "1"}  # status lines never start a background scan, except where a test turns it back on
SPAWN_ENV = {k: v for k, v in ENV.items() if k != "CACHE_GUARD_NO_SPAWN"}
NOW = int(time.time())
failures = 0


def run(args, payload=None, cwd=WORK, raw=False, env=None):
    p = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(payload) if payload is not None else "",
                       capture_output=True, text=True, env=env or ENV, cwd=cwd)
    return p.returncode, (p.stdout if raw else ANSI.sub("", p.stdout)), ANSI.sub("", p.stderr)


# runs cache_guard.py with module constants patched (argv[2], JSON) and code run first (argv[3])
PATCH = ("import json, sys; sys.path.insert(0, sys.argv[1]); import cache_guard as cg\n"
         "for k, v in json.loads(sys.argv[2]).items(): setattr(cg, k, v)\n"
         "exec(sys.argv[3])\n"
         "sys.argv = ['cache_guard.py'] + sys.argv[4:]\n"
         "cg.main()")


def check(name, cond, detail=""):
    global failures
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures += 1
        print("      " + detail.replace("\n", "\n      "))


def pc(**kw):
    base = {"warm": True, "caching_observed": True, "ttl": "1h", "expires_at": NOW + 2400,
            "requests": 40, "misses": 0, "hit_ratio": 0.95, "recache_tokens_if_cold": 180000}
    base.update(kw)
    return base


def sl(sid, raw=False, env=None, **extra):
    d = {"session_id": sid, "model": {"display_name": "Opus 5.5"}}
    d.update(extra)
    return run(["statusline"], d, raw=raw, env=env)


# ---------------- status line
rc, out, _ = sl("a")
check("statusline: before first response", rc == 0 and "waiting for first response" in out and "tip:" in out, out)

rc, out, _ = sl("a", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
                                                       "seven_day": {"used_percentage": 18, "resets_at": NOW + 86400}})
check("statusline: warm with limits", "cache 95%" in out and "warm, 39m left" in out
      and out.splitlines()[0].endswith("| misses 0 | All models: 58% left (5h) · 82% left (week)") and "5h 42%" not in out, out)

rc, out, _ = sl("a", prompt_cache=pc(misses=1, last_miss_cause={"causes": ["tools_changed"]}))
check("statusline: new miss shows cause tip", "tool definitions changed" in out, out)

rc, out, _ = sl("b", prompt_cache=pc(ttl="5m", expires_at=NOW + 100))
check("statusline: expiring warning", "Cache expires in 2m" in out, out)

rc, out, _ = sl("c", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 86, "resets_at": NOW + 3600}})
check("statusline: usage-limit tip", "5-hour limit for all models nearly used up: 14% left (resets " in out, out)

# usage limits, as percent left and named for whom they apply to
R = NOW + 3600
rc, out, _ = sl("lim-1", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 42, "resets_at": R}})
check("limits: one all-models limit reads as percent left", out.splitlines()[0].endswith("| misses 0 | All models: 58% left"), out)
rc, out, _ = sl("lim-2", prompt_cache=pc(), rate_limits={"seven_day": {"used_percentage": 18, "resets_at": R},
                                                         "seven_day_fable": {"used_percentage": 39, "resets_at": R}})
check("limits: a model's own limit comes first, named after the model, with its own value",
      out.splitlines()[0].endswith("| misses 0 | Fable: 61% left · All models: 82% left"), out)
rc, out, _ = sl("lim-3", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 42, "resets_at": R},
                                                         "seven_day": {"used_percentage": 18, "resets_at": R},
                                                         "seven_day_fable": {"used_percentage": 39, "resets_at": R}})
check("limits: both windows of one scope say which is which",
      out.splitlines()[0].endswith("| Fable: 61% left · All models: 58% left (5h) · 82% left (week)"), out)
rc, out, _ = sl("lim-4", prompt_cache=pc(), rate_limits={"seven_day": {"used_percentage": 18, "resets_at": R},
                                                         "weekend_burst": {"used_percentage": 25, "resets_at": R},
                                                         "five_hour_opus": {"used_percentage": 10}, "note": "x",
                                                         "odd": {"resets_at": R}, "flag": {"used_percentage": True}})
check("limits: unknown limit keys of the same shape are shown, named from the key",
      out.splitlines()[0].endswith("| misses 0 | Weekend Burst: 75% left · Opus: 90% left · All models: 82% left"), out)
for sid, rl in (("lim-5", None), ("lim-5b", {}), ("lim-5c", {"five_hour": None})):
    rc, out, _ = sl(sid, prompt_cache=pc(), **({} if rl is None else {"rate_limits": rl}))
    check(f"limits: no limit data leaves the block out ({sid})", rc == 0 and out.splitlines()[0].endswith("| misses 0"), out)
rc, out, _ = sl("lim-6", prompt_cache=pc(), rate_limits={"spend_limit": {
    "used_percentage": 112.4, "resets_at": R, "used_usd": 562, "limit_usd": 500, "period": "monthly"}})
check("limits: a gateway spend limit never shows below 0% left", out.splitlines()[0].endswith("| Spend limit: 0% left")
      and "Monthly spend limit used up: 0% left (resets " in out, out)
rc, out, _ = sl("lim-7", raw=True, prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 85, "resets_at": R},
                                                                   "seven_day_fable": {"used_percentage": 91, "resets_at": R + 86400}})
check("limits: colours follow how much is used", f"Fable: {RED_}9% left\x1b[0m" in out
      and f"All models: {YEL_}15% left\x1b[0m" in out, repr(out))
check("limits: the tip names the limit nearest to used up", "Weekly Fable limit nearly used up: 9% left (resets " in out, out)

rc, out, _ = sl("d", prompt_cache=pc(), context_window={"used_percentage": 82})
check("statusline: context-full tip", "Context 82% full" in out, out)

rc, out, _ = sl("e", prompt_cache=pc(warm=False, expires_at=NOW - 60, misses=1))
check("statusline: cold", "COLD" in out and "re-reads ~180k" in out and ("Cache is cold" in out or "free moment" in out), out)

rc, out, _ = run(["statusline"], {"garbage": True})
check("statusline: survives junk input", rc == 0, out)

OPUS = {"display_name": "Opus 5.5", "id": "claude-opus-5-5"}
COLD = dict(warm=False, expires_at=NOW - 600, misses=1)
rc, out, _ = sl("p", model=OPUS, prompt_cache=pc(**COLD))
check("statusline: cold shows list-price cost when the model is known", "re-reads ~180k (~$1.44)" in out, out)

# ---------------- usage pace forecast
PACE = HOME / ".claude" / "cache-guard" / "pace.json"
R5 = NOW + 3 * 3600


def pace_sl(sid, used, samples, reset=R5, seed_reset=None, extra_limits=None, pcache=None, **kw):
    PACE.parent.mkdir(parents=True, exist_ok=True)
    PACE.write_text(json.dumps({"All models|5h": {"reset": seed_reset or reset, "samples": samples}}))
    rl = {"five_hour": {"used_percentage": used, "resets_at": reset}}
    rl.update(extra_limits or {})
    return sl(sid, prompt_cache=pcache or pc(), rate_limits=rl, **kw)


rc, out, err = pace_sl("pace-1", 60, [[NOW - 1200, 40.0]])
check("pace: a fast burn says when the limit runs out", rc == 0
      and re.search(r"All models: 40% left \(out in ~4\dm\)", out.splitlines()[0]) is not None, out + err)
check("pace: and the tip says how long before the reset",
      re.search(r"tip: At this pace the 5-hour limit for all models runs out in ~4\dm, 2\.\dh before it resets", out)
      is not None, out)
rc, out, err = pace_sl("pace-2", 60, [[NOW - 1200, 59.0]])
check("pace: a pace that lasts past the reset shows nothing", "40% left" in out and "out in" not in out, out)
rc, out, err = pace_sl("pace-3", 60, [[NOW - 120, 40.0]])
check("pace: under 5 minutes of readings shows nothing", "out in" not in out, out)
rc, out, err = pace_sl("pace-4", 60, [[NOW - 1200, 40.0]], seed_reset=NOW + 600)
check("pace: a new reset time starts the readings over", "out in" not in out
      and json.loads(PACE.read_text())["All models|5h"]["reset"] == R5, out + PACE.read_text())
rc, out, err = pace_sl("pace-5", 60, [[NOW - 1200, 40.0]], extra_limits={"seven_day": {"used_percentage": 10,
                                                                                         "resets_at": NOW + 5 * 86400}})
check("pace: with two windows it names the one running out", re.search(r"40% left \(5h, out in ~4\dm\) · 90% left \(week\)",
                                                                          out) is not None, out)
rc, out, err = pace_sl("pace-6", 60, [[NOW - 4000, 10.0]])
check("pace: readings older than the window are dropped", "out in" not in out
      and all(NOW - 2000 < x[0] for x in json.loads(PACE.read_text())["All models|5h"]["samples"]), out + PACE.read_text())
cfg_path_p = HOME / ".claude" / "cache-guard" / "config.json"
cfg_path_p.parent.mkdir(parents=True, exist_ok=True)
cfg_path_p.write_text(json.dumps({"pace_forecast": False}))
rc, out, err = pace_sl("pace-7", 60, [[NOW - 1200, 40.0]])
check("pace: pace_forecast=false turns it off", rc == 0 and "out in" not in out and "At this pace" not in out, out)
cfg_path_p.unlink()
rc, out, err = pace_sl("pace-9", 100, [[NOW - 1200, 80.0]])
check("pace: a limit already used up gets no forecast", "0% left" in out and "out in" not in out
      and "At this pace" not in out, out)
PACE.write_text(json.dumps({"All models|5h": {"reset": R5, "samples": [[NOW - 1200, 40.0], [NOW - 60, 52.0]]}}))
rc, out, err = sl("pace-10", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 45, "resets_at": R5}})
kept = json.loads(PACE.read_text())["All models|5h"]["samples"]
check("pace: a lower, stale reading from another session is ignored", "out in" not in out and kept[-1][1] == 52.0
      and len(kept) == 2, out + PACE.read_text())
rc, out, err = sl("pace-11", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 30,
                                                                             "resets_at": R5 - 18000}})
kept = json.loads(PACE.read_text())["All models|5h"]
check("pace: an older reset time from an idle session doesn't wipe the readings", kept["reset"] == R5
      and len(kept["samples"]) == 2, PACE.read_text())
PACE.write_text(json.dumps({"All models|week": {"reset": NOW + 5 * 86400, "samples": [[NOW - 1700, 20.5]]}}))
rc, out, err = sl("pace-12", prompt_cache=pc(), rate_limits={"seven_day": {"used_percentage": 22,
                                                                             "resets_at": NOW + 5 * 86400}})
check("pace: only 5-hour windows are forecast (a week isn't used around the clock)", "78% left" in out
      and "out in" not in out, out)
rc, out, err = pace_sl("pace-13", 60, [[NOW - 1200, 40.0]], model=OPUS)
tip = out.splitlines()[-1] if out.strip() else ""
check("pace: when the lockout outlasts the cache, the tip says compact or hand off first, with the rebuild cost",
      "outlasts the cache" in tip and "/compact" in tip and "$1.44" in tip, out)
rc, out, err = pace_sl("pace-14", 60, [[NOW - 1200, 40.0]], reset=NOW + 50 * 60, model=OPUS)
check("pace: when the cache outlives the lockout, the tip says it will still be warm", "still be warm" in out, out)
rc, out, err = pace_sl("pace-15", 60, [[NOW - 1200, 40.0]], pcache=pc(ttl=None, recache_tokens_if_cold=None))
check("pace: with no TTL or history size known, the tip claims nothing about the cache", "still be warm" not in out
      and "~?" not in out and "At this pace" in out, out)
rc, out, err = sl("ctx-1", model=OPUS, prompt_cache=pc(recache_tokens_if_cold=337000))
check("context meter: from 200k it shows the history and what re-reading it costs a turn",
      out.splitlines()[0].endswith("| misses 0 | ctx 337k · $0.07/turn"), out)
rc, out, err = sl("ctx-2", model=OPUS, prompt_cache=pc())
check("context meter: hidden below 200k", "ctx " not in out.splitlines()[0], out)
rc, out, err = sl("ctx-3", model=OPUS, prompt_cache=pc(recache_tokens_if_cold=520000))
check("context meter: from 400k the tip suggests /clear or /compact, with the per-turn cost",
      "Context 520k" in out and "$0.10" in out.splitlines()[-1] and "/clear" in out.splitlines()[-1], out)
rc, out, err = sl("ctx-4", prompt_cache=pc(recache_tokens_if_cold=520000))
check("context meter: an unpriced model shows the size only", "ctx 520k" in out.splitlines()[0]
      and "/turn" not in out.splitlines()[0], out)
PACE.write_text("{junk")
rc, out, err = sl("pace-8", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 60, "resets_at": R5}})
check("pace: a corrupt pace.json never breaks the line", rc == 0 and "40% left" in out and not err, out + err)
PACE.unlink()

# ---------------- cold-message guard
sl("p0", model=OPUS, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-prompt"], {"session_id": "p0", "prompt": "continue", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("cold message: by default a typed prompt is warned, never stopped", rc == 0 and "decision" not in j
      and all(s in j.get("systemMessage", "") for s in ("180k", "$1.44", "cold 10m ago", "/clear")), out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p0", "prompt": "continue", "source": "user"})
check("cold message: by default warns once per cold spell", rc == 0 and out.strip() == "", out)

cfg_path = HOME / ".claude" / "cache-guard" / "config.json"
cfg_path.parent.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(json.dumps({"cold_prompt": "block"}))  # stopping a message is opt-in
rc, out, _ = run(["hook-prompt"], {"session_id": "p", "prompt": "fix the bug", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("cold message: cold_prompt=block stops a typed prompt once with size, cost and age",
      j.get("decision") == "block" and all(s in j.get("reason", "") for s in ("180k", "$1.44", "cold 10m ago", "/clear"))
      and "additionalContext" not in out, out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p", "prompt": "fix the bug", "source": "user"})
check("cold message: sending again goes through", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p", "prompt": "and another", "source": "user"})
check("cold message: stays quiet until the cache warms again", rc == 0 and out.strip() == "", out)

sl("p2", model=OPUS, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-prompt"], {"session_id": "p2", "prompt": "tick", "source": "schedule_wakeup"})
check("cold message: scheduled prompts never touched", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p2", "prompt": "tick", "source": "loop_wakeup"})
check("cold message: loop prompts never touched", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p2", "prompt": " /clear", "source": "user"})
check("cold message: slash commands never touched", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p2", "prompt": "hello"})
j = json.loads(out) if out.strip() else {}
check("cold message: unknown sender is warned, not blocked", "decision" not in j and "180k" in j.get("systemMessage", ""), out)
rc, out, _ = run(["hook-prompt"], {"session_id": "p2", "prompt": "hello"})
check("cold message: warns once per cold spell", out.strip() == "", out)

rc, out, _ = run(["hook-prompt"], {"session_id": "a", "prompt": "hello", "source": "user"})
check("cold message: warm cache is silent", rc == 0 and out.strip() == "", out)
sl("p3", model=OPUS, prompt_cache=pc(**COLD, recache_tokens_if_cold=9000))
rc, out, _ = run(["hook-prompt"], {"session_id": "p3", "prompt": "hello", "source": "user"})
check("cold message: small history is silent", rc == 0 and out.strip() == "", out)
sl("p4", model=OPUS, prompt_cache=pc(warm=False, expires_at=None, caching_observed=False))
rc, out, _ = run(["hook-prompt"], {"session_id": "p4", "prompt": "hello", "source": "user"})
check("cold message: caching off is silent", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "never-seen", "prompt": "hello", "source": "user"})
check("cold message: no status-line data is silent", rc == 0 and out.strip() == "", out)

for mode, want in (("warn", "systemMessage"), ("off", "")):
    sid = f"p-{mode}"
    sl(sid, model=OPUS, prompt_cache=pc(**COLD))
    cfg_path.write_text(json.dumps({"cold_prompt": mode}))
    rc, out, _ = run(["hook-prompt"], {"session_id": sid, "prompt": "hello", "source": "user"})
    check(f"cold message: cold_prompt={mode}", "decision" not in out and (want in out if want else out.strip() == ""), out)
cfg_path.unlink()

# dollar threshold
HAIKU = {"display_name": "Haiku 5.5", "id": "claude-haiku-5-5"}
sl("p5", model=HAIKU, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-prompt"], {"session_id": "p5", "prompt": "hello", "source": "user"})
check("cold message: on a priced model the floor is dollars, so a cheap Haiku re-read is silent", rc == 0 and out.strip() == "",
      out)
sl("p6", model=OPUS, prompt_cache=pc(**COLD, recache_tokens_if_cold=80000))
rc, out, _ = run(["hook-prompt"], {"session_id": "p6", "prompt": "hello", "source": "user"})
check("cold message: an 80k Opus re-read costs over $0.50, so it warns", "$0.64" in out and "80k" in out, out)
sl("p7", model={"display_name": "Mystery", "id": "claude-mystery-1"}, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-prompt"], {"session_id": "p7", "prompt": "hello", "source": "user"})
check("cold message: an unpriced model falls back to the token floor", "180k" in out, out)
cfg_path.write_text(json.dumps({"cold_prompt_warn_usd": 0}))
sl("p8", model=HAIKU, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-prompt"], {"session_id": "p8", "prompt": "hello", "source": "user"})
check("cold message: cold_prompt_warn_usd=0 uses the token floor only", "180k" in out, out)
cfg_path.unlink()


# session budget
def with_cost(sid, cost):
    path = HOME / ".claude" / "cache-guard" / "state" / f"{sid}.json"
    st = json.loads(path.read_text()) if path.exists() else {}
    st["costs"] = {"shown": {"cost": cost, "avoidable": 0.0, "saved": 1.0}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st))


with_cost("b1", 30.0)
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "next", "source": "user"})
check("budget: off by default", rc == 0 and out.strip() == "", out)
cfg_path.write_text(json.dumps({"session_budget_usd": 25}))
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "next", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("budget: passing it warns with the cost", "decision" not in j
      and all(x in j.get("systemMessage", "") for x in ("$30.00", "$25 budget")), out)
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "next", "source": "user"})
check("budget: once per budget passed", rc == 0 and out.strip() == "", out)
with_cost("b1", 52.0)
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "next", "source": "user"})
msg = (json.loads(out) if out.strip() else {}).get("systemMessage", "")
check("budget: again at twice the budget", "$52.00" in msg and "past 2× your $25 budget" in msg, out)
with_cost("b1", 80.0)
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "/compact", "source": "user"})
check("budget: slash commands never touched", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "b1", "prompt": "tick", "source": "loop_wakeup"})
check("budget: automated prompts never touched", rc == 0 and out.strip() == "", out)

cfg_path.write_text(json.dumps({"session_budget_usd": 25, "session_budget_action": "block"}))
with_cost("b2", 30.0)
rc, out, _ = run(["hook-prompt"], {"session_id": "b2", "prompt": "next", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("budget: session_budget_action=block stops it once", j.get("decision") == "block"
      and "$25 budget" in j.get("reason", "") and "again" in j.get("reason", ""), out)
rc, out, _ = run(["hook-prompt"], {"session_id": "b2", "prompt": "next", "source": "user"})
check("budget: sending again goes through", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["hook-prompt"], {"session_id": "b2", "prompt": "more", "source": "user"})
check("budget: and stays quiet until the next multiple", rc == 0 and out.strip() == "", out)

cfg_path.write_text(json.dumps({"session_budget_usd": 25}))
sl("b3", model=OPUS, prompt_cache=pc(**COLD))
with_cost("b3", 30.0)
rc, out, _ = run(["hook-prompt"], {"session_id": "b3", "prompt": "next", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("budget: with a cold cache too, one message carries both", out.count("systemMessage") == 1
      and "180k" in j.get("systemMessage", "") and "$25 budget" in j.get("systemMessage", ""), out)
cfg_path.unlink()

# ---------------- keep-warm
KW_CONSTS = json.dumps({"TTL_SECONDS": {"1h": 4.0, "5m": 4.0}, "KEEPWARM_POLL": 0.05, "KEEPWARM_SETTLE": 0.2,
                        "KEEPWARM_RESET_MARGIN": 0.1, "KEEPWARM_EXPIRY_GAP": 0.2})
KW_STATE = HOME / ".claude" / "cache-guard" / "state"


def kw_state(sid, **kw):
    """Session state as the status line leaves it; keep-warm's own fields go in its own file."""
    st = {"ttl": "1h", "model_id": "claude-opus-5-5", "recache_tokens_if_cold": 337000, "updated": time.time(),
          "caching_observed": True}
    own = {k: v for k, v in kw.items() if k.startswith(("keepwarm_", "handoff_")) or k == "last_prompt_at"}
    st.update({k: v for k, v in kw.items() if k not in own})
    KW_STATE.mkdir(parents=True, exist_ok=True)
    (KW_STATE / f"{sid}.json").write_text(json.dumps(st))
    (KW_STATE / f"{sid}.keepwarm.json").write_text(json.dumps(own))


def kw_get(sid):
    f = KW_STATE / f"{sid}.keepwarm.json"
    return json.loads(f.read_text()) if f.exists() else {}


def kw_start(sid, pre=""):
    p_ = subprocess.Popen([sys.executable, "-B", "-c", PATCH, str(ROOT), KW_CONSTS, pre, "hook-keepwarm"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV,
                          cwd=WORK)
    p_.stdin.write(json.dumps({"session_id": sid, "hook_event_name": "Stop"}))
    p_.stdin.close()
    return p_


def kw_wait(p_):
    rc_ = p_.wait(timeout=30)
    return rc_, p_.stdout.read(), p_.stderr.read()


LEAD = ("import sys, json; sys.path.insert(0, sys.argv[1]); import cache_guard as cg; c = cg.load_config(); "
        "print(json.dumps([cg.keepwarm_lead('1h', c), cg.keepwarm_lead('5m', c), "
        "cg.keepwarm_lead('1h', dict(c, keepwarm_lead_minutes=57)), cg.keepwarm_lead('1h', dict(c, keepwarm_lead_minutes=0))]))")
got = subprocess.run([sys.executable, "-B", "-c", LEAD, str(ROOT)], capture_output=True, text=True, env=ENV).stdout
check("keepwarm: pings 5 minutes early on a 1h cache, 1 minute on a 5m cache", got.strip() == "[300.0, 60.0, 3420.0, 30.0]", got)

kw_state("kw0")
t0 = time.time()
rc, out, err = kw_wait(kw_start("kw0"))
check("keepwarm: off by default", rc == 0 and not out.strip() and not err.strip() and time.time() - t0 < 3, f"{rc} {err}")

cfg_path.write_text(json.dumps({"keepwarm": True, "keepwarm_lead_minutes": 1 / 60}))
kw_state("kw1")
rc, out, err = kw_wait(kw_start("kw1"))
check("keepwarm: wakes Claude with a reply-only-ok ping before the cache expires", rc == 2 and "keep-warm" in err
      and "Reply with only: ok" in err and not out.strip() and kw_get("kw1").get("keepwarm_pings") == 1, f"{rc} {err}")

kw_state("kw2")
p_ = kw_start("kw2")
time.sleep(0.6)
run(["hook-prompt"], {"session_id": "kw2", "prompt": "I'm back", "source": "user"})
rc, out, err = kw_wait(p_)
check("keepwarm: typing anything cancels the timer", rc == 0 and not err.strip() and not kw_get("kw2").get("keepwarm_pings"),
      f"{rc} {err}")

kw_state("kw3")
p1_ = kw_start("kw3")
time.sleep(0.4)
p2_ = kw_start("kw3")
rc1, _, err1 = kw_wait(p1_)
rc2, _, err2 = kw_wait(p2_)
check("keepwarm: a newer turn re-arms it and the old timer quits", rc1 == 0 and rc2 == 2
      and kw_get("kw3").get("keepwarm_pings") == 1, f"{rc1} {rc2} {err1} {err2}")

kw_state("kw4", recache_tokens_if_cold=9000)
rc, out, err = kw_wait(kw_start("kw4"))
check("keepwarm: a history too small to matter isn't kept warm", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw5", updated=time.time() - 600)
rc, out, err = kw_wait(kw_start("kw5"))
check("keepwarm: a closed session (status line stopped) isn't pinged", rc == 0 and not err.strip(), f"{rc} {err}")
SLEPT = ("import time as _t\n_real = _t.time\noff = [0.0]\n"
         "def _sleep(s):\n    off[0] += 7200\n_t.sleep = _sleep\n_t.time = lambda: _real() + off[0]\n")
kw_state("kw6", updated=time.time() + 99999)
rc, out, err = kw_wait(kw_start("kw6", SLEPT))
check("keepwarm: a Mac that slept past the expiry doesn't ping a cold cache", rc == 0 and not err.strip()
      and not kw_get("kw6").get("keepwarm_pings"), f"{rc} {err}")

t = time.time()
kw_state("kw7", keepwarm_pings=40, keepwarm_fired_at=t - 10, last_prompt_at=t - 3600, keepwarm_since=t - 3600)
rc, out, err = kw_wait(kw_start("kw7"))
check("keepwarm: stops once the pings would cost more than one rebuild", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw8", keepwarm_pings=3, keepwarm_fired_at=t - 5, last_prompt_at=t - 3600, keepwarm_since=t - 3600)
rc, out, err = kw_wait(kw_start("kw8"))
check("keepwarm: the turn after a ping keeps counting the same stretch", rc == 2 and kw_get("kw8").get("keepwarm_pings") == 4,
      f"{rc} {err} {kw_get('kw8')}")
kw_state("kw9", keepwarm_pings=2, keepwarm_fired_at=t - 5, last_prompt_at=t - 14 * 3600, keepwarm_since=t - 13 * 3600)
rc, out, err = kw_wait(kw_start("kw9"))
check("keepwarm: stops after keepwarm_max_hours away", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw10", keepwarm_pings=5, keepwarm_fired_at=t - 300, last_prompt_at=t - 10, keepwarm_since=t - 7200)
rc, out, err = kw_wait(kw_start("kw10"))
check("keepwarm: a typed message after pings starts a new stretch", rc == 2 and kw_get("kw10").get("keepwarm_pings") == 1,
      f"{rc} {err} {kw_get('kw10')}")
kw_state("kw11", keepwarm_fired_at=time.time() - 5, last_prompt_at=t - 3600)
run(["hook-prompt"], {"session_id": "kw11", "prompt": "ok"})
check("keepwarm: the ping's own turn doesn't count as you coming back", kw_get("kw11").get("last_prompt_at") == t - 3600,
      json.dumps(kw_get("kw11")))
kw_state("kw13")
p_ = kw_start("kw13")
time.sleep(0.6)
run(["hook-prompt"], {"session_id": "kw13", "prompt": "tick", "source": "loop_wakeup"})
rc, out, err = kw_wait(p_)
check("keepwarm: any new turn stands the timer down, a loop's too", rc == 0 and not err.strip(), f"{rc} {err}")
t = time.time()
kw_state("kw14", keepwarm_fired_at=t - 5, last_prompt_at=t - 3600, keepwarm_since=t - 3600)
p_ = kw_start("kw14")
time.sleep(0.6)
run(["hook-prompt"], {"session_id": "kw14", "prompt": "carry on", "source": "user"})
rc, out, err = kw_wait(p_)
check("keepwarm: a typed message right after a ping still stands it down", rc == 0 and not err.strip(), f"{rc} {err}")
tp = WORK / "kw15.jsonl"
tp.write_text("{}\n")
kw_state("kw15", transcript_path=str(tp))
p_ = kw_start("kw15")
time.sleep(0.8)
with open(tp, "a") as f:
    f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}) + "\n")
rc, out, err = kw_wait(p_)
check("keepwarm: a turn that writes the transcript without a prompt stands it down", rc == 0 and not err.strip(), f"{rc} {err}")
tp = WORK / "kw15b.jsonl"
tp.write_text("{}\n")
kw_state("kw15b", transcript_path=str(tp))
p_ = kw_start("kw15b")
time.sleep(0.8)
with open(tp, "a") as f:  # what Claude Code 2.1.332 writes on its own a few minutes after a turn ends
    for rec in ({"type": "system", "subtype": "away_summary", "content": "While you were away: ...", "isMeta": True},
                {"type": "system", "subtype": "turn_duration", "durationMs": 70000}, {"type": "summary", "summary": "x"},
                {"type": "custom-title", "customTitle": "x"}):
        f.write(json.dumps(rec) + "\n")
rc, out, err = kw_wait(p_)
check("keepwarm: Claude Code's own idle notes (away_summary and the like) are not turns, so it still pings",
      rc == 2 and "keep-warm ping" in err, f"{rc} {err}")
tp = WORK / "kw15c.jsonl"
tp.write_text("{}\n")
kw_state("kw15c", transcript_path=str(tp))
p_ = kw_start("kw15c")
line = json.dumps({"type": "user", "message": {"role": "user", "content": "scheduled task"}}) + "\n"
time.sleep(0.6)
with open(tp, "a") as f:
    f.write(line[:20])  # caught half-written: the next look must still see the whole record
time.sleep(0.3)
with open(tp, "a") as f:
    f.write(line[20:])
rc, out, err = kw_wait(p_)
check("keepwarm: a turn record caught half-written still stands it down", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw16", expires_at=time.time() + 1.6)
t0 = time.time()
rc, out, err = kw_wait(kw_start("kw16"))
check("keepwarm: uses Claude Code's own expiry when it is earlier", rc == 2 and time.time() - t0 < 2.2, f"{rc} {err}")
t = time.time()
kw_state("kw17", model_id="us.anthropic.claude-mystery-9", keepwarm_pings=20, keepwarm_fired_at=t - 5,
         last_prompt_at=t - 3600, keepwarm_since=t - 3600)
rc, out, err = kw_wait(kw_start("kw17"))
check("keepwarm: a model without a price gets a conservative cap", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw18", keepwarm_fired_at=t - 600, last_prompt_at=t - 3600)
run(["hook-prompt"], {"session_id": "kw18", "prompt": "cache-guard keep-warm ping (automatic)"})
check("keepwarm: the ping's own prompt, by its text, never counts as you coming back",
      kw_get("kw18").get("last_prompt_at") == t - 3600, json.dumps(kw_get("kw18")))
kw_state("kw19", keepwarm_ping_log=[t - 600 * i for i in range(1, 39)], last_prompt_at=t - 10)
rc, out, err = kw_wait(kw_start("kw19"))
check("keepwarm: a hard cap over 24 hours holds even if every ping looked like a new stretch", rc == 0
      and not err.strip(), f"{rc} {err}")
t = time.time()
kw_state("kw20", limits=[{"used": 97.0, "reset": t + 7200}])
rc, out, err = kw_wait(kw_start("kw20"))
check("keepwarm: near a limit that resets after the cache expires, it stands down", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw21", limits=[{"used": 100.0, "reset": time.time() + 1.0}])
rc, out, err = kw_wait(kw_start("kw21"))
check("keepwarm: a limit that has already reset by ping time doesn't stop it", rc == 2, f"{rc} {err}")
kw_state("kw22", limits=[{"used": 100.0, "reset": time.time() + 3.4}])
t0 = time.time()
rc, out, err = kw_wait(kw_start("kw22"))
check("keepwarm: a lockout ending before the cache expires is bridged by a ping just after the reset", rc == 2
      and time.time() - t0 >= 3.3, f"{rc} {time.time() - t0:.2f}s {err}")
kw_state("kw23", limits=[{"used": 100.0, "reset": time.time() + 30}])
rc, out, err = kw_wait(kw_start("kw23"))
check("keepwarm: a lockout outlasting the cache means no ping", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw24", ttl="5m", limits=[{"used": 10.0, "reset": time.time() + 7200}])
rc, out, err = kw_wait(kw_start("kw24"))
check("keepwarm: a subscription on a 5-minute cache (extra-usage credits) isn't pinged", rc == 0 and not err.strip(),
      f"{rc} {err}")
kw_state("kw25", ttl="5m")
rc, out, err = kw_wait(kw_start("kw25"))
check("keepwarm: an API-key session on a 5-minute cache still is", rc == 2, f"{rc} {err}")
kw_state("kw27", limits=[{"used": 100.0, "reset": time.time() + 3.95}])  # in the last 0.3 s before expiry
rc, out, err = kw_wait(kw_start("kw27"))
check("keepwarm: a reset too close to the expiry isn't bridged (the ping would land on a cold cache)", rc == 0
      and not err.strip(), f"{rc} {err}")
kw_state("kw28", limits=[{"used": 100.0, "reset": time.time() + 7200, "scope": "Fable"}])
rc, out, err = kw_wait(kw_start("kw28"))
check("keepwarm: a used-up limit for another model doesn't stop it", rc == 2, f"{rc} {err}")
LIMITS = HOME / ".claude" / "cache-guard" / "limits.json"
kw_state("kw29", limits=[{"used": 60.0, "reset": time.time() + 7200, "scope": "All models"}])
LIMITS.write_text(json.dumps({"at": time.time(), "limits": [{"used": 100.0, "reset": time.time() + 7200,
                                                              "scope": "All models"}]}))
rc, out, err = kw_wait(kw_start("kw29"))
check("keepwarm: the newest reading from any session wins, so it never pings into another session's lockout",
      rc == 0 and not err.strip(), f"{rc} {err}")
LIMITS.unlink()
kw_state("kw30", limits=[{"used": 100.0, "reset": "2026-11-25T10:00:00Z", "scope": "All models"}])
rc, out, err = kw_wait(kw_start("kw30"))
check("keepwarm: a reset time it can't read counts as not reset", rc == 0 and not err.strip(), f"{rc} {err}")
kw_state("kw31", ttl="5m", limits=[{"used": 10.0, "reset": time.time() + 7200, "scope": "Spend limit"}])
rc, out, err = kw_wait(kw_start("kw31"))
check("keepwarm: a gateway spend limit alone isn't a subscription, so a 5m cache is still kept warm", rc == 2,
      f"{rc} {err}")
kw_state("kw32", recache_tokens_if_cold=800000)
t0 = time.time()
rc, out, err = kw_wait(kw_start("kw32"))
check("keepwarm: a history too big to keep warm is dropped at once, not after the wait", rc == 0
      and time.time() - t0 < 1.5, f"{rc} {time.time() - t0:.2f}s {err}")
kw_state("kw26", recache_tokens_if_cold=800000)
rc, out, err = kw_wait(kw_start("kw26"))
check("keepwarm: very large histories are left to idle compaction", rc == 0 and not err.strip(), f"{rc} {err}")
sl("lim-1", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 42, "resets_at": NOW + 3600}})
lim = json.loads((KW_STATE / "lim-1.json").read_text()).get("limits")
check("keepwarm: the status line saves each limit's use, reset time and scope", isinstance(lim, list) and len(lim) == 1
      and lim[0].get("used") == 42.0 and lim[0].get("reset") == NOW + 3600 and lim[0].get("scope") == "All models",
      json.dumps(lim))
LJ = HOME / ".claude" / "cache-guard" / "limits.json"
glob = json.loads(LJ.read_text()) if LJ.exists() else {}
check("keepwarm: and the newest reading for every session in limits.json", glob.get("limits") == lim, json.dumps(glob))
kw_state("kw12", keepwarm_pings=3, keepwarm_fired_at=time.time() - 5, last_prompt_at=t - 3600)
rc, out, err = sl("kw12", prompt_cache=pc())
check("keepwarm: the status line shows how many times it kept the cache warm", "kept warm ×3" in out.splitlines()[0], out)
cfg_path.unlink()

# ---------------- handoff before the cache dies
HO_DIR = HOME / ".claude" / "cache-guard" / "handoffs"
HO_TEXT = ("# cache-guard handoff\n## Goal\nShip the handoff feature.\n## State\nTests written.\n## Decisions\nUse the "
           "first ping.\n## Open questions\nNone.\n## Next steps\n1. Implement.\n## Key files and commands\ncache_guard.py")


def ho_transcript(name, reply, split=False):
    tp = WORK / f"{name}.jsonl"
    recs = [{"type": "user", "message": {"role": "user", "content": "earlier"}},
            {"type": "assistant", "message": {"id": f"old-{name}", "role": "assistant",
                                              "content": [{"type": "text", "text": "an earlier reply"}]}},
            {"type": "user", "message": {"role": "user", "content": "cache-guard keep-warm ping with handoff ..."}}]
    parts = [reply[:30], reply[30:]] if split else [reply]
    for i, part in enumerate(parts):
        recs.append({"type": "assistant", "uuid": f"{name}-{i}", "message": {
            "id": f"msg-{name}", "role": "assistant", "content": [{"type": "text", "text": part}]}})
    recs.append({"type": "system", "subtype": "turn_duration", "durationMs": 9000})
    tp.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return tp


def capture(sid, tp, cwd=None, fresh=True):
    if fresh:
        kw_state(sid, ttl=None)  # no cache seen yet: the hook saves the handoff and returns without arming a timer
    return run(["hook-keepwarm"], {"session_id": sid, "transcript_path": str(tp), "cwd": str(cwd or WORK),
                                   "hook_event_name": "Stop"})


def put_handoff(sid, cwd, at, body=HO_TEXT):
    HO_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"session": sid, "cwd": str(cwd), "at": at, "model": "claude-opus-5-5", "history_tokens": 337000}
    (HO_DIR / f"{sid}.md").write_text(f"<!-- cache-guard handoff {json.dumps(meta)} -->\n{body}\n")


cfg_path.write_text(json.dumps({"keepwarm": True, "handoff": True, "handoff_after_minutes": 0,
                                "keepwarm_lead_minutes": 1 / 60}))
kw_state("ho1")
rc, out, err = kw_wait(kw_start("ho1"))
k = kw_get("ho1")
check("handoff: the first ping of an away stretch asks for a handoff instead of ok",
      rc == 2 and "keep-warm ping" in err and "# cache-guard handoff" in err and "Reply with only: ok" not in err
      and k.get("handoff_stretch") == k.get("keepwarm_since") and k.get("keepwarm_pings") == 1, f"{rc} {err} {k}")
t = time.time()
kw_state("ho2", keepwarm_fired_at=t - 5, last_prompt_at=t - 3600, keepwarm_since=t - 3600, handoff_stretch=t - 3600)
rc, out, err = kw_wait(kw_start("ho2"))
check("handoff: later pings in the same stretch are plain ok pings", rc == 2 and "Reply with only: ok" in err
      and "# cache-guard handoff" not in err, f"{rc} {err}")
cfg_path.write_text(json.dumps({"keepwarm": True, "handoff": True, "handoff_after_minutes": 60,
                                "keepwarm_lead_minutes": 1 / 60}))
kw_state("ho3")
rc, out, err = kw_wait(kw_start("ho3"))
check("handoff: with keep-warm on it waits until you've been away handoff_after_minutes",
      rc == 2 and "Reply with only: ok" in err and "# cache-guard handoff" not in err, f"{rc} {err}")
cfg_path.write_text(json.dumps({"keepwarm": False, "handoff": True, "handoff_after_minutes": 60,
                                "keepwarm_lead_minutes": 1 / 60}))
kw_state("ho4")
rc, out, err = kw_wait(kw_start("ho4"))
check("handoff: with keep-warm off it asks at the one wake it gets", rc == 2 and "# cache-guard handoff" in err, f"{rc} {err}")
t = time.time()
kw_state("ho5", keepwarm_fired_at=t - 5, last_prompt_at=t - 3600, keepwarm_since=t - 3600, handoff_stretch=t - 3600)
t0 = time.time()
rc, out, err = kw_wait(kw_start("ho5"))
check("handoff: with keep-warm off, once it's written the cache is left to expire", rc == 0 and not err.strip()
      and not kw_get("ho5").get("keepwarm_pings"), f"{rc} {err}")

cfg_path.write_text(json.dumps({"handoff": True}))
rc, out, err = capture("hc1", ho_transcript("hc1", HO_TEXT))
f = HO_DIR / "hc1.md"
body = f.read_text() if f.exists() else ""
meta = json.loads(body.split("\n", 1)[0][len("<!-- cache-guard handoff "):-len(" -->")]) if body.startswith("<!--") else {}
check("handoff: the reply is saved from the transcript with where and when it was written",
      rc == 0 and "## Goal\nShip the handoff feature." in body and "an earlier reply" not in body
      and meta.get("session") == "hc1" and meta.get("cwd") == str(WORK) and meta.get("at"), body + err)
first = kw_get("hc1").get("handoff_at")
time.sleep(0.05)
capture("hc1", WORK / "hc1.jsonl", fresh=False)
check("handoff: the same reply isn't saved twice", first and kw_get("hc1").get("handoff_at") == first, json.dumps(kw_get("hc1")))
rc, out, err = capture("hc2", ho_transcript("hc2", HO_TEXT, split=True))
f = HO_DIR / "hc2.md"
check("handoff: a reply split over several transcript records is saved whole",
      f.exists() and "Ship the handoff feature." in f.read_text() and "# cache-guard handoff" in f.read_text(), err)
rc, out, err = capture("hc5", ho_transcript("hc5", "Here it is:\n```markdown\n" + HO_TEXT + "\n```\n"))
f = HO_DIR / "hc5.md"
saved = f.read_text().split("\n", 1)[1] if f.exists() else ""
check("handoff: a short preamble and a code fence around it are dropped", saved.startswith("# cache-guard handoff")
      and "```" not in saved and "Here it is" not in saved, saved)
rc, out, err = capture("hc3", ho_transcript("hc3", "Sure, here's a summary of where we are."))
check("handoff: an ordinary reply is not a handoff", rc == 0 and not (HO_DIR / "hc3.md").exists(), err)
cfg_path.write_text(json.dumps({"handoff": False}))
rc, out, err = capture("hc4", ho_transcript("hc4", HO_TEXT))
check("handoff: off means nothing is saved", rc == 0 and not (HO_DIR / "hc4.md").exists(), err)

cfg_path.write_text(json.dumps({"handoff": True}))
other, here = Path(tempfile.mkdtemp(prefix="cg-other-")), Path(tempfile.mkdtemp(prefix="cg-here-"))
put_handoff("hs-old", here, time.time() - 7200, HO_TEXT.replace("Ship the handoff feature.", "the older one"))
put_handoff("hs-new", here, time.time() - 60, HO_TEXT.replace("Ship the handoff feature.", "the newest one"))
put_handoff("hs-elsewhere", other, time.time() - 10, HO_TEXT.replace("Ship the handoff feature.", "another folder"))
rc, out, err = run(["handoff-show"], cwd=here)
check("handoff-show: prints the newest handoff saved for this folder", rc == 0 and "the newest one" in out
      and "the older one" not in out and "another folder" not in out and "1m ago" in out, out + err)
rc, out, err = run(["handoff-show"], cwd=HOME)
check("handoff-show: says so when this folder has none", rc == 0 and "No cache-guard handoff" in out, out + err)
rc, out, err = run(["handoff-show", "hs-elsewh"], cwd=HOME)
check("handoff-show: or prints the one for a session id", rc == 0 and "another folder" in out, out + err)

sl("hp1", model=OPUS, prompt_cache=pc(**COLD))
put_handoff("hp1", WORK, time.time() - 1800)
rc, out, _ = run(["hook-prompt"], {"session_id": "hp1", "prompt": "carry on", "source": "user"})
msg = (json.loads(out) if out.strip() else {}).get("systemMessage", "")
check("handoff: the cold-message warning points to the saved handoff and what it costs to load",
      "/handoff-load" in msg and "handoff from" in msg and "/clear" in msg and "180k" in msg, out)
sl("hp2", model=OPUS, prompt_cache=pc(**COLD))
put_handoff("hp2", WORK, time.time() - 1800)
(KW_STATE / "hp2.keepwarm.json").write_text(json.dumps({"last_prompt_at": time.time() - 60}))
rc, out, _ = run(["hook-prompt"], {"session_id": "hp2", "prompt": "carry on", "source": "user"})
check("handoff: one older than your last message isn't offered", "/handoff-load" not in out, out)
put_handoff("hp3", WORK, time.time() - 1800)
rc, out, _ = sl("hp3", model=OPUS, prompt_cache=pc(**COLD))
check("handoff: the cold status-line tip offers it", "/handoff-load" in out.splitlines()[-1], out)
put_handoff("hr1", WORK, time.time() - 1800)
rc, out, _ = run(["hook-session-start"], {"source": "resume", "session_id": "hr1", "context_tokens": 400000,
                                           "prompt_cache_likely_expired": True})
check("handoff: the cold resume warning offers it", "/handoff-load" in out and "400k" in out, out)
kw_state("hk1", handoff_at=time.time() - 60, keepwarm_pings=2, keepwarm_fired_at=time.time() - 60,
         last_prompt_at=time.time() - 7200, keepwarm_since=time.time() - 7200)
rc, out, _ = sl("hk1", prompt_cache=pc())
check("handoff: the status line shows when one was saved while you're away", "handoff " in out.splitlines()[0]
      and "kept warm ×2" in out.splitlines()[0], out)
cfg_path.unlink()

# ---------------- model switch hook
rc, out, _ = run(["hook-model-switch"], {"session_id": "a", "to_model": "claude-sonnet-5-5"})
j = json.loads(out) if out.strip() else {}
check("model switch: warm -> ask", j.get("hookSpecificOutput", {}).get("permissionDecision") == "ask"
      and "180k" in j.get("systemMessage", ""), out)
rc, out, _ = run(["hook-model-switch"], {"session_id": "e", "to_model": "claude-sonnet-5-5"})
check("model switch: cold -> silent allow", rc == 0 and out.strip() == "", out)
rc, out, _ = run(["pre-model-switch"], {"session_id": "a"})
check("model switch: v1 command name still works", rc == 0 and "ask" in out, out)

# ---------------- compact hook
rc, out, err = run(["hook-compact"], {"session_id": "e", "trigger": "manual"})
check("compact: cold manual blocked once", rc == 2 and "COLD" in err, err)
rc, out, err = run(["hook-compact"], {"session_id": "e", "trigger": "manual"})
check("compact: repeat within window allowed", rc == 0, err)
rc, out, err = run(["hook-compact"], {"session_id": "e", "trigger": "auto"})
check("compact: auto never blocked", rc == 0, err)
rc, out, err = run(["hook-compact"], {"session_id": "a", "trigger": "manual"})
check("compact: warm allowed", rc == 0, err)

# ---------------- session start hook
rc, out, _ = run(["hook-session-start"], {"source": "resume", "context_tokens": 400000, "prompt_cache_likely_expired": True})
check("resume: big + expired warns", "400k" in out and "systemMessage" in out and "additionalContext" not in out, out)
rc, out, _ = run(["hook-session-start"], {"source": "resume", "context_tokens": 400000, "prompt_cache_likely_expired": True,
                                           "estimated_cache_write_usd": 3.2})
check("resume: shows Claude Code's own cost estimate", "$3.20" in out, out)
rc, out, _ = run(["hook-session-start"], {"source": "resume", "context_tokens": 400000, "prompt_cache_likely_expired": False})
check("resume: warm is silent", out.strip() == "", out)
rc, out, _ = run(["hook-session-start"], {"source": "resume", "context_tokens": 9000, "prompt_cache_likely_expired": True})
check("resume: small is silent", out.strip() == "", out)
rc, out, _ = run(["hook-session-start"], {"source": "startup"})
check("startup: silent", rc == 0 and out.strip() == "", out)

# transcript fallback (no context_tokens field)
tdir = HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WORK))
tdir.mkdir(parents=True, exist_ok=True)
transcript = tdir / "sess-1.jsonl"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


rows, t0 = [], NOW - 3 * 3600
spec = [  # (offset s, model, write, read, input, marker)
    (0, "claude-opus-5-5", 25000, 0, 5, None),
    (60, "claude-opus-5-5", 2000, 25000, 5, None),
    (120, "claude-opus-5-5", 3000, 27000, 5, None),
    (2400, "claude-opus-5-5", 31000, 0, 5, None),          # 38-min gap
    (2460, "claude-sonnet-5-5", 33000, 0, 5, None),        # model switch
    (2520, "claude-sonnet-5-5", 1000, 33000, 5, None),
    (2580, "claude-sonnet-5-5", 22000, 0, 5, "compact"),   # after compaction
    (9000, "claude-sonnet-5-5", 60000, 0, 5, None),        # 1.8h idle
]
for i, (off, model, w, r, inp, marker) in enumerate(spec):
    if marker == "compact":
        rows.append({"type": "system", "subtype": "compact_boundary", "timestamp": iso(t0 + off - 5)})
    rec = {"type": "assistant", "timestamp": iso(t0 + off), "isSidechain": False,
           "message": {"id": f"msg_{i}", "model": model,
                       "usage": {"input_tokens": inp, "cache_creation_input_tokens": w,
                                 "cache_read_input_tokens": r, "output_tokens": 300}}}
    rows.append(rec)
    rows.append(rec)  # duplicate streaming line, must be de-duplicated
rows.append({"type": "assistant", "isSidechain": True, "timestamp": iso(t0 + 50),
             "message": {"id": "side", "model": "claude-haiku-5-5",
                         "usage": {"input_tokens": 1, "cache_creation_input_tokens": 99999,
                                   "cache_read_input_tokens": 0, "output_tokens": 1}}})
transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
os.utime(transcript, (NOW - 7200, NOW - 7200))
rc, out, _ = run(["hook-session-start"], {"source": "resume", "transcript_path": str(transcript)})
check("resume: transcript fallback estimates size", "60k" in out, out)

# ---------------- colours: readable on white and on dark backgrounds
saved_cfg = cfg_path.read_text() if cfg_path.exists() else None
if cfg_path.exists():
    cfg_path.unlink()
BIG = pc(recache_tokens_if_cold=520000)  # a yellow size warning with a plain "tip:" label
rc, out, _ = sl("col-1", raw=True, model=OPUS, prompt_cache=BIG)
check("colours: no faint text and none of the basic yellow, green or cyan by default",
      not any(c in out for c in ("\x1b[2m", "\x1b[33m", "\x1b[32m", "\x1b[36m")) and YEL_ in out, repr(out))
check("colours: by default secondary text keeps the terminal's own colour", "\ntip:\x1b[0m " in out, repr(out))
rc, out, _ = sl("col-1", raw=True, prompt_cache=pc(**COLD))
check("colours: the default red is the mid-tone one", f"{RED_}\x1b[1mCOLD" in out, repr(out))
for how, env_extra, cfg_extra, yel, sub_ in (
        ("CACHE_GUARD_THEME=light", {"CACHE_GUARD_THEME": "light"}, None, 130, 240),
        ("\"theme\": \"dark\" in config.json", {}, {"theme": "dark"}, 214, 248),
        ("COLORFGBG naming a white background", {"COLORFGBG": "0;15"}, None, 130, 240),
        ("COLORFGBG naming a black background", {"COLORFGBG": "15;0"}, None, 214, 248),
        ("CACHE_GUARD_THEME over config.json", {"CACHE_GUARD_THEME": "light"}, {"theme": "dark"}, 130, 240)):
    if cfg_extra:
        cfg_path.write_text(json.dumps(cfg_extra))
    rc, out, _ = sl("col-1", raw=True, env={**ENV, **env_extra}, model=OPUS, prompt_cache=BIG)
    check(f"colours: {how} picks the matching palette, secondary text included",
          fg(yel) in out and f"{fg(sub_)}tip:" in out, repr(out))
    if cfg_extra:
        cfg_path.unlink()
ALERT_ = "\x1b[1;38;5;231;48;5;160m"
rc, out, _ = sl("col-2", raw=True, prompt_cache=pc(), context_window={"used_percentage": 90})
tip_line = out.splitlines()[-1]
check("colours: near auto-compact the tip becomes a white-on-red COMPACT SOON badge with a bold red headline",
      tip_line.startswith(f"{ALERT_} COMPACT SOON \x1b[0m ") and f"\x1b[1m{RED_}Context 90% full.\x1b[0m" in tip_line
      and "/compact" in tip_line, repr(out))
rc, out, _ = sl("col-2", raw=True, env={**ENV, "CACHE_GUARD_THEME": "dark"}, prompt_cache=pc(),
                context_window={"used_percentage": 90})
check("colours: the badge looks the same on every theme, since it brings its own background", ALERT_ in out, repr(out))
rc, out, _ = sl("col-2", raw=True, prompt_cache=pc(), context_window={"used_percentage": 60})
check("colours: below context_warn_pct there is no badge", "COMPACT SOON" not in out and ALERT_ not in out, repr(out))
rc, out, _ = sl("col-2", prompt_cache=pc(), context_window={"used_percentage": 75})
check("colours: exactly at context_warn_pct (75) is not above it: no badge", "COMPACT SOON" not in out, out)
rc, out, _ = sl("col-2", prompt_cache=pc(), context_window={"used_percentage": 75.5})
check("colours: just above context_warn_pct the badge shows", "COMPACT SOON" in out, out)
rc, out, _ = sl("col-2", raw=True, env={**ENV, "NO_COLOR": "1"}, prompt_cache=pc(), context_window={"used_percentage": 90})
check("colours: with NO_COLOR the badge is still spelled out", "COMPACT SOON" in out and "\x1b[" not in out, repr(out))
rc, out, _ = sl("col-1", raw=True, env={**ENV, "NO_COLOR": "1"}, prompt_cache=pc(**COLD))
check("colours: NO_COLOR turns every colour off", rc == 0 and "\x1b[" not in out and "(COLD, ttl 1h)" in out, repr(out))
for appearance, want in (("dark", "dark"), ("light", "light"), (None, "auto")):
    p = subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import cache_guard as cg; "
                        f"cg.macos_appearance = lambda: {appearance!r}; print(cg.colour_theme())", str(ROOT)],
                       capture_output=True, text=True, env={**ENV, "CACHE_GUARD_THEME": "system"})
    check(f"colours: theme system follows macOS ({appearance} -> {want})", p.stdout.strip() == want, p.stdout + p.stderr)

# ---------------- signing in to another organization
CJ = HOME / ".claude.json"


def sign_in(org):
    CJ.write_text(json.dumps({"oauthAccount": {"organizationUuid": org, "organizationName": "Test org"}}))


def prompt(sid, text="carry on", env=None):
    rc, out, _ = run(["hook-prompt"], {"session_id": sid, "prompt": text, "source": "user"}, env=env)
    return rc, out, (json.loads(out) if out.strip() else {})


sign_in("org-aaaa-1111")
sl("org-1", model=OPUS, prompt_cache=pc())
rc, out, j = prompt("org-1", "hello")
check("org switch: the first typed message records the organization quietly", rc == 0 and out.strip() == "", out)
st = json.loads((HOME / ".claude" / "cache-guard" / "state" / "org-1.json").read_text())
check("org switch: stores a fingerprint, never the organization id", st.get("org") and "org-aaaa" not in json.dumps(st),
      json.dumps(st))
sign_in("org-bbbb-2222")
rc, out, j = prompt("org-1", "/compact")
check("org switch: a slash command is left alone", out.strip() == "", out)
rc, out, j = prompt("org-1")
msg = j.get("systemMessage", "")
check("org switch: a warm cache from the old organization still warns, with size, cost and the lost thinking",
      "decision" not in j and all(s in msg for s in ("different organization", "180k", "$1.44", "thinking", "/clear")), out)
rc, out, j = prompt("org-1")
check("org switch: warns once", out.strip() == "", out)
cfg_path.write_text(json.dumps({"cold_prompt": "block"}))
sign_in("org-aaaa-1111")
rc, out, j = prompt("org-1")
check("org switch: cold_prompt=block stops the first typed message once",
      j.get("decision") == "block" and "different organization" in j.get("reason", "") and "again" in j.get("reason", ""), out)
rc, out, j = prompt("org-1")
check("org switch: sending it again goes ahead", out.strip() == "", out)
cfg_path.write_text(json.dumps({"cold_prompt": "off"}))
sign_in("org-bbbb-2222")
rc, out, j = prompt("org-1")
check("org switch: cold_prompt=off stays quiet", out.strip() == "", out)
cfg_path.unlink()
sign_in("org-aaaa-1111")
rc, out, j = prompt("org-1", env={**ENV, "ANTHROPIC_API_KEY": "test-key"})
check("org switch: with an API key the signed-in organization isn't what bills, so it stays quiet", out.strip() == "", out)
CJ.write_text("{}")
rc, out, j = prompt("org-1")
check("org switch: not signed in at all is quiet", out.strip() == "", out)
sl("org-3", model=OPUS, prompt_cache=pc(recache_tokens_if_cold=9000))
sign_in("org-aaaa-1111")
prompt("org-3", "hello")
sign_in("org-bbbb-2222")
rc, out, j = prompt("org-3")
check("org switch: a small history isn't worth a warning", out.strip() == "", out)

sign_in("org-aaaa-1111")
sl("org-2", model=OPUS, prompt_cache=pc())
prompt("org-2", "hello")
sign_in("org-bbbb-2222")
rc, out, _ = run(["hook-session-start"], {"source": "resume", "session_id": "org-2", "context_tokens": 400000,
                                           "prompt_cache_likely_expired": False})
check("org switch: resuming under another organization warns even when the cache looks warm",
      all(s in out for s in ("different organization", "400k", "thinking", "/clear")), out)
rc, out, j = prompt("org-2")
check("org switch: and the first message after that resume isn't warned twice", out.strip() == "", out)
rc, out, _ = run(["hook-session-start"], {"source": "resume", "session_id": "org-2", "context_tokens": 400000,
                                           "prompt_cache_likely_expired": False})
check("org switch: resuming again in the same organization is silent", out.strip() == "", out)
CJ.unlink()

# ---------------- model switch: thinking the new model can't read
FABLE = {"display_name": "Fable 5.2", "id": "claude-fable-5-2"}
sl("ms-1", model=FABLE, prompt_cache=pc())
rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-1", "to_model": "claude-opus-5-5"})
j = json.loads(out) if out.strip() else {}
msg = j.get("systemMessage", "")
check("model switch: says when the new model can't read the old model's thinking",
      j.get("hookSpecificOutput", {}).get("permissionDecision") == "ask"
      and all(s in msg for s in ("180k", "Opus 5.5 can't read Fable 5.2's earlier thinking", "switch back")), out)
sl("ms-2", model=FABLE, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-2", "to_model": "claude-opus-5-5[1m]"})
j = json.loads(out) if out.strip() else {}
check("model switch: a cold switch that loses thinking is mentioned, not stopped",
      "hookSpecificOutput" not in j and "can't read Fable 5.2's earlier thinking" in j.get("systemMessage", ""), out)
rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-2", "to_model": "claude-opus-5-5"},
                 env={**ENV, "CLAUDE_CODE_USE_BEDROCK": "1"})
check("model switch: no thinking note off the Claude API, where the table differs", out.strip() == "", out)
sl("ms-3", model=OPUS, prompt_cache=pc())
rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-3", "to_model": "claude-fable-5-2"})
check("model switch: no thinking note when the new model reads the old one's thinking",
      "ask" in out and "can't read" not in out, out)
for frm, to in (("claude-sonnet-5", "claude-opus-5-5"), ("claude-fable-5-2", "opus"),
                ("claude-fable-5-2", "claude-fable-5-2[1m]"), ("claude-opus-5-5", "claude-fable-5-1")):
    sl("ms-4", model={"display_name": "x", "id": frm}, prompt_cache=pc(**COLD))
    rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-4", "to_model": to})
    check(f"model switch: cold {frm} -> {to} says nothing", rc == 0 and out.strip() == "", out)
sl("ms-5", model={"display_name": "Sonnet 5.5", "id": "claude-sonnet-5-5"}, prompt_cache=pc(**COLD))
rc, out, _ = run(["hook-model-switch"], {"session_id": "ms-5", "to_model": "claude-opus-4-8"})
check("model switch: moving to an earlier model loses the newer model's thinking",
      "Opus 4.8 can't read Sonnet 5.5's earlier thinking" in out, out)
if saved_cfg is not None:
    cfg_path.write_text(saved_cfg)

# ---------------- report
rc, out, err = run(["report"])
check("report: finds latest transcript for cwd", rc == 0 and "sess-1.jsonl" in out, out + err)
check("report: de-duplicates and skips subagents", "8 requests" in out and "haiku" not in out, out)
check("report: causes inferred", all(s in out for s in ("session start", "model switch", "after compaction", "idle 1.8h", "idle 38m")), out)
rc, out, err = run(["report", "sess-1"])
check("report: by session id", rc == 0 and "sess-1.jsonl" in out, out + err)
check("report: start and compaction are expected, not misses", "Cache misses: 3" in out and "Expected cache writes" in out, out)
rc, out, err = run(["report", "sess"])
check("report: by the start of a session id", rc == 0 and "sess-1.jsonl" in out, out + err)


def usage_row(i, t, model, w, r, ttl="1h", side=False):
    cc = {"ephemeral_1h_input_tokens": w if ttl == "1h" else 0, "ephemeral_5m_input_tokens": w if ttl == "5m" else 0}
    return {"type": "assistant", "timestamp": iso(t), "isSidechain": side,
            "message": {"id": f"m{i}-{side}", "model": model,
                        "usage": {"input_tokens": 5, "cache_creation_input_tokens": w, "cache_read_input_tokens": r,
                                  "output_tokens": 300, "cache_creation": cc}}}


t1 = NOW - 2 * 3600
rows = [
    usage_row(0, t1, "claude-opus-5-5", 30000, 0),
    usage_row(1, t1 + 60, "claude-opus-5-5", 50000, 30000),     # big append on a warm cache: NOT a miss
    usage_row(2, t1 + 120, "claude-opus-5-5", 2000, 80000),
    {"type": "attachment", "attachment": {"type": "deferred_tools_delta"}, "timestamp": iso(t1 + 170)},
    {"type": "user", "timestamp": iso(t1 + 175), "message": {"role": "user", "content": "quoting session_context here"}},
    usage_row(3, t1 + 180, "claude-opus-5-5", 82000, 0),        # real miss, tools changed
    usage_row(4, t1 + 4000, "claude-opus-5-5", 84000, 0),       # idle 1.1h on a 1h cache
]
(tdir / "sess-2.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
sub_dir = tdir / "sess-2" / "subagents"
sub_dir.mkdir(parents=True)
sub = [  # a subagent on the 5m cache that pauses three times for 20 minutes
    usage_row(0, t1, "claude-sonnet-5-5", 20000, 0, "5m", True),
    usage_row(1, t1 + 30, "claude-sonnet-5-5", 1000, 20000, "5m", True),
    usage_row(2, t1 + 1230, "claude-sonnet-5-5", 22000, 0, "5m", True),
    usage_row(3, t1 + 2430, "claude-sonnet-5-5", 23000, 0, "5m", True),
    usage_row(4, t1 + 3630, "claude-sonnet-5-5", 24000, 0, "5m", True),
]
(sub_dir / "agent-x.jsonl").write_text("\n".join(json.dumps(r) for r in sub) + "\n")
(sub_dir / "agent-x.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "find things"}))

rc, out, err = run(["report", "sess-2"])
check("report: a big append on a warm cache is not a miss", rc == 0 and "Cache misses: 2 (164k tokens re-cached, about $1.31)" in out, out + err)
check("report: causes from transcript markers and the real TTL",
      "tools or MCP servers changed" in out and "idle 1.1h (cache expired, ttl 1h)" in out and "resumed" not in out, out)
check("report: list-price cost", "at API list price" in out and "$" in out, out)
check("report: includes the session's subagents",
      "Subagents: 1 runs, 5 requests" in out and 'Explore "find things"' in out and "5m 90k, 1h 0" in out, out)

rc, out, err = run(["report", "--all"])
check("report --all: totals across sessions", rc == 0 and "2 sessions, 1 subagent runs" in out
      and "Main conversations" in out and "Subagents and workflows" in out, out + err)
check("report --all: rebuilds by cause", all(s in out for s in ("idle past the cache TTL", "model switch",
                                                                 "tools or MCP servers changed", "after compaction (expected)")), out)
check("report --all: subagent cache length verdict", "Now 5m" in out and "Verdict: switch to 1h" in out
      and '"subagentPromptCacheTtl": "1h"' in out, out)
rc, out, err = run(["report", "--all", "--days", "0.01"])
check("report --all --days: filters by age", rc == 0 and "1 sessions" in out, out + err)
(sub_dir / "agent-x.jsonl").write_text("\n".join(json.dumps(r) for r in sub[:2]) + "\n")
rc, out, err = run(["report", "--all"])
check("report --all: no pauses means keep 5m", "Verdict: keep 5m" in out, out + err)
rc, out, err = run(["report", "--all"])
wi = {m: float(v.strip("$").replace(",", "")) for m, v in re.findall(r"(claude-(?:opus|sonnet|haiku)-5-5)\s+main\s+\$[\d,.]+\s+subagents\s+\$[\d,.]+"
                                         r"\s+total\s+(\$[\d,.]+)", out)}
check("report --all: reprices the same tokens on Opus, Sonnet and Haiku 5.5", rc == 0 and len(wi) == 3
      and wi["claude-haiku-5-5"] < wi["claude-sonnet-5-5"] < wi["claude-opus-5-5"], out + err)
bdir = HOME / ".claude" / "projects" / "-blocks-tests"
bdir.mkdir(parents=True, exist_ok=True)
tb = NOW - 3000
burst = {"type": "user", "timestamp": iso(tb + 10), "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": f"tu{k}", "content": "x"} for k in range(25)]}}
(bdir / "blocks.jsonl").write_text("\n".join(json.dumps(r) for r in [
    usage_row(1, tb, "claude-opus-5-5", 40000, 0), burst, usage_row(2, tb + 30, "claude-opus-5-5", 41000, 0)]) + "\n")
rc, out, err = run(["report", str(bdir / "blocks.jsonl")])
check("report: a miss right after a turn that returned 25 tool results is named as Claude Code's 20-block issue",
      rc == 0 and "25 tool results" in out and "20-block" in out, out + err)
def burst_of(n, **extra):
    return dict({"type": "user", "timestamp": iso(tb + 10), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"tu{k}", "content": "x"} for k in range(n)]}}, **extra)
for name, mid in (("b12", [burst_of(12)]),
                  ("blob", [dict(burst_of(25), type="api-request-blob"), burst_of(3)]),
                  ("side", [dict(burst_of(25), isSidechain=True), burst_of(3)])):
    (bdir / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in [
        usage_row(1, tb, "claude-opus-5-5", 40000, 0), *mid, usage_row(2, tb + 30, "claude-opus-5-5", 41000, 0)]) + "\n")
out12 = run(["report", str(bdir / "b12.jsonl")])[1]
outblob = run(["report", str(bdir / "blob.jsonl")])[1]
outside = run(["report", str(bdir / "side.jsonl")])[1]
check("report: 12 parallel calls (24 blocks with their results) count, request logs and subagent lines don't",
      "12 tool results" in out12 and "20-block" not in outblob and "20-block" not in outside, out12 + outblob + outside)
cfg_path.write_text(json.dumps({"pricing": {"claude-opus-5-5": [40, 50, 80, 2, 200]}}))
rc, out, err = run(["report", "sess-2"])
check("report: prices can be overridden in config.json", "about $13.12" in out, out + err)
cfg_path.unlink()

# ---------------- status line: session cost, avoidable cost and cache savings
STATE = HOME / ".claude" / "cache-guard" / "state"
cdir = HOME / ".claude" / "projects" / "-cost-tests"  # its own folder, so the report tests above are unaffected
cdir.mkdir(parents=True, exist_ok=True)
O, S, H, MYST = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-mystery-1"
MONEY = re.compile(r"\| (?:cache cut (\$[\d,.]+) → (\$[\d,.]+) \((?:-\d+%|<1%)\)|cost (\$[\d,.]+)(?: · cache not paid back yet)?)"
                   r"(?: · avoidable (\$[\d,.]+))? \|")

# A full read with the report's own functions. Saved is restated here from its definition, not taken from the script.
REF = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import cache_guard as cg
from pathlib import Path
path = Path(sys.argv[2])
table = cg.price_table(cg.load_config())
def saved(rows):
    total = 0.0
    for r in rows:
        p = cg.price_for(table, r["model"], r["inp"] + r["write"] + r["read"])
        if p:
            w5, w1 = (r["w5"], r["w1"]) if r["w5"] + r["w1"] else (r["write"], 0)
            total += (r["read"] * (p[0] - p[3]) - w5 * (p[1] - p[0]) - w1 * (p[2] - p[0])) / 1e6
    return total
main = cg.load_requests(path)
subs = [cg.load_requests(f, sidechain=True) for f in cg.subagent_files(path)]
cost = cg.total_cost(cg.summarise(main, table)) + sum(cg.total_cost(cg.summarise(s, table)) for s in subs)
avoid = sum(x["usd"] or 0.0 for x in cg.find_rebuilds(main, table) if x["kind"] not in cg.EXPECTED)
priced = sum(1 for rows in [main] + subs for r in rows if cg.price_for(table, r["model"], 0))
print(json.dumps({"cost": cost, "avoidable": avoid, "saved": saved(main) + sum(saved(s) for s in subs), "priced": priced}))
'''


def reference(path):
    p = subprocess.run([sys.executable, "-B", "-c", REF, str(ROOT), str(path)], capture_output=True, text=True, env=ENV)
    return json.loads(p.stdout)


def cost_sl(sid, path, consts=None, pre="", raw=False, **pc_kw):
    """Status line for a session whose transcript is `path`; consts/pre patch the module first (budget, block size)."""
    payload = {"session_id": sid, "model": OPUS, "transcript_path": str(path), "prompt_cache": pc(**pc_kw)}
    p = subprocess.run([sys.executable, "-B", "-c", PATCH, str(ROOT), json.dumps(consts or {}), pre, "statusline"],
                       input=json.dumps(payload), capture_output=True, text=True, env=ENV, cwd=WORK)
    return p.returncode, (p.stdout if raw else ANSI.sub("", p.stdout)), p.stderr


def usd(s):
    return float(s.replace("$", "").replace(",", "")) if s else 0.0


def money(out):
    m = MONEY.search(out.splitlines()[0] if out.strip() else "")
    return m and {"cost": usd(m.group(2) or m.group(3)), "avoidable": usd(m.group(4)),
                  "before": usd(m.group(1)) if m.group(1) else None, "text": m.group(0)}


def agrees(fig, ref):
    """Printed figures match a full read to the cent (fmt_usd rounds to cents below $100). The cost before the cache
    is cost + saved; with no before figure the line says the cache has not paid back yet, so saved must be about 0 or less."""
    if not fig or not all(abs(fig[k] - ref[k]) <= 0.0051 for k in ("cost", "avoidable")):
        return False
    if fig["before"] is None:
        return ref["saved"] <= 0.005
    return abs(fig["before"] - (ref["cost"] + ref["saved"])) <= 0.0051


def exact(sid, ref):
    """The running totals kept in the state file match a full read, not just to the cent."""
    try:
        s = json.loads((STATE / f"{sid}.json").read_text())["costs"]["shown"]
        return all(abs(s[k] - ref[k]) < 1e-9 for k in ("cost", "avoidable", "saved"))
    except Exception:
        return False


def line(rec):
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode()


def resp(i, t, model, w, r, out=300, ttl="1h", side=False, split=True, text="ok → done"):
    u = {"input_tokens": 7, "cache_creation_input_tokens": w, "cache_read_input_tokens": r, "output_tokens": out}
    if split:
        u["cache_creation"] = {"ephemeral_1h_input_tokens": w if ttl == "1h" else 0,
                               "ephemeral_5m_input_tokens": w if ttl == "5m" else 0}
    return {"type": "assistant", "timestamp": iso(t), "isSidechain": side, "requestId": f"req_{i}",
            "message": {"id": f"msg_{i}", "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": text}], "usage": u}}


def session_bytes(tag):
    """A main transcript with every kind of rebuild, and two subagent files. Returns (main, {relpath: bytes})."""
    t, out, prefix, n = NOW - 8 * 3600, [], 0, [0]

    def ask(dt, model, grow, miss=False, **kw):
        nonlocal t, prefix
        t += dt
        n[0] += 1
        read, write = (0, prefix + grow) if miss else (prefix, grow)
        out.append(line({"type": "user", "timestamp": iso(t - 2), "message": {"role": "user", "content": "go " * 40}}))
        out.append(line(resp(f"{tag}{n[0]}", t, model, write, read, **kw)))
        prefix = read + write

    ask(0, O, 40000)                                    # session start (expected)
    for _ in range(9):
        ask(45, O, 1500)                                # warm appends
    t += 40                                             # a streamed response logged twice: the last line counts
    n[0] += 1
    out.append(line(resp(f"{tag}{n[0]}", t, O, 1200, prefix, out=12)))
    out.append(line(resp(f"{tag}{n[0]}", t, O, 1200, prefix, out=480)))
    prefix += 1200
    ask(5400, O, 900, miss=True)                        # idle 1.5h on a 1h cache
    ask(50, O, 700)
    out.append(line(resp("side-0", t + 5, H, 99999, 0, side=True)))  # sidechain line in the main file: skipped
    ask(60, S, 500, miss=True)                          # model switch
    ask(30, S, 800, ttl="5m")
    out.append(line({"type": "system", "subtype": "compact_boundary", "timestamp": iso(t + 20)}))
    prefix = 0
    ask(30, S, 20000, ttl="5m")                         # after compaction (expected)
    ask(30, S, 600, ttl="5m")
    out.append(line({"type": "attachment", "attachment": {"type": "session_context"}, "timestamp": iso(t + 10)}))
    ask(30, S, 300, miss=True, ttl="5m")                # resumed or restarted
    ask(30, S, 400, ttl="5m")
    out.append(line({"type": "attachment", "attachment": {"type": "deferred_tools_delta"}, "timestamp": iso(t + 10)}))
    ask(30, S, 300, miss=True, ttl="5m")                # tools changed
    ask(30, S, 400, ttl="5m")
    ask(30, S, 200, miss=True, ttl="5m")                # prefix changed, cause unknown
    ask(30, MYST, 100, miss=True)                       # unpriced model: left out of every figure
    ask(30, S, 100, miss=True, ttl="5m")                # back again: a priced model switch
    ask(30, S, 300, ttl="5m", split=False)              # no 5m/1h split recorded
    for _ in range(3):
        ask(30, S, 250, ttl="5m")

    a, ts, p2 = [], NOW - 7 * 3600, 0
    for k, (dt, grow, miss) in enumerate([(0, 18000, False), (20, 900, False), (20, 700, False), (1300, 400, True),
                                          (20, 500, False), (20, 300, False)]):
        ts += dt
        read, write = (0, p2 + grow) if (miss or k == 0) else (p2, grow)
        a.append(line(resp(f"{tag}a{k}", ts, S, write, read, out=100, ttl="5m", side=True)))
        if k == 2:
            a.append(line(resp(f"{tag}a{k}", ts, S, write, read, out=350, ttl="5m", side=True)))
        p2 = read + write
    b = [line(resp(f"{tag}b{k}", NOW - 6 * 3600 + 30 * k, H, w, r, out=200, ttl="5m", side=True))
         for k, (w, r) in enumerate([(120000, 0), (2000, 120000), (1500, 122000)])]  # large-prompt Haiku price
    return b"".join(out), {"subagents/agent-a.jsonl": b"".join(a), "workflows/w1/agent-b.jsonl": b"".join(b)}


def write_session(name, main, subs):
    path = cdir / f"{name}.jsonl"
    path.write_bytes(main)
    for rel, data in subs.items():
        f = cdir / name / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return path


main_b, subs_b = session_bytes("c")
p1 = write_session("cost-1", main_b, subs_b)
ref1 = reference(p1)
rc, out, err = cost_sl("cost-1", p1)
fig = money(out)
check("statusline cost: block sits between the cache and misses parts", rc == 0 and bool(fig)
      and re.search(r"\(warm, \d+m left, ttl 1h\) \| (cache cut \$[\d.,]+ → \$[\d.,]+ \(-\d+%\)|cost \$[\d.,]+ · cache not paid "
                              r"back yet) · avoidable \$[\d.]+ \| misses 0",
                    out) is not None, out + err)
rc, rep, err = run(["report", str(p1)])
costs = re.findall(r"Cost\s+\$([\d,.]+)\s+at API list price", rep)
mm = re.search(r"Cache misses: \d+ \([^)]*about (\$[\d,.]+)\)", rep)
check("statusline cost: cost = the report's main conversation + subagents",
      bool(fig) and len(costs) == 2 and abs(fig["cost"] - sum(usd(c) for c in costs)) <= 0.0101, f"{out}\n{rep}")
check("statusline cost: avoidable = the report's cache-miss cost", bool(fig) and bool(mm)
      and f"avoidable {mm.group(1)}" in fig["text"], f"{out}\n{rep}")
check("statusline cost: all three agree with a full read", agrees(fig, ref1) and exact("cost-1", ref1),
      f"{out}\n{ref1}")
check("statusline cost: an unpriced model is left out, as in the report", "1 requests on unpriced models left out" in rep
      and ref1["priced"] == 28 - 1 + 9, f"{rep}\n{ref1}")

plain = sl("plain-1", model=OPUS, prompt_cache=pc())[1].splitlines()[0]
check("statusline cost: the rest of the line is unchanged",
      bool(fig) and re.sub(r"\d+m left", "", out.splitlines()[0].replace(fig["text"], "|"))
      == re.sub(r"\d+m left", "", plain), f"{out}\n{plain}")

rc, out2, _ = cost_sl("cost-1", p1)
check("statusline cost: a second run with nothing new shows the same figures", money(out2) == fig, out2)

# appended in chunks, cut mid-line, between the two lines of a streamed response and just before a newline
main_b, subs_b = session_bytes("k")
ends = [i + 1 for i, ch in enumerate(main_b) if ch == 10]
dup_cut = next(e for e in ends if b'"output_tokens": 12' in main_b[:e] and main_b[:e].endswith(b"}\n"))
cuts = sorted({ends[2], ends[3] + 40, dup_cut, ends[22] - 1, ends[31] + 7, ends[44], ends[52] - 1, len(main_b) * 9 // 10,
               len(main_b)})


def chunked(name, consts):
    path = cdir / f"{name}.jsonl"
    bad = []
    for k, cut in enumerate(cuts):
        frac = (k + 1) / len(cuts)
        with open(path, "ab") as f:  # append only what is new since the last run
            f.write(main_b[path.stat().st_size if path.exists() else 0:cut])
        for rel, data in subs_b.items():
            f2 = cdir / name / rel
            upto = int(len(data) * frac) if k < len(cuts) - 1 else len(data)
            if upto:
                f2.parent.mkdir(parents=True, exist_ok=True)
                with open(f2, "ab") as g:
                    g.write(data[f2.stat().st_size if f2.exists() else 0:upto])
        rc, out, err = cost_sl(name, path, consts)
        ref = reference(path)
        if rc or not agrees(money(out), ref) or not exact(name, ref):
            bad.append(f"cut {cut}: {out.splitlines()[0] if out else err} vs {ref}")
    return path, bad


p2, bad = chunked("cost-2", {})
check("statusline cost: appended chunks match a full read after every chunk", not bad, "\n".join(bad))
rc, out_inc, _ = cost_sl("cost-2", p2)
rc, out_full, _ = cost_sl("cost-2-once", p2)
check("statusline cost: chunked totals = one full read", bool(money(out_inc)) and money(out_inc) == money(out_full)
      and exact("cost-2", reference(p2)), f"{out_inc}\n{out_full}")
p3, bad = chunked("cost-3", {"COST_TAIL": 1, "COST_READ_BLOCK": 97})
check("statusline cost: chunks still match with 1 open request and 97-byte reads", not bad, "\n".join(bad))

p2.write_bytes(main_b[:ends[30]])
rc, out, _ = cost_sl("cost-2", p2)
ref = reference(p2)
check("statusline cost: a transcript that shrank is read again from the start", agrees(money(out), ref)
      and exact("cost-2", ref), f"{out}\n{ref}")
last = main_b[ends[28]:ends[29]].replace(b'"output_tokens": 300', b'"output_tokens": 90000, "pad": "' + b"z" * 300 + b'"')
p2.write_bytes(main_b[:ends[28]] + last + main_b[ends[29]:ends[30]])
rc, out, _ = cost_sl("cost-2", p2)
ref = reference(p2)
check("statusline cost: an offset no longer at a line boundary means a full read", agrees(money(out), ref)
      and exact("cost-2", ref), f"{out}\n{ref}")

# time budget: out of time, the line still prints with the last known figures and catches up on later runs
p5 = write_session("cost-5", *session_bytes("b"))
tiny = {"COST_BUDGET_SECONDS": 0, "COST_READ_BLOCK": 2500}
rc, out, err = cost_sl("cost-5", p5, tiny)
check("statusline cost: over budget on the first run prints the line without figures",
      rc == 0 and "misses 0" in out and "cost $" not in out and "tip:" in out, out + err)
runs = 1
while runs < 80 and not money(out):
    rc, out, err = cost_sl("cost-5", p5, tiny)
    runs += 1
ref5 = reference(p5)
check("statusline cost: catches up over several runs", runs > 2 and agrees(money(out), ref5) and exact("cost-5", ref5),
      f"{runs} runs: {out}\n{ref5}")
before = money(out)
with open(p5, "ab") as f:
    for k in range(12):
        f.write(line(resp(f"extra{k}", NOW - 3600 + k, O, 80000, 0, out=900)))
rc, out, err = cost_sl("cost-5", p5, tiny)
check("statusline cost: still catching up shows the last known figures", money(out) == before, f"{out}\n{before}")
while runs < 160 and money(out) == before:
    rc, out, err = cost_sl("cost-5", p5, tiny)
    runs += 1
ref5 = reference(p5)
check("statusline cost: and then the new ones", agrees(money(out), ref5) and money(out) != before, f"{out}\n{ref5}")

# colour, zero avoidable, unpriced, switched off, errors
cfg_path.write_text(json.dumps({"avoidable_warn_pct": 1}))
rc, out, _ = cost_sl("cost-1", p1, raw=True)
check("statusline cost: avoidable turns yellow above avoidable_warn_pct", f"{YEL_}avoidable $" in out, repr(out))
cfg_path.write_text(json.dumps({"avoidable_warn_pct": 99}))
rc, out, _ = cost_sl("cost-1", p1, raw=True)
check("statusline cost: and stays plain below it", " avoidable $" in out and f"{YEL_}avoidable" not in out, repr(out))
cfg_path.unlink()

warm_only = b"".join(line(resp(f"w{k}", NOW - 3000 + 30 * k, O, 30000 if k == 0 else 1000, 0 if k == 0 else 29000 + 1000 * k))
                     for k in range(6))
p6 = write_session("cost-6", warm_only, {})
rc, out, _ = cost_sl("cost-6", p6)
check("statusline cost: no avoidable rebuilds leaves avoidable out", "cache cut $" in out
      and "avoidable" not in out, out)
p8 = write_session("cost-8", line(resp("s0", NOW - 60, O, 40000, 0)), {})  # one write, no reads: saved < 0
rc, out, _ = cost_sl("cost-8", p8)
check("statusline cost: before the cache pays back it says so",
      "| cost $" in out and "· cache not paid back yet" in out and "cache cut" not in out.splitlines()[0], out)
tips = json.loads(subprocess.run([sys.executable, "-B", "-c", "import sys, json; sys.path.insert(0, sys.argv[1]); "
                                  "import cache_guard as cg; print(json.dumps(cg.TIPS))", str(ROOT)],
                                 capture_output=True, text=True, env=ENV).stdout or "[]")
CUT = ("import sys, json; sys.path.insert(0, sys.argv[1]); import cache_guard as cg; "
       "print(json.dumps([cg.cut_text(*a) for a in json.loads(sys.argv[2])]))")
cut_cases = [(12.4, 48.1), (5.00, 0.01), (100, 0.4), (12.4, 0), (12.4, -0.3)]
got = json.loads(subprocess.run([sys.executable, "-B", "-c", CUT, str(ROOT), json.dumps(cut_cases)], capture_output=True,
                                text=True, env=ENV).stdout or "[]")
check("statusline cost: cache cut wording never says -0%, nor not-paid-back once it has",
      got == ["cache cut $60.50 → $12.40 (-80%)", "cache cut $5.01 → $5.00 (<1%)", "cache cut $100 → $100 (<1%)",
              "cost $12.40", "cost $12.40 · cache not paid back yet"], json.dumps(got))
API = ("import sys, json; sys.path.insert(0, sys.argv[1]); import cache_guard as cg; "
       "print(json.dumps([cg.api_price_text(*a) for a in json.loads(sys.argv[2])]))")
got = json.loads(subprocess.run([sys.executable, "-B", "-c", API, str(ROOT), json.dumps(cut_cases)], capture_output=True,
                                text=True, env=ENV).stdout or "[]")
check("totals: API-price wording leads with the cost, never says -0% or a false not-paid-back",
      got == ["$12.40 (would be $60.50 without cache, -80%)", "$5.00 (would be $5.01 without cache, <1%)",
              "$100 (would be $100 without cache, <1%)", "$12.40", "$12.40 (cache not paid back yet)"], json.dumps(got))
check("statusline cost: a tip explains cache cut", any("cache cut" in t and "5%" in t for t in tips), json.dumps(tips))
p7 = write_session("cost-7", b"".join(line(resp(f"u{k}", NOW - 3000 + 30 * k, MYST, 1000, 30000 * k)) for k in range(4)), {})
rc, out, _ = cost_sl("cost-7", p7)
check("statusline cost: nothing priced leaves the block out", rc == 0 and "cost" not in out.splitlines()[0]
      and "misses 0" in out, out)

cfg_path.write_text(json.dumps({"session_budget_usd": 0.01}))
rc, out, _ = cost_sl("cost-1", p1)
check("budget: the status line says when the session is over budget", "· over $0.01 budget |" in out.splitlines()[0], out)
cfg_path.unlink()

cfg_path.write_text(json.dumps({"statusline_cost": False, "session_budget_usd": 0.01}))
rc, out, _ = cost_sl("cost-bud", p1)
rc2, out2, _ = run(["hook-prompt"], {"session_id": "cost-bud", "prompt": "next", "source": "user"})
check("budget: still guards when the cost display is off", "cost $" not in out.splitlines()[0]
      and "$0.01 budget" in out2, out + out2)
cfg_path.write_text(json.dumps({"statusline_cost": False}))
rc, out, _ = cost_sl("cost-1", p1)
st = json.loads((STATE / "cost-1.json").read_text())
check("statusline cost: statusline_cost=false turns it off", "cost $" not in out.splitlines()[0] and "misses 0" in out
      and "costs" not in st, out)
cfg_path.unlink()

rc, out, err = cost_sl("cost-err", p1, pre="cg.session_costs = lambda *a, **k: 1 / 0")
check("statusline cost: an error prints the line without the block", rc == 0 and "misses 0" in out
      and "cost" not in out.splitlines()[0] and "tip:" in out and not err, out + err)
STATE.joinpath("cost-bad.json").write_text(json.dumps({"transcript_path": str(p1), "costs": {
    "path": str(p1), "prices": None, "main": "junk", "subs": [1], "shown": {"cost": "x"}}}))
rc, out, err = cost_sl("cost-bad", p1)
rc2, out2, _ = cost_sl("cost-bad", p1)
check("statusline cost: a corrupt state never breaks the line and recovers", rc == 0 and rc2 == 0
      and "misses 0" in out and agrees(money(out2), ref1), out + err + out2)

# ---------------- all-projects totals
TOTALS_JSON = HOME / ".claude" / "cache-guard" / "totals.json"
TOTALS_FILES = HOME / ".claude" / "cache-guard" / "totals-files.json"
TOTALS_LOCK = HOME / ".claude" / "cache-guard" / "totals.lock"
PROJ = HOME / ".claude" / "projects"


def tot_session(folder, cwd, rows):
    d = PROJ / folder
    d.mkdir(parents=True, exist_ok=True)
    head = line({"type": "user", "timestamp": iso(NOW - 900), "cwd": cwd,
                 "message": {"role": "user", "content": "hi"}}) if cwd else b""
    p = d / "s1.jsonl"
    p.write_bytes(head + b"".join(line(r) for r in rows))
    return p


def warm_rows(tag, n, size):
    """A session start then n-1 warm turns: each reads the whole prefix and writes 500 new tokens."""
    return [resp(f"{tag}{k}", NOW - 800 + 20 * k, O, size if k == 0 else 500, 0 if k == 0 else size + 500 * (k - 1))
            for k in range(n)]


def refresh(pre="", *extra):
    p = subprocess.run([sys.executable, "-B", "-c", PATCH, str(ROOT), "{}", pre, "totals-refresh", *extra],
                       capture_output=True, text=True, env=ENV, cwd=WORK)
    return p.returncode, p.stdout, p.stderr


COUNT_READS = ("n = [0]\nf = cg.load_requests\n"
               "def counted(*a, **k):\n    n[0] += 1\n    return f(*a, **k)\n"
               "cg.load_requests = counted\nimport atexit\natexit.register(lambda: print('READS', n[0]))\n")


def reads(out):
    m = re.search(r"READS (\d+)", out)
    return int(m.group(1)) if m else -1


def totals_vs_full_read():
    try:
        t = json.loads(TOTALS_JSON.read_text())
    except Exception as e:
        return {}, False, str(e)
    refs = [reference(p) for p in PROJ.glob("*/*.jsonl") if p.stat().st_mtime >= time.time() - 30 * 86400]
    cost, saved = sum(r["cost"] for r in refs), sum(r["saved"] for r in refs)
    ok = abs(t["cost"] - cost) < 1e-6 and abs(t["saved"] - saved) < 1e-6
    return t, ok, f"totals {t.get('cost')}/{t.get('saved')} vs full read {cost}/{saved}"


pa = tot_session("-tot-alpha", "/x/alpha", warm_rows("al", 30, 60000))
pb = tot_session("-tot-beta", "/x/beta", warm_rows("be", 10, 30000))
tot_session("-tot-nocwd", None, warm_rows("nc", 5, 20000))
po = tot_session("-tot-old", "/x/old", warm_rows("ol", 5, 20000))
os.utime(po, (NOW - 40 * 86400, NOW - 40 * 86400))

rc, out, err = refresh()
t, ok, why = totals_vs_full_read()
check("totals: refresh matches a full read of every session", rc == 0 and ok, f"{out}{err}\n{why}")
names = [x.get("name") for x in t.get("projects", [])]
check("totals: projects named after the last folder of cwd, most saved first",
      "alpha" in names and "beta" in names and names.index("alpha") < names.index("beta"), json.dumps(t)[:600])
check("totals: a project with no cwd uses its folder name", "tot-nocwd" in names, json.dumps(names))
check("totals: sessions older than totals_days are left out",
      "-tot-old" not in [x.get("key") for x in t.get("projects", [])] and str(po) not in TOTALS_FILES.read_text()
      if TOTALS_FILES.exists() else False, json.dumps(t)[:600])

rc, out, err = refresh(COUNT_READS)
check("totals: an unchanged second scan re-reads nothing", rc == 0 and reads(out) == 0, out + err)
with open(pb, "ab") as f:
    f.write(line(resp("be-extra", NOW - 10, O, 500, 35000)))
rc, out, err = refresh(COUNT_READS)
t, ok, why = totals_vs_full_read()
check("totals: appending to one session re-reads only that file", rc == 0 and reads(out) == 1 and ok,
      f"{out}{err}\n{why}")

extra = line(resp("al-extra", NOW - 5, O, 500, 75000))
before = t.get("cost")
with open(pa, "ab") as f:
    f.write(extra[:len(extra) // 2])
rc, out, err = refresh()
t, ok, why = totals_vs_full_read()
check("totals: a half-written last line is skipped", rc == 0 and ok and t.get("cost") == before, f"{out}{err}\n{why}")
with open(pa, "ab") as f:
    f.write(extra[len(extra) // 2:])
rc, out, err = refresh()
t, ok, why = totals_vs_full_read()
check("totals: and counted once it is complete", rc == 0 and ok and t.get("cost", 0) > before, f"{out}{err}\n{why}")

TOTALS_LOCK.write_text("other")
rc, out, err = refresh("", "--lock", "mine")
check("totals: a scan leaves a lock that isn't its own", rc == 0 and TOTALS_LOCK.read_text() == "other", out + err)
TOTALS_LOCK.write_text("mine")
rc, out, err = refresh("", "--lock", "mine")
check("totals: a scan removes its own lock when done", rc == 0 and not TOTALS_LOCK.exists(), out + err)

TOTALS_LOCK.write_text("tok")
os.utime(TOTALS_LOCK, (NOW - 400, NOW - 400))
with open(pb, "ab") as f:
    f.write(line(resp("be-beat", NOW - 3, O, 500, 36000)))
SEEN_LOCK = ("seen = []\nff = cg.file_figures\n"
             "def watched(*a, **k):\n    seen.append(cg.TOTALS_LOCK.stat().st_mtime)\n    return ff(*a, **k)\n"
             "cg.file_figures = watched\nimport atexit\natexit.register(lambda: print('LOCKAGE', time.time() - max(seen)))\n"
             "import time\n")
rc, out, err = refresh(SEEN_LOCK, "--lock", "tok")
age = float(re.search(r"LOCKAGE (\S+)", out).group(1)) if "LOCKAGE" in out else 1e9
check("totals: a running scan keeps its lock fresh", rc == 0 and age < 60, f"{age}\n{out}{err}")
TOTALS_LOCK.write_text("tok")
rc, out, err = refresh("cg.scan_totals = lambda *a, **k: 1 / 0", "--lock", "tok")
check("totals: a failed scan keeps its lock, so it waits until the lock is stale",
      rc != 0 and TOTALS_LOCK.exists() and TOTALS_LOCK.read_text() == "tok", out + err)
TOTALS_LOCK.unlink()
refresh()

# the all-projects row
def fmt(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:.2f}"


def wait_for(cond, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


tot_session("-tot-long", "/x/" + "a" * 40, [resp("lo0", NOW - 700, O, 1000, 0), resp("lo1", NOW - 680, O, 500, 300_000_000)])
refresh()
t = json.loads(TOTALS_JSON.read_text())
rc, out, err = sl("tot-1", prompt_cache=pc())
lines = out.splitlines()
row = lines[1] if len(lines) == 3 else ""
check("totals: row sits between the cache line and the tip", rc == 0 and len(lines) == 3 and lines[2].startswith("tip:")
      and re.fullmatch(r"30d at API prices: \$[\d,.]+ \(would be \$[\d,.]+ without cache, -\d+%\) \| most saved: .+", row) is not None,
      out + err)
check("totals: row figures are the scan's", f"API prices: {fmt(t['cost'])} (would be {fmt(t['cost'] + t['saved'])} without cache, " in row,
      f"{row}\n{t['cost']} {t['saved']}")
top = row.split("most saved: ")[-1]
want = [x["name"] for x in sorted(t["projects"], key=lambda x: -x["saved"]) if x["saved"] > 0][:3]
check("totals: top 3 only, most saved first, compact amounts", top.count(" · ") == 2 and want[:2] == ["a" * 40, "alpha"]
      and re.fullmatch(r"a{23}… \$[\d.]+k · alpha \$[\d.]+ · " + re.escape(want[2]) + r" \$[\d.]+", top) is not None,
      f"{row}\n{want}")
check("totals: long project names are cut to 24 characters", "a" * 23 + "… " in row and "a" * 24 not in row, row)
rc, out, err = sl("tot-2")
check("totals: also shown while waiting for the first response", rc == 0 and len(out.splitlines()) == 3
      and out.splitlines()[1].startswith("30d at API prices: $"), out + err)

TOTALS_JSON.rename(TOTALS_JSON.with_name("totals.bak"))
rc, out, err = sl("tot-3", prompt_cache=pc())
check("totals: before the first scan it says counting…", rc == 0 and out.splitlines()[1:2] == ["30d at API prices: counting…"],
      out + err)
TOTALS_JSON.write_text("{junk")
rc, out, err = sl("tot-4", prompt_cache=pc())
check("totals: a corrupt totals.json never breaks the line", rc == 0 and "misses 0" in out and "tip:" in out and not err,
      out + err)
TOTALS_JSON.with_name("totals.bak").replace(TOTALS_JSON)

home2 = Path(tempfile.mkdtemp(prefix="cg-home2-"))
env2 = {**ENV, "HOME": str(home2)}
(home2 / ".claude" / "projects" / "-u").mkdir(parents=True)
(home2 / ".claude" / "projects" / "-u" / "s1.jsonl").write_bytes(
    b"".join(line(resp(f"u{k}", NOW - 600 + 20 * k, MYST, 1000, 30000 * k)) for k in range(4)))
rc1, _, _ = run(["totals-refresh"], env=env2)
rc, out, err = sl("tot-5", prompt_cache=pc(), env=env2)
check("totals: nothing priced leaves the row out", rc1 == 0 and rc == 0
      and (home2 / ".claude" / "cache-guard" / "totals.json").exists() and "API prices" not in out, out + err)

cfg_path.write_text(json.dumps({"statusline_totals": False}))
TOTALS_JSON.unlink(missing_ok=True)
rc, out, err = sl("tot-6", prompt_cache=pc(), env=SPAWN_ENV)
time.sleep(1.5)
check("totals: statusline_totals=false leaves the row out and starts nothing", rc == 0 and "API prices" not in out
      and not TOTALS_JSON.exists() and not TOTALS_LOCK.exists(), out + err)
cfg_path.unlink()

t0 = time.time()
rc, out, err = sl("tot-7", prompt_cache=pc(), env=SPAWN_ENV)
took = time.time() - t0
check("totals: a stale result starts one background scan", rc == 0 and took < 3 and "counting…" in out
      and wait_for(TOTALS_JSON.exists) and wait_for(lambda: not TOTALS_LOCK.exists()), f"{took:.1f}s\n{out}{err}")

TOTALS_JSON.unlink(missing_ok=True)
TOTALS_LOCK.write_text("")
rc, out, err = sl("tot-8", prompt_cache=pc(), env=SPAWN_ENV)
time.sleep(2)
check("totals: a fresh lock stops a scan", rc == 0 and not TOTALS_JSON.exists() and TOTALS_LOCK.exists(), out + err)
os.utime(TOTALS_LOCK, (NOW - 360, NOW - 360))
rc, out, err = sl("tot-9", prompt_cache=pc(), env=SPAWN_ENV)
check("totals: a lock older than 5 minutes is taken over", rc == 0 and wait_for(TOTALS_JSON.exists)
      and wait_for(lambda: not TOTALS_LOCK.exists()), out + err)

TOTALS_JSON.unlink(missing_ok=True)
spawn_log = WORK / "spawns.log"
stub = (f"import pathlib, json\ncg.subprocess.Popen = lambda *a, **k: "
        f"pathlib.Path({str(spawn_log)!r}).open('a').write(json.dumps(a[0]) + '\\n')\n")
payload = json.dumps({"session_id": "tot-par", "model": OPUS, "prompt_cache": pc()})
procs = [subprocess.Popen([sys.executable, "-B", "-c", PATCH, str(ROOT), "{}", stub, "statusline"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=SPAWN_ENV, cwd=WORK)
         for _ in range(4)]
for p_ in procs:
    p_.stdin.write(payload)
    p_.stdin.close()
codes = [p_.wait() for p_ in procs]
spawns = len(spawn_log.read_text().splitlines()) if spawn_log.exists() else 0
check("totals: four status lines at once start one scan", codes == [0] * 4 and spawns == 1, f"{codes} {spawns}")
argv = json.loads(spawn_log.read_text().splitlines()[0]) if spawns else []
token = TOTALS_LOCK.read_text() if TOTALS_LOCK.exists() else ""
check("totals: the scan is handed the lock's token", len(token) >= 8 and argv[-2:] == ["--lock", token]
      and oct(TOTALS_LOCK.stat().st_mode & 0o777) == oct(0o644), f"{argv} {token!r}")
TOTALS_LOCK.unlink(missing_ok=True)
refresh()

# ---------------- trim hook
big = "\n".join([f"line {i}" for i in range(3000)] + ["ERROR: something broke at foo.py:12"] +
                [f"tail {i}" for i in range(100)])
rc, out, _ = run(["hook-trim"], {"session_id": "a", "tool_name": "Bash", "tool_input": {"command": "pytest"},
                                  "tool_response": {"stdout": big, "stderr": "", "interrupted": False, "isImage": False}})
j = json.loads(out) if out.strip() else {}
upd = j.get("hookSpecificOutput", {}).get("updatedToolOutput", {})
check("trim: object output keeps shape", isinstance(upd, dict) and set(upd) == {"stdout", "stderr", "interrupted", "isImage"}, out[:300])
s = upd.get("stdout", "") if isinstance(upd, dict) else ""
check("trim: keeps head, error, tail, and is much smaller", "line 0" in s and "ERROR: something broke" in s
      and "tail 99" in s and len(s) < len(big) / 4, s[:500])
log = re.search(r"Full output saved to (\S+?)\.( |$)", s) or re.search(r"saved to (\S+\.log)", s)
check("trim: full log saved", bool(log) and Path(log.group(1) if log.group(1).endswith(".log") else log.group(1) + "").exists(), s[:400])
rc, out, _ = run(["hook-trim"], {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                  "tool_response": {"stdout": "a\nb", "stderr": ""}})
check("trim: small output untouched", out.strip() == "", out)
rc, out, _ = run(["hook-trim"], {"tool_name": "Bash", "tool_input": {"command": "cat big.log # no-trim"},
                                  "tool_response": {"stdout": big, "stderr": ""}})
check("trim: # no-trim escape hatch", out.strip() == "", out[:200])
rc, out, _ = run(["hook-trim"], {"tool_name": "Bash", "tool_input": {"command": "x"}, "tool_response": "x" * 50000})
j = json.loads(out) if out.strip() else {}
check("trim: string output, long single line cut by chars",
      isinstance(j.get("hookSpecificOutput", {}).get("updatedToolOutput"), str)
      and len(j["hookSpecificOutput"]["updatedToolOutput"]) < 20000, out[:200])

# ---------------- subagent cost guard
sdir = HOME / ".claude" / "projects" / "-sub-tests"
(sdir / "s1" / "subagents").mkdir(parents=True, exist_ok=True)
(sdir / "s1.jsonl").write_bytes(line(resp("m1", NOW - 100, O, 1000, 0)))
(sdir / "s1" / "subagents" / "agent-ax1.jsonl").write_bytes(
    line(resp("x1", NOW - 90, O, 1000000, 0, side=True)) + line(resp("x2", NOW - 80, O, 1000, 1000000, side=True)))
(sdir / "s1" / "subagents" / "agent-ax2.jsonl").write_bytes(line(resp("y1", NOW - 90, O, 2000, 0, side=True)))
stop = {"session_id": "s1", "transcript_path": str(sdir / "s1.jsonl"), "hook_event_name": "SubagentStop"}
rc, out, err = run(["hook-subagent-stop"], dict(stop, agent_id="ax1", agent_type="general-purpose"))
try:
    msg = (json.loads(out) if out.strip() else {}).get("systemMessage", "")
except ValueError:
    msg = ""
check("subagent guard: an expensive run tells you its cost and how to make subagents cheaper", rc == 0
      and "general-purpose" in msg and "$8." in msg and "CLAUDE_CODE_SUBAGENT_MODEL" in msg
      and "additionalContext" not in out, out + err)
rc, out, err = run(["hook-subagent-stop"], dict(stop, agent_id="ax1", agent_type="general-purpose"))
check("subagent guard: an agent continued later isn't warned again until it passes the next multiple",
      rc == 0 and not out.strip(), out + err)
(sdir / "elsewhere").mkdir(exist_ok=True)
(sdir / "elsewhere" / "sonnet-run.jsonl").write_bytes(
    line(resp("z1", NOW - 90, S, 3000000, 0, side=True)))
rc, out, err = run(["hook-subagent-stop"], dict(stop, agent_id="az1", agent_type="general-purpose",
                                               agent_transcript_path=str(sdir / "elsewhere" / "sonnet-run.jsonl")))
msg = (json.loads(out) if out.strip().startswith("{") else {}).get("systemMessage", "")
check("subagent guard: uses agent_transcript_path, and skips the model advice when the run was already cheap",
      "$" in msg and "CLAUDE_CODE_SUBAGENT_MODEL" not in msg, out + err)
rc, out, err = run(["hook-subagent-stop"], dict(stop, agent_id="ax2", agent_type="Explore"))
check("subagent guard: a cheap run is silent", rc == 0 and not out.strip(), out + err)
rc, out, err = run(["hook-subagent-stop"], dict(stop, agent_id="nope", agent_type="Explore"))
check("subagent guard: a missing transcript is silent", rc == 0 and not out.strip() and not err.strip(), out + err)

# ---------------- install / doctor / uninstall
settings = HOME / ".claude" / "settings.json"
settings.write_text(json.dumps({
    "model": "opusplan",
    "env": {"FORCE_PROMPT_CACHING_5M": "1"},
    "statusLine": {"type": "command", "command": "~/old.sh"},
    "hooks": {
        "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "echo done"}]}],
        "PreModelSwitch": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 \"/x/cache_guard.py\" pre-model-switch"}]}],
        "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": f"bash {WORK}/inject.sh"}]}],
    }}))
(WORK / "inject.sh").write_text('echo \'{"hookSpecificOutput":{"additionalContext":"hi"}}\'\n')
(WORK / "CLAUDE.md").write_text("x" * 30000)

rc, out, err = run(["install", "--trim"])
s = json.loads(settings.read_text())
h = s["hooks"]
check("install: ok", rc == 0, out + err)
check("install: status line set", "cache_guard.py\" statusline" in s["statusLine"]["command"] and s["statusLine"]["refreshInterval"] == 30, json.dumps(s["statusLine"]))
check("install: v1 hook upgraded in place", len(h["PreModelSwitch"]) == 1 and "hook-model-switch" in h["PreModelSwitch"][0]["hooks"][0]["command"], json.dumps(h["PreModelSwitch"]))
check("install: compact/session/trim hooks", "hook-compact" in json.dumps(h["PreCompact"]) and "hook-session-start" in json.dumps(h["SessionStart"])
      and "hook-trim" in json.dumps(h["PostToolUse"]), json.dumps(h))
check("install: cold-message hook", "hook-prompt" in json.dumps(h["UserPromptSubmit"]), json.dumps(h))
check("install: subagent cost guard", "hook-subagent-stop" in json.dumps(h.get("SubagentStop", [])), json.dumps(h))
check("install: config gets the new defaults", json.loads(cfg_path.read_text()).get("cold_prompt") == "warn"
      and json.loads(cfg_path.read_text()).get("statusline_cost") is True
      and {k: json.loads(cfg_path.read_text()).get(k) for k in ("statusline_totals", "totals_days", "totals_refresh_minutes")}
      == {"statusline_totals": True, "totals_days": 30, "totals_refresh_minutes": 10}, cfg_path.read_text())
check("install: user hooks preserved", h["Stop"][0]["hooks"][0]["command"] == "echo done" and any("inject.sh" in json.dumps(g) for g in h["PostToolUse"]), json.dumps(h))
check("install: style copied", (HOME / ".claude" / "output-styles" / "lean.md").exists())
check("install: backup written", any(p.name.startswith("settings.json.bak-") for p in settings.parent.iterdir()))
c = json.loads(cfg_path.read_text())
c["cold_prompt"] = "block"
cfg_path.write_text(json.dumps(c))
rc, out, err = run(["install"])
check("install: re-run keeps trimmer, no duplicates", sum("hook-trim" in json.dumps(g) for g in json.loads(settings.read_text())["hooks"]["PostToolUse"]) == 1)
check("install: re-run keeps a cold_prompt you set", json.loads(cfg_path.read_text()).get("cold_prompt") == "block", cfg_path.read_text())
rc, out, err = run(["install", "--no-trim", "--lean"])
s = json.loads(settings.read_text())
check("install: --no-trim removes only ours, --lean sets style", "hook-trim" not in json.dumps(s["hooks"].get("PostToolUse", []))
      and "inject.sh" in json.dumps(s["hooks"]["PostToolUse"]) and s.get("outputStyle") == "Lean", json.dumps(s))
rc, out, err = run(["install", "--all"])
s = json.loads(settings.read_text())
check("install: --all turns on alerts and Lean, never the trimmer", rc == 0
      and "hook-trim" not in json.dumps(s["hooks"].get("PostToolUse", [])) and "Bash output trimmer (off; add --trim)" in out
      and s.get("outputStyle") == "Lean" and json.loads(cfg_path.read_text()).get("notify") is True, out + json.dumps(s))

rc, out, err = run(["install", "--keepwarm"])
s = json.loads(settings.read_text())
kh = [h for g in s["hooks"].get("Stop", []) for h in g["hooks"] if "hook-keepwarm" in h["command"]]
check("install: --keepwarm wires a background Stop hook that can wake Claude", rc == 0 and len(kh) == 1
      and "other Stop hooks" in out
      and kh[0].get("async") is True and kh[0].get("asyncRewake") is True and kh[0].get("timeout", 0) >= 43200
      and json.loads(cfg_path.read_text()).get("keepwarm") is True and "keep-warm" in out, out + json.dumps(s["hooks"]))
rc, out, err = run(["install"])
s = json.loads(settings.read_text())
check("install: re-run keeps keep-warm", sum("hook-keepwarm" in json.dumps(g) for g in s["hooks"].get("Stop", [])) == 1
      and json.loads(cfg_path.read_text()).get("keepwarm") is True, json.dumps(s["hooks"]))
rc, out, err = run(["install", "--no-keepwarm"])
s = json.loads(settings.read_text())
check("install: --no-keepwarm removes it and keeps your own Stop hooks", "hook-keepwarm" not in json.dumps(s["hooks"])
      and "echo done" in json.dumps(s["hooks"]["Stop"]) and json.loads(cfg_path.read_text()).get("keepwarm") is False,
      json.dumps(s["hooks"]))

CMDS = HOME / ".claude" / "commands"
rc, out, err = run(["install", "--handoff"])
s = json.loads(settings.read_text())
c = json.loads(cfg_path.read_text())
load_cmd = (CMDS / "handoff-load.md").read_text() if (CMDS / "handoff-load.md").exists() else ""
save_cmd = (CMDS / "handoff-save.md").read_text() if (CMDS / "handoff-save.md").exists() else ""
check("install: --handoff turns it on, wires the Stop hook even with keep-warm off, and adds the two commands",
      rc == 0 and c.get("handoff") is True and c.get("keepwarm") is False and "hook-keepwarm" in json.dumps(s["hooks"])
      and "handoff-show" in load_cmd and "allowed-tools" in load_cmd and str(HOME / ".claude" / "cache-guard") in load_cmd
      and "# cache-guard handoff" in save_cmd and "handoff" in out, out + json.dumps(s["hooks"]))
check("install: says keep-warm is still off", "· keep-warm" in out, out)
rc, out, err = run(["install"])
check("install: re-run keeps the handoff", json.loads(cfg_path.read_text()).get("handoff") is True
      and (CMDS / "handoff-load.md").exists() and "hook-keepwarm" in settings.read_text(), out)
rc, out, err = run(["install", "--no-handoff"])
check("install: --no-handoff removes the commands and the Stop hook when keep-warm is off",
      json.loads(cfg_path.read_text()).get("handoff") is False and not (CMDS / "handoff-load.md").exists()
      and not (CMDS / "handoff-save.md").exists() and "hook-keepwarm" not in settings.read_text(), out)
CMDS.mkdir(parents=True, exist_ok=True)
(CMDS / "handoff-save.md").write_text("my own command\n")
rc, out, err = run(["install", "--handoff"])
check("install: never overwrites a command of yours with the same name",
      (CMDS / "handoff-save.md").read_text() == "my own command\n" and "handoff-save.md" in out, out)
run(["install", "--no-handoff"])
check("install: and never removes it either", (CMDS / "handoff-save.md").read_text() == "my own command\n", "")
(CMDS / "handoff-save.md").unlink()

rc, out, err = run(["doctor"])
check("doctor: runs", rc == 0, out + err)
check("doctor: flags opusplan, forced 5m, injecting hook, big CLAUDE.md",
      all(x in out for x in ("opusplan", "FORCE_PROMPT_CACHING_5M", "returns additionalContext", "CLAUDE.md is ~8k")), out)

W2 = Path(tempfile.mkdtemp(prefix="cg-work2-")).resolve()
(W2 / "CLAUDE.md").write_text("Read @docs/guide.md for more. Mail me at me@example.com, see `@not/this.md`.\n"
                              "password = hunter2hunter2\n" + "y" * 8000)
(W2 / "docs").mkdir()
(W2 / "docs" / "guide.md").write_text("z" * 20000 + "\nsee @nested.md\n")
(W2 / "docs" / "nested.md").write_text("n" * 4000)
(W2 / ".claude" / "rules").mkdir(parents=True)
(W2 / ".claude" / "rules" / "always.md").write_text("r" * 30000)
(W2 / ".claude" / "rules" / "scoped.md").write_text("---\npaths: src/**\n---\n" + "s" * 40000)
mem = HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(W2)) / "memory"
mem.mkdir(parents=True)
fake_key = "sk-ant-api03-" + "A1b2" * 10
(mem / "MEMORY.md").write_text("m" * 8000 + "\n- notes\nAPI key: " + fake_key + "\n")
env_x = {**ENV, "NODE_OPTIONS": "--import /opt/rewriter/preload.mjs", "HTTPS_PROXY": "http://127.0.0.1:9801",
         "NODE_EXTRA_CA_CERTS": "/opt/rewriter/ca.pem", "ANTHROPIC_BASE_URL": "http://localhost:9801"}
rc, out, err = run(["doctor"], cwd=W2, env=env_x)
check("doctor: adds up everything loaded every session", rc == 0
      and re.search(r"Always loaded every session: ~1[5-9]k tokens \(CLAUDE\.md ~2k, @imports ~6k, auto memory ~2k, "
                    r"rules ~8k\)", out) is not None, out + err)
check("doctor: flags secrets by file and line without printing them",
      f"{mem / 'MEMORY.md'}:3 looks like an Anthropic API key" in out and f"{W2 / 'CLAUDE.md'}:2 looks like a password"
      in out and fake_key not in out and "hunter2" not in out, out)
check("doctor: flags request interceptors", all(x in out for x in (
    "NODE_OPTIONS preloads code into Claude Code", "proxy that holds its own certificate",
    "ANTHROPIC_BASE_URL=http://localhost:9801 points at a local proxy")), out)
(W2 / ".claude" / "rules" / "body.md").write_text("---\ndescription: x\n---\npaths: are mentioned here\ntoken "
                                                   "ghp_" + "Z9" * 18 + "\n")
(W2 / "CLAUDE.local.md").write_text("DB password: ${DB_PASSWORD}\napiKey: process.env.OPENAI_API_KEY\n"
                                    "Run `pwd: /Users/x/proj` first\nsecret = os.environ['APP_SECRET_VALUE']\n")
env_p = {**ENV, "HTTPS_PROXY": "http://jdoe:S3cretPassw0rd@proxy.example:8080", "NODE_EXTRA_CA_CERTS": "/x/ca.pem",
         "ANTHROPIC_BASE_URL": "http://bob:Hunter2Hunter2@localhost:9801"}
rc, out, err = run(["doctor"], cwd=W2, env=env_p)
check("doctor: never prints a password in a proxy or base URL", rc == 0 and "S3cretPassw0rd" not in out
      and "Hunter2Hunter2" not in out and "proxy.example:8080" in out and "localhost:9801" in out, out)
check("doctor: a paths: line in a rule's body doesn't scope it", f"{W2 / '.claude' / 'rules' / 'body.md'}:5 looks like a "
      "GitHub token" in out, out)
check("doctor: references to env vars are not secrets", f"{W2 / 'CLAUDE.local.md'}:" not in out, out)
rc, out, err = run(["doctor"], cwd=WORK)
check("doctor: says subagents run on the session's model when CLAUDE_CODE_SUBAGENT_MODEL isn't set",
      "CLAUDE_CODE_SUBAGENT_MODEL" in out and "Explore" in out, out)
(W2 / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-haiku-5-5"}))
rc3, out3, _ = run(["doctor"], cwd=W2)
(W2 / ".claude" / "settings.json").unlink()
check("doctor: no subagent-model note when the session model is already cheap",
      "CLAUDE_CODE_SUBAGENT_MODEL" not in out3, out3)
rc2, out2, _ = run(["doctor"], cwd=WORK, env={**ENV, "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"})
check("doctor: and is quiet about it once it is set", "CLAUDE_CODE_SUBAGENT_MODEL" not in out2, out2)
check("doctor: a clean environment has no interceptor or secret warnings", rc == 0 and "preloads code" not in out
      and "looks like" not in out and "local proxy" not in out, out)

run(["install", "--keepwarm", "--handoff"])
rc, out, err = run(["uninstall"])
check("uninstall: removes the handoff commands and turns it off for running sessions",
      not (HOME / ".claude" / "commands" / "handoff-load.md").exists()
      and json.loads(cfg_path.read_text()).get("handoff") is False, out)
check("uninstall: turns keep-warm off for sessions still running the old hook",
      json.loads(cfg_path.read_text()).get("keepwarm") is False, cfg_path.read_text())
s = json.loads(settings.read_text())
check("uninstall: removes ours, keeps user's", "cache_guard" not in json.dumps(s) and "echo done" in json.dumps(s)
      and "inject.sh" in json.dumps(s) and "outputStyle" not in s, json.dumps(s))

print(f"\n{'All tests passed' if not failures else f'{failures} test(s) FAILED'}  (HOME={HOME})")
sys.exit(1 if failures else 0)
