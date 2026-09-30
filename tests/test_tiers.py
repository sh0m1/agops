from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git, project

from agops.hub import Hub
from agops.policy import DEFAULT_POLICY_TEXT, PolicyError, parse_policy, policy_path
from agops.sessions import load_record, record_session
from agops.state import State, load_state, validate_plan


def _claim_event(payload: dict) -> dict:
    return {
        "schema_version": 1,
        "id": "e1",
        "occurred_at": "2026-09-07T00:00:00Z",
        "type": "task_claimed",
        "actor": "codex",
        "session": "one",
        "plan_id": "p",
        "task_id": "t",
        "payload": payload,
    }


def test_claim_replay_records_model_and_tier() -> None:
    state = State()
    state.apply(
        _claim_event(
            {
                "lease_until": "2999-01-01T00:00:00Z",
                "model": "claude-sonnet-5",
                "tier": "standard",
                "tier_override": True,
            }
        )
    )
    task = state.plans["p"].tasks["t"]
    assert (task.model, task.tier, task.tier_override) == ("claude-sonnet-5", "standard", True)


def test_legacy_claim_replays_without_model() -> None:
    state = State()
    state.apply(_claim_event({"lease_until": "2999-01-01T00:00:00Z"}))
    task = state.plans["p"].tasks["t"]
    assert (task.model, task.tier, task.tier_override) == (None, None, False)


def test_heartbeat_preserves_claim_model() -> None:
    state = State()
    state.apply(
        _claim_event({"lease_until": "2999-01-01T00:00:00Z", "model": "m", "tier": "standard"})
    )
    heartbeat = _claim_event({"lease_until": "2999-01-02T00:00:00Z"})
    heartbeat["type"] = "task_heartbeat"
    state.apply(heartbeat)
    assert state.plans["p"].tasks["t"].model == "m"


def _plan(tier: object) -> dict:
    return {
        "id": "p",
        "title": "P",
        "goal": "g",
        "tasks": [{"id": "t", "title": "T", "project": "acme-widgets", "tier": tier}],
    }


def test_validate_plan_checks_tier_against_policy() -> None:
    policy = parse_policy(DEFAULT_POLICY_TEXT)
    validate_plan(_plan("frontier"), policy)
    with pytest.raises(ValueError, match="unknown tier"):
        validate_plan(_plan("cheap"), policy)
    with pytest.raises(ValueError, match="must be a string"):
        validate_plan(_plan(3), policy)


def test_validate_plan_without_policy_accepts_any_tier_string() -> None:
    validate_plan(_plan("anything"))
    with pytest.raises(ValueError, match="must be a string"):
        validate_plan(_plan(["x"]))


