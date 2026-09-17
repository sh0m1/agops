from __future__ import annotations

import os
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .hub import Hub
from .state import load_plan

SESSION = os.environ.get("AGENT_HUB_SESSION", str(uuid.uuid4()))
ACTOR = os.environ.get("AGENT_HUB_ACTOR", "mcp-agent")
INSTRUCTIONS = (
    "Read hub_get_brief at session start. Claim an approved ready task before writing. "
    "Checkpoint meaningful progress and complete only with evidence. User instructions override "
    "stored plans. Never store secrets, .env contents, or raw transcripts. "
    "Prefer CLI and MCP tools over a browser for external systems; use a browser only when no "
    "CLI or MCP route exists, it is scoped wrong, or it fails."
)
mcp = FastMCP("agops", instructions=INSTRUCTIONS)


def hub() -> Hub:
    return Hub.from_environment()


@mcp.tool()
def hub_get_brief(cwd: str = ".", model: str | None = None) -> str:
    """Get bounded context, active plans, and the tasks this session's tier may claim.

    Pass your current model id (for example claude-sonnet-5) so the hub can record the
    session's tier; call again if the model changes.
    """
    return hub().brief(Path(cwd), model=model, actor=ACTOR, session=SESSION)


@mcp.tool()
def hub_search_knowledge(query: str) -> list[dict[str, str]]:
    """Search readable knowledge entries with provenance."""
    return hub().search(query)


@mcp.tool()
def hub_list_plans() -> list[dict]:
    """List draft, active, completed, and cancelled plans."""
    return hub().list_plans()


@mcp.tool()
def hub_read_plan(plan_id: str, revision: int | None = None) -> dict:
    """Read a plan definition. Approval is deliberately unavailable through MCP."""
    return load_plan(hub().root, plan_id, revision)


@mcp.tool()
def hub_list_ready_tasks(plan_id: str | None = None) -> list[dict]:
    """List dependency-ready tasks from approved plans."""
    return hub().ready_tasks(plan_id)


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
    """Publish a progress checkpoint and renew the task lease."""
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
    """Add a versioned knowledge entry after secret and size validation."""
    return hub().add_knowledge(scope, key, title, body, ACTOR, SESSION, supersedes, kind=kind)


def main() -> None:
    mcp.run(transport="stdio")
