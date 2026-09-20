from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import git, which_for

from agent_hub.health import doctor
from agent_hub.hub import Hub
from agent_hub.setup import INSTRUCTIONS, config_path, merge_managed_block, setup


def test_managed_instruction_block_is_idempotent_and_preserves_existing(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("# Existing\n\nKeep me.\n", encoding="utf-8")
    merge_managed_block(path)
    first = path.read_text(encoding="utf-8")
    merge_managed_block(path)
    assert path.read_text(encoding="utf-8") == first
    assert "Keep me." in first
    assert INSTRUCTIONS.strip() in first


def test_managed_instructions_require_a_stable_cli_session() -> None:
    assert "stable session ID" in INSTRUCTIONS


def test_managed_instructions_require_a_model_declaration() -> None:
    assert "model id" in INSTRUCTIONS
    assert "hub_get_brief" in INSTRUCTIONS
    assert "matching your tier" in INSTRUCTIONS


# --- end-to-end setup() against a fake home -------------------------------------------------


@pytest.fixture
def bare_remote(hub_repo: Path, tmp_path: Path) -> str:
    return str(tmp_path / "origin.git")


def _setup(remote: str | None, runtime: Path, home: Path, which, runner, **kwargs):
    return setup(remote, runtime, home=home, which=which, runner=runner, **kwargs)


def _configured_remote(home: Path) -> str | None:
    data = json.loads(config_path(home).read_text(encoding="utf-8"))
    return data["profiles"]["default"]["remote"]


def test_setup_remembers_remote_on_second_run(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    runtime = tmp_path / "rt"
    first = _setup(bare_remote, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert first["remote"] == bare_remote
    config = config_path(fake_home)
    saved = config.read_text(encoding="utf-8")
    second = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert second["remote"] == bare_remote
    assert config.read_text(encoding="utf-8") == saved
    data = json.loads(saved)
    assert data["schema_version"] == 3 and data["default"] == "default"
    assert data["profiles"] == {"default": {"repo": str(runtime.resolve()), "remote": bare_remote}}


def test_setup_without_remote_creates_a_local_hub(
    tmp_path: Path, fake_home: Path, recording_runner, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_HUB_TESTING", "1")
    monkeypatch.setenv("AGENT_HUB_LOCK_DIR", str(tmp_path / "locks"))
    _, runner = recording_runner
    runtime = tmp_path / "rt"
    summary = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert summary["remote"] is None
    assert summary["ok"] is True
    assert summary["policy"] == "created"
    assert summary["doctor"]["remote"] is None
    assert (runtime / "memory" / "README.md").exists()
    assert (runtime / "memory" / "policy" / "tiers.yaml").exists()
    assert git(runtime, "status", "--porcelain", "--untracked-files=no") == ""
    assert _configured_remote(fake_home) is None
    again = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert again["remote"] is None and again["policy"] == "already-present"


def test_setup_local_detaches_remote_and_forgets_it(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    runtime = tmp_path / "rt"
    _setup(bare_remote, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert git(runtime, "remote", "get-url", "origin") == bare_remote

    summary = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner, local=True)
    assert summary["remote"] is None
    assert summary["remote_removed"] == bare_remote
    assert summary["doctor"]["remote"] is None
    assert git(runtime, "remote") == ""
    assert _configured_remote(fake_home) is None

    # A plain re-run must stay local: the old remote is forgotten, not merely detached.
    again = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert again["remote"] is None and again["remote_removed"] is None
    assert git(runtime, "remote") == ""

    # --local on an already-local hub is a no-op.
    once_more = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner, local=True)
    assert once_more["remote_removed"] is None and once_more["ok"] is True


def test_setup_local_and_remote_are_mutually_exclusive(
    tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    with pytest.raises(ValueError, match="--local.*--remote"):
        _setup(
            "https://example.invalid/m.git",
            tmp_path / "rt",
            fake_home,
            which_for(),
            runner,
            local=True,
        )


def test_setup_attaches_a_remote_to_an_existing_local_hub(
    tmp_path: Path, fake_home: Path, recording_runner, monkeypatch
) -> None:
    monkeypatch.setenv("AGENT_HUB_TESTING", "1")
    monkeypatch.setenv("AGENT_HUB_LOCK_DIR", str(tmp_path / "locks"))
    _, runner = recording_runner
    runtime = tmp_path / "rt"
    _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    bare = tmp_path / "later.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    summary = _setup(str(bare), runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert summary["remote"] == str(bare)
    assert summary["doctor"]["remote"] == str(bare)
    assert git(runtime, "remote", "get-url", "origin") == str(bare)
    assert git(bare, "rev-parse", "main") == git(runtime, "rev-parse", "main")
    assert _configured_remote(fake_home) == str(bare)
    hub_summary_after_claimless_mutation = Hub(runtime).ensure_policy()
    assert hub_summary_after_claimless_mutation is None


def test_setup_requires_mcp_executable_with_path_hint(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    with pytest.raises(RuntimeError, match="agent-hub-mcp.*PATH"):
        _setup(bare_remote, tmp_path / "rt", fake_home, which_for(), runner)


def test_setup_skips_tools_not_on_path(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    calls, runner = recording_runner
    summary = _setup(bare_remote, tmp_path / "rt", fake_home, which_for("agent-hub-mcp"), runner)
    assert summary["tools"] == {
        "codex": "skipped-not-installed",
        "claude": "skipped-not-installed",
    }
    assert not any("mcp" in argv for argv in calls)


def test_setup_configures_tools_without_duplicating_entries(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    calls, runner = recording_runner
    which = which_for("agent-hub-mcp", "claude", "codex")
    runtime = tmp_path / "rt"
    summary = _setup(bare_remote, runtime, fake_home, which, runner)
    assert summary["tools"] == {"codex": "configured", "claude": "configured"}
    for tool in ("codex", "claude"):
        mcp_calls = [
            argv for argv in calls if argv[0] == tool and argv[1:3] != ["mcp", "get"]
        ]
        assert [argv[:3] for argv in mcp_calls] == [
            [tool, "mcp", "remove"],
            [tool, "mcp", "add"],
        ]
        add = mcp_calls[1]
        assert not any(arg.startswith("AGENT_HUB_REPO=") for arg in add)
        assert add[-1] == "/fake/bin/agent-hub-mcp"
    first_count = len(calls)
    _setup(None, runtime, fake_home, which, runner)
    assert len(calls) == first_count * 2


def test_setup_summary_matches_doctor_and_scan(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    runtime = tmp_path / "rt"
    summary = _setup(bare_remote, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert set(summary) == {
        "ok",
        "runtime",
        "remote",
        "config_path",
        "tools",
        "instructions",
        "claude_memory_disabled",
        "policy",
        "doctor",
        "scan",
        "remote_removed",
        "profile",
        "default_profile",
    }
    assert summary["profile"] == "default" and summary["default_profile"] == "default"
    assert summary["remote_removed"] is None
    assert summary["ok"] is True
    assert summary["policy"] == "created"
    assert summary["scan"]["errors"] == 0
    expected = doctor(Hub(runtime, profile="default"), home=fake_home, which=which_for())
    assert summary["doctor"] == expected
    assert summary["doctor"]["mcp_pinned"] == []
    assert summary["doctor"]["codex_instructions"] is True
    again = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert again["policy"] == "already-present"
    assert again["instructions"] == {"codex_agents_md": "unchanged", "claude_md": "unchanged"}


def test_setup_managed_blocks_are_idempotent_and_backed_up_once(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    agents = fake_home / ".codex" / "AGENTS.md"
    agents.parent.mkdir(parents=True)
    agents.write_text("# Mine\n", encoding="utf-8")
    runtime = tmp_path / "rt"
    first = _setup(bare_remote, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert first["instructions"] == {"codex_agents_md": "updated", "claude_md": "updated"}
    after_first = {
        path.name: path.read_bytes() for path in (agents, fake_home / ".claude" / "CLAUDE.md")
    }
    backups = sorted(agents.parent.glob("AGENTS.md.bak.*"))
    assert len(backups) == 1
    _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    for path in (agents, fake_home / ".claude" / "CLAUDE.md"):
        assert path.read_bytes() == after_first[path.name]
    assert sorted(agents.parent.glob("AGENTS.md.bak.*")) == backups
    assert "# Mine" in agents.read_text(encoding="utf-8")


def test_setup_claude_memory_toggle(
    bare_remote: str, tmp_path: Path, fake_home: Path, recording_runner
) -> None:
    _, runner = recording_runner
    settings = fake_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMemoryEnabled": True, "theme": "dark"}), encoding="utf-8")
    runtime = tmp_path / "rt"
    first = _setup(bare_remote, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert first["claude_memory_disabled"] is True
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written == {"autoMemoryEnabled": False, "theme": "dark"}
    assert len(list(settings.parent.glob("settings.json.bak.*"))) == 1
    second = _setup(None, runtime, fake_home, which_for("agent-hub-mcp"), runner)
    assert second["claude_memory_disabled"] is False
    assert len(list(settings.parent.glob("settings.json.bak.*"))) == 1

    other_home = tmp_path / "home2"
    other_settings = other_home / ".claude" / "settings.json"
    other_settings.parent.mkdir(parents=True)
    other_settings.write_text(json.dumps({"autoMemoryEnabled": True}), encoding="utf-8")
    kept = _setup(
        bare_remote,
        tmp_path / "rt2",
        other_home,
        which_for("agent-hub-mcp"),
        runner,
        disable_claude_memory=False,
    )
    assert kept["claude_memory_disabled"] is False
    assert json.loads(other_settings.read_text(encoding="utf-8")) == {"autoMemoryEnabled": True}
