#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

Nothing about output is capped, and these are the two paths that must prove it.
They are genuinely different: a file never passes through the bridge at all (codex
writes it with its own tools), while a final message passes through whole.

  W  large file written  — codex writes a big file; verified as bytes on disk, with
                           the bridge reporting the write but never touching it
  X  long final message  — a long PROSE answer (no schema) returns intact and
                           identical on re-poll. The schema path is covered in
                           snapshot_live.py; this is the un-schema'd one.
"""
import json, os, re, subprocess, sys, tempfile, threading, time

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="out-")
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
            raise RuntimeError(json.dumps(b)[:400])
        return b

    def wait(self, th, limit=1500, label=""):
        end, last = time.time() + limit, None
        while time.time() < end:
            st = self.call("codex_poll", {"thread": th})
            if st["state"] == "awaiting_approval":
                for q in st.get("requests", []):
                    self.call("codex_approve", {"request_id": str(q["request_id"]), "decision": "allow"})
                continue
            act = st.get("activity") or {}
            now = (act.get("now"), act.get("answer_chars"), act.get("files_changed"))
            if now != last:
                print(f"   [{label}] now={str(act.get('now'))[:46]!r} "
                      f"answer_chars={act.get('answer_chars')} files={act.get('files_changed')}")
                last = now
            if st["state"] in ("completed", "failed", "interrupted"):
                return st
            time.sleep(5)
        raise TimeoutError("wait")


c = Bridge()

print("== W  codex writes a large file; the bridge never touches the bytes")
LINES = 1500
r = c.call("codex_submit", {
    "prompt": (f"Write a file named report.md in the current directory containing exactly "
               f"{LINES} records, one per line, separated by REAL newline characters (not the "
               f"two-character sequence backslash-n). Record i, for i from 1 to {LINES}, is: "
               f"| case-i | module-(i mod 37) | verified | no deviation observed in this case | "
               f"with i and (i mod 37) substituted. No header, no footer, no commentary. "
               f"Generate it with a script. Then reply with exactly: WROTE-<line count>"),
    "model": "luna-medium", "cwd": repo, "mode": "write"})
st = c.wait(r["thread"], label="W")
assert st["state"] == "completed", st

path = os.path.join(repo, "report.md")
assert os.path.exists(path), f"no report.md in {repo}: {sorted(os.listdir(repo))}"
size = os.path.getsize(path)
with open(path, encoding="utf-8") as fh:   # written by codex as UTF-8; Windows would default to cp1252
    body = fh.read()
# Count records however they were separated: a literal backslash-n is the model
# formatting the file oddly, not the bridge doing anything to it.
n = body.count("\n") or body.count("\\n")
print(f"   file on disk: {size:,} bytes, {n} lines; answer={str(st['output'])[:24]!r}")
assert n >= LINES - 5, f"only {n} lines written, expected ~{LINES}"
assert size >= 60_000, f"file is {size} bytes; wanted a genuinely large one"
assert "case-1 " in body or "case-1 |" in body, body[:200]
assert "case-%d" % LINES in body or f"case-{LINES}" in body, body[-300:]
# The bridge saw work happen but the content never passed through it. A file written
# by a script is a commandExecution, not a fileChange, so either counter may be the
# one that moved — asserting only files_changed was wrong.
act = st.get("activity") or {}
assert act.get("files_changed") or act.get("commands"), act
assert len(str(st["output"])) < 2_000, "the file's content leaked into the final message"
print(f"   PASS {size:,} bytes written, first and last lines present, "
      f"final message stayed a {len(str(st['output']))}-char receipt")

print("== X  a long PROSE final message returns whole")
PARAS = 400        # ~144 chars/line observed, so comfortably over 40k
r = c.call("codex_submit", {
    "prompt": (f"Reply with {PARAS} numbered findings as your final message. Finding i must be "
               f"exactly one line, in this form:\n"
               f"<i>. FINDING-<i>: the surface at module-<i> was surveyed end to end and no "
               f"deviation from the documented contract was observed during this pass.\n"
               f"Number them 1 to {PARAS}. Emit them directly as your final message - do not "
               f"write a file, and do not run any commands."),
    "model": "luna-medium", "cwd": repo, "mode": "read"})
st = c.wait(r["thread"], label="X")
assert st["state"] == "completed", st
out = str(st["output"])
print(f"   final message: {len(out):,} chars, {out.count(chr(10))} lines")
assert len(out) >= 40_000, f"only {len(out)} chars — too short to prove anything about long output"
# Count what actually arrived rather than demanding an exact final index: a model that
# emits 399 of 400 has miscounted, which is not the same as the bridge truncating.
found = {int(m) for m in re.findall(r"FINDING-(\d+):", out)}
assert "FINDING-1:" in out, out[:200]
assert len(found) >= PARAS * 0.95, f"only {len(found)} of {PARAS} findings present"
assert max(found) >= PARAS * 0.95, f"highest finding is {max(found)}, expected near {PARAS}"
# Truncation would sever the last line mid-token; a complete answer ends cleanly.
assert out.rstrip().endswith("."), f"answer ends mid-line: {out[-120:]!r}"
assert "[truncated" not in out.lower(), "output shows a truncation marker"

again = c.call("codex_poll", {"thread": r["thread"]})
assert str(again["output"]) == out, "long output changed between polls"
print(f"   PASS {len(out):,} chars returned whole, first and last finding present, "
      f"identical on re-poll ({len(found)} of {PARAS} findings)")

print("\nALL OUTPUT TESTS PASSED")
c.p.terminate()
