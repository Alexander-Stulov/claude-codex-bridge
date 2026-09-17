#!/usr/bin/env python3
"""Protocol smoke test — no codex binary required (codex_check may report absent)."""
import json, os, re, subprocess, sys, time

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "prompts/list"},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "codex_check", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
    # unknown model must be rejected before any app-server spawn
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "codex_submit", "arguments": {"prompt": "hi", "model": "gpt-9"}}},
]
server = os.path.join(os.path.dirname(__file__), "..", "server.py")
p = subprocess.run([sys.executable, server], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                   capture_output=True, text=True, timeout=120)
out = {r["id"]: r for r in map(json.loads, p.stdout.strip().split("\n"))}

instructions = out[1]["result"]["instructions"]
for marker in ("SESSIONS", "POLL, DON'T BLOCK", "APPROVALS", "JSON ANY TIME",
               "LONG THREADS:", "SIZE THE DELIVERABLE:"):
    assert marker in instructions, f"instructions missing {marker!r}"
# Measured on one 120-item survey: unbounded 16.7KB, one-row-per-item 11.2KB (linear,
# so ~500KB at 5000 items), cap-with-aggregate-fallback 0.85KB and constant. All three
# clauses have to survive, or scouts quietly go back to emitting monoliths.
# to the next section, not a fixed slice — a char budget silently drops clauses
_sizing = instructions.split("SIZE THE DELIVERABLE:")[1].split("MODELS by task weight")[0]
for phrase in ("output_schema", "cap", "index", "exceptions", "never a truncated enumeration"):
    assert phrase in _sizing, f"deliverable-sizing guidance lost {phrase!r}"
# Don't pin the number (that breaks on every bump); pin the invariant the release
# pipeline depends on: server and manifest report the same semver.
version = out[1]["result"]["serverInfo"]["version"]
assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
manifest = json.load(open(os.path.join(os.path.dirname(__file__), "..", "manifest.json"), encoding="utf-8"))
assert manifest["version"] == version, (manifest["version"], version)

tools = {t["name"] for t in out[2]["result"]["tools"]}
assert tools == {"codex_check", "codex_submit", "codex_fork", "codex_poll", "codex_approve", "codex_interrupt",
                 "codex_compact", "codex_capabilities"}, tools
# The manifest's tool list is what the install preview shows and what a reviewer reads:
# it must name exactly the tools the server registers, in the same order, or a new
# tool ships invisible to anyone deciding whether to install.
manifest_tools = [t["name"] for t in manifest["tools"]]
served_tools = [t["name"] for t in out[2]["result"]["tools"]]
assert manifest_tools == served_tools, f"manifest.json tools {manifest_tools} != served {served_tools}"

# required/optional contract: prompt+model required, project optional
sub = next(t for t in out[2]["result"]["tools"] if t["name"] == "codex_submit")
assert sub["inputSchema"]["required"] == ["prompt", "model"], sub["inputSchema"]["required"]
props = sub["inputSchema"]["properties"]
for k in ("thread", "cwd", "mode", "output_schema"):
    assert k in props, f"codex_submit missing {k}"
assert set(props["mode"]["enum"]) == {"read", "write"}, props["mode"]

# codex_fork: the thread to copy is required, where the copy works is optional, and it
# sits next to codex_submit, which is what a caller reaches for after it
fork_tool = next(t for t in out[2]["result"]["tools"] if t["name"] == "codex_fork")
assert fork_tool["inputSchema"]["required"] == ["thread"], fork_tool["inputSchema"]
assert set(fork_tool["inputSchema"]["properties"]) == {"thread", "cwd"}, fork_tool["inputSchema"]["properties"]
_served = [t["name"] for t in out[2]["result"]["tools"]]
assert _served.index("codex_fork") == _served.index("codex_submit") + 1, _served

assert out[3]["result"]["prompts"][0]["name"] == "run-codex-job"
# notifications/initialized is MCP's lifecycle handshake, sent on every connect. It
# has no answer and is not an error, so it must not be logged as one.
assert "notification failed" not in p.stderr, p.stderr

check = json.loads(out[4]["result"]["content"][0]["text"])
assert "codex_on_path" in check and "models" in check and "threads" in check, check
assert "project" not in props, "project should be gone — cwd is the one location knob"
assert check["cwd_rule"].startswith("any existing directory"), check
assert "roots" not in check and "config" not in check, "zero-config: no roots, no config file"
# Host-independent: ready mirrors codex presence (CI has no codex binary — that is fine).
# Windows is the exception: with codex present, ready also needs codex's sandbox, and
# asking about it spawns the app-server — so the two assertions below hold everywhere
# except a Windows box that actually has codex.
assert isinstance(check["ready"], bool), check
_win_with_codex = sys.platform == "win32" and check["codex_on_path"]
if _win_with_codex:
    assert "windows_sandbox" in check, check
    assert check["ready"] == (check["windows_sandbox"] == "ready"), check
else:
    assert check["ready"] == check["codex_on_path"], check
    assert "windows_sandbox" not in check, "windows_sandbox is a Windows-only field"
assert "app_server_running" not in check, "bare running-boolean invites a false failure read"
if not _win_with_codex:
    assert check["app_server"].startswith("idle"), check["app_server"]  # lazy spawn: idle before any submit
