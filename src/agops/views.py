"""Compact text and dict views of hub data, so agents read a few lines instead of full JSON.

Every helper is pure: it takes data the hub already returned and formats it. The CLI prints the
text views by default and keeps the full JSON behind `--json`; the MCP server returns the dict
views by default and the full data on request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .policy import PolicyError, TierPolicy
from .state import PlanState, TaskState

GOAL_LIMIT = 300
KNOWLEDGE_BODY_LIMIT = 500
_SEP = " · "
_FRACTION = re.compile(r"\.\d+")


def collapse(text: Any, limit: int | None = None) -> str:
    """One line: whitespace runs become single spaces; cut with an ellipsis past `limit`."""
    flat = " ".join(str(text or "").split())
    if limit is not None and len(flat) > limit:
        return flat[: limit - 1].rstrip() + "…"
    return flat


def policy_or_none(hub: Any) -> TierPolicy | None:
    """The hub's tier policy; None when it is absent or invalid, so a view never fails on it."""
    try:
        return hub.policy()
    except PolicyError:
        return None


def task_tier(task: Mapping[str, Any], policy: TierPolicy | None) -> str:
    if policy is not None:
        return policy.task_tier(dict(task))
    return str(task.get("tier") or "-")


def task_status(plan_state: PlanState | None, task_id: str) -> str:
    """The hub status of a task; a task with no events is ready, and so is one whose lease ended."""
    state = plan_state.tasks.get(task_id) if plan_state else None
    if state is None:
        return "ready"
    if state.status == "claimed" and not state.actively_claimed():
        return "ready"
    return state.status


def _state(plan_state: PlanState | None, task_id: str) -> TaskState | None:
    return plan_state.tasks.get(task_id) if plan_state else None


def short_time(value: Any) -> str:
    """An ISO time without fractional seconds."""
    return _FRACTION.sub("", str(value or ""))


def format_ready_tasks(tasks: Sequence[Mapping[str, Any]], policy: TierPolicy | None) -> str:
    if not tasks:
        return "No ready tasks."
    return "\n".join(
        _SEP.join(
            (
                f"{task['plan_id']}/{task['id']}",
                task_tier(task, policy),
                collapse(task.get("title")),
            )
        )
        for task in tasks
    )


def compact_ready_task(task: Mapping[str, Any], policy: TierPolicy | None) -> dict[str, Any]:
    return {
        "plan_id": task["plan_id"],
        "id": task["id"],
        "title": task.get("title"),
        "tier": task_tier(task, policy),
        "project": task.get("project"),
        "depends_on": list(task.get("depends_on") or []),
    }


_EVENT_WORDS = {
    "task_claimed": "claimed",
    "task_checkpoint": "checkpointed",
    "task_blocked": "blocked",
    "task_unblocked": "unblocked",
    "task_released": "released",
    "task_completed": "completed",
}


def format_task_event(event: Mapping[str, Any]) -> str:
    """One line for the event a claim, checkpoint, block, unblock, release or complete returns."""
    ref = f"{event['plan_id']}/{event['task_id']}"
    if event.get("queued"):
        reason = collapse(event.get("reason"), 120)
        return f"queued checkpoint {ref}; the hub was unreachable ({reason})"
    kind = str(event.get("type"))
    payload = event.get("payload") or {}
    line = f"{_EVENT_WORDS.get(kind, kind)} {ref}"
    if kind == "task_claimed" and payload.get("lease_until"):
        line += f"{_SEP}lease until {short_time(payload['lease_until'])}"
    if kind == "task_completed" and payload.get("completes_plan"):
        line += f"{_SEP}plan complete"
    return line


def format_page(result: Mapping[str, Any]) -> str:
    return str(result.get("vault_path") or result.get("page"))


def format_knowledge_event(event: Mapping[str, Any]) -> str:
    payload = event.get("payload") or {}
    action = "retired" if payload.get("status") == "retired" else "added"
    return f"knowledge {payload.get('scope')}/{payload.get('key')} {action}"


def knowledge_size_warning(body: str) -> str | None:
    """The warning for a knowledge body over the soft limit, or None."""
    size = len(body.strip().encode("utf-8"))
    if size <= KNOWLEDGE_BODY_LIMIT:
        return None
    return (
        f"knowledge body is {size} bytes; keep it under {KNOWLEDGE_BODY_LIMIT} "
        "and link a page for detail"
    )


