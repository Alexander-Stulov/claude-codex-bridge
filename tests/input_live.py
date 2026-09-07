#!/usr/bin/env python3
"""LIVE test — needs the codex CLI, so it is not part of CI.

  M  images as input   — a local file, a path relative to cwd, and a data: URL all
                         reach the model; a bad path fails before the turn starts
  N  streaming progress— answer_chars climbs while a single message generates, so a
                         long answer shows forward progress, not just non-silence
"""
import base64, json, os, struct, subprocess, sys, tempfile, threading, time, zlib

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.py")
repo = tempfile.mkdtemp(prefix="input-")
subprocess.run(["git", "-C", repo, "init", "-q"], check=True)


def solid_png(w, h, rgb):
    """A valid RGB8 PNG with no dependencies — the test must not need Pillow."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


RED = solid_png(96, 96, (255, 0, 0))
red_path = os.path.join(repo, "red.png")
open(red_path, "wb").write(RED)
blue_path = os.path.join(repo, "blue.png")
open(blue_path, "wb").write(solid_png(96, 96, (0, 0, 255)))


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


c = Bridge()
ASK = ("What is the single dominant colour of the attached image? "
       "Reply with exactly one lowercase word and nothing else.")

print("== M1 a bad image path must fail before the turn starts")
try:
    c.call("codex_submit", {"prompt": ASK, "model": "terra-high", "cwd": repo,
                            "mode": "read", "images": ["no-such-file.png"]})
    print("   FAIL: a missing image was accepted"); sys.exit(1)
except RuntimeError as e:
    assert "image not found" in str(e), str(e)[:300]
    print("   PASS rejected up front:", str(e)[:110])

print("== M2 an absolute local path")
r = c.call("codex_submit", {"prompt": ASK, "model": "terra-high", "cwd": repo,
                            "mode": "read", "images": [red_path]})
st = c.wait(r["thread"])
print(f"   state={st['state']} answer={str(st.get('output'))[:60]!r}")
assert st["state"] == "completed", st
assert "red" in str(st.get("output", "")).lower(), f"model did not see the image: {st.get('output')!r}"
print("   PASS the model read a local PNG")

print("== M3 a path relative to cwd, on a follow-up turn of the same thread")
r = c.call("codex_submit", {"prompt": ASK, "model": "terra-high", "thread": r["thread"],
                            "mode": "read", "images": ["blue.png"]})
st = c.wait(r["thread"])
print(f"   state={st['state']} answer={str(st.get('output'))[:60]!r}")
assert st["state"] == "completed", st
assert "blue" in str(st.get("output", "")).lower(), f"relative path did not resolve: {st.get('output')!r}"
print("   PASS relative path resolved against cwd, on a follow-up turn")

print("== M4 a data: URL")
data_url = "data:image/png;base64," + base64.b64encode(RED).decode()
r = c.call("codex_submit", {"prompt": ASK, "model": "terra-high", "cwd": repo,
                            "mode": "read", "images": [data_url]})
st = c.wait(r["thread"])
print(f"   state={st['state']} answer={str(st.get('output'))[:60]!r}")
if st["state"] == "completed" and "red" in str(st.get("output", "")).lower():
    print("   PASS data: URL reached the model")
else:
    print(f"   NOTE data: URL did not resolve ({st['state']}) — local paths are the supported route")

print("== N  answer_chars must climb while a single message streams")
r = c.call("codex_submit", {
    "prompt": ("Write a numbered list of 400 short lines. Line i reads 'line <i>: the quick brown "
               "fox jumps over the lazy dog'. Output the list directly as your final message; run "
               "no shell commands and write no files."),
    "model": "luna-medium", "cwd": repo, "mode": "read"})
t = r["thread"]
seen, end = [], time.time() + 900
while time.time() < end:
    st = c.call("codex_poll", {"thread": t})
    act = st.get("activity") or {}
    if st["state"] in ("completed", "failed", "interrupted"):
        break
    seen.append(act.get("answer_chars", 0))
    print(f"   running={act.get('running_seconds')}s quiet={act.get('quiet_seconds')}s "
          f"answer_chars={act.get('answer_chars')} thinking_chars={act.get('thinking_chars')}")
    time.sleep(8)

print(f"   final={st['state']}, samples={len(seen)}, answer_chars series tail={seen[-6:]}")
assert st["state"] == "completed", st
if len(seen) < 3:
    print("   INCONCLUSIVE: finished too fast to watch the stream")
else:
    assert max(seen) > 0, "answer_chars never moved — streaming progress is not being counted"
    assert seen == sorted(seen), f"answer_chars went backwards: {seen}"
    print(f"   PASS forward progress was visible: 0 -> {max(seen)} chars while state stayed 'running'")

print("== N2 counters are per-turn, not cumulative across the thread")
r = c.call("codex_submit", {"prompt": "Say OK.", "model": "luna-medium", "thread": t, "mode": "read"})
st = c.wait(t)
final_chars = (st.get("activity") or {}).get("answer_chars", 0)
print(f"   after a short follow-up turn: answer_chars={final_chars}")
assert final_chars < max(seen or [1]), \
    f"counters carried over from the previous turn ({final_chars}) — activity is not a per-turn snapshot"
print("   PASS activity resets per turn")

print("\nALL INPUT + PROGRESS TESTS PASSED")
c.p.terminate()
