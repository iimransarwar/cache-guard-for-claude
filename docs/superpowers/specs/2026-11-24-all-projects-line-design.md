# All-projects line in the status line

## Why

The status line only shows the current session. The user wants to see, at a glance, what caching is doing across every
project, and which projects benefit most. "Saved" on its own didn't say what was saved or how, so both lines should
show the cost with and without the cache.

## What it looks like

```
Opus 5.5 | cache 95% (warm, 39m left, ttl 1h) | cache cut $60.50 → $12.40 (-79%) · avoidable $1.90 | misses 1 | ...
30d all projects: cache cut $38,520 → $4,310 (-89%) | most saved: api-server $13.1k · web-app $11.8k · docs-site $1.9k
tip: ...
```

- **Middle line (new):** last 30 days, every project. Before → after is the cost without the cache versus the cost with it.
  Top 3 projects ranked by amount saved.
- **Session line (changed):** `cost $X · avoidable $Y · saved $Z` becomes `cache cut $X+Z → $X (-N%) · avoidable $Y`.
- **New rotating tip:** explains "cache cut": the before figure is what re-sending your history every turn would cost at
  full price; the cache bills those repeats at about 5%.

## Goals / non-goals / anti-goals

- Goal: one line, top 3 projects plus the total, numbers matching `report --all` over the same window.
- Non-goal: changing `report --all` output or the cost formulas. "Without cache" is simply cost + saved, as already
  calculated per request.
- Anti-goal: the status line getting slower, failing, or printing an error because of this. Any problem leaves the
  middle line out, never the whole status line.
- Anti-goal: several open sessions each scanning 4 GB of transcripts at once.

## Approaches

1. **Background scan with a saved result (picked).** Status line reads a small saved file; when it's over 10 minutes
   old, it starts one detached scan under a lock. The scan keeps per-transcript figures and only re-reads changed files.
   First scan ~10s, later ones ~1s. Trade-off: numbers lag up to 10 minutes.
2. Fill it in within each refresh's half-second budget. No background process, but ~10 minutes to first figure, and it
   takes time from the session cost figures.
3. Scheduled job (launchd/cron). Per-OS setup, runs when Claude Code isn't, doesn't fit a one-command install.

## How it works (picked approach)

- New internal command `cache_guard.py totals-refresh`. It scans `~/.claude/projects/*/*.jsonl` modified in the last
  `totals_days` days, plus each session's subagent files: the same set `report --all` reads. It writes
  `~/.claude/cache-guard/totals.json` (total cost, total saved, per-project figures, time computed).
- Per-file cache `~/.claude/cache-guard/totals-files.json`: path → size, mtime, cost, saved, project. Files whose size
  and mtime are unchanged are not re-read.
- Status line: if `totals.json` is missing or older than `totals_refresh_minutes`, and no fresh lock exists, start
  `totals-refresh` detached (new session, output discarded; Windows: detached process flags). A lock older than
  5 minutes counts as stale.
- Project name: last folder of the session's `cwd` from the transcript; the encoded project folder name if there is no
  `cwd`. Figures grouped per project folder.
- Money format: totals as `$4,310`; top-3 figures compact (`$13.1k`, `$954`).

## Calls I made (correct any)

- Before the first scan finishes, the middle line shows `30d all projects: counting…` in dim text.
- Session line early on, while saved is zero or negative (cache writes not yet paid back):
  `cost $12.40 · cache not paid back yet` instead of a before → after.
- claude-mem's observer sessions count toward the total like any other project.
- New config keys: `statusline_totals` (true), `totals_days` (30), `totals_refresh_minutes` (10).
- README updated for the new line, the new wording and the new keys; tests added for the refresh, the cached read, the
  lock, and both wordings; existing tests that look for `saved $` updated.

## Left to the builder

File layout inside `cache_guard.py`, lock file mechanics, and how tests fake the background scan.
