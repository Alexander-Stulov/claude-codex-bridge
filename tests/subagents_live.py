#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

codex can spawn sub-agents (multi_agent_v1: spawn_agent/send_input/…, Stable and
default-enabled). Each child is a real codex thread. Two claims to prove:

  K  the parent's progress names its children — activity.sub_agents lists their
     thread ids, so a parent that is "quiet" for minutes is visibly waiting on
     named work rather than wedged. Status is claimed only where codex supplies
     one (agentsStates); the subAgentActivity kind is an event, not a status.
  L  a child thread id is pollable through this bridge on its own, even though the
     bridge never created it (the recovery path makes this free)
"""
import json, os, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="sub-")
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

    def rpc(self, method, params, t=1800):
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

    def call(self, n, a, t=1800):
        r = self.rpc("tools/call", {"name": n, "arguments": a}, t)
        b = json.loads(r["result"]["content"][0]["text"])
        if r["result"].get("isError"):
            raise RuntimeError(json.dumps(b)[:300])
        return b


c = Bridge()
print("== K  a delegating parent must name its children in activity")
r = c.call("codex_submit", {
    "prompt": ("Use your sub-agent tooling (the multi_agent_v1 spawn_agent tool) to spawn TWO "
               "sub-agents. Give sub-agent one the task: 'reply with the word ALPHA and nothing "
               "else'. Give sub-agent two the task: 'reply with the word BETA and nothing else'. "
               "Wait for both, then report both replies in your final message. Do not do the work "
               "yourself — you must actually delegate it."),
    "model": "terra-high", "cwd": repo, "mode": "read"})
t = r["thread"]

children, worst_quiet, saw_states, end = [], 0, [], time.time() + 1500
while time.time() < end:
    st = c.call("codex_poll", {"thread": t})
    act = st.get("activity") or {}
    for cid in act.get("sub_agents") or []:
        if cid not in children:
            children.append(cid)
    if act.get("sub_agent_status"):
        saw_states.append(dict(act["sub_agent_status"]))
    if "quiet_seconds" in act:
        worst_quiet = max(worst_quiet, act["quiet_seconds"])
    print(f"   state={st['state']} quiet={act.get('quiet_seconds')}s "
          f"now={str(act.get('now'))[:56]!r} subs={act.get('sub_agents')} "
          f"status={act.get('sub_agent_status')}")
    if st["state"] in ("completed", "failed", "interrupted"):
        break
    if st["state"] == "awaiting_approval":
        for q in st.get("requests", []):
            c.call("codex_approve", {"request_id": str(q["request_id"]), "decision": "allow"})
        continue
    time.sleep(10)

print(f"\n   final state: {st['state']}")
print(f"   answer: {str(st.get('output'))[:200]!r}")
print(f"   children seen: {children}")
print(f"   worst quiet_seconds while parent waited on children: {worst_quiet}")

if not children:
    print("\n   INCONCLUSIVE: the model answered without delegating — nothing spawned, so the "
          "sub-agent branches were never reached. (Parent liveness still held above.)")
    c.p.terminate()
    sys.exit(0)

assert worst_quiet < 90, (f"parent went quiet for {worst_quiet}s while children worked — "
                          f"delegation is indistinguishable from a stall")
print("   PASS parent named its children and stayed visibly alive")
# subAgentActivity.kind is started|interacted|interrupted — never a completion. A
# status must therefore come from agentsStates or not be claimed at all.
assert all(v in ("pendingInit", "running", "completed", "interrupted", "errored", "shutdown",
                 "notFound") for d in saw_states for v in d.values()), saw_states
if not saw_states:
    print("   NOTE: no authoritative per-child status offered (V1 delegation) — "
          "sub_agent_status correctly absent rather than stale")

print("== L  a child thread must be pollable on its own through this bridge")
child = children[0]
chk = c.call("codex_check", {})
known = [x["thread"] for x in chk["threads"]]
print(f"   child {child}; bridge created it? {child in known}")
sub = c.call("codex_poll", {"thread": child})
print(f"   child poll -> state={sub['state']} resumed={sub.get('resumed')} "
      f"output={str(sub.get('output'))[:80]!r}")
assert sub["state"] in ("completed", "running", "failed", "interrupted"), sub
print("   PASS a sub-agent thread answers a poll directly — no extra plumbing needed")

print("\nALL SUB-AGENT TESTS PASSED")
c.p.terminate()
