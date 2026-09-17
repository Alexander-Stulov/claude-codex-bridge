#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

The case that matters in practice: a thread this extension created, picked back up
later — next day, next week, after Desktop restarted and the bridge process is gone.

  P  attach-then-continue — a NEW bridge process resumes a thread it never saw and
                            adds a turn that still has the earlier context
  Q  honest provenance    — the resumed thread reports the model/mode it ACTUALLY ran
                            with, read from codex, never a fabricated default
  R  poll-then-continue   — polling first (which only reads) then submitting works, and
                            the thread is not duplicated or relocated
"""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="resume-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
open(os.path.join(repo, "secret.txt"), "w").write("resume-secret-4417\n")


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

    def rpc(self, method, params, t=900):
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

    def call(self, n, a, t=900):
        r = self.rpc("tools/call", {"name": n, "arguments": a}, t)
        b = json.loads(r["result"]["content"][0]["text"])
        if r["result"].get("isError"):
            raise RuntimeError(json.dumps(b)[:400])
        return b

    def wait(self, thread, limit=600):
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


print("== setup: create a thread with a distinctive model, then lose the bridge entirely")
a = Bridge()
r = a.call("codex_submit", {"prompt": "Read secret.txt and remember the exact string in it. Reply: READ.",
                            "model": "terra-high", "cwd": repo, "mode": "write"})
t = r["thread"]
st = a.wait(t)
assert st["state"] == "completed", st
print(f"   created {t} with terra-high/write; first turn: {str(st['output'])[:40]!r}")
a.p.terminate(); a.p.wait(timeout=20)
time.sleep(1)

print("== P  a new bridge continues the thread with prior context intact")
b = Bridge()
assert t not in [x["thread"] for x in b.call("codex_check", {})["threads"]], "new bridge already knows it"
r2 = b.call("codex_submit", {"prompt": "Without reading any file again, what exact string did you read earlier?",
                             "model": "terra-high", "mode": "write"} | {"thread": t})
print(f"   resumed -> cwd={r2.get('cwd')} workspace={r2.get('workspace')}")
assert os.path.realpath(r2["cwd"]) == os.path.realpath(repo), (r2["cwd"], repo)
st2 = b.wait(t)
assert st2["state"] == "completed", st2
assert "4417" in str(st2["output"]), f"context was lost across the restart: {st2['output']!r}"
print(f"   PASS continued with context: {str(st2['output'])[:60]!r}")

print("== Q  provenance must be read, not invented")
prov = st2["provenance"]
print(f"   model={prov['model']} slug={prov['model_slug']} effort={prov['effort']} mode={prov['mode']}")
assert prov["model_slug"] == "terra-high", f"reported {prov['model_slug']}, not the model actually used"
assert prov["mode"] == "write", f"reported mode={prov['mode']}, not the sandbox actually in force"
b.p.terminate(); b.p.wait(timeout=20)
time.sleep(1)

print("== Q2 read WITHOUT being told the model: must read it off codex, not default")
c = Bridge()
peek = c.call("codex_poll", {"thread": t})
print(f"   poll-only read -> read_only={peek.get('read_only')} state={peek['state']} "
      f"model={(peek.get('provenance') or {}).get('model_slug')} "
      f"mode={(peek.get('provenance') or {}).get('mode')}")
assert peek.get("read_only") is True and "resumed" not in peek, peek
assert t not in [x["thread"] for x in c.call("codex_check", {})["threads"]], "a poll must not attach the thread"
pv = peek.get("provenance") or {}
assert pv.get("model_slug") != "luna-medium" or "terra" in str(pv.get("model")), \
    f"attach fabricated a default model instead of reading it: {pv}"
# mode is NOT inferable: thread/resume reports the thread's stored sandbox (readOnly by
# default) while every turn carries its own sandboxPolicy. Unknown beats fabricated.
assert pv.get("mode") is None, \
    f"the read claimed mode={pv.get('mode')!r}; it cannot know, and must not guess"
print("   PASS model read from codex; mode reported as unknown rather than fabricated")

print("== R  poll-then-continue: codex_submit attaches the thread a poll only read")
r3 = c.call("codex_submit", {"prompt": "Say STILL-HERE.", "model": "terra-high", "thread": t, "mode": "write"})
assert os.path.realpath(r3["cwd"]) == os.path.realpath(repo), r3["cwd"]
st3 = c.wait(t)
assert st3["state"] == "completed" and "STILL-HERE" in str(st3["output"]).upper(), st3
live = [x["thread"] for x in c.call("codex_check", {})["threads"]]
assert live.count(t) == 1, f"thread duplicated in the table: {live}"
print(f"   PASS continued after a read-only poll; one entry in the table, cwd unchanged")

print("\nALL RESUME TESTS PASSED")
c.p.terminate()
