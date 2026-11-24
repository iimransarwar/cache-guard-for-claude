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


def run(args, payload=None, cwd=WORK):
    p = subprocess.run([sys.executable, str(SCRIPT), *args], input=json.dumps(payload) if payload is not None else "",
                       capture_output=True, text=True, env=ENV, cwd=cwd)
    return p.returncode, ANSI.sub("", p.stdout), ANSI.sub("", p.stderr)


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


def sl(sid, **extra):
    d = {"session_id": sid, "model": {"display_name": "Opus 5.5"}}
    d.update(extra)
    return run(["statusline"], d)


# ---------------- status line
rc, out, _ = sl("a")
check("statusline: before first response", rc == 0 and "waiting for first response" in out and "tip:" in out, out)

rc, out, _ = sl("a", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
                                                       "seven_day": {"used_percentage": 18, "resets_at": NOW + 86400}})
check("statusline: warm with limits", "cache 95%" in out and "warm, 39m left" in out and "5h 42%" in out and "7d 18%" in out, out)

rc, out, _ = sl("a", prompt_cache=pc(misses=1, last_miss_cause={"causes": ["tools_changed"]}))
check("statusline: new miss shows cause tip", "tool definitions changed" in out, out)

rc, out, _ = sl("b", prompt_cache=pc(ttl="5m", expires_at=NOW + 100))
check("statusline: expiring warning", "Cache expires in 2m" in out, out)

rc, out, _ = sl("c", prompt_cache=pc(), rate_limits={"five_hour": {"used_percentage": 86, "resets_at": NOW + 3600}})
check("statusline: usage-limit tip", "5-hour limit at 86%" in out and "resets" in out, out)

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
rc, out, _ = run(["hook-prompt"], {"session_id": "p", "prompt": "fix the bug", "source": "user"})
j = json.loads(out) if out.strip() else {}
check("cold message: typed prompt blocked once with size, cost and age",
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

cfg_path = HOME / ".claude" / "cache-guard" / "config.json"
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
check("install: config gets the new defaults", json.loads(cfg_path.read_text()).get("cold_prompt") == "block", cfg_path.read_text())
check("install: user hooks preserved", h["Stop"][0]["hooks"][0]["command"] == "echo done" and any("inject.sh" in json.dumps(g) for g in h["PostToolUse"]), json.dumps(h))
check("install: style copied", (HOME / ".claude" / "output-styles" / "lean.md").exists())
check("install: backup written", any(p.name.startswith("settings.json.bak-") for p in settings.parent.iterdir()))
rc, out, err = run(["install"])
check("install: re-run keeps trimmer, no duplicates", sum("hook-trim" in json.dumps(g) for g in json.loads(settings.read_text())["hooks"]["PostToolUse"]) == 1)
rc, out, err = run(["install", "--no-trim", "--lean"])
s = json.loads(settings.read_text())
check("install: --no-trim removes only ours, --lean sets style", "hook-trim" not in json.dumps(s["hooks"].get("PostToolUse", []))
      and "inject.sh" in json.dumps(s["hooks"]["PostToolUse"]) and s.get("outputStyle") == "Lean", json.dumps(s))

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
