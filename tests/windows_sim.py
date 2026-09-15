#!/usr/bin/env python3
"""Windows behaviour without Windows — no codex needed either.

The win32-only branches of server.py, driven with sys.platform and os.path swapped for
their Windows counterparts (on a real Windows host the swaps are no-ops and the real
environment is used). Covers the gate that keeps Windows dispatch behind codex's own
sandbox, the workspace refusals, codex discovery, and the resumed-thread rule.
"""
import importlib.util, ntpath, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bridge", os.path.join(HERE, "..", "server.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

# Filesystem fixtures first, while os.path is still the host's.
tmp = tempfile.mkdtemp()
fake_bin = os.path.join(tmp, "Programs", "OpenAI", "Codex", "bin")
os.makedirs(fake_bin, exist_ok=True)
open(os.path.join(fake_bin, "codex.exe"), "w").close()
project = os.path.join(tmp, "proj")
os.makedirs(project, exist_ok=True)

if sys.platform != "win32":
    sys.platform = "win32"
    os.path = ntpath
    _real_stat = os.stat                      # ntpath builds backslashed paths; let the host stat see them
    os.stat = lambda p, *a, **k: _real_stat(str(p).replace("\\", "/"), *a, **k)
    os.environ.update({"USERPROFILE": r"C:\Users\alex", "HOME": r"C:\Users\alex",
                       "SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files",
                       "ProgramData": r"C:\ProgramData"})
os.environ["LOCALAPPDATA"] = tmp
home = os.path.expanduser("~")

# --- workspace refusal --------------------------------------------------------
R = lambda p: bridge.refuse_reason(os.path.realpath(p))
assert R("C:\\") and "whole drive" in R("C:\\"), R("C:\\")
assert R("D:\\"), "every drive root is refused"
assert R(os.environ["SystemRoot"]) and R(os.environ["SystemRoot"].lower()), "system root, any case"
assert R(os.environ["ProgramFiles"]) and R(os.environ["ProgramData"])
assert R(home) and "home" in R(home), R(home)
assert R(home.upper()), "home compare is case-insensitive on Windows"
assert R(os.path.dirname(home)) and "profiles" in R(os.path.dirname(home)), R(os.path.dirname(home))
assert R(os.path.join(home, "proj")) is None, "a project under the home is allowed"
assert R(project) is None, project
print("windows_sim: workspace refusal ok")

# --- codex discovery falls back to the installer's location -------------------------
bridge.shutil.which = lambda *a, **k: None
exe = bridge.find_codex()
assert exe and exe.lower().endswith("codex.exe"), exe
print("windows_sim: find_codex fallback ok")

# --- the mode codex would apply, from a config/read payload --------------------------
f = bridge.windows_sandbox_mode_from_config
assert f({"windows": {"sandbox": "unelevated"}}) == "unelevated"
assert f({"windows": {"sandbox": "elevated"}, "features": {"experimental_windows_sandbox": True}}) == "elevated"
assert f({"features": {"experimental_windows_sandbox": True}}) == "unelevated", "legacy key counts"
assert f({"features": {"elevated_windows_sandbox": True}}) == "elevated"
assert f({"windows": {"sandbox": None}}) is None and f({}) is None and f(None) is None
assert f({}, [{"config": {"windows": {"sandbox": "unelevated"}}}]) == "unelevated", "raw layers as fallback"
assert f({}, [{"config": {"windows": {"sandbox": "unelevated"}}, "disabledReason": "off"}]) is None
print("windows_sim: sandbox mode resolution ok")

# --- the gate at spawn: readiness AND a live mode ------------------------------------
app = bridge.AppServer()
answers = {"windowsSandbox/readiness": {"status": "ready"},
           "config/read": {"config": {"windows": {"sandbox": "unelevated"}}, "layers": []}}
app.request = lambda m, p, timeout=30: answers[m]
app._require_windows_sandbox()
assert app.windows_sandbox == "ready"
answers["windowsSandbox/readiness"] = {"status": "notConfigured"}
try:
    app._require_windows_sandbox(); raise SystemExit("readiness notConfigured must refuse")
except ValueError as e:
    assert 'sandbox = "unelevated"' in str(e) and "notConfigured" in str(e), e
answers["windowsSandbox/readiness"] = {"status": "ready"}
answers["config/read"] = {"config": {}, "layers": []}
try:
    app._require_windows_sandbox(); raise SystemExit("ready without a mode must refuse")
except ValueError as e:
    assert "notConfigured" in str(e) and app.windows_sandbox == "notConfigured", (e, app.windows_sandbox)
print("windows_sim: spawn gate ok")

# --- the gate before every thread: fresh mode, refusal drops the child ---------------
dropped = []
bridge.APP.shutdown = lambda *a, **k: dropped.append(1)
bridge.APP.windows_sandbox_mode = lambda: None
try:
    bridge.windows_gate(); raise SystemExit("a vanished mode must refuse")
except ValueError as e:
    assert "notConfigured" in str(e) and dropped == [1], (e, dropped)
bridge.APP.windows_sandbox_mode = lambda: "elevated"
assert bridge.windows_gate() == "elevated"
print("windows_sim: per-thread gate ok")

