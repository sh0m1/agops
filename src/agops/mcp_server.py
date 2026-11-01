from __future__ import annotations

import os
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import views
from .hub import Hub, task_definition
from .notes import NotesBridge
from .state import load_plan, load_state

SESSION = os.environ.get("AGOPS_SESSION", str(uuid.uuid4()))
ACTOR = os.environ.get("AGOPS_ACTOR", "mcp-agent")
INSTRUCTIONS = (
    "Follow the agops rules in your instruction file: read hub_get_brief once per session with "
    "your model id, claim before you write, and complete only with evidence."
)
mcp = FastMCP("agops", instructions=INSTRUCTIONS)


def hub() -> Hub:
    return Hub.from_environment()


@mcp.tool()
def hub_get_brief(cwd: str = ".", model: str | None = None) -> str:
    """Get bounded context, active plans, the tasks this session's tier may claim, and the
    user's open todos that link plans (when the notes folder names a todo file).

    Pass your current model id (for example claude-sonnet-5) so the hub can record the
    session's tier; call again if the model changes.
    """
    return hub().brief(Path(cwd), model=model, actor=ACTOR, session=SESSION)


@mcp.tool()
def hub_search_knowledge(
    query: str, cwd: str | None = None, include_archives: bool = False
) -> list[dict[str, str]]:
    """Search knowledge: at most 8 hits with key, title, scope, kind and a short body excerpt.

    Pass cwd to rank the entries of that directory's project first. Archives are left out
    unless include_archives is true.
    """
    return hub().search(query, include_archives=include_archives, cwd=Path(cwd) if cwd else None)


@mcp.tool()
def hub_list_plans() -> list[dict]:
    """List draft, active, completed, and cancelled plans."""
    return hub().list_plans()


@mcp.tool()
def hub_read_plan(
    plan_id: str,
    revision: int | None = None,
    task_id: str | None = None,
    full: bool = False,
) -> dict:
    """Read a plan. The default is compact: id, title, goal, and per task its id, title, tier,
    status and dependencies, with acceptance listed only for tasks that are not completed.

    Pass task_id for one task in full (every field plus status, owner and latest summary), or
    full=True for the whole plan definition. Approval is deliberately unavailable through MCP.
    """
    current = hub()
    plan = load_plan(current.root, plan_id, revision)
    if full and not task_id:
        return plan
    plan_state = load_state(current.root).plans.get(str(plan.get("id", plan_id)))
    if task_id:
        return views.task_detail(task_definition(plan, task_id), plan_state)
    return views.compact_plan(plan, plan_state, views.policy_or_none(current))


@mcp.tool()
def hub_list_ready_tasks(plan_id: str | None = None) -> list[dict]:
    """List dependency-ready tasks from approved plans as plan_id, id, title, tier, project and
    depends_on. Read a task's acceptance with hub_read_plan(plan_id, task_id=...)."""
    current = hub()
    policy = views.policy_or_none(current)
    return [views.compact_ready_task(task, policy) for task in current.ready_tasks(plan_id)]


@mcp.tool()
def hub_draft_plan(source_path: str) -> dict:
    """Draft a new immutable plan revision from a YAML file."""
    return hub().draft_plan(Path(source_path), ACTOR, SESSION)


@mcp.tool()
def hub_claim_task(plan_id: str, task_id: str, cwd: str = ".") -> dict:
    """Atomically claim a ready task with a renewable 60-minute lease."""
    return hub().claim_task(plan_id, task_id, ACTOR, SESSION, Path(cwd))


@mcp.tool()
def hub_checkpoint_task(
    plan_id: str, task_id: str, summary: str, evidence: list[str] | None = None
) -> dict:
    """Publish a progress checkpoint and renew the task lease.

    Cite any page made for the task (the path hub_add_page returns) in the evidence.
    """
    return hub().checkpoint_task(plan_id, task_id, ACTOR, SESSION, summary, evidence or [])


@mcp.tool()
def hub_heartbeat_task(plan_id: str, task_id: str) -> dict:
    """Renew the current session's task lease without changing its checkpoint summary."""
    return hub().heartbeat_task(plan_id, task_id, ACTOR, SESSION)


@mcp.tool()
def hub_block_task(plan_id: str, task_id: str, reason: str) -> dict:
    """Block and release a claimed task with a concrete reason."""
    return hub().block_task(plan_id, task_id, ACTOR, SESSION, reason)


@mcp.tool()
def hub_unblock_task(plan_id: str, task_id: str, resolution: str) -> dict:
    """Return a blocked task to ready state after recording its resolution."""
    return hub().unblock_task(plan_id, task_id, ACTOR, SESSION, resolution)


@mcp.tool()
def hub_retire_knowledge(scope: str, key: str, reason: str) -> dict:
    """Retire a stale knowledge entry while preserving its history."""
    return hub().retire_knowledge(scope, key, reason, ACTOR, SESSION)


@mcp.tool()
def hub_release_task(plan_id: str, task_id: str) -> dict:
    """Release a claimed task for another agent."""
    return hub().release_task(plan_id, task_id, ACTOR, SESSION)


@mcp.tool()
def hub_complete_task(
    plan_id: str,
    task_id: str,
    evidence: list[str],
    summary: str = "Completed",
) -> dict:
    """Complete a claimed task; at least one evidence item is required."""
    return hub().complete_task(plan_id, task_id, ACTOR, SESSION, summary, evidence)


@mcp.tool()
def hub_add_knowledge(
    scope: str,
    key: str,
    title: str,
    body: str,
    supersedes: str | None = None,
    kind: str = "fact",
) -> dict:
    """Add a versioned knowledge entry after secret and size validation.

    Keep the body under 500 bytes and link a page for detail; a longer body adds a `warning`
    key to the result.
    """
    event = hub().add_knowledge(scope, key, title, body, ACTOR, SESSION, supersedes, kind=kind)
    warning = views.knowledge_size_warning(body)
    return {**event, "warning": warning} if warning else event


@mcp.tool()
def hub_add_page(
    file_path: str,
    plan_id: str,
    task_id: str | None = None,
    title: str | None = None,
    slug: str | None = None,
) -> dict:
    """Store a self-contained HTML page for a plan (and task), tag it, and link it in the notes.

    Returns the stored path and the vault path. Cite the vault path in the task checkpoint.
    """
    return NotesBridge(hub()).add_page(Path(file_path), plan_id, task_id, title, slug)


def main() -> None:
    mcp.run(transport="stdio")
