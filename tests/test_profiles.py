from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import which_for

from agent_hub import cli
from agent_hub.config import config_path, load_profiles
from agent_hub.hub import Hub
from agent_hub.setup import CODEX_FORWARDED_ENV, setup


def _setup(home: Path, runner, *, profile: str | None = None, **kwargs):
    return setup(
        kwargs.pop("remote", None),
        kwargs.pop("runtime", None),
        home=home,
        which=kwargs.pop("which", which_for("agent-hub-mcp")),
        runner=runner,
        profile=profile,
        **kwargs,
    )


@pytest.fixture
def isolated(tmp_path: Path, fake_home: Path, monkeypatch) -> Path:
    monkeypatch.setenv("AGENT_HUB_TESTING", "1")
    monkeypatch.setenv("AGENT_HUB_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("AGENT_HUB_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_HUB_REPO", raising=False)
    return fake_home


def test_two_profiles_team_and_private(isolated: Path, tmp_path: Path, recording_runner) -> None:
    _, runner = recording_runner
    bare = tmp_path / "team.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "memory").mkdir()
    (seed / "memory" / "README.md").write_text("x\n")
    for argv in (["add", "memory"], ["commit", "-q", "-m", "i"], ["push", "-q", str(bare), "main"]):
        subprocess.run(["git", "-C", str(seed), *argv], check=True, capture_output=True)

    team = _setup(isolated, runner, profile="team", remote=str(bare), make_default=True)
    assert team["profile"] == "team" and team["default_profile"] == "team"
    assert team["runtime"] == str(isolated / ".local" / "share" / "agent-hub" / "team")

    private = _setup(isolated, runner, profile="private", local=True)
    assert private["profile"] == "private" and private["default_profile"] == "team"
    assert private["remote"] is None
    assert private["runtime"] == str(isolated / ".local" / "share" / "agent-hub" / "private")

    profiles = load_profiles(isolated)
    assert profiles.default == "team"
    assert set(profiles.profiles) == {"team", "private"}
    assert profiles.profiles["team"]["remote"] == str(bare)
    assert profiles.profiles["private"]["remote"] is None

    # Knowledge written under one profile never appears in the other.
    Hub(Path(private["runtime"])).add_knowledge(
        "global", "secret", "Secret", "Only for me.", "codex", "one"
    )
    assert not list((Path(team["runtime"]) / "memory").rglob("*secret*"))
    assert list((Path(private["runtime"]) / "memory").rglob("*secret*"))

    # A plain re-run targets the default profile and leaves the other untouched.
    before = json.dumps(load_profiles(isolated).profiles["private"], sort_keys=True)
    again = _setup(isolated, runner)
    assert again["profile"] == "team"
    assert json.dumps(load_profiles(isolated).profiles["private"], sort_keys=True) == before


def test_legacy_config_migrates_in_place(isolated: Path, tmp_path: Path, recording_runner) -> None:
    _, runner = recording_runner
    runtime = tmp_path / "legacy-rt"
    path = config_path(isolated)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"repo": str(runtime), "remote": None}) + "\n")
    summary = _setup(isolated, runner)
    assert summary["profile"] == "default" and summary["default_profile"] == "default"
    data = json.loads(path.read_text())
    assert data["schema_version"] == 3
    assert data["profiles"]["default"]["repo"] == str(runtime.resolve())
    assert (runtime / "memory" / "policy" / "tiers.yaml").exists()


def test_mcp_registration_is_not_pinned_and_codex_forwards_env(
    isolated: Path, recording_runner
) -> None:
    calls, runner = recording_runner
    codex_config = isolated / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        'model = "x"\n\n[mcp_servers.other]\ncommand = "y"\n\n'
        '[mcp_servers.agent-hub]\ncommand = "/fake/bin/agent-hub-mcp"\n\n[other]\nk = 1\n'
    )
    _setup(isolated, runner, which=which_for("agent-hub-mcp", "claude", "codex"), local=True)
    adds = [argv for argv in calls if argv[1:3] == ["mcp", "add"]]
    assert len(adds) == 2
    for argv in adds:
        assert "--env" not in argv
        assert not any(arg.startswith("AGENT_HUB_REPO=") for arg in argv)
    text = codex_config.read_text()
    section = text.split("[mcp_servers.agent-hub]\n", 1)[1].split("\n[", 1)[0]
    assert f"env_vars = {json.dumps(CODEX_FORWARDED_ENV)}" in section
    assert "AGENT_HUB_PROFILE" in CODEX_FORWARDED_ENV
    assert 'model = "x"' in text and "[other]\nk = 1" in text and "[mcp_servers.other]" in text
    _setup(isolated, runner, which=which_for("agent-hub-mcp", "claude", "codex"), local=True)
    assert codex_config.read_text().count("env_vars") == 1


def test_brief_header_names_the_hub(
    isolated: Path, tmp_path: Path, recording_runner, monkeypatch
) -> None:
    _, runner = recording_runner
    _setup(isolated, runner, profile="private", local=True)
    hub = Hub.from_environment()
    assert hub.profile == "private"
    header = hub.brief(Path("/tmp")).splitlines()[:5]
    assert "Hub: private · local only" in header

    monkeypatch.setenv("AGENT_HUB_REPO", hub.root.as_posix())
    monkeypatch.setenv("AGENT_HUB_PROFILE", "private")
    overridden = Hub.from_environment()
    lines = overridden.brief(Path("/tmp")).splitlines()[:6]
    assert any(
        line.startswith("Warning: AGENT_HUB_REPO overrides AGENT_HUB_PROFILE=private")
        for line in lines
    )

    monkeypatch.delenv("AGENT_HUB_REPO")
    monkeypatch.setenv("AGENT_HUB_PROFILE", "nope")
    with pytest.raises(ValueError, match="Unknown hub profile"):
        Hub.from_environment()


def test_profile_cli(isolated: Path, tmp_path: Path, recording_runner, monkeypatch) -> None:
    _, runner = recording_runner
    _setup(isolated, runner, profile="team", local=True, make_default=True)
    _setup(isolated, runner, profile="private", local=True)

    listing = cli.dispatch(cli.build_parser().parse_args(["--json", "profile", "list"]))
    by_name = {entry["name"]: entry for entry in listing["profiles"]}
    assert by_name["team"]["default"] is True and by_name["private"]["default"] is False
    assert listing["current"] == "team"
    assert by_name["private"]["remote"] is None

    text = cli.dispatch(cli.build_parser().parse_args(["profile", "list"]))
    assert "* ← team" in text and "    private" in text
    assert "Current hub: team (via default)" in text

    cli.dispatch(cli.build_parser().parse_args(["profile", "default", "private"]))
    assert load_profiles(isolated).default == "private"
    with pytest.raises(ValueError, match="Unknown hub profile"):
        cli.dispatch(cli.build_parser().parse_args(["profile", "default", "nope"]))

    monkeypatch.setenv("AGENT_HUB_PROFILE", "team")
    listing = cli.dispatch(cli.build_parser().parse_args(["--json", "profile", "list"]))
    assert listing["current"] == "team" and listing["source"] == "env"

    args = cli.build_parser().parse_args(["setup", "--profile", "x", "--default", "--local"])
    assert args.profile == "x" and args.default is True and args.local is True