# --- thread/start carries the pin; resume goes through the gate ----------------------
sent = []
def fake_request(method, params=None, timeout=30):
    sent.append((method, params))
    return {"thread/start": {"thread": {"id": "t-new"}}, "turn/start": {"turn": {"id": "u1"}}}.get(method, {})
bridge.APP.request = fake_request
bridge.APP.ensure = lambda: None
bridge.APP.windows_sandbox_mode = lambda: "unelevated"
out = bridge.codex_submit({"prompt": "hi", "model": "luna-medium", "cwd": project})
assert out["thread"] == "t-new", out
start = next(p for m, p in sent if m == "thread/start")
assert start.get("config") == {"windows.sandbox": "unelevated"}, start
print("windows_sim: thread/start pin ok")

# --- a follow-up turn that moves the thread passes the gate too ------------------------
# The gate reads config/read on EVERY start and resume, so any fake app-server a test
# hands codex_submit must answer it — tests/smoke.py once did not, and passed only on
# POSIX, where the gate is skipped. Here the real gate runs against a fake that does.
bridge.APP.windows_sandbox_mode = bridge.AppServer.windows_sandbox_mode.__get__(bridge.APP)
sent.clear()
def gated_request(method, params=None, timeout=30):
    sent.append((method, params))
    return {"config/read": {"config": {"windows": {"sandbox": "unelevated"}}, "layers": []},
            "thread/start": {"thread": {"id": "t-moved"}},
            "turn/start": {"turn": {"id": f"u{len(sent)}"}}}[method]
bridge.APP.request = gated_request
scratch_dir = os.path.join(project, "scratch-stand-in")
os.makedirs(scratch_dir.replace("\\", "/"), exist_ok=True)
bridge.new_scratch = lambda: scratch_dir
first = bridge.codex_submit({"prompt": "research it", "model": "sol-high"})
assert first["workspace"] == "scratch" and first["cwd"] == scratch_dir, first
bridge.APP.threads["t-moved"]["state"] = "completed"
second = bridge.codex_submit({"prompt": "save it", "model": "terra-medium", "thread": "t-moved", "cwd": project})
assert second["workspace"] == "project", second
methods = [m for m, _ in sent]
assert methods.count("config/read") == 1 and methods.index("config/read") < methods.index("thread/start"), methods
assert bridge._provenance(bridge.APP.threads["t-moved"])["workspace"] == "project"
bridge.APP.threads.clear()
print("windows_sim: moved thread passes the gate and reports project")

# --- install() tells Windows users the route that works ------------------------------
# Windows has no .mcpb file association, so "double-click it" leaves the user with an
# Explorer prompt; the instruction has to be the Settings route, and only that.
mcpb = os.path.join(home, ".claude-codex-bridge", "claude-codex-bridge.mcpb")
msg = bridge.open_instructions(mcpb)
assert "Settings" in msg and "Install Extension" in msg and mcpb in msg, msg
assert "double-click" not in msg, msg

# --- codex_check on Windows: ready means codex present AND its sandbox ready ----------
bridge.find_codex = lambda: r"C:\x\codex.exe"
bridge.subprocess.run = lambda *a, **k: type("R", (), {"stdout": "codex-cli 0.153.3", "stderr": ""})()
def failing_ensure():
    bridge.APP.windows_sandbox = "notConfigured"
    raise ValueError(bridge.WINDOWS_SANDBOX_NOT_READY.format(status="notConfigured"))
bridge.APP.ensure = failing_ensure
info = bridge.codex_check({})
assert info["ready"] is False and info["codex_on_path"] is True, info
assert info["windows_sandbox"] == "notConfigured" and "unelevated" in info["windows_sandbox_fix"], info
bridge.APP.windows_sandbox = "ready"                # a stale "ready" from an earlier child...
def dead_ensure():
    bridge.APP.windows_sandbox = None               # ...is reset by the next spawn attempt...
    raise bridge.CodexError({"message": "connection closed during initialize"})
bridge.APP.ensure = dead_ensure
info = bridge.codex_check({})
assert info["ready"] is False and info["windows_sandbox"] == "unknown", info   # ...so a failed restart never reports ready
bridge.APP.ensure = lambda: setattr(bridge.APP, "windows_sandbox", "ready")
info = bridge.codex_check({})
assert info["ready"] is True and info["windows_sandbox"] == "ready" and "windows_sandbox_fix" not in info, info
print("windows_sim: codex_check ok")

# --- a resumed thread in a refused cwd cannot be steered or given a new turn ----------
bridge.APP.ensure = lambda: None
for state, turn in (("completed", None), ("running", "u-live")):
    st = bridge._new_thread_state("t-resumed", home, "write", "luna-medium")
    st.update({"resumed": True, "gen": bridge.APP.gen, "state": state, "turn_id": turn})
    bridge.APP.threads["t-resumed"] = st
    try:
        bridge.codex_submit({"prompt": "hi", "model": "luna-medium", "thread": "t-resumed"})
        raise SystemExit(f"resumed thread in {home} must be refused ({state})")
    except ValueError as e:
        assert "resumed thread t-resumed works in" in str(e) and "home" in str(e), e
print("windows_sim: resumed-cwd rule ok")

shutil.rmtree(tmp.replace("\\", "/"), ignore_errors=True)
print("windows_sim: all passed")