assert "sol-ultra" in check["models"] and "luna-medium" in check["models"], check["models"]
# astra is the flagship seat and the only family carrying codex's whole thinking
# ladder — the point of the family, so pin every rung rather than a sample.
assert {f"astra-{e}" for e in ("medium", "high", "xhigh", "max", "ultra")} \
    <= set(check["models"]), check["models"]
assert "astra-low" not in check["models"], "astra starts at medium — luna is the scout"

assert "error" in out[5], "unknown tool must return an error"

bad_model = json.loads(out[6]["result"]["content"][0]["text"])
assert out[6].get("result", {}).get("isError") or "rejected" in bad_model.get("outcome", ""), bad_model

print("smoke: all assertions passed (codex_on_path =", check["codex_on_path"], ")")

# --- approval id coercion (regression: CI-safe, no codex needed) ---------------
# MCP arguments arrive as JSON: an id the app-server issued as integer 0 comes back
# from a real caller as "0". The earlier e2e could never catch this because it fed
# back the int the bridge itself emitted.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server as bridge  # noqa: E402

bridge.APP.requests.clear()
bridge.APP.requests[0] = {"kind": "command", "params": {}, "thread": "t"}
key, req = bridge._peek_request("0")          # string in, int stored
assert key == 0 and isinstance(key, int), (key, type(key))   # canonical key back
assert req["kind"] == "command" and bridge.APP.requests, "peek must NOT remove — a bad grant has to stay retryable"
bridge.APP.requests.clear()

bridge.APP.requests[7] = {"kind": "command", "params": {}, "thread": "t"}
key, _ = bridge._peek_request(7)              # int in, int stored
assert key == 7, key
bridge.APP.requests.clear()

bridge.APP.requests["abc"] = {"kind": "command", "params": {}, "thread": "t"}
key, _ = bridge._peek_request("abc")          # non-numeric string id
assert key == "abc", key
bridge.APP.requests.clear()

bridge.APP.requests[3] = {"kind": "command", "params": {}, "thread": "t"}
try:
    bridge._peek_request("nope")
    raise AssertionError("missing id must raise")
except ValueError as e:
    assert "pending: ['3']" in str(e), str(e)  # error names what IS pending
bridge.APP.requests.clear()
print("smoke: approval id coercion ok (str<->int, canonical key returned)")

# --- allow_class + bounded event ring (0.5.2) ---------------------------------
sent = {}
bridge.APP.respond = lambda rid, result: sent.update(rid=rid, result=result)

# class grant uses the amendment the request proposed
bridge.APP.requests[5] = {"kind": "command", "thread": None,
                          "params": {"proposedExecpolicyAmendment": ["git", "add"]}}
out = bridge.codex_approve({"request_id": "5", "decision": "allow_class"})
assert sent["result"] == {"decision": {"acceptWithExecpolicyAmendment":
                                       {"execpolicy_amendment": ["git", "add"]}}}, sent
assert "note" not in out, out

# no amendment offered -> degrade to session grant, and SAY so
bridge.APP.requests[6] = {"kind": "command", "thread": None, "params": {}}
out = bridge.codex_approve({"request_id": 6, "decision": "allow_class"})
assert sent["result"] == {"decision": "acceptForSession"}, sent
assert "no execpolicy amendment" in out.get("note", ""), out

# the older decisions still map as before
bridge.APP.requests[7] = {"kind": "command", "thread": None, "params": {}}
bridge.codex_approve({"request_id": 7, "decision": "allow"})
assert sent["result"] == {"decision": "accept"}, sent
bridge.APP.requests.clear()

# --- 0.6.0: snapshot polling, no accumulation --------------------------------
st = bridge._new_thread_state("t2", "/tmp", "write", "luna-medium")
bridge.APP.threads["t2"] = st
bridge.APP.requests.clear()

# activity is a snapshot: "now" is replaced, counts accumulate, nothing is buffered
bridge.APP._note(st, now="running: pytest", commands=1)
bridge.APP._note(st, now="running: ruff", commands=1)
assert st["activity"] == {"now": "running: ruff", "commands": 2}, st["activity"]
assert "events" not in st, "no event buffer should exist"

# two approvals on one thread: both visible, derived from the one canonical table
for rid, cmd in ((10, "git add a"), (11, "git commit")):
    bridge.APP._on_server_request({"method": "item/commandExecution/requestApproval", "id": rid,
                                   "params": {"threadId": "t2", "command": cmd}})
assert [p["request_id"] for p in bridge.pending_for("t2")] == [10, 11], bridge.pending_for("t2")
assert st["state"] == "awaiting_approval"

# poll is IDEMPOTENT: twice in a row returns the same thing
a = bridge.codex_poll({"thread": "t2"})
b = bridge.codex_poll({"thread": "t2"})
assert a == b, (a, b)
assert [r["request_id"] for r in a["requests"]] == [10, 11], a["requests"]
assert a["activity"]["commands"] == 2, a["activity"]

# resolving one leaves the thread parked on the other
bridge.codex_approve({"request_id": "11", "decision": "allow"})
assert [p["request_id"] for p in bridge.pending_for("t2")] == [10], bridge.pending_for("t2")
assert st["state"] == "awaiting_approval", "must not report running while R1 waits"
bridge.codex_approve({"request_id": 10, "decision": "allow"})
assert bridge.pending_for("t2") == [] and st["state"] == "running", st["state"]

