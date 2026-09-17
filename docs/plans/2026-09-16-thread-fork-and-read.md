# Thread Fork and Read-Only Poll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use codecraft:subagent-driven-development (recommended) or codecraft:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let callers continue a codex thread that another process holds (`codex_fork`), read any thread without taking it (read-only `codex_poll`), and get an explanation instead of `unknown thread` when codex refuses a thread.

**Architecture:** All behaviour lives in `server.py`, the single-file MCP server. A classifier turns codex's `-32600` refusals into caller-facing errors. `codex_poll` reads any thread that this app-server child is not running with `thread/read` plus `thread/turns/list`, and never resumes it. A new `codex_fork` tool wraps `thread/fork` and caches the fork like a thread the bridge started. The existing CI scripts gain coverage for each path, and a new live test drives the whole flow against real codex.

**Tech Stack:** Python 3 standard library only; the codex app-server JSON-RPC protocol (codex-cli 0.154.0 installed, schema fixture codex-cli 0.153.4); the MCPB manifest.

**Spec:** `docs/specs/2026-09-16-thread-fork-and-read-design.md`

## Global Constraints

**Repository and version**
- Python standard library only, in `server.py` and in tests. Add no dependencies.
- Work on branch `tune-14-1` in `/Users/alexanderstulov/DevMcp/claude-codex-bridge`, and never push.
- Set version 0.15.0 in `SERVER_INFO` and `manifest.json`, with release notes at `docs/release-notes/v0.15.0.md`.

**Refusal classifier**
- Every codex refusal arrives as code `-32600`. Classify by these message substrings, exactly:
  - `already has an active writer` gives `ThreadHeldElsewhere`.
  - `no rollout found` and `thread not loaded` give "unknown thread".
  - `invalid session id` and `invalid thread id` give "not a thread id".
- Anything else is re-raised as the original `CodexError`.
- **Spec amended, approved by the owner on 2026-09-16:** `invalid thread id`. On codex-cli 0.154.0, `thread/read` and `thread/turns/list` report a malformed id with that text, while `thread/fork` and `thread/resume` use `invalid session id`. The spec's table now lists both.

**Wire parameters and markers**
- `thread/fork` params are `threadId`, `approvalPolicy: "on-request"` and `approvalsReviewer: "user"`, plus `cwd` when given and `config: {"windows.sandbox": <mode>}` when the Windows gate pins a mode. The fork is saved, never ephemeral.
- The read path sends `thread/read` with `{"threadId": <id>, "includeTurns": false}`, then `thread/turns/list` with `{"threadId": <id>, "limit": 1, "sortDirection": "desc", "itemsView": "full"}`. A read never sends `thread/resume` and never caches.
- Poll answers from the read path carry `read_only: true` and never `resumed`. A poll answer carries `forked_from` whenever it is known. The MCP error body for a held thread carries `"reason": "held_elsewhere"`.
- A cache entry is live only when `st["gen"] == APP.gen`.
- Every `.get("key")` on a wire payload must name a field present in `tests/fixtures/app-server-schema-keys.json`, which `tests/protocol_conformance.py` enforces. Every field this plan reads is already present.

**Contract changes**

Changes stay additive, except for three deliberate ones:
- `codex_poll` no longer attaches threads that the bridge is not running.
- Unexpected resume, fork and read failures return `outcome: codex_error` instead of `rejected`.
- `codex_interrupt` on a missing or stale entry raises the new "not running in this plugin" text.

**Environment**
- CI tests need no codex binary, and they run on both `ubuntu-latest` and `windows-latest` (`.github/workflows/ci.yml`). Any fake app-server that reaches the Windows gate must answer `config/read`.
- Live tests need codex with `thread/turns/list` and the fork listener attach, both verified on 0.154.0. The controller runs them at integration, never inside an executor turn: codex's sandbox cannot run a nested codex that writes `~/.codex`.

**Out of scope**
- Releasing finished threads.
- codecraft's scripts.
- Ephemeral forks and `lastTurnId`.
- The DevLoop3 bridge copy.
- Running `--install`.

## Slices

**One slice: `thread-access`.** Every change here alters how the bridge takes, reads, copies and explains codex threads, inside one deployable: the `.mcpb`, which holds `server.py` and `manifest.json`.

| Component \ Layer | codex wire calls | thread cache and tool handlers | MCP surface (`TOOLS`, `INSTRUCTIONS`, `handle`, manifest) | docs and release |
|---|---|---|---|---|
| thread-access | `thread/fork`, `thread/read`, `thread/turns/list` | classifier, `_live_entry`, poll, compact, interrupt, fork, submit's cwd rule | `codex_fork` tool, descriptions, `reason` field | README, release notes, version |

**Why the candidate split was merged.** The work could be split into three components: fork, read-only poll, and refusal classifier. They fail the separation test:
- They share one vocabulary: thread, attach, held elsewhere.
- One invariant must hold across all of them: what counts as a live cache entry.
- They read and write the same `APP.threads` cache.
- The refusal text points callers at `codex_fork`.

They are one component, so there are no cross-slice boundaries.

**External surface.** One surface is consumed outside this plan: by Claude sessions over MCP, and by codecraft's in-process `load_bridge()`. It is recorded so its compatibility rule is explicit.

| Field | MCP tool surface |
|---|---|
| Owner | thread-access |
| State ownership | thread-access owns `APP.threads`; codex owns the threads themselves |
| Authoritative source | `server.py`: `TOOLS`, `HANDLERS`, `INSTRUCTIONS`, `handle()`; `manifest.json` `tools` |
| Dependency direction | Claude sessions and codecraft scripts depend on the bridge; the bridge owns the surface |
| Consumer access | MCP `tools/list` and `tools/call` over stdio; in-process `import server` through codecraft's `load_bridge()` |
| Translation | `thread_access_error` (Task 2) translates codex `-32600` messages into caller errors; `read_thread_state` (Task 3) translates codex thread records into poll snapshots |
| Binding | `python3 tests/smoke.py` (tool set, schemas, instructions, error body) and `python3 tests/pack_check.py <packed .mcpb>` (manifest tools == `TOOLS`, versions match) |
| Compatibility rule | additive-only, except the three deliberate changes in Global Constraints |
| Mock removal | Task 4 replaces Task 1's `codex_fork` stand-in |
| Readiness | Task 1 green: `py_compile`, `smoke.py`, `protocol_conformance.py`, `windows_sim.py`, manifest validate, pack + `pack_check.py` |

## Integration

The controller runs integration after Task 8, from the repository root.

**CI suite** (no codex needed):

```bash
python3 -m py_compile server.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
python3 tests/enable_1m_sim.py
```

**Packaging:**

```bash
npx -y @anthropic-ai/mcpb@latest validate manifest.json
mkdir -p .codecraft/pack/build_dir .codecraft/pack/dist
[ -f .codecraft/.gitignore ] || printf '*\n' > .codecraft/.gitignore
cp server.py manifest.json .codecraft/pack/build_dir/
npx -y @anthropic-ai/mcpb@latest pack .codecraft/pack/build_dir .codecraft/pack/dist/claude-codex-bridge.mcpb
cp scripts/enable-1m-context.sh scripts/enable-1m-context.ps1 .codecraft/pack/dist/
python3 tests/pack_check.py .codecraft/pack/dist/claude-codex-bridge.mcpb .codecraft/pack/dist/enable-1m-context.sh .codecraft/pack/dist/enable-1m-context.ps1
```

**Live tests** (need codex and cost model turns; the controller runs them):

```bash
python3 tests/fork_live.py        # S–W: refusal, read without taking, running elsewhere, fork, compact the fork
python3 tests/resume_live.py      # P, Q, Q2 (read-only poll keeps provenance), R (submit attaches after a read)
python3 tests/liveness_live.py    # I (recovery is now a read), I2 (unknown and malformed ids), J (liveness)
python3 tests/lifecycle_live.py   # steer, interrupt, crash recovery, cold resume: the attach paths still work
```

Together these cover every path the spec changes. `fork_live.py` is the end-to-end check of the new behaviour. The other three guard the attach, recovery and interrupt paths this change touches.

---

### Task 1: MCP surface for fork, read-only poll and refusals

**Phase:** surface
**Slice:** thread-access
**Depends on:** none

**Files:**
- Modify: `server.py`
  - the module docstring's tool list (lines 14-22)
  - `INSTRUCTIONS`, DURABLE paragraph (line 62)
  - after `class CodexError` (lines 370-375)
  - after `codex_compact` (ends near line 1499)
  - `TOOLS` (lines 1502-1589)
  - `HANDLERS` (lines 1591-1594)
  - `handle()` error body (lines 1639-1649)
- Modify: `manifest.json`, the `tools` list
- Test: `tests/smoke.py`
  - lines 40-41, the tool set
  - after line 53, the `codex_submit` schema checks
  - a new section appended at the end of the file

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class ThreadHeldElsewhere(ValueError)`, with class attribute `reason = "held_elsewhere"`.
  - `handle()` copies any raised exception's `reason` attribute into the MCP error body as `"reason"`.
  - `HANDLERS["codex_fork"]` calls `codex_fork(args: dict) -> dict`. In this task it is a stand-in that raises `ValueError`; Task 4 replaces it.
  - A `TOOLS` entry named `codex_fork`, placed right after `codex_submit`. Its `inputSchema` properties are `thread` (required) and `cwd` (optional).

- [ ] **Step 1: Write the failing tests**

In `tests/smoke.py`, replace lines 40-41:

```python
assert tools == {"codex_check", "codex_submit", "codex_poll", "codex_approve", "codex_interrupt",
                 "codex_compact", "codex_capabilities"}, tools
```

with:

```python
assert tools == {"codex_check", "codex_submit", "codex_fork", "codex_poll", "codex_approve", "codex_interrupt",
                 "codex_compact", "codex_capabilities"}, tools
```

In `tests/smoke.py`, directly after the line `assert set(props["mode"]["enum"]) == {"read", "write"}, props["mode"]`, insert:

```python
# codex_fork: the thread to copy is required, where the copy works is optional, and it
# sits next to codex_submit, which is what a caller reaches for after it
fork_tool = next(t for t in out[2]["result"]["tools"] if t["name"] == "codex_fork")
assert fork_tool["inputSchema"]["required"] == ["thread"], fork_tool["inputSchema"]
assert set(fork_tool["inputSchema"]["properties"]) == {"thread", "cwd"}, fork_tool["inputSchema"]["properties"]
_served = [t["name"] for t in out[2]["result"]["tools"]]
assert _served.index("codex_fork") == _served.index("codex_submit") + 1, _served
```

Append at the end of `tests/smoke.py`:

```python
# --- 0.15.0: the surface for fork, read-only poll and refusals -------------------------
# A caller has to learn at the decision points that a thread can be open in another
# process, that polling never takes it, and that codex_fork is how to keep working.
_durable = bridge.INSTRUCTIONS.split("DURABLE:")[1].split("LONG THREADS:")[0]
for phrase in ("codex_fork", "held_elsewhere", "read_only: true", "resumed: true", "another process"):
    assert phrase in _durable, f"DURABLE guidance lost {phrase!r}"
