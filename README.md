# claude-codex-bridge

Dispatch OpenAI **codex** as an interactive subagent on your machine, from any
Claude surface — Claude Desktop chat, Cowork sessions, Claude Code. Sessions often
run in sandboxes where the codex CLI and its auth are unreachable; this bridge runs
codex where it lives.

One Python file, stdlib only.

## Prerequisites

The bridge shells out to the **codex CLI on your machine** — it ships no model
access of its own, so codex must be installed and signed in first.

**Python 3**, as `python3` on macOS or `python` on Windows. The bridge is one
stdlib-only file, so that is all it needs — but it must be a real interpreter:
`python --version` in a fresh terminal has to print a version. On a new Windows
install `python` is a Microsoft Store placeholder until Python is actually installed,
and Claude Desktop refuses to install a Python extension it cannot find one for.

**Install codex** — macOS or Linux:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

Also on npm (`npm install -g @openai/codex`) and Homebrew (`brew install codex`).
See [openai/codex](https://github.com/openai/codex) for details.

**Then sign in:**

```bash
codex login                      # opens a browser; sign in with ChatGPT or an API key
```

Verify before installing the bridge:

```bash
codex --version                  # 0.153.1 or newer, for the astra-* models
codex login status               # should report an authenticated account
```

`codex doctor` diagnoses a broken install, config or auth. If `codex` is not on
`PATH`, `codex_check` reports `ready: false` and every dispatch fails with
`codex is not on PATH on the host`. Claude Desktop reads `PATH` once, when it starts:
install codex before launching Desktop, or restart Desktop afterwards. On Windows the
bridge also looks in the installer's default location,
`%LOCALAPPDATA%\Programs\OpenAI\Codex\bin`, so a codex installed after Desktop
started is still found there.

**Windows only — enable codex's sandbox.** codex ships its Windows sandbox switched
off, and with it off it would run every command with no filesystem boundary. The
bridge refuses to dispatch until codex reports the sandbox ready — `codex_check`
shows `windows_sandbox` and the fix. Enable it once in
`%USERPROFILE%\.codex\config.toml`:

```toml
[windows]
sandbox = "unelevated"
```

No restart needed: the bridge asks codex for the current setting before every
thread it starts or resumes. Needs Windows 10 1809 or later.

## Install

**From a release (easiest):** download the latest `claude-codex-bridge-vX.Y.Z.mcpb`
from [Releases](https://github.com/Alexander-Stulov/claude-codex-bridge/releases).
The same file installs on macOS and Windows:

- **macOS:** double-click it — Claude Desktop opens the extension installer.
- **Windows:** Claude Desktop → Settings → Extensions → Advanced settings →
  Install Extension → pick the file. Windows has no `.mcpb` file association, so
  double-clicking the file does nothing.

Approve the install, then restart Claude Desktop.

**Keep the file on a local drive.** Claude Desktop refuses extension files on network
paths (`\\server\share`) with "Extension Preview Failed" — and a Downloads folder
redirected to one counts, which Parallels shared profiles and corporate folder
redirection both do. Copy the file to a local folder such as
`%USERPROFILE%\.claude-codex-bridge\` first.

### From source

```bash
python3 server.py --install        # Windows: python server.py --install
```

Idempotent. It copies the server to `~/.claude-codex-bridge/` and builds
`~/.claude-codex-bridge/claude-codex-bridge.mcpb`. There is nothing to configure —
no roots to register, no paths to declare.

**Then two one-time GUI steps** (Cowork only sees *registered* extensions —
copying files cannot register one):

1. Open the built file — macOS: `open ~/.claude-codex-bridge/claude-codex-bridge.mcpb`;
   Windows: Claude Desktop → Settings → Extensions → Advanced settings →
   Install Extension → `%USERPROFILE%\.claude-codex-bridge\claude-codex-bridge.mcpb`
   → approve the install
2. Restart Claude Desktop

`--uninstall` reverses everything it can non-interactively.

## Optional: raise the context window

Every [release](https://github.com/Alexander-Stulov/claude-codex-bridge/releases)
carries both editions of the script next to the `.mcpb`, so no clone is needed.
On macOS:

```bash
curl -fsSLO https://github.com/Alexander-Stulov/claude-codex-bridge/releases/latest/download/enable-1m-context.sh
bash enable-1m-context.sh
```

On Windows, run the PowerShell edition — same behaviour, same checks:

```powershell
Invoke-WebRequest https://github.com/Alexander-Stulov/claude-codex-bridge/releases/latest/download/enable-1m-context.ps1 -OutFile enable-1m-context.ps1
powershell -ExecutionPolicy Bypass -File enable-1m-context.ps1
```

From a checkout, run them in place: `./scripts/enable-1m-context.sh`, or
`powershell -ExecutionPolicy Bypass -File scripts\enable-1m-context.ps1`.

Codex ships GPT-6 Astra and GPT-5.6 Sol, Terra and Luna alike at a **272,000**-token
context window (258,400 after its 95% headroom), even though the models support
**1,050,000** upstream. Astra is no exception: a fresh astra thread reports
`model_context_window: 258400` until this script runs. The documented override does
*not* lift it to 1.05M either:

```toml
model_context_window = 1050000     # clamped to the catalog's max_context_window
```

because codex computes `min(requested, max_context_window)`, and the catalog's
per-model ceiling is below 1.05M. The cap lives in the model catalog, so the script
has codex fetch its catalog afresh, copies that, raises both the window and its
ceiling on every model the bridge dispatches, and points `config.toml` at the copy
via `model_catalog_json`. It then verifies with a one-word codex run — on the raised
model you are most likely to use — that the effective window really moved to
**997,500**, and refuses to claim success otherwise.

Run it once, then restart Claude Desktop — the catalog is read at app-server
startup. Re-run after upgrading codex so the copy picks up new models: codex stops
refreshing its own model cache while `model_catalog_json` is set, so the script
lifts the override for that one catalog fetch and puts it back — and it says so,
loudly, when a model it meant to raise is not in what codex fetched.
`--revert` restores the stock catalog; `--no-verify` skips the live check
(`-Revert` and `-NoVerify` in the PowerShell edition).

This step is **optional**, and it has a trade-off worth knowing about: it will
consume your OpenAI usage quota faster. Raising the cap removes the ceiling that
was keeping threads small — codex now lets a thread grow to ~945,000 tokens before
it compacts, and every turn re-sends the whole conversation as input. A long thread
that used to compact down at 244,800 tokens now keeps sending a context up to ~4x
larger on each subsequent turn, so the same work draws down your ChatGPT plan
limits (or API spend) noticeably faster. That is entirely between you and OpenAI —
this bridge charges nothing and sits in the middle. Raise the cap when you actually
need long-context runs; `--revert` puts the stock catalog back.

Everything works without the script — you just get the 272K cap. To confirm it
reached the bridge (not only `codex exec`), run `python3 tests/context_live.py`.

## Tools

| Tool | Purpose |
|---|---|
| `codex_check` | Preflight: codex present, version, models, threads in flight |
| `codex_capabilities` | What codex can do here: plugins and their skills, MCP servers and tools, connected apps |
| `codex_submit` | Start a thread, add a turn to one, or steer a running turn. Returns immediately |
| `codex_poll` | State, progress snapshot, pending approvals, and the complete output once done |
| `codex_approve` | Rule on an approval the thread is parked on — a command, file change, permission, or an MCP server's elicitation |
| `codex_interrupt` | Stop the active turn; the thread stays usable |
| `codex_compact` | Summarise the thread's history now, at a boundary you choose |

Models: `astra-medium|astra-high|astra-xhigh|astra-max|astra-ultra` ·
`sol-high|sol-xhigh|sol-ultra` · `terra-medium|terra-high` ·
`luna-medium|luna-high`.

Scout with `luna-medium/high` whatever the follow-up is — gather context, locate
the relevant code, extract facts — then hand the findings on: `terra-medium/high`
for everyday work, `sol-high/xhigh` for complex implementation and debugging,
`astra-high/xhigh` for hard or ambiguous work, long-context runs, terminal-heavy
agentic work, and independent review. Shape it as a pyramid: many cheap scouts,
one per area, each finishing inside its runway and returning findings verbatim;
fewer, stronger threads then process those results, and so on up. Split for
parallelism, not for length — coherent, closely-coupled work belongs in one thread,
and what matters survives compaction.

`astra-*` is GPT-6 Astra (codex 0.153.1+, codex's own default), exposed from
`medium` up — there is no `astra-low`; Luna is the scout. `max` rarely improves on `xhigh`. `ultra` (Sol
and Astra) is not "more than max": each agent runs at `xhigh` and proactive
sub-agent delegation switches on — breadth, not depth. It pays only when the work
splits into substantial independent parts; on small or tightly-coupled work it
costs more, takes as long, and is no better than `max`.

`run-codex-job` defaults to `astra-xhigh`; `codex_submit` always requires an
explicit model.

MCP tool calls hard-timeout at 300s, so nothing blocks: `codex_submit` returns at
once and `codex_poll` reports progress. Polls are idempotent snapshots — nothing is
consumed, nothing needs stitching together, and a missed poll costs nothing.

## Threads

A thread is the unit of work and its id is the handle. codex stores threads, not
this bridge, so a thread can be picked up tomorrow or next week: pass the id to
`codex_poll` or `codex_submit` and the bridge re-attaches it (`resumed: true`),
recovering the thread's real model, effort and working directory from codex.

Filling the context window does not end a thread, and there is no size to keep a
thread under. codex summarises the history in place and continues under the same id.

`codex_poll` reports the room left rather than how full the thread is:

```json
"context": { "tokens_used": 14173, "tokens_before_compaction": 930827 }
```

A runway is actionable in a way a percentage is not — "84% full" reads as an
emergency and invites abandoning a thread that is fine. While the runway is large,
just keep going. As it shrinks, decide: let it compact, have codex write findings to
a file first, or call `codex_compact` between turns to take the summary at a clean
point instead of mid-task. `activity.compactions` counts summaries already taken;
after one, early turns exist only as a summary, so restate anything exact.

The threshold is **derived**, not reported: the wire carries only the effective
window (raw × 95%) while codex compacts at raw × 90%. Exact for both real windows
(258,400 → 244,800 and 997,500 → 945,000) and asserted in `tests/smoke.py`, which is
the thing that will notice if a codex upgrade moves either percentage. It is an upper
bound — a `model_auto_compact_token_limit` in your `config.toml` is invisible on the
wire and would compact sooner. Erring high is deliberate: an early compaction costs
nothing, while understating the runway causes the premature thread-splitting this
number exists to prevent.

Running out of room is **not** a failure mode. codex checks the budget before a turn's
first model call (`run_pre_sampling_compact`) and again mid-turn whenever the agent
wants another step, summarising rather than erroring, and it reserves a buffer so
there is always room left to perform the summary. Verified by forcing the budget down
to 20,000 tokens: the thread compacted itself on four consecutive turns and every one
returned the right answer.

## Deliverables

Ask for the answer as the turn's **output**, not as a report file. Output is capped
by the model's own output limit, arrives whole through `codex_poll`, and its shape
can be *enforced* with `output_schema` — a file has none of that, and the bridge
cannot even see one. Write files only when the file is the work product itself
(source changes, a document that belongs in the repo).

Measured on one 120-function survey:

| brief shape | size | at 5,000 items | enforced? |
|---|---:|---:|---|
| unbounded file | 16.7 KB | ~750 KB | no |
| file, one row per item | 11.2 KB | ~500 KB | no |
| file, cap + aggregate fallback | 0.85 KB | ~1 KB | advisory |
| output under `output_schema` | 1.2 KB | ~1 KB | **structural** |

A per-item budget is not the fix — it stays linear and only buys ~1.5×. What works
is a hard cap plus an explicit instruction for when the items *don't* fit: counts
and patterns plus the exceptions worth naming, never a truncated enumeration. Under
`output_schema`, `maxItems`/`maxLength` make that a constraint rather than a request.

## Where it runs

`cwd` is the only location knob: any existing directory, nothing to register. Reads
see the surrounding repo; writes are confined to `cwd`. Omit it and the thread gets
a private scratch workspace. Point concurrent write threads at different `cwd`
(separate worktrees, say) and they cannot collide.

`mode` is `write` (default) or `read`; both keep network access and the full tool
surface. In-scope work never asks for approval — a request means codex hit the
sandbox boundary or a suspicious-command rule. An MCP server codex is using can
also park the thread on an *elicitation* — a permission prompt (a browser plugin
asking to open a tab, say) or a short form — and it routes to the caller the same
way: `allow` accepts, `deny` declines, `allow_always` has it remembered for the
session when the request offers that, and `grant` carries a form's answers.

Containment is codex's own sandbox, not the bridge's: Seatbelt on macOS, always on;
codex's restricted-token sandbox on Windows, which is opt-in (see Prerequisites) —
the bridge will not dispatch without it. Two edges of that sandbox worth knowing:
in `write` mode the system temp directory stays writable alongside `cwd` (`/tmp`
and `$TMPDIR` on macOS, `%TEMP%` on Windows), which is codex's default; and on
Windows, hosts with unusually permissive ACLs can weaken `read` mode's write
denial — an exception codex's own test suite documents. Approvals are the
deliberate exception to all of it: a command you approve, or one matched by an
execpolicy allow rule — including every class `allow_class` grants — runs outside
the sandbox.

## Plugins, skills and apps

codex brings its own plugins, MCP servers and connected ChatGPT apps, and the
bridge exposes them as they are. `codex_capabilities` asks the app-server what is
installed and returns one map: every enabled plugin with the skills it
contributes, every MCP server with its tools, the connected apps, and the standing
facts (network is always on). `query` narrows it to matching skills, tools, plugins
and apps and adds their descriptions. Call it before briefing work that might lean
on one.

A skill is invoked by passing its name in `codex_submit`'s `skills` —
`["deep-research-work:deep-research"]`, or just `["deep-research"]` when that is
unique — and the bridge sends its instructions with the turn's input as the
structured skill item codex's own clients use. A `$skill` typed into the prompt is
*not* honoured through the app-server: two live threads asked to quote the heading
of the skill such a mention loaded answered NONE, while the structured item made
the model quote it. Skills also fire implicitly when the brief matches their
description. An app is mentioned in the prompt as `[$Name](app://connector_id)`;
MCP tools by name. Plugins are installed and enabled in codex itself (`codex plugin list|add`,
the `[plugins."name@marketplace"]` tables in `~/.codex/config.toml`, or the ChatGPT
app); the bridge changes nothing there.

Three are worth knowing:

- **Deep Research** (`skills: ["deep-research"]`) — OpenAI Deep Research inside codex:
  multi-pass web research with cited sources, which Cowork and Claude Code threads
  lack natively. Through codex it is metered against the account's Codex/Work
  usage allowance rather than the Chat deep-research task quota (OpenAI help
  center, September 2026; see `docs/research/2026-09-13-deep-research-quota.md`),
  and it is the most expensive thing a thread does — one run reads well over a
  million tokens — so spend it on questions that merit it, and run it on
  `sol-high`, `sol-xhigh` or `astra-*` rather than a scout. Ask for the report in chat ("no
  document, deck or site") with a Sources section; `codex_poll` returns it whole.
  When it is worth keeping — it usually is — add a turn on the same thread with
  `cwd` set to the project and ask codex to save the report to markdown
  (`docs/research/<topic>.md`, say): it writes it verbatim with every source, in
  about a minute on `terra-medium`. Or write a condensed version yourself from the
  poll output. The bridge stores nothing. Its clarifying-question step (`request_user_input`) cannot reach the
  caller through the bridge yet, so tell it to state assumptions and proceed.
- **Chrome** — the user's real Google Chrome through the ChatGPT Chrome extension:
  logged-in sessions, open tabs, page content. Ask for the Chrome plugin by name
  (the in-app Browser plugin is a separate, isolated browser). Each new site raises
  an elicitation (`tool: access_browser_origin`, `persist_modes: ["always"]`):
  `allow` grants once, `allow_class` grants that origin for good, `deny` blocks it.
- **Computer Use** — native macOS app control through the Codex Computer Use app.
  It runs through the same `cua_repl` `js` tool as Chrome (plugin
  `unified-computer-use`): ask for Computer Use by name and codex opens the app
  with `cua.getApp`. The first use of each app raises an elicitation (`tool:
  get_app_state`, `persist_modes: ["session", "always"]`): `allow` grants once,
  `allow_always` for the session, `allow_class` for good. A `computer-use` MCP
  server shown with no tools is a legacy `config.toml` entry, not the
  capability; the map marks such entries `source: config.toml`.

Documents, presentations, spreadsheets, PDF, visualize, sites and the rest appear
in the map with their mentions.

Network access is on in both modes, always — web search, HTTP, package installs,
git remotes; there is nothing to enable or approve. Containment is the sandbox
(writes confined to `cwd`), not the network.

## Guarantees

codex's credentials are never sent to Claude — they stay on this machine, used
only by codex itself · provenance is stamped by the bridge from
what codex reports, never from the model's self-report · unknown values are
reported as unknown rather than guessed · the final output arrives whole, once,
and is never truncated or reshaped.

## Model-facing documentation (built in)

The server documents itself to every Claude client at the protocol level — no extra
setup: the MCP `instructions` field (delivered at handshake) carries the usage
protocol (poll-don't-block, self-contained briefs, model guide, durability and
compaction semantics); tool descriptions repeat the load-bearing rules at the
decision points; and a `run-codex-job` prompt template appears in Claude Desktop's
prompt picker.

## Tests

```bash
python3 tests/smoke.py                  # protocol smoke, no codex needed (CI)
python3 tests/protocol_conformance.py   # every field and method we touch is real (CI)
python3 tests/windows_sim.py            # the Windows-only branches, on any OS (CI)
python3 tests/enable_1m_sim.py          # the 1M-context scripts, against a stand-in codex (CI)
python3 tests/context_live.py           # context reporting + the uplift (needs codex)
python3 tests/output_live.py            # large file writes and long final messages
python3 tests/resume_live.py            # pick a thread back up in a new process
python3 tests/lifecycle_live.py         # steer, interrupt, crash recovery, cold resume
```

## Releases

Merging to `master` publishes `v<version>` from `manifest.json` — a release is cut
whenever that version is one that has not been tagged before, so bumping the
version in `manifest.json` is what ships. Pushes to `master` that leave the
version alone build and test as usual and publish nothing.

Each release ships three assets: `claude-codex-bridge-v<version>.mcpb`, and the
`enable-1m-context.sh` / `enable-1m-context.ps1` scripts under unversioned names, so
`releases/latest/download/<script>` always points at the newest copy.
`tests/pack_check.py` checks all three before they are published.

Release notes come from `docs/release-notes/v<version>.md` when that file exists;
without it GitHub generates the usual list of merged PRs. Re-run the workflow
manually with **force** to re-publish a tag whose notes or assets need fixing.