# a turn that completed mid-approval stays completed
bridge.APP.requests[12] = {"kind": "command", "thread": "t2", "params": {}, "view": {"request_id": 12}}
st["state"] = "completed"
bridge.codex_approve({"request_id": 12, "decision": "allow"})
assert st["state"] == "completed", "resolving an approval resurrected a finished turn"

# output is returned whole and repeatedly — never trimmed, never consumed
st.update(state="completed", output="X" * 500_000, structured=False)
p1 = bridge.codex_poll({"thread": "t2"})
p2 = bridge.codex_poll({"thread": "t2"})
assert len(p1["output"]) == 500_000 and p1["output"] == p2["output"], len(p1["output"])

# an approval for an untracked thread is declined, never swallowed
bridge.APP.requests.clear()
bridge.APP._on_server_request({"method": "item/commandExecution/requestApproval", "id": 99,
                               "params": {"threadId": "ghost", "command": "x"}})
assert sent["rid"] == 99 and sent["result"] == {"decision": "decline"}, sent

# terminal threads are pruned after their retry window; live ones never are
st["terminal_at"] = time.time() - bridge.TERMINAL_TTL_SECONDS - 1
bridge.prune_threads()
assert "t2" not in bridge.APP.threads, "finished thread should have been pruned"
bridge.APP.threads["live"] = bridge._new_thread_state("live", "/tmp", "write", "luna-medium")
bridge.prune_threads()
assert "live" in bridge.APP.threads, "a running thread must never be pruned"
bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: snapshot polling idempotent; output whole; approvals derived; pruning ok")

# --- 0.7.0: images as input, streamed progress counters ----------------------
import tempfile as _tf
_d = _tf.mkdtemp(); _p = os.path.join(_d, "shot.png"); open(_p, "wb").write(b"\x89PNG")
_items = bridge.build_input("look", [_p, "shot.png", "https://x/y.png",
                                     "data:image/png;base64,AA", "  "], _d)
assert _items[0] == {"type": "text", "text": "look"}, _items[0]
assert [i["type"] for i in _items[1:]] == ["localImage", "localImage", "image", "image"], _items
assert _items[1]["path"] == os.path.realpath(_p) == _items[2]["path"], _items
try:
    bridge.build_input("look", ["missing.png"], _d)
    raise AssertionError("a missing image path was accepted")
except ValueError as e:
    assert "image not found" in str(e), e

# a streamed delta counts characters, never stores them; per-turn, not cumulative
_st = bridge._new_thread_state("t9", "/tmp", "read", "luna-medium")
bridge.APP.threads["t9"] = _st
for _m, _k in (("item/agentMessage/delta", "answer_chars"),
               ("item/reasoning/textDelta", "thinking_chars")):
    bridge.APP._on_event({"method": _m, "params": {"threadId": "t9", "delta": "12345"}})
    bridge.APP._on_event({"method": _m, "params": {"threadId": "t9", "delta": "678"}})
    assert _st["activity"][_k] == 8, (_k, _st["activity"])
assert "12345" not in json.dumps(_st["activity"]), "delta text must not be stored"
bridge.APP.threads.clear()

_tools = {t["name"]: t for t in bridge.TOOLS}
assert "images" in _tools["codex_submit"]["inputSchema"]["properties"], "images not exposed"
assert "images" not in _tools["codex_submit"]["inputSchema"]["required"], "images must stay optional"
print("smoke: images map to local/url wire types; deltas count chars without storing them")

# --- 0.7.1: the thread id must survive every exit, including failures -------
_st2 = bridge._new_thread_state("t10", "/tmp", "write", "luna-medium")
_st2["state"] = "awaiting_approval"
bridge.APP.threads["t10"] = _st2
for _rid in (70, 71):
    bridge.APP.requests[_rid] = {"thread": "t10", "kind": "command", "params": {},
                                 "view": {"request_id": _rid, "kind": "command"}}
_sent = {}
bridge.APP.respond = lambda rid, result: _sent.update({"rid": rid, "result": result})
_r = bridge.codex_approve({"request_id": "70", "decision": "allow"})
assert _r["thread"] == "t10", _r
assert _r["state"] == "awaiting_approval", \
    f"approve reported {_r['state']!r} while request 71 is still parked — state must be read, not assumed"
_r = bridge.codex_approve({"request_id": "71", "decision": "allow"})
assert _r["state"] == "running", _r
bridge.APP.threads.clear(); bridge.APP.requests.clear()

# an error must still name the thread: codex holds it, so losing the id strands it
def _boom(_args):
    e = ValueError("turn/start exploded")
    e.codex_thread = "t11"
    raise e

bridge.HANDLERS["_smoke_boom"] = _boom
_resp = bridge.handle({"method": "tools/call",
                       "params": {"name": "_smoke_boom", "arguments": {}}})
_body = json.loads(_resp["content"][0]["text"])
assert _resp.get("isError") and _body.get("thread") == "t11", _body
# and an error on a caller-supplied thread echoes it back without any annotation
_resp = bridge.handle({"method": "tools/call",
                       "params": {"name": "codex_poll", "arguments": {"thread": ""}}})
_body = json.loads(_resp["content"][0]["text"])
assert _resp.get("isError"), _body
del bridge.HANDLERS["_smoke_boom"]
print("smoke: thread id survives success, approval, and both failure paths")

