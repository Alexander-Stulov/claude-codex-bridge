#!/usr/bin/env python3
"""claude-codex-bridge — run OpenAI codex as an interactive subagent from any Claude surface.

Claude sessions often run in sandboxes (Cowork VM, Claude Code sandbox) where the
codex CLI and its auth are unreachable. This single file bridges the gap on the
machine where codex lives.

Transport: one supervised `codex app-server` child, spoken to over bidirectional
JSON-RPC (newline-delimited JSON, no "jsonrpc" member — that is the app-server
wire format). Sessions are real codex threads: they hold context between calls,
accept follow-up turns, can be steered mid-turn, and survive a bridge restart via
thread/resume.

Exposed as eight MCP tools:
  codex_check      readiness, models, live threads
  codex_capabilities  what codex can do here: plugins and their $skills, MCP servers, apps
  codex_submit     new thread or next turn (or steer a running one); per-turn model,
                   mode, cwd and JSON output schema
  codex_fork       copy a thread's history to a new id: branch it, or continue a thread
                   another process has open
  codex_poll       snapshot: state, what it is doing now, pending approvals, final output;
                   a thread this bridge is not running is read without taking it
  codex_approve    resolve an approval codex is waiting on, then it continues
  codex_interrupt  stop the active turn
  codex_compact    summarise the thread's history now, at a boundary you choose

Posture: full tool surface, live web search, network in both modes. The sandbox
envelope (mode + cwd) is what keeps work contained, so in-scope reads, writes and
commands never generate approval traffic. Only sandbox escapes, execpolicy
`prompt` rules and MCP elicitations (a tool server asking for a permission or a
form) escalate — and those route to the caller (approvalsReviewer=user), never to
a silent auto-accept or auto-decline.

Install (idempotent, re-run after updates):  python3 server.py --install
Then open the built .mcpb once via the Desktop UI (the GUI step that registers the
extension — Cowork only sees REGISTERED extensions), and restart Claude Desktop.

No configuration. A thread works in whatever cwd the caller names — any existing
directory, so a brand-new project or a worktree parked anywhere works the moment
it exists — or, when no cwd is given, in a private scratch workspace under
~/.claude-codex-bridge/scratch/. The only refusals are whole-machine targets
(filesystem root, your home directory, system directories); real containment is
the codex sandbox, which confines writes to cwd. On Windows that sandbox is off
until config.toml enables it ([windows] sandbox = "unelevated"), and codex would
otherwise run every approved command with no filesystem boundary — so the bridge
refuses to dispatch until codex reports the sandbox ready. Provenance is stamped
by the bridge from run parameters — never from the model's self-report. Logs go
to stderr; stdout carries only newline-delimited JSON-RPC.
"""
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = "2024-11-05"

INSTRUCTIONS = """Dispatch OpenAI codex as an interactive subagent on the user's machine.

SESSIONS: codex_submit with no thread starts one and returns {thread, turn}; pass that thread back to add a turn (follow-up input, a correction, a new question) with full prior context. If the thread is mid-turn, the input steers the running turn instead.

DURABLE: the thread id is the handle to the work. codex stores threads, not this bridge, so keep the id and pick the same thread up tomorrow or next week - codex_submit re-attaches it automatically and the answer carries resumed: true, and codex_poll reads its latest saved results without taking it (read_only: true). One process at a time can write a thread: when another process has it open - another Claude Desktop connection, the Codex app, codex in a terminal, a script - codex_submit and codex_compact answer reason held_elsewhere, and codex_fork copies its history to a new id you can continue at once. Record the thread id with whatever the work belongs to; it is the only thing needed to continue.

LONG THREADS: a thread is not capped. When it fills up codex summarises its own history and carries on under the same id, so keep one thread for one line of work instead of splitting it to stay small: coherent, closely-coupled work belongs in one thread, and what matters survives compaction. Split for parallelism, not for length - work that divides cleanly runs faster and cheaper as several threads, whatever the model. Scouts should finish and return their findings inside the runway, so the detail reaches the caller verbatim rather than through a summary. activity.context.tokens_before_compaction is the room left before that happens - a runway, not a limit. While it is large, just keep going. As it gets small, choose deliberately:
- carry on and let codex compact (fine when the work is exploratory and only conclusions matter);
- have codex write findings to a file FIRST, then continue, so nothing important depends on recall;
- codex_compact(thread) between turns to take the summary at a clean point of your choosing rather than mid-task;
- start a fresh thread only when the next piece of work is genuinely a different line of enquiry, not merely to stay under a number.
activity.compactions counts summaries already taken; after one, early turns exist only as a summary, so restate any exact value that still matters.

POLL, DON'T BLOCK: after codex_submit, call codex_poll(thread) every 20-30s and tell the user what is happening from the activity snapshot: activity.now, counts of commands run and files changed, answer_chars while a long answer streams, sub_agents when it delegates, and quiet_seconds - seconds since codex last said anything, which is how you tell a long run from a stuck one (a working thread stays near zero). Polls are idempotent snapshots - nothing to stitch together, nothing lost if one is missed. State is running | awaiting_approval | completed | failed; the complete output arrives once, whole, when state is completed.

BRIEFS: the prompt is all codex sees for a new thread - state the goal, constraints, exact repo-relative paths, and what the answer should contain. Follow-up turns keep the thread's context, so they can be short.

SIZE THE DELIVERABLE: ask for the answer as the turn's OUTPUT, not as a report file. Output is capped by the model's own output limit, comes back whole through codex_poll, and its shape can be ENFORCED with output_schema (maxItems, maxLength) instead of merely requested. A file has none of that: it grows with the surface until nobody can read it, and this bridge cannot even see it. Write files only when the file IS the work product - source changes, a document that has to live in the repo. For a survey or a set of findings, the answer is the answer.

Then bound it, because a survey still grows with the surface unless the brief says otherwise. Give a hard cap (items or lines) and require an index. The cap is the part that works: budgeting one row per item still scales linearly, so a 5000-item sweep overruns any readable size anyway. So also say what happens when the items do NOT fit - counts and patterns plus the exceptions worth naming, never a truncated enumeration. A complete list nobody can read is a failed deliverable, not a thorough one.

MODELS by task weight: luna-medium/high = scouting and mechanical extraction - scout here whatever the follow-up is, then hand the findings to the model that does the work. Shape it as a pyramid: many cheap scouts, one per area, each finishing inside its runway and returning findings verbatim; fewer, stronger threads then process those results, and so on up; terra-medium/high = everyday analysis, drafting, second opinions; sol-high/xhigh = complex implementation and debugging; astra-high/xhigh = GPT-6, the heavyweight for hard or ambiguous work, long-context runs, terminal-heavy agentic work, and independent review. max rarely improves on xhigh - reserve it for a problem xhigh has already failed. ultra (sol/astra) is not more than max: each agent runs at xhigh and proactive sub-agent delegation switches on - faster only when the work splits into substantial independent parts; on small or tightly-coupled work it costs more, takes as long, and is no better - max wins there.

WHERE IT WORKS: cwd is the one location knob - any existing directory, normally the folder the calling session is already in. Nothing to register: a new project or a worktree parked anywhere works immediately. Reads still see the surrounding repo; writes are confined to cwd, so point it at the repo for repo-wide work or at a subdirectory to contain the blast radius. Omit it for work that needs no repo (research, reasoning, throwaway code) and the thread gets a private scratch workspace - the result says workspace: scratch. mode is write (default) or read. Point concurrent write threads at different cwd (e.g. separate worktrees) and they cannot collide. On Windows the bridge dispatches only while codex's own sandbox is enabled; codex_check reports windows_sandbox and the fix when it is not.

CAPABILITIES: codex arrives with plugins (skills), MCP servers and connected apps of its own, beyond files and shell. Before briefing work that might lean on one - research, documents, decks, spreadsheets, browsing, desktop control, an external service - call codex_capabilities: it lists every enabled plugin with the $skill mentions it contributes, every MCP server with its tools, and the connected apps; query narrows it. Invoke a skill by passing its name in codex_submit's skills (skills: ["deep-research-work:deep-research"], or just "deep-research" when that is unique) - the bridge injects the skill's instructions into the turn; a $mention typed into the prompt is not honoured through the app-server. An app is mentioned in the prompt as [$Name](app://id); MCP tools by name. Skills also fire implicitly when the brief matches their description.

PLUGINS: three worth knowing. deep-research (skills: ["deep-research"]) is OpenAI Deep Research inside codex - multi-pass web research with cited sources, the capability Cowork and Code threads lack natively. Through codex it is metered against the account's Codex/Work usage allowance rather than the Chat deep-research task quota (OpenAI help center, September 2026), and it is the most expensive thing a thread does - one run reads well over a million tokens - so spend it on questions that merit it, and run it on sol-high/xhigh or astra, never a scout. Ask for the report in chat - say no document, deck or site - with a Sources section; codex_poll returns it whole. When it is worth keeping, and it usually is, add a turn on the same thread with cwd set to the project and ask codex to save the report to a markdown file (docs/research/<topic>.md, say) - it writes it verbatim with every source, in about a minute on terra-medium; or write a condensed version yourself from the poll output. The bridge stores nothing. Its clarifying questions cannot reach you through this bridge yet, so tell it to state assumptions and proceed. Chrome is the user's real Google Chrome through the ChatGPT Chrome extension - logged-in sessions, open tabs, page content - and the easy way to work with web pages: codex itself prefers it over Computer Use for anything in a browser. Ask for the Chrome plugin by name and tell codex to call the cua_repl js tool directly - its first call is cua.createBrowserTab("chrome", url, {sessionName}) - because cua_repl's tools are kept out of codex's code-mode exec tool, and a model that goes looking for a chrome tool in there concludes the plugin is unreachable. Each new site raises an elicitation (tool access_browser_origin) that allow grants once and allow_class grants for good. Computer Use is native macOS app control through the Codex Computer Use app, and it runs through the same cua_repl js tool as Chrome (plugin unified-computer-use): ask for Computer Use by name and codex opens the app with cua.getApp; the first use of each app raises an elicitation (tool get_app_state, persist_modes session and always) that allow grants once, allow_always for the session, allow_class for good. A computer-use MCP server listed with no tools is a legacy config.toml entry, not the capability - ignore it. Documents, presentations, spreadsheets, pdf, visualize, sites and the rest appear in codex_capabilities with their mentions.

NETWORK: codex has network access in both modes, always - web search, http, package installs, git remotes; there is nothing to enable or approve. Containment is the sandbox (writes confined to cwd), not the network.

JSON ANY TIME: pass output_schema on any turn - new or existing thread - and that turn's final message is constrained to it. Omit it for prose.

IMAGES: pass images on any turn - a list of file paths (absolute or relative to cwd) or http(s)/data URLs. Screenshots, mockups, diagrams, a rendering that looks wrong. A bad path is rejected before the turn starts.

APPROVALS: in-scope work never asks. When codex_poll returns awaiting_approval, codex hit the sandbox boundary or a suspicious-command rule; the request and the thread's declared scope come with it. Decide, then codex_approve(request_id, allow|allow_always|deny) and keep polling - allow_always covers that exact command again, and allow_class covers the whole command class when the request offers proposed_execpolicy_amendment (the one that stops a build loop re-prompting on every git add). allow_class writes an allow rule into codex's execpolicy: it outlives this session, and commands it matches run outside the sandbox from then on - grant it only for classes you would trust with the whole machine tomorrow. An MCP server codex is using can park the thread the same way, on an elicitation (kind: elicitation): a permission prompt from that server - a browser plugin asking to open a tab, say - or a short form. The request comes with server, message, requested_schema and persist_modes. codex_approve resolves it too: allow accepts, deny declines, allow_always accepts and has it remembered for the session (allow_class: permanently) when persist_modes offers that - a mode the request did not offer is never sent, and the answer says so. When requested_schema has fields, pass the answers in grant as an object keyed by field name; required fields are checked before anything is sent, so a refused grant can be retried.

RESULTS: completed carries output (text, or your schema's JSON) plus bridge-stamped provenance - trust that over anything the model says about itself. Errors come back verbatim, including schema rejections."""

