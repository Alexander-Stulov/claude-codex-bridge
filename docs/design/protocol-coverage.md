# Design: what the bridge should use from the codex protocol

Status: **proposal — awaiting decision**
Base: 0.7.0 (`5f906ac`), codex-cli 0.147.0

## Why this exists

Four capability gaps were found in one working session — sub-agents, streaming
liveness, thread recovery, image input — each because a question happened to poke
the right spot, not because anyone had read the protocol end to end. Three finds
per hour from unsystematic probing implies more remain. This is the systematic
pass: every method classified once, so the next gap is a decision rather than a
discovery.

Measured coverage at 0.7.0:

| Surface | Used | Exists |
|---|---|---|
| ClientRequest (calls we make) | 7 | 95 |
| ServerNotification (codex tells us) | 11 | 70 |
| ServerRequest (codex asks us) | 6 | 10 |
| ThreadItem types | 9 | 18 |

Low coverage is not itself a defect — most of the 95 are desktop-host concerns.
The defects are the specific things below.

## Findings that are bugs today

**F1 — the bridge dispatches on two methods that do not exist.**
`_on_event` branches on `thread/error` and `turn/failed`. Neither is in the
protocol. Failures are still caught, because `turn/completed` carries a
non-completed `status` and `_complete_turn` handles it — so this is dead code,
not a broken path. But it is dead code that *looks* like failure handling, which
is worse than none.

**F2 — the real `error` notification is unhandled.**
`error` exists, is thread-scoped, and carries **`willRetry`**. A transient error
that codex will retry and a fatal one are indistinguishable to us right now.

**F3 — the conformance test cannot catch F1 or F2.**
It validates *field* names against the schema, not *method* names. That
asymmetry is the same one that let `availableDecisions` return None for weeks.

## Proposal

### A. Adopt (clear value, low cost)

| Item | Why | Cost |
|---|---|---|
| `error` notification | F2. Surface `willRetry` so a controller waits instead of failing over. | ~6 lines |
| `turn/plan/updated` | Carries `plan` + `explanation` — the model's own plan. This is the honest answer to "what is it doing", which `activity.now` only approximates. | ~5 lines |
| `turn/diff/updated` | Cumulative diff of the turn. Answers "what did it change" without a `git diff` round trip. Snapshot-shaped: replace, never append. | ~5 lines |
| `thread/compacted` + `contextCompaction` item | On an hours-long thread, context *will* be trimmed. The caller should know its earlier instructions may no longer be in view. | ~4 lines |
| `plan` item type | Same signal at item granularity. | ~3 lines |
| `model/list` | `MODELS` hardcodes wire slugs and effort names. Adding `gpt-6-astra` proved the hazard is real and silent: `thread/start` accepts *any* model string and *any* effort string, so a slug this table gets wrong (or a CLI too old to know it) fails at turn time, not at dispatch. `model/list` is free and already present — validate the map at `codex_check` time and report drift. Note it must stay lazy: `codex_check` deliberately does not spawn the app-server, and `tests/smoke.py` asserts that. | ~15 lines |
| Method-name conformance (F3) | Extend the existing test to assert every dispatched method exists. Would have caught F1 the day it landed. | ~10 lines |

### B. Adopt with a design decision needed

**`item/tool/requestUserInput` — let codex ask questions.**
Codex can ask the caller structured questions mid-turn (`questions[]`,
`isBlocking`). We auto-decline it, so codex cannot ask Claude anything. This is
the interactive capability originally asked for, and it is currently off.

*Recommended shape: no new tool.* A question is another thing codex is parked
on, exactly like an approval. Add it to `APPROVAL_KINDS` as `kind: "question"`,
let it surface in `poll.requests`, and extend `codex_approve` with an `answer`
field. Tool count stays at five; the caller's mental model stays "codex is
waiting on me — `codex_poll` tells me what for."

