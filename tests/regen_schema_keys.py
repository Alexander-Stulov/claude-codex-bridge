#!/usr/bin/env python3
"""Regenerate the app-server schema fixture from the pinned codex CLI.

Run after upgrading codex; a red protocol_conformance test is the signal that a
field the bridge reads was renamed or removed upstream.
"""
import json, glob, os, subprocess, sys, tempfile

out = tempfile.mkdtemp(prefix="codex-schema-")
subprocess.run(["codex", "app-server", "generate-json-schema", "--out", out], check=True)
known = set()

def walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                known.update(value.keys())
            walk(value)
    elif isinstance(node, list):
        for value in node:
            walk(value)

for f in glob.glob(os.path.join(out, "*.json")):
    walk(json.load(open(f)))


def methods(name):
    """Wire method names for one direction. A method the bridge dispatches on but that
    does not exist here is dead code — `thread/error` and `turn/failed` lived in the
    dispatcher for months after codex removed them."""
    path = os.path.join(out, f"{name}.json")
    if not os.path.exists(path):
        return []
    found = set()
    for variant in (json.load(open(path)).get("oneOf") or []):
        m = (variant.get("properties") or {}).get("method") or {}
        for v in (m.get("enum") or ([m["const"]] if "const" in m else [])):
            found.add(v)
    return sorted(found)


version = subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()
target = os.path.join(os.path.dirname(__file__), "fixtures", "app-server-schema-keys.json")
payload = {
    "_source": "codex app-server generate-json-schema --out <dir>, property and method names from the bundle",
    "_regenerate": "python3 tests/regen_schema_keys.py  (needs the codex CLI)",
    "codex_version": version,
    "properties": sorted(known),
    "client_requests": methods("ClientRequest"),
    "server_notifications": methods("ServerNotification"),
    "server_requests": methods("ServerRequest"),
}
json.dump(payload, open(target, "w"), indent=1)
print(f"wrote {target}: {version}, {len(known)} property names, "
      f"{len(payload['client_requests'])}/{len(payload['server_notifications'])}/"
      f"{len(payload['server_requests'])} client/notification/server-request methods")
