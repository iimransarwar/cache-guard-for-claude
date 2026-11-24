# cache-guard

**Stop paying full price for Claude Code's memory.**

Every turn, Claude Code re-sends your whole conversation. Prompt caching bills those repeats at about 5% of the normal price, until the cache goes cold, you switch model, or a setting quietly breaks it. Then you pay full price again, and nothing tells you.

cache-guard tells you. It shows what the cache is saving you right under the input box, warns you before the moves that throw a warm cache away, forecasts when you'll hit your usage limit, can keep a long session warm while you're away, and finds the settings and files that waste tokens. One Python file. No dependencies, no network, no proxy, and its warnings go to you, never into Claude's conversation.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB) ![Dependencies: none](https://img.shields.io/badge/dependencies-none-2ea44f) ![Network: none](https://img.shields.io/badge/network-none-2ea44f) ![Tests: 200](https://img.shields.io/badge/tests-200%20checks-2ea44f)

```
Opus 5.5 | cache 95% (warm, 39m left, ttl 1h) | cache cut $60.50 → $12.40 (-80%) · avoidable $1.90 | misses 1 | All models: 42% left (5h, out in ~40m) · 82% left (week)
30d at API prices: $4,310 (would be $38,520 without cache, -89%) | most saved: api-server $13.1k · web-app $11.8k · docs-site $1.9k
tip: At this pace the 5-hour limit for all models runs out in ~40m, 2.3h before it resets (15:10). /effort low and fewer subagents stretch it.
```

*Example figures. Costs are at API list price; on a subscription they show relative size, not a bill.*

## What you get

- **See the money, live.** `cache cut $60.50 → $12.40` is what this session would have cost without the cache, then what it did cost. `avoidable $1.90` is what was spent rebuilding a cache that should have been read.
- **Every project at a glance.** A second line prices the last 30 days across every project at API rates, shows what it would have cost without the cache, and names the three projects where caching saved the most. It's refreshed in the background, so the status line never slows down.
- **Know before you hit the wall.** A pace forecast measures how fast your 5-hour limit is rising right now and says `out in ~40m` when you'd run out before the reset. Idle hours never skew it, and it stays silent when you're fine.
- **Guards on the expensive moves.** A model switch on a warm cache asks first. A `/compact` on a cold cache says how much it would re-read and needs a second go. Typing into a big cold session tells you what it will cost, in dollars, and that `/clear` is cheaper for a new task.
- **Come back to a warm cache.** Opt in with `--keepwarm` and, while you're away, cache-guard wakes Claude for a one-word reply just before the cache expires. On a 1-hour cache that's about one ping an hour, a few cents each against dollars for a rebuild. It stops on its own once the pings would cost more than one rebuild, after 12 hours, or as soon as you're back.
- **A budget, if you want one.** Set `session_budget_usd` and you're told when a session, subagents included, passes it, and again at each multiple. Or have the message stopped once.
- **A doctor for your setup.** It finds settings that silently break caching, adds up the CLAUDE.md files, imports, memory and rules loaded into every session, flags API keys and passwords sitting in them, and spots proxies that can rewrite your requests.
- **Reports that explain the bill.** `report` lists every real cache miss with its likely cause and cost. `report --all` splits 30 days of spend by cause and subagents, and tells you whether a 5-minute or 1-hour subagent cache would have been cheaper on your own history.

## Why not a proxy, a keep-warm bot or a usage bar?

There are other ways to attack the same problem. Each one gives up something cache-guard doesn't.

| | **cache-guard** | Request-rewriting proxies | Keep-warm pingers | Usage bars and dashboards |
| - | - | - | - | - |
| Sits between Claude Code and Anthropic | **No** | Yes, every request | No | No |
| Sends requests that use your quota | **Only opt-in keep-warm, capped at the cost of one rebuild** | No | Yes, every ping | Some poll usage APIs |
| Touches your Claude login token | **Never** | Every request passes through it | No | Some read it from your keychain |
| Can change what Claude sees | **Only what you opt into** (Bash trimmer, Lean style, keep-warm pings) | Yes | Some add turns | No |
| Warns before cache-breaking moves | **Yes: model switch, cold `/compact`, cold message, resume** | No | Some, before a cold message | No |
| Explains misses and their cost | **Yes, per miss and per cause** | Partly | No | Partly |
| Keeps the cache warm while you're away | **Opt-in, only while it pays** | No | Yes, often by default | No |
| Forecasts running out of quota | **Yes, from the recent pace** | No | No | Some, from window averages |
| Install footprint | **One stdlib Python file** | Service, certificates or env vars | Plugin or hook | Extension, daemon or installer |

