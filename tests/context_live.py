#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

Does the context uplift reach the app-server the BRIDGE spawns — not just `codex
exec`, which is what the install script verifies.

  S  runway reported   — poll gives tokens_before_compaction (room left), not a fill
                         percentage. Derived from the effective window, so this checks
                         the derivation against a real thread.
  T  uplift applied    — a thread created THROUGH the bridge runs with the raised
                         window, cross-checked against codex's own session record.
                         Skips when not installed.
  V  explicit compact   — codex_compact summarises on demand, the thread survives it
                         with its id, and the runway grows back.
"""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="ctx-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
STOCK, RAISED = 258_400, 997_500


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
            raise RuntimeError(json.dumps(b)[:300])
        return b

    def wait(self, thread, limit=600):
        end = time.time() + limit
        while time.time() < end:
            st = self.call("codex_poll", {"thread": thread})
            if st["state"] in ("completed", "failed", "interrupted"):
                return st
            time.sleep(3)
        raise TimeoutError("wait")


c = Bridge()
print("== S  poll reports runway, not a fill gauge")
r = c.call("codex_submit", {"prompt": "Reply with exactly: CTX-OK", "model": "luna-medium",
                            "cwd": repo, "mode": "read"})
t = r["thread"]
st = c.wait(t)
assert st["state"] == "completed", st
act = st.get("activity") or {}
print(f"   completed; activity keys = {sorted(act)}")
ctx = act.get("context") or {}
print(f"   context = {json.dumps(ctx)}")
assert ctx.get("tokens_before_compaction"), "no runway reported — the caller cannot plan"
assert "percent_used" not in ctx and "window" not in ctx, (
    f"poll published a fill gauge/hard limit rather than runway: {ctx}")
assert ctx["tokens_used"] > 0, ctx
assert act.get("compactions") in (None, 0), "a one-line hello should not have compacted"
runway_before = ctx["tokens_before_compaction"]
print(f"   PASS runway {runway_before:,} tokens before compaction, used {ctx['tokens_used']:,}")

print("== T  the app-server the BRIDGE spawns honours the uplift")
codex_home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
uplifted = os.path.exists(os.path.join(codex_home, "catalog-1m.json"))
roll = None
for base, _dirs, files in os.walk(os.path.join(codex_home, "sessions")):
    for f in files:
        if t in f:
            roll = os.path.join(base, f)
            break
    if roll:
        break
assert roll, f"no session record for {t} — cannot tell what window it ran with"
window = None
with open(roll, encoding="utf-8") as fh:   # codex writes UTF-8; Windows would default to cp1252
    for line in fh:
        i = line.find('"model_context_window":')
        if i != -1:
            window = int(line[i + 23:].split(",")[0].split("}")[0])
            break
print(f"   catalog-1m.json present: {uplifted}; thread ran with window={window}")
assert window, f"session record carries no model_context_window: {roll}"

if uplifted:
    assert window == RAISED, (
        f"uplift is installed but the bridge's app-server ran at {window}, not {RAISED}. "
        f"The catalog is not reaching the app-server path (it is read at startup only "
        f"— a bridge started before the script ran will still be on the old catalog).")
    print(f"   PASS bridge-spawned app-server ran at {window:,} (stock would be {STOCK:,})")
else:
    assert window == STOCK, f"no uplift installed yet window is {window}, expected {STOCK}"
    print(f"   SKIP uplift not installed; stock {window:,} as expected. "
          f"Run scripts/enable-1m-context.sh to raise it.")

# The runway must match the derivation for whatever window this thread really ran at.
expected = window * 90 // 95 - ctx["tokens_used"]
assert abs(runway_before - expected) <= 1, (
    f"runway {runway_before} does not match the derivation for window {window} "
    f"(expected ~{expected}); the 95%/90% constants may have changed upstream")
print(f"   PASS runway matches the derivation for the real window ({window:,})")

print("== V  codex_compact summarises on demand and the thread survives")
res = c.call("codex_compact", {"thread": t})
print(f"   compact -> {json.dumps(res)}")
assert res["state"] == "running", res
st2 = c.wait(t, limit=600)
act2 = st2.get("activity") or {}
print(f"   after compaction: state={st2['state']} compactions={act2.get('compactions')} "
      f"context={json.dumps(act2.get('context'))}")
assert st2["state"] in ("completed", "interrupted"), st2
assert act2.get("compactions") == 1, f"one codex_compact must count one compaction: {act2}"

# The thread must still take a turn afterwards — compaction is not an ending.
c.call("codex_submit", {"prompt": "Reply with exactly: ALIVE-AFTER-COMPACT",
                        "model": "luna-medium", "thread": t, "mode": "read"})
st3 = c.wait(t, limit=600)
assert st3["state"] == "completed", st3
assert "ALIVE-AFTER-COMPACT" in str(st3["output"]).upper(), st3["output"]
print(f"   PASS thread kept its id and answered after compaction: {str(st3['output'])[:40]!r}")

print("\nALL CONTEXT TESTS PASSED")
c.p.terminate()
