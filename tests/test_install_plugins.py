import json
from pathlib import Path
from unittest import mock

import pytest

from bootstrap.install_plugins import (
    PluginSpec,
    build_installed_plugins_entry,
    build_marketplaces_entry,
    install_all,
    install_one,
    parse_manifest,
    _merge_installed_plugins,
    _merge_marketplaces,
)


SUPERPOWERS_SPEC = PluginSpec(
    name="superpowers",
    marketplace="superpowers-marketplace",
    marketplace_repo="obra/superpowers-marketplace",
    marketplace_sha="6be22035d873c31ca246db4f4932a1098aea46fc",
    version="5.1.0",
    plugin_subdir="",
)


def test_parse_manifest_reads_both_plugins(tmp_path):
    manifest = tmp_path / "plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "name": "superpowers",
                        "marketplace": "superpowers-marketplace",
                        "marketplace_repo": "obra/superpowers-marketplace",
                        "marketplace_sha": "abc",
                        "version": "5.1.0",
                        "plugin_subdir": "",
                    },
                    {
                        "name": "frontend-design",
                        "marketplace": "claude-plugins-official",
                        "marketplace_repo": "anthropics/claude-plugins-official",
                        "marketplace_sha": "def",
                        "version": "unknown",
                        "plugin_subdir": "plugins/frontend-design",
                    },
                ]
            }
        )
    )
    specs = parse_manifest(manifest)
    assert [s.name for s in specs] == ["superpowers", "frontend-design"]
    assert specs[1].plugin_subdir == "plugins/frontend-design"


def test_parse_manifest_defaults_plugin_subdir_to_empty(tmp_path):
    manifest = tmp_path / "plugins.json"
    manifest.write_text(json.dumps({"plugins": [{
        "name": "p", "marketplace": "m", "marketplace_repo": "o/r",
        "marketplace_sha": "s", "version": "v",
    }]}))
    spec = parse_manifest(manifest)[0]
    assert spec.plugin_subdir == ""
    # A plugin whose content is bundled in the marketplace repo declares no
    # separate content repo.
    assert spec.plugin_repo == ""
    assert spec.plugin_sha == ""


def test_parse_manifest_reads_separate_plugin_repo(tmp_path):
    manifest = tmp_path / "plugins.json"
    manifest.write_text(json.dumps({"plugins": [{
        "name": "superpowers", "marketplace": "superpowers-marketplace",
        "marketplace_repo": "obra/superpowers-marketplace", "marketplace_sha": "s",
        "version": "5.1.0", "plugin_subdir": "",
        "plugin_repo": "obra/superpowers", "plugin_sha": "content-sha",
    }]}))
    spec = parse_manifest(manifest)[0]
    assert spec.plugin_repo == "obra/superpowers"
    assert spec.plugin_sha == "content-sha"


def test_build_installed_plugins_entry_uses_install_path_string():
    entry = build_installed_plugins_entry(SUPERPOWERS_SPEC, Path("/home/dev/.claude/plugins/cache/x"))
    assert entry == {
        "scope": "user",
        "installPath": "/home/dev/.claude/plugins/cache/x",
        "version": "5.1.0",
        "gitCommitSha": "6be22035d873c31ca246db4f4932a1098aea46fc",
    }


def test_build_marketplaces_entry_includes_github_source():
    entry = build_marketplaces_entry(SUPERPOWERS_SPEC, Path("/home/dev/.claude/plugins/marketplaces/superpowers-marketplace"))
    assert entry["source"] == {"source": "github", "repo": "obra/superpowers-marketplace"}
    assert entry["installLocation"].endswith("superpowers-marketplace")


def test_merge_installed_plugins_preserves_sibling_entries(tmp_path):
    plugins_root = tmp_path
    (plugins_root / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "existing@market": [{"scope": "user", "installPath": "/old"}],
                },
            }
        )
    )
    _merge_installed_plugins(
        plugins_root,
        "new@market",
        {"scope": "user", "installPath": "/new", "version": "1", "gitCommitSha": "x"},
    )
    data = json.loads((plugins_root / "installed_plugins.json").read_text())
    assert set(data["plugins"].keys()) == {"existing@market", "new@market"}
    # Existing entry untouched
    assert data["plugins"]["existing@market"] == [{"scope": "user", "installPath": "/old"}]


