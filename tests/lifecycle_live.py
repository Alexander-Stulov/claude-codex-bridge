#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

The paths 0.6.0 shipped but never ran: steer, interrupt, app-server restart
(generation fix), and cold resume of a thread this bridge has never seen (#7 —
must recover the thread's own cwd, not invent a scratch dir)."""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="lifecycle-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
open(os.path.join(repo, "marker.txt"), "w").write("lifecycle-secret-8842\n")


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

    def call(self, n, a, t=300):
        r = self.rpc("tools/call", {"name": n, "arguments": a}, t)
        b = json.loads(r["result"]["content"][0]["text"])
        if r["result"].get("isError"):
            raise RuntimeError(json.dumps(b)[:300])
        return b

    def wait(self, thread, limit=300, resolve="allow"):
        end = time.time() + limit
        while time.time() < end:
            st = self.call("codex_poll", {"thread": thread})
            if st["state"] == "awaiting_approval":
                for req in st["requests"]:
                    self.call("codex_approve", {"request_id": str(req["request_id"]), "decision": resolve})
                continue
            if st["state"] in ("completed", "failed", "interrupted"):
                return st
            time.sleep(3)
        raise TimeoutError("wait")

    def codex_child(self):
        if sys.platform == "win32":
            # no pgrep on Windows: ask WMI for the codex child of the bridge process
            query = (f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={self.p.pid}' "
                     "| Where-Object Name -like 'codex*').ProcessId")
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", query]
        else:
            cmd = ["pgrep", "-P", str(self.p.pid)]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None


print("== A  steer: submitting to a RUNNING thread injects into the live turn")
a = Bridge()
r = a.call("codex_submit", {"prompt": "Run exactly this shell command and wait for it: sleep 60 . "
                                      "After it finishes, say DONE.",
                            "model": "luna-medium", "cwd": repo, "mode": "read"})
t = r["thread"]
for _ in range(20):                      # confirm it really is mid-turn before steering
    time.sleep(2)
    if a.call("codex_poll", {"thread": t})["state"] == "running":
        break
state_before = a.call("codex_poll", {"thread": t})["state"]
print(f"   state before second submit: {state_before}")
assert state_before == "running", f"precondition failed — nothing to steer (state={state_before})"
steered = a.call("codex_submit", {"prompt": "Actually stop waiting and just say STEERED.",
                                  "model": "luna-medium", "thread": t, "mode": "read"})
print("   submit-while-running ->", json.dumps({k: steered.get(k) for k in ("state", "steered", "turn")}))
assert steered.get("steered") is True, f"expected a steer, got {steered}"
st = a.wait(t)
print(f"   PASS steered a live turn; final state={st['state']}")

print("== B  interrupt: stop a running turn, thread stays usable")
r = a.call("codex_submit", {"prompt": "Run exactly this shell command and wait: sleep 90 . Then say DONE.",
                            "model": "luna-medium", "thread": t, "mode": "read"})
for _ in range(20):
    time.sleep(2)
    if a.call("codex_poll", {"thread": t})["state"] == "running":
        break
out = a.call("codex_interrupt", {"thread": t})
print("   interrupt ->", json.dumps(out))
st = a.wait(t, limit=120)
assert st["state"] in ("interrupted", "completed"), st
r = a.call("codex_submit", {"prompt": "Say ALIVE.", "model": "luna-medium", "thread": t, "mode": "read"})
st = a.wait(t, limit=180)
assert st["state"] == "completed", st
print(f"   PASS interrupted, then the same thread took another turn: {str(st['output'])[:40]!r}")

print("== C  app-server dies mid-session: generation fix must recover, not wedge")
child = a.codex_child()
assert child, "could not find the app-server child"
print(f"   killing app-server child pid {child}")
os.kill(child, 9)                      # SIGKILL on POSIX, TerminateProcess on Windows
time.sleep(3)
r = a.call("codex_submit", {"prompt": "Say RECOVERED.", "model": "luna-medium", "cwd": repo, "mode": "read"})
st = a.wait(r["thread"], limit=240)
assert st["state"] == "completed", st
print(f"   PASS bridge started a fresh app-server and completed a new thread: {str(st['output'])[:40]!r}")
a.p.terminate()

print("== D  cold resume: a NEW bridge, thread it has never seen, no cwd given")
b = Bridge()
r = b.call("codex_submit", {"prompt": "Without listing files, what is the exact secret string in marker.txt "
                                      "that you read earlier in this thread? If you never read it, read it now.",
                            "model": "luna-medium", "thread": t, "mode": "read"})
print("   resumed ->", json.dumps({k: r.get(k) for k in ("thread", "cwd", "workspace")}))
assert r["thread"] == t, r
assert "/scratch/" not in r["cwd"], f"cold resume invented a scratch dir instead of the thread's cwd: {r['cwd']}"
assert os.path.realpath(r["cwd"]) == os.path.realpath(repo), (r["cwd"], repo)
st = b.wait(r["thread"], limit=240)
assert st["state"] == "completed", st
assert "8842" in str(st["output"]), st["output"]
print(f"   PASS resumed into the original repo and answered: {str(st['output'])[:60]!r}")
b.p.terminate()

print("\nALL LIFECYCLE TESTS PASSED")
