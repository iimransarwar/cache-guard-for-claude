# Handoff before the cache dies (opt-in)

Design, chosen by Claude at the user's request ("decide the best way and build it"). Saves a short summary of a
long session while its prompt cache is still warm, so coming back after the cache has died can start a fresh session
from that summary (a few thousand tokens) instead of re-reading the whole history at full price.

## Why

30 days of the user's transcripts: 51 main-conversation rebuilds after going idle past the cache TTL cost $278 at list
price (10% of main cost, ~$5.45 each). By idle gap: 1-12 h was 44% of those tokens, which keep-warm now covers;
12 h+ (overnight) was 56%, which keep-warm deliberately doesn't (12 h cap, break-even allowance). Resumes and restarts
were negligible ($3.30). Summarising is only cheap while the cache is warm, and keep-warm already knows the minute
before it dies.

## Goals, non-goals, anti-goals

- Goal: a handoff exists for any long session you walked away from, written while warm, and you're told about it,
  with the cost of each choice, when you come back to a cold cache.
- Goal: loading it is your choice, in one command, in a fresh session.
- Non-goal: long-term memory, search across sessions, automatic retrieval (claude-mem's territory; Claude Code has
  CLAUDE.md and its own memory folder).
- Anti-goal: anything that runs on every tool call, starts background Claude sessions, or puts text into sessions you
  didn't ask it to. Anti-goal: a handoff turn that costs more than the ping it replaces.

## Approaches

- A (chosen): the first keep-warm ping of an away stretch, once you've been away `handoff_after_minutes` (20), asks
  for a handoff instead of "ok". It reads the cache like a ping (resetting its timer), so the only extra cost is the
  handoff's output tokens (~$0.03-0.08). Claude replies with plain text, so no file permission is needed while you're
  away; the Stop hook saves it from the transcript.
- B: build the handoff locally from the transcript (Claude Code's own recap, last requests and replies, files and
  commands). Free and works when the Mac slept, but an excerpt rather than a summary. Possible later fallback.
- Ruled out: `/compact` before death (hooks and Claude can't run slash commands); auto-injecting a handoff into new
  sessions (claude-mem style: puts text into sessions meant to be clean, costs every session).

## How

- Config `handoff` (false), `handoff_after_minutes` (20), `handoff_keep_days` (30). `install --handoff` sets it and
  wires the same background Stop hook as keep-warm (`hook-keepwarm`); `--no-handoff` clears it. Works with keep-warm
  off: one wake to write the handoff, then the cache is left to expire.
- The request contains the keep-warm marker, so the prompt hook treats it as a ping turn, not the user coming back.
  It asks for: first line `# cache-guard handoff`, then Goal, State, Decisions, Open questions, Next steps, Key files
  and commands; under 500 words; no tools; don't continue the task.
- Capture: at every Stop, when handoff is on, the newest assistant text of the turn that ended (joined across its
  records) is saved if it starts with the header line. One file per session,
  `~/.claude/cache-guard/handoffs/<session>.md`, first line an HTML comment with JSON metadata (session, cwd, time,
  model, history size). The same capture saves a handoff written on demand with `/handoff-save`.
- Load: install writes two commands to `~/.claude/commands/`: `/handoff-save` (the same request, on demand) and
  `/handoff-load`, which runs `cache_guard.py handoff-show` through the command file's `!` shell step and asks Claude to
  treat the notes as notes, check the files before acting, summarise in two or three lines and wait. `handoff-show`
  prints the newest handoff saved for the current folder. A command file that isn't cache-guard's is never
  overwritten or removed.
- Pointers: when a handoff for this session was saved after your last typed message, the cold-message guard, the
  cold resume warning and the cold status-line tip say so: `/clear`, then `/handoff-load` (~N tokens) instead of
  re-reading ~M. The status line shows `handoff HH:MM` next to `kept warm ×N`.
- Housekeeping: handoffs older than `handoff_keep_days` are deleted with the other old files; uninstall removes the
  two commands.

## Promise that changes

Like keep-warm, this puts a message in Claude's conversation (the handoff request and its reply) through Claude
Code's own hooks. It never adds text to a session unless you run `/handoff-load`. README says so.

## Not proven

How a woken turn is recorded in the transcript isn't documented, so capture keys on the reply's header line, not on
the request. A Mac that sleeps before the first ping gets no handoff.
