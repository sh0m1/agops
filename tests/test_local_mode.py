from __future__ import annotations

import threading
from pathlib import Path

from conftest import git, project

from agops.git import has_remote, remote_url
from agops.hub import Hub
from agops.state import load_state


def test_has_remote(local_hub: Path, hub_repo: Path) -> None:
    assert has_remote(local_hub) is False
    assert has_remote(hub_repo) is True


def test_local_hub_runs_the_full_task_flow_without_a_remote(
    local_hub: Path, plan_file: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub)
    hub.sync()
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("shared-plan")
    worktree = project(tmp_path / "work")
    hub.claim_task("shared-plan", "first", "codex", "one", worktree)
    hub.checkpoint_task("shared-plan", "first", "codex", "one", "Half way", ["pytest: 3 passed"])
    hub.complete_task("shared-plan", "first", "codex", "one", "Done", ["pytest: 6 passed"])
    assert load_state(local_hub).plans["shared-plan"].tasks["first"].status == "completed"
    assert git(local_hub, "status", "--porcelain", "--untracked-files=no") == ""
    assert "hub: completed shared-plan/first" in git(local_hub, "log", "--oneline", "-1")
    assert remote_url(local_hub) is None


def test_local_hub_serializes_competing_claims_with_the_lock(
    local_hub: Path, plan_file: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("shared-plan")
    worktrees = [project(tmp_path / "work-one"), project(tmp_path / "work-two")]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def claim(actor: str, session: str, worktree: Path) -> None:
        barrier.wait()
        try:
            Hub(local_hub).claim_task("shared-plan", "first", actor, session, worktree)
            outcomes.append("won")
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [
        threading.Thread(target=claim, args=("codex", "one", worktrees[0])),
        threading.Thread(target=claim, args=("claude", "two", worktrees[1])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("won") == 1
    assert any("already claimed" in outcome for outcome in outcomes)
