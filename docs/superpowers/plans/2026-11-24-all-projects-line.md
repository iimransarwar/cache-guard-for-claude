# All-projects line Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a status-line row with 30-day cost with and without the cache across all projects plus the top 3 projects by saving, and reword the session figure as "cache cut $A → $B".

**Architecture:** A new `totals-refresh` command scans every transcript (same set as `report --all`), keeps per-file figures so later scans re-read only changed files, and writes `totals.json`. The status line only reads that file; when it is stale it takes a lock and starts the scan detached, so a status-line run never waits on it.

**Tech Stack:** Python 3.8+ stdlib only, single file `cache_guard.py`; tests are the home-grown `check()` suite in `tests/test_cache_guard.py` run against a throwaway HOME.

**Spec:** `docs/superpowers/specs/2026-11-24-all-projects-line-design.md`

## Global Constraints

- Python 3.8+, standard library only. No `str.removeprefix`, no `list[int]`-style runtime annotations.
- The status line must always print. Any error in the new code leaves the new part out, never the line, and never writes to stderr.
- A status-line run never waits on the scan: the child gets `stdin/stdout/stderr=DEVNULL` (so it holds no pipe Claude Code waits on), `start_new_session=True` on POSIX, `creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows.
- Exact copy: `cache cut $A → $B (-N%)`, `cost $B · cache not paid back yet`, `30d all projects: ` (30 = `totals_days`), `most saved: `, `counting…`.
- New config keys and defaults: `statusline_totals: true`, `totals_days: 30`, `totals_refresh_minutes: 10`. A lock older than 5 minutes is stale.
- Figures must equal `report --all` over the same window: main file in window if its mtime ≥ now − days; its subagent files always count with it.
- Work on branch `feat/all-projects-line`. Commit per task; do not push.
- Test command for every task: `python3 tests/test_cache_guard.py`; pass = last line starts `All tests passed`.

## Review Focus

1. A transcript being written mid-line while scanned (the active session): the partial last line is skipped, no crash, and the next scan catches up because size/mtime changed. Test in Task 2.
2. A project folder whose transcripts carry no `cwd` (old sessions): labelled with the folder name, leading `-` stripped. Test in Task 2.
3. No `~/.claude/projects` or nothing priced (fresh machine, API key with unknown models): the middle line is left out entirely, not `$0.00 → $0.00`. Test in Task 3.
4. A very long folder name: project labels cut to 24 chars ending in `…` so the line stays readable. Test in Task 3.
5. Several open sessions going stale at the same moment: the `O_EXCL` lock lets exactly one spawn. Test in Task 3 (four status lines in parallel, one scan).

---

### Task 1: Session figure reads "cache cut $A → $B"

**Files:**
- Modify: `cache_guard.py` (`fmt_signed_usd` area for helpers; `cost_block`; `TIPS`)
- Modify: `tests/test_cache_guard.py:314-378` (`MONEY`, `money`, `agrees`), `:469`, `:579`
- Modify: `README.md:36-46`

**Interfaces:**
- Produces: `fmt_compact_usd(x: float) -> str` (`>= 1000` → `"$13.1k"`, else `fmt_usd(x)`); `cut_text(cost: float, saved: float) -> str` returning `"cache cut {fmt_usd(cost+saved)} → {fmt_usd(cost)} (-{round(100*saved/(cost+saved))}%)"`, or `"cost {fmt_usd(cost)} · cache not paid back yet"` when `saved <= 0` or `fmt_usd(cost+saved) == fmt_usd(cost)`.

- [ ] **Step 1: Branch and update the tests to the new wording**

`git switch -c feat/all-projects-line`. Then in the tests:

```python
MONEY = re.compile(r"\| (?:cache cut (\$[\d,.]+) → (\$[\d,.]+) \(-\d+%\)|cost (\$[\d,.]+) · cache not paid back yet)"
                   r"(?: · avoidable (\$[\d,.]+))? \|")