**`review/start` — codex's built-in review mode.**
Verified a real API, not a TUI affordance. `ReviewTarget` is one of
`uncommittedChanges`, `baseBranch{branch}`, `commit{sha,title}`, or
`custom{instructions}`; `ReviewDelivery` is `inline` (current thread) or
`detached` (a new thread, id returned as `reviewThreadId`).

That last part matters: **detached delivery needs no new bridge machinery.** The
returned thread id is an ordinary codex thread, and `_recover` already lets
`codex_poll` answer for a thread the bridge never created — the same property
that made sub-agent polling free. So "review this branch against main" costs one
call plus existing poll, not a new subsystem.

### C. Defer (real, but not now)

- **`thread/fork`** — branch a thread from shared context (`lastTurnId`, own
  `cwd`/`sandbox`). The natural primitive for "try two approaches from the same
  understanding". Pairs with worktrees. Wants its own design.
- **`item/tool/call` / `dynamicToolCall`** — codex calling Claude-side tools.
  Powerful, and it fits the same parked-request pattern, but it inverts the
  dispatch direction and needs tool registration. Not before B lands.
- **`thread/rollback`** (`numTurns`) — rewind a turn that went wrong.
- **`thread/compact/start`** — manual compaction to extend a long thread.
- **`thread/list`** — enumerate real codex threads rather than bridge-known ones;
  would make `codex_check` truthful across sessions.
- **`thread/approveGuardianDeniedAction`** + `guardianWarning` — unknown whether
  a guardian denial is silently blocking work today. Needs a probe, not a guess.
- **`reasoning` item / `item/reasoning/summaryPartAdded`** — currently counted as
  `thinking_chars` only. Surfacing summaries may help; may also be noise.

### D. Deliberately ignore (with reason)

- `fs/*`, `command/exec*`, `fuzzyFileSearch`, `mcpServer/tool/call` — Claude has
  its own file, shell and search tools. Routing them through codex adds a hop
  and no capability.
- `account/*`, `app/*`, `plugin/*`, `marketplace/*`, `config/*`,
  `experimentalFeature/*`, `externalAgentConfig/*`, `feedback/*`,
  `permissionProfile/*`, `mcpServerStatus/*`, `modelProvider/*`,
  `configRequirements/*`, `hooks/list`, `threadSection/*` — desktop-host
  concerns. The bridge is a dispatcher, not a codex UI.
- `thread/realtime/*`, `*/outputAudio/*` — voice. Out of scope.
- `windowsSandbox/*`, `windows/worldWritableWarning` — platform-specific; the
  bridge runs where codex runs.
- `thread/archive|delete|unarchive|metadata|name|goal/*`, `threadSection/*` —
  library housekeeping; codex owns its own store.
- `attestation/generate`, `account/chatgptAuthTokens/refresh` — host-managed
  auth. The codex CLI manages its own credentials.
- `userMessage`, `hookPrompt` items — our own input echoed back.
- `imageView`, `imageGeneration`, `sleep` items — no caller decision depends on
  them. Revisit `imageView` if image input turns out to need debugging.

## Principles this should hold to

1. **Five tools.** New capability arrives as a field on an existing tool unless
   it genuinely changes the interaction model. "codex is waiting on you" is one
   concept, not four.
2. **Snapshot, not log.** Anything added to `activity` is replaced or counted,
   never appended. Polls stay idempotent.
3. **Claim only what codex asserts.** `sub_agent_status` is absent under V1
   rather than stale — that is the standard. An event is not a status.
4. **Guard what we read.** Every field *and now every method* checked against the
   committed schema, so a codex upgrade fails the build instead of going quiet.

## Open questions

1. **Questions (B1) — approve the "fifth kind of parked request" shape,** or keep
   auto-decline until there is a concrete need?
2. **`review/start`** — now verified as a real API with detached delivery and
   near-zero integration cost. Adopt it, or keep running the Sol-reviews-Opus
   loop as a normal thread with a review brief (which already works, and gives
   full control over the brief)? The tradeoff is codex's built-in review prompt
   versus our own.
3. **`thread/fork` — is branch-from-context wanted now,** given the orchestrator
   already splits work across worktrees?
