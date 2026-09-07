#!/usr/bin/env bash
# Raise the codex context window for GPT-6 Astra and GPT-5.6 models to 1.05M.
#
# Why a script and not a config line: codex clamps the documented override.
#
#     model_context_window = 1050000      ->  min(1050000, max_context_window)
#
# and the shipped catalog caps every one of these models below that, so the
# override cannot reach 1,050,000. Out of the box they all run at a 272000 window
# (258400 after codex's 95% headroom) — gpt-6-astra included, despite being the
# flagship. The cap itself lives in the model catalog, and the only supported way
# to change it is to hand codex a whole catalog via `model_catalog_json`. That is
# what this does: it has codex fetch its catalog afresh, copies that, raises the
# cap on every model the bridge dispatches to the 1,050,000 their upstream API
# actually supports, and points config.toml at the copy.
#
# Run once. Re-run after a codex upgrade, so the copy picks up new models.
#
#   ./scripts/enable-1m-context.sh              raise the cap
#   ./scripts/enable-1m-context.sh --revert     undo (restores the stock catalog)
#   ./scripts/enable-1m-context.sh --no-verify  skip the live check (no API call)
#
# Windows: scripts/enable-1m-context.ps1 (same behaviour; -Revert / -NoVerify).
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CACHE="$CODEX_HOME/models_cache.json"
CATALOG="$CODEX_HOME/catalog-1m.json"
CONFIG="$CODEX_HOME/config.toml"
TARGET_WINDOW=1050000
REVERT=0
VERIFY=1
VERIFY_PICK="$(mktemp)"                # which raised model the live check should use
ORIG_CONFIG="$(mktemp)"                # config.toml as found, for the failure path
RESTORE=0                              # 1 while the override is lifted and not yet re-set
tmp=""
cleanup() {
  if [ "$RESTORE" -eq 1 ]; then cp "$ORIG_CONFIG" "$CONFIG"; fi
  rm -f "$VERIFY_PICK" "$ORIG_CONFIG"
  if [ -n "$tmp" ]; then rm -rf "$tmp"; fi
}
trap cleanup EXIT

for arg in "$@"; do
  case "$arg" in
    --revert) REVERT=1 ;;
    --no-verify) VERIFY=0 ;;
    -h|--help) sed -n '2,/^# Windows:/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v codex   >/dev/null || { echo "the codex CLI is not on PATH" >&2; exit 1; }

# --- edit config.toml -------------------------------------------------------
# A bare key must appear before the first [table] in TOML, so this inserts at the
# top rather than appending. Always writes through a backup.
set_config() {                       # set_config <value|-> ; "-" removes the key
  python3 - "$CONFIG" "$1" <<'PY'
import os, shutil, sys
path, value = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True) if os.path.exists(path) else []
if lines:
    shutil.copy(path, path + ".bak")
kept, replaced = [], False
for line in lines:
    if line.lstrip().startswith("model_catalog_json"):
        if value != "-" and not replaced:
            kept.append(f'model_catalog_json = "{value}"\n')
            replaced = True
        continue                      # drop any stale duplicates
    kept.append(line)
if value != "-" and not replaced:
    # before the first [table] header, or at the very top
    at = next((i for i, l in enumerate(kept) if l.lstrip().startswith("[")), 0)
    kept.insert(at, f'model_catalog_json = "{value}"\n')
    if at == 0 and len(kept) > 1:
        kept.insert(1, "\n")
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w").write("".join(kept))
PY
}

if [ "$REVERT" -eq 1 ]; then
  set_config -
  rm -f "$CATALOG"
  echo "reverted: model_catalog_json removed from $CONFIG (backup at $CONFIG.bak)"
  echo "codex is back to its stock catalog — 272000 cap, 258400 effective."
  exit 0
fi

# --- refresh the model cache ------------------------------------------------
# codex fetches /models only while model_catalog_json is unset. With the override
# installed it serves the catalog file and never touches models_cache.json, so the
# cache stays frozen at whatever it held the day the override went in, and a re-run
# after a codex upgrade would copy that stale snapshot and never see a model that
# appeared since (how gpt-6-astra stayed at 258400 while the check kept passing on
# gpt-5.6-sol). So lift the override for one `codex debug models` — a catalog
# fetch, no model turn — and put it back. Logged out, codex prints its bundled
# catalog instead and writes nothing, which is why the copy is taken from the cache
# a real fetch writes and never from that output.
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$ORIG_CONFIG"
  if grep -q '^[[:space:]]*model_catalog_json' "$CONFIG"; then
    RESTORE=1
    set_config -
  fi
fi
if ! refresh_err="$(codex debug models 2>&1 >/dev/null)"; then
  echo "WARNING: 'codex debug models' failed, so the model cache could not be refreshed:" >&2
  printf '%s\n' "$refresh_err" | tail -2 >&2
fi

if [ ! -f "$CACHE" ]; then
  echo "no model cache at $CACHE" >&2
  echo "codex did not fetch its catalog — is it logged in ('codex login') and online? Fix that and re-run." >&2
  exit 1
