# cache-guard

Keeps your Claude Code prompt cache warm and your token use low. It shows live cache stats and timely tips under the input box, guards the actions that throw a warm cache away, and tells you afterwards where your tokens went.

Needs Python 3.8+ and Claude Code v2.1.251 or later (v2.1.260+ for miss causes). No other dependencies.

## Install

```bash
python3 cache_guard.py install          # status line + safety hooks + Lean style file
python3 cache_guard.py install --all    # also: Bash output trimmer, expiry alerts, Lean as default
```

Then restart Claude Code and run:

```bash
python3 ~/.claude/cache-guard/cache_guard.py doctor
```

Install backs up `~/.claude/settings.json` first, keeps your own hooks, and replaces any existing status line. That status line is kept in the backup. Re-running install is safe.

| Flag | What it adds |
| - | - |
| `--trim` | Trims huge Bash output (test and build logs) before Claude reads it |
| `--notify` | Desktop alert when a warm cache is about to expire |
| `--lean` | Makes the bundled Lean output style your default |
| `--all` | All three |
| `--no-trim`, `--no-notify` | Turn those back off |

## What you get

**Status line** (two lines, refreshes every 30s)

```
Opus 5.5 | cache 95% (warm, 39m left, ttl 1h) | cost $12.40 · avoidable $1.90 · saved $48.10 | misses 1 | 5h 42% 7d 18%
tip: Last miss: tool definitions changed. Leave MCP servers and plugins alone until a break.
```

The three money figures are for the current session at API list prices. On a subscription they show relative size, not a bill.

- **cost**: what the session has cost so far, main conversation plus its subagents. It is the same total `report <session>` gives (its two Cost lines added together).
- **avoidable**: the part of that cost spent re-writing cache that should have been read. That is every rebuild `report` lists under Cache misses (idle past the cache TTL, a model switch, a resume or restart, changed tools, or a prefix change with no cause in the transcript), at the price `report` shows for each. Session start and after-compaction writes are expected and not counted. Like the report, it covers the main conversation. It turns yellow at `avoidable_warn_pct` of the cost (default 15%) and is left out while it is $0.00.
- **saved**: what caching has saved. For every request, main and subagents, it is what the cache reads would have cost at the plain input price, minus what was paid for cache reads and writes: (read tokens × (input price − read price)) − (write tokens × (write price − input price)), at that request's model prices and 5m or 1h write price. Early in a session it can be negative, because writes are paid before reads pay them back.

Requests on models without a price are left out of all three, as in `report`. If nothing in the session can be priced, the block is left out. The status line keeps running totals in its state file and reads only what the transcripts gained since the last refresh. It spends at most half a second reading per refresh. If a refresh runs out of time (the first one on a very large session, for example), it shows the last figures it had, or none yet, and carries on at the next refresh. An error leaves the block out, never the line. Turn it off with `statusline_cost`.

The tip is the most urgent of these:

1. Cache is cold: suggests `/clear` for new work, and points out that now is a free moment to switch model or MCP servers.
2. Cache about to expire: reply soon.
3. 5-hour or 7-day usage limit above 80%: avoid expensive actions until it resets.
4. The cause of a miss in the last 10 minutes.
5. Context above 75%: `/compact` at your next break.
6. Otherwise, a rotating tip from 24 do's and don'ts.

**Guards** (hooks)

| Hook | What it does |
| - | - |
| Model switch | When the cache is warm, asks first and shows how many tokens the switch re-reads. When it's cold, stays silent. |
| `/compact` | When the cache is cold, a manual `/compact` is stopped once with the cost. Run it again within 2 minutes to go ahead. Auto-compaction is never touched. |
| Cold message | When you type into a session whose cache has gone cold and whose history is over 100k tokens, the message is stopped once. You see how long the cache has been cold, how many tokens the message re-reads and what that costs at list price. Send it again within 2 minutes to go ahead, or `/clear` first for a new task. Slash commands, loops, scheduled tasks and SDK prompts are never stopped. |
| Resume | Warns when a resume re-sends a big history after the cache has expired. |
| Bash trimmer (opt-in) | Output over 12,000 chars keeps its first 30 lines, last 60 lines and every error or warning line. The full log is saved to `~/.claude/cache-guard/logs/` and Claude is told where. Add `# no-trim` to a command to skip trimming. |

The hooks only show messages to you. They never inject text into Claude's context, so they can't break the cache themselves.