# money(): cost = group(2) or group(3); before = group(1) or None; avoidable = group(4) or 0
# agrees(fig, ref): cost and avoidable within 0.0051 of ref; if fig["before"] is not None then
#   abs(fig["before"] - (ref["cost"] + ref["saved"])) <= 0.0051, else ref["saved"] <= 0.005
```

Line 469 regex: `\(warm, \d+m left, ttl 1h\) \| (cache cut \$[\d.,]+ → \$[\d.,]+ \(-\d+%\)|cost \$[\d.,]+ · cache not paid back yet) · avoidable \$[\d.]+ \| misses 0`. Line 579: `"saved $"` → `"cache cut $"`. Add:

```python
p8 = write_session("cost-8", line(resp("s0", NOW - 60, O, 40000, 0)), {})   # one write, no reads: saved < 0
rc, out, _ = cost_sl("cost-8", p8)
check("statusline cost: before the cache pays back it says so",
      "| cost $" in out and "· cache not paid back yet" in out and "cache cut" not in out.splitlines()[0], out)
check("statusline cost: a tip explains cache cut", any("cache cut" in t and "5%" in t for t in TIPS_FROM_SCRIPT), "")
```

(`TIPS_FROM_SCRIPT`: load via `subprocess.run([sys.executable, "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import cache_guard as cg, json; print(json.dumps(cg.TIPS))", str(ROOT)], ...)`.)

- [ ] **Step 2: Run tests, expect the reworded checks to FAIL**

Run: `python3 tests/test_cache_guard.py | grep -E "FAIL|passed"`. Expected: the `statusline cost:` checks FAIL (line still says `saved $`).

- [ ] **Step 3: Implement `fmt_compact_usd`, `cut_text`; use `cut_text` in `cost_block`; add the tip**

`cost_block` parts become `[cut_text(cost, saved)]` plus the existing avoidable part; drop the `saved` part. New `TIPS` entry, exact: `"cache cut $A → $B: $A is what re-sending your history every turn would cost at full price. The cache bills those repeats at about 5%."`

- [ ] **Step 4: Run tests, expect PASS**

Run: `python3 tests/test_cache_guard.py | tail -n 1`. Expected: `All tests passed`.

- [ ] **Step 5: README, then commit**

README line 36 example → `cache cut $60.50 → $12.40 (-79%) · avoidable $1.90`. Replace the **cost**/**saved** bullets with one **cache cut** bullet (before = cost + saved, after = cost; the old saved formula stays as the explanation; the not-paid-back wording). Keep the **avoidable** bullet.

```bash
git add cache_guard.py tests/test_cache_guard.py README.md docs/superpowers
git commit -m "feat: session figure reads cache cut \$A → \$B"
```

---

### Task 2: `totals-refresh` scan

**Files:**
- Modify: `cache_guard.py` (constants near `CONFIG_PATH`; new section `# ---- all-projects totals` after `cost_block`; `main()` jobs; `DEFAULT_CONFIG`)
- Modify: `tests/test_cache_guard.py` (new block after the cost tests, before trim tests)

**Interfaces:**
- Consumes: `load_requests`, `subagent_files`, `req_cost`, `cache_saving`, `price_table`, `digest`, `LARGE_PROMPT`, `VERSION`.
- Produces:
  - `TOTALS_PATH = GUARD_DIR / "totals.json"`, `TOTALS_FILES_PATH = GUARD_DIR / "totals-files.json"`, `TOTALS_LOCK = GUARD_DIR / "totals.lock"`
  - `totals.json` schema: `{"computed_at": float, "days": number, "cost": float, "saved": float, "projects": [{"key": str, "name": str, "cost": float, "saved": float}]}`, projects sorted by `saved` desc then `name`.
  - `transcript_cwd(path: Path) -> str | None`: first string `cwd` in the first 500 JSON lines.
  - `scan_totals(cfg: dict, now: float) -> dict`: returns the `totals.json` dict; reads and rewrites `TOTALS_FILES_PATH`.
  - `cmd_totals_refresh(args: list) -> None`: writes `TOTALS_PATH` atomically (tmp + `os.replace`); with `--release-lock` it unlinks `TOTALS_LOCK` in a `finally`.
  - CLI job `"totals-refresh"`.

- [ ] **Step 1: Write the failing tests**