fi

# --- build the catalog ------------------------------------------------------
# Sourced from the cache codex itself wrote, so the shape always matches the
# installed CLI. Building it from a checked-out models.json does not: the field
# set drifts between versions and codex rejects the file outright.
python3 - "$CACHE" "$CATALOG" "$TARGET_WINDOW" "$VERIFY_PICK" <<'PY'
import datetime, json, sys
cache, out_path, target, pick_path = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
# Every wire model this bridge can dispatch that supports the 1.05M upstream window.
# A prefix tuple, not one family: gpt-6-astra ships capped exactly like the GPT-5.6
# seats, so gating on "gpt-5.6-" alone would leave the flagship silently at 258400.
RAISE = ("gpt-6-astra", "gpt-5.6-")
data = json.load(open(cache))
models = data.get("models") or []
if not models:
    sys.exit(f"{cache} contains no models — cannot build a catalog from it")
fetched, version = str(data.get("fetched_at", "?")), str(data.get("client_version", "?"))
print(f"cache: {cache}  (fetched {fetched} by codex {version})")
# A fetch that just happened stamps the cache with now; codex also keeps a cache under
# five minutes old as-is. Older than that means the refresh above did not happen.
try:
    stamp = datetime.datetime.strptime(fetched[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - stamp
except ValueError:
    age = None
if age is None or age > datetime.timedelta(minutes=10):
    print(f"WARNING: codex did not refresh its model cache (not logged in, offline, or a CLI without "
          f"'debug models'); building from the copy it fetched {fetched}. Models that appeared "
          f"since are not in it — fix the cause and re-run to pick them up.", file=sys.stderr)
raised = []
for m in models:
    slug = str(m.get("slug", ""))
    if slug.startswith(RAISE):
        before = m.get("context_window")
        m["context_window"] = target
        m["max_context_window"] = target
        raised.append((slug, before))
slugs = [s for s, _ in raised]
for prefix in RAISE:
    if not any(s.startswith(prefix) for s in slugs):
        print(f"WARNING: no {prefix}* model in the catalog codex fetched, so it stays at the stock "
              f"258400 (a CLI too old to list it, or an account without access to it).", file=sys.stderr)
if not raised:
    sys.exit(f"no {' or '.join(RAISE)}* models in the catalog — nothing to raise")
json.dump({"models": models}, open(out_path, "w"), indent=1)
# Verify against a model that was actually raised, preferring the one callers reach
# for by default. Hard-coding a family here is how the check goes on passing while
# the model people actually use stays capped.
open(pick_path, "w").write(next((s for s in RAISE if s in slugs), slugs[0]))
print(f"catalog: {out_path}  ({len(models)} models, {len(raised)} raised)")
for slug, before in raised:
    print(f"  {slug:16} context_window {before} -> {target}")
PY
VERIFY_MODEL="$(cat "$VERIFY_PICK")"

set_config "$CATALOG"
RESTORE=0
echo "config: model_catalog_json set in $CONFIG"

# --- verify -----------------------------------------------------------------
# The catalog is parsed at startup, so a typo here is a hard failure on the next
# codex run. Prove it loads and that the window really moved, rather than
# assuming: a wrong answer here is silent until something important breaks.
if [ "$VERIFY" -eq 0 ]; then
  echo
  echo "skipped verification. Restart Claude Desktop so the bridge spawns a fresh app-server."
  exit 0
fi

echo
echo "verifying with a one-word codex run on $VERIFY_MODEL..."
tmp="$(mktemp -d)"
if ! out="$(cd "$tmp" && codex exec --json --model "$VERIFY_MODEL" --skip-git-repo-check \
        -C . "Reply only with OK." </dev/null 2>&1)"; then
  echo "FAILED: codex could not start with the new catalog" >&2
  printf '%s\n' "$out" | tail -3 >&2
  echo "restore with: $0 --revert" >&2
  exit 1
fi

tid="$(printf '%s' "$out" | python3 -c 'import json,sys
for l in sys.stdin:
    if "thread.started" in l:
        print(json.loads(l)["thread_id"]); break')"
roll="$(find "$CODEX_HOME/sessions" -name "*$tid*" 2>/dev/null | head -1)"
window="$(grep -o '"model_context_window":[0-9]*' "$roll" 2>/dev/null | head -1 | cut -d: -f2)"
expected=$(( TARGET_WINDOW * 95 / 100 ))

if [ "$window" = "$expected" ]; then
  echo "OK  effective window is now $window (= $TARGET_WINDOW x 95% headroom), was 258400."
  echo
  echo "Restart Claude Desktop so the bridge spawns a fresh app-server — the catalog"
  echo "is read at startup only."
else
  echo "UNEXPECTED: effective window reported as ${window:-unknown}, expected $expected" >&2
  echo "the catalog may not have been picked up. Restore with: $0 --revert" >&2
  exit 1
fi