SERVER_INFO = {"name": "codex", "version": "0.14.0"}

# Friendly slug -> (wire model, reasoning effort). One caller-facing knob; the
# app-server takes them as separate per-turn fields.
#
# astra is GPT-6 (wire slug gpt-6-astra, codex >= 0.153.1), codex's own default model,
# exposed from medium up through every effort the app-server advertises. `low` is
# deliberately absent: a scout at astra depth is the wrong tool, luna is the scout.
# The GPT-5.6 seats stay the curated subset they were.
#
# The effort names are codex's own, verbatim from `model/list`.supportedReasoningEfforts.
# Two of them are not what they look like: `ultra` never reaches the wire — codex sends
# xhigh and turns on proactive sub-agent delegation — and `max` is a single agent at
# wire effort max, so ultra is breadth, not depth.
# Getting one wrong is not caught here: thread/start accepts any string (an invented
# effort survives all the way to turn time, then fails against the API), so this table
# is the only thing standing between a caller and an opaque mid-turn error.
MODELS = {
    "astra-medium": ("gpt-6-astra", "medium"),
    "astra-high": ("gpt-6-astra", "high"),
    "astra-xhigh": ("gpt-6-astra", "xhigh"),
    "astra-max": ("gpt-6-astra", "max"),
    "astra-ultra": ("gpt-6-astra", "ultra"),
    "luna-medium": ("gpt-5.6-luna", "medium"),
    "luna-high": ("gpt-5.6-luna", "high"),
    "terra-medium": ("gpt-5.6-terra", "medium"),
    "terra-high": ("gpt-5.6-terra", "high"),
    "sol-high": ("gpt-5.6-sol", "high"),
    "sol-xhigh": ("gpt-5.6-sol", "xhigh"),
    "sol-ultra": ("gpt-5.6-sol", "ultra"),
}

MODES = ("read", "write")

# Server->client requests we park for the caller to rule on. Everything else gets
# an error response so the app-server never hangs waiting on us.
APPROVAL_KINDS = {
    "item/permissions/requestApproval": "permissions",
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "execCommandApproval": "command",
    "applyPatchApproval": "file_change",
    "mcpServer/elicitation/request": "elicitation",
}

# Keys codex puts in an elicitation's `_meta` when the elicitation is one of its own
# MCP tool-call approvals (codex-rs/protocol/src/mcp_approval_meta.rs). `_meta` is
# untyped on the wire, so the schema fixture cannot vouch for these names — they are
# pinned here verbatim instead. A request lists the persistence it offers under
# `persist` (one mode or a list); the reply names the one chosen.
ELICITATION_PERSIST_KEY = "persist"
ELICITATION_PERSIST_SESSION = "session"    # remembered for the rest of the session
ELICITATION_PERSIST_ALWAYS = "always"      # written to codex's MCP policy: outlives the session
ELICITATION_TOOL_NAME_KEY = "tool_name"

# Storage belongs to codex (which persists threads) and to the caller (which has the
# context window). The bridge accumulates nothing: progress is a SNAPSHOT of what is
# happening now, so polls are idempotent and no partial text is ever stitched together.
# `output` arrives whole, once, and is never truncated — under an output_schema
# trimming it would produce invalid JSON, corrupting rather than merely lossy.
TERMINAL_TTL_SECONDS = 900         # local cache only: a pruned thread is recovered from codex on demand


def log(msg):
    print(f"[codex-bridge] {msg}", file=sys.stderr, flush=True)


SCRATCH_ROOT = os.path.join(os.path.expanduser("~"), ".claude-codex-bridge", "scratch")
SCRATCH_TTL_DAYS = 7

# The only real hazard is handing the agent something enormous. This is a generic
# extension with no knowledge of anyone's machine, so it registers nothing and
# gates on danger rather than on a list that would have to track your filesystem.
SYSTEM_DIRS = frozenset({
    "/", "/Users", "/home", "/etc", "/usr", "/bin", "/sbin", "/var", "/opt", "/dev",
    "/tmp", "/System", "/Library", "/Applications", "/private", "/Volumes", "/mnt", "/srv",
})


def refuse_reason(path):
    """`path` is already a realpath. Compare against realpaths too: on macOS /etc,
    /var and /tmp are symlinks into /private, so matching the literal names alone
    would wave through exactly the directories this refuses. Windows names its
    system directories through the environment and compares paths case-insensitively,
    so both sides go through normcase there."""
    home = os.path.realpath(os.path.expanduser("~"))
    denied = {home: "that is your entire home directory",
              os.path.join(home, "Library"): "that is your whole ~/Library"}
    for d in SYSTEM_DIRS:
        denied.setdefault(d, f"{d} is a system directory")
        try:
            denied.setdefault(os.path.realpath(d), f"{d} is a system directory")
        except OSError:
            pass
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(path)
        if drive and tail in ("\\", "/", ""):
            return f"{drive}\\ is a whole drive"
        # /Users above resolves against the current drive, so on the system drive it IS
        # the profiles container: assign, not setdefault, or that entry wins with the
        # wrong reason. The real container is wherever this user's profile lives.
        denied[os.path.dirname(home)] = "that is the profiles directory — every user's home"
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
            val = os.environ.get(var)
            if val:
                denied.setdefault(os.path.realpath(val), f"%{var}% is a system directory")
        denied = {os.path.normcase(k): v for k, v in denied.items()}
        path = os.path.normcase(path)
    return denied.get(path)


def windows_gate():
    """Windows only: the sandbox mode codex will apply to the next thread, fresh from
    disk, or a refusal. Runs before every thread/start and thread/resume; a refusal
    also drops the child so the next call re-gates from a clean start."""
    if sys.platform != "win32":
        return None
    mode = APP.windows_sandbox_mode()
    if mode is None:
        APP.shutdown()
        raise ValueError(WINDOWS_SANDBOX_NOT_READY.format(status="notConfigured"))
    return mode


def find_codex():
    """PATH first. Claude Desktop hands the bridge the PATH it was launched with, so a
    codex installed after Desktop started is invisible there on Windows — fall back to
    the installer's fixed location before declaring it absent."""
    exe = shutil.which("codex")
    if not exe and sys.platform == "win32":
        default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "OpenAI",
                               "Codex", "bin", "codex.exe")
        if os.path.isfile(default):
            exe = default
    return exe


def windows_sandbox_mode_from_config(cfg, layers=()):
    """codex's resolve_windows_sandbox_mode, applied to a config/read payload: the
    `[windows] sandbox` value, else the legacy feature keys, from the effective config
    first and the raw layers as a fallback. "unelevated" | "elevated" | None."""
    def from_table(c):
        mode = ((c or {}).get("windows") or {}).get("sandbox")
        if mode in ("unelevated", "elevated"):
            return mode
        feats = (c or {}).get("features") or {}
        if feats.get("elevated_windows_sandbox") is True:
            return "elevated"
        if feats.get("experimental_windows_sandbox") is True or \
                feats.get("enable_experimental_windows_sandbox") is True:
            return "unelevated"
        return None
    mode = from_table(cfg)
    for layer in layers or ():
        if mode:
            break
        if not layer.get("disabledReason"):
            mode = from_table(layer.get("config"))
    return mode


WINDOWS_SANDBOX_NOT_READY = (
    "codex's Windows sandbox is {status}, so codex would run commands with no filesystem "
    "boundary - the bridge does not dispatch in that state. Enable it once in "
    "%USERPROFILE%\\.codex\\config.toml:\n\n[windows]\nsandbox = \"unelevated\"\n\n"
    "then retry - no restart needed. updateRequired means the elevated sandbox's setup is "
    "stale: run `codex` interactively once to re-provision it.")


def new_scratch():
    """A thread with no cwd still needs somewhere to stand. Give it a private
    workspace: research, reasoning and throwaway code all work, and nothing can
    reach a real project by accident."""
    os.makedirs(SCRATCH_ROOT, exist_ok=True)
    path = os.path.join(SCRATCH_ROOT, time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex())
    os.makedirs(path, exist_ok=True)
    return path


def reap_scratch():
    cutoff = time.time() - SCRATCH_TTL_DAYS * 86400
    try:
        for name in os.listdir(SCRATCH_ROOT):
            p = os.path.join(SCRATCH_ROOT, name)
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
    except FileNotFoundError:
        pass


def resolve_workspace(cwd):
    """Where the thread works. Any existing directory — no registration, so a new
    project or a worktree parked anywhere works the moment it exists. Omitted → a
    private scratch workspace. Returns (cwd, kind)."""
    if not cwd:
        scratch = new_scratch()
        return scratch, "scratch"
    real = os.path.realpath(os.path.expanduser(str(cwd)))
    if not os.path.isdir(real):
        raise ValueError(f"cwd '{cwd}' is not an existing directory — pass an absolute path, "
                         f"or omit cwd to work in a private scratch workspace")
    reason = refuse_reason(real)
    if reason:
        raise ValueError(f"refusing {real} as a workspace: {reason}. Point cwd at a project directory.")
    return real, "project"


def sandbox_policy(mode):
    """read and write BOTH keep network — a read-only research seat needs the web.
    Containment comes from cwd, not from crippling the agent."""
    if mode == "read":
        return {"type": "readOnly", "networkAccess": True}
    return {"type": "workspaceWrite", "networkAccess": True}


# codex compacts at 90% of a model's RAW context window, but the only figure on the
# wire is the effective window (raw x 95%, the headroom it reserves for prompts and
# output). So the threshold is derived: raw = window * 100/95, compact at raw * 9/10,
# i.e. window * 90/95. Exact for both real windows — 258400 -> 244800 and
# 997500 -> 945000 — and matching codex's own unit test (400000 raw -> 360000).
# If codex ever changes either percentage this silently drifts; the fixture-backed
# conformance test will not catch it, so re-check these two constants after an upgrade.
EFFECTIVE_WINDOW_PCT, AUTO_COMPACT_PCT = 95, 90


def compaction_runway(window, used):
    """How much room is left before codex summarises the history.

    A runway, deliberately not a fill gauge: "84% full" reads as an emergency and
    invites abandoning a thread that is fine, while "620k tokens before it compacts"
    is something a caller can actually act on — bank the results now, or keep going
    and let it compact.

    An UPPER bound, not a promise. codex uses min(config limit, raw x 90%), and a
    `model_auto_compact_token_limit` set in config.toml is invisible on the wire, so
    a caller who has set one will compact sooner than this says. Erring high is the
    safe direction: compaction is not a failure, so an early one costs nothing, while
    understating the runway would cause exactly the premature thread-splitting this
    number exists to prevent.
    """
    if not window or used is None:
        return None
    threshold = window * AUTO_COMPACT_PCT // EFFECTIVE_WINDOW_PCT
    return {"tokens_used": used, "tokens_before_compaction": max(0, threshold - used)}


IMAGE_URL_SCHEMES = ("http://", "https://", "data:")


