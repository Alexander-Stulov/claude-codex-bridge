# Design: fork a thread, and read one without taking it

Status: approved in conversation, 2026-09-16
Base: `tune-14-1` at `a1bb587` (same commit as `master`), bridge 0.14.0, codex-cli 0.154.0

## Problem

codex lets one process write a thread at a time. Loading a thread (`thread/start`,
`thread/resume`, `codex exec resume`) takes an exclusive file lock on
`~/.codex/thread-writer-locks/<id>.lock`, and the process keeps it until the thread
unloads or the process exits. The bridge never unloads a thread, so its app-server
keeps every thread it has started or resumed for as long as Claude Desktop runs.

That breaks mixed use in both directions:

- Any other process that tries to continue a bridge thread is refused with
  `-32600 "thread <id> already has an active writer"`. That includes a codecraft
  script running `codex exec resume`, the Codex app, `codex` in a terminal, and the
  second bridge process that Desktop runs.
- When the bridge meets a thread that another process holds, `attach_thread` reports
  `unknown thread '<id>': codex could not resume it`, which reads as if the thread
  vanished.
- `codex_poll` resumes a thread it isn't tracking just to read its results. That takes
  the lock and keeps it until the bridge exits.

## Verified behaviour

These results come from codex-cli 0.154.0, using probes that ran no model turns. In
each case one process held the thread and a second process made the call.

| Call from the second process | Result |
|---|---|
| `thread/resume`, `codex exec resume`, `codex exec resume --ephemeral` | refused: `-32600 "thread <id> already has an active writer"` |
| `thread/read` with `includeTurns: false` | ok in 4–13 ms; no lock taken |
| `thread/turns/list` with `limit: 1, sortDirection: "desc", itemsView: "full"` | ok in 3–13 ms; returns the last turn with its items; no lock taken |
| `thread/fork` (saved), `codex exec fork` | ok: a new thread id with the full history; only the new id is locked |
| `thread/fork` with `ephemeral: true` | requires `excludeTurns: true`; creates no lock |

The design also relies on these facts:

- Every resume failure uses code `-32600`, so only the message tells them apart:
  `already has an active writer`, `no rollout found for thread id <id>`, and
  `invalid session id: …`. `thread/read` and `thread/turns/list` word the same two cases
  differently. An id with no saved thread answers `thread not loaded: <id>`, and a
  malformed id answers `invalid thread id: …`.
- codex attaches the forking connection as a listener of the new thread. Its source
  says so in `thread_fork_inner`: "Auto-attach a conversation listener when forking a
  thread". Turn events for the fork therefore reach the bridge without a resume.
  `turn/start` never attaches a listener.
- `Thread.createdAt`, `Thread.updatedAt`, `Turn.startedAt` and `Turn.completedAt` are
  Unix seconds. The `Thread` record carries `cwd`, `model`, `reasoningEffort`,
  `forkedFromId` and `status`.
- A turn with no saved end reads as `interrupted` with `completedAt: null`. That shape
  was found at integration, verified live on 2026-09-16 and confirmed in codex source.
  It has four causes:
  - the turn is still running in another process;
  - the turn is a copy frozen at a fork;
  - the process running it died, which leaves the turn without an end forever;
  - it was stopped by a codex version that did not save a finish time.

  Three codex behaviours produce it:
  - codex rewrites every in-progress turn to `interrupted` for a thread that is not
    active in the reading process (`normalize_thread_turns_status`);
  - `thread/fork` of a mid-turn source writes an abort with no finish time into the
    fork (`append_interrupted_boundary`);
  - a process killed mid-turn writes no end at all.

  A turn its owner interrupted is saved with `completedAt` and `durationMs`. A fork's
  copied turns started before the fork's `createdAt`, while its own turns start at or
  after it; a turn submitted in the same second as the fork starts exactly at
  `createdAt`.

## Decisions

1. `codex_poll` never takes a thread the bridge is not running. It reads.
2. Forking gets its own tool, `codex_fork`. It is documented for two uses: branching a
   line of work on purpose, and continuing a thread that another process holds.
