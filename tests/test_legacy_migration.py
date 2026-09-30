from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import which_for

from agops.migration import LEGACY, migrate, pending


def _legacy_home(home: Path) -> Path:
    share = home / ".local" / "share" / LEGACY
    clone = share / "repo"
    clone.mkdir(parents=True)
    (clone / ".agent-hub-managed").touch()
    (share / "backups").mkdir()
    cfg = home / ".config" / LEGACY
    cfg.mkdir(parents=True)
    repo = str(clone)
    (cfg / "config.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "default": "default",
                "profiles": {
                    "default": {"repo": repo, "remote": None},
                    "elsewhere": {"repo": "/srv/other", "remote": None},
                },
            }
        ),
        encoding="utf-8",
    )
    state = home / ".local" / "state" / LEGACY
    state.mkdir(parents=True)
    (state / "session.json").write_text("{}", encoding="utf-8")
    return share


def _runner(registered: set[str]):
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        code = 0
        if argv[1:3] == ["mcp", "get"] and argv[0] not in registered:
            code = 1
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="")

    return calls, runner


def test_migrates_directories_config_marker_and_mcp(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _legacy_home(home)
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text(
        "<!-- BEGIN AGENT HUB MANAGED -->\nx\n<!-- END AGENT HUB MANAGED -->\n", encoding="utf-8"
    )
    calls, runner = _runner({"claude"})
    which = which_for("claude", "codex")
    assert pending(home, which=which, runner=runner)
    report = migrate(home, which=which, runner=runner)

    assert len(report["moved"]) == 3
    assert not (home / ".local" / "share" / LEGACY).exists()
    assert not (home / ".config" / LEGACY).exists()
    assert not (home / ".local" / "state" / LEGACY).exists()
    new_share = home / ".local" / "share" / "agops"
    assert (new_share / "backups").is_dir()
    assert (new_share / "repo" / ".agops-managed").exists()
    assert not (new_share / "repo" / ".agent-hub-managed").exists()
    data = json.loads((home / ".config" / "agops" / "config.json").read_text())
    assert data["profiles"]["default"]["repo"] == str(new_share / "repo")
    assert data["profiles"]["elsewhere"]["repo"] == "/srv/other"
    assert (home / ".local" / "state" / "agops" / "session.json").exists()
    text = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "BEGIN AGOPS MANAGED" in text and "AGENT HUB" not in text
    assert report["mcp_removed"] == ["claude"]
    assert ["claude", "mcp", "remove", LEGACY] in calls
    assert not any(argv[:3] == ["codex", "mcp", "remove"] for argv in calls)
    assert pending(home, which=which, runner=_runner(set())[1]) == []


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _legacy_home(home)
    _, runner = _runner(set())
    migrate(home, which=which_for(), runner=runner)
    again = migrate(home, which=which_for(), runner=runner)
    assert again["moved"] == [] and again["warnings"] == []


def test_conflict_warns_and_never_merges_or_deletes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    share = _legacy_home(home)
    new_share = home / ".local" / "share" / "agops"
    new_share.mkdir(parents=True)
    (new_share / "keep.txt").write_text("new", encoding="utf-8")
    _, runner = _runner(set())
    report = migrate(home, which=which_for(), runner=runner)
    assert len(report["warnings"]) == 1
    assert (share / "repo" / ".agent-hub-managed").exists()
    assert (new_share / "keep.txt").read_text(encoding="utf-8") == "new"
    # Config still moved, but its repo paths keep pointing at the untouched old clone.
    data = json.loads((home / ".config" / "agops" / "config.json").read_text())
    assert data["profiles"]["default"]["repo"] == str(share / "repo")
    assert any(item.startswith("conflict:") for item in pending(home, which=which_for()))


def test_fresh_home_needs_nothing(tmp_path: Path) -> None:
    _, runner = _runner(set())
    assert pending(tmp_path, which=which_for("claude"), runner=runner) == []
    assert migrate(tmp_path, which=which_for("claude"), runner=runner)["moved"] == []


def test_reowns_notes_sidecar_of_moved_local_hub(tmp_path: Path) -> None:
    import hashlib

    home = tmp_path / "home"
    _legacy_home(home)
    vault = tmp_path / "vault"
    vault.mkdir()
    config = home / ".config" / LEGACY / "config.json"
    data = json.loads(config.read_text())
    data["profiles"]["default"]["notes_target"] = str(vault)
    config.write_text(json.dumps(data))
    old_repo = home / ".local" / "share" / LEGACY / "repo"
    fingerprint = hashlib.sha256(f"local:{old_repo}".encode()).hexdigest()
    sidecar = vault / ".agops-notes.json"
    sidecar.write_text(json.dumps({"format_version": 1, "hub_fingerprint": fingerprint}))

    report = migrate(home, which=which_for(), runner=_runner(set())[1])

    new_repo = (home / ".local" / "share" / "agops" / "repo").resolve()
    expected = hashlib.sha256(f"local:{new_repo}".encode()).hexdigest()
    assert json.loads(sidecar.read_text())["hub_fingerprint"] == expected
    assert report["notes_reowned"] == [str(sidecar)]
