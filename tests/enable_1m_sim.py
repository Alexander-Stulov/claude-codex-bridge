#!/usr/bin/env python3
"""The 1M-context script against a stand-in codex — no codex binary, no network, any OS.

scripts/enable-1m-context.sh (and the .ps1 edition, whenever a PowerShell is on PATH)
run against a scratch CODEX_HOME and a fake `codex` that behaves the way the real CLI
was observed to on 0.153.4:

  * `debug models` fetches the catalog into models_cache.json only while config.toml
    has no model_catalog_json. With the override installed it renders the override
    file and leaves the cache alone; logged out it prints the bundled catalog and
    writes no cache; a CLI too old for the subcommand exits 2.
  * `exec` writes a rollout whose model_context_window is 95% of the loaded catalog's
    context_window for that model, or codex's 258400 fallback for a slug the loaded
    catalog does not carry.

The breaks the scenarios catch: a script that copies the cache without lifting the
override first can never see a model that appeared after the override went in. That
is exactly how gpt-6-astra stayed at 258400 while the script kept printing OK. And a
script whose RAISE list predates a model copies it into the catalog but leaves it at
the stock cap, which is where GPT-6.1 Sol, GPT-6 Sol and GPT-6 Luna were left.
"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SH = os.path.join(HERE, "..", "scripts", "enable-1m-context.sh")
PS1 = os.path.join(HERE, "..", "scripts", "enable-1m-context.ps1")
WIN = sys.platform == "win32"
TARGET = 1050000

# --- fixtures: the shape codex writes, trimmed to what the script reads or rewrites ----
def model(slug, ctx=272000, cap=872000, vis="list"):
    return {"slug": slug, "display_name": slug, "context_window": ctx, "max_context_window": cap,
            "visibility": vis, "priority": 1, "supported_reasoning_levels": ["medium", "high"],
            "input_modalities": ["text"]}

GPT56 = [model("gpt-5.6-sol"), model("gpt-5.6-terra"), model("gpt-5.6-luna")]
GPT6 = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]  # codex 0.159.0's order
OTHERS = [model("gpt-5.5", cap=272000), model("codex-auto-review", vis="hide")]
STALE = GPT56 + OTHERS                                         # the cache from before astra
SEP6 = [model("gpt-6-astra"), model("gpt-reserve", vis="hide")] + GPT56 + OTHERS   # astra, no 6-sol/luna/6.1
FRESH = [model(s) for s in GPT6] + [model("gpt-reserve", vis="hide")] + GPT56 + OTHERS
BUNDLED = FRESH + [model("gpt-daybreak-blue-latest")]         # what a logged-out CLI prints
OLD_RAISE = ("gpt-6-astra", "gpt-5.6-")                        # what the script raised before GPT-6 Sol

FAKE = r'''
import datetime, json, os, re, sys, uuid
home = os.environ["CODEX_HOME"]
beh = json.load(open(os.path.join(home, "fake-codex.json")))
cfg = os.path.join(home, "config.toml")
cache = os.path.join(home, "models_cache.json")

def override():
    if not os.path.exists(cfg):
        return None
    for line in open(cfg):
        m = re.match(r'\s*model_catalog_json\s*=\s*"([^"]*)"', line)
        if m:
            return m.group(1)
    return None

def loaded_models():                       # what codex would serve a turn from
    ov = override()
    if ov:
        return json.load(open(ov))["models"]
    if os.path.exists(cache):
        return json.load(open(cache))["models"]
    return beh["bundled"]

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("codex-cli 0.153.4"); sys.exit(0)
if argv[:2] == ["debug", "models"]:
    if beh.get("no_debug_models"):
        sys.stderr.write("error: unrecognized subcommand 'models'\n"); sys.exit(2)
    ov = override()
    if ov:                                 # override installed: render it, never fetch
        sys.stdout.write(open(ov).read()); sys.exit(0)
    if beh["remote"] is None:              # logged out: bundled catalog, no cache written
        print(json.dumps({"models": beh["bundled"]})); sys.exit(0)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    json.dump({"fetched_at": now, "etag": 'W/"fake"', "client_version": "0.153.4",
               "models": beh["remote"]}, open(cache, "w"), indent=1)
    print(json.dumps({"models": beh["remote"]})); sys.exit(0)
if argv[:1] == ["exec"]:
    slug = argv[argv.index("--model") + 1]
    ctx = next((m["context_window"] for m in loaded_models() if m["slug"] == slug), 272000)
    tid = str(uuid.uuid4())
    d = os.path.join(home, "sessions", "2026", "09", "06")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "rollout-2026-09-06T12-00-00-%s.jsonl" % tid), "w") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": tid}}, separators=(",", ":")) + "\n")
        f.write(json.dumps({"type": "turn_context", "payload": {"model": slug, "model_context_window": ctx * 95 // 100}},
                           separators=(",", ":")) + "\n")
    print(json.dumps({"type": "thread.started", "thread_id": tid}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}))
    sys.exit(0)
sys.stderr.write("fake codex: unexpected argv %r\n" % (argv,)); sys.exit(2)
'''


def make_fake_bin():
    d = tempfile.mkdtemp(prefix="fake-codex-")
    src = os.path.join(d, "codex_fake.py")
    open(src, "w").write(FAKE)
    if WIN:
        open(os.path.join(d, "codex.cmd"), "w").write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, src))
    else:
        p = os.path.join(d, "codex")
        open(p, "w").write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, src))
        os.chmod(p, 0o755)
    return d


FAKE_BIN = make_fake_bin()


def write_home(cache, override, remote, no_debug_models=False, prior=STALE):
    home = tempfile.mkdtemp(prefix="codex-home-")
    catalog = os.path.join(home, "catalog-1m.json")
    lines = ['model = "gpt-5.6-sol"', 'model_reasoning_effort = "high"', ""]
    if override:                           # a previous run's output, built from `prior`
        lines.append('model_catalog_json = "%s"' % catalog.replace(os.sep, "/"))
        raised = [dict(m, context_window=TARGET, max_context_window=TARGET)
                  if m["slug"].startswith(OLD_RAISE) else m for m in prior]
        json.dump({"models": raised}, open(catalog, "w"), indent=1)
    lines += ['[plugins."x"]', "enabled = true", ""]
    open(os.path.join(home, "config.toml"), "w").write("\n".join(lines))
    if cache is not None:
        json.dump({"fetched_at": "2026-08-18T11:58:22.151216Z", "etag": 'W/"old"',
                   "client_version": "0.147.0", "models": cache},
                  open(os.path.join(home, "models_cache.json"), "w"), indent=1)
    json.dump({"remote": remote, "bundled": BUNDLED, "no_debug_models": no_debug_models},
              open(os.path.join(home, "fake-codex.json"), "w"))
    return home


def run(cmd, home):
    env = dict(os.environ, CODEX_HOME=home)
    if WIN:
        # Find-Codex prefers any codex.exe over a .cmd, so hide a real install entirely.
        env["PATH"] = FAKE_BIN + os.pathsep + os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        env["LOCALAPPDATA"] = home
    else:
        env["PATH"] = FAKE_BIN + os.pathsep + os.environ.get("PATH", "")
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)


def catalog_of(home):
    return {m["slug"]: m for m in json.load(open(os.path.join(home, "catalog-1m.json")))["models"]}


def warnings(r):
    return [l for l in (r.stdout + r.stderr).splitlines() if l.startswith("WARNING")]


def transcript(r):
    return "\n--- stdout ---\n%s\n--- stderr ---\n%s" % (r.stdout, r.stderr)


def check_config(home, kind):
    catalog = os.path.join(home, "catalog-1m.json").replace(os.sep, "/")
    lines = open(os.path.join(home, "config.toml")).read().splitlines()
    hits = [i for i, l in enumerate(lines) if l.lstrip().startswith("model_catalog_json")]
    assert len(hits) == 1, "%s: expected exactly one override line, got %r in %r" % (kind, hits, lines)
    assert lines[hits[0]] == 'model_catalog_json = "%s"' % catalog, lines[hits[0]]
    first_table = next(i for i, l in enumerate(lines) if l.lstrip().startswith("["))
    assert hits[0] < first_table, "a bare key after the first [table] belongs to that table: %r" % lines
    for keep in ('model = "gpt-5.6-sol"', 'model_reasoning_effort = "high"', '[plugins."x"]', "enabled = true"):
        assert keep in lines, "%s: config line lost: %r" % (kind, keep)


# --- A: the override is installed, the cache predates astra, the remote has it now -------
def scenario_installed_override_sees_new_model(kind, cmd):
    home = write_home(cache=STALE, override=True, remote=FRESH)
    r = run(cmd, home)
    assert r.returncode == 0, kind + transcript(r)
    by = catalog_of(home)
    assert "gpt-6-astra" in by, (
        "%s: astra missing from the rebuilt catalog: the cache was copied without being "
        "refreshed first, so a model that appeared after the override went in is invisible"
        % kind + transcript(r))
    for s in GPT6 + ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]:
        assert (by[s]["context_window"], by[s]["max_context_window"]) == (TARGET, TARGET), by[s]
    stock = {m["slug"]: m for m in FRESH}
    for s in ("gpt-5.5", "codex-auto-review", "gpt-reserve"):
        assert by[s] == stock[s], "only the RAISE models move: %r" % by[s]
    assert "7 raised" in r.stdout and "gpt-6-astra" in r.stdout, transcript(r)
    assert "on gpt-6-astra" in r.stdout, "the live check must run on the flagship once it is raised" + transcript(r)
    assert "OK" in r.stdout and "997500" in r.stdout, transcript(r)
    assert not warnings(r), warnings(r)
    check_config(home, kind)


# --- B: the account's catalog has no astra: say so, raise the rest ----------------------
def scenario_missing_family_is_called_out(kind, cmd):
    home = write_home(cache=STALE, override=True, remote=[m for m in FRESH if m["slug"] != "gpt-6-astra"])
    r = run(cmd, home)
    assert r.returncode == 0, kind + transcript(r)
    by = catalog_of(home)
    assert "gpt-6-astra" not in by and by["gpt-5.6-sol"]["context_window"] == TARGET, by.keys()
    assert "6 raised" in r.stdout and "on gpt-6.1-sol" in r.stdout, (
        "%s: without astra the live check moves to the next raised model in RAISE order" % kind + transcript(r))
    w = warnings(r)
    assert any("gpt-6-astra" in l for l in w), (
        "%s: a RAISE family that matched nothing must be called out, not silently skipped" % kind + transcript(r))
    assert len(w) == 1, "%s: only the missing model is called out: %r" % (kind, w)
    check_config(home, kind)


# --- C: first run, nothing cached yet: the script fetches the catalog itself ------------
def scenario_first_run_needs_no_prior_codex_run(kind, cmd):
    home = write_home(cache=None, override=False, remote=FRESH)
    r = run(cmd, home)
    assert r.returncode == 0, "%s: a first run must not demand a prior codex run" % kind + transcript(r)
    by = catalog_of(home)
    for s in GPT6 + ["gpt-5.6-luna"]:
        assert by[s]["context_window"] == TARGET, by[s]
    check_config(home, kind)


# --- D: a CLI without `debug models`: fall back to the cache, and say it is unrefreshed --
def scenario_old_cli_falls_back_to_cache(kind, cmd):
    home = write_home(cache=STALE, override=True, remote=FRESH, no_debug_models=True)
    r = run(cmd, home)
    assert r.returncode == 0, "%s: the old behaviour must survive a CLI without the subcommand" % kind + transcript(r)
    by = catalog_of(home)
    assert "gpt-6-astra" not in by and by["gpt-5.6-terra"]["context_window"] == TARGET, by.keys()
    w = warnings(r)
    assert any("refresh" in l.lower() for l in w), "%s: an unrefreshed cache must be reported" % kind + transcript(r)
    for s in GPT6:
        assert any(s + "*" in l for l in w), "%s: %s missing from the cache must be called out" % (kind, s) + transcript(r)
    assert "on gpt-5.6-sol" in r.stdout, transcript(r)
    check_config(home, kind)


# --- F: the catalog from before GPT-6 Sol: the new models appear, and at 1.05M ----------
# The report this covers: an override built on 2026-09-06 (astra and the GPT-5.6 seats
# raised) kept GPT-6.1 Sol, GPT-6 Sol and GPT-6 Luna out of codex's model list, and a
# script that only knew astra and GPT-5.6 copied them in at the stock 272000 cap.
def scenario_new_gpt6_models_listed_and_raised(kind, cmd):
    home = write_home(cache=SEP6, override=True, remote=FRESH, prior=SEP6)
    before = catalog_of(home)
    assert "gpt-6-sol" not in before and before["gpt-6-astra"]["context_window"] == TARGET, before.keys()
    r = run(cmd, home)
    assert r.returncode == 0, kind + transcript(r)
    by = catalog_of(home)
    for s in ("gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"):
        assert s in by, "%s: %s missing from the rebuilt catalog, so codex never lists it" % (kind, s) + transcript(r)
        assert (by[s]["context_window"], by[s]["max_context_window"]) == (TARGET, TARGET), (
            "%s: %s listed but left at the stock cap: %r" % (kind, s, by[s]) + transcript(r))
        assert s in r.stdout, "%s: %s must be in the raised list the script prints" % (kind, s) + transcript(r)
    assert [m["slug"] for m in json.load(open(os.path.join(home, "catalog-1m.json")))["models"]] == \
        [m["slug"] for m in FRESH], "the catalog keeps codex's model order (its picker follows it)"
    assert "7 raised" in r.stdout and "on gpt-6-astra" in r.stdout, transcript(r)
    assert not warnings(r), warnings(r)
    check_config(home, kind)


# --- E: no cache and no fetch (logged out): fail, and put the override back --------------
def scenario_failure_restores_config(kind, cmd):
    home = write_home(cache=None, override=True, remote=None)
    cfg, cat = os.path.join(home, "config.toml"), os.path.join(home, "catalog-1m.json")
    before_cfg, before_cat = open(cfg).read(), open(cat).read()
    r = run(cmd, home)
    assert r.returncode != 0, "%s: nothing trustworthy to build from must not pass" % kind + transcript(r)
    assert open(cfg).read() == before_cfg, "%s: the override must be put back when the run fails after lifting it" % kind + transcript(r)
    assert open(cat).read() == before_cat, "%s: the previous catalog must survive a failed run" % kind


SCENARIOS = [
    ("installed override sees a new model", scenario_installed_override_sees_new_model),
    ("missing family is called out", scenario_missing_family_is_called_out),
    ("first run needs no prior codex run", scenario_first_run_needs_no_prior_codex_run),
    ("old cli falls back to the cache", scenario_old_cli_falls_back_to_cache),
    ("failure restores config", scenario_failure_restores_config),
    ("new GPT-6 models listed and raised", scenario_new_gpt6_models_listed_and_raised),
]


def runners():
    out = []
    if not WIN and shutil.which("bash"):
        out.append(("sh", ["bash", SH]))
    ps = shutil.which("pwsh") or (shutil.which("powershell") if WIN else None)
    if ps:
        out.append(("ps1", [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1]))
    return out


if __name__ == "__main__":
    ran = runners()
    assert ran, "neither bash nor a PowerShell on PATH: nothing to test"
    failed = []
    for kind, cmd in ran:
        for name, fn in SCENARIOS:
            try:
                fn(kind, cmd)
                print("enable_1m_sim: %s: %s ok" % (kind, name))
            except AssertionError as e:
                failed.append((kind, name))
                print("enable_1m_sim: %s: %s FAILED\n%s" % (kind, name, e))
    shutil.rmtree(FAKE_BIN, ignore_errors=True)
    if failed:
        sys.exit("enable_1m_sim: %d scenario(s) failed: %s" % (len(failed), failed))
    print("enable_1m_sim: all ok (%s)" % ", ".join(k for k, _ in ran))