# --- model table round-trips ------------------------------------------------
# WIRE_TO_SLUG is a reverse dict comprehension, so two slugs sharing one
# (wire model, effort) pair collapse silently to whichever was defined last — and the
# loser becomes unrecoverable on the resume path, where this map is the ONLY way a
# thread gets its friendly slug back. Nothing else enforces injectivity, and every
# family added widens the surface.
assert len(bridge.WIRE_TO_SLUG) == len(bridge.MODELS), (
    "two slugs share a (wire model, effort) pair — one is unrecoverable on resume: "
    f"{sorted(set(bridge.MODELS) - set(bridge.WIRE_TO_SLUG.values()))}")
for _slug, _pair in bridge.MODELS.items():
    assert bridge.WIRE_TO_SLUG[_pair] == _slug, (_slug, _pair)
# Effort names are codex's, not ours: thread/start accepts any string and an invented
# one fails at turn time instead, so a typo here is invisible until a run burns.
_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
for _slug, (_wire, _effort) in bridge.MODELS.items():
    assert _effort in _EFFORTS, f"{_slug} declares effort {_effort!r}, not one codex reports"
    assert _slug.endswith(f"-{_effort}"), f"{_slug} does not name its own effort {_effort!r}"
print(f"smoke: {len(bridge.MODELS)} model slugs round-trip through WIRE_TO_SLUG")

