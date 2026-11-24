# Keep-warm while you're away (opt-in)

Approved design. Keeps a long session's prompt cache warm while the user is away, so coming back doesn't pay a full
rebuild, without the user typing `/loop`.

## How

- `install --keepwarm` wires a Stop hook `hook-keepwarm` with `"async": true, "asyncRewake": true` and a 13-hour
  `timeout`, and sets `keepwarm: true` in config. `--no-keepwarm` removes both. Re-running install keeps it, like the
  trimmer. Off by default.
- Each Stop arms a background timer with a fresh token in the session state. It polls every 30 s (wall clock, so a
  sleeping Mac is noticed) and quits quietly when a newer turn re-armed, a typed prompt arrived, or keep-warm was
  turned off.
- Ping time: cache expiry minus `keepwarm_lead_minutes` (5) on a 1h cache; 1 minute before expiry on a 5m cache.
  Expiry = when the turn ended + the TTL the status line last saw.
- At ping time it fires (exit 2, message on stderr to Claude: reply only `ok`, no tools, no task) only if all hold:
  still warm (not past expiry), the status line ran in the last 2 minutes (session still open), the rebuild passes the
  cold-message floor (`cold_prompt_warn_usd`, else `cold_prompt_warn_tokens`), pings so far in this away stretch are
  below break-even (rebuild cost ÷ ping cost), and the stretch is under `keepwarm_max_hours` (12).
- Away stretch: starts at the first Stop after a typed prompt; continues while the last event was our ping. A prompt
  within 90 s of a ping is the ping's own turn, not the user coming back.
- Status line: `kept warm ×N` in the cache part during a stretch.

## Promises that change

Keep-warm is the one feature that puts a message in Claude's conversation and makes Claude Code send requests. It
does so through Claude Code's own hook system: still no network access in cache-guard and no login token. README says
so, including the comparison table.

## Not proven

Docs don't say whether the woken turn is marked, or whether async hooks die when the session exits (the heartbeat check
covers that). Tested with simulated inputs; the user verifies live by setting `keepwarm_lead_minutes` to 57.