3. Sometimes `codex_submit` or `codex_compact` cannot take a thread because another
   process holds it. The bridge then refuses, with an explanation that points at
   `codex_fork`; a fork can be compacted too. The bridge never forks on its own: the
   thread id is the caller's handle to the work, and a silent change would break that.

## Design

### 1. `codex_fork(thread, cwd?)`

A new tool. It is listed right after `codex_submit`, both in `TOOLS` and in
`manifest.json`.

Inputs: `thread` (required) and `cwd` (optional, an absolute path that
`resolve_workspace` validates).

Flow:

1. Call `APP.ensure()`, then `windows_gate()`. If the gate pins a Windows sandbox
   mode, the request carries it as `config: {"windows.sandbox": <mode>}`, the same way
   `thread/start` does.
2. Send `thread/fork` with `threadId`, `approvalPolicy: "on-request"`,
   `approvalsReviewer: "user"`, and `cwd` when one was given. The fork is saved rather
   than ephemeral, so its id is a durable handle.
3. Take the new id from `result.thread.id`. A missing id is a `CodexError`, as it is
   for `thread/start`.
4. Cache the fork like a thread the bridge started, with these fields:
   - state `idle`, with no turn
   - mode unknown until the first turn sets it
   - `forked_from` set to the source id
   - model slug from (`result.model`, `result.reasoningEffort`) through `WIRE_TO_SLUG`
   - cwd from the argument, falling back to `result.cwd` and then `result.thread.cwd`
   - marked `inherited_cwd` when no `cwd` was passed
5. Return `{thread, forked_from, state: "idle", cwd, workspace, model}`. Every later
   `codex_poll` of the fork also carries `forked_from`.

A `codex_submit` to the new id goes straight to `turn/start`. Two things make that
safe: the fork is cached under the current app-server generation, and codex has
already attached this connection to it.

An inherited working directory follows the rule a resumed thread follows today.
`refuse_reason` is checked whenever `codex_submit` sends a turn without a `cwd` to a
thread marked `resumed` or `inherited_cwd`. A refusal reads
`thread <id> works in <cwd>: <reason>. Pass cwd to move it.`

Errors from `thread/fork` go through the classifier in section 3.

The tool description says, in substance:

> Copy a thread's full saved history to a new thread id, then codex_submit to the new
> id to continue there. There are two uses. On purpose: branch a line of work, such as
> trying another direction, while the original stays exactly as it was. As a
> workaround: when codex_submit or codex_compact answers reason held_elsewhere, the
> thread is open in another process. Fork it and keep working on the copy, compaction
> included; the original stays with that process. codex copies what it has saved, so a
> turn still running elsewhere comes over only up to its last saved step. After the
> fork the two threads are independent, so record the new id with the work. cwd
> defaults to the original thread's working directory.

### 2. `codex_poll` reads threads it is not running

Some threads are in the bridge's cache under the current app-server generation,
because this process started them, forked them, or re-attached them through
`codex_submit` or `codex_compact`. Polls of those threads behave exactly as today,
with live progress, pending approvals and output.

An entry from an earlier generation is not a thread this child is running. Such an
entry is left in the cache for `codex_submit`'s existing stale handling, but the child
that ran it has since restarted. A turn that was active then is already marked failed
by the reader, and the thread may have been continued elsewhere since. Poll treats
such an entry like any uncached thread and reads it.

The bridge reads any other thread instead of resuming it, and caches nothing:

1. Send `thread/read` with `includeTurns: false`. It returns `cwd`, `model`,
   `reasoningEffort`, `updatedAt` and `forkedFromId`.
2. Send `thread/turns/list` with `limit: 1`, `sortDirection: "desc"` and
   `itemsView: "full"`. It returns the last turn.