def format_session(record: Mapping[str, Any]) -> str:
    line = _SEP.join(
        (
            f"session {record['session']}",
            str(record["model"]),
            f"tier={record['tier'] or 'unenforced'}",
        )
    )
    if record.get("warning"):
        line += f"\nWarning: {record['warning']}"
    return line


def _open_acceptance(task: Mapping[str, Any]) -> list[str]:
    return [collapse(item) for item in task.get("acceptance") or []]


def compact_plan(
    plan: Mapping[str, Any], plan_state: PlanState | None, policy: TierPolicy | None
) -> dict[str, Any]:
    """The plan without long fields and without finished work.

    Only tasks that are not completed are listed, with their acceptance. Completed tasks appear
    as the `completed` count next to the `total` count of all tasks.
    """
    tasks: list[dict[str, Any]] = []
    completed = 0
    for task in plan["tasks"]:
        status = task_status(plan_state, task["id"])
        if status == "completed":
            completed += 1
            continue
        entry: dict[str, Any] = {
            "id": task["id"],
            "title": task.get("title"),
            "tier": task_tier(task, policy),
            "status": status,
            "depends_on": list(task.get("depends_on") or []),
        }
        if task.get("acceptance"):
            entry["acceptance"] = _open_acceptance(task)
        tasks.append(entry)
    return {
        "id": plan.get("id"),
        "title": plan.get("title"),
        "goal": collapse(plan.get("goal"), GOAL_LIMIT),
        "tasks": tasks,
        "completed": completed,
        "total": len(plan["tasks"]),
    }


def format_plan(
    plan: Mapping[str, Any], plan_state: PlanState | None, policy: TierPolicy | None
) -> str:
    compact = compact_plan(plan, plan_state, policy)
    completed, total = compact["completed"], compact["total"]
    lines = [f"{compact['id']}{_SEP}{compact['title']}", f"Goal: {compact['goal']}"]
    if total and completed == total:
        lines.append(f"All {total} task(s) completed.")
    elif completed:
        lines.append(
            f"Completed: {completed} of {total} task(s); "
            "`--task <id>` shows one task, `--full` the whole plan."
        )
    for task in compact["tasks"]:
        lines.append(f"- {task['id']} [{task['status']}, {task['tier']}] {collapse(task['title'])}")
        lines.extend(f"    - {item}" for item in task.get("acceptance", []))
    return "\n".join(lines)


def task_detail(task: Mapping[str, Any], plan_state: PlanState | None) -> dict[str, Any]:
    """The whole task definition plus its hub status, owner and latest summary."""
    detail: dict[str, Any] = dict(task)
    detail["status"] = task_status(plan_state, task["id"])
    state = _state(plan_state, task["id"])
    if state is not None:
        if state.owner:
            detail["owner"] = state.owner
        if state.lease_until and detail["status"] == "claimed":
            detail["lease_until"] = state.lease_until
        if state.summary:
            detail["summary"] = state.summary
        if state.reason and detail["status"] == "blocked":
            detail["reason"] = state.reason
    return detail


def _field_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [f"{key}: {json.dumps(value, sort_keys=True)}"]
    if isinstance(value, list):
        items = [json.dumps(i) if isinstance(i, Mapping) else collapse(i) for i in value]
        if not items:
            return [f"{key}: -"]
        if key != "acceptance" and all(len(item) <= 40 for item in items):
            return [f"{key}: {', '.join(items)}"]
        return [f"{key}:", *(f"  - {item}" for item in items)]
    if isinstance(value, str) and "\n" in value.strip():
        return [f"{key}:", *(f"  {line}" for line in value.strip().splitlines())]
    return [f"{key}: {value}"]


def format_task_detail(plan_id: str, detail: Mapping[str, Any]) -> str:
    """Every field of one task, one per line; the status fields come last."""
    status_keys = ("status", "owner", "lease_until", "reason", "summary")
    skip = {"id", "title", *status_keys}
    lines = [f"{plan_id}/{detail['id']}{_SEP}{collapse(detail.get('title'))}"]
    for key, value in detail.items():
        if key not in skip:
            lines.extend(_field_lines(key, value))
    for key in status_keys:
        if key in detail:
            lines.extend(_field_lines(key, detail[key]))
    return "\n".join(lines)
