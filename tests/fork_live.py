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