# --- compaction runway ------------------------------------------------------
# Derived, not reported: the wire carries the effective window (raw x 95%) while
# codex compacts at raw x 90%. Both real windows must come out exact, and codex's
# own unit test (400000 raw -> 360000) has to hold too. If a codex upgrade changes
# either percentage these numbers move and this is the only thing that will say so.
assert bridge.compaction_runway(997_500, 0)["tokens_before_compaction"] == 945_000
assert bridge.compaction_runway(258_400, 0)["tokens_before_compaction"] == 244_800
assert bridge.compaction_runway(400_000 * 95 // 100, 0)["tokens_before_compaction"] == 360_000
# used is subtracted, and a thread past the threshold reports 0 rather than negative
assert bridge.compaction_runway(997_500, 45_000)["tokens_before_compaction"] == 900_000
assert bridge.compaction_runway(258_400, 999_999)["tokens_before_compaction"] == 0
assert bridge.compaction_runway(258_400, 10_000)["tokens_used"] == 10_000
# unknown inputs must yield no claim at all, never a guess
assert bridge.compaction_runway(None, 10) is None
assert bridge.compaction_runway(997_500, None) is None
print("smoke: compaction runway derives exactly for both real windows; unknowns stay unknown")

# --- workspace refusal is platform-aware; scratch root uses native separators ---------
# (regression for Windows: whole drives and env-named system dirs are refused, and the
# scratch prefix must match what codex echoes back, which is backslashed there.)
if sys.platform == "win32":
    assert bridge.refuse_reason(os.path.realpath("C:\\")), "C:\\ must be refused"
    assert bridge.refuse_reason(os.path.realpath(os.environ["SystemRoot"])), "%SystemRoot% must be refused"
assert bridge.refuse_reason(os.path.realpath(os.path.expanduser("~"))), "home must be refused"
assert bridge.refuse_reason(os.path.realpath(os.getcwd())) is None, "the repo itself must not be refused"
assert bridge.SCRATCH_ROOT == os.path.normpath(bridge.SCRATCH_ROOT), bridge.SCRATCH_ROOT
print("smoke: workspace refusal + scratch root ok")

# --- 0.13.3: MCP elicitations are approvals, not auto-declines ------------------
# An MCP server codex is using (a browser plugin, say) can park the thread on an
# elicitation: a permission prompt or a short form. Since 0.4.0 the bridge answered
# every one with decline whatever the caller decided, so codex_approve(allow) came
# back {"sent": {"action": "decline"}} and the plugin reported the action blocked.
_sent = {}
bridge.APP.respond = lambda rid, result: _sent.update({"rid": rid, "result": result})
bridge.APP.threads.clear(); bridge.APP.requests.clear()
_st = bridge._new_thread_state("t20", "/tmp", "write", "astra-high")
bridge.APP.threads["t20"] = _st


def _elicit(rid, **extra):
    params = {"threadId": "t20", "turnId": "turn-1", "serverName": "browser", "mode": "form",
              "message": "Creating a new tab requires permission",
              "requestedSchema": {"type": "object", "properties": {}}}
    params.update(extra)
    bridge.APP._on_server_request({"method": "mcpServer/elicitation/request", "id": rid,
                                   "params": params})


# the request surfaces with what is being asked, not just an id
_elicit(20, _meta={"codex_approval_kind": "mcp_tool_call", "tool_name": "open_tab",
                   "persist": ["session", "always"]})
_view = bridge.codex_poll({"thread": "t20"})["requests"][0]
assert _view["kind"] == "elicitation" and _view["server"] == "browser", _view
assert _view["message"].startswith("Creating a new tab") and _view["mode"] == "form", _view
assert _view["requested_schema"] == {"type": "object", "properties": {}}, _view
assert _view["tool"] == "open_tab" and _view["persist_modes"] == ["session", "always"], _view

# allow -> accept. codex treats a bare accept as content {}; it is sent explicitly so
# the reply is a complete MCP ElicitResult however the MCP server reads it.
_r = bridge.codex_approve({"request_id": "20", "decision": "allow"})
assert _sent["rid"] == 20 and _sent["result"] == {"action": "accept", "content": {}}, _sent
assert _r["sent"]["action"] == "accept" and _r["state"] == "running", _r

# deny -> decline, and no content
_elicit(21)
bridge.codex_approve({"request_id": 21, "decision": "deny"})
assert _sent["result"] == {"action": "decline"}, _sent

# allow_always -> accept, remembered for the session, when the request offered that
_elicit(22, _meta={"persist": ["session", "always"]})
_r = bridge.codex_approve({"request_id": 22, "decision": "allow_always"})
assert _sent["result"] == {"action": "accept", "content": {}, "_meta": {"persist": "session"}}, _sent
assert "note" not in _r, _r
# allow_class -> the durable variant codex calls "always"
_elicit(23, _meta={"persist": ["session", "always"]})
bridge.codex_approve({"request_id": 23, "decision": "allow_class"})
assert _sent["result"]["_meta"] == {"persist": "always"}, _sent
# a mode the request never offered is never sent: plain accept, and the caller is told
_elicit(24)
_r = bridge.codex_approve({"request_id": 24, "decision": "allow_always"})
assert _sent["result"] == {"action": "accept", "content": {}}, _sent
assert "persist" in _r.get("note", ""), _r
# always asked for, only session offered -> session, and say so
_elicit(25, _meta={"persist": "session"})
_r = bridge.codex_approve({"request_id": 25, "decision": "allow_class"})
assert _sent["result"]["_meta"] == {"persist": "session"} and "session" in _r.get("note", ""), _r

# a form: grant carries the answers, checked against the schema's required fields
# BEFORE anything is sent — the request must stay pending so a refused grant is retryable
_form = {"type": "object", "required": ["project"],
         "properties": {"project": {"type": "string", "title": "Project"},
                        "notify": {"type": "boolean", "default": False}}}
_elicit(26, message="Which project?", requestedSchema=_form)
_sent.clear()
try:
    bridge.codex_approve({"request_id": 26, "decision": "allow"})
    raise AssertionError("an accept without the required field must be refused")
except ValueError as e:
    assert "project" in str(e) and "grant" in str(e), e
assert not _sent, "nothing may be sent for a refused grant"
assert [p["request_id"] for p in bridge.pending_for("t20")] == [26], bridge.pending_for("t20")
try:
    bridge.codex_approve({"request_id": 26, "decision": "allow", "grant": ["not", "an", "object"]})
    raise AssertionError("a non-object grant must be refused")
except ValueError as e:
    assert "object" in str(e), e
_r = bridge.codex_approve({"request_id": 26, "decision": "allow",
                           "grant": '{"project": "bridge", "notify": true}'})   # JSON string form
assert _sent["result"] == {"action": "accept", "content": {"project": "bridge", "notify": True}}, _sent
assert bridge.pending_for("t20") == [] and _st["state"] == "running", _st["state"]
# deny needs no answers, however many fields the form requires
_elicit(27, requestedSchema=_form)
bridge.codex_approve({"request_id": 27, "decision": "deny"})
assert _sent["result"] == {"action": "decline"}, _sent

# a url-mode elicitation exposes the url it wants opened
bridge.APP._on_server_request({"method": "mcpServer/elicitation/request", "id": 28, "params": {
    "threadId": "t20", "serverName": "github", "mode": "url", "elicitationId": "e1",
    "message": "Sign in to continue", "url": "https://example.test/auth"}})
_view = bridge.codex_poll({"thread": "t20"})["requests"][0]
assert _view["mode"] == "url" and _view["url"] == "https://example.test/auth", _view
assert _view["requested_schema"] is None and _view["persist_modes"] == [], _view
bridge.codex_approve({"request_id": 28, "decision": "allow"})
assert _sent["result"] == {"action": "accept", "content": {}}, _sent

# an elicitation for a thread the bridge does not track is still declined — in the
# shape codex defines for THIS request type. {"decision": ...} is a command reply:
# codex logged it as malformed before falling back to declining anyway.
bridge.APP._on_server_request({"method": "mcpServer/elicitation/request", "id": 99, "params": {
    "threadId": "ghost", "serverName": "x", "mode": "form", "message": "?",
    "requestedSchema": {"type": "object", "properties": {}}}})
assert _sent["rid"] == 99 and _sent["result"] == {"action": "decline"}, _sent
# ... and a command for an untracked thread keeps its own shape
bridge.APP._on_server_request({"method": "item/commandExecution/requestApproval", "id": 98,
                               "params": {"threadId": "ghost", "command": "x"}})
assert _sent["result"] == {"decision": "decline"}, _sent

# the caller is told elicitations are approvable, at every decision point
_approve = next(t for t in bridge.TOOLS if t["name"] == "codex_approve")
assert "elicitation" in _approve["description"], _approve["description"]
_props = _approve["inputSchema"]["properties"]
assert "elicitation" in _props["grant"]["description"].lower(), _props["grant"]
assert "elicitation" in _props["decision"]["description"].lower(), _props["decision"]
_approvals = bridge.INSTRUCTIONS.split("APPROVALS:")[1].split("RESULTS:")[0]
for phrase in ("elicitation", "grant", "persist_modes"):
    assert phrase in _approvals, f"APPROVALS guidance lost {phrase!r}"
bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: elicitations accept/decline as decided; forms carry grant; untracked threads still decline")

# --- 0.14.0: codex_capabilities — what codex can do here, before a brief is written -----
# The inventory is shaped from four app-server answers. The payloads below mirror the real
# shapes observed on codex-cli 0.153.4 (plugin/installed, skills/list, mcpServerStatus/list,
# app/installed), trimmed to what the bridge reads.
_plugins = {"marketplaces": [
    {"name": "openai-curated-remote", "plugins": [
        {"name": "deep-research-work", "id": "deep-research-work@openai-curated-remote",
         "installed": True, "enabled": True,
         "interface": {"displayName": "Deep Research", "shortDescription": "Deep research",
                       "longDescription": "Investigate complex questions with cited synthesis."}},
        {"name": "vercel", "id": "vercel@openai-curated-remote", "installed": True, "enabled": False,
         "interface": {"displayName": "Vercel", "shortDescription": "Build and deploy web apps"}}]},
    {"name": "openai-bundled", "plugins": [
        {"name": "chrome", "id": "chrome@openai-bundled", "installed": True, "enabled": True,
         "interface": {"displayName": "Chrome", "shortDescription": "Control Chrome with ChatGPT"}}]}],
    "marketplaceLoadErrors": []}
_skills = {"data": [{"cwd": "/tmp", "skills": [
    {"name": "deep-research-work:deep-research", "enabled": True, "scope": "user",
     "pluginId": "deep-research-work@openai-curated-remote", "path": "/x/SKILL.md",
     "description": "Use only when the user asks for deep research. Produce a comprehensive, cited artifact."},
    {"name": "vercel:vercel-queues", "enabled": True, "scope": "user",
     "pluginId": "vercel@openai-curated-remote", "path": "/y/SKILL.md", "description": "Vercel Queues guidance"},
    {"name": "imagegen", "enabled": True, "scope": "system", "pluginId": None, "path": "/z/SKILL.md",
     "description": "Generate or edit raster images"},
    {"name": "review-agent", "enabled": False, "scope": "system", "pluginId": None, "path": "/w/SKILL.md",
     "description": "Perform a read-only review"}]}]}
_mcp = {"data": [
    {"name": "MCP_DOCKER", "runtimeStatus": None, "pluginId": None,
     "serverInfo": {"name": "Docker AI MCP Gateway", "version": "2.0.1"},
     "tools": {"mcp-find": {"name": "mcp-find", "description": "Find MCP servers in the current catalog"},
               "mcp-add": {"name": "mcp-add", "description": "Add a new MCP server to the session"}}},
    {"name": "cua_repl", "runtimeStatus": None, "pluginId": "chrome@openai-bundled", "serverInfo": None,
     "tools": {"js": {"name": "js", "description": "Run JavaScript against the browser"}}},
    {"name": "computer-use", "runtimeStatus": None, "pluginId": None, "serverInfo": None, "tools": {}}]}
_apps = {"apps": [{"id": "connector_690a", "runtimeName": "Vercel", "enabled": True, "callable": True},
                  {"id": "connector_openai_hotline", "runtimeName": "Hotline", "enabled": False, "callable": False}]}

inv = bridge.capabilities_inventory(_plugins, _skills, _mcp, _apps)
assert "both modes" in inv["network"], inv["network"]
assert "skills" in inv["how_to_use"], inv["how_to_use"]
_byid = {p["id"]: p for p in inv["plugins"]}
_dr = _byid["deep-research-work@openai-curated-remote"]
assert _dr["name"] == "Deep Research" and _dr["enabled"] is True, _dr
assert _dr["skills"] == ["deep-research-work:deep-research"], _dr
assert _byid["vercel@openai-curated-remote"]["enabled"] is False, "a disabled plugin is still listed, flagged"
assert _byid["vercel@openai-curated-remote"]["skills"] == ["vercel:vercel-queues"], _byid["vercel@openai-curated-remote"]
assert _byid["chrome@openai-bundled"]["skills"] == [], _byid["chrome@openai-bundled"]
assert inv["system_skills"] == ["imagegen"], "system skills listed; a disabled skill is not offered: %r" % inv["system_skills"]
_srv = {s["name"]: s for s in inv["mcp_servers"]}
assert _srv["MCP_DOCKER"]["tools"] == ["mcp-add", "mcp-find"], _srv["MCP_DOCKER"]
assert _srv["cua_repl"]["plugin"] == "chrome@openai-bundled", _srv["cua_repl"]
# a server that lists no tools cannot be used; the map says so instead of showing an empty list
assert _srv["computer-use"]["tools"] == [] and "no tools" in _srv["computer-use"]["status"], _srv["computer-use"]
assert _srv["computer-use"]["source"] == "config.toml" and _srv["cua_repl"]["source"] == "plugin", _srv
# a plugin with no skills still shows what it brings: the servers it provides
assert _byid["chrome@openai-bundled"]["servers"] == ["cua_repl"], _byid["chrome@openai-bundled"]
# the bundled surface plugins carry nothing of their own on the wire; the map says what serves them
_plugins["marketplaces"][1]["plugins"].append(
    {"name": "computer-use", "id": "computer-use@openai-bundled", "installed": True, "enabled": True,
     "interface": {"displayName": "Computer Use", "shortDescription": "Control Mac apps from ChatGPT"}})
_mcp2 = {"data": [dict(_mcp["data"][0]), dict(_mcp["data"][1], pluginId="unified-computer-use@openai-bundled"), _mcp["data"][2]]}
_inv3 = bridge.capabilities_inventory(_plugins, _skills, _mcp2, _apps)
_by3 = {p["id"]: p for p in _inv3["plugins"]}
assert _by3["computer-use@openai-bundled"]["via"] == "cua_repl", _by3["computer-use@openai-bundled"]
assert "via" not in _by3["deep-research-work@openai-curated-remote"], "a plugin with its own skills needs no via"
_mcp3 = {"data": [_mcp["data"][0]]}                       # cua_repl absent: say so rather than claim it
_by4 = {p["id"]: p for p in bridge.capabilities_inventory(_plugins, _skills, _mcp3, _apps)["plugins"]}
assert _by4["computer-use@openai-bundled"]["via"] == "cua_repl (not running)", _by4["computer-use@openai-bundled"]
_plugins["marketplaces"][1]["plugins"].pop()
assert _byid["deep-research-work@openai-curated-remote"]["servers"] == [], _byid["deep-research-work@openai-curated-remote"]
_apps_out = {a["name"]: a for a in inv["apps"]}
assert _apps_out["Vercel"]["mention"] == "[$Vercel](app://connector_690a)", _apps_out["Vercel"]
assert _apps_out["Vercel"]["enabled"] is True and _apps_out["Hotline"]["enabled"] is False, _apps_out
assert inv["counts"] == {"plugins": 3, "skills": 3, "mcp_servers": 3, "apps": 2}, inv["counts"]
assert "errors" not in inv, inv
# descriptions stay out of the default view — it is a map, not a manual
assert "description" not in json.dumps(inv["plugins"]) and "Generate or edit" not in json.dumps(inv), inv

# a query narrows to matches and brings the descriptions with them
q = bridge.capabilities_inventory(_plugins, _skills, _mcp, _apps, query="ReSearch")
assert "plugins" not in q and q["query"] == "ReSearch", q
_kinds = {(m["kind"], m["name"]) for m in q["matches"]}
assert ("skill", "deep-research-work:deep-research") in _kinds and ("plugin", "Deep Research") in _kinds, _kinds
assert not any(m["kind"] == "tool" for m in q["matches"]), q["matches"]
_skill_match = next(m for m in q["matches"] if m["kind"] == "skill")
assert _skill_match["name"] == "deep-research-work:deep-research" and "cited artifact" in _skill_match["description"], _skill_match
assert _skill_match["path"] == "/x/SKILL.md", "a match carries the path codex injects from"
q2 = bridge.capabilities_inventory(_plugins, _skills, _mcp, _apps, query="find")
_tool = next(m for m in q2["matches"] if m["kind"] == "tool")
assert _tool["name"] == "mcp-find" and _tool["server"] == "MCP_DOCKER" and "catalog" in _tool["description"], _tool
assert bridge.capabilities_inventory(_plugins, _skills, _mcp, _apps, query="zzz-nothing")["matches"] == []

# a failed source is reported, never fatal: the rest of the map still comes back
inv2 = bridge.capabilities_inventory(_plugins, _skills, _mcp, None, errors={"apps": "app/installed failed"})
assert inv2["apps"] == [] and inv2["errors"] == {"apps": "app/installed failed"}, inv2
assert inv2["counts"]["plugins"] == 3, inv2["counts"]

# the tool asks the app-server in the ORDER that matters: remote-marketplace plugins only
# show their skills in skills/list once plugin/installed has loaded them in that process
_calls = []
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: _calls.append("ensure")
def _fake_request(method, params, timeout=120):
    _calls.append(method)
    return {"plugin/installed": _plugins, "skills/list": _skills,
            "mcpServerStatus/list": _mcp, "app/installed": _apps}[method]
bridge.APP.request = _fake_request
try:
    out = bridge.codex_capabilities({})
    assert _calls[0] == "ensure" and _calls.index("plugin/installed") < _calls.index("skills/list"), _calls
    assert out["counts"]["skills"] == 3, out["counts"]
    _calls.clear()
    def _failing_request(method, params, timeout=120):
        _calls.append(method)
        if method == "app/installed":
            raise bridge.CodexError({"message": "unsupported"})
        return _fake_request(method, params)
    bridge.APP.request = _failing_request
    out = bridge.codex_capabilities({"query": "find"})
    assert "apps" in out["errors"] and out["matches"], out
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request

_cap = next(t for t in bridge.TOOLS if t["name"] == "codex_capabilities")
assert _cap["inputSchema"].get("required", []) == [], _cap["inputSchema"]
assert "query" in _cap["inputSchema"]["properties"], _cap["inputSchema"]
# the caller is told to look before briefing, what the important plugins are, and that the
# network is always there — at the decision points, not just in the README
for marker in ("CAPABILITIES:", "PLUGINS:", "NETWORK:"):
    assert marker in bridge.INSTRUCTIONS, f"instructions missing {marker!r}"
_plug = bridge.INSTRUCTIONS.split("PLUGINS:")[1].split("NETWORK:")[0]
for phrase in ("deep-research", "codex_capabilities", "Computer Use", "Chrome", "sol", "quota", "markdown", "skills"):
    assert phrase in _plug, f"PLUGINS guidance lost {phrase!r}"
assert "both modes" in bridge.INSTRUCTIONS.split("NETWORK:")[1].split("\n")[0]
print("smoke: capabilities inventory shaped, queried, resilient; app-server asked in the right order")

# --- a follow-up turn that moves the thread reports where it now works -------------
# A thread starts in scratch, a later turn passes a real cwd: the work lands there, so
# the result and provenance must say project, not the label from creation time.
_calls2 = []
_real_ensure, _real_request, _real_scratch = bridge.APP.ensure, bridge.APP.request, bridge.new_scratch
_tmp_scratch, _tmp_proj = _tf.mkdtemp(prefix="scratch-"), _tf.mkdtemp(prefix="proj-")
bridge.new_scratch = lambda: _tmp_scratch
bridge.APP.ensure = lambda: None
def _fake_submit_request(method, params, timeout=120):
    _calls2.append(method)
    return {"thread/start": {"thread": {"id": "t30"}},
            "turn/start": {"turn": {"id": f"u{len(_calls2)}"}},
            # on Windows codex_submit gates every start/resume on the sandbox mode, read
            # fresh from config/read; answer it, or this section only passes on POSIX
            "config/read": {"config": {"windows": {"sandbox": "unelevated"}}, "layers": []}}[method]
bridge.APP.request = _fake_submit_request
try:
    r1 = bridge.codex_submit({"prompt": "research it", "model": "sol-high"})
    assert r1["workspace"] == "scratch" and r1["cwd"] == _tmp_scratch, r1
    bridge.APP.threads["t30"]["state"] = "completed"          # the research turn finished
    r2 = bridge.codex_submit({"prompt": "save it", "model": "terra-medium", "thread": "t30", "cwd": _tmp_proj})
    assert r2["workspace"] == "project" and r2["cwd"] == os.path.realpath(_tmp_proj), r2
    assert bridge._provenance(bridge.APP.threads["t30"])["workspace"] == "project"
    bridge.APP.threads["t30"]["state"] = "completed"
    r3 = bridge.codex_submit({"prompt": "again", "model": "terra-medium", "thread": "t30"})
    assert r3["workspace"] == "project" and r3["cwd"] == os.path.realpath(_tmp_proj), "no cwd given: stays where it moved to"
finally:
    bridge.APP.ensure, bridge.APP.request, bridge.new_scratch = _real_ensure, _real_request, _real_scratch
    bridge.APP.threads.clear(); bridge.APP.requests.clear()
print("smoke: a moved thread reports its real workspace")

# --- explicit skills go on the wire as structured items, resolved from the catalog ----
# A `$skill` written in the prompt is NOT honoured through the app-server: two live
# threads answered NONE to "quote the first heading of the skill this mention loaded",
# while a {type: skill, name, path} input item made the model quote "# Visualize".
_catalog = [{"name": "deep-research-work:deep-research", "path": "/x/SKILL.md", "enabled": True},
            {"name": "product-design:index", "path": "/pd/SKILL.md", "enabled": True},
            {"name": "data-analytics:index", "path": "/da/SKILL.md", "enabled": True},
            {"name": "imagegen", "path": "/sys/imagegen/SKILL.md", "enabled": True},
            {"name": "review-agent", "path": "/sys/review/SKILL.md", "enabled": False}]
_items = bridge.resolve_skills(["deep-research", "$imagegen", "product-design:index"], _catalog)
assert _items == [{"type": "skill", "name": "deep-research", "path": "/x/SKILL.md"},
                  {"type": "skill", "name": "imagegen", "path": "/sys/imagegen/SKILL.md"},
                  {"type": "skill", "name": "index", "path": "/pd/SKILL.md"}], _items
for bad, needle in (("index", "product-design:index"),          # ambiguous: name the candidates
                    ("nope", "unknown skill"),                  # unknown: say so
                    ("review-agent", "disabled")):              # disabled: cannot be injected
    try:
        bridge.resolve_skills([bad], _catalog)
        raise AssertionError(f"{bad!r} must be refused")
    except ValueError as e:
        assert needle in str(e), (bad, str(e))
assert bridge.resolve_skills([], _catalog) == [] and bridge.resolve_skills(None, _catalog) == []
# and they lead the turn input, before the text and any images
_in = bridge.build_input("go", [], "/tmp", skills=[{"type": "skill", "name": "x", "path": "/x/SKILL.md"}])
assert _in[0]["type"] == "skill" and _in[1] == {"type": "text", "text": "go"}, _in
_sub = next(t for t in bridge.TOOLS if t["name"] == "codex_submit")
assert "skills" in _sub["inputSchema"]["properties"] and "skills" not in _sub["inputSchema"]["required"]
# the catalog is fetched in the order that matters and cached per app-server generation
_calls3 = []
_real_ensure, _real_request = bridge.APP.ensure, bridge.APP.request
bridge.APP.ensure = lambda: None
def _cat_request(method, params, timeout=120):
    _calls3.append(method)
    return {"plugin/installed": {"marketplaces": []},
            "skills/list": {"data": [{"cwd": "/tmp", "skills": _catalog}]}}[method]
bridge.APP.request = _cat_request
try:
    bridge.APP.skill_catalog_cache = None
    c1 = bridge.skill_catalog()
    c2 = bridge.skill_catalog()
    assert [s["name"] for s in c1] == [s["name"] for s in _catalog], c1
    assert _calls3 == ["plugin/installed", "skills/list"], _calls3   # once, plugins first
    assert c2 is c1, "second call must come from the cache"
    bridge.APP.gen += 1                                           # a restarted child forgets
    bridge.skill_catalog()
    assert _calls3 == ["plugin/installed", "skills/list"] * 2, _calls3
finally:
    bridge.APP.ensure, bridge.APP.request = _real_ensure, _real_request
    bridge.APP.skill_catalog_cache = None
print("smoke: explicit skills resolve from the catalog and ride the turn input as structured items")

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