def test_merge_installed_plugins_initializes_file_when_missing(tmp_path):
    _merge_installed_plugins(
        tmp_path,
        "p@m",
        {"scope": "user", "installPath": "/p", "version": "1", "gitCommitSha": "x"},
    )
    data = json.loads((tmp_path / "installed_plugins.json").read_text())
    assert data["version"] == 2
    assert "p@m" in data["plugins"]


def test_merge_marketplaces_overwrites_same_name(tmp_path):
    _merge_marketplaces(tmp_path, "m", {"source": {"source": "github", "repo": "a/b"}, "installLocation": "/v1"})
    _merge_marketplaces(tmp_path, "m", {"source": {"source": "github", "repo": "a/b"}, "installLocation": "/v2"})
    data = json.loads((tmp_path / "known_marketplaces.json").read_text())
    assert data["m"]["installLocation"] == "/v2"


def _fake_git_clone(spec_subdir, plugin_marker_path):
    """Build a fake `_run_git` that materializes a clone tree with a marker file
    at plugin_marker_path (relative to clone root). Returns the side-effect
    function to pass as Mock(side_effect=...)."""

    def fake(args, cwd=None):
        if args[:2] == ["clone", "--quiet"]:
            clone_path = Path(args[-1])
            clone_path.mkdir(parents=True, exist_ok=True)
            # Marker file so we can verify cache contents later.
            target = clone_path / plugin_marker_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("marker")
            # Simulate a .git dir so we can confirm it's filtered.
            (clone_path / ".git").mkdir(exist_ok=True)
            (clone_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
            return
        if args[:2] == ["checkout", "--quiet"]:
            # No-op; the marker file is already in place from the clone step.
            return
        raise AssertionError(f"unexpected git call: {args}")

    return fake


def test_install_one_stages_cache_marketplace_and_json(tmp_path):
    plugins_root = tmp_path / ".claude" / "plugins"
    fake = _fake_git_clone(SUPERPOWERS_SPEC, "skills/some-skill/SKILL.md")
    with mock.patch("bootstrap.install_plugins._run_git", side_effect=fake):
        install_one(SUPERPOWERS_SPEC, plugins_root)

    cache = plugins_root / "cache" / "superpowers-marketplace" / "superpowers" / "5.1.0"
    assert (cache / "skills" / "some-skill" / "SKILL.md").read_text() == "marker"
    # .git should be filtered from the cache copy.
    assert not (cache / ".git").exists()

    mkt = plugins_root / "marketplaces" / "superpowers-marketplace"
    assert (mkt / "skills" / "some-skill" / "SKILL.md").exists()

    installed = json.loads((plugins_root / "installed_plugins.json").read_text())
    assert "superpowers@superpowers-marketplace" in installed["plugins"]

    known = json.loads((plugins_root / "known_marketplaces.json").read_text())
    assert "superpowers-marketplace" in known


def test_install_one_with_separate_plugin_repo_stages_content_not_metadata(tmp_path):
    """When a plugin's content lives in its own repo (the marketplace repo only
    holds metadata, e.g. obra/superpowers-marketplace -> obra/superpowers), the
    cache must contain the content repo's skills, not the marketplace metadata."""
    spec = PluginSpec(
        name="superpowers",
        marketplace="superpowers-marketplace",
        marketplace_repo="obra/superpowers-marketplace",
        marketplace_sha="mkt-sha",
        version="5.1.0",
        plugin_subdir="",
        plugin_repo="obra/superpowers",
        plugin_sha="content-sha",
    )

    def fake(args, cwd=None):
        if args[:2] == ["clone", "--quiet"]:
            url, dest = args[-2], Path(args[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(exist_ok=True)
            if "superpowers-marketplace" in url:
                # Metadata-only marketplace repo: no skills.
                meta = dest / ".claude-plugin" / "marketplace.json"
                meta.parent.mkdir(parents=True, exist_ok=True)
                meta.write_text("{}")
            else:
                # Real content repo: actual skills.
                skill = dest / "skills" / "using-superpowers" / "SKILL.md"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text("real skill")
            return
        if args[:2] == ["checkout", "--quiet"]:
            return
        raise AssertionError(f"unexpected git call: {args}")

    plugins_root = tmp_path / ".claude" / "plugins"
    with mock.patch("bootstrap.install_plugins._run_git", side_effect=fake):
        install_one(spec, plugins_root)

    cache = plugins_root / "cache" / "superpowers-marketplace" / "superpowers" / "5.1.0"
    # Cache holds real skill content from the content repo...
    assert (cache / "skills" / "using-superpowers" / "SKILL.md").read_text() == "real skill"
    # ...and NOT the marketplace metadata.
    assert not (cache / ".claude-plugin" / "marketplace.json").exists()

    # The marketplace dir still mirrors the metadata clone.
    mkt = plugins_root / "marketplaces" / "superpowers-marketplace"
    assert (mkt / ".claude-plugin" / "marketplace.json").exists()

    # installed_plugins records the content repo's pinned SHA.
    installed = json.loads((plugins_root / "installed_plugins.json").read_text())
    entry = installed["plugins"]["superpowers@superpowers-marketplace"][0]
    assert entry["gitCommitSha"] == "content-sha"


def test_install_one_with_plugin_subdir_copies_only_subtree(tmp_path):
    fd_spec = PluginSpec(
        name="frontend-design",
        marketplace="claude-plugins-official",
        marketplace_repo="anthropics/claude-plugins-official",
        marketplace_sha="def",
        version="unknown",
        plugin_subdir="plugins/frontend-design",
    )
    fake = _fake_git_clone(fd_spec, "plugins/frontend-design/skills/frontend-design/SKILL.md")
    plugins_root = tmp_path / ".claude" / "plugins"
    with mock.patch("bootstrap.install_plugins._run_git", side_effect=fake):
        install_one(fd_spec, plugins_root)

    cache = plugins_root / "cache" / "claude-plugins-official" / "frontend-design" / "unknown"
    # Cache contains the subdir contents directly, NOT under plugins/frontend-design.
    assert (cache / "skills" / "frontend-design" / "SKILL.md").exists()
    assert not (cache / "plugins").exists()


def test_install_one_raises_when_plugin_subdir_missing(tmp_path):
    bad_spec = PluginSpec(
        name="bad",
        marketplace="m",
        marketplace_repo="o/r",
        marketplace_sha="x",
        version="v",
        plugin_subdir="does/not/exist",
    )

    def fake(args, cwd=None):
        if args[:2] == ["clone", "--quiet"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        # checkout no-op

    with mock.patch("bootstrap.install_plugins._run_git", side_effect=fake):
        with pytest.raises(FileNotFoundError):
            install_one(bad_spec, tmp_path / ".claude" / "plugins")


def test_install_all_continues_after_per_plugin_failure(tmp_path):
    manifest = tmp_path / "plugins.json"
    manifest.write_text(
        json.dumps(
            {
                "plugins": [
                    {
                        "name": "good",
                        "marketplace": "m1",
                        "marketplace_repo": "o/r1",
                        "marketplace_sha": "s1",
                        "version": "v",
                        "plugin_subdir": "",
                    },
                    {
                        "name": "broken",
                        "marketplace": "m2",
                        "marketplace_repo": "o/r2",
                        "marketplace_sha": "s2",
                        "version": "v",
                        "plugin_subdir": "missing/dir",
                    },
                ]
            }
        )
    )

    def fake(args, cwd=None):
        if args[:2] == ["clone", "--quiet"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    home = tmp_path / "home"
    with mock.patch("bootstrap.install_plugins._run_git", side_effect=fake):
        failures = install_all(manifest, home)

    assert failures == 1
    # First plugin still made it into installed_plugins.json
    installed = json.loads((home / ".claude" / "plugins" / "installed_plugins.json").read_text())
    assert "good@m1" in installed["plugins"]
    assert "broken@m2" not in installed["plugins"]
