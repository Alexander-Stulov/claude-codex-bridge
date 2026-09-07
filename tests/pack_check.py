#!/usr/bin/env python3
"""Check a packed .mcpb — the artifact CI uploads and a release ships — not the sources.

    python tests/pack_check.py dist/claude-codex-bridge.mcpb

The same bundle installs on macOS and Windows, so everything both platforms depend on
is pinned here: the archive holds exactly the two files at its root, server.py ships
byte-identical to the checkout with LF endings, and the manifest inside names both
platforms, the Windows interpreter override, the same tools the server registers and
the same version the server reports."""
import importlib.util, json, os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_bridge():
    spec = importlib.util.spec_from_file_location("bridge", os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(bundle):
    z = zipfile.ZipFile(bundle)
    assert z.testzip() is None, "corrupt archive"
    names = sorted(z.namelist())
    assert names == ["manifest.json", "server.py"], f"archive must hold exactly manifest.json and server.py at its root, got {names}"

    packed = z.read("server.py")
    assert b"\r\n" not in packed, "packed server.py has CRLF endings — pack from an LF checkout"
    with open(os.path.join(ROOT, "server.py"), "rb") as f:
        source = f.read().replace(b"\r\n", b"\n")
    assert packed == source, "packed server.py differs from the checkout"

    manifest = json.loads(z.read("manifest.json").decode("utf-8"))
    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as f:
        assert manifest == json.load(f), "packed manifest differs from manifest.json"

    for key in ("manifest_version", "name", "version", "description", "author", "server"):
        assert key in manifest, f"manifest lacks {key}"
    assert manifest["author"].get("name"), "manifest author needs a name"
    server = manifest["server"]
    assert server["type"] == "python" and server["entry_point"] == "server.py", server
    mc = server["mcp_config"]
    assert mc["command"] == "python3" and mc["args"] == ["${__dirname}/server.py"], mc
    assert mc["platform_overrides"]["win32"]["command"] == "python", \
        "Windows needs the interpreter override: python3 is not a real binary there"
    platforms = set(manifest["compatibility"]["platforms"])
    assert platforms >= {"darwin", "win32"}, f"manifest must list both platforms, got {sorted(platforms)}"

    bridge = load_bridge()
    manifest_tools = [t["name"] for t in manifest.get("tools", [])]
    served_tools = [t["name"] for t in bridge.TOOLS]
    assert manifest_tools == served_tools, f"manifest tools {manifest_tools} != server tools {served_tools}"
    assert manifest["version"] == bridge.SERVER_INFO["version"], \
        (manifest["version"], bridge.SERVER_INFO["version"])
    return manifest, packed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    manifest, packed = check(sys.argv[1])
    print(f"pack check: {sys.argv[1]} ok — v{manifest['version']}, {len(packed)} bytes server.py, "
          f"{len(manifest['tools'])} tools, platforms {manifest['compatibility']['platforms']}")