## Safe by design

- **No network.** cache-guard itself never makes a request; even keep-warm only asks Claude Code to take a turn. Everything comes from what Claude Code passes to the status line and the transcripts already on your disk.
- **No proxy, no token.** It never sits in front of your API traffic and never reads your login credentials.
- **Never in Claude's context.** Hooks only show messages to you, so cache-guard can't break the cache it's guarding. The exceptions are opt-ins you pick by name: the Bash trimmer shortens huge command output, the Lean output style changes how Claude writes its replies, and keep-warm wakes Claude for a one-word reply while you're away.
- **Your settings, backed up.** Install backs up `settings.json`, keeps your own hooks, and uninstalls in one command.
- **Readable.** One file, standard library only, with a 200-check test suite that runs against a throwaway home folder.

## Install

Needs Python 3.8+ and Claude Code v2.1.251 or later (v2.1.260+ for miss causes).

```bash
git clone https://github.com/iimransarwar/cache-guard-for-claude.git
cd cache-guard-for-claude
python3 cache_guard.py install
```

Restart Claude Code, then check your setup:

```bash
python3 ~/.claude/cache-guard/cache_guard.py doctor
```

**Update** from the same folder. Your config and the options you turned on are kept:

```bash
git pull && python3 cache_guard.py install
```

Install copies the script to `~/.claude/cache-guard/`, backs up `~/.claude/settings.json` first, keeps your own hooks, and replaces any existing status line (it stays in the backup). Re-running it is safe. It keeps the trimmer and keep-warm if they are already on; `--no-trim` and `--no-keepwarm` remove them.

| Flag | What it adds |
| - | - |
| `--trim` | Trims huge Bash output (test and build logs) before Claude reads it |
| `--notify` | Desktop alert when a warm cache is about to expire |
| `--keepwarm` | Keeps a long session's cache warm while you're away (see Keep-warm below) |
| `--lean` | Makes the bundled Lean output style your default |
| `--all` | `--notify` and `--lean`. It never turns on the trimmer: only `--trim` does |
| `--no-trim`, `--no-notify`, `--no-keepwarm` | Turn those back off |

## Reference

### Status line

Three lines, refreshed every 30 seconds:

```
Opus 5.5 | cache 95% (warm, 39m left, ttl 1h) | cache cut $60.50 → $12.40 (-80%) · avoidable $1.90 | misses 1 | All models: 42% left (5h, out in ~40m) · 82% left (week)
30d at API prices: $4,310 (would be $38,520 without cache, -89%) | most saved: api-server $13.1k · web-app $11.8k · docs-site $1.9k
tip: At this pace the 5-hour limit for all models runs out in ~40m, 2.3h before it resets (15:10). /effort low and fewer subagents stretch it.
```

The money figures are for the current session at API list prices. On a subscription they show relative size, not a bill.

- **cache cut $A → $B**: $B is what the session has cost so far, main conversation plus its subagents. It is the same total `report <session>` gives (its two Cost lines added together). $A is what the same requests would have cost with no prompt cache. Every turn re-sends the whole history; the cache bills those repeated tokens at about 5% of the input price, and $A − $B is what that saved: (read tokens × (input price − read price)) − (write tokens × (write price − input price)), for every request, main and subagents, at that request's model prices and 5m or 1h write price. Early in a session the cache writes have not paid for themselves yet, and the line says `cost $B · cache not paid back yet` instead. With caching off it shows just `cost $B`, and a saving under 1% shows as `(<1%)`.
- **avoidable**: the part of that cost spent re-writing cache that should have been read. That is every rebuild `report` lists under Cache misses (idle past the cache TTL, a model switch, a resume or restart, changed tools, or a prefix change with no cause in the transcript), at the price `report` shows for each. Session start and after-compaction writes are expected and not counted. Like the report, it covers the main conversation. It turns yellow at `avoidable_warn_pct` of the cost (default 15%) and is left out while it is $0.00.

