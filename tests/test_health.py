from __future__ import annotations

from pathlib import Path

from agops.health import doctor
from agops.hub import Hub
from agops.sessions import default_home


def test_default_home_honours_environment(fake_home: Path) -> None:
    assert default_home() == fake_home


def test_doctor_reads_instruction_files_from_the_given_home(
    hub_repo: Path, fake_home: Path
) -> None:
    report = doctor(Hub(hub_repo), home=fake_home)
    assert report["codex_instructions"] is False
    assert report["claude_instructions"] is False
    assert report["policy"] == "absent"
    assert report["remote"] == str(hub_repo.parent / "origin.git")
    (fake_home / ".codex").mkdir()
    (fake_home / ".codex" / "AGENTS.md").write_text("x\n", encoding="utf-8")
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    report = doctor(Hub(hub_repo))
    assert report["codex_instructions"] is True
    assert report["claude_instructions"] is True
    assert report["ok"] is True


def test_doctor_reports_local_hubs(local_hub: Path, fake_home: Path) -> None:
    report = doctor(Hub(local_hub), home=fake_home)
    assert report["remote"] is None
    assert report["git"] is True


def test_doctor_reports_profile_and_unpinned_mcp(local_hub: Path, fake_home: Path) -> None:
    from conftest import which_for

    report = doctor(Hub(local_hub, profile="private"), home=fake_home, which=which_for())
    assert report["profile"] == "private"
    assert report["mcp_pinned"] == []


def test_doctor_flags_pinned_codex_registration(local_hub: Path, fake_home: Path) -> None:
    from conftest import which_for

    codex = fake_home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        '[mcp_servers.agops]\ncommand = "x"\n\n[mcp_servers.agops.env]\n'
        'AGOPS_REPO = "/old"\n'
    )
    report = doctor(Hub(local_hub), home=fake_home, which=which_for("codex"))
    assert report["mcp_pinned"] == ["codex"]


def test_env_vars_forwarding_line_is_not_a_pinned_registration(
    local_hub: Path, fake_home: Path
) -> None:
    from conftest import which_for

    from agops.setup import CODEX_FORWARDED_ENV

    codex = fake_home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        "[mcp_servers.agops]\n"
        f"env_vars = {CODEX_FORWARDED_ENV!s}\n"
        'command = "x"\n'
    )
    report = doctor(Hub(local_hub), home=fake_home, which=which_for("codex"))
    assert report["mcp_pinned"] == []


def test_format_setup_summary_treats_informational_keys_as_such() -> None:
    from agops.cli import format_setup_summary

    summary = {
        "ok": True,
        "profile": "private",
        "default_profile": "team",
        "runtime": "/r",
        "remote": None,
        "remote_removed": None,
        "tools": {"codex": "configured", "claude": "configured"},
        "policy": "created",
        "scan": {"files": 3, "errors": 0},
        "doctor": {
            "ok": True,
            "root": "/r",
            "profile": "private",
            "remote": None,
            "mcp_pinned": [],
            "policy": "ok",
            "queued_checkpoints": 0,
            "git": True,
            "scan": True,
            "managed_clone": True,
            "codex_instructions": True,
            "claude_instructions": True,
        },
    }
    text = format_setup_summary(summary)
    assert "doctor:   ok" in text
    assert "profile:  private\n" in text
    summary["doctor"]["git"] = False
    summary["ok"] = False
    text = format_setup_summary(summary)
    assert "doctor:   NOT OK (git)" in text
