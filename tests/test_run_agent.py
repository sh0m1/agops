from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import project

from agops import cli
from agops.hub import Hub
from agops.state import load_state

_REAL_RUN = cli.subprocess.run


def _fake_run(captured: dict):
    def fake(cmd, *args, check=False, env=None, cwd=None, **kwargs):
        if cmd and cmd[0] == "fake-tool":
            captured["cmd"] = cmd
            captured["env"] = env
            captured["cwd"] = cwd
            return SimpleNamespace(returncode=0)
        return _REAL_RUN(cmd, *args, check=check, env=env, cwd=cwd, **kwargs)

    return fake


def _dispatch(hub_repo: Path, worktree: Path, *extra: str):
    argv = [
        "--repo",
        str(hub_repo),
        "run",
        "--plan",
        "shared-plan",
        "--task",
        "first",
        "--cwd",
        str(worktree),
        *extra,
        "fake-tool",
    ]
    args = cli.build_parser().parse_args(argv)
    return cli.dispatch(args)


def test_run_without_model_raises_under_policy(
    policy_hub: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    hub = Hub(policy_hub)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("shared-plan")
    worktree = project(tmp_path / "work")

    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(captured))

    with pytest.raises(ValueError, match="agops run needs --model"):
        _dispatch(policy_hub, worktree)

    state = load_state(policy_hub)
    assert state.plans["shared-plan"].tasks.get("first") is None
    assert "cmd" not in captured


def test_run_with_model_claims_and_exports_env(
    policy_hub: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    hub = Hub(policy_hub)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("shared-plan")
    worktree = project(tmp_path / "work")

    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(captured))

    result = _dispatch(policy_hub, worktree, "--model", "claude-sonnet-5")
    assert result["exit_code"] == 0
    assert captured["env"]["AGOPS_MODEL"] == "claude-sonnet-5"

    state = load_state(hub.root)
    assert state.plans["shared-plan"].tasks["first"].model == "claude-sonnet-5"


def test_run_without_policy_still_works_without_model(
    hub_repo: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    hub = Hub(hub_repo)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("shared-plan")
    worktree = project(tmp_path / "work")

    captured: dict = {}
    monkeypatch.setattr(cli.subprocess, "run", _fake_run(captured))

    result = _dispatch(hub_repo, worktree)
    assert result["exit_code"] == 0

    state = load_state(hub.root)
    assert state.plans["shared-plan"].tasks["first"].model is None