def build_input(prompt, images, cwd, skills=None):
    """A turn's input is a list of typed items: any skills to inject, the text, then images.

    A URL and a file on disk are different wire types, so the caller passes plain
    strings and this decides. Bad paths are caught here — before the turn starts —
    because a turn that dies on a missing file wastes the whole round trip.
    """
    items = list(skills or []) + [{"type": "text", "text": str(prompt)}]
    for ref in images or []:
        ref = str(ref).strip()
        if not ref:
            continue
        if ref.startswith(IMAGE_URL_SCHEMES):
            items.append({"type": "image", "url": ref})
            continue
        path = ref if os.path.isabs(ref) else os.path.join(cwd, ref)
        path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise ValueError(f"image not found: {ref!r} (resolved to {path}). Pass an absolute "
                             f"path, a path relative to cwd, or an http(s)/data URL.")
        items.append({"type": "localImage", "path": path})
    return items


class CodexError(Exception):
    """An error from the app-server, surfaced verbatim (schema rejections included)."""

    def __init__(self, payload):
        self.payload = payload
        super().__init__(json.dumps(payload)[:400])


class ThreadHeldElsewhere(ValueError):
    """Another process has the thread open. codex lets one process write a thread at a
    time, so this is a refusal the caller can act on (fork it, or wait), not codex failing.
    A ValueError, so in-process callers that already catch refusals keep working."""
    reason = "held_elsewhere"


