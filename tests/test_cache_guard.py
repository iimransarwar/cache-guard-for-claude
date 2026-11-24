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
HOME = Path(tempfile.mkdtemp(prefix="cg-home-"))
WORK = Path(tempfile.mkdtemp(prefix="cg-work-"))
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith(("BASH_MAX", "MAX_MCP"))}, "HOME": str(HOME)}
NOW = int(time.time())
failures = 0


def run(args, payload=None, cwd=WORK, raw=False):
    p = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(payload) if payload is not None else "",
                       capture_output=True, text=True, env=ENV, cwd=cwd)
    return p.returncode, (p.stdout if raw else ANSI.sub("", p.stdout)), ANSI.sub("", p.stderr)


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


def sl(sid, raw=False, **extra):
    d = {"session_id": sid, "model": {"display_name": "Opus 5.5"}}
    d.update(extra)
    return run(["statusline"], d, raw=raw)


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
check("limits: colours follow how much is used", "Fable: \x1b[31m9% left\x1b[0m" in out
      and "All models: \x1b[33m15% left\x1b[0m" in out, repr(out))
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
cfg_path.write_text(json.dumps({"pricing": {"claude-opus-5-5": [40, 50, 80, 2, 200]}}))
rc, out, err = run(["report", "sess-2"])
check("report: prices can be overridden in config.json", "about $13.12" in out, out + err)
cfg_path.unlink()

# ---------------- status line: session cost, avoidable cost and cache savings
STATE = HOME / ".claude" / "cache-guard" / "state"
cdir = HOME / ".claude" / "projects" / "-cost-tests"  # its own folder, so the report tests above are unaffected
cdir.mkdir(parents=True, exist_ok=True)
O, S, H, MYST = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-mystery-1"
MONEY = re.compile(r"\| cost (\$[\d,.]+)(?: · avoidable (\$[\d,.]+))? · saved (-?\$[\d,.]+) \|")

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
PATCH = ("import json, sys; sys.path.insert(0, sys.argv[1]); import cache_guard as cg\n"
         "for k, v in json.loads(sys.argv[2]).items(): setattr(cg, k, v)\n"
         "exec(sys.argv[3])\n"
         "sys.argv = ['cache_guard.py'] + sys.argv[4:]\n"
         "cg.main()")


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
    return m and {"cost": usd(m.group(1)), "avoidable": usd(m.group(2)), "saved": usd(m.group(3)), "text": m.group(0)}


def agrees(fig, ref):
    """Printed figures match a full read to the cent (fmt_usd rounds to cents below $100)."""
    return bool(fig) and all(abs(fig[k] - ref[k]) <= 0.0051 for k in ("cost", "avoidable", "saved"))


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
      and re.search(r"\(warm, \d+m left, ttl 1h\) \| cost \$[\d.]+ · avoidable \$[\d.]+ · saved -?\$[\d.]+ \| misses 0",
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
check("statusline cost: avoidable turns yellow above avoidable_warn_pct", "\x1b[33mavoidable $" in out, repr(out))
cfg_path.write_text(json.dumps({"avoidable_warn_pct": 99}))
rc, out, _ = cost_sl("cost-1", p1, raw=True)
check("statusline cost: and stays plain below it", " avoidable $" in out and "\x1b[33mavoidable" not in out, repr(out))
cfg_path.unlink()

warm_only = b"".join(line(resp(f"w{k}", NOW - 3000 + 30 * k, O, 30000 if k == 0 else 1000, 0 if k == 0 else 29000 + 1000 * k))
                     for k in range(6))
p6 = write_session("cost-6", warm_only, {})
rc, out, _ = cost_sl("cost-6", p6)
check("statusline cost: no avoidable rebuilds leaves avoidable out", "cost $" in out and "saved $" in out
      and "avoidable" not in out, out)
p7 = write_session("cost-7", b"".join(line(resp(f"u{k}", NOW - 3000 + 30 * k, MYST, 1000, 30000 * k)) for k in range(4)), {})
rc, out, _ = cost_sl("cost-7", p7)
check("statusline cost: nothing priced leaves the block out", rc == 0 and "cost" not in out and "misses 0" in out, out)

cfg_path.write_text(json.dumps({"statusline_cost": False}))
rc, out, _ = cost_sl("cost-1", p1)
st = json.loads((STATE / "cost-1.json").read_text())
check("statusline cost: statusline_cost=false turns it off", "cost $" not in out and "misses 0" in out
      and "costs" not in st, out)
cfg_path.unlink()

rc, out, err = cost_sl("cost-err", p1, pre="cg.session_costs = lambda *a, **k: 1 / 0")
check("statusline cost: an error prints the line without the block", rc == 0 and "misses 0" in out
      and "cost" not in out and "tip:" in out and not err, out + err)
STATE.joinpath("cost-bad.json").write_text(json.dumps({"transcript_path": str(p1), "costs": {
    "path": str(p1), "prices": None, "main": "junk", "subs": [1], "shown": {"cost": "x"}}}))
rc, out, err = cost_sl("cost-bad", p1)
rc2, out2, _ = cost_sl("cost-bad", p1)
check("statusline cost: a corrupt state never breaks the line and recovers", rc == 0 and rc2 == 0
      and "misses 0" in out and agrees(money(out2), ref1), out + err + out2)

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
check("install: config gets the new defaults", json.loads(cfg_path.read_text()).get("cold_prompt") == "warn"
      and json.loads(cfg_path.read_text()).get("statusline_cost") is True, cfg_path.read_text())
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

rc, out, err = run(["doctor"])
check("doctor: runs", rc == 0, out + err)
check("doctor: flags opusplan, forced 5m, injecting hook, big CLAUDE.md",
      all(x in out for x in ("opusplan", "FORCE_PROMPT_CACHING_5M", "returns additionalContext", "CLAUDE.md is ~8k")), out)

rc, out, err = run(["uninstall"])
s = json.loads(settings.read_text())
check("uninstall: removes ours, keeps user's", "cache_guard" not in json.dumps(s) and "echo done" in json.dumps(s)
      and "inject.sh" in json.dumps(s) and "outputStyle" not in s, json.dumps(s))

print(f"\n{'All tests passed' if not failures else f'{failures} test(s) FAILED'}  (HOME={HOME})")
sys.exit(1 if failures else 0)
