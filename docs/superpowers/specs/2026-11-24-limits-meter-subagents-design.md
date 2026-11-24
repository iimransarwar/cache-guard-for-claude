# Usage-limit-aware keep-warm, context meter, subagent guards, report what-ifs

From a research pass over Claude Code's docs, practitioner reports and one user's 30-day spend. Nothing here depends
on any particular plugin.

## Keep-warm around usage limits

The status line saves every usage limit's used % and reset time in the session state. Before pinging, keep-warm:

- **Bridges short lockouts.** A limit at or above `keepwarm_limit_pct` (95) that hasn't reset yet: if it resets before
  the cache expires, wait until just after the reset and ping then; if it resets after, stand down (the cache can't
  survive the lockout, and pinging would spend the last of the quota). A limit whose reset time has passed counts as
  reset, since the status line's figure only updates with the next request.
- **Stands down on a subscription's 5-minute cache.** Claude Code drops the main cache to 5 minutes once a subscriber
  draws on extra-usage credits; pinging every 4 minutes there rarely pays. API-key sessions keep their behaviour.
- **Leaves very large histories alone.** Above `keepwarm_max_tokens` (700k) it stands down, so Claude Code's idle
  compaction can shrink the session while the cache is warm instead of a ping keeping 700k+ alive.

## Status line

- **Context meter:** from 200k tokens of history, `ctx 337k · $0.07/turn`: what re-reading the history costs every
  turn at list price. Yellow from `context_warn_tokens` (400k), which also triggers the compact/clear tip (previously
  only at 75% of the window, i.e. 750k on a 1M window).
- **Pre-lockout tip:** when the pace forecast runs a limit out and the lockout outlasts the cache, the tip says so and
  suggests compacting or writing a handoff first, with the rebuild cost.

## Subagents

- `SubagentStop` hook (installed with the other guards): when one subagent run costs at least `subagent_warn_usd` (5)
  at list price, a message to the user (never Claude) with its cost and how to put subagents on a cheaper model.
- `doctor`: when `CLAUDE_CODE_SUBAGENT_MODEL` isn't set, note that subagents, Explore included since v2.1.198, run on
  the session's model.

## report

- `report --all` reprices the same tokens on Opus 5.5, Sonnet 5.5 and Haiku 5.5 (quality not compared).
- A miss right after a turn that added more than 20 content blocks is labelled as Claude Code's known 20-block cache
  issue instead of "cause not in transcript".

## Not doing

- Plugin-specific accounting (any one plugin is optional).
- Keep-warm as an in-process mod (`$.model.fork`): promising, but needs a live test of the mods API first.