Requests on models without a price are left out of every figure, as in `report`. If nothing in the session can be priced, the block is left out. The status line keeps running totals in its state file and reads only what the transcripts gained since the last refresh. It spends at most half a second reading per refresh. If a refresh runs out of time (the first one on a very large session, for example), it shows the last figures it had, or none yet, and carries on at the next refresh. An error leaves the block out, never the line. Turn it off with `statusline_cost`.

**All-projects line**: what every session in every project cost over the last `totals_days` days (default 30) at API list prices, main conversations and subagents included, then what the same calls would have cost without the cache, then the three projects caching saved the most in. On a subscription it is not your bill; it shows what the same usage would cost on the API. The figures match `report --all --days 30` (its two cost rows added together). Projects are named after the folder the session ran in. Reading 30 days of transcripts can take several seconds, so the status line never does it itself: it shows the last saved figures, and when they are over `totals_refresh_minutes` old (default 10) it starts one scan in the background. A lock file makes sure several open sessions start only one scan between them; a running scan keeps its lock fresh, and a scan that dies or fails frees it after 5 minutes. After the first scan, only transcripts that changed are read again. Until the first scan finishes the line says `counting…`. With nothing priced, the line is left out. Turn it off with `statusline_totals`.

Usage limits show as percent left, named for whom they apply to. When a limit has both a 5-hour and a weekly window, each says which it is, as in `All models: 58% left (5h) · 82% left (week)`. A limit for a single model comes first, under that model's name, for example `Fable: 61% left`. Behind a Claude apps gateway, a spend limit shows as `Spend limit: 37% left`. Any other limit Claude Code passes in the same shape is shown too, named from its key (`seven_day_fable` would read as the weekly Fable limit). A limit turns yellow at `limit_warn_pct` used (default 80%) and red at 90% used. With no limit data, on an API key for example, this part is left out.

**Pace forecast**: "58% left" doesn't tell you whether you'll make it to the reset. cache-guard keeps a reading of each limit every minute and measures how fast it is rising over the last `pace_window_minutes` (default 30). When that pace uses a limit up before it resets, the limit says so, as in `42% left (5h, out in ~40m)`, and the tip says how long before the reset you'd be locked out. Only 5-hour windows are forecast: a weekly limit isn't used around the clock, so its recent pace would cry wolf. It needs at least 5 minutes of readings and a 1-point rise, so a single reading or an idle stretch never raises a false alarm. A new reset time starts the readings over, and a lower reading from another, idle session is ignored. Turn it off with `pace_forecast`.

Claude Code's status line documentation lists only the 5-hour and weekly windows for all models (`rate_limits.five_hour`, `rate_limits.seven_day`) and the gateway `spend_limit`. A model's own weekly limit, such as Fable's, shows in `/usage` but is not passed to status lines, so it appears here only once Claude Code passes it.

The tip is the most urgent of these:

1. Cache is cold: suggests `/clear` for new work, and points out that now is a free moment to switch model or MCP servers.
2. Cache about to expire: reply soon.
3. At the current pace a usage limit runs out before it resets: says when, and how long before the reset.
4. A usage limit over 80% used: names the limit, says how much is left and when it resets, and suggests avoiding expensive actions until then.
5. The cause of a miss in the last 10 minutes.
6. Context above 75%: `/compact` at your next break.
7. Otherwise, a rotating tip from 26 do's and don'ts.

### Guards (hooks)