def test_ensure_policy_writes_and_publishes_default(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    assert hub.policy() is None
    event = hub.ensure_policy()
    assert event is not None and event["type"] == "policy_initialized"
    assert policy_path(hub_repo).read_text(encoding="utf-8") == DEFAULT_POLICY_TEXT
    assert hub.ensure_policy() is None
    policy = hub.policy()
    assert policy is not None and policy.default_task_tier == "standard"


def test_scan_rejects_invalid_policy(policy_hub: Path) -> None:
    hub = Hub(policy_hub)
    assert hub.scan()["errors"] == 0
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        hub.scan()


def test_draft_rejects_unknown_tier_when_policy_present(policy_hub: Path, tmp_path: Path) -> None:
    plan = tmp_path / "tiered.yaml"
    plan.write_text(
        """id: tiered
title: Tiered
goal: g
tasks:
  - {id: a, title: A, project: acme-widgets, tier: cheap}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown tier"):
        Hub(policy_hub).draft_plan(plan, "codex", "draft")


TIERED_PLAN = """id: tiered
title: Tiered
goal: g
tasks:
  - {id: exec, title: Execute, project: acme-widgets, write_scope: [a/**]}
  - {id: review, title: Review, read_only: true, tier: frontier}
"""


def _activate_tiered(hub: Hub, tmp_path: Path) -> None:
    plan = tmp_path / "tiered.yaml"
    plan.write_text(TIERED_PLAN, encoding="utf-8")
    hub.draft_plan(plan, "codex", "draft")
    hub.approve_plan("tiered")


def test_brief_declares_session_and_hides_other_tiers(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "Session: codex · claude-sonnet-5 · tier=standard" in brief
    assert "exec: ready" in brief
    assert "review: ready" not in brief
    assert "1 task(s) hidden by tier: 1 frontier" in brief
    record = load_record("one")
    assert record is not None and (record.model, record.tier) == ("claude-sonnet-5", "standard")


def test_brief_redeclaration_overwrites_record(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    brief = hub.brief(Path("/tmp"), model="claude-opus-5", actor="codex", session="one")
    assert "tier=frontier" in brief
    assert "review: ready" in brief
    assert "1 task(s) hidden by tier: 1 standard" in brief
    record = load_record("one")
    assert record is not None and record.model == "claude-opus-5"


def test_brief_uses_record_when_no_model_passed(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    brief = hub.brief(Path("/tmp"), actor="codex", session="one")
    assert "tier=standard" in brief


def test_brief_undeclared_shows_nudge_and_everything(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), actor="codex", session="one")
    assert "Session: undeclared — pass model=<your model id> to hub_get_brief" in brief
    assert "exec: ready" in brief and "review: ready" in brief
    assert "hidden by tier" not in brief


def test_brief_unmapped_model_warns_and_hides_nothing(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="mystery-9", actor="codex", session="one")
    assert "tier=unknown" in brief
    assert "Warning: model mystery-9 is not mapped in memory/policy/tiers.yaml" in brief
    assert "exec: ready" in brief and "review: ready" in brief


def test_brief_without_policy_is_unenforced(hub_repo: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    hub = Hub(hub_repo)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "tier=unenforced" in brief
    assert "exec: ready" in brief and "review: ready" in brief


def test_env_model_overrides_argument(policy_hub: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGOPS_MODEL", "claude-opus-5")
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "claude-opus-5 · tier=frontier" in brief


def test_matching_tier_claims_and_stamps_event(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    event = hub.claim_task("tiered", "exec", "codex", "one", worktree)
    assert event["payload"]["model"] == "claude-sonnet-5"
    assert event["payload"]["tier"] == "standard"
    assert event["payload"]["tier_override"] is False
    task = load_state(policy_hub).plans["tiered"].tasks["exec"]
    assert (task.model, task.tier) == ("claude-sonnet-5", "standard")


def test_mismatched_tier_is_rejected_both_ways(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("frontier-session", "claude", "claude-opus-5", "frontier")
    record_session("standard-session", "codex", "claude-sonnet-5", "standard")
    with pytest.raises(
        ValueError,
        match="Task exec requires tier standard; session model claude-opus-5 is tier frontier",
    ):
        hub.claim_task("tiered", "exec", "claude", "frontier-session", worktree)
    with pytest.raises(
        ValueError,
        match="Task review requires tier frontier; session model claude-sonnet-5 is tier standard",
    ):
        hub.claim_task("tiered", "review", "codex", "standard-session", worktree)


def test_undeclared_and_unmapped_sessions_are_rejected(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    with pytest.raises(ValueError, match="Session has not declared a model; call brief"):
        hub.claim_task("tiered", "exec", "codex", "nobody", worktree)
    record_session("odd", "codex", "mystery-9", "unknown")
    with pytest.raises(
        ValueError, match="Model 'mystery-9' is not mapped to a tier in memory/policy/tiers.yaml"
    ):
        hub.claim_task("tiered", "exec", "codex", "odd", worktree)


def test_override_bypasses_comparison_and_is_stamped(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "claude", "claude-opus-5", "frontier")
    event = hub.claim_task("tiered", "exec", "claude", "one", worktree, allow_tier_mismatch=True)
    assert event["payload"]["tier_override"] is True
    assert event["payload"]["tier"] == "frontier"
    with pytest.raises(ValueError, match="not declared"):
        hub.claim_task("tiered", "review", "claude", "nobody", worktree, allow_tier_mismatch=True)


def test_invalid_policy_rejects_every_claim(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        hub.claim_task("tiered", "exec", "codex", "one", worktree)


def test_without_policy_claims_record_model_and_null_tier(
    hub_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    hub = Hub(hub_repo)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", None)
    event = hub.claim_task("tiered", "exec", "codex", "one", worktree)
    assert event["payload"]["model"] == "claude-sonnet-5"
    assert event["payload"]["tier"] is None
    undeclared = hub.claim_task("tiered", "review", "codex", "two", worktree)
    assert undeclared["payload"]["model"] is None


def test_claim_rejects_task_tier_not_defined_in_policy(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")

    policy_path(policy_hub).write_text(
        """schema_version: 1
default_task_tier: standard
tiers:
  standard:
    models: ["claude-sonnet-*"]
""",
        encoding="utf-8",
    )
    git(policy_hub, "add", "memory")
    git(policy_hub, "commit", "-m", "drop frontier")
    git(policy_hub, "push", "origin", "HEAD:main")

    with pytest.raises(ValueError, match="not defined in memory/policy/tiers.yaml"):
        hub.claim_task("tiered", "review", "codex", "one", worktree)





def test_matching_tier_override_stamps_false(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    event = hub.claim_task("tiered", "exec", "codex", "one", worktree, allow_tier_mismatch=True)
    assert event["payload"]["tier_override"] is False