_desc = {t["name"]: t["description"] for t in bridge.TOOLS}
for _name, _phrases in (("codex_fork", ("held_elsewhere", "On purpose", "independent", "last saved step")),
                        ("codex_submit", ("held_elsewhere", "codex_fork")),
                        ("codex_compact", ("held_elsewhere", "codex_fork")),
                        ("codex_poll", ("read_only", "not running"))):
    for _phrase in _phrases:
        assert _phrase in _desc[_name], f"{_name} description lost {_phrase!r}"

# a refusal that carries a reason stays an ordinary rejection, names the reason and the thread
def _held(_args):
    raise bridge.ThreadHeldElsewhere("thread t70 is open in another process")

bridge.HANDLERS["_smoke_held"] = _held
try:
    _resp = bridge.handle({"method": "tools/call",
                           "params": {"name": "_smoke_held", "arguments": {"thread": "t70"}}})
    _body = json.loads(_resp["content"][0]["text"])
    assert _resp.get("isError") and _body == {"outcome": "rejected", "thread": "t70", "reason": "held_elsewhere",
                                              "error": "thread t70 is open in another process"}, _body
    assert isinstance(bridge.ThreadHeldElsewhere("x"), ValueError), "in-process callers catch ValueError"
    # an ordinary refusal carries no reason
    _resp = bridge.handle({"method": "tools/call",
                           "params": {"name": "codex_submit", "arguments": {"prompt": "hi", "model": "gpt-9"}}})
    assert "reason" not in json.loads(_resp["content"][0]["text"]), _resp
finally:
    del bridge.HANDLERS["_smoke_held"]
print("smoke: codex_fork is on the surface; refusals carry their reason; guidance names fork and read-only poll")
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `python3 tests/smoke.py`
Expected: FAIL with `AssertionError` on the tool set, because `codex_fork` is missing from the served tools.

- [ ] **Step 3: Add `ThreadHeldElsewhere` and the reason in `handle()`**

In `server.py`, directly after the `CodexError` class (the one whose `__init__` sets `self.payload = payload`), add:

```python
class ThreadHeldElsewhere(ValueError):
    """Another process has the thread open. codex lets one process write a thread at a
    time, so this is a refusal the caller can act on (fork it, or wait), not codex failing.
    A ValueError, so in-process callers that already catch refusals keep working."""
    reason = "held_elsewhere"
```

In `handle()`, replace:

```python
            thread = getattr(e, "codex_thread", None) or args.get("thread")
            if thread:
                body["thread"] = thread
```

with:

```python
            thread = getattr(e, "codex_thread", None) or args.get("thread")
            if thread:
                body["thread"] = thread
            # A refusal the caller can act on names its reason, so a script branches on it
            # without parsing prose: held_elsewhere means codex_fork.
            reason = getattr(e, "reason", None)
            if reason:
                body["reason"] = reason
```

- [ ] **Step 4: Add the stand-in, the tool entry, the descriptions and the handler**

In `server.py`, directly after the end of `def codex_compact(args):` (the function returns `{"thread": tid, "state": "running", "note": "compaction started — poll until it completes; the thread keeps its id"}`), add:

```python
def codex_fork(args):
    """Stand-in until the fork itself lands: the tool goes on the surface first, so the
    manifest, schema and guidance are checked before anything is built behind them."""
    raise ValueError("codex_fork is not available in this build yet")
```

In `TOOLS`, in the `codex_submit` entry, replace the description:

```python
        "description": ("Start a codex thread or add a turn to one. No thread → new session; a thread that is idle → "
                        "next turn with full prior context; a thread mid-turn → the input steers the running turn. "
                        "Returns instantly: poll with codex_poll. Pass output_schema on any turn to get JSON back. Inject a plugin skill "
                        "with skills (codex_capabilities lists their names)."),
```

with:

```python
        "description": ("Start a codex thread or add a turn to one. No thread → new session; a thread that is idle → "
                        "next turn with full prior context; a thread mid-turn → the input steers the running turn. "
                        "Returns instantly: poll with codex_poll. Pass output_schema on any turn to get JSON back. Inject a plugin skill "
                        "with skills (codex_capabilities lists their names). A thread another process has open — "
                        "another Claude Desktop connection, the Codex app, codex in a terminal, a script — is refused "
                        "with reason held_elsewhere: codex_fork it to continue on a new id."),
```

Directly after the closing `},` of the `codex_submit` entry, and before the `codex_poll` entry, insert:

```python
    {
        "name": "codex_fork",
        "description": ("Copy a thread's full saved history to a new thread id, then codex_submit to the new id "
                        "to continue there. Two uses. On purpose: branch a line of work — try another direction — "
                        "while the original stays exactly as it was. As a workaround: when codex_submit or "
                        "codex_compact answers reason held_elsewhere, the thread is open in another process; fork it "
                        "and keep working on the copy, compaction included, while the original stays with that "
                        "process. codex copies what it has saved, so a turn still running elsewhere comes over only "
                        "up to its last saved step. From then on the two threads are independent: record the new id "
                        "with the work. cwd defaults to the original thread's working directory."),
        "inputSchema": {"type": "object", "properties": {
            "thread": {"type": "string", "description": "The thread to copy."},
            "cwd": {"type": "string", "description": "Absolute path the fork works in. Omit to keep the original thread's working directory."}},
            "required": ["thread"], "additionalProperties": False},
    },
```

In the `codex_poll` entry, replace the description:

```python
        "description": ("Where the thread is right now: state (running | awaiting_approval | completed | "
                        "failed), an activity snapshot (what it is doing and how much it has done), the "
                        "pending approval request(s) when it is waiting — a command, file change, permission "
                        "or an MCP elicitation, each with what is being asked — and the complete output "
                        "once it is done. Idempotent — nothing is consumed, and the answer arrives whole rather than in "
                        "pieces to reassemble. Poll every 20-30s and relay activity in plain language."),
```

with:

```python
        "description": ("Where the thread is right now: state (running | awaiting_approval | completed | "
                        "failed), an activity snapshot (what it is doing and how much it has done), the "
                        "pending approval request(s) when it is waiting — a command, file change, permission "
                        "or an MCP elicitation, each with what is being asked — and the complete output "
                        "once it is done. Idempotent — nothing is consumed, and the answer arrives whole rather than in "
                        "pieces to reassemble. Poll every 20-30s and relay activity in plain language. A thread this "
                        "bridge is not running is read from codex's saved record without taking it (read_only: true): "
                        "its latest results, never its approvals, which belong to the process running it."),
```

In the `codex_compact` entry, replace the description:

```python
        "description": ("Summarise this thread's history now, at a point you choose. Optional: codex "
                        "does it automatically when the thread fills up, but then the summary lands "
                        "wherever the work happens to be. Call this between turns once results are "
                        "banked and activity.context.tokens_before_compaction is getting small. The "
                        "thread keeps its id and stays usable; poll until it completes."),
```

with:

```python
        "description": ("Summarise this thread's history now, at a point you choose. Optional: codex "
                        "does it automatically when the thread fills up, but then the summary lands "
                        "wherever the work happens to be. Call this between turns once results are "
                        "banked and activity.context.tokens_before_compaction is getting small. The "
                        "thread keeps its id and stays usable; poll until it completes. A thread open in "
                        "another process is refused with reason held_elsewhere: codex_fork it and compact the fork."),
```

Replace `HANDLERS`:

```python
HANDLERS = {"codex_check": codex_check, "codex_capabilities": codex_capabilities,
            "codex_submit": codex_submit, "codex_poll": codex_poll,
            "codex_approve": codex_approve, "codex_interrupt": codex_interrupt,
            "codex_compact": codex_compact}
```

with:

```python
HANDLERS = {"codex_check": codex_check, "codex_capabilities": codex_capabilities,
            "codex_submit": codex_submit, "codex_fork": codex_fork, "codex_poll": codex_poll,
            "codex_approve": codex_approve, "codex_interrupt": codex_interrupt,
            "codex_compact": codex_compact}
```

- [ ] **Step 5: Rewrite the module docstring's tool list, the DURABLE paragraph and the manifest tools**

In the `server.py` module docstring, replace:

```
Exposed as seven MCP tools:
  codex_check      readiness, models, live threads
  codex_capabilities  what codex can do here: plugins and their $skills, MCP servers, apps
  codex_submit     new thread or next turn (or steer a running one); per-turn model,
                   mode, cwd and JSON output schema
  codex_poll       snapshot: state, what it is doing now, pending approvals, final output
```

with:

```
Exposed as eight MCP tools:
  codex_check      readiness, models, live threads
  codex_capabilities  what codex can do here: plugins and their $skills, MCP servers, apps
  codex_submit     new thread or next turn (or steer a running one); per-turn model,
                   mode, cwd and JSON output schema
  codex_fork       copy a thread's history to a new id: branch it, or continue a thread
                   another process has open
  codex_poll       snapshot: state, what it is doing now, pending approvals, final output;
                   a thread this bridge is not running is read without taking it
```

In `server.py` `INSTRUCTIONS`, replace the whole line that starts with `DURABLE:`:

```
DURABLE: the thread id is the handle to the work. codex stores threads, not this bridge, so keep the id and pick the same thread up tomorrow or next week - codex_poll or codex_submit re-attaches it automatically and the answer carries resumed: true. Record the thread id with whatever the work belongs to; it is the only thing needed to continue.
```

with:

```
DURABLE: the thread id is the handle to the work. codex stores threads, not this bridge, so keep the id and pick the same thread up tomorrow or next week - codex_submit re-attaches it automatically and the answer carries resumed: true, and codex_poll reads its latest saved results without taking it (read_only: true). One process at a time can write a thread: when another process has it open - another Claude Desktop connection, the Codex app, codex in a terminal, a script - codex_submit and codex_compact answer reason held_elsewhere, and codex_fork copies its history to a new id you can continue at once. Record the thread id with whatever the work belongs to; it is the only thing needed to continue.
```

In `manifest.json`, replace:

```json
    {
      "name": "codex_submit"
    },
```

with:

```json
    {
      "name": "codex_submit"
    },
    {
      "name": "codex_fork"
    },
```

- [ ] **Step 6: Run the binding checks**

Run:
```bash
python3 -m py_compile server.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
```
Expected:
- every command exits 0
- `smoke.py` prints `smoke: codex_fork is on the surface; refusals carry their reason; guidance names fork and read-only poll`
- `protocol_conformance.py` prints `... all present in codex-cli 0.153.4`

Next, run the manifest and pack checks. If this sandbox cannot run `npx`, leave them to the controller, and say so in the report.
```bash
npx -y @anthropic-ai/mcpb@latest validate manifest.json
mkdir -p .codecraft/pack/build_dir .codecraft/pack/dist
[ -f .codecraft/.gitignore ] || printf '*\n' > .codecraft/.gitignore
cp server.py manifest.json .codecraft/pack/build_dir/
npx -y @anthropic-ai/mcpb@latest pack .codecraft/pack/build_dir .codecraft/pack/dist/claude-codex-bridge.mcpb
python3 tests/pack_check.py .codecraft/pack/dist/claude-codex-bridge.mcpb
```
Expected: `Manifest schema validation passes!`, then `pack check: ... ok — v0.14.0, ... 8 tools, platforms ['darwin', 'win32']`.