**`doctor`** scans for settings that silently break the cache or waste tokens:

- the `opusplan` model setting
- caching disabled or forced to 5 minutes
- tool search turned off
- a gateway that may strip cache markers
- other hooks that inject `additionalContext` on every tool call
- an oversized CLAUDE.md
- missing output caps

**`report [session-id | path.jsonl]`** shows one session: tokens, cache hit rate, cost at list price, and every real cache miss with a likely cause (idle past the TTL, a model switch, a resume or restart, changed tools, or unknown). It then shows the same totals for the session's subagents and names the three most expensive ones. With no argument it uses the latest session for the current folder. The start of a session id is enough.

A miss is a request whose cached prefix shrank, the same test Claude Code's own miss counter uses. A large write on a warm cache is new content, not a miss, and is not listed. Session start and compaction are shown separately as expected writes.

**`report --all [--days N]`** covers every session from the last N days (default 30):

- cost split between main conversations and subagents
- cache rebuilds grouped by cause, with their share of main-conversation cost
- the five sessions with the most avoidable rebuilds
- a verdict on subagent cache length: what the other setting (5m or 1h) would have cost on your own transcripts, and whether to change `subagentPromptCacheTtl`

Costs use API list prices built into the script. On a subscription they show relative size, not a bill.

## Lean output style

`lean.md` tells Claude to lead with the result and skip recaps. It also tells Claude to point at code as `path:line`, use quiet commands, filter long logs, grep before reading, and hand noisy digging to a subagent. It keeps Claude Code's built-in coding instructions. Pick it in `/config`, or install with `--lean`.

## Settings

Thresholds live in `~/.claude/cache-guard/config.json`:

| Key | Default | Meaning |
| - | - | - |
| `notify` | false | Desktop alert before expiry |
| `notify_minutes` | 3 | How early to alert |
| `expiry_warn_minutes` | 3 | When the status line warns about expiry |
| `context_warn_pct` | 75 | When to suggest `/compact` |
| `limit_warn_pct` | 80 | Usage-limit warning threshold |
| `resume_warn_tokens` | 50000 | Minimum history size for the resume warning |
| `compact_confirm_seconds` | 120 | Window to confirm a cold `/compact` |
| `cold_prompt` | block | Cold-message guard: `block` stops the message once, `warn` only tells you, `off` disables it |
| `cold_prompt_warn_tokens` | 100000 | Smallest history the cold-message guard acts on |
| `cold_prompt_confirm_seconds` | 120 | Window to send a stopped message again |
| `pricing` | {} | Price overrides, per million tokens: `{"model-id-prefix": [input, 5m write, 1h write, cache read, output]}` |
| `statusline_cost` | true | Show session cost, avoidable cost and cache savings on the status line |
| `avoidable_warn_pct` | 15 | Avoidable share of the session's cost (%) at which that figure turns yellow |
| `trim.max_chars` | 12000 | Only trim output longer than this |
| `trim.head_lines`, `trim.tail_lines` | 30, 60 | Lines kept at the start and end |
| `trim.max_error_lines` | 80 | Most error lines kept from the middle |
| `trim.keep_logs_days` | 3 | How long full logs are kept |

## Uninstall

```bash
python3 ~/.claude/cache-guard/cache_guard.py uninstall
rm -rf ~/.claude/cache-guard ~/.claude/output-styles/lean.md
```

## Tests

```bash
python3 tests/test_cache_guard.py   # 95 checks against a throwaway HOME
```

## Caveats

- Tested with simulated Claude Code inputs, not in a live session. Field names come from Claude Code's docs and changelog.
- The trimmer replaces what Claude sees. If Claude needs the full output, it can grep the saved log.
- If your Claude Code version doesn't let hooks stop `/compact`, you'll see the warning and it will compact anyway.
- Causes in `report` are inferred from timing, model changes and markers in the transcript. `/usage` shows Claude Code's own diagnosis.
- Prices are list prices from November 2026 and will go stale. Models without a price are counted in tokens and left out of cost. Fix either with `pricing` in the config.
- The cold-message guard relies on the status line having run in that session, and on Claude Code saying who sent the prompt. If your version doesn't say, the guard warns instead of stopping.
- The subagent cache-length verdict prices writes and pauses inside a subagent. It can't tell whether a longer cache would have warmed subagents that started cold.
