# Pace forecast, dollar threshold, session budget, doctor upgrades

Ideas picked from a safe read of other cache and usage tools, rebuilt the cache-guard way: local, stdlib only, no proxy,
no login token, nothing written into Claude's context. The user chose these four; calls below are the builder's.

## 1. Usage-limit pace forecast

- "58% left" doesn't say whether you'll run out. Show it only when the recent pace empties a limit before it resets:
  `All models: 42% left (5h, out in ~40m)`, and a tip: `At this pace the 5-hour limit for all models runs out in ~40m,
  1.5h before it resets (15:10).`
- Rate = rise in used % over the last `pace_window_minutes` (30), from samples the status line keeps in
  `~/.claude/cache-guard/pace.json` (one per limit per minute). Needs at least 5 minutes of samples and a 1-point rise,
  so idle time or a single reading never produces a forecast. A new reset time starts the samples over.
- Better than whole-window averages, which idle hours drag down. Config: `pace_forecast` (true), `pace_window_minutes`.
- Tip priority: after "cache about to expire", before "limit over 80% used".

## 2. Dollar threshold for the cold-message guard

- 100k tokens costs very different amounts on Haiku and Opus. New `cold_prompt_warn_usd` (0.50): when the model is
  priced, the guard acts when re-reading the history costs at least that much at the real cache-write rate for the TTL.
  Unpriced models fall back to `cold_prompt_warn_tokens`. Set it to 0 for tokens only.

## 3. Session budget guard

- `session_budget_usd` (0 = off). Uses the session cost the status line already keeps (main conversation plus subagents).
- Status line: `over $25 budget` in red after the cost figures once passed.
- Typed prompts (never automated ones or slash commands): warn when the session first passes the budget and again at
  each further multiple. `session_budget_action: block` stops the prompt once instead; sending it again within
  `session_budget_confirm_seconds` (120) goes ahead. Shares the hook with the cold-message guard; both messages are
  combined into one.

## 4. doctor

- Total always-loaded context: CLAUDE.md files, their `@imports` (up to 5 deep), auto-memory `MEMORY.md` for this
  folder, and `.claude/rules/*.md` without a `paths:` scope (project and user). Warn above 15k tokens, note above 6k.
- Secrets in always-loaded files and memory files (API keys, tokens, private keys, `password=` lines): warn with
  file:line, never print the value.
- Request interceptors: `NODE_OPTIONS` preloading code, `HTTPS_PROXY` plus `NODE_EXTRA_CA_CERTS`, or
  `ANTHROPIC_BASE_URL` on localhost. They can rewrite TTLs and cache markers, so cache-guard's costs and miss causes
  would be wrong.

## Not doing

Keep-warm pings (spend requests and quota), request rewriting, reading the login token, auto-update.
