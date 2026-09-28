from __future__ import annotations

import builtins
import inspect
import os
import sys
from pathlib import Path

import pytest
from conftest import project

from agent_hub import cli
from agent_hub.hub import Hub
from agent_hub.policy import policy_path
from agent_hub.sessions import load_record, record_session

TIERED_PLAN = """id: tiered
title: Tiered
goal: g
tasks:
  - {id: exec, title: Execute, project: acme-widgets, write_scope: [a/**]}
  - {id: review, title: Review, read_only: true, tier: frontier}
"""


def _run(hub_repo: Path, *argv: str):
    args = cli.build_parser().parse_args(["--repo", str(hub_repo), *argv])
    return cli.dispatch(args)


def _activate(hub_repo: Path, tmp_path: Path) -> Hub:
    hub = Hub(hub_repo)
    plan = tmp_path / "tiered.yaml"
    plan.write_text(TIERED_PLAN, encoding="utf-8")
    hub.draft_plan(plan, "codex", "draft")
    hub.approve_plan("tiered")
    return hub


def test_brief_model_flag_declares_env_session(
    policy_hub: Path, tmp_path: Path, monkeypatch
) -> None:
    _activate(policy_hub, tmp_path)
    monkeypatch.setenv("AGENT_HUB_SESSION", "cli-one")
    monkeypatch.setenv("AGENT_HUB_ACTOR", "codex")
    out = _run(policy_hub, "brief", "--cwd", "/tmp", "--model", "claude-sonnet-5")
    assert "Session: codex · claude-sonnet-5 · tier=standard" in out
    record = load_record("cli-one")
    assert record is not None and record.model == "claude-sonnet-5"


def test_brief_without_session_env_uses_the_terminal_session(
    policy_hub: Path, tmp_path: Path, monkeypatch
) -> None:
    _activate(policy_hub, tmp_path)
    monkeypatch.delenv("AGENT_HUB_SESSION", raising=False)
    expected = f"shell-{os.getppid()}"
    assert cli.default_session() == expected
    assert cli.default_session() == expected
    _run(policy_hub, "brief", "--cwd", "/tmp", "--model", "claude-sonnet-5")
    record = load_record(expected)
    assert record is not None and record.model == "claude-sonnet-5"
    worktree = project(tmp_path / "work")
    event = _run(policy_hub, "task", "claim", "tiered", "exec", "--cwd", str(worktree))
    assert event["session"] == expected
    assert event["payload"]["tier"] == "standard"
    monkeypatch.setenv("AGENT_HUB_SESSION", "explicit")
    assert cli.default_session() == "explicit"


def test_task_ready_tier_filter(policy_hub: Path, tmp_path: Path) -> None:
    _activate(policy_hub, tmp_path)
    assert [t["id"] for t in _run(policy_hub, "task", "ready")] == ["exec", "review"]
    assert [t["id"] for t in _run(policy_hub, "task", "ready", "--tier", "frontier")] == ["review"]
    assert [t["id"] for t in _run(policy_hub, "task", "ready", "--tier", "standard")] == ["exec"]
    with pytest.raises(ValueError, match="Unknown tier: cheap"):
        _run(policy_hub, "task", "ready", "--tier", "cheap")


def test_task_ready_tier_requires_policy(hub_repo: Path) -> None:
    with pytest.raises(ValueError, match="task ready --tier requires memory/policy/tiers.yaml"):
        _run(hub_repo, "task", "ready", "--tier", "standard")


def test_override_requires_tty_and_confirmation(
    policy_hub: Path, tmp_path: Path, monkeypatch
) -> None:
    _activate(policy_hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "claude", "claude-opus-5", "frontier")
    base = [
        "task", "claim", "tiered", "exec",
        "--actor", "claude", "--session", "one", "--cwd", str(worktree),
    ]
    with pytest.raises(ValueError, match="requires tier standard"):
        _run(policy_hub, *base)

    monkeypatch.setenv("AGENT_HUB_AGENT_SESSION", "1")
    with pytest.raises(ValueError, match="unavailable inside a managed agent session"):
        _run(policy_hub, *base, "--allow-tier-mismatch")
    monkeypatch.delenv("AGENT_HUB_AGENT_SESSION")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="interactive terminal") as excinfo:
        _run(policy_hub, *base, "--allow-tier-mismatch")
    assert "--yes" not in str(excinfo.value)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "wrong")
    with pytest.raises(ValueError, match="cancelled"):
        _run(policy_hub, *base, "--allow-tier-mismatch")

    monkeypatch.setattr(builtins, "input", lambda _prompt: "exec")
    event = _run(policy_hub, *base, "--allow-tier-mismatch")
    assert event["payload"]["tier_override"] is True


def test_policy_show_and_validate(policy_hub: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_HUB_SESSION", "one")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    report = _run(policy_hub, "policy", "show")
    assert report["present"] is True and report["valid"] is True
    assert report["default_task_tier"] == "standard"
    assert "frontier" in report["tiers"]
    assert report["session"] == {"session": "one", "model": "claude-sonnet-5", "tier": "standard"}
    assert _run(policy_hub, "policy", "validate")["valid"] is True

    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    broken = _run(policy_hub, "policy", "show")
    assert broken["valid"] is False and "tiers" in broken["error"]
    with pytest.raises(ValueError):
        _run(policy_hub, "policy", "validate")


def test_policy_report_when_absent(hub_repo: Path) -> None:
    report = _run(hub_repo, "policy", "show")
    assert report == {"path": str(policy_path(hub_repo)), "present": False, "valid": None}
    assert _run(hub_repo, "policy", "validate")["valid"] is None


def test_doctor_reports_policy_state(policy_hub: Path, hub_repo: Path) -> None:
    assert cli.doctor(Hub(policy_hub))["policy"] == "ok"
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    report = cli.doctor(Hub(policy_hub))
    assert report["policy"].startswith("invalid:")
    assert report["ok"] is False


def test_doctor_reports_absent_policy(hub_repo: Path) -> None:
    report = cli.doctor(Hub(hub_repo))
    assert report["policy"] == "absent"


def test_mcp_brief_accepts_model_and_claim_has_no_override() -> None:
    from agent_hub import mcp_server

    assert "model" in inspect.signature(mcp_server.hub_get_brief).parameters
    assert "allow_tier_mismatch" not in inspect.signature(mcp_server.hub_claim_task).parameters


def test_setup_cli_rejects_local_with_remote() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["setup", "--local", "--remote", "https://x.invalid/r.git"])
    assert excinfo.value.code == 2
    args = cli.build_parser().parse_args(["setup", "--local"])
    assert args.local is True and args.remote is None


def test_agops_cli_name_and_json_after_health_command() -> None:
    parser = cli.build_parser()
    assert parser.prog == "agops"
    for command in ("doctor", "scan"):
        assert parser.parse_args([command, "--json"]).json is True
        assert parser.parse_args(["--json", command]).json is True