class AppServer:
    """Supervises one `codex app-server` child and speaks its bidirectional JSON-RPC.

    Message classification (the app-server omits the "jsonrpc" member):
        id + no method  -> response to a request we sent
        method + no id  -> event notification
        method + id     -> request FROM codex that we must answer
    """

    def __init__(self):
        self.proc = None
        self.gen = 0             # child generation: a restart invalidates the old reader
        self.lock = threading.RLock()
        # Separate from self.lock on purpose: the handshake below waits on a response
        # the reader thread must deliver, so it must not hold the message lock.
        self.start_lock = threading.Lock()
        # Writes get their own lock: a wedged child must block writers, never the
        # reader that would report the wedge.
        self.write_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.pending = {}        # our request id -> {"event": Event, "msg": dict|None}
        self.threads = {}        # thread_id -> thread state dict
        self.requests = {}       # approval request id -> {kind, params, thread, view}
        self.windows_sandbox = None   # last windowsSandbox/readiness status (Windows only)
        self.skill_catalog_cache = None   # (generation, [{name, path, enabled}]) — see skill_catalog()

    # ---- lifecycle -------------------------------------------------------
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        with self.start_lock:
            if self.alive():
                return
            exe = find_codex()
            if not exe:
                raise ValueError("codex is not on PATH on the host — install/authenticate codex, then retry"
                                 + (" (just installed it? restart Claude Desktop so it sees the new PATH)"
                                    if sys.platform == "win32" else ""))
            with self.lock:
                self.gen += 1
                gen = self.gen
                self.windows_sandbox = None      # readiness belongs to this child, not the last
                # UTF-8 on both pipes explicitly: Windows would otherwise decode codex's
                # UTF-8 with the ANSI code page and mangle or reject it.
                self.proc = subprocess.Popen(
                    [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
                self.pending.clear()
                self.requests.clear()
            threading.Thread(target=self._reader, args=(self.proc, gen), daemon=True).start()
            reap_scratch()
            log(f"app-server started (pid {self.proc.pid}, generation {gen})")
            try:
                self.request("initialize", {
                    "clientInfo": {"name": "claude-codex-bridge", "title": "Claude Codex Bridge",
                                   "version": SERVER_INFO["version"]},
                    "capabilities": {"experimentalApi": True},
                }, timeout=60)
                self.notify("initialized", None)
                if sys.platform == "win32":
                    self._require_windows_sandbox()
            except Exception:
                # Half-open child helps nobody: drop it so the next call retries a
                # complete handshake instead of talking to something uninitialized.
                # The Windows gate lands here on purpose: app-server reads config.toml
                # once at startup, so dropping the child is what lets the user's fix
                # take effect on the very next call.
                with self.lock:
                    if self.gen == gen and self.proc is not None:
                        try:
                            self.proc.terminate()
                        except OSError:
                            pass
                        self.proc = None
                raise

    def _require_windows_sandbox(self):
        """codex sandboxes on Windows only once config.toml opts in; disabled, it would
        run every approved command with no filesystem boundary. Two answers from codex
        itself, never a parse of config.toml here: readiness, which also covers the
        elevated sandbox's provisioning, and the mode read fresh from disk."""
        res = self.request("windowsSandbox/readiness", None, timeout=30)
        status = res.get("status") or "unknown"
        self.windows_sandbox = status
        if status != "ready":
            raise ValueError(WINDOWS_SANDBOX_NOT_READY.format(status=status))
        if self.windows_sandbox_mode() is None:
            self.windows_sandbox = "notConfigured"
            raise ValueError(WINDOWS_SANDBOX_NOT_READY.format(status="notConfigured"))

    def windows_sandbox_mode(self):
        """The sandbox mode codex would give a thread started NOW: "unelevated",
        "elevated" or None. config/read reloads config.toml on every call, unlike
        windowsSandbox/readiness, which answers from the config the child started with
        — and thread/start reloads from disk too. So this, asked before every thread
        the bridge starts or resumes, is what keeps the gate true for the child's whole
        life rather than its first second."""
        res = self.request("config/read", {"includeLayers": True}, timeout=30)
        return windows_sandbox_mode_from_config(res.get("config"), res.get("layers") or ())

    def shutdown(self, timeout=5):
        """Close the child's stdin — EOF is how app-server is told to wind down — and
        only terminate if it will not go. TerminateProcess on Windows skips every
        cleanup, so the polite path comes first there especially."""
        with self.start_lock:
            proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout)
        except Exception:  # noqa: BLE001 — best effort on the way out
            try:
                proc.terminate()
                proc.wait(3)
            except Exception:  # noqa: BLE001
                pass

    def _reader(self, proc, gen):
        """Everything here is gated on `gen`. A reader draining the tail of a dead
        child must not touch state a replacement child already owns — otherwise it
        fails the new generation's in-flight handshake and poisons its maps."""
        try:
            for line in proc.stdout:
                if gen != self.gen:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self._dispatch(msg)
                except Exception as e:  # noqa: BLE001 — a bad message must not kill the reader
                    log(f"reader error: {e}")
        finally:
            with self.lock:
                if gen != self.gen:
                    return          # a newer child owns the state now
                log(f"app-server stdout closed (generation {gen})")
                for st in self.threads.values():
                    if st["state"] in ("running", "awaiting_approval"):
                        st["state"] = "failed"
                        st["error"] = {"message": "app-server exited while the turn was active"}
                for slot in self.pending.values():
                    slot["event"].set()

    def _dispatch(self, msg):
        if "id" in msg and "method" not in msg:
            with self.lock:
                slot = self.pending.get(msg["id"])
                if slot:
                    slot["msg"] = msg
                    slot["event"].set()
            return
        if "method" in msg and "id" in msg:
            self._on_server_request(msg)
            return
        self._on_event(msg)

    # ---- rpc -------------------------------------------------------------
    def request(self, method, params, timeout=120):
        rid = next(self.ids)
        ev = threading.Event()
        with self.lock:
            self.pending[rid] = {"event": ev, "msg": None}
        payload = {"method": method, "id": rid}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
        except Exception:
            with self.lock:
                self.pending.pop(rid, None)   # never leave a slot nobody will answer
            raise
        if not ev.wait(timeout):
            with self.lock:
                self.pending.pop(rid, None)
            raise CodexError({"message": f"timed out after {timeout}s waiting for {method}"})
        with self.lock:
            msg = self.pending.pop(rid)["msg"]
        if msg is None:
            raise CodexError({"message": f"connection closed during {method}"})
        if "error" in msg:
            raise CodexError(msg["error"])
        return msg.get("result") or {}

    def notify(self, method, params):
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def respond(self, req_id, result):
        self._write({"id": req_id, "result": result})

    def _write(self, obj):
        with self.write_lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                raise CodexError({"message": "app-server is not running"})
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

    # ---- inbound ---------------------------------------------------------
    def _on_server_request(self, msg):
        method, rid = msg["method"], msg["id"]
        params = msg.get("params") or {}
        kind = APPROVAL_KINDS.get(method)
        if not kind:
            # Never leave codex hanging on something we do not implement.
            log(f"unhandled server request {method} — declining")
            self.respond(rid, {"error": {"message": f"{method} is not supported by this bridge"}})
            return
        tid = params.get("threadId")
        with self.lock:
            st = self.threads.get(tid)
            if st is None:
                # Nothing we can surface it on, and an unanswered request parks the
                # turn forever — decline rather than swallow it.
                log(f"{method} for unknown thread {tid} — declining")
                self.respond(rid, decline_response(kind))
                return
            # One canonical table. codex can hold SEVERAL approvals open on a thread,
            # so poll derives the list from here rather than keeping a second copy.
            view = {
                "request_id": rid,
                "kind": kind,
                "reason": params.get("reason"),
                "command": params.get("command"),
                "cwd": params.get("cwd"),
                "permissions": params.get("permissions"),
                "proposed_execpolicy_amendment": params.get("proposedExecpolicyAmendment"),
            }
            if kind == "elicitation":
                view.update(elicitation_view(params))     # what the MCP server is asking
            self.requests[rid] = {"kind": kind, "params": params, "thread": tid, "view": view}
            st["state"] = "awaiting_approval"
            self._note(st, now="waiting for your approval")

    def _on_event(self, msg):
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        tid = params.get("threadId")
        with self.lock:
            st = self.threads.get(tid)
            if st is None:
                return
            # Any notification about this thread is a sign of life. A single message can
            # stream for minutes with no item boundary and no command, so item events
            # alone would read as a stall; token usage and the per-token deltas keep
            # ticking throughout. Only a timestamp — nothing accumulates.
            st["changed_at"] = time.time()
            if method == "turn/started":
                st["turn_id"] = (params.get("turn") or {}).get("id") or st.get("turn_id")
                st["state"] = "running"
            elif method == "turn/completed":
                self._complete_turn(st, params.get("turn") or {})
            elif method in ("item/started", "item/completed"):
                self._push_item(st, params.get("item") or {}, method.endswith("completed"))
            elif method in ("item/agentMessage/delta", "item/reasoning/textDelta",
                            "item/reasoning/summaryTextDelta"):
                # Forward progress while a single message streams for minutes: how many
                # characters have landed, never the characters themselves. Two integers;
                # the answer itself still arrives whole, once, at the end.
                key = "answer_chars" if method == "item/agentMessage/delta" else "thinking_chars"
                st["activity"][key] = st["activity"].get(key, 0) + len(params.get("delta") or "")
            elif method == "thread/tokenUsage/updated":
                tu = params.get("tokenUsage") or {}
                usage = tu.get("total") or {}
                if usage:
                    st["usage"] = usage
                # `total` is cumulative over every model call in a turn and runs to
                # millions; context occupancy is the LAST call's total, which is what
                # codex's own status display uses.
                last = tu.get("last") or {}
                if last.get("totalTokens") is not None:
                    st["context_used"] = last["totalTokens"]
                if tu.get("modelContextWindow"):
                    st["context_window"] = tu["modelContextWindow"]
            elif method == "serverRequest/resolved":
                # Also emitted when lifecycle cleanup cancels a request, so drop it
                # without resurrecting a thread that already finished.
                self._forget_request(st, params.get("requestId"))
            elif method == "error":
                # The real notification (there is no thread/error or turn/failed — those
                # were removed upstream and this branch dispatched on them for months).
                # willRetry=true is codex retrying a stream; the turn is NOT over, so
                # report it without terminating. A fatal error still arrives as
                # turn/completed with status=failed, which _complete_turn owns.
                st["last_error"] = {"error": params.get("error"),
                                    "will_retry": bool(params.get("willRetry"))}
                self._note(st, now=("retrying after an error" if params.get("willRetry")
                                    else "error reported"))
            elif method == "thread/status/changed":
                flags = (params.get("status") or {}).get("activeFlags") or []
                st["active_flags"] = flags or None

    def _note(self, st, now=None, **counts):
        """Progress is a snapshot, never a log: what it is doing and how much it has
        done. Nothing accumulates, so a poll can be repeated or missed harmlessly."""
        if now is not None and now != st["activity"].get("now"):
            st["activity"]["now"] = now[:300]
            st["changed_at"] = time.time()
        for key, n in counts.items():
            st["activity"][key] = st["activity"].get(key, 0) + n

    def _forget_request(self, st, rid):
        """Drop a server request without reviving a thread that already finished."""
        if rid is None:
            return
        self.requests.pop(rid, None)
        if st["state"] == "awaiting_approval" and not pending_for(st["thread_id"]):
            st["state"] = "running"

    def _add_children(self, st, ids):
        """Child thread ids, deduped and in spawn order. Bounded by how many agents a
        turn spawns, so it is a roster, not a log."""
        subs = st["activity"].setdefault("sub_agents", [])
        for child in ids or []:
            if child and child not in subs:
                subs.append(child)

    def _push_item(self, st, item, completed):
        t = item.get("type")
        if t == "commandExecution":
            if completed:
                self._note(st, commands=1)
                if item.get("exitCode"):
                    self._note(st, failed_commands=1)
            else:
                self._note(st, now=f"running: {item.get('command') or ''}")
        elif t == "fileChange" and completed:
            changes = item.get("changes") or []
            n = len(changes) if isinstance(changes, list) else 1
            self._note(st, now="editing files", files_changed=n)
        elif t == "webSearch" and completed:
            self._note(st, now="searching the web", web_searches=1)
        elif t == "mcpToolCall" and completed:
            self._note(st, now=f"tool: {item.get('tool') or item.get('toolName') or ''}")
        elif t == "contextCompaction":
            # The thread hit its window and codex summarised the history in place. The
            # thread keeps going and keeps its id — this is why a long run can continue
            # past the context window instead of dying at it. Worth reporting: the
            # earlier turns still exist, but as a summary rather than verbatim.
            self._note(st, now="compacting context", compactions=1)
        elif t == "collabAgentToolCall":
            # Delegation: the parent can sit for many minutes running no command of its
            # own while children work. Children are real threads — codex_poll(child)
            # answers for them — so the ids are the actionable part.
            self._add_children(st, item.get("receiverThreadIds"))
            states = {c: s.get("status") for c, s in (item.get("agentsStates") or {}).items()
                      if isinstance(s, dict)}
            if states:
                # Only this item carries a real status. Nothing else does, so nothing
                # else may claim to.
                live = st["activity"].setdefault("sub_agent_status", {})
                live.update(states)
                busy = [c for c, s in live.items() if s in ("pendingInit", "running")]
                self._note(st, now=(f"delegating: {len(busy)} of {len(live)} sub-agents running"
                                    if busy else f"{len(live)} sub-agents finished"))
            else:
                self._note(st, now=f"delegating to {len(st['activity']['sub_agents'])} sub-agents")
        elif t == "subAgentActivity":
            # kind is started|interacted|interrupted — an event, never a completion, so
            # it is reported as the latest event and never stored as a status.
            self._add_children(st, [item.get("agentThreadId")])
            name = os.path.basename(item.get("agentPath") or "") or "sub-agent"
            self._note(st, now=f"sub-agent {name}: {item.get('kind')}")
        elif t == "agentMessage":
            if completed:
                st["final_message"] = item.get("text") or st.get("final_message")
            else:
                self._note(st, now="writing its answer")

    def _complete_turn(self, st, turn):
        status = turn.get("status")
        text = st.get("final_message")
        for it in turn.get("items") or []:
            if isinstance(it, dict) and it.get("type") == "agentMessage" and it.get("text"):
                text = it["text"]
        st["duration_ms"] = turn.get("durationMs")
        for rid in [r for r, v in self.requests.items() if v["thread"] == st["thread_id"]]:
            self.requests.pop(rid, None)
        st["terminal_at"] = time.time()
        if status == "completed":
            st["state"] = "completed"
            st["output"] = text or ""
        elif status in ("interrupted", "cancelled"):
            st["state"] = "interrupted"
            st["output"] = text or ""
        else:
            st["state"] = "failed"
            st["error"] = turn.get("error") or {"message": f"turn ended with status {status}"}


APP = AppServer()


def _new_thread_state(tid, cwd, mode, model_slug, kind="project"):
    return {"thread_id": tid, "cwd": cwd, "mode": mode, "model": model_slug,
            "workspace": kind, "gen": APP.gen, "turn_id": None, "state": "idle",
            "activity": {"now": None},
            # a boolean, not the schema: poll only needs to know whether to JSON-decode
            "structured": False,
            "output": None, "error": None, "final_message": None, "usage": None,
            "duration_ms": None, "terminal_at": None,
            "started_at": time.time(), "changed_at": time.time()}


def pending_for(tid):
    """Derived, never stored twice: APP.requests is the one table."""
    return [r["view"] for r in APP.requests.values() if r["thread"] == tid]


WIRE_TO_SLUG = {(m, e): slug for slug, (m, e) in MODELS.items()}


def _live_entry(tid):
    """The cached state of a thread THIS app-server child has loaded, or None. An entry
    from an earlier generation outlived the child that loaded it: codex no longer has
    the thread open here, so it is not ours to report live, compact or interrupt."""
    with APP.lock:
        st = APP.threads.get(tid)
    return st if st is not None and st.get("gen") == APP.gen else None


def thread_access_error(tid, err):
    """The error a caller sees when codex will not resume, fork or read a thread. codex
    sends every such refusal as -32600, so only its message tells them apart. The classes
    matched here describe the thread or the id the caller passed, so they are refusals;
    anything else is codex failing and goes back exactly as codex said it. thread/read
    and thread/turns/list word a malformed id as "invalid thread id", resume and fork as
    "invalid session id"; an unknown well-formed id is "thread not loaded" to the first
    two and "no rollout found" to the others."""
    payload = err.payload if isinstance(err.payload, dict) else {}
    message = str(payload.get("message") or "")
    if "already has an active writer" in message:
        return ThreadHeldElsewhere(
            f"thread {tid} is open in another process: another Claude Desktop connection, the Codex app, "
            f"codex in a terminal, or a script. codex lets one process write a thread at a time, and that "
            f"process keeps the thread until it unloads it or exits. codex_poll still reads its latest "
            f"results. To continue now, codex_fork it and use the new thread, which can be compacted too. "
            f"Otherwise retry once that process lets go.")
    if "no rollout found" in message or "thread not loaded" in message:
        return ValueError(f"unknown thread '{tid}': codex has no saved thread with that id")
    if "invalid session id" in message or "invalid thread id" in message:
        return ValueError(f"'{tid}' is not a thread id")
    return err


def attach_thread(tid, cwd_override=None, mode=None, model_slug=None):
    """Take ownership of a thread this bridge is not currently tracking — pruned from
    the local cache, or held over from a previous bridge process — before codex_submit
    or codex_compact sends it work.

    resume, not read. `thread/read` returns a snapshot: it does not subscribe, and it
    does not replay the server requests the thread is already parked on, so a live
    thread would come back as plain "running" with an approval nobody can answer.
    resume attaches the connection, replays those requests, and reports the thread's
    real model, effort, sandbox and cwd — so provenance is read, never invented.
    """
    APP.ensure()
    windows_gate()
    params = {"threadId": tid, "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    if cwd_override:
        params["cwd"] = cwd_override
    # Claim the id BEFORE the request. The reader drops notifications for threads it
    # does not know, so anything arriving while resume is in flight — turn/completed
    # included — would be lost, leaving the thread running forever.
    placeholder = _new_thread_state(tid, cwd_override or SCRATCH_ROOT, mode or "write",
                                    model_slug or "luna-medium")
    placeholder["state"] = "running"
    with APP.lock:
        claimed = tid not in APP.threads
        if claimed:
            APP.threads[tid] = placeholder
    try:
        res = APP.request("thread/resume", params, timeout=120)
    except CodexError as e:
        with APP.lock:
            if claimed and APP.threads.get(tid) is placeholder:
                APP.threads.pop(tid, None)
        raise thread_access_error(tid, e) from None

    thread = res.get("thread") or {}
    cwd = cwd_override or res.get("cwd") or thread.get("cwd") or SCRATCH_ROOT
    with APP.lock:
        st = APP.threads.get(tid) or placeholder
        st["cwd"] = cwd
        st["workspace"] = "scratch" if str(cwd).startswith(SCRATCH_ROOT) else "project"
        st["gen"] = APP.gen
        # Real values off the resume response — the whole reason to prefer it over read.
        st["model"] = model_slug or WIRE_TO_SLUG.get(
            (res.get("model"), res.get("reasoningEffort")), res.get("model"))
        # mode is deliberately NOT inferred from res["sandbox"]: that is the thread's
        # stored default, while every turn carries its own sandboxPolicy, so a thread
        # whose turns all ran workspace-write still reports readOnly here. Unknown until
        # the next turn states it — the same rule as sub_agent_status.
        st["mode"] = mode
        st["resumed"] = True
        _adopt_thread_record(st, thread)
        APP.threads[tid] = st
    log(f"attached thread {tid} (state={st['state']}, model={st['model']})")
    return st


def _adopt_thread_record(st, thread):
    """Set state from the resumed thread's own record. Only claims what the record
    actually asserts: an unloaded or summarised turn is not evidence of an empty answer."""
    status = (thread.get("status") or {})
    flags = status.get("activeFlags") or []
    turns = thread.get("turns") or []
    last = turns[-1] if turns else {}
    st["turn_id"] = last.get("id") or st.get("turn_id")
    st["duration_ms"] = last.get("durationMs")

    items_view = last.get("itemsView")
    text, truncated = "", items_view not in (None, "Full", "full")
    for it in last.get("items") or []:
        if isinstance(it, dict) and it.get("type") == "agentMessage" and it.get("text"):
            text = it["text"]
    # A recovered poll must not hand back a JSON string where the first poll gave an object.
    st["structured"] = text.lstrip()[:1] in ("{", "[")

    turn_status = last.get("status")
    if flags:
        # The thread is live and parked. resume replayed the requests, so the caller
        # can actually answer them; state follows from the request table, not a guess.
        st["state"] = "awaiting_approval" if pending_for(st["thread_id"]) else "running"
        st["active_flags"] = flags
    elif turn_status == "completed":
        st["state"], st["output"], st["terminal_at"] = "completed", text, time.time()
    elif turn_status in ("interrupted", "cancelled"):
        st["state"], st["output"], st["terminal_at"] = "interrupted", text, time.time()
    elif turn_status == "failed":
        st["state"], st["error"], st["terminal_at"] = "failed", last.get("error"), time.time()
    else:
        st["state"] = "running"
    if truncated and st["state"] in ("completed", "interrupted") and not text:
        # codex did not load this turn's items; "" is absence of evidence, not an answer.
        st["output"] = None
        st["output_unavailable"] = (f"codex returned itemsView={items_view!r} for this turn, "
                                    f"so the final message was not loaded")


def prune_threads():
    """A finished thread is a cache. Keep it briefly so a lost poll can be retried,
    then let it go — codex still has it, and thread/resume brings it back."""
    now = time.time()
    with APP.lock:
        for tid in [t for t, st in APP.threads.items()
                    if st.get("terminal_at") and now - st["terminal_at"] > TERMINAL_TTL_SECONDS]:
            APP.threads.pop(tid, None)


def _provenance(st):
    wire_model, effort = MODELS.get(st["model"], (st["model"], None))
    return {"transport": "app-server", "model": wire_model, "model_slug": st["model"], "effort": effort,
            "mode": st["mode"], "workspace": st.get("workspace", "project"),
            "cwd": st["cwd"], "thread": st["thread_id"], "turn": st["turn_id"],
            "duration_ms": st.get("duration_ms"), "usage": st.get("usage"),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


# ---- tools ---------------------------------------------------------------
def codex_check(_args):
    exe = find_codex()
    # "ready" is the question a caller actually has. The app-server is spawned lazily,
    # so its being idle is normal and says nothing about health — report it as a state
    # that reads correctly rather than a bare boolean that looks like a failure.
    info = {"ready": bool(exe), "codex_on_path": bool(exe), "path": exe,
            "models": sorted(MODELS),
            "cwd_rule": "any existing directory — nothing to register",
            "scratch_workspace": SCRATCH_ROOT,
            "no_cwd_behavior": "a private scratch workspace",
            "app_server": "running" if APP.alive() else "idle — starts on the first submit"}
    if exe:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30)
            info["codex_version"] = (out.stdout or out.stderr).strip()
        except Exception as e:  # noqa: BLE001 — report, never crash the server
            info["version_error"] = str(e)
    if exe and sys.platform == "win32":
        # The one platform where "codex is here" is not "codex is safe to dispatch":
        # its sandbox is opt-in. Spawn the app-server now to ask, rather than let the
        # first submit be what finds out.
        try:
            APP.ensure()
            info["ready"] = APP.windows_sandbox == "ready"
        except Exception as e:  # noqa: BLE001 — the status is the answer, the error is the fix
            info["windows_sandbox_fix"] = str(e)
            info["ready"] = False
        info["windows_sandbox"] = APP.windows_sandbox or "unknown"
        info["app_server"] = "running" if APP.alive() else "idle — starts on the first submit"
    with APP.lock:
        info["threads"] = [{"thread": s["thread_id"], "state": s["state"], "model": s["model"],
                            "mode": s["mode"], "cwd": s["cwd"]} for s in APP.threads.values()]
    return info


NETWORK_NOTE = ("always on in both modes — web search, http, package installs, git remotes; nothing to "
                "enable or approve. Containment is the sandbox (writes confined to cwd), not the network.")
HOW_TO_USE = ("pass a skill's name in codex_submit's skills (deep-research-work:deep-research, or just "
              "deep-research when that is unique) and the bridge injects its instructions into the turn — a "
              "$mention typed into the prompt is not honoured through the app-server. An app is mentioned in the "
              "prompt as [$Name](app://id); MCP tools by name. Skills also fire implicitly when the brief matches "
              "their description. Chrome (the ChatGPT Chrome extension), the in-app Browser and Computer Use are "
              "surfaces of one server, cua_repl (plugin unified-computer-use): ask for them by name in the prompt; "
              "codex prefers Chrome over Computer Use for anything on a web page.")

# OpenAI's bundled surface plugins carry no skill and no server of their own: their capability is
# served by the unified-computer-use plugin's cua_repl server (its js tool's browser and app
# surfaces). Nothing on the wire says so, and without it the map shows them empty.
SERVED_BY = {"chrome@openai-bundled": "cua_repl", "browser@openai-bundled": "cua_repl",
             "computer-use@openai-bundled": "cua_repl"}


def skill_catalog():
    """Every skill codex lists here — name, SKILL.md path, enabled — fetched once per
    app-server child. plugin/installed goes first: skills/list only lists a remote-marketplace
    plugin's skills once that process has loaded the remote catalog."""
    APP.ensure()
    cached = APP.skill_catalog_cache
    if cached and cached[0] == APP.gen:
        return cached[1]
    APP.request("plugin/installed", {"cwds": None, "installSuggestionPluginNames": None}, timeout=60)
    res = APP.request("skills/list", {"cwds": [], "forceReload": True}, timeout=60)
    catalog, seen = [], set()
    for entry in (res or {}).get("data") or []:
        for sk in entry.get("skills") or []:
            if sk.get("name") in seen:
                continue
            seen.add(sk.get("name"))
            catalog.append({"name": sk.get("name"), "path": sk.get("path"),
                            "enabled": sk.get("enabled") is not False})
    APP.skill_catalog_cache = (APP.gen, catalog)
    return catalog


def resolve_skills(names, catalog=None):
    """Skill names as codex lists them -> the structured input items codex injects from.

    Verified, not assumed: a `$skill` written into the prompt text is not honoured through
    the app-server (two live threads answered NONE to "quote the first heading of the skill
    this mention loaded"), while a {type: skill, name, path} input item made the model quote
    the SKILL.md heading. Names resolve exactly, or by the part after the colon when that is
    unique; anything else is refused here, before the turn starts."""
    items = []
    for want in names or []:
        want = str(want).strip().lstrip("$")
        if not want:
            continue
        if catalog is None:
            catalog = skill_catalog()
        hits = [sk for sk in catalog if sk["name"] == want] \
            or [sk for sk in catalog if str(sk["name"]).split(":")[-1] == want]
        if not hits:
            raise ValueError(f"unknown skill {want!r} — codex_capabilities lists the skills codex has here")
        if len(hits) > 1:
            raise ValueError(f"skill {want!r} is ambiguous — pass one of: "
                             + ", ".join(str(sk["name"]) for sk in hits))
        skill = hits[0]
        if not skill.get("enabled", True):
            raise ValueError(f"skill {skill['name']!r} is disabled in codex, so it cannot be injected")
        items.append({"type": "skill", "name": str(skill["name"]).split(":")[-1], "path": skill["path"]})
    return items


def _one_line(value, limit):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def capabilities_inventory(plugins_res, skills_res, mcp_res, apps_res, query=None, errors=None):
    """Shape four app-server answers into one map of what codex can do here.

    A map, not a manual: the default view carries names, mentions and one-line summaries
    only, so it stays a few KB however many skills are installed; a query brings the
    matching descriptions with it. A source that failed is reported under `errors` and
    the rest of the map still comes back — a missing app list is no reason to hide the
    plugins."""
    plugins, skills, servers, apps, seen = [], [], [], [], set()
    for market in (plugins_res or {}).get("marketplaces") or []:
        for p in market.get("plugins") or []:
            iface = p.get("interface") or {}
            plugins.append({"id": p.get("id") or f"{p.get('name')}@{market.get('name')}",
                            "name": iface.get("displayName") or p.get("name"),
                            "summary": _one_line(iface.get("shortDescription") or iface.get("longDescription"), 120),
                            "enabled": bool(p.get("enabled")), "skills": []})
    by_id = {p["id"]: p for p in plugins}
    for entry in (skills_res or {}).get("data") or []:
        for s in entry.get("skills") or []:
            if s.get("enabled") is False or s.get("name") in seen:
                continue                      # a disabled skill cannot be invoked; one cwd's copy is enough
            seen.add(s.get("name"))
            rec = {"name": s.get("name"), "plugin": s.get("pluginId"), "path": s.get("path"),
                   "scope": s.get("scope"), "description": s.get("description") or ""}
            skills.append(rec)
            if rec["plugin"] in by_id:
                by_id[rec["plugin"]]["skills"].append(rec["name"])
    for srv in (mcp_res or {}).get("data") or []:
        tools = srv.get("tools") or {}
        if not isinstance(tools, dict):
            tools = {t.get("name"): t for t in tools if isinstance(t, dict)}
        status = srv.get("runtimeStatus")
        if not tools:
            # A config.toml entry that is disabled, or a server that did not start. Not a verdict
            # on the capability: Computer Use is served by cua_repl while a same-named legacy
            # [mcp_servers.computer-use] entry sits here with nothing in it.
            status = status or "no tools listed (disabled in config.toml, or not started)"
        servers.append({"name": srv.get("name"), "status": status, "plugin": srv.get("pluginId"),
                        "source": "plugin" if srv.get("pluginId") else "config.toml",
                        "tools": sorted(tools), "_tools": tools})
    # A plugin's MCP servers, so a plugin with no skills (Computer Use, Chrome) still shows
    # what it brings — the capability lives in the server's tools, not in a skill.
    running = {srv["name"] for srv in servers if srv["tools"]}
    for p in plugins:
        p["servers"] = [srv["name"] for srv in servers if srv["plugin"] == p["id"]]
        if not p["skills"] and not p["servers"] and p["id"] in SERVED_BY:
            host = SERVED_BY[p["id"]]
            p["via"] = host if host in running else f"{host} (not running)"
    for a in (apps_res or {}).get("apps") or []:
        apps.append({"name": a.get("runtimeName") or a.get("id"),
                     "mention": f"[${a.get('runtimeName') or a.get('id')}](app://{a.get('id')})",
                     "enabled": bool(a.get("enabled")), "callable": bool(a.get("callable"))})

    out = {"network": NETWORK_NOTE, "how_to_use": HOW_TO_USE}
    if query:
        q = str(query).lower()
        matches = []
        for p in plugins:
            if q in f"{p['name']} {p['summary']} {p['id']}".lower():
                matches.append({"kind": "plugin", **p})
        for s in skills:
            if q in f"{s['name']} {s['description']}".lower():
                matches.append({"kind": "skill", "name": s["name"], "plugin": s["plugin"], "path": s["path"],
                                "description": _one_line(s["description"], 300)})
        for srv in servers:
            for tname, t in srv["_tools"].items():
                desc = (t.get("description") if isinstance(t, dict) else "") or ""
                if q in f"{tname} {desc}".lower():
                    matches.append({"kind": "tool", "name": tname, "server": srv["name"], "description": _one_line(desc, 300)})
        for a in apps:
            if q in a["name"].lower():
                matches.append({"kind": "app", **a})
        out.update({"query": query, "matches": matches})
    else:
        out.update({"plugins": plugins,
                    "system_skills": [s["name"] for s in skills if not s["plugin"]],
                    "mcp_servers": [{k: v for k, v in srv.items() if k != "_tools"} for srv in servers],
                    "apps": apps,
                    "counts": {"plugins": len(plugins), "skills": len(skills),
                               "mcp_servers": len(servers), "apps": len(apps)}})
    if errors:
        out["errors"] = errors
    return out


def codex_capabilities(args):
    """What codex can do on this machine, asked of the app-server itself rather than
    guessed from disk: codex owns the discovery rules (marketplaces, config, caches)."""
    APP.ensure()
    results, errors = {}, {}
    # plugin/installed goes FIRST. skills/list only lists a remote-marketplace plugin's
    # skills once that app-server process has loaded the remote catalog, which
    # plugin/installed does — asked the other way round, Deep Research is invisible.
    for key, method, params in (
            ("plugins", "plugin/installed", {"cwds": None, "installSuggestionPluginNames": None}),
            ("skills", "skills/list", {"cwds": [], "forceReload": True}),
            ("mcp", "mcpServerStatus/list", {}),
            ("apps", "app/installed", {})):
        try:
            results[key] = APP.request(method, params, timeout=60)
        except CodexError as e:
            results[key] = None
            errors[key] = f"{method}: {json.dumps(e.payload)[:200]}"
    return capabilities_inventory(results["plugins"], results["skills"], results["mcp"], results["apps"],
                                  query=args.get("query"), errors=errors or None)


def codex_submit(args):
    prompt = args.get("prompt")
    if not prompt or not str(prompt).strip():
        raise ValueError("prompt is required (the complete instruction; for a new thread it is all codex sees)")
    model_slug = args.get("model")
    if model_slug not in MODELS:
        raise ValueError(f"unknown model '{model_slug}' — one of {sorted(MODELS)}")
    wire_model, effort = MODELS[model_slug]
    mode = args.get("mode") or "write"
    if mode not in MODES:
        raise ValueError(f"unknown mode '{mode}' — one of {list(MODES)}")
    schema = args.get("output_schema")
    if isinstance(schema, str):
        schema = json.loads(schema)

    APP.ensure()
    skills = args.get("skills")
    skill_items = resolve_skills([skills] if isinstance(skills, str) else skills)   # refused before any state change
    tid = args.get("thread")

    if tid:
        st = APP.threads.get(tid)
        stale = st is not None and st.get("gen") != APP.gen
        if st is None or stale:
            # Unknown to us, or held over from a previous app-server child: the new
            # child has not loaded it, so attach before any turn/start. Same path poll
            # uses — one way to take ownership of a thread, one set of semantics.
            cwd_override = None
            if args.get("cwd"):
                cwd_override = resolve_workspace(args["cwd"])[0]
            elif stale:
                cwd_override = st["cwd"]
            # else: send no cwd override — codex restores the thread's own working
            # directory. Inventing a scratch dir here would silently relocate the work.
            if stale:
                with APP.lock:
                    APP.threads.pop(tid, None)     # so attach_thread claims it afresh
            st = attach_thread(tid, cwd_override, mode=mode, model_slug=model_slug)
        if st.get("resumed") and not args.get("cwd"):
            # A resumed thread's cwd is whatever another client chose for it. Hold it to
            # the same rule as a thread the bridge starts — before steering or a new turn
            # sends more work there. Inspection and interruption stay available.
            reason = refuse_reason(os.path.realpath(st["cwd"]))
            if reason:
                raise ValueError(f"resumed thread {tid} works in {st['cwd']}: {reason}. Pass cwd to move it.")
        if st["state"] == "awaiting_approval":
            ids = [p["request_id"] for p in pending_for(tid)]
            raise ValueError(f"thread {tid} is waiting on approval request(s) {ids} — "
                             f"resolve them with codex_approve first")
        if st["state"] == "running" and st.get("turn_id"):
            # Mid-turn input steers the live turn rather than queueing a new one.
            # turn/steer carries input and nothing else, so a per-turn override cannot
            # apply here. Say so instead of accepting it and quietly using the old value —
            # a cwd override would otherwise resolve images against the wrong directory.
            ignored = [k for k in ("cwd", "output_schema") if args.get(k)]
            if ignored:
                raise ValueError(
                    f"thread {tid} is mid-turn, so this input steers the running turn; "
                    f"{', '.join(ignored)} cannot apply to a turn already in flight. "
                    f"Drop {'it' if len(ignored) == 1 else 'them'} to steer, or wait for "
                    f"the turn to finish and submit a new one.")
            APP.request("turn/steer", {"threadId": tid, "expectedTurnId": st["turn_id"],
                                       "input": build_input(prompt, args.get("images"), st["cwd"], skill_items)})
            return {"thread": tid, "turn": st["turn_id"], "state": "running", "steered": True}
    else:
        cwd, kind = resolve_workspace(args.get("cwd"))
        start = {"cwd": cwd, "model": wire_model,
                 "approvalPolicy": "on-request", "approvalsReviewer": "user"}
        pinned = windows_gate()
        if pinned:
            # Bind the verified mode to this thread. thread/start reloads config.toml,
            # so without this a setting removed mid-session would silently apply.
            start["config"] = {"windows.sandbox": pinned}
        res = APP.request("thread/start", start, timeout=120)
        tid = (res.get("thread") or {}).get("id")
        if not tid:
            raise CodexError({"message": "thread/start returned no thread id", "result": res})
        st = _new_thread_state(tid, cwd, mode, model_slug, kind)
        with APP.lock:
            APP.threads[tid] = st

    # Per-turn overrides: model, effort, cwd, sandbox and schema all apply to this turn.
    # A follow-up may move the thread; each cwd is validated the same way and stamped.
    if args.get("cwd"):
        cwd, kind = resolve_workspace(args["cwd"])      # a follow-up may move the thread
    else:
        cwd, kind = st["cwd"], st.get("workspace", "project")
    turn_input = build_input(prompt, args.get("images"), cwd, skill_items)   # validated before any state change
    # Under the lock: the reader mutates this same state. Everything a turn produces is
    # cleared, so a fresh turn can never report the previous turn's tokens or duration.
    with APP.lock:
        st.update({"mode": mode, "model": model_slug, "cwd": cwd, "workspace": kind,
                   "structured": schema is not None, "terminal_at": None,
                   "started_at": time.time(), "changed_at": time.time(), "activity": {},
                   "state": "running", "output": None, "error": None, "final_message": None,
                   "usage": None, "duration_ms": None, "output_unavailable": None,
                   "active_flags": None})
    params = {"threadId": tid, "input": turn_input,
              "cwd": cwd, "model": wire_model, "effort": effort,
              "sandboxPolicy": sandbox_policy(mode),
              "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    if schema is not None:
        params["outputSchema"] = schema
    st["turn_id"] = None            # never steer a stale turn if this start fails
    try:
        res = APP.request("turn/start", params, timeout=120)
    except Exception as e:
        with APP.lock:
            st["state"] = "failed"
            st["error"] = e.payload if isinstance(e, CodexError) else {"message": str(e)}
        # The thread exists in codex even though this turn did not start. Carry its id
        # out with the error — without it the caller cannot poll or retry, and a thread
        # holding their cwd and context is orphaned by an error message.
        e.codex_thread = tid
        raise
    turn = res.get("turn") or {}
    st["turn_id"] = turn.get("id")
    return {"thread": tid, "turn": st["turn_id"], "state": "running",
            "mode": mode, "model": model_slug, "cwd": cwd,
            "workspace": st.get("workspace", "project")}


def codex_poll(args):
    tid = args.get("thread")
    with APP.lock:
        st = APP.threads.get(tid)
    if st is None:
        # Pruned from the local cache, or left over from a previous bridge process.
        # codex is the store, so attach rather than lose hours of work to a cache expiry.
        st = attach_thread(tid)
    with APP.lock:
        # Everything here repeats on every poll: nothing is consumed, so a lost or
        # duplicated poll costs nothing and there are no pieces to reassemble.
        state = st["state"]
        # Deep enough to detach the nested containers: sub_agents is a list the reader
        # thread appends to, so a shallow copy would still be shared and could change
        # underneath us while this result is serialised.
        activity = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                    for k, v in st["activity"].items()}
        if state in ("running", "awaiting_approval"):
            # Hours-long turns are normal; a stalled one is not. quiet_seconds is time
            # since codex last said anything at all about this thread — a long message
            # streams deltas and token usage the whole way, so silence means silence.
            activity["running_seconds"] = int(time.time() - st["started_at"])
            activity["quiet_seconds"] = int(time.time() - st["changed_at"])
            if st.get("usage"):
                activity["tokens"] = st["usage"]      # climbs while a long answer generates
        # Runway, not a gauge: how many tokens are left before codex compacts. That is a
        # decision the caller can act on (bank the results now, or let it compact and
        # keep going); "84% full" only invites abandoning a thread that was fine.
        runway = compaction_runway(st.get("context_window"), st.get("context_used"))
        if runway is not None:
            activity["context"] = runway
        out = {"thread": tid, "state": state, "activity": activity}
        if st.get("resumed"):
            # the bridge re-attached to an existing thread rather than creating it
            out["resumed"] = True
        if state == "awaiting_approval":
            out["requests"] = pending_for(tid)          # derived; may be more than one
            out["thread_scope"] = {"mode": st["mode"], "cwd": st["cwd"]}
        elif state == "completed" and st.get("output_unavailable"):
            # Never pass off "no items loaded" as an empty answer.
            out["output"] = None
            out["output_unavailable"] = st["output_unavailable"]
            out["provenance"] = _provenance(st)
        elif state == "completed":
            text = st.get("output") or ""
            if st.get("structured"):
                try:
                    out["output"] = json.loads(text)
                except json.JSONDecodeError as e:
                    out["output"] = text          # never trimmed, never reshaped
                    out["schema_parse_error"] = str(e)
            else:
                out["output"] = text
            out["provenance"] = _provenance(st)
        elif state in ("failed", "interrupted"):
            out["error"] = st.get("error")
            out["output"] = st.get("output")
            out["provenance"] = _provenance(st)
    prune_threads()
    return out


def _peek_request(rid):
    """MCP arguments arrive as JSON, so an id the app-server issued as integer 0 comes
    back from a caller as "0" and a plain dict lookup misses. JSON-RPC permits either
    type, so match on the value as given and on both coercions — and return the
    CANONICAL key, because the reply must echo the id in the type the app-server sent
    or it will not match the request and the turn stays parked forever."""
    candidates = [rid, str(rid)]
    if str(rid).lstrip("-").isdigit():
        candidates.append(int(rid))
    with APP.lock:
        for key in candidates:
            if key in APP.requests:
                return key, APP.requests[key]
        pending = sorted(str(k) for k in APP.requests)
    raise ValueError(f"no pending approval with request_id {rid!r}"
                     + (f" — pending: {pending}" if pending else " — nothing is awaiting approval"))


def decline_response(kind):
    """A refusal in the reply shape this request type defines. A permission request
    wants an (empty) grant, an elicitation an MCP action, a command or patch a
    decision. codex logs a reply in the wrong shape as malformed before it falls
    back to declining, so the shape matters even when the answer is no."""
    if kind == "permissions":
        return {"permissions": {}, "scope": "turn"}
    if kind == "elicitation":
        return {"action": "decline"}
    return {"decision": "decline"}


def persist_modes(meta):
    """The persistence an elicitation offers, as a list. codex attaches `persist` to
    its own MCP tool-call approvals as one mode or a list of modes; only those may be
    echoed back, so a mode the request never offered is never sent."""
    offered = meta.get(ELICITATION_PERSIST_KEY) if isinstance(meta, dict) else None
    if isinstance(offered, str):
        return [offered]
    if isinstance(offered, list):
        return [m for m in offered if isinstance(m, str)]
    return []


def elicitation_view(params):
    """What an MCP server is asking, so the caller can decide and, for a form, answer.
    message, mode and requested_schema (or url) are the request itself; tool and
    persist_modes come from the `_meta` codex attaches to its MCP tool-call approvals."""
    meta = params.get("_meta")
    meta = meta if isinstance(meta, dict) else {}
    return {"server": params.get("serverName"),
            "message": params.get("message"),
            "mode": params.get("mode"),
            "requested_schema": params.get("requestedSchema"),
            "url": params.get("url"),
            "tool": meta.get(ELICITATION_TOOL_NAME_KEY),
            "persist_modes": persist_modes(meta)}


def elicitation_response(params, decision, grant):
    """The reply to an MCP elicitation for the caller's decision — codex's
    McpServerElicitationRequestResponse: action accept | decline, content for a form,
    _meta.persist when the grant is to be remembered. Returns (result, note).

    content goes on every accept. codex itself reads a bare accept as content {}, but
    the reply travels on to the MCP server, and a complete ElicitResult holds however
    that server checks it. Required fields are checked here, before anything is sent:
    the request is still pending at this point, so a refused grant stays retryable."""
    if decision == "deny":
        return {"action": "decline"}, None
    content = json.loads(grant) if isinstance(grant, str) else grant
    if content is None:
        content = {}
    if not isinstance(content, dict):
        raise ValueError("grant for an elicitation must be a JSON object — the form's answers keyed by field name")
    schema = params.get("requestedSchema")
    if isinstance(schema, dict):
        missing = [name for name in (schema.get("required") or []) if name not in content]
        if missing:
            raise ValueError(
                f"elicitation from {params.get('serverName')} requires {missing} — pass them in grant, an "
                f"object keyed by field name. Fields: {json.dumps(schema.get('properties') or {})[:600]}")
    result = {"action": "accept", "content": content}
    note = None
    if decision in ("allow_always", "allow_class"):
        offered = persist_modes(params.get("_meta"))
        wanted = ELICITATION_PERSIST_SESSION if decision == "allow_always" else ELICITATION_PERSIST_ALWAYS
        if wanted in offered:
            result["_meta"] = {ELICITATION_PERSIST_KEY: wanted}
        elif wanted == ELICITATION_PERSIST_ALWAYS and ELICITATION_PERSIST_SESSION in offered:
            result["_meta"] = {ELICITATION_PERSIST_KEY: ELICITATION_PERSIST_SESSION}
            note = "this elicitation does not offer 'always' persistence — accepted for the session instead"
        else:
            note = (f"this elicitation does not offer '{wanted}' persistence"
                    + (f" (offered: {', '.join(offered)})" if offered else "") + " — accepted once")
    return result, note


def codex_approve(args):
    note = None
    rid = args.get("request_id")
    decision = args.get("decision")
    if decision not in ("allow", "allow_always", "allow_class", "deny"):
        raise ValueError("decision must be allow | allow_always | allow_class | deny")
    rid, req = _peek_request(rid)     # not removed yet: a bad grant must be retryable
    kind = req["kind"]

    if kind == "permissions":
        requested = req["params"].get("permissions") or {}
        grant = args.get("grant")
        if decision == "deny":
            granted = {}
        elif grant is not None:
            granted = json.loads(grant) if isinstance(grant, str) else grant
        else:
            granted = requested
        scope = args.get("scope") or ("session" if decision == "allow_always" else "turn")
        result = {"permissions": granted, "scope": scope}
    elif kind == "elicitation":
        result, note = elicitation_response(req["params"], decision, args.get("grant"))
    else:  # command / file_change
        amendment = req["params"].get("proposedExecpolicyAmendment")
        if decision == "allow_class" and amendment:
            # One grant covers the whole command class — codex appends an allow rule to
            # its execpolicy, so it persists past this session — instead of
            # acceptForSession, which only matches this exact command string.
            result = {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}}
        else:
            if decision == "allow_class":
                note = "no execpolicy amendment was offered for this command — granted for the session instead"
            result = {"decision": {"allow": "accept", "allow_always": "acceptForSession",
                                   "allow_class": "acceptForSession", "deny": "decline"}[decision]}

    APP.respond(rid, result)          # only now is the request truly spent
    tid = req.get("thread")
    with APP.lock:
        APP.requests.pop(rid, None)
        st = APP.threads.get(tid)
        if st is not None:
            # The turn may have completed while we were answering — do not resurrect it,
            # and stay in awaiting_approval while other requests are still outstanding.
            if st["state"] == "awaiting_approval" and not pending_for(tid):
                st["state"] = "running"
            APP._note(st, now="continuing after approval")
        # Read the state back rather than asserting "running": another request may still
        # be parked, or the turn may have finished while the caller was deciding.
        state = st["state"] if st is not None else "unknown"
    out = {"resolved": True, "thread": tid, "state": state, "sent": result}
    if note:
        out["note"] = note
    return out


def codex_interrupt(args):
    tid = args.get("thread")
    st = _live_entry(tid)
    if st is None:
        # Never seen, or held over from an earlier child: the turn it remembers died with
        # that child, and a turn running in another process is not this bridge's to stop.
        raise ValueError(f"thread {tid} is not running in this plugin: a turn running in another "
                         f"process has to be stopped there")
    if not st.get("turn_id"):
        return {"thread": tid, "state": st["state"], "note": "no active turn"}
    APP.request("turn/interrupt", {"threadId": tid, "turnId": st["turn_id"]}, timeout=60)
    return {"thread": tid, "state": "interrupted"}


def codex_compact(args):
    """Compact now, at a boundary the caller picks.

    Left alone, codex compacts mid-task when it runs out of room — whenever that
    happens to fall. Calling this between turns, once results are banked, means the
    summary is taken at a clean point instead of halfway through an investigation.
    """
    tid = args.get("thread")
    st = _live_entry(tid)
    if st is None:
        # Missing, or held over from an earlier app-server child that has not loaded it:
        # attach first, as codex_submit does, keeping the working directory it had.
        with APP.lock:
            stale = APP.threads.pop(tid, None)
        st = attach_thread(tid, stale["cwd"] if stale else None)
    if st["state"] == "running" and st.get("turn_id"):
        raise ValueError(f"thread {tid} is mid-turn — compacting now would summarise the work "
                         f"in progress. Wait for the turn to finish, or codex_interrupt first.")
    APP.request("thread/compact/start", {"threadId": tid}, timeout=60)
    # Runs as a turn of its own: it returns immediately and the work happens after.
    with APP.lock:
        st["state"] = "running"
        st["changed_at"] = time.time()
    return {"thread": tid, "state": "running",
            "note": "compaction started — poll until it completes; the thread keeps its id"}


def codex_fork(args):
    """Stand-in until the fork itself lands: the tool goes on the surface first, so the
    manifest, schema and guidance are checked before anything is built behind them."""
    raise ValueError("codex_fork is not available in this build yet")


TOOLS = [
    {
        "name": "codex_check",
        "description": "Preflight: ready (can dispatch), codex version, available models, live threads. There is nothing to configure — cwd may be any existing directory. The app-server spawns on the first submit — on Windows also on this check, to ask codex about its sandbox — so an idle app_server is normal and never a reason to hold off. On Windows the answer includes windows_sandbox: codex's own sandbox must be ready, and the bridge does not dispatch until it is.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "codex_capabilities",
        "description": ("What codex can do on this machine beyond files and shell: every enabled plugin with the "
                        "skills it contributes (pass their names in codex_submit's skills), every MCP server with "
                        "its tools, and the connected apps — "
                        "plus the standing facts (network always on). Call it before briefing work that might lean on "
                        "one: research (deep-research), documents, decks, spreadsheets, browsing, desktop control, an "
                        "external service. Spawns the app-server if it is idle. query narrows the answer to matching "
                        "skills, tools, plugins and apps and adds their descriptions."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Substring to search across skill, tool, plugin and app names and descriptions — research, spreadsheet, chrome. Omit for the whole map."}},
            "additionalProperties": False},
    },
    {
        "name": "codex_submit",
        "description": ("Start a codex thread or add a turn to one. No thread → new session; a thread that is idle → "
                        "next turn with full prior context; a thread mid-turn → the input steers the running turn. "
                        "Returns instantly: poll with codex_poll. Pass output_schema on any turn to get JSON back. Inject a plugin skill "
                        "with skills (codex_capabilities lists their names). A thread another process has open — "
                        "another Claude Desktop connection, the Codex app, codex in a terminal, a script — is refused "
                        "with reason held_elsewhere: codex_fork it to continue on a new id."),
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "The instruction. For a new thread this is all codex sees — make it self-contained. Ask for the answer as output rather than a report file, and if it covers many items bound it here — cap, index, and what to do when they do not fit. Nothing else will."},
            "model": {"type": "string", "enum": sorted(MODELS),
                      "description": "luna=scouting, then hand findings to the model that does the work; terra=everyday work; sol=complex implementation; astra=GPT-6 heavyweight for hard, long-context or agentic work and independent review. Effort: high/xhigh are the working range; max rarely improves on xhigh; ultra = xhigh plus proactive sub-agent delegation, for work that splits into substantial independent parts (max for small or tightly-coupled work)."},
            "thread": {"type": "string", "description": "Continue this thread. Omit to start a new one."},
            "cwd": {"type": "string", "description": "Absolute path to the directory to work in — normally the folder this session is already in. Any existing directory works; nothing needs registering. Reads still see the surrounding repo; writes are confined here, so aim it at the narrowest directory the writes should reach. Omit for work that needs no repo (research, reasoning, throwaway code): the thread gets a private scratch workspace. Point concurrent write threads at different cwd (e.g. worktrees) and they cannot collide."},
            "mode": {"type": "string", "enum": list(MODES), "description": "write (default) or read. Both keep network access (always on) and the full tool surface."},
            "output_schema": {"type": ["object", "string"], "description": "JSON Schema constraining this turn's final message. Omit for prose."},
            "images": {"type": "array", "items": {"type": "string"},
                       "description": "Images to send with this turn — screenshots, mockups, diagrams, a failing UI. Each entry is a file path (absolute, or relative to cwd) or an http(s)/data URL. Works on any turn, new thread or follow-up."},
            "skills": {"type": "array", "items": {"type": "string"},
                       "description": "Skills to inject into this turn, by the names codex_capabilities lists (deep-research-work:deep-research, or just deep-research when that is unique). Each skill's instructions go on the wire with the input — a $mention typed into the prompt is not honoured. Unknown, ambiguous or disabled names are rejected before the turn starts."}},
            "required": ["prompt", "model"], "additionalProperties": False},
    },
    {
        "name": "codex_fork",
        "description": ("Copy a thread's full saved history to a new thread id, then codex_submit to the new id "
                        "to continue there. Two uses. On purpose: branch a line of work — try another direction — "
                        "while the original stays exactly as it was. As a workaround: when codex_submit or "
                        "codex_compact answers reason held_elsewhere, the thread is open in another process; fork it "
                        "and keep working on the copy, compaction included, while the original stays with that "
                        "process. codex copies what it has saved, so a turn still running elsewhere comes over only "
                        "up to its last saved step. From then on the two threads are independent: record the new id "
                        "with the work. cwd defaults to the original thread's working directory."),
        "inputSchema": {"type": "object", "properties": {
            "thread": {"type": "string", "description": "The thread to copy."},
            "cwd": {"type": "string", "description": "Absolute path the fork works in. Omit to keep the original thread's working directory."}},
            "required": ["thread"], "additionalProperties": False},
    },
    {
        "name": "codex_poll",
        "description": ("Where the thread is right now: state (running | awaiting_approval | completed | "
                        "failed), an activity snapshot (what it is doing and how much it has done), the "
                        "pending approval request(s) when it is waiting — a command, file change, permission "
                        "or an MCP elicitation, each with what is being asked — and the complete output "
                        "once it is done. Idempotent — nothing is consumed, and the answer arrives whole rather than in "
                        "pieces to reassemble. Poll every 20-30s and relay activity in plain language. A thread this "
                        "bridge is not running is read from codex's saved record without taking it (read_only: true): "
                        "its latest results, never its approvals, which belong to the process running it."),
        "inputSchema": {"type": "object", "properties": {"thread": {"type": "string"}},
                        "required": ["thread"], "additionalProperties": False},
    },
    {
        "name": "codex_approve",
        "description": ("Resolve an approval codex is waiting on; the thread then continues. allow = this once, "
                        "allow_always = stop asking for this exact command for the session, deny = refuse (codex adapts). "
                        "For permission requests, grant may narrow the request to a subset. When the "
                        "request carries proposed_execpolicy_amendment, allow_class grants that whole class as a "
                        "codex execpolicy allow rule — it persists beyond this session, and commands it "
                        "matches run outside the sandbox from then on. An elicitation (kind: elicitation) "
                        "is an MCP server codex is using asking the caller something — a permission prompt "
                        "such as a browser plugin opening a tab, or a short form. It resolves the same way: "
                        "allow accepts, deny declines, allow_always accepts and has it remembered for the "
                        "session and allow_class permanently, each only when the request's persist_modes "
                        "offers it. When its requested_schema has fields, grant carries the answers."),
        "inputSchema": {"type": "object", "properties": {
            "request_id": {"type": ["string", "integer"]},
            "decision": {"type": "string", "enum": ["allow", "allow_always", "allow_class", "deny"],
                         "description": "allow = this once. allow_always = this exact command, rest of session. allow_class = the whole command class (uses the request's proposed_execpolicy_amendment, e.g. any git add) — the one that actually stops a loop re-prompting. It is written to codex's execpolicy as an allow rule: it outlives the session and its matches run outside the sandbox from then on. deny = refuse; codex adapts. For an elicitation: allow = accept once, allow_always = accept and remember for the session, allow_class = accept and remember permanently — each only when the request's persist_modes offers it, otherwise accepted once with a note — deny = decline."},
            "grant": {"type": ["object", "string"], "description": "Permission requests: the subset to grant; omit to grant what was asked. Elicitations: the form's answers as an object keyed by field name (a JSON string is accepted too) — required when requested_schema lists required fields; omit for a plain yes/no elicitation."},
            "scope": {"type": "string", "enum": ["turn", "session"]}},
            "required": ["request_id", "decision"], "additionalProperties": False},
    },
    {
        "name": "codex_interrupt",
        "description": "Stop the thread's active turn. The thread stays usable for later turns.",
        "inputSchema": {"type": "object", "properties": {"thread": {"type": "string"}},
                        "required": ["thread"], "additionalProperties": False},
    },
    {
        "name": "codex_compact",
        "description": ("Summarise this thread's history now, at a point you choose. Optional: codex "
                        "does it automatically when the thread fills up, but then the summary lands "
                        "wherever the work happens to be. Call this between turns once results are "
                        "banked and activity.context.tokens_before_compaction is getting small. The "
                        "thread keeps its id and stays usable; poll until it completes. A thread open in "
                        "another process is refused with reason held_elsewhere: codex_fork it and compact the fork."),
        "inputSchema": {"type": "object", "properties": {"thread": {"type": "string"}},
                        "required": ["thread"], "additionalProperties": False},
    },
]