Fixtures: `-tot-alpha/a1.jsonl` (user line with `"cwd": "/x/alpha"`, then warm reads worth more than beta), `-tot-beta/b1.jsonl` (`cwd` `/x/beta`), `-tot-nocwd/n1.jsonl` (no `cwd`), `-tot-old/o1.jsonl` (mtime set 40 days back with `os.utime`). Helper `refresh(pre="")` runs `totals-refresh` through `PATCH`.

```python
check("totals: refresh matches a full read of every session",
      abs(t["cost"] - sum(r["cost"] for r in refs)) < 1e-6 and abs(t["saved"] - sum(r["saved"] for r in refs)) < 1e-6)
      # refs = [reference(p) for p in HOME/.claude/projects/*/*.jsonl if mtime >= NOW - 30 days]
check("totals: projects named after the last folder of cwd, most saved first",
      [p["name"] for p in t["projects"]].index("alpha") < [p["name"] for p in t["projects"]].index("beta"))
check("totals: a project with no cwd uses its folder name", "tot-nocwd" in [p["name"] for p in t["projects"]])
check("totals: sessions older than totals_days are left out", "tot-old" not in [p["key"] for p in t["projects"]])
check("totals: an unchanged second scan re-reads nothing", reads == 0)
      # reads counted with pre="n=[0]; f=cg.load_requests; cg.load_requests=lambda *a, **k: (n.__setitem__(0, n[0]+1), f(*a, **k))[1]; import atexit; atexit.register(lambda: print('READS', n[0]))"
check("totals: appending to one session re-reads only that file", reads == 1)
check("totals: a half-written last line is skipped, then counted once complete", ...)
      # append half a resp() line to a1.jsonl, refresh: rc 0 and totals unchanged; append the rest, refresh: equals reference
check("totals: --release-lock removes the lock", not TOTALS_LOCK.exists())
```

- [ ] **Step 2: Run tests, expect the `totals:` checks to FAIL** (`totals-refresh` is not a command yet: exit 1).

- [ ] **Step 3: Implement `transcript_cwd`, `scan_totals`, `cmd_totals_refresh`; register the job**

`TOTALS_FILES_PATH` holds `{"prices": <digest as in session_costs>, "files": {str(path): {"size", "mtime_ns", "cost", "saved", "cwd"}}}`. Discard it whole when `prices` differs. A file is re-read only when size or `mtime_ns` changed. Cost per file = sum of `req_cost` values over priced requests; saved = sum of `cache_saving` (skip `None`). Main files use `load_requests(p)`, subagent files `load_requests(f, sidechain=True)` and count toward the parent's project. Project key = parent folder name; name = basename of `cwd` from the newest main file in that folder that has one, else key with leading `-` stripped. Entries for files outside the window are dropped when rewriting. Add the three config keys to `DEFAULT_CONFIG`.

- [ ] **Step 4: Run tests, expect PASS** (`All tests passed`).

- [ ] **Step 5: Commit**

```bash
git add cache_guard.py tests/test_cache_guard.py
git commit -m "feat: totals-refresh scans every project, re-reading only changed transcripts"
```

---

### Task 3: Middle status-line row and background refresh

**Files:**
- Modify: `cache_guard.py` (`cmd_statusline`; totals section)
- Modify: `tests/test_cache_guard.py` (`ENV` gets `"CACHE_GUARD_NO_SPAWN": "1"`; `run()` gains `env=ENV`; checks at :579-598 that assert `"cost" not in out` look at `out.splitlines()[0]` only; install config check adds the new keys)
- Modify: `README.md` (example block, new **All-projects line** paragraph, settings table rows)

