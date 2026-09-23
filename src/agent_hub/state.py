from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .policy import TierPolicy


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class TaskState:
    status: str = "ready"
    owner: str | None = None
    session: str | None = None
    lease_until: str | None = None
    worktree: str | None = None
    summary: str | None = None
    evidence: list[str] = field(default_factory=list)
    reason: str | None = None
    model: str | None = None
    tier: str | None = None
    tier_override: bool = False

    def actively_claimed(self, now: datetime | None = None) -> bool:
        if self.status != "claimed" or not self.lease_until:
            return False
        return parse_time(self.lease_until) > (now or utc_now())


@dataclass
class PlanState:
    plan_id: str
    approved_revision: int | None = None
    completed: bool = False
    cancelled: bool = False
    tasks: dict[str, TaskState] = field(default_factory=dict)
    first_event_at: str | None = None
    last_event_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None

    @property
    def active(self) -> bool:
        return self.approved_revision is not None and not self.completed and not self.cancelled


class State:
    def __init__(self) -> None:
        self.plans: dict[str, PlanState] = {}

    def plan(self, plan_id: str) -> PlanState:
        return self.plans.setdefault(plan_id, PlanState(plan_id))

    def apply(self, event: dict[str, Any]) -> None:
        plan_id = event.get("plan_id")
        if not plan_id:
            return
        plan = self.plan(plan_id)
        occurred_at = event.get("occurred_at")
        if occurred_at:
            if plan.first_event_at is None or parse_time(occurred_at) < parse_time(
                plan.first_event_at
            ):
                plan.first_event_at = occurred_at
            if plan.last_event_at is None or parse_time(occurred_at) > parse_time(
                plan.last_event_at
            ):
                plan.last_event_at = occurred_at
        kind = event["type"]
        payload = event.get("payload", {})
        if kind == "plan_approved":
            plan.approved_revision = int(payload["revision"])
            plan.completed = False
            plan.completed_at = None
            return
        if kind == "plan_completed":
            plan.completed = True
            plan.completed_at = occurred_at
            return
        if kind == "plan_cancelled":
            plan.cancelled = True
            plan.cancelled_at = occurred_at
            return
        task_id = event.get("task_id")
        if not task_id:
            return
        task = plan.tasks.setdefault(task_id, TaskState())
        if kind in {"task_claimed", "task_heartbeat"}:
            task.status = "claimed"
            task.owner = event["actor"]
            task.session = event["session"]
            task.lease_until = payload["lease_until"]
            task.worktree = payload.get("worktree", task.worktree)
            if kind == "task_claimed":
                task.model = payload.get("model")
                task.tier = payload.get("tier")
                task.tier_override = bool(payload.get("tier_override", False))
            return
        if kind == "task_checkpoint":
            task.summary = payload.get("summary")
            task.evidence.extend(payload.get("evidence", []))
            task.lease_until = payload.get("lease_until", task.lease_until)
            return
        if kind == "task_blocked":
            task.status = "blocked"
            task.reason = payload["reason"]
            task.owner = None
            task.session = None
            task.lease_until = None
            return
        if kind == "task_released":
            task.status = "ready"
            task.owner = None
            task.session = None
            task.lease_until = None
            return
        if kind == "task_unblocked":
            task.status = "ready"
            task.reason = None
            return
        if kind == "task_completed":
            task.status = "completed"
            task.summary = payload.get("summary", task.summary)
            task.evidence.extend(payload.get("evidence", []))
            task.owner = None
            task.session = None
            task.lease_until = None
            if payload.get("completes_plan"):
                plan.completed = True
                plan.completed_at = occurred_at


def load_state(root: Path) -> State:
    state = State()
    events_root = root / "memory" / "events"
    if not events_root.exists():
        return state
    for path in sorted(events_root.rglob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
            state.apply(event)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid event {path}: {exc}") from exc
    return state


def load_plan(root: Path, plan_id: str, revision: int | None = None) -> dict[str, Any]:
    revisions = root / "memory" / "plans" / plan_id / "revisions"
    if revision is None:
        candidates = sorted(revisions.glob("*.yaml"))
        if not candidates:
            raise ValueError(f"Unknown plan: {plan_id}")
        path = candidates[-1]
    else:
        path = revisions / f"{revision:04d}.yaml"
    if not path.exists():
        raise ValueError(f"Unknown plan revision: {plan_id}@{revision}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid plan revision: {path}")
    return data


def validate_plan(plan: dict[str, Any], policy: TierPolicy | None = None) -> None:
    required = {"id", "title", "goal", "tasks"}
    missing = sorted(required - plan.keys())
    if missing:
        raise ValueError(f"Plan is missing: {', '.join(missing)}")
    if not isinstance(plan["tasks"], list) or not plan["tasks"]:
        raise ValueError("Plan must contain at least one task")
    task_ids: set[str] = set()
    for task in plan["tasks"]:
        if not isinstance(task, dict) or not task.get("id") or not task.get("title"):
            raise ValueError("Every task requires id and title")
        if not task.get("read_only") and not task.get("project"):
            raise ValueError(f"Writing task {task['id']} requires a project id")
        task_id = str(task["id"])
        if task_id in task_ids:
            raise ValueError(f"Duplicate task id: {task_id}")
        task_ids.add(task_id)
        tier = task.get("tier")
        if tier is not None and not isinstance(tier, str):
            raise ValueError(f"Task {task_id} tier must be a string")
        if policy is not None and tier is not None and tier not in policy.tiers:
            raise ValueError(f"Task {task_id} uses unknown tier: {tier}")
    for task in plan["tasks"]:
        unknown = set(task.get("depends_on", [])) - task_ids
        if unknown:
            raise ValueError(f"Task {task['id']} has unknown dependencies: {sorted(unknown)}")
