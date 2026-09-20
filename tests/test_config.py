from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_hub.config import (
    Profiles,
    config_path,
    load_profiles,
    profile_runtime,
    resolve_repo,
    save_profiles,
)


def _write_legacy(home: Path, repo: str, remote: str | None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"repo": repo, "remote": remote}, indent=2) + "\n")
    return path


def test_no_config_resolves_to_legacy_default(fake_home: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENT_HUB_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_HUB_REPO", raising=False)
    assert load_profiles(fake_home) == Profiles(default=None, profiles={})
    resolution = resolve_repo(None, home=fake_home)
    assert resolution.profile == "default"
    assert resolution.source == "legacy"
    assert resolution.root == fake_home / ".local" / "share" / "agent-hub" / "repo"
    assert resolution.overridden_profile is None
    assert not config_path(fake_home).exists()


def test_legacy_config_is_read_as_default_profile_without_rewrite(
    fake_home: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AGENT_HUB_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_HUB_REPO", raising=False)
    path = _write_legacy(fake_home, "/somewhere/repo", "git@example.invalid:m.git")
    before = path.read_bytes()
    profiles = load_profiles(fake_home)
    assert profiles.default == "default"
    assert profiles.profiles == {
        "default": {"repo": "/somewhere/repo", "remote": "git@example.invalid:m.git"}
    }
    resolution = resolve_repo(None, home=fake_home)
    assert (resolution.profile, resolution.source) == ("default", "default")
    assert resolution.root == Path("/somewhere/repo")
    assert path.read_bytes() == before


def test_profile_runtime_paths(fake_home: Path) -> None:
    base = fake_home / ".local" / "share" / "agent-hub"
    assert profile_runtime("default", fake_home) == base / "repo"
    assert profile_runtime("team", fake_home) == base / "team"
    with pytest.raises(ValueError, match="profile name"):
        profile_runtime("../evil", fake_home)


def test_save_and_reload_v3(fake_home: Path) -> None:
    profiles = Profiles(
        default="team",
        profiles={
            "team": {"repo": "/r/team", "remote": "git@example.invalid:t.git"},
            "private": {"repo": "/r/private", "remote": None},
        },
    )
    save_profiles(profiles, fake_home)
    data = json.loads(config_path(fake_home).read_text())
    assert data["schema_version"] == 3 and data["default"] == "team"
    assert load_profiles(fake_home) == profiles


@pytest.fixture
def two_profiles(fake_home: Path, monkeypatch) -> Profiles:
    monkeypatch.delenv("AGENT_HUB_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_HUB_REPO", raising=False)
    profiles = Profiles(
        default="team",
        profiles={
            "team": {"repo": "/r/team", "remote": "git@example.invalid:t.git"},
            "private": {"repo": "/r/private", "remote": None},
        },
    )
    save_profiles(profiles, fake_home)
    return profiles


def test_resolution_order(fake_home: Path, two_profiles: Profiles, monkeypatch) -> None:
    assert resolve_repo(None, home=fake_home).profile == "team"
    assert resolve_repo(None, home=fake_home).source == "default"

    monkeypatch.setenv("AGENT_HUB_PROFILE", "private")
    private = resolve_repo(None, home=fake_home)
    assert (private.profile, private.source, private.root) == (
        "private",
        "env",
        Path("/r/private"),
    )

    monkeypatch.setenv("AGENT_HUB_REPO", "/r/team")
    explicit = resolve_repo(None, home=fake_home)
    assert (explicit.profile, explicit.source) == ("explicit", "explicit")
    assert explicit.root == Path("/r/team")
    assert explicit.overridden_profile == "private"

    argument = resolve_repo("/elsewhere", home=fake_home)
    assert argument.root == Path("/elsewhere") and argument.source == "explicit"


def test_unknown_profile_fails_closed(fake_home: Path, two_profiles: Profiles, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HUB_PROFILE", "nope")
    with pytest.raises(ValueError, match=r"Unknown hub profile 'nope'; configured: private, team"):
        resolve_repo(None, home=fake_home)
