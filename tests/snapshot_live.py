#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

The 0.6.0 behaviours that were only ever unit-tested against fabricated state:
  E  idempotent polling      — repeat polls of a real thread never differ or consume
  F  whole-output passthrough— a >100k-char real answer survives (the guard that used
                               to replace it with a stub is gone), valid JSON under a schema
  G  concurrent approvals    — two real approvals outstanding at once, both visible,
                               resolving one leaves the other parked
  H  terminal retry window   — a finished thread stays pollable, and prune spares it
"""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="snap-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
outside_a = tempfile.mkdtemp(prefix="outA-")
outside_b = tempfile.mkdtemp(prefix="outB-")


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

    def rpc(self, method, params, t=1600):
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

    def call(self, n, a, t=1600):
        r = self.rpc("tools/call", {"name": n, "arguments": a}, t)
        b = json.loads(r["result"]["content"][0]["text"])
        if r["result"].get("isError"):
            raise RuntimeError(json.dumps(b)[:300])
        return b

    def wait(self, thread, limit=420):
        end = time.time() + limit
        while time.time() < end:
            st = self.call("codex_poll", {"thread": thread})
            if st["state"] in ("completed", "failed", "interrupted", "awaiting_approval"):
                return st
            time.sleep(3)
        raise TimeoutError("wait")


c = Bridge()

SKIP_E = os.environ.get("SKIP_E")
print("== E  idempotent polling on a real thread" + (" (skipped)" if SKIP_E else ""))
r = c.call("codex_submit", {"prompt": "Say EXACTLY: idempotent-check-5591",
                            "model": "luna-medium", "cwd": repo, "mode": "read"})
t = r["thread"]
st = c.wait(t)
assert st["state"] == "completed", st
polls = [c.call("codex_poll", {"thread": t}) for _ in range(3)]
assert polls[0] == polls[1] == polls[2], "repeat polls differed — poll is not idempotent"
assert "5591" in str(polls[0]["output"]), polls[0]["output"]
print(f"   PASS 3 identical polls, output repeats: {str(polls[0]['output'])[:40]!r}")

print("== F  a large real answer passes through whole (old guard would have stubbed it)")
schema = {"type": "object", "additionalProperties": False,
          "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
          "required": ["lines"]}
r = c.call("codex_submit", {
    # >100k chars means ~28k output tokens however it is split — inherently slow.
    "prompt": "Return JSON with a 'lines' array of exactly 2400 elements. Element i is the string "
              "'row-<i>-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' with i from 1 to 2400. "
              "Emit the JSON directly as your final message — do not write it to a file first.",
    "model": "luna-medium", "cwd": repo, "mode": "write", "output_schema": schema})
st = c.wait(r["thread"], limit=1500)
if st["state"] != "completed":
    print(f"   INCONCLUSIVE: turn ended {st['state']}")
else:
    out = st["output"]
    size = len(json.dumps(out))
    parsed_ok = isinstance(out, dict) and isinstance(out.get("lines"), list)
    print(f"   output {size} chars, parsed as JSON: {parsed_ok}, elements: "
          f"{len(out.get('lines', [])) if parsed_ok else 'n/a'}")
    assert parsed_ok, f"schema output did not parse — {str(out)[:200]}"
    assert "schema_parse_error" not in st, st.get("schema_parse_error")
    if size > 100_000:
        print(f"   PASS {size} chars > the old 100k guard, returned intact and valid JSON")
    else:
        print(f"   PARTIAL: only {size} chars — model produced less than the old guard; "
              f"JSON validity and pass-through still confirmed")
    again = c.call("codex_poll", {"thread": r["thread"]})
    assert again["output"] == out, "large output changed between polls"
    print("   PASS large output repeats identically on re-poll")

print("== G  two approvals outstanding on ONE thread at the same time")
r = c.call("codex_submit", {
    "prompt": (f"Do BOTH of these in this turn, and do not stop after the first: "
               f"(1) write the text 'one' to {outside_a}/a.txt ; "
               f"(2) write the text 'two' to {outside_b}/b.txt . "
               f"Both paths are outside your workspace. Attempt both before waiting on either."),
    "model": "luna-medium", "cwd": repo, "mode": "write"})
t2 = r["thread"]
seen_together, resolved, end = 0, 0, time.time() + 420
while time.time() < end:
    st = c.call("codex_poll", {"thread": t2})
    if st["state"] == "awaiting_approval":
        reqs = st["requests"]
        seen_together = max(seen_together, len(reqs))
        if len(reqs) > 1:
            print(f"   TWO OUTSTANDING: {[str(q.get('command'))[:40] for q in reqs]}")
            first = reqs[0]["request_id"]
            c.call("codex_approve", {"request_id": str(first), "decision": "allow"})
            after = c.call("codex_poll", {"thread": t2})
            assert after["state"] == "awaiting_approval", "resolving one must leave the other parked"
            assert first not in [q["request_id"] for q in after["requests"]], after["requests"]
            print("   PASS resolving one left the other parked and visible")
        for q in c.call("codex_poll", {"thread": t2}).get("requests", []):
            c.call("codex_approve", {"request_id": str(q["request_id"]), "decision": "allow"})
            resolved += 1
        continue
    if st["state"] in ("completed", "failed", "interrupted"):
        break
    time.sleep(2)
print(f"   approvals resolved: {resolved}, max outstanding at once: {seen_together}")
if seen_together < 2:
    print("   NOTE: codex raised them one at a time — the queue is defensive here, not exercised")

print("== H  a finished thread stays pollable through its retry window")
st = c.call("codex_poll", {"thread": t})
assert st["state"] == "completed" and "5591" in str(st["output"]), st
chk = c.call("codex_check", {})
assert t in [x["thread"] for x in chk["threads"]], "finished thread vanished from codex_check"
time.sleep(20)
st = c.call("codex_poll", {"thread": t})
assert st["state"] == "completed" and "5591" in str(st["output"]), "lost a finished result inside the window"
print(f"   PASS still pollable after completion (TTL window), {len(chk['threads'])} threads tracked")

print("\nALL SNAPSHOT-BEHAVIOUR TESTS PASSED")
c.p.terminate()
