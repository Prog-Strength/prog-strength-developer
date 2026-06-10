"""
install_plugins — stage Claude Code plugins for the worker's developer user.

Reads bootstrap/plugins.json and, for each entry, clones the marketplace
repo at a pinned SHA into the developer's plugin cache so that Claude
Code in --print mode finds the named plugins (and their skills) without
any interactive `/plugin install` step.

Why pre-stage instead of running `claude plugin install` headlessly:
the CLI install path assumes a session and an interactive permission
prompt; the file layout under ~/.claude/plugins/ is well-defined enough
that mirroring it directly is the more reliable headless option. If
upstream introduces a non-interactive install command later, swap this
out for that.

Designed to be importable for tests (build_installed_plugins_entry,
build_marketplaces_entry, parse_manifest) and runnable as a script
(python3 install_plugins.py /path/to/plugins.json [--home /home/dev]).

Failure mode: a single plugin failing to install logs a warning and
returns a non-fatal exit; the caller in userdata.sh.tpl uses `|| true`
so the SOW run continues with whatever skills did install.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLUGINS_BASENAME = ".claude/plugins"
INSTALLED_PLUGINS_FILE = "installed_plugins.json"
KNOWN_MARKETPLACES_FILE = "known_marketplaces.json"


@dataclass(frozen=True)
class PluginSpec:
    name: str
    marketplace: str
    marketplace_repo: str
    marketplace_sha: str
    version: str
    plugin_subdir: str


def parse_manifest(path: Path) -> list[PluginSpec]:
    raw = json.loads(Path(path).read_text())
    out: list[PluginSpec] = []
    for entry in raw.get("plugins", []):
        out.append(
            PluginSpec(
                name=entry["name"],
                marketplace=entry["marketplace"],
                marketplace_repo=entry["marketplace_repo"],
                marketplace_sha=entry["marketplace_sha"],
                version=entry["version"],
                plugin_subdir=entry.get("plugin_subdir", ""),
            )
        )
    return out


def build_installed_plugins_entry(spec: PluginSpec, install_path: Path) -> dict[str, Any]:
    """Shape one entry for installed_plugins.json's `plugins` map."""
    return {
        "scope": "user",
        "installPath": str(install_path),
        "version": spec.version,
        "gitCommitSha": spec.marketplace_sha,
    }


def build_marketplaces_entry(spec: PluginSpec, install_location: Path) -> dict[str, Any]:
    """Shape one entry for known_marketplaces.json keyed by marketplace name."""
    return {
        "source": {
            "source": "github",
            "repo": spec.marketplace_repo,
        },
        "installLocation": str(install_location),
    }


def _run_git(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _checkout_at_sha(spec: PluginSpec, tmp: Path) -> Path:
    """Clone marketplace_repo at marketplace_sha into tmp; return clone path."""
    clone = tmp / spec.marketplace
    _run_git(
        [
            "clone",
            "--quiet",
            f"https://github.com/{spec.marketplace_repo}.git",
            str(clone),
        ]
    )
    _run_git(["checkout", "--quiet", spec.marketplace_sha], cwd=clone)
    return clone


def _stage_plugin_cache(spec: PluginSpec, clone: Path, plugins_root: Path) -> Path:
    cache = plugins_root / "cache" / spec.marketplace / spec.name / spec.version
    if cache.exists():
        shutil.rmtree(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    src = clone if not spec.plugin_subdir else clone / spec.plugin_subdir
    if not src.exists():
        raise FileNotFoundError(
            f"plugin_subdir '{spec.plugin_subdir}' missing in cloned marketplace"
        )
    # copytree refuses to overwrite, hence the rmtree above. .git lives at
    # the clone root; if plugin_subdir is empty we'd otherwise copy it
    # too, ballooning the cache and confusing claude's loaders.
    shutil.copytree(src, cache, ignore=shutil.ignore_patterns(".git"))
    return cache


def _stage_marketplace_clone(spec: PluginSpec, clone: Path, plugins_root: Path) -> Path:
    mkt = plugins_root / "marketplaces" / spec.marketplace
    if mkt.exists():
        shutil.rmtree(mkt)
    mkt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(clone, mkt)
    return mkt


def _merge_installed_plugins(plugins_root: Path, key: str, entry: dict[str, Any]) -> None:
    """Update installed_plugins.json with one plugin entry, preserving siblings."""
    path = plugins_root / INSTALLED_PLUGINS_FILE
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = {"version": 2, "plugins": {}}
    data.setdefault("version", 2)
    data.setdefault("plugins", {})
    data["plugins"][key] = [entry]
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merge_marketplaces(plugins_root: Path, name: str, entry: dict[str, Any]) -> None:
    path = plugins_root / KNOWN_MARKETPLACES_FILE
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = {}
    data[name] = entry
    path.write_text(json.dumps(data, indent=2) + "\n")


def install_one(spec: PluginSpec, plugins_root: Path) -> None:
    """Clone + stage one plugin. Raises on any failure."""
    plugins_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        clone = _checkout_at_sha(spec, tmp)
        cache = _stage_plugin_cache(spec, clone, plugins_root)
        mkt = _stage_marketplace_clone(spec, clone, plugins_root)
    _merge_installed_plugins(
        plugins_root,
        f"{spec.name}@{spec.marketplace}",
        build_installed_plugins_entry(spec, cache),
    )
    _merge_marketplaces(plugins_root, spec.marketplace, build_marketplaces_entry(spec, mkt))


def install_all(manifest: Path, home: Path) -> int:
    """Install every plugin in manifest. Returns count of failures."""
    plugins_root = home / PLUGINS_BASENAME
    specs = parse_manifest(manifest)
    failures = 0
    for spec in specs:
        try:
            install_one(spec, plugins_root)
            logging.info("installed %s@%s", spec.name, spec.marketplace)
        except (subprocess.CalledProcessError, OSError, KeyError, json.JSONDecodeError) as e:
            logging.warning("install failed for %s@%s: %s", spec.name, spec.marketplace, e)
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="path to plugins.json")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.path.expanduser("~")),
        help="user home (default: current user's home)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="[install_plugins] %(levelname)s %(message)s",
    )
    failures = install_all(args.manifest, args.home)
    if failures:
        logging.warning("%d plugin(s) failed to install", failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