HANDLERS = {"codex_check": codex_check, "codex_capabilities": codex_capabilities,
            "codex_submit": codex_submit, "codex_fork": codex_fork, "codex_poll": codex_poll,
            "codex_approve": codex_approve, "codex_interrupt": codex_interrupt,
            "codex_compact": codex_compact}


def handle(req):
    method = req.get("method")
    if method == "initialize":
        client = req.get("params", {}).get("protocolVersion") or PROTOCOL_VERSION
        return {"protocolVersion": client, "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "prompts/list":
        return {"prompts": [{
            "name": "run-codex-job",
            "description": "Dispatch a codex thread with live progress",
            "arguments": [
                {"name": "cwd", "description": "Absolute path to work in; omit for a scratch workspace", "required": False},
                {"name": "goal", "description": "What codex should do and report", "required": True},
                {"name": "model", "description": "luna-medium (scout) / terra-high (everyday) / sol-xhigh (complex) / astra-xhigh (heavy, default) / astra-ultra (full run, self-delegates)", "required": False},
                {"name": "mode", "description": "write (default) or read", "required": False},
            ]}]}
    if method == "prompts/get":
        p = req.get("params", {})
        a = p.get("arguments") or {}
        if p.get("name") != "run-codex-job":
            raise ValueError(f"unknown prompt '{p.get('name')}'")
        text = (f"Run a codex thread in {a.get('cwd') or 'a scratch workspace'} "
                f"with model {a.get('model') or 'astra-xhigh'} in {a.get('mode') or 'write'} mode: "
                f"{a.get('goal', '<goal>')}.\n"
                "Use codex_submit, then poll codex_status-style with codex_poll every 20-30 seconds and tell me "
                "what it is doing in plain language. If it asks for an approval, show me the request and your "
                "recommendation before resolving it. When it finishes, show me the result and the git diff if it wrote anything.")
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    if method == "tools/call":
        name = req.get("params", {}).get("name")
        args = req.get("params", {}).get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            raise ValueError(f"unknown tool '{name}'")
        try:
            # No blanket size guard: it used to replace an oversized result with a
            # stub, which permanently destroyed exactly the large answers worth having.
            # Narration is bounded at the buffer; the deliverable passes through whole.
            out = fn(args)
            return {"content": [{"type": "text", "text": json.dumps(out, indent=2)}]}
        except (CodexError, ValueError, json.JSONDecodeError) as e:
            codex_error = isinstance(e, CodexError)
            body = {"outcome": "codex_error" if codex_error else "rejected",
                    "error": e.payload if codex_error else str(e)}
            # Always name the thread. A failure that loses the id strands work that
            # codex is still holding: the caller cannot poll it, resume it, or stop it.
            thread = getattr(e, "codex_thread", None) or args.get("thread")
            if thread:
                body["thread"] = thread
            # A refusal the caller can act on names its reason, so a script branches on it
            # without parsing prose: held_elsewhere means codex_fork.
            reason = getattr(e, "reason", None)
            if reason:
                body["reason"] = reason
            return {"content": [{"type": "text", "text": json.dumps(body, indent=2)}],
                    "isError": True}
    if method == "ping":
        return {}
    if method.startswith("notifications/"):
        return None   # lifecycle and progress notifications: nothing to answer, nothing wrong
    raise ValueError(f"unsupported method '{method}'")


def desktop_config_path():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "Claude", "claude_desktop_config.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def open_instructions(mcpb_path):
    """The one install step only the GUI can do, phrased for the platform. Windows has
    no .mcpb file association, so "double-click it" would leave the user at an Explorer
    prompt; the Settings route is the one that works there."""
    if sys.platform == "win32":
        return ("Claude Desktop -> Settings -> Extensions -> Advanced settings -> Install Extension "
                f"-> {mcpb_path}")
    return f"open '{mcpb_path}'   (or Settings -> Extensions -> Advanced -> Install Extension)"


def install(argv):
    """Idempotent self-installer: stable copy + a packed .mcpb. No configuration."""
    stable_dir = os.path.join(os.path.expanduser("~"), ".claude-codex-bridge")
    os.makedirs(stable_dir, exist_ok=True)
    stable = os.path.join(stable_dir, "server.py")
    src = os.path.realpath(__file__)
    if src != os.path.realpath(stable):
        shutil.copy2(src, stable)
    # The EXTENSION is the single registration surface (Desktop chat AND Cowork).
    dc = desktop_config_path()
    if os.path.exists(dc):
        try:
            conf = json.load(open(dc, encoding="utf-8"))
            if conf.get("mcpServers", {}).pop("claude-codex-bridge", None) is not None:
                shutil.copy2(dc, dc + ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
                with open(dc, "w", encoding="utf-8") as f:
                    json.dump(conf, f, indent=2)
                print("removed redundant raw mcpServers entry (the extension is the single surface)")
        except json.JSONDecodeError:
            print(f"warning: {dc} is not valid JSON; left untouched", file=sys.stderr)
    mcpb_path = os.path.join(stable_dir, "claude-codex-bridge.mcpb")
    build = os.path.join(stable_dir, "_build")
    os.makedirs(build, exist_ok=True)
    shutil.copy2(stable, os.path.join(build, "server.py"))
    repo_manifest = os.path.join(os.path.dirname(os.path.realpath(__file__)), "manifest.json")
    if os.path.exists(repo_manifest):
        manifest = json.load(open(repo_manifest, encoding="utf-8"))  # canonical: repo file (CI packs from the same one)
    else:
        manifest = {
            "manifest_version": "0.4", "name": "codex", "display_name": "Codex",
            "version": SERVER_INFO["version"],
            "description": "Run OpenAI codex as an interactive subagent on this machine — sessions, per-turn JSON, approvals routed to the caller.",
            "author": {"name": "Alexander Stulov"},
            "server": {"type": "python", "entry_point": "server.py",
                       "mcp_config": {"command": "python3", "args": ["${__dirname}/server.py"],
                                      "platform_overrides": {"win32": {"command": "python"}}}},
            "tools": [{"name": t["name"]} for t in TOOLS],
            "compatibility": {"platforms": ["darwin", "win32"]},
        }
    with open(os.path.join(build, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    npx = shutil.which("npx") or "npx"          # resolves npx.cmd on Windows
    packed = subprocess.run([npx, "-y", "@anthropic-ai/mcpb@latest", "pack", build, mcpb_path],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    # Retire the 0.3.x queue watcher — the registered extension is the only transport now.
    for plist in (os.path.expanduser("~/Library/LaunchAgents/dev.claude-codex-bridge.plist"),
                  os.path.expanduser("~/Library/LaunchAgents/com.lexforge.codex-bridge.plist")):
        if sys.platform == "darwin" and os.path.exists(plist):
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            os.remove(plist)
            print(f"retired queue watcher: {os.path.basename(plist)}")
    loose = os.path.expanduser("~/Library/Application Support/Claude/Claude Extensions/dev.lexforge.codex-bridge")
    if os.path.isdir(loose):
        shutil.rmtree(loose, ignore_errors=True)
    if packed.returncode == 0 and os.path.exists(mcpb_path):
        print(f"extension package built: {mcpb_path}")
        print("  ACTION NEEDED (one-time, GUI — only you can do this): open that .mcpb —")
        print(f"    {open_instructions(mcpb_path)}")
        print("  Installing through the UI registers it so Cowork sessions can see it. Restart Claude Desktop after.")
    else:
        print(f"extension: could not build .mcpb (npx/mcpb unavailable: {packed.stderr.strip()[:120]})")
        print(f"  build dir ready at {build}; run: npx -y @anthropic-ai/mcpb@latest pack '{build}' '{mcpb_path}'")
    print(f"installed: bridge copied to {stable}")
    print("no configuration needed — cwd may be any existing directory on this machine.")
    return 0


def uninstall():
    for plist in (os.path.expanduser("~/Library/LaunchAgents/dev.claude-codex-bridge.plist"),):
        if sys.platform == "darwin" and os.path.exists(plist):
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            os.remove(plist)
            print("watcher unloaded and plist removed.")
    dc = desktop_config_path()
    if os.path.exists(dc):
        try:
            conf = json.load(open(dc, encoding="utf-8"))
            if conf.get("mcpServers", {}).pop("claude-codex-bridge", None) is not None:
                shutil.copy2(dc, dc + ".backup-" + time.strftime("%Y%m%d-%H%M%S"))
                with open(dc, "w", encoding="utf-8") as f:
                    json.dump(conf, f, indent=2)
                print("unregistered from Claude Desktop config (backup saved).")
        except json.JSONDecodeError:
            pass
    print("uninstall the registered extension via Settings -> Extensions.")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        sys.exit(install(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--uninstall":
        sys.exit(uninstall())
    log(f"starting {SERVER_INFO['version']}; scratch={SCRATCH_ROOT}")
    # UTF-8 whatever the locale says: Claude Desktop speaks UTF-8, and on Windows the
    # default would be the ANSI code page.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                log(f"skipping non-JSON line: {line[:80]}")
                continue
            rid = req.get("id")
            try:
                result = handle(req)
                if rid is None:
                    continue
                resp = {"jsonrpc": "2.0", "id": rid, "result": result}
            except Exception as e:  # noqa: BLE001 — a bad request must never kill the server
                if rid is None:
                    log(f"notification failed: {e}")
                    continue
                resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
            print(json.dumps(resp), flush=True)
    finally:
        # stdin closed, or stdout went away mid-reply: Claude Desktop is gone either
        # way. Let the child go the same way.
        APP.shutdown()


if __name__ == "__main__":
    main()