| Hook | What it does |
| - | - |
| Model switch | When the cache is warm, asks first and shows how many tokens the switch re-reads. When it's cold, stays silent. |
| `/compact` | When the cache is cold, a manual `/compact` is stopped once with the cost. Run it again within 2 minutes to go ahead. Auto-compaction is never touched. |
| Cold message | When you type into a session whose cache has gone cold and whose history would cost at least `cold_prompt_warn_usd` ($0.50) to re-read, you get a warning. The floor is in dollars because 100k tokens costs very different amounts on Haiku and Opus; models without a price fall back to `cold_prompt_warn_tokens` (100k). The warning says how long the cache has been cold, how many tokens the message re-reads and what that costs at list price. For a new task, `/clear` first is far cheaper. The message itself goes through. Set `cold_prompt` to `block` to have it stopped once instead; you then send it again within 2 minutes to go ahead. Slash commands, loops, scheduled tasks and SDK prompts are never touched. |
| Session budget (opt-in) | Set `session_budget_usd` and typed messages get a warning when the session's list-price cost (main conversation plus subagents) passes it, and again at 2×, 3× and so on. The status line adds `over $25 budget` in red. Set `session_budget_action` to `block` to have the message stopped once instead; send it again within 2 minutes to go ahead. If the cache is also cold, both warnings arrive as one message. It keeps working with `statusline_cost` off. |
| Resume | Warns when a resume re-sends a big history after the cache has expired. |
| Keep-warm (opt-in, `--keepwarm`) | A background Stop hook. After each of Claude's turns it waits quietly; if you haven't typed by shortly before the cache expires (`keepwarm_lead_minutes`, 5, before a 1h cache runs out; 1 minute before a 5m one), it wakes Claude with *reply with only: ok, no tools, no task*. That reply reads the cache, which resets its timer. It only pings when the rebuild would pass the cold-message floor, stops once the pings would add up to more than one rebuild (about 38 on a 1h Opus 5.5 cache, about 1.7 hours' worth on a 5m one; models without a price get a conservative cap) or after `keepwarm_max_hours` (12), with a 24-hour cap on top. It won't ping once the cache has gone cold, going by Claude Code's own expiry time (a Mac that slept through, a long final reply), or a session you closed. Any new turn cancels it: typing, a `/loop`, a scheduled task. Each ping is a full turn, so your other Stop hooks run after it too, and Claude Code may show its waiting-for-input notification. The status line shows `kept warm ×3` while it's working. Your Mac has to stay awake: run `caffeinate -i` while you're away. |
| Bash trimmer (opt-in) | Output over 12,000 chars keeps its first 30 lines, last 60 lines and every error or warning line. The full log is saved to `~/.claude/cache-guard/logs/` and Claude is told where. Add `# no-trim` to a command to skip trimming. |

The hooks only show messages to you. They never inject text into Claude's context, so they can't break the cache themselves. The one exception is opt-in keep-warm, whose ping asks Claude for a one-word reply.

### `doctor`

Scans for settings that silently break the cache, waste tokens or leak secrets:

- the `opusplan` model setting
- caching disabled or forced to 5 minutes
- tool search turned off
- a gateway that may strip cache markers
- other hooks that inject `additionalContext` on every tool call
- everything loaded into every session, added up: CLAUDE.md files here and in parent folders, their `@imports`, this folder's auto-memory `MEMORY.md`, and rules without a `paths:` scope. Over 15k tokens is flagged.
- secrets in those files and in memory files (API keys, tokens, private keys, `password=` lines), by file and line. The value itself is never printed, and references such as `${VAR}` or `process.env.X` are not flagged.
- request interceptors: `NODE_OPTIONS` preloading code, `HTTPS_PROXY` with its own certificate, or `ANTHROPIC_BASE_URL` on localhost. They can rewrite TTLs and cache markers, so the figures you see may not be what Anthropic billed. Proxy and base URLs are printed with any password hidden.
- missing output caps

### `report`

**`report [session-id | path.jsonl]`** shows one session: tokens, cache hit rate, cost at list price, and every real cache miss with a likely cause (idle past the TTL, a model switch, a resume or restart, changed tools, or unknown). It then shows the same totals for the session's subagents and names the three most expensive ones. With no argument it uses the latest session for the current folder. The start of a session id is enough.

A miss is a request whose cached prefix shrank, the same test Claude Code's own miss counter uses. A large write on a warm cache is new content, not a miss, and is not listed. Session start and compaction are shown separately as expected writes.

**`report --all [--days N]`** covers every session from the last N days (default 30):

- cost split between main conversations and subagents
- cache rebuilds grouped by cause, with their share of main-conversation cost
- the five sessions with the most avoidable rebuilds
- a verdict on subagent cache length: what the other setting (5m or 1h) would have cost on your own transcripts, and whether to change `subagentPromptCacheTtl`

Costs use API list prices built into the script. On a subscription they show relative size, not a bill.

### Lean output style

`lean.md` tells Claude to lead with the result and skip recaps. It also tells Claude to point at code as `path:line`, use quiet commands, filter long logs, grep before reading, and hand noisy digging to a subagent. It keeps Claude Code's built-in coding instructions. Pick it in `/config`, or install with `--lean`.

### Settings

Thresholds live in `~/.claude/cache-guard/config.json`. Install adds new keys to it and keeps the values already there. A config.json written by an earlier install says `"cold_prompt": "block"`; change it to `"warn"` to get the current default.

| Key | Default | Meaning |
| - | - | - |
| `notify` | false | Desktop alert before expiry |
| `notify_minutes` | 3 | How early to alert |
| `expiry_warn_minutes` | 3 | When the status line warns about expiry |
| `context_warn_pct` | 75 | When to suggest `/compact` |
| `limit_warn_pct` | 80 | Share of a usage limit used (%) at which it turns yellow and the tip warns (red from 90%) |
| `keepwarm` | false | Keep the cache warm while you're away (`install --keepwarm` sets it and wires the hook) |
| `keepwarm_lead_minutes` | 5 | On a 1h cache, how long before it expires to ping (a 5m cache always uses 1 minute) |
| `keepwarm_max_hours` | 12 | Stop keeping a session warm after this long away |
| `pace_forecast` | true | Say when the recent pace uses a 5-hour limit up before it resets |
| `pace_window_minutes` | 30 | How far back the pace is measured |
| `resume_warn_tokens` | 50000 | Minimum history size for the resume warning |
| `compact_confirm_seconds` | 120 | Window to confirm a cold `/compact` |
| `cold_prompt` | warn | Cold-message guard: `warn` only tells you, `block` stops the message once, `off` disables it |
| `cold_prompt_warn_usd` | 0.5 | On priced models, the smallest re-read cost (list price) the cold-message guard acts on; 0 = use tokens only |
| `cold_prompt_warn_tokens` | 100000 | Smallest history the cold-message guard acts on, for models without a price |
| `cold_prompt_confirm_seconds` | 120 | With `block`, the window to send a stopped message again |
| `session_budget_usd` | 0 | Session budget at list price; 0 = off |
| `session_budget_action` | warn | `warn` only tells you, `block` stops the message once |
| `session_budget_confirm_seconds` | 120 | With `block`, the window to send a stopped message again |
| `pricing` | {} | Price overrides, per million tokens: `{"model-id-prefix": [input, 5m write, 1h write, cache read, output]}` |
| `statusline_cost` | true | Show session cost, avoidable cost and cache savings on the status line |
| `statusline_totals` | true | Show the all-projects line |
| `totals_days` | 30 | Days of sessions the all-projects line covers |
| `totals_refresh_minutes` | 10 | How old the all-projects figures may get before a background scan |
| `avoidable_warn_pct` | 15 | Avoidable share of the session's cost (%) at which that figure turns yellow |
| `trim.max_chars` | 12000 | Only trim output longer than this |
| `trim.head_lines`, `trim.tail_lines` | 30, 60 | Lines kept at the start and end |
| `trim.max_error_lines` | 80 | Most error lines kept from the middle |
| `trim.keep_logs_days` | 3 | How long full logs are kept |

### Uninstall

```bash
python3 ~/.claude/cache-guard/cache_guard.py uninstall
rm -rf ~/.claude/cache-guard ~/.claude/output-styles/lean.md
```

### Tests

```bash
python3 tests/test_cache_guard.py   # 200 checks against a throwaway HOME
```

### Caveats

- Tested with simulated Claude Code inputs, not in a live session. Field names come from Claude Code's docs and changelog.
- The trimmer replaces what Claude sees. If Claude needs the full output, it can grep the saved log.
- If your Claude Code version doesn't let hooks stop `/compact`, you'll see the warning and it will compact anyway.
- Causes in `report` are inferred from timing, model changes and markers in the transcript. `/usage` shows Claude Code's own diagnosis.
- Prices are list prices from November 2026 and will go stale. Models without a price are counted in tokens and left out of cost. Fix either with `pricing` in the config.
- The cold-message guard, the session budget and the pace forecast rely on the status line having run. With `cold_prompt` set to `block`, it also relies on Claude Code saying who sent the prompt. If your version doesn't say, the guard warns instead of stopping.
- Keep-warm relies on Claude Code's background hooks (`async`, `asyncRewake`). Claude Code's docs don't say whether a woken turn is marked as automatic, so a typed message within 90 seconds of a ping isn't counted as you coming back. Claude is asked to reply with only `ok`, but it may occasionally say more. Each ping adds a few dozen tokens to the history. Keep-warm knows the session is still open because the cache-guard status line keeps refreshing; if your Claude Code doesn't refresh status lines while idle, it won't ping.
- The subagent cache-length verdict prices writes and pauses inside a subagent. It can't tell whether a longer cache would have warmed subagents that started cold.
