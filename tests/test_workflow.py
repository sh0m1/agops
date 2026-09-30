from __future__ import annotations

from pathlib import Path

import pytest

from agops.git import GitError
from agops.hub import Hub
from agops.state import load_state


def activate(hub: Hub, plan_file: Path) -> None:
    hub.draft_plan(plan_file, "codex", "draft-session")
    hub.approve_plan("shared-plan")


def test_draft_requires_approval_before_claim(hub_repo: Path, plan_file: Path) -> None:
    hub = Hub(hub_repo)
    hub.draft_plan(plan_file, "codex", "draft-session")
    assert hub.list_plans()[0]["status"] == "draft"
    with pytest.raises(ValueError, match="not active"):
        hub.claim_task("shared-plan", "first", "codex", "one", Path("/tmp/work-one"))


def test_claim_checkpoint_dependency_and_evidence(
    hub_repo: Path, plan_file: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    hub = Hub(hub_repo)
    first_worktree, second_worktree, _ = project_paths
    activate(hub, plan_file)
    with pytest.raises(ValueError, match="Dependency"):
        hub.claim_task("shared-plan", "second", "claude", "two", second_worktree)
    hub.claim_task("shared-plan", "first", "codex", "one", first_worktree)
    hub.checkpoint_task("shared-plan", "first", "codex", "one", "Implemented", ["pytest"])
    with pytest.raises(ValueError, match="evidence"):
        hub.complete_task("shared-plan", "first", "codex", "one", "Done", [])
    hub.complete_task("shared-plan", "first", "codex", "one", "Done", ["pytest: pass"])
    assert [task["id"] for task in hub.ready_tasks()] == ["second"]
    hub.claim_task("shared-plan", "second", "claude", "two", second_worktree)
    hub.complete_task("shared-plan", "second", "claude", "two", "Done", ["pytest: pass"])
    assert load_state(hub_repo).plans["shared-plan"].completed


def test_wrong_session_cannot_update_claim(
    hub_repo: Path, plan_file: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    hub = Hub(hub_repo)
    activate(hub, plan_file)
    hub.claim_task("shared-plan", "first", "codex", "one", project_paths[0])
    with pytest.raises(ValueError, match="owned by"):
        hub.checkpoint_task("shared-plan", "first", "claude", "two", "No", [])


def test_separate_nonoverlapping_worktrees_can_claim(
    hub_repo: Path, tmp_path: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    plan_file = tmp_path / "parallel.yaml"
    plan_file.write_text(
        """id: parallel
title: Parallel work
goal: Work concurrently.
tasks:
  - {id: a, title: A, project: acme-widgets, write_scope: [a/**]}
  - {id: b, title: B, project: acme-widgets, write_scope: [b/**]}
""",
        encoding="utf-8",
    )
    hub = Hub(hub_repo)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("parallel")
    hub.claim_task("parallel", "a", "codex", "one", project_paths[0])
    hub.claim_task("parallel", "b", "claude", "two", project_paths[1])


def test_same_worktree_and_overlapping_scope_are_rejected(
    hub_repo: Path, tmp_path: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    plan_file = tmp_path / "parallel.yaml"
    plan_file.write_text(
        """id: overlap
title: Overlap
goal: Reject conflicts.
tasks:
  - {id: a, title: A, project: acme-widgets, write_scope: [src/**]}
  - {id: b, title: B, project: acme-widgets, write_scope: [src/api/**]}
  - {id: c, title: C, project: acme-widgets, write_scope: [other/**]}
""",
        encoding="utf-8",
    )
    hub = Hub(hub_repo)
    hub.draft_plan(plan_file, "codex", "draft")
    hub.approve_plan("overlap")
    hub.claim_task("overlap", "a", "codex", "one", project_paths[0])
    with pytest.raises(ValueError, match="overlaps"):
        hub.claim_task("overlap", "b", "claude", "two", project_paths[1])
    with pytest.raises(ValueError, match="checkout"):
        hub.claim_task("overlap", "c", "claude", "two", project_paths[0])


def test_claim_rejects_the_wrong_project(
    hub_repo: Path, plan_file: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    hub = Hub(hub_repo)
    activate(hub, plan_file)
    with pytest.raises(ValueError, match="targets project"):
        hub.claim_task("shared-plan", "first", "codex", "one", project_paths[2])


def test_checkpoint_queues_during_sync_failure_and_flushes_later(
    hub_repo: Path,
    plan_file: Path,
    project_paths: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = Hub(hub_repo)
    activate(hub, plan_file)
    hub.claim_task("shared-plan", "first", "codex", "one", project_paths[0])
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    mutate = hub._mutate

    def unavailable(_):
        raise GitError("network unavailable")

    monkeypatch.setattr(hub, "_mutate", unavailable)
    queued = hub.checkpoint_task(
        "shared-plan", "first", "codex", "one", "Saved locally", ["test: pending"]
    )
    assert queued["queued"] is True
    monkeypatch.setattr(hub, "_mutate", mutate)
    published = hub.flush_outbox()
    assert published[0]["payload"]["delayed"] is True
    assert not list(hub._outbox_root().glob("*.json"))


def test_blocked_task_can_be_unblocked_and_reclaimed(
    hub_repo: Path, plan_file: Path, project_paths: tuple[Path, Path, Path]
) -> None:
    hub = Hub(hub_repo)
    activate(hub, plan_file)
    hub.claim_task("shared-plan", "first", "codex", "one", project_paths[0])
    hub.block_task("shared-plan", "first", "codex", "one", "Needs a fixture")
    assert hub.ready_tasks() == []
    hub.unblock_task("shared-plan", "first", "claude", "two", "Fixture is available")
    assert [task["id"] for task in hub.ready_tasks()] == ["first"]
    hub.claim_task("shared-plan", "first", "claude", "two", project_paths[1])
