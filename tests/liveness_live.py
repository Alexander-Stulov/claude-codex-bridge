#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

Two 0.6.1 claims that had never executed:
  I  recovery      — a bridge that has never seen a thread must still answer for it,
                     rebuilt from codex's own record, with the same output shape
  J  liveness      — a single long message streams for minutes with no command and no
                     item boundary. quiet_seconds must stay small anyway, or the caller
                     cannot tell "writing a 128k answer" from "wedged".
"""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="live-")
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


SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
          "required": ["lines"]}
BIG = ("Return JSON with a 'lines' array of exactly 1200 elements. Element i is the string "
       "'row-<i>-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' with i from 1 to 1200. "
       "Emit the JSON directly as your final message — do not write it to a file, and do "
       "not run any shell commands.")

print("== J  a long single message must not look like a stall")
a = Bridge()
r = a.call("codex_submit", {"prompt": BIG, "model": "luna-medium", "cwd": repo,
                            "mode": "read", "output_schema": SCHEMA})
t = r["thread"]
worst_quiet, samples, saw_tokens, end = 0, 0, False, time.time() + 1500
while time.time() < end:
    st = a.call("codex_poll", {"thread": t})
    if st["state"] in ("completed", "failed", "interrupted"):
        break
    act = st.get("activity") or {}
    if "quiet_seconds" in act:
        samples += 1
        worst_quiet = max(worst_quiet, act["quiet_seconds"])
        if act.get("tokens"):
            saw_tokens = True
        print(f"   running={act.get('running_seconds')}s quiet={act['quiet_seconds']}s "
              f"tokens={(act.get('tokens') or {}).get('outputTokens')} now={str(act.get('now'))[:40]!r}")
    time.sleep(10)

assert st["state"] == "completed", st
assert samples >= 3, f"turn finished too fast to observe liveness ({samples} samples) — nothing proven"
print(f"   {samples} samples while generating, worst quiet_seconds = {worst_quiet}, tokens seen = {saw_tokens}")
assert worst_quiet < 60, (f"quiet_seconds reached {worst_quiet}s during an actively streaming message — "
                          f"the liveness signal does not work; a long answer is indistinguishable from a stall")
print(f"   PASS a streaming message stayed visibly alive (max silence {worst_quiet}s)")

original = st["output"]
assert isinstance(original, dict) and len(original.get("lines", [])) == 1200, str(original)[:200]

print("== I  a bridge that never saw this thread must still answer for it")
a.p.terminate()
time.sleep(1)
b = Bridge()
chk = b.call("codex_check", {})
assert t not in [x["thread"] for x in chk["threads"]], "new bridge already knows the thread — not a recovery test"
rec = b.call("codex_poll", {"thread": t})
print(f"   resumed={rec.get('resumed')} state={rec['state']} "
      f"cwd={(rec.get('provenance') or {}).get('cwd')}")
assert rec.get("resumed") is True, "poll answered without marking the result as recovered"
assert rec["state"] == "completed", rec
assert isinstance(rec["output"], dict), f"recovered output came back as {type(rec['output']).__name__}, " \
                                        f"not the object the first poll returned"
assert rec["output"] == original, "recovered output differs from what the original bridge returned"
assert os.path.realpath((rec.get("provenance") or {}).get("cwd", "")) == os.path.realpath(repo), \
    f"recovery invented a cwd instead of reading the thread's own: {rec.get('provenance')}"
print(f"   PASS recovered {len(rec['output']['lines'])} lines identically, from codex's record alone")

print("== I2 an unknown thread id must fail clearly, not silently")
try:
    b.call("codex_poll", {"thread": "thr_does_not_exist_0000"})
    print("   FAIL: polling a nonexistent thread returned success")
    sys.exit(1)
except RuntimeError as e:
    assert "unknown thread" in str(e), str(e)[:200]
    print("   PASS clear error for a genuinely unknown thread")

print("\nALL LIVENESS + RECOVERY TESTS PASSED")
b.p.terminate()
