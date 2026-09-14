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
assert tools == {"codex_check", "codex_submit", "codex_poll", "codex_approve", "codex_interrupt",
                 "codex_compact"}, tools
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