3. Derive the state from that turn through the existing `_adopt_thread_record`
   mapping:
   - `completed` gives the output, with structured detection as today.
   - `interrupted` with a `completedAt` gives interrupted, with any output: its owner
     stopped it.
   - `interrupted` without a `completedAt` has no saved end. It gives interrupted when
     it is a copy frozen at a fork. That means the record has a `forkedFromId` and the
     turn either started strictly before the thread's `createdAt`, has no `startedAt`,
     or started in that very second and the source thread has a turn with the same id.
     A fork keeps the ids of the turns it copies, and whole-second times cannot
     otherwise separate a copied turn from the fork's own. The source is looked up only
     in that same-second case, and a source that cannot be read counts as not a copy.
     Otherwise it gives `running`, because it may be live in another process.
     Owner decision, 2026-09-16: fix the knowable cases, and accept that a turn whose
     other process died reads as running with growing quiet time.
   - `failed` gives the error.
   - `inProgress` gives `running`.
   - A thread with no turns is `idle`.

   Status flags are not used. `thread/read` describes the thread from this process's
   side, where a thread held elsewhere shows as `notLoaded`. The turn is handed to
   `_adopt_thread_record` without the thread's status, so that helper's active-flag
   branch never applies to a read.
4. For a `running` read, `activity.running_seconds` comes from the turn's `startedAt`
   and `activity.quiet_seconds` from the thread's `updatedAt`. When the holder has
   stalled or died, the quiet time keeps growing.

   This bridge can recognise one death: its own. It may still hold a stale entry for
   the thread whose cached state is `failed`, set by the reader when its app-server
   child exited mid-turn. When that entry's `turn_id` equals the read's last turn id
   and the read would say `running`, the poll answers `failed` with the cached error
   instead: that turn will never finish.
5. The answer carries `read_only: true`, never `resumed`. It also carries
   `forked_from` when the record has a `forkedFromId`, and provenance as today with
   `mode` unknown.

A failure of either request, `thread/read` or `thread/turns/list`, goes through the
classifier in section 3.

A read never attaches. As a result, `codex_poll` does not surface approval requests
for a thread it is not running; those belong to the process running the turn.

### 3. Refusals when a thread cannot be taken

A single classifier turns a `CodexError` from `thread/resume`, `thread/fork`,
`thread/read` or `thread/turns/list` into the error the caller sees:

| codex message contains | Raised | MCP `outcome` |
|---|---|---|
| `already has an active writer` | `ThreadHeldElsewhere` (a `ValueError`), with the text below | `rejected`, plus `reason` |
| `no rollout found`, `thread not loaded` | `ValueError`: `unknown thread '<id>': codex has no saved thread with that id` | `rejected` |
| `invalid session id`, `invalid thread id` | `ValueError`: `'<id>' is not a thread id` | `rejected` |
| anything else | the original `CodexError`, unchanged | `codex_error`, with codex's payload verbatim |

The first three describe the thread or the id the caller passed, so they are refusals.
Anything else is codex failing, and it reaches the caller exactly as codex said it.
Today every resume failure becomes `rejected`, so the last row changes the outcome for
unexpected failures only.

The held-elsewhere text:

> thread <id> is open in another process: another Claude Desktop connection, the Codex
> app, codex in a terminal, or a script. codex lets one process write a thread at a
> time, and that process keeps the thread until it unloads it or exits. codex_poll
> still reads its latest results. To continue now, codex_fork it and use the new
> thread, which can be compacted too. Otherwise retry once that process lets go.

For `ThreadHeldElsewhere`, `handle()` adds `"reason": "held_elsewhere"` to the MCP
error body, next to the existing `outcome` and `thread`. The class subclasses
`ValueError`, so in-process callers such as codecraft's `load_bridge()` keep working
unchanged.

After this change, only `codex_submit` and `codex_compact` call `attach_thread`.

Stale entries get the same treatment everywhere. An entry is stale when its `gen`
differs from `APP.gen`:

- `codex_compact` treats a missing or stale entry the way `codex_submit` does. It
  drops the entry and re-attaches through `attach_thread` before sending
  `thread/compact/start`. Today it re-attaches only a missing entry, so a stale one
  sends compaction to a child that never loaded the thread.
- `codex_interrupt` on a missing or stale entry sends nothing to codex. It raises a
  `ValueError`:
  `thread <id> is not running in this plugin: a turn running in another process has to be stopped there`.
  Today a missing entry raises `unknown thread`, and a stale one sends `turn/interrupt`
  for a turn the current child never had.

### 4. Instructions and docs

