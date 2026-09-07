#!/usr/bin/env python3
"""Every app-server field the bridge READS must exist in the pinned protocol schema.

Sending a field the app-server doesn't know fails loudly. Reading one fails
silently forever: `params.get("availableDecisions")` returned None for weeks
because that field does not exist — the class-grant feature was invisible and
nothing complained. This closes that asymmetry, and turns a field rename in a
future codex release into a red build instead of a quiet null.

Fixture is committed, so this runs in CI without the codex CLI. After upgrading
codex: python3 tests/regen_schema_keys.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
fixture = json.load(open(os.path.join(HERE, "fixtures", "app-server-schema-keys.json"), encoding="utf-8"))
known = set(fixture["properties"])
source = open(os.path.join(HERE, "..", "server.py"), encoding="utf-8").read()

# Keys the bridge invents itself rather than reading off the wire.
OURS = set()

# EVERY `x.get("key")` counts as reading the wire unless x is named below as a
# bridge-owned container. This used to be an allowlist of payload variable names,
# and it silently missed four separate additions — each new accessor was named
# something the list did not anticipate (`thread`, `last`, `it`, `tu`), so the
# fields went unchecked exactly when a check was most wanted. Inverted, an
# unrecognised name produces a loud false alarm instead of a silent gap: add it
# here when it really is ours.
LOCAL = {
    "args", "st", "out", "info", "activity", "sub", "live", "states", "fixture",
    "payload", "cfg", "config", "d", "kw", "opts", "row", "entry", "self",
    "MODELS", "WIRE_TO_SLUG", "APPROVAL_KINDS", "os", "environ",
}
reads = set()
for m in re.finditer(r"""(?<![.\w])([A-Za-z_]\w*)\.get\(\s*["']([a-zA-Z][a-zA-Z]*)["']""", source):
    holder, key = m.group(1), m.group(2)
    if holder not in LOCAL:
        reads.add(key)
# Dict-literal access on the request envelope, e.g. req["params"].get("threadId").
# Anchored to the envelope specifically: a bare `].get(` also matches bridge-owned
# nesting like st["activity"].get("now").
for m in re.finditer(r"""req\[["']params["']\]\.get\(\s*["']([a-zA-Z][a-zA-Z]*)["']""", source):
    reads.add(m.group(1))

# Floor guards against the regex breaking wholesale, not against an exact count.
assert len(reads) > 20, f"the scan found only {len(reads)} reads — the regex has drifted from the code"

unknown = sorted(k for k in reads if k not in known and k not in OURS)
assert not unknown, (
    f"server.py reads app-server fields that do not exist in {fixture['codex_version']}: {unknown}\n"
    f"Either the name is wrong (it will silently read None), or codex renamed it — "
    f"check the schema and, after a codex upgrade, run tests/regen_schema_keys.py."
)

print(f"protocol conformance: {len(reads)} inbound fields, all present in {fixture['codex_version']}")

# --- methods, by direction --------------------------------------------------
# Fields fail silently when misspelled; methods fail silently when REMOVED upstream.
# `thread/error` and `turn/failed` sat in the dispatcher for months after codex deleted
# them, and a field-only scan cannot see that. Each direction is checked against its own
# registry: a client request is not a notification.
sent = set(re.findall(r'APP\.request\(\s*"([a-zA-Z][\w/]*)"', source)) \
     | set(re.findall(r'self\.request\(\s*"([a-zA-Z][\w/]*)"', source)) \
     | set(re.findall(r'\.notify\(\s*"([a-zA-Z][\w/]*)"', source))
# Scope to the codex notification dispatcher: the MCP request handler in the same file
# switches on MCP methods (tools/call, initialize), which belong to a different protocol.
_start = source.index("def _on_event")
_end = source.index("def _note", _start)
_dispatcher = source[_start:_end]
dispatched = set(re.findall(r'method\s*==\s*"([a-zA-Z][\w/]*)"', _dispatcher))
for _group in re.findall(r'method\s+in\s+\(([^)]*)\)', _dispatcher):
    dispatched |= set(re.findall(r'"([a-zA-Z][\w/]+)"', _group))
handled_requests = set(re.findall(r'^\s+"([a-zA-Z][\w/]+)":\s*"(?:permissions|command|file_change|elicitation)"',
                                  source, re.M))

# `initialized` is a client->server notification in the handshake, not a request.
HANDSHAKE = {"initialized"}
for label, used, registry in (
    ("client request", sent - HANDSHAKE, set(fixture["client_requests"])),
    ("notification dispatched", dispatched, set(fixture["server_notifications"])),
    ("server request handled", handled_requests, set(fixture["server_requests"])),
):
    ghosts = sorted(m for m in used if m not in registry)
    assert not ghosts, (
        f"server.py uses {label} methods that do not exist in {fixture['codex_version']}: {ghosts}\n"
        f"They were probably removed upstream — that code is dead and silently does nothing."
    )

# An OMITTED handler leaves no string to check, so existence alone cannot catch it.
# These carry state the bridge would otherwise get wrong; name them explicitly.
REQUIRED = {"turn/started", "turn/completed", "item/started", "item/completed", "error"}
missing = sorted(REQUIRED - dispatched)
assert not missing, f"no handler for required notification(s): {missing}"

print(f"protocol conformance: {len(sent - HANDSHAKE)} client requests, {len(dispatched)} notifications, "
      f"{len(handled_requests)} server requests — all real, required handlers present")