**Interfaces:**
- Consumes: `TOTALS_PATH`, `TOTALS_LOCK`, `cmd_totals_refresh` (Task 2); `cut_text`, `fmt_compact_usd` (Task 1).
- Produces: `totals_line(cfg: dict, now: float) -> str | None`; `maybe_spawn_refresh(cfg: dict, now: float) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
lines = sl("tot-1", prompt_cache=pc())[1].splitlines()        # after a synchronous totals-refresh
check("totals: row sits between the cache line and the tip", len(lines) == 3 and lines[2].startswith("tip:")
      and re.fullmatch(r"30d all projects: cache cut \$[\d,.]+ → \$[\d,.]+ \(-\d+%\) \| most saved: .+", lines[1]))
check("totals: top 3 only, compact amounts", lines[1].count(" · ") == 2 and re.search(r"alpha \$[\d.]+k? ·", lines[1]))
check("totals: also shown while waiting for the first response", "30d all projects:" in sl("tot-2")[1])
check("totals: long project names are cut to 24 chars", "a" * 23 + "…" in line_with_long_cwd)
check("totals: before the first scan it says counting…", lines_no_file[1] == "30d all projects: counting…")
check("totals: nothing priced leaves the row out", "all projects" not in out_unpriced_home)   # HOME with only MYST sessions
check("totals: statusline_totals=false leaves the row out and starts nothing", ...)
check("totals: a corrupt totals.json never breaks the line", rc == 0 and "tip:" in out and not err)
check("totals: a stale result starts one background scan", totals_json_appears_within_15s and not TOTALS_LOCK.exists())
      # run with ENV minus CACHE_GUARD_NO_SPAWN; statusline returns in < 1s
check("totals: a fresh lock stops a scan", not TOTALS_PATH.exists() after 2s and TOTALS_LOCK.exists())
check("totals: a lock older than 5 minutes is taken over", totals_json_appears_within_15s)   # os.utime lock to NOW-360
check("totals: four status lines at once start one scan", spawns == 1)
      # 4 statuslines via Popen in parallel through PATCH, spawn enabled, no totals.json, no lock;
      # pre stubs cg.subprocess.Popen with a function that appends one line to WORK/"spawns.log" and returns;
      # spawns = number of lines in that file. Delete the lock afterwards.
```

`line_with_long_cwd`: a `-tot-long` session whose `cwd` ends in a folder of 40 `a`s, saved above beta's. `out_unpriced_home`: a second temp HOME holding only a `MYST`-model session, `totals-refresh` then `statusline` run with `HOME` pointed at it. `lines_no_file`: status line run after deleting `TOTALS_PATH`, spawn disabled.

- [ ] **Step 2: Run tests, expect the new `totals:` row checks to FAIL.**

- [ ] **Step 3: Implement `totals_line` and `maybe_spawn_refresh`; call them in both branches of `cmd_statusline`**

`totals_line`: `None` when `statusline_totals` is off; `"{DIM}{days}d all projects:{RST} counting…"` when `TOTALS_PATH` is missing; `None` when cost and saved are both 0; else `"{DIM}{days}d all projects:{RST} " + cut_text(cost, saved)` plus `" | most saved: " + " · ".join(f"{name} {fmt_compact_usd(saved)}")` over the first 3 projects with `saved > 0` (omitted if none). Names over 24 chars → first 23 + `…`. Whole function in `try/except Exception: return None`.
`maybe_spawn_refresh`: returns if off, if `CACHE_GUARD_NO_SPAWN` is set, or if `TOTALS_PATH` is fresh (`now - computed_at <= totals_refresh_minutes*60` and `days` matches). Lock with `os.open(TOTALS_LOCK, O_CREAT | O_EXCL | O_WRONLY)`; on `FileExistsError`, if the lock's mtime is over 300s old, unlink and retry once, else return. Spawn `[sys.executable, str(Path(__file__).resolve()), "totals-refresh", "--release-lock"]` with the flags in Global Constraints; if `Popen` raises, unlink the lock. Never raises.
Print order: line 1, then the totals row if not `None`, then the tip.

- [ ] **Step 4: Run tests, expect PASS** (`All tests passed`).

- [ ] **Step 5: README and commit**

README: middle row in the example block; an **All-projects line** paragraph (what before → after means, 30-day window, refresh every 10 minutes in the background, `counting…` on first run, matches `report --all`); rows for `statusline_totals`, `totals_days`, `totals_refresh_minutes`.

```bash
git add cache_guard.py tests/test_cache_guard.py README.md
git commit -m "feat: all-projects row in the status line, refreshed in the background"
```

---

### After the tasks

Ask before pushing. Once pushed, reinstall from git: fresh clone into the scratchpad, `python3 <clone>/cache_guard.py install`, then `doctor`.