- [ ] **Step 7: Commit**

```bash
git add server.py manifest.json tests/smoke.py
git commit --only -m "Put codex_fork, read-only poll and held_elsewhere on the tool surface" -- server.py manifest.json tests/smoke.py
```

---

### Task 2: Refusal classifier and live cache entries

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 1

**Files:**
- Modify: `server.py`
  - new `_live_entry` and `thread_access_error`, directly above `def attach_thread` (line 800)
  - `attach_thread`: its docstring and `except CodexError` block (lines 801-832)
  - `codex_interrupt` (lines 1466-1476)
  - the head of `codex_compact` (lines 1478-1489)
- Test: `tests/smoke.py`, a new section appended at the end

**Interfaces:**
- Consumes: `ThreadHeldElsewhere` (Task 1).
- Produces:
  - `_live_entry(tid: str) -> dict | None`: the cached state only when its `gen` equals `APP.gen`.
  - `thread_access_error(tid: str, err: CodexError) -> Exception`: returns `ThreadHeldElsewhere` or `ValueError` for the four known message classes, or `err` itself when nothing matches. Call sites use `raise thread_access_error(tid, e) from None`.

- [ ] **Step 1: Write the failing tests**

Append at the end of `tests/smoke.py`:

```python
# --- 0.15.0: a thread codex will not hand over is explained, not called unknown --------
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: None
_sent = []
_GATE = {"config": {"windows": {"sandbox": "unelevated"}}, "layers": []}   # the Windows runner gates on this

def _refusing(message):
    def fake(method, params, timeout=120):
        _sent.append(method)
        if method == "config/read":
            return _GATE
        if method == "thread/resume":
            raise bridge.CodexError({"code": -32600, "message": message})
        raise AssertionError(f"unexpected {method} after a refused resume")
    return fake

try:
    for _message, _kind, _needle in (
            ("thread t40 already has an active writer", bridge.ThreadHeldElsewhere, "codex_fork"),
            ("no rollout found for thread id t40", ValueError, "unknown thread 't40'"),
            ("invalid session id: invalid character", ValueError, "'t40' is not a thread id")):
        for _tool, _args in (("codex_submit", {"prompt": "go", "model": "luna-medium", "thread": "t40"}),
                             ("codex_compact", {"thread": "t40"})):
            bridge.APP.request = _refusing(_message)
            try:
                bridge.HANDLERS[_tool](_args)
                raise AssertionError(f"{_tool}: {_message!r} must refuse")
            except ValueError as e:
                assert type(e) is _kind and _needle in str(e), (_tool, _message, type(e).__name__, str(e))
            assert "t40" not in bridge.APP.threads, f"{_tool}: a refused attach left a placeholder"
    # over MCP the held refusal is an ordinary rejection that names its reason
    bridge.APP.request = _refusing("thread t40 already has an active writer")
    _resp = bridge.handle({"method": "tools/call", "params": {"name": "codex_compact", "arguments": {"thread": "t40"}}})
    _body = json.loads(_resp["content"][0]["text"])
    assert _resp.get("isError") and _body["outcome"] == "rejected" and _body["reason"] == "held_elsewhere", _body
    assert _body["thread"] == "t40" and "another process" in _body["error"], _body
    # anything codex did not explain is codex failing: verbatim, as a codex_error, with no reason
    bridge.APP.request = _refusing("rollout file is corrupt")
    _resp = bridge.handle({"method": "tools/call", "params": {"name": "codex_compact", "arguments": {"thread": "t40"}}})
    _body = json.loads(_resp["content"][0]["text"])
    assert _body["outcome"] == "codex_error", _body
    assert _body["error"] == {"code": -32600, "message": "rollout file is corrupt"} and "reason" not in _body, _body

    # an entry from an earlier app-server child is re-attached before compacting ...
    def _stale_ok(method, params, timeout=120):
        _sent.append(method)
        return {"config/read": _GATE,
                "thread/resume": {"thread": {"id": "t41", "status": {"type": "idle"},
                                             "turns": [{"id": "u1", "status": "completed", "items": []}]},
                                  "cwd": "/tmp"},
                "thread/compact/start": {}}[method]
    bridge.APP.request = _stale_ok
    _sent.clear()
    _old = bridge._new_thread_state("t41", "/tmp", "write", "luna-medium")
    _old.update(state="completed", gen=bridge.APP.gen - 1)
    bridge.APP.threads["t41"] = _old
    bridge.codex_compact({"thread": "t41"})
    assert [m for m in _sent if m != "config/read"] == ["thread/resume", "thread/compact/start"], _sent
    assert bridge.APP.threads["t41"]["gen"] == bridge.APP.gen, "compaction must run on a re-attached entry"
    # ... a live entry is compacted as it is ...
    _sent.clear()
    bridge.APP.threads["t41"]["state"] = "completed"
    bridge.codex_compact({"thread": "t41"})
    assert _sent == ["thread/compact/start"], _sent
    # ... and interrupt never reaches codex for a thread this child is not running
    _sent.clear()
    _gone = bridge._new_thread_state("t42", "/tmp", "write", "luna-medium")
    _gone.update(state="running", turn_id="u-gone", gen=bridge.APP.gen - 1)
    bridge.APP.threads["t42"] = _gone
    for _tid in ("t42", "t-never-seen"):
        try:
            bridge.codex_interrupt({"thread": _tid})
            raise AssertionError(f"interrupt of {_tid} must be refused")
        except ValueError as e:
            assert f"thread {_tid} is not running in this plugin" in str(e), e
    assert _sent == [], f"interrupt reached codex for a thread it is not running: {_sent}"
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request
    bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: refusals are classified; stale entries re-attach before compaction and are never interrupted")
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `python3 tests/smoke.py`
Expected: FAIL with `AssertionError: ('codex_submit', 'thread t40 already has an active writer', 'ValueError', "unknown thread 't40': codex could not resume it ...")`.

- [ ] **Step 3: Add `_live_entry` and `thread_access_error`**

In `server.py`, directly above `def attach_thread(`, add:

```python
def _live_entry(tid):
    """The cached state of a thread THIS app-server child has loaded, or None. An entry
    from an earlier generation outlived the child that loaded it: codex no longer has
    the thread open here, so it is not ours to report live, compact or interrupt."""
    with APP.lock:
        st = APP.threads.get(tid)
    return st if st is not None and st.get("gen") == APP.gen else None


def thread_access_error(tid, err):
    """The error a caller sees when codex will not resume, fork or read a thread. codex
    sends every such refusal as -32600, so only its message tells them apart. The classes
    matched here describe the thread or the id the caller passed, so they are refusals;
    anything else is codex failing and goes back exactly as codex said it. thread/read
    and thread/turns/list word a malformed id as "invalid thread id", resume and fork as
    "invalid session id"; an unknown well-formed id is "thread not loaded" to the first
    two and "no rollout found" to the others."""
    payload = err.payload if isinstance(err.payload, dict) else {}
    message = str(payload.get("message") or "")
    if "already has an active writer" in message:
        return ThreadHeldElsewhere(
            f"thread {tid} is open in another process: another Claude Desktop connection, the Codex app, "
            f"codex in a terminal, or a script. codex lets one process write a thread at a time, and that "
            f"process keeps the thread until it unloads it or exits. codex_poll still reads its latest "
            f"results. To continue now, codex_fork it and use the new thread, which can be compacted too. "
            f"Otherwise retry once that process lets go.")
    if "no rollout found" in message or "thread not loaded" in message:
        return ValueError(f"unknown thread '{tid}': codex has no saved thread with that id")
    if "invalid session id" in message or "invalid thread id" in message:
        return ValueError(f"'{tid}' is not a thread id")
    return err
```

- [ ] **Step 4: Classify in `attach_thread`, and use live entries in compact and interrupt**

In `attach_thread`, replace the first line of the docstring:

```python
    """Take ownership of a thread this bridge is not currently tracking — pruned from
    the local cache, or held over from a previous bridge process.
```

with:

```python
    """Take ownership of a thread this bridge is not currently tracking — pruned from
    the local cache, or held over from a previous bridge process — before codex_submit
    or codex_compact sends it work.
```

In `attach_thread`, replace:

```python
    except CodexError as e:
        with APP.lock:
            if claimed and APP.threads.get(tid) is placeholder:
                APP.threads.pop(tid, None)
        raise ValueError(f"unknown thread '{tid}': codex could not resume it "
                         f"({json.dumps(e.payload)[:200]})")
```

with:

```python
    except CodexError as e:
        with APP.lock:
            if claimed and APP.threads.get(tid) is placeholder:
                APP.threads.pop(tid, None)
        raise thread_access_error(tid, e) from None
```

Replace the whole of `codex_interrupt`:

```python
def codex_interrupt(args):
    tid = args.get("thread")
    with APP.lock:
        st = APP.threads.get(tid)
    if st is None:
        raise ValueError(f"unknown thread '{tid}'")
    if not st.get("turn_id"):
        return {"thread": tid, "state": st["state"], "note": "no active turn"}
    APP.request("turn/interrupt", {"threadId": tid, "turnId": st["turn_id"]}, timeout=60)
    return {"thread": tid, "state": "interrupted"}
```

with:

```python
def codex_interrupt(args):
    tid = args.get("thread")
    st = _live_entry(tid)
    if st is None:
        # Never seen, or held over from an earlier child: the turn it remembers died with
        # that child, and a turn running in another process is not this bridge's to stop.
        raise ValueError(f"thread {tid} is not running in this plugin: a turn running in another "
                         f"process has to be stopped there")
    if not st.get("turn_id"):
        return {"thread": tid, "state": st["state"], "note": "no active turn"}
    APP.request("turn/interrupt", {"threadId": tid, "turnId": st["turn_id"]}, timeout=60)
    return {"thread": tid, "state": "interrupted"}
```

In `codex_compact`, replace:

```python
    tid = args.get("thread")
    with APP.lock:
        st = APP.threads.get(tid)
    if st is None:
        st = attach_thread(tid)
```

with:

```python
    tid = args.get("thread")
    st = _live_entry(tid)
    if st is None:
        # Missing, or held over from an earlier app-server child that has not loaded it:
        # attach first, as codex_submit does, keeping the working directory it had.
        with APP.lock:
            stale = APP.threads.pop(tid, None)
        st = attach_thread(tid, stale["cwd"] if stale else None)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
python3 -m py_compile server.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
```
Expected: all exit 0. `smoke.py` prints `smoke: refusals are classified; stale entries re-attach before compaction and are never interrupted`.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/smoke.py
git commit --only -m "Explain threads codex will not hand over; never compact or interrupt a stale entry" -- server.py tests/smoke.py
```

---

### Task 3: codex_poll reads threads it is not running

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 2

**Files:**
- Modify: `server.py`
  - new `read_thread_state`, directly after `_adopt_thread_record` (which ends near line 893)
  - `codex_poll`: its head and `out` construction (lines 1253-1287)
- Modify: `tests/resume_live.py`: the docstring R line (line 11), the Q2 block (lines 104-119), the R block prints (lines 121 and 129)
- Modify: `tests/liveness_live.py`: the I block (lines 101-104) and the I2 block (lines 113-120)
- Test: `tests/smoke.py`, a new section appended at the end

**Interfaces:**
- Consumes: `_live_entry` and `thread_access_error` (Task 2).
- Produces:
  - `read_thread_state(tid: str) -> dict`: an uncached state dict shaped like `_new_thread_state`, plus `forked_from` when codex records `forkedFromId`.
  - `codex_poll` answers gain `read_only: true` on the read path, and `forked_from` whenever the state has it.

- [ ] **Step 1: Write the failing tests**

Append at the end of `tests/smoke.py`:

```python
# --- 0.15.0: codex_poll reads a thread it is not running, and never takes it ------------
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: None
_sent = []
_now = int(time.time())

def _record(turns, **thread):
    base = {"id": "t50", "cwd": "/tmp", "model": "gpt-5.6-terra", "reasoningEffort": "high",
            "status": {"type": "notLoaded"}, "updatedAt": _now - 40, "forkedFromId": None, "turns": []}
    base.update(thread)
    def fake(method, params, timeout=120):
        _sent.append(method)
        if method == "thread/read":
            assert params == {"threadId": "t50", "includeTurns": False}, params
            return {"thread": base}
        if method == "thread/turns/list":
            assert params == {"threadId": "t50", "limit": 1, "sortDirection": "desc", "itemsView": "full"}, params
            return {"data": turns, "nextCursor": None}
        raise AssertionError(f"a read-only poll sent {method}")
    return fake

def _turn(status, text=None, **extra):
    items = [{"type": "agentMessage", "id": "i1", "text": text}] if text is not None else []
    return {"id": "u9", "status": status, "items": items, "itemsView": "full", "durationMs": 1200,
            "startedAt": _now - 100, "completedAt": None, "error": None, **extra}

try:
    bridge.APP.request = _record([_turn("completed", '{"verdict": "ok"}')], forkedFromId="t49")
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["read_only"] is True and "resumed" not in _p, _p
    assert _p["state"] == "completed" and _p["output"] == {"verdict": "ok"}, _p       # structured, as a live poll
    assert _p["forked_from"] == "t49" and _p["provenance"]["model_slug"] == "terra-high", _p
    assert _p["provenance"]["mode"] is None and _p["provenance"]["cwd"] == "/tmp", _p["provenance"]
    assert "t50" not in bridge.APP.threads, "a read must not cache the thread"

    bridge.APP.request = _record([_turn("failed", error={"message": "boom"})])
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["state"] == "failed" and _p["error"] == {"message": "boom"}, _p

    bridge.APP.request = _record([_turn("interrupted", "partial")])
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["state"] == "interrupted" and _p["output"] == "partial", _p

    bridge.APP.request = _record([_turn("inProgress")])
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["state"] == "running" and _p["read_only"] is True, _p
    assert 95 <= _p["activity"]["running_seconds"] <= 110, _p["activity"]      # from the turn's startedAt
    assert 35 <= _p["activity"]["quiet_seconds"] <= 50, _p["activity"]         # from the thread's updatedAt

    bridge.APP.request = _record([])
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["state"] == "idle" and "output" not in _p, _p

    # an entry held over from an earlier app-server child is read, not answered from the cache
    _old = bridge._new_thread_state("t50", "/tmp", "write", "luna-medium")
    _old.update(state="failed", error={"message": "app-server exited while the turn was active"},
                gen=bridge.APP.gen - 1)
    bridge.APP.threads["t50"] = _old
    bridge.APP.request = _record([_turn("completed", "done elsewhere")])
    _p = bridge.codex_poll({"thread": "t50"})
    assert _p["read_only"] is True and _p["state"] == "completed" and _p["output"] == "done elsewhere", _p
    assert "thread/resume" not in _sent, _sent

    # ids codex has no record of, or that are not ids at all, are named as such
    for _message, _needle in (("thread not loaded: t50", "unknown thread 't50'"),
                              ("invalid thread id: invalid character", "'t50' is not a thread id")):
        def _missing(method, params, timeout=120, _message=_message):
            raise bridge.CodexError({"code": -32600, "message": _message})
        bridge.APP.request = _missing
        try:
            bridge.codex_poll({"thread": "t50"})
            raise AssertionError(_message)
        except ValueError as e:
            assert _needle in str(e), e

    # a turns/list failure codex did not explain is codex's own error, verbatim
    def _list_breaks(method, params, timeout=120):
        if method == "thread/read":
            return {"thread": {"id": "t50", "cwd": "/tmp"}}
        raise bridge.CodexError({"code": -32603, "message": "turn store unavailable"})
    bridge.APP.request = _list_breaks
    _resp = bridge.handle({"method": "tools/call", "params": {"name": "codex_poll", "arguments": {"thread": "t50"}}})
    _body = json.loads(_resp["content"][0]["text"])
    assert _body["outcome"] == "codex_error" and _body["error"]["message"] == "turn store unavailable", _body
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request
    bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: codex_poll reads threads it is not running — states, stale entries, unknown ids")
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `python3 tests/smoke.py`
Expected: FAIL with `AssertionError: unexpected thread/resume ...` or `a read-only poll sent thread/resume`, because `codex_poll` still attaches uncached threads.

- [ ] **Step 3: Add `read_thread_state`**

In `server.py`, directly after the end of `_adopt_thread_record` (its last statement sets `st["output_unavailable"]`), add:

```python
def read_thread_state(tid):
    """A thread this app-server child is not running, rebuilt from codex's saved record.

    Read, never resumed: resuming takes codex's writer lock, and this bridge keeps what it
    takes for as long as it runs, so a poll alone would lock every other process out of
    the thread. Never cached, so the next poll reads fresh. thread/read describes the
    thread from this process's side, where a thread open elsewhere is notLoaded, so its
    status flags say nothing about a live turn: the state is the last turn's own."""
    APP.ensure()
    try:
        res = APP.request("thread/read", {"threadId": tid, "includeTurns": False}, timeout=60)
        page = APP.request("thread/turns/list", {"threadId": tid, "limit": 1, "sortDirection": "desc",
                                                 "itemsView": "full"}, timeout=60)
    except CodexError as e:
        raise thread_access_error(tid, e) from None
    thread = res.get("thread") or {}
    cwd = thread.get("cwd") or SCRATCH_ROOT
    slug = WIRE_TO_SLUG.get((thread.get("model"), thread.get("reasoningEffort")), thread.get("model"))
    st = _new_thread_state(tid, cwd, None, slug, "scratch" if str(cwd).startswith(SCRATCH_ROOT) else "project")
    forked_from = thread.get("forkedFromId")
    if forked_from:
        st["forked_from"] = forked_from
    turns = page.get("data") or []
    if not turns:
        return st          # no turn yet: idle, as _new_thread_state leaves it
    last = turns[0]
    # The turn alone, without the thread's status, so the active-flag branch cannot fire.
    _adopt_thread_record(st, {"turns": [last]})
    if st["state"] == "running":
        st["started_at"] = last.get("startedAt") or st["started_at"]
        st["changed_at"] = thread.get("updatedAt") or st["changed_at"]
    return st
```

- [ ] **Step 4: Make `codex_poll` read, and mark its answers**

In `codex_poll`, replace:

```python
    tid = args.get("thread")
    with APP.lock:
        st = APP.threads.get(tid)
    if st is None:
        # Pruned from the local cache, or left over from a previous bridge process.
        # codex is the store, so attach rather than lose hours of work to a cache expiry.
        st = attach_thread(tid)
```

with:

```python
    tid = args.get("thread")
    st = _live_entry(tid)
    read_only = st is None
    if read_only:
        # Never seen, pruned, or held over from an earlier app-server child: read what
        # codex has saved instead of attaching, so polling never takes the thread from
        # whichever process has it open.
        st = read_thread_state(tid)
```

In `codex_poll`, replace:

```python
        out = {"thread": tid, "state": state, "activity": activity}
        if st.get("resumed"):
            # the bridge re-attached to an existing thread rather than creating it
            out["resumed"] = True
```

with:

```python
        out = {"thread": tid, "state": state, "activity": activity}
        if read_only:
            out["read_only"] = True
        elif st.get("resumed"):
            # the bridge re-attached to an existing thread rather than creating it
            out["resumed"] = True
        if st.get("forked_from"):
            out["forked_from"] = st["forked_from"]
```

- [ ] **Step 5: Update the live tests that asserted poll re-attaches**

In `tests/resume_live.py`, replace:

```
  R  poll-then-continue   — polling first (which attaches) then submitting works, and
```

with:

```
  R  poll-then-continue   — polling first (which only reads) then submitting works, and
```

In `tests/resume_live.py`, replace:

```python
print("== Q2 attach WITHOUT being told the model: must read it off codex, not default")
c = Bridge()
peek = c.call("codex_poll", {"thread": t})
print(f"   poll-only attach -> resumed={peek.get('resumed')} state={peek['state']} "
      f"model={(peek.get('provenance') or {}).get('model_slug')} "
      f"mode={(peek.get('provenance') or {}).get('mode')}")
assert peek.get("resumed") is True, peek
```

with:

```python
print("== Q2 read WITHOUT being told the model: must read it off codex, not default")
c = Bridge()
peek = c.call("codex_poll", {"thread": t})
print(f"   poll-only read -> read_only={peek.get('read_only')} state={peek['state']} "
      f"model={(peek.get('provenance') or {}).get('model_slug')} "
      f"mode={(peek.get('provenance') or {}).get('mode')}")
assert peek.get("read_only") is True and "resumed" not in peek, peek
assert t not in [x["thread"] for x in c.call("codex_check", {})["threads"]], "a poll must not attach the thread"
```

In `tests/resume_live.py`, replace:

```python
    f"attach claimed mode={pv.get('mode')!r}; it cannot know, and must not guess"
```

with:

```python
    f"the read claimed mode={pv.get('mode')!r}; it cannot know, and must not guess"
```

In `tests/resume_live.py`, replace:

```python
print("== R  poll-then-continue on the same attached thread")
```

with:

```python
print("== R  poll-then-continue: codex_submit attaches the thread a poll only read")
```

and replace:

```python
print(f"   PASS continued after a poll-attach; one entry in the table, cwd unchanged")
```

with:

```python
print(f"   PASS continued after a read-only poll; one entry in the table, cwd unchanged")
```

In `tests/liveness_live.py`, replace:

```python
print(f"   resumed={rec.get('resumed')} state={rec['state']} "
      f"cwd={(rec.get('provenance') or {}).get('cwd')}")
assert rec.get("resumed") is True, "poll answered without marking the result as recovered"
```

with:

```python
print(f"   read_only={rec.get('read_only')} state={rec['state']} "
      f"cwd={(rec.get('provenance') or {}).get('cwd')}")
assert rec.get("read_only") is True, "poll answered without marking the result as read from codex's record"
```

In `tests/liveness_live.py`, replace:

```python
print("== I2 an unknown thread id must fail clearly, not silently")
try:
    b.call("codex_poll", {"thread": "thr_does_not_exist_0000"})
    print("   FAIL: polling a nonexistent thread returned success")
    sys.exit(1)
except RuntimeError as e:
    assert "unknown thread" in str(e), str(e)[:200]
    print("   PASS clear error for a genuinely unknown thread")
```

with:

```python
print("== I2 an unknown thread id, or no thread id at all, must fail clearly, not silently")
for bogus, needle in (("01a0ffff-0000-7000-8000-000000000000", "unknown thread"),
                      ("thr_does_not_exist_0000", "is not a thread id")):
    try:
        b.call("codex_poll", {"thread": bogus})
        print(f"   FAIL: polling {bogus} returned success")
        sys.exit(1)
    except RuntimeError as e:
        assert needle in str(e), str(e)[:200]
print("   PASS clear errors for a well-formed unknown id and a malformed one")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
python3 -m py_compile server.py tests/resume_live.py tests/liveness_live.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
```
Expected: all exit 0. `smoke.py` prints `smoke: codex_poll reads threads it is not running — states, stale entries, unknown ids`. Do not run the `*_live.py` files: the controller runs them at integration.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/smoke.py tests/resume_live.py tests/liveness_live.py
git commit --only -m "Read threads codex_poll is not running instead of taking them" -- server.py tests/smoke.py tests/resume_live.py tests/liveness_live.py
```

---

### Task 4: codex_fork

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 1, Task 2, Task 3 (its test polls the fork and expects `forked_from`, which Task 3's `codex_poll` adds)

**Files:**
- Modify: `server.py`
  - replace the `codex_fork` stand-in from Task 1
  - `codex_submit`'s resumed-cwd check (lines 1169-1175)
- Test: `tests/smoke.py`, a new section appended at the end
- Test: `tests/windows_sim.py`, a new block after the line `print("windows_sim: thread/start pin ok")` (line 109)

**Interfaces:**
- Consumes: `thread_access_error` (Task 2); `resolve_workspace`, `windows_gate`, `_new_thread_state`, `WIRE_TO_SLUG`, `SCRATCH_ROOT` (existing).
- Produces:
  - `codex_fork(args: {"thread": str, "cwd"?: str}) -> {"thread", "forked_from", "state": "idle", "cwd", "workspace", "model"}`.
  - The cached fork state carries `forked_from: str` and `inherited_cwd: bool`.

- [ ] **Step 1: Write the failing tests**

Append at the end of `tests/smoke.py`:

```python
# --- 0.15.0: codex_fork copies a thread to a new id, and the next turn goes straight on ---
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: None
_sent = []
_proj = os.path.realpath(_tf.mkdtemp(prefix="fork-proj-"))
_GATE = {"config": {"windows": {"sandbox": "unelevated"}}, "layers": []}

def _forking(result_cwd):
    def fake(method, params, timeout=120):
        _sent.append((method, params))
        return {"config/read": _GATE,
                "thread/fork": {"thread": {"id": "t61", "cwd": result_cwd, "turns": []},
                                "cwd": result_cwd, "model": "gpt-5.6-sol", "reasoningEffort": "high"},
                "turn/start": {"turn": {"id": "u61"}}}[method]
    return fake

try:
    bridge.APP.request = _forking(_proj)
    _f = bridge.codex_fork({"thread": "t60"})
    assert _f == {"thread": "t61", "forked_from": "t60", "state": "idle", "cwd": _proj,
                  "workspace": "project", "model": "sol-high"}, _f
    _fp = next(p for m, p in _sent if m == "thread/fork")
    assert _fp == {"threadId": "t60", "approvalPolicy": "on-request", "approvalsReviewer": "user"} or \
        (sys.platform == "win32" and _fp.get("config") == {"windows.sandbox": "unelevated"}), _fp
    assert bridge.APP.threads["t61"]["forked_from"] == "t60" and bridge.APP.threads["t61"]["inherited_cwd"] is True
    _p = bridge.codex_poll({"thread": "t61"})
    assert _p["state"] == "idle" and _p["forked_from"] == "t60" and "read_only" not in _p, _p
    # codex attached this connection to the fork: the next turn needs no resume
    _sent.clear()
    _r = bridge.codex_submit({"prompt": "carry on", "model": "sol-high", "thread": "t61"})
    assert _r["thread"] == "t61" and [m for m, _ in _sent if m != "config/read"] == ["turn/start"], _sent

    # an explicit cwd rides the request, and the fork is not marked inherited
    _sent.clear()
    bridge.APP.threads.clear()
    bridge.codex_fork({"thread": "t60", "cwd": _proj})
    assert next(p for m, p in _sent if m == "thread/fork")["cwd"] == _proj, _sent
    assert bridge.APP.threads["t61"]["inherited_cwd"] is False

    # a copied cwd the bridge would refuse holds the next turn until cwd moves it
    bridge.APP.threads.clear()
    _home = os.path.realpath(os.path.expanduser("~"))
    bridge.APP.request = _forking(_home)
    bridge.codex_fork({"thread": "t60"})
    try:
        bridge.codex_submit({"prompt": "go", "model": "sol-high", "thread": "t61"})
        raise AssertionError("a fork working in the home directory must be held")
    except ValueError as e:
        assert "forked thread t61 works in" in str(e) and "Pass cwd to move it" in str(e), e
    _r = bridge.codex_submit({"prompt": "go", "model": "sol-high", "thread": "t61", "cwd": _proj})
    assert _r["cwd"] == _proj, _r

    # fork errors are classified like resume errors; a missing thread is refused before codex
    def _no_such(method, params, timeout=120):
        if method == "config/read":
            return _GATE
        raise bridge.CodexError({"code": -32600, "message": "no rollout found for thread id t60"})
    bridge.APP.request = _no_such
    try:
        bridge.codex_fork({"thread": "t60"})
        raise AssertionError("an unknown source must be refused")
    except ValueError as e:
        assert "unknown thread 't60'" in str(e), e
    try:
        bridge.codex_fork({"thread": " "})
        raise AssertionError("a blank thread must be refused")
    except ValueError as e:
        assert "thread is required" in str(e), e
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request
    bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: codex_fork copies to a new id, the next turn needs no resume, an inherited cwd is held")
```

In `tests/windows_sim.py`, directly after the line `print("windows_sim: thread/start pin ok")`, insert:

```python
# --- thread/fork carries the pin too: a fork is a new thread -------------------------
sent.clear()
def fork_request(method, params=None, timeout=30):
    sent.append((method, params))
    return {"thread/fork": {"thread": {"id": "t-fork"}, "cwd": project}}.get(method, {})
bridge.APP.request = fork_request
bridge.APP.windows_sandbox_mode = lambda: "elevated"
forked = bridge.codex_fork({"thread": "t-new"})
assert forked["thread"] == "t-fork" and forked["forked_from"] == "t-new", forked
fork_params = next(p for m, p in sent if m == "thread/fork")
assert fork_params.get("config") == {"windows.sandbox": "elevated"}, fork_params
bridge.APP.threads.clear()
print("windows_sim: thread/fork pin ok")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 tests/smoke.py`
Expected: FAIL with `ValueError: codex_fork is not available in this build yet`.

- [ ] **Step 3: Implement `codex_fork`**

In `server.py`, replace the stand-in:

```python
def codex_fork(args):
    """Stand-in until the fork itself lands: the tool goes on the surface first, so the
    manifest, schema and guidance are checked before anything is built behind them."""
    raise ValueError("codex_fork is not available in this build yet")
```

with:

```python
def codex_fork(args):
    """Copy a thread's saved history to a new thread id, cached like a thread this bridge
    started. codex takes the writer lock on the NEW id only, so this works while another
    process holds the original: the way to keep working on a thread codex_submit refused
    as held_elsewhere. codex also attaches this connection to the fork, so a turn on it
    needs no resume. Saved, not ephemeral: the new id is a handle that lasts."""
    tid = args.get("thread")
    if not tid or not str(tid).strip():
        raise ValueError("thread is required: the id of the thread to copy")
    APP.ensure()
    params = {"threadId": tid, "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    explicit = None
    if args.get("cwd"):
        explicit = resolve_workspace(args["cwd"])[0]
        params["cwd"] = explicit
    pinned = windows_gate()
    if pinned:
        # a fork is a new thread: bind the verified sandbox mode to it, as thread/start does
        params["config"] = {"windows.sandbox": pinned}
    try:
        res = APP.request("thread/fork", params, timeout=120)
    except CodexError as e:
        raise thread_access_error(tid, e) from None
    thread = res.get("thread") or {}
    new_id = thread.get("id")
    if not new_id:
        raise CodexError({"message": "thread/fork returned no thread id", "result": res})
    cwd = explicit or res.get("cwd") or thread.get("cwd") or SCRATCH_ROOT
    kind = "scratch" if str(cwd).startswith(SCRATCH_ROOT) else "project"
    slug = WIRE_TO_SLUG.get((res.get("model"), res.get("reasoningEffort")), res.get("model"))
    st = _new_thread_state(new_id, cwd, None, slug, kind)
    st["forked_from"] = tid
    # The copied cwd is whatever the original's client chose: codex_submit holds it to the
    # resumed-thread rule until a turn passes cwd.
    st["inherited_cwd"] = explicit is None
    with APP.lock:
        APP.threads[new_id] = st
    return {"thread": new_id, "forked_from": tid, "state": "idle", "cwd": cwd, "workspace": kind, "model": slug}
```

- [ ] **Step 4: Hold an inherited cwd to the resumed-thread rule in `codex_submit`**

In `codex_submit`, replace:

```python
        if st.get("resumed") and not args.get("cwd"):
            # A resumed thread's cwd is whatever another client chose for it. Hold it to
            # the same rule as a thread the bridge starts — before steering or a new turn
            # sends more work there. Inspection and interruption stay available.
            reason = refuse_reason(os.path.realpath(st["cwd"]))
            if reason:
                raise ValueError(f"resumed thread {tid} works in {st['cwd']}: {reason}. Pass cwd to move it.")
```

with:

```python
        if (st.get("resumed") or st.get("inherited_cwd")) and not args.get("cwd"):
            # A resumed thread's cwd is whatever another client chose for it, and a fork's is
            # copied from the thread it came from. Hold both to the same rule as a thread the
            # bridge starts — before steering or a new turn sends more work there.
            # Inspection and interruption stay available.
            reason = refuse_reason(os.path.realpath(st["cwd"]))
            if reason:
                label = "resumed thread" if st.get("resumed") else "forked thread"
                raise ValueError(f"{label} {tid} works in {st['cwd']}: {reason}. Pass cwd to move it.")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
python3 -m py_compile server.py
python3 tests/smoke.py
python3 tests/windows_sim.py
python3 tests/protocol_conformance.py
```
Expected:
- all exit 0
- `smoke.py` prints `smoke: codex_fork copies to a new id, the next turn needs no resume, an inherited cwd is held`
- `windows_sim.py` prints `windows_sim: thread/fork pin ok` and, still, `windows_sim: resumed-cwd rule ok`

- [ ] **Step 6: Commit**

```bash
git add server.py tests/smoke.py tests/windows_sim.py
git commit --only -m "Add codex_fork: copy a thread to a new id and continue there" -- server.py tests/smoke.py tests/windows_sim.py
```

---

### Task 5: Live test for fork and read-only poll

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 3, Task 4

**Files:**
- Create: `tests/fork_live.py`

**Interfaces:**
- Consumes: the MCP tools `codex_submit`, `codex_poll`, `codex_fork`, `codex_compact` and `codex_check`, over stdio to `server.py`; `codex app-server` JSON-RPC for the holder process.
- Produces: `tests/fork_live.py`, which exits 0 and prints `ALL FORK TESTS PASSED` when every stage holds.

- [ ] **Step 1: Write the live test**

Create `tests/fork_live.py`:

```python
#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

codex lets one process write a thread at a time. This drives the bridge against a
thread another codex process has open, the way the Codex app, codex in a terminal or a
script would hold it:

  S  refused, explained — codex_submit answers reason held_elsewhere, naming codex_fork
  T  read, not taken    — codex_poll returns the saved answer read_only, and the other
                          process still holds the thread afterwards
  U  running elsewhere  — a turn the other process runs shows as running
  V  fork and continue  — codex_fork copies the history to a new id; a turn there
                          remembers the original conversation, and its events reach the bridge
  W  compact the fork   — codex_compact works on the copy
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
CODE_WORD = "fork-owl-7731"
repo = tempfile.mkdtemp(prefix="fork-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)


class Bridge:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.inbox, self.lock, self.n = {}, threading.Lock(), 0
        threading.Thread(target=self._r, daemon=True).start()
        self.rpc("initialize", {"protocolVersion": "2024-11-05"})

    def _r(self):
        for line in self.p.stdout:
            line = line.strip()
            if line:
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self.lock:
                    self.inbox[m.get("id")] = m

    def rpc(self, method, params, t=300):
        self.n += 1; rid = self.n
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        end = time.time() + t
        while time.time() < end:
            with self.lock:
                if rid in self.inbox:
                    return self.inbox.pop(rid)
            time.sleep(0.05)
        raise TimeoutError(method)

    def raw(self, n, a, t=300):
        r = self.rpc("tools/call", {"name": n, "arguments": a}, t)
        return bool(r["result"].get("isError")), json.loads(r["result"]["content"][0]["text"])

    def call(self, n, a, t=300):
        failed, body = self.raw(n, a, t)
        if failed:
            raise RuntimeError(json.dumps(body)[:400])
        return body

    def wait(self, thread, limit=300):
        end = time.time() + limit
        while time.time() < end:
            st = self.call("codex_poll", {"thread": thread})
            if st["state"] == "awaiting_approval":
                for q in st.get("requests", []):
                    self.call("codex_approve", {"request_id": str(q["request_id"]), "decision": "allow"})
                continue
            if st["state"] in ("completed", "failed", "interrupted"):
                return st
            time.sleep(3)
        raise TimeoutError("wait")


class Holder:
    """A second codex process with the thread open: the stand-in for the Codex app, codex
    in a terminal, or a script."""

    def __init__(self):
        self.p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.inbox, self.notes, self.cv, self.n = {}, [], threading.Condition(), 0
        threading.Thread(target=self._r, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "fork-live-holder", "title": "fork live holder",
                                                   "version": "0"},
                                    "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _r(self):
        for line in self.p.stdout:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.cv:
                if "id" in m and "method" not in m:
                    self.inbox[m["id"]] = m
                elif "id" in m:
                    # approvalPolicy never means none should come; decline anything that does
                    self._send({"id": m["id"], "result": {"decision": "decline"}})
                else:
                    self.notes.append(m)
                self.cv.notify_all()

    def request(self, method, params, t=120):
        self.n += 1; rid = self.n
        self._send({"id": rid, "method": method, "params": params})
        with self.cv:
            if not self.cv.wait_for(lambda: rid in self.inbox, t):
                raise TimeoutError(method)
            m = self.inbox.pop(rid)
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result") or {}

    def saw(self, method, thread, t):
        def seen():
            return any(n.get("method") == method and (n.get("params") or {}).get("threadId") == thread
                       for n in self.notes)
        with self.cv:
            return self.cv.wait_for(seen, t)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(15)
        except Exception:
            self.p.kill()


def lock_holders(thread):
    """Pids holding codex's writer lock on the thread, or None where lsof is unavailable."""
    if not shutil.which("lsof"):
        return None
    lock = os.path.join(os.path.expanduser("~"), ".codex", "thread-writer-locks", f"{thread}.lock")
    if not os.path.exists(lock):
        return []
    out = subprocess.run(["lsof", "-t", lock], capture_output=True, text=True).stdout.split()
    return sorted(int(pid) for pid in out)


print("== setup: a thread learns a code word, then another process opens it")
a = Bridge()
r = a.call("codex_submit", {"prompt": f"Remember this code word for later: {CODE_WORD}. "
                                      f"Reply with the code word only.",
                            "model": "luna-medium", "cwd": repo, "mode": "read"})
t = r["thread"]
st = a.wait(t)
assert st["state"] == "completed" and CODE_WORD in str(st["output"]), st
a.p.terminate(); a.p.wait(timeout=20)          # its app-server exits with it and lets the thread go
time.sleep(2)

holder = Holder()
holder.request("thread/resume", {"threadId": t, "approvalPolicy": "never"})
held = lock_holders(t)
if held is not None:
    assert held == [holder.p.pid], f"the holder should own {t}'s writer lock; lsof says {held}"
print(f"   {t} is open in pid {holder.p.pid}")

b = Bridge()
try:
    print("== S  codex_submit is refused with held_elsewhere, and told about codex_fork")
    failed, body = b.raw("codex_submit", {"prompt": "What was the code word?", "model": "luna-medium",
                                          "thread": t, "mode": "read"})
    assert failed and body.get("outcome") == "rejected" and body.get("reason") == "held_elsewhere", body
    assert "codex_fork" in body["error"] and body.get("thread") == t, body
    print("   PASS refused with reason held_elsewhere")

    print("== T  codex_poll reads the saved answer without taking the thread")
    peek = b.call("codex_poll", {"thread": t})
    assert peek.get("read_only") is True and peek["state"] == "completed", peek
    assert CODE_WORD in str(peek["output"]), peek
    if held is not None:
        assert lock_holders(t) == [holder.p.pid], "a poll took the writer lock"
    assert t not in [x["thread"] for x in b.call("codex_check", {})["threads"]], "a poll attached the thread"
    print("   PASS read_only answer; the holder still has the thread")

    print("== U  a turn the holder runs shows as running")
    holder.request("turn/start", {"threadId": t, "approvalPolicy": "never",
                                  "input": [{"type": "text", "text": "Run exactly this shell command and wait "
                                                                      "for it: sleep 25 . Then reply DONE."}]})
    snap, running = None, False
    for _ in range(30):
        snap = b.call("codex_poll", {"thread": t})
        if snap["state"] == "running":
            running = True
            assert snap.get("read_only") is True, snap
            print(f"   running_seconds={snap['activity'].get('running_seconds')} "
                  f"quiet_seconds={snap['activity'].get('quiet_seconds')}")
            break
        time.sleep(1)
    assert running, f"a turn running in another process never showed as running: {snap}"
    assert holder.saw("turn/completed", t, 180), "the holder's turn did not finish"
    print("   PASS the other process's turn was visible as running")

    print("== V  codex_fork copies the thread; a turn on the copy remembers the conversation")
    f = b.call("codex_fork", {"thread": t})
    fork = f["thread"]
    assert fork != t and f["forked_from"] == t and f["state"] == "idle", f
    assert os.path.realpath(f["cwd"]) == os.path.realpath(repo), (f["cwd"], repo)
    b.call("codex_submit", {"prompt": "What was the code word I asked you to remember at the start "
                                      "of this conversation? Reply with the code word only.",
                            "model": "luna-medium", "thread": fork, "mode": "read"})
    st = b.wait(fork, limit=240)
    assert st["state"] == "completed" and CODE_WORD in str(st["output"]), st
    assert b.call("codex_poll", {"thread": fork}).get("forked_from") == t
    if held is not None:
        assert lock_holders(t) == [holder.p.pid], "forking moved the original thread's lock"
    print(f"   PASS {fork} answered {str(st['output'])[:40]!r} with the original's context")

    print("== W  codex_compact works on the fork")
    c = b.call("codex_compact", {"thread": fork})
    assert c["state"] == "running", c
    st = b.wait(fork, limit=240)
    assert st["state"] == "completed", st
    print("   PASS compacted the fork")
finally:
    holder.close()
    b.p.terminate()

print("\nALL FORK TESTS PASSED")
```

- [ ] **Step 2: Check that it compiles**

Run: `python3 -m py_compile tests/fork_live.py`
Expected: exit 0. Do not run the live test inside an executor turn, because it starts its own codex processes and model turns. The controller runs it at integration, and a failure there comes back as a finding against this task.

- [ ] **Step 3: Commit**

```bash
git add tests/fork_live.py
git commit --only -m "Live test: a held thread is refused, read without taking it, forked and compacted" -- tests/fork_live.py
```

---

### Task 6: Version 0.15.0, README and release notes

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 3, Task 4, Task 5

**Files:**
- Modify: `server.py`: `SERVER_INFO` (line 97)
- Modify: `manifest.json`: `"version"`
- Modify: `README.md`
  - tools table (lines 174-182)
  - Threads section (lines 212-216)
  - tests list (lines 385-393)
- Create: `docs/release-notes/v0.15.0.md`

**Interfaces:**
- Consumes: the behaviour from Tasks 1-5.
- Produces: version 0.15.0 in both places that `tests/smoke.py` and `tests/pack_check.py` compare; user-facing docs.

- [ ] **Step 1: Bump the version**

In `server.py`, replace `SERVER_INFO = {"name": "codex", "version": "0.14.0"}` with `SERVER_INFO = {"name": "codex", "version": "0.15.0"}`.

In `manifest.json`, replace `"version": "0.14.0",` with `"version": "0.15.0",`.

- [ ] **Step 2: Update the README**

In `README.md`, replace:

```
| `codex_submit` | Start a thread, add a turn to one, or steer a running turn. Returns immediately |
| `codex_poll` | State, progress snapshot, pending approvals, and the complete output once done |
```

with:

```
| `codex_submit` | Start a thread, add a turn to one, or steer a running turn. Returns immediately |
| `codex_fork` | Copy a thread's history to a new id: branch on purpose, or continue a thread another process has open |
| `codex_poll` | State, progress snapshot, pending approvals, and the complete output once done; a thread the bridge is not running is read without taking it |
```

In `README.md`, replace:

```
A thread is the unit of work and its id is the handle. codex stores threads, not
this bridge, so a thread can be picked up tomorrow or next week: pass the id to
`codex_poll` or `codex_submit` and the bridge re-attaches it (`resumed: true`),
recovering the thread's real model, effort and working directory from codex.
```

with:

```
A thread is the unit of work and its id is the handle. codex stores threads, not
this bridge, so a thread can be picked up tomorrow or next week: pass the id to
`codex_submit` and the bridge re-attaches it (`resumed: true`), recovering the
thread's real model, effort and working directory from codex. `codex_poll` on a
thread the bridge is not running reads codex's saved record instead
(`read_only: true`), so looking at a thread never takes it from another process.

codex lets one process write a thread at a time, and a process keeps each thread
it has loaded until it unloads it or exits. When another process has the thread
open — another Claude Desktop connection, the Codex app, `codex` in a terminal, a
script — `codex_submit` and `codex_compact` refuse with `reason: "held_elsewhere"`.
`codex_fork` copies the thread's saved history to a new id that can be continued,
and compacted, at once; from then on the two threads are independent. Forking is
just as useful on purpose, to branch a line of work while the original stays as it
was.
```

In `README.md`, replace:

```
python3 tests/lifecycle_live.py         # steer, interrupt, crash recovery, cold resume
```

with:

```
python3 tests/lifecycle_live.py         # steer, interrupt, crash recovery, cold resume
python3 tests/fork_live.py              # a thread another process holds: refused, read, forked
```

- [ ] **Step 3: Write the release notes**

Create `docs/release-notes/v0.15.0.md`:

```markdown
Fork a thread, and read one without taking it.

codex lets one process write a thread at a time, and a process keeps each thread it
has loaded until it unloads it or exits. The bridge never unloads one, so every thread
it touches stays with Claude Desktop, and a thread another process has open (the
Codex app, `codex` in a terminal, a script) could not be continued here — only
reported as an `unknown thread`. This release makes both directions workable.

## codex_fork: continue a thread another process holds

`codex_fork(thread, cwd?)` copies a thread's saved history to a new thread id and
opens it idle in the bridge; `codex_submit` to the new id continues there. It works
while another process holds the original, because codex locks only the new id, and
it is just as useful on purpose, to branch a line of work while the original stays as
it was. codex copies what it has saved, so a turn still running elsewhere comes over
only up to its last saved step, and after the fork the two threads are independent.
A fork keeps the original's working directory unless `cwd` is passed, and that
directory is held to the same rule as a resumed thread's.

## codex_poll never takes a thread

A poll of a thread the bridge is not running — never seen, pruned from the cache, or
left from an app-server that has since restarted — now reads codex's saved record
(`thread/read`, and the last turn from `thread/turns/list`) instead of resuming it.
Resuming took codex's writer lock and kept it for as long as the bridge ran, so a
poll alone locked every other process out of the thread. The answer carries
`read_only: true`; a turn still in progress elsewhere reports `running`, with
`quiet_seconds` measured from the thread's last save. Threads the bridge is running
report live progress and approvals exactly as before.

## Refusals say why

When codex will not hand a thread over, the error now says what happened instead of
`unknown thread … codex could not resume it`:

- open in another process: `reason: "held_elsewhere"`, with what to do next —
  `codex_poll` still reads it, `codex_fork` continues it now;
- no saved thread with that id: `unknown thread`;
- not a thread id at all: `is not a thread id`;
- anything else codex reports comes back verbatim as `outcome: codex_error`, where
  it used to be `rejected`.

## Also in this release

- `codex_compact` re-attaches a thread left over from an earlier app-server before
  compacting it, as `codex_submit` already did.
- `codex_interrupt` refuses a thread the bridge is not running without calling codex:
  a turn running in another process has to be stopped there.
- `tests/fork_live.py` drives the whole flow against a thread a second codex process
  holds; `tests/smoke.py` and `tests/windows_sim.py` cover the new paths without codex.

Install: download `claude-codex-bridge-v0.15.0.mcpb` below. macOS: double-click it.
Windows: Claude Desktop → Settings → Extensions → Advanced settings → Install
Extension, from a local folder — Desktop refuses files on network paths, and Windows
has no `.mcpb` file association. Then restart Claude Desktop. Needs Python 3 on the
machine (`python3` on macOS, `python` on Windows) and codex installed before Desktop
starts. Prerequisites and the full documentation are in the
[README](https://github.com/Alexander-Stulov/claude-codex-bridge#readme).
```

- [ ] **Step 4: Run the checks**

Run:
```bash
python3 -m py_compile server.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
python3 tests/enable_1m_sim.py
```
Expected: all exit 0. The smoke test's version check passes, because the server and the manifest both report 0.15.0.

If `npx` is available, also run:
```bash
npx -y @anthropic-ai/mcpb@latest validate manifest.json
mkdir -p .codecraft/pack/build_dir .codecraft/pack/dist
[ -f .codecraft/.gitignore ] || printf '*\n' > .codecraft/.gitignore
cp server.py manifest.json .codecraft/pack/build_dir/
npx -y @anthropic-ai/mcpb@latest pack .codecraft/pack/build_dir .codecraft/pack/dist/claude-codex-bridge.mcpb
python3 tests/pack_check.py .codecraft/pack/dist/claude-codex-bridge.mcpb
```
Expected: `pack check: ... ok — v0.15.0, ... 8 tools, platforms ['darwin', 'win32']`. Otherwise the controller runs these at integration.

- [ ] **Step 5: Commit**

```bash
git add server.py manifest.json README.md docs/release-notes/v0.15.0.md
git commit --only -m "0.15.0: document codex_fork, read-only poll and held_elsewhere" -- server.py manifest.json README.md docs/release-notes/v0.15.0.md
```


---

### Task 7: Turns with no saved end — a fork's frozen copy, and this bridge's own crash

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 3, Task 4, Task 5, Task 6 (added after the final whole-branch review; spec amended with the owner's decision, 2026-09-16)

**Why:** integration showed codex reports every turn it is not running itself as `interrupted` with `completedAt: null`, and the fix in `dcd2e1b` read that shape as `running`. The final review found the same shape is permanent in two cases the bridge can recognise: a fork made while its source was mid-turn holds a frozen copy of that turn, and a turn this bridge was running when its own app-server child died. Both must stop reading as `running`.

**Files:**
- Modify: `server.py`
  - `read_thread_state`, the unfinished-turn rewrite
  - the head of `codex_poll`
- Modify: `tests/fork_live.py`, stage U
- Modify: `docs/release-notes/v0.15.0.md`, the `read_only: true` paragraph
- Test: `tests/smoke.py`, a new section appended at the end

**Interfaces:**
- Consumes: `read_thread_state`, `_live_entry`, `_new_thread_state` (Tasks 2 and 3).
- Produces:
  - `read_thread_state` reads an unfinished turn that a fork copied as `interrupted`.
  - `codex_poll` reads this bridge's own crashed turn as `failed`, with the cached error.

- [ ] **Step 1: Write the failing tests**

Append at the end of `tests/smoke.py`:

```python
# --- 0.15.0: turns with no saved end — a fork's frozen copy, and this bridge's own crash ---
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: None
_now = int(time.time())

def _unfinished_record(started_at, turn_id="u80", **thread):
    base = {"id": "t80", "cwd": "/tmp", "model": "gpt-5.6-terra", "reasoningEffort": "high",
            "status": {"type": "notLoaded"}, "createdAt": _now - 50, "updatedAt": _now - 20,
            "forkedFromId": None, "turns": []}
    base.update(thread)
    turn = {"id": turn_id, "status": "interrupted", "items": [], "itemsView": "full", "durationMs": None,
            "startedAt": started_at, "completedAt": None, "error": None}
    def fake(method, params, timeout=120):
        if method == "thread/read":
            return {"thread": base}
        if method == "thread/turns/list":
            return {"data": [turn], "nextCursor": None}
        raise AssertionError(f"a read-only poll sent {method}")
    return fake

try:
    # a fork's copy of a turn cut at the fork started before the fork existed: frozen, not running
    bridge.APP.request = _unfinished_record(_now - 100, forkedFromId="t79")
    _p = bridge.codex_poll({"thread": "t80"})
    assert _p["state"] == "interrupted" and _p["read_only"] is True and _p["forked_from"] == "t79", _p
    # the same shape with no startedAt at all is a copy too
    bridge.APP.request = _unfinished_record(None, forkedFromId="t79")
    assert bridge.codex_poll({"thread": "t80"})["state"] == "interrupted"
    # a fork's own turn starts at or after the fork, and may be live elsewhere
    for _started in (_now - 50, _now - 30):
        bridge.APP.request = _unfinished_record(_started, forkedFromId="t79")
        _p = bridge.codex_poll({"thread": "t80"})
        assert _p["state"] == "running" and _p["forked_from"] == "t79", (_started, _p)
    # not a fork: an unfinished turn may be live elsewhere
    bridge.APP.request = _unfinished_record(_now - 100)
    assert bridge.codex_poll({"thread": "t80"})["state"] == "running"

    # this bridge's own turn, cut when its app-server child died: failed, not running
    _died = {"message": "app-server exited while the turn was active"}
    _old = bridge._new_thread_state("t80", "/tmp", "write", "terra-high")
    _old.update(state="failed", error=_died, turn_id="u80", gen=bridge.APP.gen - 1)
    bridge.APP.threads["t80"] = _old
    bridge.APP.request = _unfinished_record(_now - 100)
    _p = bridge.codex_poll({"thread": "t80"})
    assert _p["state"] == "failed" and _p["error"] == _died and _p["read_only"] is True, _p
    # ... but only for that very turn: a later turn run elsewhere reads as itself
    bridge.APP.request = _unfinished_record(_now - 10, turn_id="u81")
    assert bridge.codex_poll({"thread": "t80"})["state"] == "running"
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request
    bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: a fork's frozen turn reads interrupted; this bridge's own crashed turn reads failed")
```

In `tests/fork_live.py`, replace:

```python
    assert running, f"a turn running in another process never showed as running: {snap}"
    assert holder.saw("turn/completed", t, 180), "the holder's turn did not finish"
    print("   PASS the other process's turn was visible as running")
```

with:

```python
    assert running, f"a turn running in another process never showed as running: {snap}"
    print("   PASS the other process's turn was visible as running")

    print("== U2 a fork made mid-turn holds a frozen copy of that turn: it reads interrupted, not running")
    cut = b.call("codex_fork", {"thread": t})
    c = Bridge()                      # a bridge that never saw the fork reads it from codex's record
    try:
        seen = c.call("codex_poll", {"thread": cut["thread"]})
    finally:
        c.p.terminate()
    assert seen.get("read_only") is True and seen.get("forked_from") == t, seen
    assert seen["state"] == "interrupted", f"a fork cut mid-turn must not read as running: {seen}"
    assert holder.saw("turn/completed", t, 180), "the holder's turn did not finish"
    print(f"   PASS the fork {cut['thread']} reads interrupted while the holder's turn ran on")
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `python3 tests/smoke.py`
Expected: FAIL with `AssertionError` on the first fork case, whose state is `running`: the `dcd2e1b` rewrite promotes every unfinished turn.

- [ ] **Step 3: Recognise a fork's frozen copy in `read_thread_state`**

In `server.py`, replace:

```python
    last = turns[0]
    # thread/turns/list normalizes an active turn held by another app-server child to
    # interrupted. A real interruption has completedAt; without it, the turn is live.
    if last.get("status") == "interrupted" and last.get("completedAt") is None:
        last = {**last, "status": "inProgress"}
```

with:

```python
    last = turns[0]
    if last.get("status") == "interrupted" and last.get("completedAt") is None:
        # No end saved for this turn. codex reports every turn it is not running itself
        # as interrupted, so it may be live in another process -- unless it is a copy
        # frozen at a fork: a turn that started before this thread existed was cut there
        # and never finishes on this thread. A turn whose process died has the same shape
        # and reads as running with growing quiet time; codex_poll recognises only this
        # bridge's own.
        started = last.get("startedAt")
        copied = bool(thread.get("forkedFromId")) and (started is None or started < (thread.get("createdAt") or 0))
        if not copied:
            last = {**last, "status": "inProgress"}
```

- [ ] **Step 4: Recognise this bridge's own crashed turn in `codex_poll`**

In `server.py`, replace:

```python
    tid = args.get("thread")
    st = _live_entry(tid)
    read_only = st is None
    if read_only:
        # Never seen, pruned, or held over from an earlier app-server child: read what
        # codex has saved instead of attaching, so polling never takes the thread from
        # whichever process has it open.
        st = read_thread_state(tid)
```

with:

```python
    tid = args.get("thread")
    st = _live_entry(tid)
    read_only = st is None
    if read_only:
        # Never seen, pruned, or held over from an earlier app-server child: read what
        # codex has saved instead of attaching, so polling never takes the thread from
        # whichever process has it open.
        with APP.lock:
            entry = APP.threads.get(tid)
        st = read_thread_state(tid)
        if (entry is not None and entry.get("state") == "failed" and st["state"] == "running"
                and entry.get("turn_id") and entry.get("turn_id") == st.get("turn_id")):
            # This bridge was running that very turn when its app-server child died. codex
            # saved no end for it, so the read says running, but it will never finish.
            st.update(state="failed", error=entry.get("error"),
                      terminal_at=entry.get("terminal_at") or time.time())
```

The variable is named `entry` on purpose: `tests/protocol_conformance.py` treats `entry` as a bridge-owned container, so reading `turn_id` and `terminal_at` from it is not mistaken for a wire read.

- [ ] **Step 5: Update the release notes**

In `docs/release-notes/v0.15.0.md`, replace:

```
`read_only: true`; a turn still in progress elsewhere reports `running`, with
`quiet_seconds` measured from the thread's last save. Threads the bridge is running
report live progress and approvals exactly as before.
```

with:

```
`read_only: true`; a turn still in progress elsewhere reports `running`, with
`quiet_seconds` measured from the thread's last save. A fork made while its source
was mid-turn holds a frozen copy of that turn, which reads as `interrupted`, and a
turn the bridge was running when its own app-server died reads as `failed`. Threads
the bridge is running report live progress and approvals exactly as before.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
python3 -m py_compile server.py tests/fork_live.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
```
Expected:
- all exit 0
- `smoke.py` prints `smoke: a fork's frozen turn reads interrupted; this bridge's own crashed turn reads failed`

Do not run `tests/fork_live.py`: the controller runs it at integration.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/smoke.py tests/fork_live.py docs/release-notes/v0.15.0.md
git commit --only -m "Read a fork's frozen turn as interrupted and this bridge's own crashed turn as failed" -- server.py tests/smoke.py tests/fork_live.py docs/release-notes/v0.15.0.md
```


---

### Task 8: A fork keeps its source's model; a compaction counts once

**Phase:** build
**Slice:** thread-access
**Depends on:** Task 4, Task 7 (added after testing the installed extension, approved by the owner 2026-09-16)

**Why:** two problems surfaced when the installed 0.15.0 extension was tested:
- **Fork model.** A fork took codex's default model (`gpt-6-astra`, `xhigh`) instead of its source's (`gpt-5.6-luna`, `medium`), because `thread/fork` was sent without a model. Verified live: passing `model`, `modelProvider` and `config.model_reasoning_effort` makes the fork keep the source's values.
- **Compaction count.** `activity.compactions` read 2 after one compaction. codex sends the single `contextCompaction` item twice, as `item/started` and then `item/completed`, and `_push_item` counted both. This bug predates the branch.

**Files:**
- Modify: `server.py`
  - `codex_fork`: read the source and pass its model fields
  - `AppServer._push_item`: the `contextCompaction` branch
- Modify: `tests/context_live.py`, stage V
- Modify: `docs/release-notes/v0.15.0.md`
- Test: `tests/smoke.py`
  - the codex_fork section's fake and first assertions
  - a new section appended at the end

**Interfaces:**
- Consumes: `thread_access_error` (Task 2), `codex_fork` (Task 4).
- Produces:
  - `codex_fork` sends `thread/read` before `thread/fork`, and passes `model`, `modelProvider` and `config.model_reasoning_effort` from the source.
  - `activity.compactions` counts one per completed compaction.

- [ ] **Step 1: Write the failing tests**

In `tests/smoke.py`, replace:

```python
def _forking(result_cwd):
    def fake(method, params, timeout=120):
        _sent.append((method, params))
        return {"config/read": _GATE,
                "thread/fork": {"thread": {"id": "t61", "cwd": result_cwd, "turns": []},
                                "cwd": result_cwd, "model": "gpt-5.6-sol", "reasoningEffort": "high"},
                "turn/start": {"turn": {"id": "u61"}}}[method]
    return fake
```

with:

```python
_SOURCE = {"id": "t60", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "modelProvider": "openai"}

def _forking(result_cwd):
    def fake(method, params, timeout=120):
        _sent.append((method, params))
        if method == "thread/fork":
            # codex gives the fork the model it is asked for, and its own default otherwise
            return {"thread": {"id": "t61", "cwd": result_cwd, "turns": []}, "cwd": result_cwd,
                    "model": params.get("model", "gpt-6-astra"),
                    "reasoningEffort": (params.get("config") or {}).get("model_reasoning_effort", "xhigh")}
        return {"config/read": _GATE, "thread/read": {"thread": _SOURCE},
                "turn/start": {"turn": {"id": "u61"}}}[method]
    return fake
```

In `tests/smoke.py`, replace:

```python
    _f = bridge.codex_fork({"thread": "t60"})
    assert _f == {"thread": "t61", "forked_from": "t60", "state": "idle", "cwd": _proj,
                  "workspace": "project", "model": "sol-high"}, _f
    _fp = next(p for m, p in _sent if m == "thread/fork")
    assert _fp == {"threadId": "t60", "approvalPolicy": "on-request", "approvalsReviewer": "user"} or \
        (sys.platform == "win32" and _fp.get("config") == {"windows.sandbox": "unelevated"}), _fp
```

with:

```python
    _f = bridge.codex_fork({"thread": "t60"})
    # the fork keeps the model, provider and effort its source ran on, not codex's default
    assert _f == {"thread": "t61", "forked_from": "t60", "state": "idle", "cwd": _proj,
                  "workspace": "project", "model": "luna-medium"}, _f
    assert ("thread/read", {"threadId": "t60", "includeTurns": False}) in _sent, _sent
    _fp = next(p for m, p in _sent if m == "thread/fork")
    _config = {"model_reasoning_effort": "medium"}
    if sys.platform == "win32":
        _config["windows.sandbox"] = "unelevated"
    assert _fp == {"threadId": "t60", "approvalPolicy": "on-request", "approvalsReviewer": "user",
                   "model": "gpt-5.6-luna", "modelProvider": "openai", "config": _config}, _fp
```

Append at the end of `tests/smoke.py`:

```python
# --- a compaction is counted once, when it completes ---------------------------------
# codex sends the one contextCompaction item twice, item/started then item/completed
# (observed live 2026-09-16); counting both made one codex_compact read as compactions 2.
_st = bridge._new_thread_state("t90", "/tmp", "write", "luna-medium")
bridge.APP._push_item(_st, {"type": "contextCompaction", "id": "c1"}, False)
assert _st["activity"]["now"] == "compacting context" and "compactions" not in _st["activity"], _st["activity"]
bridge.APP._push_item(_st, {"type": "contextCompaction", "id": "c1"}, True)
assert _st["activity"]["compactions"] == 1, _st["activity"]
print("smoke: a compaction is counted once, when it completes")
```

In `tests/context_live.py`, replace:

```python
assert st2["state"] in ("completed", "interrupted"), st2
```

with:

```python
assert st2["state"] in ("completed", "interrupted"), st2
assert act2.get("compactions") == 1, f"one codex_compact must count one compaction: {act2}"
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `python3 tests/smoke.py`
Expected: FAIL with `AssertionError` on the fork answer, whose `model` is `astra-xhigh` instead of `luna-medium`. After that assertion is fixed, the compaction section fails on `"compactions" not in _st["activity"]`.

- [ ] **Step 3: Carry the source's model into the fork**

In `server.py` `codex_fork`, replace:

```python
    APP.ensure()
    params = {"threadId": tid, "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    explicit = None
```

with:

```python
    APP.ensure()
    # thread/fork without a model gives the fork codex's configured default, not the model
    # the original ran on. Read the source (no lock taken) and carry its model over.
    try:
        source = APP.request("thread/read", {"threadId": tid, "includeTurns": False}, timeout=60).get("thread") or {}
    except CodexError as e:
        raise thread_access_error(tid, e) from None
    params = {"threadId": tid, "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    config = {}
    if source.get("model"):
        params["model"] = source["model"]
    if source.get("modelProvider"):
        params["modelProvider"] = source["modelProvider"]
    if source.get("reasoningEffort"):
        config["model_reasoning_effort"] = source["reasoningEffort"]
    explicit = None
```

In `server.py` `codex_fork`, replace:

```python
    pinned = windows_gate()
    if pinned:
        # a fork is a new thread: bind the verified sandbox mode to it, as thread/start does
        params["config"] = {"windows.sandbox": pinned}
```

with:

```python
    pinned = windows_gate()
    if pinned:
        # a fork is a new thread: bind the verified sandbox mode to it, as thread/start does
        config["windows.sandbox"] = pinned
    if config:
        params["config"] = config
```

- [ ] **Step 4: Count a compaction once**

In `server.py` `AppServer._push_item`, replace:

```python
            # earlier turns still exist, but as a summary rather than verbatim.
            self._note(st, now="compacting context", compactions=1)
```

with:

```python
            # earlier turns still exist, but as a summary rather than verbatim. codex sends
            # the one compaction item twice, started then completed: count it once.
            if completed:
                self._note(st, compactions=1)
            else:
                self._note(st, now="compacting context")
```

- [ ] **Step 5: Update the release notes**

In `docs/release-notes/v0.15.0.md`, replace:

```
A fork keeps the original's working directory unless `cwd` is passed, and that
directory is held to the same rule as a resumed thread's.
```

with:

```
A fork keeps the original's working directory unless `cwd` is passed, and that
directory is held to the same rule as a resumed thread's. It also keeps the model,
provider and reasoning effort the original ran on, where codex would otherwise give
it its default model.
```

In `docs/release-notes/v0.15.0.md`, replace:

```
## Also in this release

```

with:

```
## Also in this release

- `activity.compactions` counted every compaction twice, because codex sends the
  compaction item once as started and again as completed. It now counts one per
  compaction, when it completes.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
python3 -m py_compile server.py tests/context_live.py
python3 tests/smoke.py
python3 tests/protocol_conformance.py
python3 tests/windows_sim.py
```
Expected:
- all exit 0
- `smoke.py` prints `smoke: a compaction is counted once, when it completes`
- `windows_sim.py` still prints `windows_sim: thread/fork pin ok`

Do not run `tests/context_live.py`: the controller runs it.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/smoke.py tests/context_live.py docs/release-notes/v0.15.0.md
git commit --only -m "Keep the source's model on a fork; count a compaction once" -- server.py tests/smoke.py tests/context_live.py docs/release-notes/v0.15.0.md
```
