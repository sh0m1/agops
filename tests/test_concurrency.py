from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from conftest import git, project

from agops.hub import Hub
from agops.sessions import record_session
from agops.state import load_state


def test_remote_push_serializes_competing_claims(
    hub_repo: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    first = Hub(hub_repo)
    first.draft_plan(plan_file, "codex", "draft")
    first.approve_plan("shared-plan")

    remote = git(hub_repo, "remote", "get-url", "origin")
    second_path = tmp_path / "machine-two" / "runtime"
    second_path.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-b", "main", remote, str(second_path)],
        check=True,
        capture_output=True,
    )
    git(second_path, "config", "user.email", "test@example.com")
    git(second_path, "config", "user.name", "agops Test")
    (second_path / ".agops-managed").touch()
    second = Hub(second_path)
    first_worktree = project(tmp_path / "work-one")
    second_worktree = project(tmp_path / "work-two")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def claim(hub: Hub, actor: str, session: str, worktree: str) -> None:
        barrier.wait()
        try:
            hub.claim_task("shared-plan", "first", actor, session, Path(worktree))
            outcomes.append((actor, "won"))
        except Exception as exc:
            outcomes.append((actor, str(exc)))

    monkeypatch.delenv("AGOPS_LOCK_DIR")
    threads = [
        threading.Thread(target=claim, args=(first, "codex", "one", str(first_worktree))),
        threading.Thread(target=claim, args=(second, "claude", "two", str(second_worktree))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result == "won" for _, result in outcomes) == 1
    first.sync()
    owner = load_state(hub_repo).plans["shared-plan"].tasks["first"].owner
    assert owner in {"codex", "claude"}


def test_only_matching_tier_can_win_a_race(
    policy_hub: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    first = Hub(policy_hub)
    first.draft_plan(plan_file, "codex", "draft")
    first.approve_plan("shared-plan")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    record_session("two", "claude", "claude-opus-5", "frontier")

    remote = git(policy_hub, "remote", "get-url", "origin")
    second_path = tmp_path / "machine-two" / "runtime"
    second_path.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-b", "main", remote, str(second_path)],
        check=True,
        capture_output=True,
    )
    git(second_path, "config", "user.email", "test@example.com")
    git(second_path, "config", "user.name", "agops Test")
    (second_path / ".agops-managed").touch()
    second = Hub(second_path)
    first_worktree = project(tmp_path / "work-one")
    second_worktree = project(tmp_path / "work-two")

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim(hub: Hub, actor: str, session: str, worktree: str) -> None:
        barrier.wait()
        try:
            hub.claim_task("shared-plan", "first", actor, session, Path(worktree))
            outcomes[actor] = "won"
        except Exception as exc:
            outcomes[actor] = str(exc)

    monkeypatch.delenv("AGOPS_LOCK_DIR")
    threads = [
        threading.Thread(target=claim, args=(first, "codex", "one", str(first_worktree))),
        threading.Thread(target=claim, args=(second, "claude", "two", str(second_worktree))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["codex"] == "won"
    assert outcomes["claude"] != "won"
    first.sync()
    task = load_state(policy_hub).plans["shared-plan"].tasks["first"]
    assert (task.owner, task.tier) == ("codex", "standard")