- `INSTRUCTIONS`, DURABLE paragraph:
  - `codex_submit` re-attaches a thread and answers `resumed: true`.
  - `codex_poll` reads its latest saved results without taking it and answers
    `read_only: true`.
  - Only one process can write a thread at a time. When another process has the thread
    open, `codex_submit` answers `held_elsewhere`, and `codex_fork` continues the work
    on a new id.
- Tool descriptions:
  - `codex_fork` as described above.
  - `codex_poll` says it reads threads it is not running.
  - `codex_submit` and `codex_compact` name `held_elsewhere` and `codex_fork`.
- README:
  - add a `codex_fork` row to the tools table
  - update the durable-threads section
  - add `tests/fork_live.py` to the tests list
- `manifest.json`: add `codex_fork` to the tools list, in `TOOLS` order.

## Testing

These tests run in CI and need no codex binary:

- `tests/smoke.py` runs against a fake app-server:
  - `codex_poll` on an uncached thread sends `thread/read` and `thread/turns/list`,
    and never `thread/resume`. It maps completed, failed, interrupted, in-progress and
    no-turn records. Its answers carry `read_only: true`, `quiet_seconds` from
    `updatedAt`, and `forked_from`. Unknown and malformed ids get their messages.
  - When a resume fails with the active-writer message, `codex_submit` and
    `codex_compact` raise `held_elsewhere` with text that names `codex_fork`. The MCP
    error body carries `reason`. The other three failure classes get their messages.
  - `codex_fork` sends `thread/fork` with `threadId`, the approval fields, and `cwd`
    when given. It caches the fork as idle with `forked_from`. A following
    `codex_submit` to the new id sends `turn/start` without `thread/resume`. When
    `refuse_reason` rejects an inherited cwd, the turn is refused until a `cwd` is
    passed.
  - A stale-generation entry is read by `codex_poll` (`read_only: true`) rather than
    answered from the cache. `codex_compact` re-attaches it before
    `thread/compact/start`. `codex_interrupt` refuses it, and a missing entry, without
    sending anything to codex.
  - Unexpected errors from `thread/resume`, `thread/read` and `thread/turns/list` keep
    MCP outcome `codex_error` with codex's payload. A `thread/turns/list` failure goes
    through the classifier like a `thread/read` failure.
  - The tools list includes `codex_fork`.
- `tests/windows_sim.py`: `thread/fork` carries the pinned sandbox mode, as
  `thread/start` does.
- `tests/protocol_conformance.py`: every field the new code reads must exist in the
  fixture. If a field is missing from the codex-cli 0.153.4 fixture, regenerate the
  fixture from the installed codex with `tests/regen_schema_keys.py`.
- `tests/pack_check.py`: the manifest's tools match `TOOLS`.

A new live test, `tests/fork_live.py`, needs codex and stays out of CI. It uses a few
luna-medium turns:

1. The bridge starts a thread whose first turn records a code word.
2. A separate `codex app-server` resumes that thread, standing in for another process.
   The test confirms that process holds the lock.
3. A `codex_submit` to the thread is refused with `reason: "held_elsewhere"`.
4. `codex_poll` returns the code-word answer with `read_only: true`. The other process
   still holds the lock afterwards.
5. The other process starts a turn, and `codex_poll` reports `running` for it.
6. `codex_fork` returns a new id. A `codex_submit` asking for the code word completes
   on the fork with the right answer, which also shows that the fork's events reach
   the bridge.
7. `codex_compact` on the fork succeeds.

`tests/resume_live.py`: the poll-only step expects `read_only: true` and no attach,
instead of `resumed: true`.

## Version and release

Set 0.15.0 in `SERVER_INFO` and `manifest.json`, and add
`docs/release-notes/v0.15.0.md`.

## Out of scope

- Releasing finished threads (`thread/unsubscribe`, `thread_unload_delay_secs`).
- codecraft's scripts. Moving `codex-apply-review.py` from `codex exec resume` to
  `codex exec fork` is a codecraft change.
- Ephemeral forks, and forking from an earlier turn (`lastTurnId`).
- The DevLoop3 copy of the bridge.
- Installing the new version, which stays the owner's step.
