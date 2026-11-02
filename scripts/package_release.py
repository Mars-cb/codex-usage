#!/usr/bin/env python3
"""Validate and build deterministic marketplace and standalone plugin ZIPs."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins/codex-usage-panel"
FILES = ("plugin.json", "mcp.json", "server.py", "floating.py", "requirements.txt",
         "test_server.py", "test_floating.py", "LICENSE", "PRIVACY.md", "web/panel.html", "assets/icon.svg", "assets/preview.png")
REPO_FILES = ("README.md", "LICENSE", "PRIVACY.md", "start-floating.sh", ".agents/plugins/marketplace.json",
              "docs/PUBLISHING.md")


def validate(tag=None):
    manifest = json.loads((PLUGIN / "plugin.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Invalid plugin version")
    expected = "v" + version
    if tag and tag != expected:
        raise ValueError("Release tag {} does not match manifest {}".format(tag, expected))
    constants = {}
    for node in ast.parse((PLUGIN / "server.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Str, ast.Constant)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    if constants.get("VERSION") != version:
        raise ValueError("server.py VERSION differs from plugin.json")
    market = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert market["plugins"][0]["name"] == manifest["name"]
    assert market["plugins"][0]["source"]["path"] == "./plugins/codex-usage-panel"
    servers = json.loads((PLUGIN / "mcp.json").read_text())["mcpServers"]
    assert servers["quota"]["args"] == ["${PLUGIN_ROOT}/server.py"]
    for rel in FILES:
        if not (PLUGIN / rel).is_file():
            raise ValueError("Missing plugin file: " + rel)
    for rel in REPO_FILES:
        if not (ROOT / rel).is_file():
            raise ValueError("Missing marketplace file: " + rel)
    return expected


def archive(path, entries):
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as bundle:
        for source, target in sorted(entries, key=lambda item: item[1]):
            info = zipfile.ZipInfo(target, date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if source.suffix == ".sh" else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, source.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    tag = validate(args.tag or None)
    args.output.mkdir(parents=True, exist_ok=True)
    plugin_zip = args.output / ("codex-usage-panel-" + tag + ".zip")
    archive(plugin_zip, [(PLUGIN / rel, rel) for rel in FILES])
    repo_zip = args.output / ("codex-usage-" + tag + ".zip")
    prefix = "codex-usage-" + tag + "/"
    entries = [(ROOT / rel, prefix + rel) for rel in REPO_FILES]
    entries.extend((PLUGIN / rel, prefix + "plugins/codex-usage-panel/" + rel) for rel in FILES)
    archive(repo_zip, entries)
    hashes = []
    for path in (plugin_zip, repo_zip):
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
        print(path)
    (args.output / "SHA256SUMS").write_text("\n".join(hashes) + "\n")
    if args.github_output:
        with args.github_output.open("a") as stream:
            stream.write("tag=" + tag + "\n")


if __name__ == "__main__":
    main()
