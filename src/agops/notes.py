"""A deliberately small, app-neutral Markdown mirror for plan definitions.

The files are ordinary Markdown.  Obsidian, Logseq, Foam, and a plain editor can all
use them; agops only owns the marked regions and the small sidecar baseline.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import html
import json
import os
import posixpath
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .config import config_path, load_profiles, save_profiles
from .git import remote_url
from .ids import normalize_remote, slug
from .security import SECRET_PATTERNS, validate_content
from .state import (
    PlanState,
    State,
    TaskState,
    load_plan,
    load_state,
    parse_time,
    utc_now,
    validate_plan,
)

if TYPE_CHECKING:
    from .hub import Hub

FORMAT_VERSION = 1
SIDECAR = ".agops-notes.json"
PERSONAL_START = "<!-- agops:personal:start -->"
PERSONAL_END = "<!-- agops:personal:end -->"
# The fenced definition block of the old note layout; read only to migrate such a note.
DEFINITION_START = "<!-- agops:definition:start -->"
DEFINITION_END = "<!-- agops:definition:end -->"
DEFINITIONS_DIR = "plans/.definitions"
STALE_DAYS = 7
RETIRE_MIN_AGE_DAYS = 3
LEGACY_AGOPS_BASE = """\
formulas:
  idle: 'today() - agops_last_activity'
properties:
  agops_title:
    displayName: Plan
  agops_workspace:
    displayName: Workspace
  agops_health:
    displayName: Health
  agops_last_activity:
    displayName: Last activity
  formula.idle:
    displayName: Idle
views:
  - type: table
    name: Active plans
    filters:
      and:
        - file.hasProperty("agops_id")
        - 'agops_status == "active"'
    order:
      - file.name
      - agops_title
      - agops_workspace
      - agops_health
      - agops_last_activity
      - formula.idle
      - agops_tasks_open
      - agops_tasks_blocked
    sort:
      - property: agops_last_activity
        direction: DESC
  - type: table
    name: Stalled
    filters:
      and:
        - file.hasProperty("agops_id")
        - 'agops_status == "active"'
        - or:
            - 'agops_health == "stalled"'
            - 'agops_health == "blocked"'
    order:
      - file.name
      - agops_title
      - agops_workspace
      - agops_health
      - agops_last_activity
      - formula.idle
      - agops_tasks_open
      - agops_tasks_blocked
    sort:
      - property: agops_last_activity
        direction: ASC
  - type: table
    name: By workspace
    filters:
      and:
        - file.hasProperty("agops_id")
        - 'agops_status == "active"'
    order:
      - file.name
      - agops_title
      - agops_workspace
      - agops_health
      - agops_last_activity
      - formula.idle
      - agops_tasks_open
      - agops_tasks_blocked
    groupBy:
      property: agops_workspace
      direction: ASC
  - type: table
    name: Recently completed
    filters:
      and:
        - file.hasProperty("agops_id")
        - 'agops_status == "completed"'
        - 'agops_completed > today() - "14d"'
    order:
      - file.name
      - agops_title
      - agops_workspace
      - agops_completed
    sort:
      - property: agops_completed
        direction: DESC
  - type: table
    name: Knowledge
    filters:
      and:
        - file.hasProperty("agops_knowledge_id")
    order:
      - file.name
      - agops_kind
      - agops_plan
      - agops_plan_status
      - agops_created_at
    groupBy:
      property: agops_workspace
      direction: ASC
  - type: table
    name: Retire candidates
    filters:
      and:
        - file.hasProperty("agops_knowledge_id")
        - 'agops_kind == "fact"'
        - or:
            - 'agops_plan_status == "completed"'
            - 'agops_plan_status == "cancelled"'
    order:
      - file.name
      - agops_plan
      - agops_plan_status
      - agops_created_at
"""
# The default written before drafts and knowledge topics had views; an untouched copy upgrades.
PREVIOUS_AGOPS_BASE = """\
formulas:
  idle: 'today() - last_activity'
properties:
  status:
    displayName: Status
  health:
    displayName: Health
  workspace:
    displayName: Workspace
  progress:
    displayName: Done
  last_activity:
    displayName: Last activity
  formula.idle:
    displayName: Idle
views:
  - type: table
    name: Active plans
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "active"'
    order:
      - file.name
      - workspace
      - health
      - progress
      - last_activity
      - formula.idle
    sort:
      - property: last_activity
        direction: DESC
  - type: table
    name: Stalled
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "active"'
        - or:
            - 'health == "stalled"'
            - 'health == "blocked"'
    order:
      - file.name
      - workspace
      - health
      - progress
      - last_activity
      - formula.idle
    sort:
      - property: last_activity
        direction: ASC
  - type: table
    name: By workspace
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "active"'
    order:
      - file.name
      - health
      - progress
      - last_activity
      - formula.idle
    groupBy:
      property: workspace
      direction: ASC
  - type: table
    name: Recently completed
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "completed"'
        - 'last_activity > today() - "14d"'
    order:
      - file.name
      - workspace
      - progress
      - last_activity
    sort:
      - property: last_activity
        direction: DESC
  - type: table
    name: Knowledge
    filters:
      and:
        - file.hasTag("agops/knowledge")
    order:
      - file.name
      - kind
      - plan
      - updated
    groupBy:
      property: scope
      direction: ASC
  - type: table
    name: Plan-linked facts
    filters:
      and:
        - file.hasTag("agops/knowledge")
        - 'kind == "fact"'
        - file.hasProperty("plan")
    order:
      - file.name
      - plan
      - updated
"""
AGOPS_BASE = """\
formulas:
  idle: 'today() - last_activity'
properties:
  status:
    displayName: Status
  health:
    displayName: Health
  workspace:
    displayName: Workspace
  progress:
    displayName: Done
  last_activity:
    displayName: Last activity
  formula.idle:
    displayName: Idle
  topics:
    displayName: Topics
  kind:
    displayName: Kind
  scope:
    displayName: Scope
  updated:
    displayName: Updated
views:
  - type: table
    name: Open plans
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status != "completed"'
        - 'status != "cancelled"'
    order:
      - file.name
      - status
      - health
      - workspace
      - progress
      - last_activity
      - formula.idle
    sort:
      - property: last_activity
        direction: DESC
  - type: table
    name: Stalled
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "active"'
        - or:
            - 'health == "stalled"'
            - 'health == "blocked"'
    order:
      - file.name
      - workspace
      - health
      - progress
      - last_activity
      - formula.idle
    sort:
      - property: last_activity
        direction: ASC
  - type: table
    name: By workspace
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status != "completed"'
        - 'status != "cancelled"'
    order:
      - file.name
      - status
      - health
      - progress
      - last_activity
      - formula.idle
    groupBy:
      property: workspace
      direction: ASC
  - type: table
    name: Recently completed
    filters:
      and:
        - file.hasTag("agops/plan")
        - 'status == "completed"'
        - 'last_activity > today() - "14d"'
    order:
      - file.name
      - workspace
      - progress
      - last_activity
    sort:
      - property: last_activity
        direction: DESC
  - type: table
    name: Knowledge
    filters:
      and:
        - file.hasTag("agops/knowledge")
    order:
      - file.name
      - kind
      - topics
      - plan
      - updated
    groupBy:
      property: scope
      direction: ASC
    sort:
      - property: updated
        direction: DESC
  - type: table
    name: Decisions and preferences
    filters:
      and:
        - file.hasTag("agops/knowledge")
        - or:
            - 'kind == "decision"'
            - 'kind == "preference"'
    order:
      - file.name
      - kind
      - topics
      - scope
      - updated
    sort:
      - property: updated
        direction: DESC
  - type: table
    name: Plan-linked facts
    filters:
      and:
        - file.hasTag("agops/knowledge")
        - 'kind == "fact"'
        - file.hasProperty("plan")
    order:
      - file.name
      - plan
      - updated
"""
SETTINGS_FILE = "notes.yaml"
NOTES_SETTINGS = """\
# agops notes settings. agops writes this file once and never changes it; edit it freely.
#
# docs: folders of Markdown (and the images they use) copied read-only into
# docs/<folder name>/ on every sync, so vault search and plan notes reach long-form
# documents. Edit the source folder: a copy edited in the vault is no longer refreshed.
#   docs:
#     - ~/work/agent-knowledge
docs: []
#
# topics: tags plans and knowledge (topic/<name>) and groups Knowledge.md. A note gets a
# topic when one of its projects matches a `projects` glob or its text has a `keywords` word.
#   topics:
#     networking:
#       projects: ["*-nlb", "*-vpc"]
#       keywords: [nlb, vpc, vpn]
topics: {}
"""
DOCS_DIR = "docs"
DOC_SUFFIXES = frozenset({".md", ".html", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"})
# Agents' web pages (diagrams, interactive views) are mirrored as pages beside the docs.
PAGE_SUFFIX = ".html"
MAX_DOC_BYTES = 10 * 1024 * 1024
# Files an earlier notes layout generated. They are removed when they still carry its markers.
LEGACY_VIEWS = ("views/Plans.base", "views/Knowledge.base", "views/Projects.base")
LEGACY_PROJECTS_DIR = "projects"
HOME_EVENT_DAYS = 7
HOME_EVENT_LIMIT = 15
HOME_KNOWLEDGE_LIMIT = 8
HOME_PAGE_LIMIT = 5
ACTIVITY_EVENT_DAYS = 30
ACTIVITY_EVENT_LIMIT = 200
BUCKET_TITLES = {
    "claimed": "In progress",
    "blocked": "Blocked",
    "next": "Next",
    "waiting": "Waiting on other tasks",
    "done": "Done",
}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_DEFINITION = re.compile(
    re.escape(DEFINITION_START) + r"\s*```ya?ml\n(.*?)```\s*" + re.escape(DEFINITION_END),
    re.DOTALL,
)
_PERSONAL = re.compile(
    re.escape(PERSONAL_START) + r"\n?(.*?)" + re.escape(PERSONAL_END), re.DOTALL
)
_KEY_DATE_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
_ID_DATE_SUFFIX = re.compile(r"-\d{8}$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`#(\[\"'])")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_PAGE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DOC_LINK = re.compile(r"\]\(([^)\s<>]+?\.md)(#[^)\s<>]*)?\)")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as raw:
        raw.write(content)
        name = raw.name
    os.replace(name, path)


def _write_if_changed(path: Path, content: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        _atomic_write(path, content)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tidy(lines: list[str]) -> str:
    """Generated index text: one blank line between blocks, one final newline."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n") + "\n"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value is None else [value])


def _paragraphs(value: Any) -> list[str]:
    """A string or list field as single-line paragraphs; each list item is its own line."""
    paragraphs: list[str] = []
    for item in _as_list(value):
        for block in re.split(r"\n\s*\n", str(item)):
            lines = [line for line in block.splitlines() if line.strip()]
            if sum(1 for line in lines if _LIST_MARKER.match(line)) > 1:
                paragraphs.extend(" ".join(_LIST_MARKER.sub("", line).split()) for line in lines)
            elif lines:
                paragraphs.append(" ".join(_LIST_MARKER.sub("", " ".join(lines)).split()))
    return [text for text in paragraphs if text]


def _lead(text: str, limit: int = 200) -> tuple[str, str]:
    """Whole sentences of `text` up to about `limit` characters, and the rest of it.

    A first sentence far over the limit is cut, and the rest is then the whole text.
    """
    sentences = _SENTENCE_END.split(" ".join(text.split()))
    lead, used = sentences[0], 1
    while used < len(sentences) and len(lead) + 1 + len(sentences[used]) <= limit:
        lead, used = f"{lead} {sentences[used]}", used + 1
    if len(lead) > limit + 40:
        return _short(lead, limit), " ".join(sentences)
    return lead, " ".join(sentences[used:])


def _href(from_note: str, to_note: str) -> str:
    """A relative Markdown link target between two vault-relative paths."""
    relative = posixpath.relpath(to_note, posixpath.dirname(from_note) or ".")
    return f"<{relative}>" if re.search(r"[\s()<>]", relative) else relative


def _link(text: str, from_note: str, to_note: str) -> str:
    return f"[{text}]({_href(from_note, to_note)})"


def _vault_prefix(target: Path) -> str | None:
    """The notes folder's path inside its Obsidian vault ("" at the root), None outside one."""
    resolved = target.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".obsidian").is_dir():
            relative = resolved.relative_to(candidate).as_posix()
            return "" if relative == "." else f"{relative}/"
    return None


def _doc_name(inner: str, folder: str) -> str:
    """A mirrored doc's name: `workstreams/x.md` becomes `workstreams/x (workstreams).md`.

    Notes link plans by bare name (`[[network-isolation]]`), and a workstream doc is often
    named after its plan; the suffix keeps every copy from capturing such a link.
    """
    parent = posixpath.dirname(inner)
    stem = posixpath.splitext(posixpath.basename(inner))[0]
    return posixpath.join(parent, f"{stem} ({posixpath.basename(parent) or folder}).md")


def _rewrite_doc_links(text: str, inner: str, renamed: dict[str, str]) -> str:
    """Point relative Markdown links between mirrored docs at the renamed copies."""
    base = posixpath.dirname(inner)

    def replace(match: re.Match[str]) -> str:
        target, anchor = match.group(1), match.group(2) or ""
        if "://" in target or target.startswith("/"):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(base, target))
        if resolved not in renamed:
            return match.group(0)
        return f"](<{posixpath.relpath(renamed[resolved], base or '.')}{anchor}>)"

    return _DOC_LINK.sub(replace, text)


def _wikilink(prefix: str | None, note_path: str, label: str) -> str:
    """A property link. Inside a vault it is a full path, so a doc of the same name never wins."""
    if prefix is None:
        return f"[[{label}]]"
    return f"[[{prefix}{note_path.removesuffix('.md')}|{label}]]"


def _topic_tag(name: str) -> str:
    return re.sub(r"[^a-z0-9/_-]+", "-", name.strip().lower()).strip("-")


def _topics(rules: dict[str, dict[str, list[str]]], projects: list[str], text: str) -> list[str]:
    """The configured topics a note belongs to, in configuration order."""
    lowered = text.lower()
    found: list[str] = []
    for name, rule in rules.items():
        by_project = any(
            fnmatch.fnmatchcase(project, pattern)
            for project in projects
            for pattern in rule["projects"]
        )
        by_keyword = any(
            re.search(rf"\b{re.escape(word.lower())}\b", lowered) for word in rule["keywords"]
        )
        if by_project or by_keyword:
            found.append(name)
    return found


def _with_topic_tags(tags: list[Any], base: list[str], topics: list[str]) -> list[Any]:
    """Custom tags kept, agops tags added; `topic/*` tags are agops-owned and recomputed."""
    custom = [tag for tag in tags if not str(tag).startswith("topic/")]
    return list(dict.fromkeys([*custom, *base, *(f"topic/{topic}" for topic in topics)]))


def _task_buckets(
    execution_plan: dict[str, Any], state: PlanState, now: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Tasks by where they stand: claimed, blocked, next (can start), waiting, done."""
    tasks = execution_plan.get("tasks", [])
    done_ids = {
        task["id"]
        for task in tasks
        if (state.tasks.get(task["id"]) or TaskState()).status == "completed"
    }
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in BUCKET_TITLES}
    for task in tasks:
        current = state.tasks.get(task["id"])
        if task["id"] in done_ids:
            key = "done"
        elif current and current.status == "blocked":
            key = "blocked"
        elif current and current.actively_claimed(now):
            key = "claimed"
        elif any(dep not in done_ids for dep in task.get("depends_on", [])):
            key = "waiting"
        else:
            key = "next"
        buckets[key].append(task)
    return buckets


def _revision_changes(approved: dict[str, Any], latest: dict[str, Any]) -> dict[str, list[str]]:
    old = {task["id"]: task for task in approved.get("tasks", [])}
    new = {task["id"]: task for task in latest.get("tasks", [])}
    return {
        "added": [task_id for task_id in new if task_id not in old],
        "removed": [task_id for task_id in old if task_id not in new],
        "changed": [task_id for task_id in new if task_id in old and new[task_id] != old[task_id]],
    }


def _changes_text(changes: dict[str, list[str]] | None) -> str:
    if not changes:
        return ""
    parts = []
    for key, verb in (("added", "adds"), ("removed", "removes"), ("changed", "changes")):
        ids = changes.get(key) or []
        if ids:
            shown = ", ".join(f"`{task_id}`" for task_id in ids[:8])
            more = f" and {len(ids) - 8} more" if len(ids) > 8 else ""
            parts.append(f"{verb} {shown}{more}")
    return "; ".join(parts)


def _task_callout(
    task: dict[str, Any],
    current: TaskState | None,
    approved: bool,
    done_ids: set[str],
    now: datetime,
) -> list[str]:
    """A task as a folded Obsidian callout coloured by status, with its gist inside.

    `todo` (blue) is open, `tip` (cyan) is being worked on, `failure` (red) is blocked, and
    `done` (green) is complete. Other Markdown apps show a plain quote with the same text.
    """
    title = " ".join(str(task["title"]).split())
    waiting = [dep for dep in task.get("depends_on", []) if dep not in done_ids]
    if task["id"] in done_ids:
        kind, detail = "done", ""
    elif current and current.status == "blocked":
        kind = "failure"
        detail = "blocked" + (f": {_short(current.reason, 100)}" if current.reason else "")
    elif current and current.actively_claimed(now):
        kind, detail = "tip", f"claimed by {current.owner or 'unknown'}"
    elif not approved:
        kind, detail = "todo", "draft"
    elif waiting:
        kind, detail = "todo", "after " + ", ".join(f"`{dep}`" for dep in waiting)
    else:
        kind, detail = "todo", "ready"
    head = f"> [!{kind}]- `{task['id']}` {title}" + (f" — {detail}" if detail else "")
    return [head, *(f"> {line}".rstrip() for line in _task_body(task, current))]


def _task_body(task: dict[str, Any], current: TaskState | None) -> list[str]:
    """The gist as the first line, then the rest of the text and the facts as bullets."""
    summary = " ".join(str(task.get("summary") or "").split())
    notes = _paragraphs(task.get("notes"))
    instructions = _paragraphs(task.get("instructions"))
    acceptance = _paragraphs(task.get("acceptance"))
    gist, details = "", []
    if summary:
        gist, details = summary, notes
    elif notes:
        gist, rest = _lead(notes[0])
        details = [rest, *notes[1:]]
    elif instructions:
        gist, rest = _lead(instructions[0])
        details, instructions = [rest, *instructions[1:]], []
    bullets = [f"- {item}" for item in details if item]
    for label, items in (("Instructions", instructions), ("Done when", acceptance)):
        if items:
            bullets.append(f"- **{label}:** {items[0]}")
            bullets.extend(f"  - {item}" for item in items[1:])
    facts = [f"`{task['project']}`"] if task.get("project") else []
    for key, label in (("tier", "tier"), ("model", "model"), ("reasoning_effort", "effort")):
        if task.get(key):
            facts.append(f"{label} {task[key]}")
    if task.get("read_only"):
        facts.append("read-only")
    for key, label in (("write_scope", "writes"), ("depends_on", "after"), ("covers", "covers")):
        if task.get(key):
            facts.append(f"{label} {_code_items(task[key])}")
    if facts:
        bullets.append("- " + " · ".join(facts))
    if current and current.summary:
        label = "Result" if current.status == "completed" else "Latest"
        bullets.append(f"- **{label}:** {_short(current.summary, 160)}")
    if current and current.status == "completed" and current.evidence:
        bullets.append("- **Evidence:** " + _short("; ".join(current.evidence), 200))
    lines = [gist] if gist else []
    if gist and bullets:
        lines.append("")
    return lines + bullets


def _inline(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_inline(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(_inline(item) for item in value)
    return str(value)


def _code_items(value: Any) -> str:
    return ", ".join(f"`{item}`" for item in _as_list(value))


def _quoted(text: str) -> list[str]:
    return [f"> {line}".rstrip() if line else ">" for line in text.strip("\n").split("\n")]


def _plan_details(plan: dict[str, Any]) -> list[str]:
    """Plan-level definition fields in readable form; tasks are shown once, under Tasks."""

    def labelled(label: str, value: Any) -> str:
        text = str(value).strip()
        return f"- **{label}:**{chr(10) if chr(10) in text else ' '}{text}"

    lines = ["", "## Plan details", ""]
    scope = plan.get("scope")
    if isinstance(scope, dict) and scope:
        lines.append("**Scope:**")
        lines.extend(labelled(key, _inline(value)) for key, value in scope.items())
        lines.append("")
    policy = plan.get("execution_policy")
    if isinstance(policy, dict) and policy:
        lines.append("**Execution policy:**")
        lines.extend(labelled(key, _inline(value)) for key, value in policy.items())
        lines.append("")
    references = [
        (key, plan[key])
        for key in ("design_document", "review_document", "reviewed_heads", "milestones")
        if plan.get(key)
    ]
    if references:
        lines.append("**References:**")
        for key, value in references:
            if isinstance(value, dict):
                lines.append(f"- **{key}:**")
                lines.extend(f"  - **{name}:** {_inline(item)}" for name, item in value.items())
            elif isinstance(value, list):
                lines.append(f"- **{key}:**")
                lines.extend(f"  - {_inline(item)}" for item in value)
            else:
                lines.append(f"- **{key}:** {value}")
        lines.append("")
    revision = [f"revision {plan['revision']}"]
    for key in ("created_at", "created_by"):
        if plan.get(key):
            revision.append(f"{key} {plan[key]}")
    lines.append(f"**Revision:** {' · '.join(revision)}")
    return lines


def _definition_yaml(plan: dict[str, Any]) -> str:
    return yaml.safe_dump(_definition(plan), sort_keys=False, allow_unicode=True)


def _definition(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in plan.items()
        if key not in {"revision", "created_at", "created_by"}
    }


def definition_hash(plan: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(_definition(plan), sort_keys=True, allow_unicode=True)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def hub_fingerprint(root: Path) -> str:
    """A stable, non-secret owner marker shared by clones of the same remote hub."""
    remote = remote_url(root)
    identity = f"origin:{normalize_remote(remote)}" if remote else f"local:{root.resolve()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _legacy_root_fingerprint(root: Path) -> str:
    """Accept sidecars written before remote-aware ownership was introduced."""
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def _state_hash(state: PlanState) -> str:
    payload = {
        "approved_revision": state.approved_revision,
        "completed": state.completed,
        "cancelled": state.cancelled,
        "tasks": {
            task_id: {
                "status": task.status,
                "owner": task.owner,
                "summary": task.summary,
                "evidence": task.evidence,
                "reason": task.reason,
            }
            for task_id, task in sorted(state.tasks.items())
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _workspace_dir(workspace: str) -> str:
    try:
        return slug(workspace)
    except ValueError:
        return "unassigned"


def plan_note_path(plan_id: str, status: str, workspace: str) -> str:
    """A plan note's vault-relative home: open plans in active/, finished ones in archive/."""
    if status in {"completed", "cancelled"}:
        return f"plans/archive/{_workspace_dir(workspace)}/{plan_id}.md"
    return f"plans/active/{plan_id}.md"


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty directories below `root` (never `root` itself, never files)."""
    if not root.is_dir():
        return
    for directory, _, _ in os.walk(root, topdown=False):
        current = Path(directory)
        if current != root:
            with contextlib.suppress(OSError):
                current.rmdir()


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Read an agops memory file: a YAML mapping, then the body.  Malformed files yield {}."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text[match.end() :].strip()
    metadata = loaded if isinstance(loaded, dict) else {}
    return metadata, text[match.end() :].strip()


def _status(state: PlanState) -> str:
    if state.active:
        return "active"
    if state.completed:
        return "completed"
    if state.cancelled:
        return "cancelled"
    return "draft"


def idle_days(last_event_at: str | None, now: datetime | None = None) -> int | None:
    """Whole days since the plan's last event, or None if it never had one."""
    if not last_event_at:
        return None
    return ((now or utc_now()) - parse_time(last_event_at)).days


def _date_only(value: str | None) -> date | None:
    return parse_time(value).date() if value else None


def plan_task_counts(
    plan_state: PlanState, execution_plan: dict[str, Any], now: datetime | None = None
) -> dict[str, int]:
    """total/done/open/blocked/claimed counts over an execution plan's tasks."""
    now = now or utc_now()
    tasks = execution_plan.get("tasks", [])
    done = blocked = claimed = 0
    for task in tasks:
        task_state = plan_state.tasks.get(task["id"])
        status = task_state.status if task_state else "ready"
        if status == "completed":
            done += 1
        elif status == "blocked":
            blocked += 1
        if task_state and task_state.actively_claimed(now):
            claimed += 1
    total = len(tasks)
    return {
        "total": total,
        "done": done,
        "open": total - done,
        "blocked": blocked,
        "claimed": claimed,
    }


def plan_health(
    plan_state: PlanState, execution_plan: dict[str, Any], now: datetime | None = None
) -> str:
    now = now or utc_now()
    if plan_state.completed:
        return "done"
    if plan_state.cancelled:
        return "cancelled"
    if not plan_state.approved_revision:
        return "draft"
    if any(task.actively_claimed(now) for task in plan_state.tasks.values()):
        return "live"
    counts = plan_task_counts(plan_state, execution_plan, now)
    if counts["open"] and counts["open"] == counts["blocked"]:
        return "blocked"
    if plan_state.last_event_at and (idle_days(plan_state.last_event_at, now) or 0) >= STALE_DAYS:
        return "stalled"
    return "waiting"


def plan_project_ids(execution_plan: dict[str, Any]) -> list[str]:
    """Every project a plan touches: its declared scope plus each task's project."""
    scope = execution_plan.get("scope") or {}
    ids = {str(item) for item in (scope.get("projects") or [])}
    ids.update(
        str(task["project"]) for task in execution_plan.get("tasks", []) if task.get("project")
    )
    return sorted(ids)


def project_workspace(project_id: str, projects: list[dict[str, Any]]) -> str | None:
    """A project's workspace; an unregistered sub-project inherits its registered parent's.

    Plans often name `quinceai-stack-backend` while only `quinceai-stack` is registered.
    """
    workspace_by_project = {str(item["id"]): item.get("workspace") for item in projects}
    if workspace_by_project.get(project_id):
        return workspace_by_project[project_id]
    parents = [pid for pid in workspace_by_project if project_id.startswith(pid + "-")]
    return workspace_by_project[max(parents, key=len)] if parents else None


def plan_workspace(execution_plan: dict[str, Any], projects: list[dict[str, Any]]) -> str:
    """The workspace(s) a plan touches, derived from its scope and its tasks' projects."""
    workspaces = sorted(
        {
            workspace
            for pid in plan_project_ids(execution_plan)
            if (workspace := project_workspace(pid, projects))
        }
    )
    if not workspaces:
        return str(execution_plan["id"]).split("-", 1)[0]
    if len(workspaces) == 1:
        return workspaces[0]
    return ", ".join(workspaces)


def _knowledge_workspace(scope: str, projects: list[dict[str, Any]]) -> str:
    if scope == "global":
        return "global"
    if ":" not in scope:
        return scope
    kind, identifier = scope.split(":", 1)
    if kind != "project":
        return identifier
    return project_workspace(identifier, projects) or identifier


def link_plan(
    entry: dict[str, Any], plans: list[str], tasks: dict[str, str] | None = None
) -> str | None:
    """The plan a knowledge entry belongs to: by plan id mention, then task id mention, then key.

    A task id counts only when it is distinctive (hyphenated, 8+ characters) and appears as a
    whole word; `tasks` maps such ids to their plan. A key matches a plan by either of two
    stems: the plan id's *core* (id minus its first `-` segment, minus a trailing
    `-YYYYMMDD`), or the full id minus that trailing date — so a key that repeats the whole
    plan id still links. The longest match wins at every step.
    """
    haystack = f"{entry.get('title') or ''}\n{entry.get('body') or ''}"
    direct = [plan_id for plan_id in plans if plan_id in haystack]
    if direct:
        return max(direct, key=len)
    mentioned = [
        task_id
        for task_id in (tasks or {})
        if "-" in task_id
        and len(task_id) >= 8
        and re.search(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])", haystack)
    ]
    if mentioned:
        return (tasks or {})[max(mentioned, key=len)]
    key = _KEY_DATE_SUFFIX.sub("", str(entry.get("key") or ""))
    best_plan: str | None = None
    best_len = -1
    for plan_id in plans:
        core = plan_id.split("-", 1)[1] if "-" in plan_id else plan_id
        core = _ID_DATE_SUFFIX.sub("", core)
        full = _ID_DATE_SUFFIX.sub("", plan_id)
        for stem in {core, full}:
            if not stem:
                continue
            if (key == stem or key.startswith(stem + "-")) and len(stem) > best_len:
                best_plan, best_len = plan_id, len(stem)
    return best_plan


def _knowledge_verdict(
    entry: dict[str, Any],
    plan_id: str | None,
    plan_status: str | None,
    has_personal_notes: bool,
    age_days: int | None,
    keep: bool,
) -> tuple[str, str | None]:
    """Whether a knowledge entry can be retired, and why."""
    if keep:
        return "keep", "marked keep in notes"
    if entry["kind"] != "fact":
        return "keep", None
    if plan_id and plan_status in {"completed", "cancelled"}:
        old_enough = age_days is not None and age_days >= RETIRE_MIN_AGE_DAYS
        if not has_personal_notes and old_enough:
            return "retire_safe", f"progress log of {plan_status} plan {plan_id}"
        return "review", None
    if plan_id:
        return "keep", None
    if _KEY_DATE_SUFFIX.search(str(entry.get("key") or "")):
        return "review", None
    return "keep", None


def _plan_overview(
    plan: dict[str, Any],
    plan_state: PlanState,
    execution_plan: dict[str, Any],
    projects: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Everything Plans.md, Home.md, frontmatter, and `notes review` need about one plan."""
    counts = plan_task_counts(plan_state, execution_plan, now)
    blocked = [
        {"task": task["id"], "reason": plan_state.tasks[task["id"]].reason or ""}
        for task in execution_plan.get("tasks", [])
        if plan_state.tasks.get(task["id"]) and plan_state.tasks[task["id"]].status == "blocked"
    ]
    latest = int(plan["revision"])
    status = _status(plan_state)
    workspace = plan_workspace(execution_plan, projects)
    buckets = _task_buckets(execution_plan, plan_state, now)
    goal = _paragraphs(plan.get("goal"))
    changes = _revision_changes(execution_plan, plan) if execution_plan is not plan else None
    return {
        "goal": _lead(goal[0])[0] if goal else "",
        "next": [(task["id"], task["title"]) for task in buckets["claimed"] + buckets["next"]],
        "tasks_claimed": len(buckets["claimed"]),
        "open_by_project": dict(
            Counter(
                str(task["project"])
                for key in ("claimed", "blocked", "next", "waiting")
                for task in buckets[key]
                if task.get("project")
            )
        ),
        "changes": changes if changes and any(changes.values()) else None,
        "id": plan["id"],
        "title": plan["title"],
        "status": status,
        "health": plan_health(plan_state, execution_plan, now),
        "workspace": workspace,
        "note_path": plan_note_path(plan["id"], status, workspace),
        "projects": plan_project_ids(execution_plan),
        "latest_revision": latest,
        "first_event_at": plan_state.first_event_at,
        "last_event_at": plan_state.last_event_at,
        "completed_at": plan_state.completed_at,
        "cancelled_at": plan_state.cancelled_at,
        "idle_days": idle_days(plan_state.last_event_at, now),
        "tasks_total": counts["total"],
        "tasks_done": counts["done"],
        "tasks_open": counts["open"],
        "tasks_blocked": counts["blocked"],
        "blocked": blocked,
        "pending_approval": bool(
            plan_state.approved_revision and latest > plan_state.approved_revision
        ),
    }


def _load_settings(target: Path) -> tuple[dict[str, Any], list[str]]:
    """`notes.yaml`: docs folders to mirror and topic rules. Written once, then user-owned."""
    settings: dict[str, Any] = {"docs": [], "topics": {}}
    path = target / SETTINGS_FILE
    if not path.exists():
        _atomic_write(path, NOTES_SETTINGS)
        return settings, []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return settings, [f"{SETTINGS_FILE}: {exc}"]
    if not isinstance(data, dict):
        return settings, [f"{SETTINGS_FILE} must be a YAML mapping"]
    warnings: list[str] = []
    docs = data.get("docs") or []
    if isinstance(docs, list) and all(isinstance(item, str) for item in docs):
        settings["docs"] = docs
    else:
        warnings.append(f"{SETTINGS_FILE}: docs must be a list of folder paths")
    topics = data.get("topics") or {}
    if not isinstance(topics, dict):
        return settings, [*warnings, f"{SETTINGS_FILE}: topics must be a mapping"]
    for name, rule in topics.items():
        rule = rule or {}
        tag = _topic_tag(str(name))
        if not isinstance(rule, dict) or not tag:
            warnings.append(f"{SETTINGS_FILE}: topic {name!r} must map to projects/keywords")
            continue
        settings["topics"][tag] = {
            "projects": [str(item) for item in _as_list(rule.get("projects"))],
            "keywords": [str(item) for item in _as_list(rule.get("keywords"))],
        }
    return settings, warnings


@dataclass
class _Context:
    """What plan, knowledge, and index rendering share within one render pass."""

    prefix: str | None = None
    docs: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    topics: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def plan_docs(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Docs named after the plan (`<id>.md`, `<id>-*.md`) or mentioned by path in it."""
        plan_id = str(plan["id"])
        text = yaml.safe_dump(_definition(plan), allow_unicode=True)
        found = [
            doc
            for doc in self.docs
            if doc["stem"] == plan_id
            or doc["stem"].startswith(plan_id + "-")
            or doc["tail"] in text
        ]
        return sorted(found, key=lambda doc: (doc["stem"] != plan_id, doc["relative"]))

    def doc_plans(self) -> dict[str, list[dict[str, Any]]]:
        """Each doc's or page's relative path → the plans that link it."""
        used_by: dict[str, list[dict[str, Any]]] = {}
        for plan in self.plans.values():
            for doc in plan.get("docs") or []:
                used_by.setdefault(doc["relative"], []).append(plan)
        return used_by

    def plan_knowledge(self, plan_id: str) -> list[dict[str, Any]]:
        """Entries linked to the plan, plus any that mention its id."""
        related = [
            entry
            for entry in self.entries
            if entry.get("plan") == plan_id
            or plan_id in f"{entry.get('title') or ''}\n{entry.get('body') or ''}"
        ]
        return sorted(related, key=lambda entry: str(entry.get("created_at") or ""), reverse=True)


def _entry_line(entry: dict[str, Any], from_note: str, excerpt: bool = False) -> str:
    parts = [_link(entry["title"], from_note, entry["relative"]), entry["kind"]]
    parts.extend(entry.get("topics") or [])
    day = _date_only(entry.get("created_at"))
    if day is not None:
        parts.append(day.isoformat())
    line = "- " + " · ".join(parts)
    if excerpt and entry.get("body"):
        line += f" — {_short(_lead(entry['body'])[0], 110)}"
    return line


def _plan_status_cell(plan: dict[str, Any]) -> str:
    status = plan["health"] if plan["status"] == "active" else plan["status"]
    return status + (" · approval" if plan["pending_approval"] else "")


def _next_cell(plan: dict[str, Any]) -> str:
    upcoming = plan["next"]
    if not upcoming:
        return "—"
    task_id, title = upcoming[0]
    more = f" (+{len(upcoming) - 1})" if len(upcoming) > 1 else ""
    return f"`{task_id}` {_short(str(title), 60)}{more}"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _suffix(value: Any) -> str:
    return f" — {_short(str(value), 140)}" if value else ""


class NotesBridge:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    @property
    def profile(self) -> str:
        if self.hub.profile == "explicit":
            raise ValueError("Notes require a configured hub profile, not an explicit --repo path")
        return self.hub.profile or "default"

    def target(self) -> Path | None:
        entry = load_profiles().profiles.get(self.profile, {})
        raw = entry.get("notes_target")
        return Path(raw).expanduser() if isinstance(raw, str) and raw else None

    def connect(self, target: Path) -> dict[str, Any]:
        target = target.expanduser().resolve()
        if target.exists() and not target.is_dir():
            raise ValueError(f"Notes target is not a directory: {target}")
        if target.exists() and any(target.iterdir()) and not (target / SIDECAR).exists():
            raise ValueError("Notes target must be new, empty, or already managed by agops")
        # Validate a managed target before changing profile configuration.  This makes a failed
        # connect transactional and prevents one hub from adopting another hub's notes.
        if (target / SIDECAR).exists():
            self._sidecar(target)
        profiles = load_profiles()
        profile_path = config_path()
        previous = profile_path.read_bytes() if profile_path.exists() else None
        target.mkdir(parents=True, exist_ok=True)
        entry = dict(profiles.profiles.get(self.profile, {}))
        entry.update(
            {
                "repo": str(self.hub.root),
                "remote": remote_url(self.hub.root),
                "notes_target": str(target),
            }
        )
        profiles.profiles[self.profile] = entry
        if profiles.default is None:
            profiles.default = self.profile
        save_profiles(profiles)
        # Existing managed folders may contain an intentionally edited or malformed note;
        # connecting must not erase it. Missing files still receive the initial export.
        try:
            self.render_all()
        except Exception:
            if previous is None:
                profile_path.unlink(missing_ok=True)
            else:
                _atomic_write(profile_path, previous.decode("utf-8"))
            raise
        return {"connected": str(target), "profile": self.profile}

    def disconnect(self) -> dict[str, Any]:
        profiles = load_profiles()
        entry = profiles.profiles.get(self.profile)
        if not entry or not entry.get("notes_target"):
            raise ValueError("No notes target is connected")
        target = str(entry.pop("notes_target"))
        save_profiles(profiles)
        return {"disconnected": target, "profile": self.profile}

    def _sidecar(self, target: Path) -> dict[str, Any]:
        path = target / SIDECAR
        if not path.exists():
            return {
                "format_version": FORMAT_VERSION,
                "hub_fingerprint": hub_fingerprint(self.hub.root),
                "plans": {},
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid {SIDECAR}: {exc}") from exc
        if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"Unsupported {SIDECAR} format")
        expected = hub_fingerprint(self.hub.root)
        actual = data.get("hub_fingerprint")
        if actual == _legacy_root_fingerprint(self.hub.root):
            # Migration is persisted by the next sidecar write. It is only valid in this exact
            # clone, unlike a remote-aware fingerprint which travels safely between machines.
            data["hub_fingerprint"] = expected
        elif actual != expected:
            raise ValueError(f"{SIDECAR} belongs to a different hub")
        data.setdefault("plans", {})
        return data

    def _write_sidecar(self, target: Path, sidecar: dict[str, Any]) -> None:
        _atomic_write(target / SIDECAR, json.dumps(sidecar, indent=2, sort_keys=True) + "\n")

    def _frontmatter_and_personal(self, path: Path) -> tuple[dict[str, Any], str]:
        if not path.exists():
            return {}, ""
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        if not match:
            raise ValueError(f"Missing frontmatter in {path}")
        frontmatter: dict[str, Any] = {}
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid frontmatter in {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Frontmatter in {path} must be a YAML object")
        frontmatter = {
            key: value for key, value in loaded.items() if not str(key).startswith("agops_")
        }
        personal = _PERSONAL.search(text)
        if not personal:
            raise ValueError(f"Missing personal-notes markers in {path}")
        return frontmatter, personal.group(1).strip("\n")

    def _locate_plan(self, target: Path, sidecar: dict[str, Any], plan_id: str) -> Path | None:
        """Find a plan's existing note: sidecar path, legacy flat path, then a search."""
        root = target.resolve()
        recorded = (sidecar["plans"].get(plan_id) or {}).get("path")
        candidates: list[Path] = []
        if isinstance(recorded, str) and recorded:
            candidates.append(target / recorded)
        candidates.append(target / "plans" / f"{plan_id}.md")
        for candidate in candidates:
            if candidate.is_file() and candidate.resolve().is_relative_to(root):
                return candidate
        plans = target / "plans"
        if plans.is_dir():
            for candidate in sorted(plans.rglob(f"{plan_id}.md")):
                if candidate.is_file() and candidate.resolve().is_relative_to(root):
                    return candidate
        return None

    def _definition_path(self, target: Path, plan_id: str) -> Path:
        return target / DEFINITIONS_DIR / f"{plan_id}.yaml"

    def _read_definition(
        self, target: Path, plan_id: str, note: Path | None
    ) -> tuple[dict[str, Any], str] | None:
        """A plan's editable definition as (data, yaml text), or None if it has none yet.

        The source is `plans/.definitions/<id>.yaml`; a note that still has the old fenced block
        is the fallback, so a note from before that layout keeps any unsynced edit.
        """
        note = note if note is not None and note.exists() else None
        if note is not None:
            # Imports need an intact note envelope, not just an isolated definition.
            self._frontmatter_and_personal(note)
        source = self._definition_path(target, plan_id)
        if source.is_symlink() or not source.resolve().is_relative_to(target.resolve()):
            raise ValueError(f"Definition for {plan_id} must be a regular file inside the vault")
        if source.is_file():
            raw = source.read_text(encoding="utf-8")
        elif note is not None and (match := _DEFINITION.search(note.read_text(encoding="utf-8"))):
            source, raw = note, match.group(1)
        else:
            return None
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {source}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Definition in {source} must be a YAML object")
        validate_content(raw, source.name)
        validate_plan(data, self.hub.policy())
        if data.get("id") != plan_id:
            raise ValueError(f"Plan ID in {source} must match {plan_id}")
        return data, raw

    def _render_plan(
        self,
        plan: dict[str, Any],
        state: PlanState,
        path: Path,
        execution_plan: dict[str, Any],
        overview: dict[str, Any],
        context: _Context,
    ) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        latest = int(plan["revision"])
        status = _status(state)
        now = utc_now()
        last_activity = _date_only(state.last_event_at)
        progress = f"{overview['tasks_done']}/{overview['tasks_total']}"
        topics = overview.get("topics") or []
        owned = {"tags", "status", "health", "workspace", "progress", "last_activity", "topics"}
        front: dict[str, Any] = {
            "tags": _with_topic_tags(_as_list(custom.get("tags")), ["agops", "agops/plan"], topics),
            "status": status,
            "health": overview["health"],
            "workspace": overview["workspace"],
            "progress": progress,
        }
        if last_activity is not None:
            front["last_activity"] = last_activity
        if topics:
            front["topics"] = topics
        front.update({key: value for key, value in custom.items() if key not in owned})
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        note = overview["note_path"]

        summary = [overview["health"], overview["workspace"], f"{progress} done"]
        if last_activity is not None:
            summary.append(f"last {last_activity.isoformat()}")
        lines = ["---", managed, "---", "", f"# {plan['title']}", "", " · ".join(summary), ""]
        goal = str(plan.get("goal") or "").strip()
        if goal:
            paragraphs = _paragraphs(goal)
            lead, rest = _lead(paragraphs[0], 280) if paragraphs else (goal, "")
            lines.extend([f"**Goal:** {lead}", ""])
            if rest or len(paragraphs) > 1:
                lines.extend(["> [!quote]- Full goal", *_quoted(goal), ""])
        for label, kind in (("Docs", "doc"), ("Pages", "page")):
            links = [
                _link(doc["title"], note, doc["relative"])
                for doc in context.plan_docs(plan)
                if doc["kind"] == kind
            ]
            if links:
                lines.extend([f"**{label}:** " + " · ".join(links), ""])
        if overview["projects"]:
            lines.extend(["**Projects:** " + _code_items(overview["projects"]), ""])

        tasks = execution_plan["tasks"]
        buckets = _task_buckets(execution_plan, state, now)
        done_ids = {task["id"] for task in buckets["done"]}
        finished = status in {"completed", "cancelled"}

        lines.extend(["## Needs you", ""])
        needs: list[str] = []
        if not finished and (not state.approved_revision or latest > state.approved_revision):
            changes = _changes_text(overview.get("changes"))
            needs.append(
                f"- Revision {latest} is waiting for approval" + (f": {changes}" if changes else "")
            )
        for item in overview["blocked"]:
            needs.append(f"- Blocked: `{item['task']}` — {_short(item['reason'], 160)}")
        lines.extend(needs or ["_Nothing._"])

        lines.extend(["", f"## Tasks ({len(done_ids)}/{len(tasks)} done)"])
        if any(task.get("phase") for task in tasks):
            phases: dict[str, list[dict[str, Any]]] = {}
            for task in tasks:
                phases.setdefault(str(task.get("phase") or "Other").strip(), []).append(task)
            if "Other" in phases:
                phases["Other"] = phases.pop("Other")
            sections = []
            for name, group in phases.items():
                finished_here = sum(task["id"] in done_ids for task in group)
                sections.append((f"{name} ({finished_here}/{len(group)} done)", group))
        else:
            sections = [
                (f"{BUCKET_TITLES[key]} ({len(group)})", group)
                for key, group in buckets.items()
                if group
            ]
        approved = bool(state.approved_revision)
        for heading, group in sections:
            lines.extend(["", f"### {heading}"])
            for task in group:
                current = state.tasks.get(task["id"])
                lines.extend(["", *_task_callout(task, current, approved, done_ids, now)])

        criteria = plan.get("acceptance_criteria") or []
        if criteria:
            lines.extend(["", "## Acceptance", ""])
            for criterion in criteria:
                if isinstance(criterion, dict):
                    label = f"**{criterion['id']}** " if criterion.get("id") else ""
                    lines.append(f"- {label}{criterion.get('text', '')}".rstrip())
                else:
                    lines.append(f"- {criterion}")

        related = context.plan_knowledge(str(plan["id"]))
        if related:
            lines.extend(["", f"## Related knowledge ({len(related)})", ""])
            lines.extend(_entry_line(entry, note) for entry in related)

        lines.extend(_plan_details(plan))
        lines.extend(["", "## My notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        definition = f"{DEFINITIONS_DIR}/{plan['id']}.yaml"
        footer = (
            f"_Generated by agops. Edit the plan in `{definition}`, then run `agops notes sync`._"
            if not finished
            else f"_Generated by agops. This plan is finished; `{definition}` is history only._"
        )
        lines.extend([PERSONAL_END, "", "---", footer, ""])
        return "\n".join(lines)

    @staticmethod
    def _plan_meta(
        plan: dict[str, Any], state: PlanState, overview: dict[str, Any]
    ) -> dict[str, Any]:
        """What the notes no longer show in frontmatter, kept in the sidecar for bookkeeping."""

        def day(value: str | None) -> str | None:
            found = _date_only(value)
            return found.isoformat() if found else None

        return {
            "title": plan["title"],
            "latest_revision": int(plan["revision"]),
            "approved_revision": state.approved_revision,
            "pending_approval": overview["pending_approval"],
            "projects": overview["projects"],
            "created": day(state.first_event_at),
            "completed": day(state.completed_at),
            "created_by": plan.get("created_by"),
        }

    def _index(self, overviews: list[dict[str, Any]]) -> str:
        groups: dict[str, list[dict[str, Any]]] = {
            "active": [], "draft": [], "completed": [], "cancelled": []
        }
        for item in overviews:
            groups[item["status"]].append(item)

        def newest_first(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return sorted(plans, key=lambda plan: plan["last_event_at"] or "", reverse=True)

        def details(plan: dict[str, Any], next_label: str) -> list[str]:
            out = [f"  - {_short(plan['goal'], 200)}"] if plan["goal"] else []
            if plan["next"]:
                out.append(f"  - {next_label}: {_next_cell(plan)}")
            changes = _changes_text(plan.get("changes"))
            if changes:
                out.append(f"  - Pending revision {plan['latest_revision']} {changes}")
            return out

        def last(plan: dict[str, Any]) -> str:
            found = _date_only(plan["last_event_at"])
            return f" · last {found.isoformat()}" if found else ""

        lines = ["# Plans", "", "## Active", ""]
        if not groups["active"]:
            lines.append("_None._")
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for plan in groups["active"]:
            by_workspace.setdefault(plan["workspace"], []).append(plan)
        for workspace in sorted(by_workspace):
            lines.extend([f"### {workspace}", ""])
            for plan in newest_first(by_workspace[workspace]):
                pending = " · pending approval" if plan["pending_approval"] else ""
                idle = plan["idle_days"]
                idle_text = f" · {idle}d idle" if idle is not None else ""
                lines.append(
                    f"- [{plan['title']}]({plan['note_path']}) · {plan['health']} · "
                    f"{plan['tasks_done']}/{plan['tasks_total']} done"
                    f"{last(plan)}{idle_text}{pending}"
                )
                lines.extend(details(plan, "Next"))
            lines.append("")
        lines.extend(["", "## Draft", ""])
        if not groups["draft"]:
            lines.append("_None._")
        for plan in newest_first(groups["draft"]):
            lines.append(
                f"- [{plan['title']}]({plan['note_path']}) · draft · "
                f"{_count(plan['tasks_total'], 'task')}{last(plan)}"
            )
            lines.extend(details(plan, "Starts with"))
        lines.extend(["", "## Completed", ""])
        completed = sorted(
            groups["completed"], key=lambda plan: plan["completed_at"] or "", reverse=True
        )
        if not completed:
            lines.append("_None._")
        for plan in completed:
            date = _date_only(plan["completed_at"])
            suffix = f" · completed {date.isoformat()}" if date else ""
            lines.append(f"- [{plan['title']}]({plan['note_path']}){suffix}")
        lines.extend(["", "## Cancelled", ""])
        cancelled = sorted(
            groups["cancelled"], key=lambda plan: plan["cancelled_at"] or "", reverse=True
        )
        if not cancelled:
            lines.append("_None._")
        for plan in cancelled:
            date = _date_only(plan["cancelled_at"])
            suffix = f" · cancelled {date.isoformat()}" if date else ""
            lines.append(f"- [{plan['title']}]({plan['note_path']}){suffix}")
        return _tidy(lines)

    def _home(
        self,
        overviews: list[dict[str, Any]],
        now: datetime,
        context: _Context,
        events: list[dict[str, Any]],
    ) -> str:
        active = [item for item in overviews if item["status"] == "active"]
        drafts = [item for item in overviews if item["status"] == "draft"]
        live = sum(1 for item in active if item["health"] == "live")
        stalled = sum(1 for item in active if item["health"] == "stalled")
        blocked_tasks = sum(item["tasks_blocked"] for item in active)
        ready = len(self.hub.ready_tasks())
        lines = [
            "# agops home",
            "",
            f"_As of {now.strftime('%Y-%m-%d %H:%M')} UTC · {len(active)} active · {live} live · "
            f"{stalled} stalled · {len(drafts)} draft · {blocked_tasks} blocked tasks · "
            f"{ready} ready tasks_",
            "",
            "## Needs you",
            "",
        ]

        def link(plan: dict[str, Any]) -> str:
            return _link(plan["title"], "Home.md", plan["note_path"])

        needs: list[str] = []
        for plan in active:
            if plan["pending_approval"]:
                changes = _changes_text(plan.get("changes"))
                needs.append(
                    f"- Pending approval: {link(plan)} · revision {plan['latest_revision']}"
                    + (f" — {changes}" if changes else "")
                )
        for plan in active:
            for item in plan["blocked"]:
                needs.append(
                    f"- Blocked: {link(plan)} / `{item['task']}` — {_short(item['reason'], 160)}"
                )
        for plan in active:
            if plan["health"] == "stalled":
                needs.append(f"- Stalled: {link(plan)} · {plan['idle_days']}d idle")
        for plan in sorted(drafts, key=lambda item: item["last_event_at"] or "", reverse=True):
            needs.append(
                f"- Approve or cancel the draft: {link(plan)} · revision "
                f"{plan['latest_revision']} · {_count(plan['tasks_total'], 'task')}"
            )
        lines.extend(needs or ["_Nothing._"])

        lines.extend(["", "## Plans", ""])
        open_plans = active + drafts
        if not open_plans:
            lines.append("_None._")
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for plan in open_plans:
            by_workspace.setdefault(plan["workspace"], []).append(plan)

        def newest(workspace: str) -> str:
            return max(plan["last_event_at"] or "" for plan in by_workspace[workspace])

        for workspace in sorted(by_workspace, key=lambda name: (newest(name), name), reverse=True):
            lines.extend(
                [
                    f"### {workspace}",
                    "",
                    "| Plan | Status | Done | Next | Last activity |",
                    "|---|---|---|---|---|",
                ]
            )
            ordered = sorted(
                by_workspace[workspace], key=lambda item: item["last_event_at"] or "", reverse=True
            )
            ordered.sort(key=lambda item: item["status"] != "active")
            for plan in ordered:
                last = _date_only(plan["last_event_at"])
                idle = plan["idle_days"]
                last_text = last.isoformat() if last else ""
                if last and idle is not None:
                    last_text += f" ({idle}d)"
                cells = [
                    link(plan),
                    _plan_status_cell(plan),
                    f"{plan['tasks_done']}/{plan['tasks_total']}",
                    _next_cell(plan),
                    last_text,
                ]
                lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
            lines.append("")

        lines.extend(["", f"## Last {HOME_EVENT_DAYS} days", ""])
        cutoff = now - timedelta(days=HOME_EVENT_DAYS)
        recent = self._event_lines(
            [event for event in events if parse_time(event["occurred_at"]) >= cutoff],
            context,
            "Home.md",
        )
        lines.extend(recent[:HOME_EVENT_LIMIT] or ["_Nothing recorded._"])
        if len(recent) > HOME_EVENT_LIMIT:
            lines.append(f"- _{len(recent) - HOME_EVENT_LIMIT} more in [[Activity]]_")

        lines.extend(["", "## Recent pages", ""])
        pages = sorted(
            (doc for doc in context.docs if doc["kind"] == "page"),
            key=lambda doc: (doc["modified"], doc["relative"]),
            reverse=True,
        )[:HOME_PAGE_LIMIT]
        used_by = context.doc_plans()
        for page in pages:
            plans = "".join(f" · {link(plan)}" for plan in used_by.get(page["relative"], []))
            lines.append(
                f"- {_link(page['title'], 'Home.md', page['relative'])} · "
                f"{page['modified'].isoformat()}{plans}"
            )
        if not pages:
            lines.append("_None._")

        lines.extend(["", "## Recent knowledge", ""])
        newest_entries = sorted(
            context.entries, key=lambda entry: str(entry.get("created_at") or ""), reverse=True
        )[:HOME_KNOWLEDGE_LIMIT]
        lines.extend(_entry_line(entry, "Home.md") for entry in newest_entries)
        if not newest_entries:
            lines.append("_None._")

        lines.extend(["", "## Recently completed (14 days)", ""])
        cutoff = now - timedelta(days=14)
        completed = [
            item
            for item in overviews
            if item["status"] == "completed"
            and item["completed_at"]
            and parse_time(item["completed_at"]) >= cutoff
        ]
        completed.sort(key=lambda item: item["completed_at"], reverse=True)
        for plan in completed:
            date = _date_only(plan["completed_at"])
            lines.append(f"- {link(plan)} · {date.isoformat() if date else ''}".rstrip(" ·"))
        if not completed:
            lines.append("_None._")
        lines.extend(
            [
                "",
                "## Indexes",
                "",
                "[[Plans]] · [[Knowledge]] · [[Projects]] · [[Activity]] · [[Docs]] · "
                "[[agops.base|Views]]",
            ]
        )
        return _tidy(lines)

    def _event_lines(
        self, events: list[dict[str, Any]], context: _Context, from_note: str
    ) -> list[str]:
        """Log lines, newest first; one knowledge entry's same-day revisions make one line."""

        def group(event: dict[str, Any]) -> tuple[str, str, str]:
            payload = event.get("payload") or {}
            day = str(event.get("occurred_at") or "")[:10]
            return day, str(payload.get("scope") or ""), str(payload.get("key") or "")

        knowledge = [event for event in events if event.get("type") == "knowledge_added"]
        new = {
            group(event)
            for event in knowledge
            if not (event.get("payload") or {}).get("supersedes")
        }
        seen: set[tuple[str, str, str]] = set()
        lines: list[str] = []
        for event in events:
            if event.get("type") == "knowledge_added":
                if group(event) in seen:
                    continue
                seen.add(group(event))
            line = self._event_line(event, context, from_note, group(event) in new)
            if line:
                lines.append(line)
        return lines

    def _event_line(
        self, event: dict[str, Any], context: _Context, from_note: str, new: bool = False
    ) -> str | None:
        """One line of the activity log, or None for an event nobody needs to read."""
        kind = event.get("type")
        payload = event.get("payload") or {}
        day = _date_only(event.get("occurred_at"))
        prefix = f"- {day.isoformat() if day else '?'} · "
        if kind == "knowledge_added":
            key = str(payload.get("key") or "")
            if payload.get("status") == "retired":
                return f"{prefix}knowledge retired: `{key}`"
            relative = f"knowledge/{str(payload.get('scope') or '').replace(':', '/')}/{key}.md"
            title = str(payload.get("title") or key)
            current = any(entry["relative"] == relative for entry in context.entries)
            label = _link(title, from_note, relative) if current else title
            verb = "added" if new or not payload.get("supersedes") else "updated"
            return f"{prefix}knowledge {verb}: {label} · {payload.get('kind') or 'fact'}"
        plan_id = event.get("plan_id")
        if not plan_id:
            return None
        plan = context.plans.get(str(plan_id))
        plan_label = _link(plan["title"], from_note, plan["note_path"]) if plan else f"`{plan_id}`"
        task = f"`{event['task_id']}` " if event.get("task_id") else ""
        texts = {
            "plan_drafted": f"revision {payload.get('revision')} drafted"
            + (" from notes" if payload.get("imported") else ""),
            "plan_approved": f"revision {payload.get('revision')} approved",
            "plan_completed": "completed",
            "plan_cancelled": "cancelled" + _suffix(payload.get("reason")),
            "task_claimed": f"{task}claimed by {event.get('actor') or 'unknown'}",
            "task_checkpoint": f"{task}checkpoint" + _suffix(payload.get("summary")),
            "task_blocked": f"{task}blocked" + _suffix(payload.get("reason")),
            "task_unblocked": f"{task}unblocked",
            "task_released": f"{task}released",
            "task_completed": f"{task}completed" + _suffix(payload.get("summary")),
        }
        text = texts.get(str(kind))
        return f"{prefix}{plan_label} · {text}" if text else None

    def _events_since(self, since: datetime) -> list[dict[str, Any]]:
        """Hub events at or after `since`, newest first. Only the matching day folders are read."""
        root = self.hub.root / "memory" / "events"
        if not root.is_dir():
            return []
        first_day = (since - timedelta(days=1)).date()
        events: list[dict[str, Any]] = []
        for day_dir in sorted(root.glob("*/*/*")):
            try:
                day = date(*(int(part) for part in day_dir.relative_to(root).parts))
            except (TypeError, ValueError):
                continue
            if day < first_day or not day_dir.is_dir():
                continue
            for path in sorted(day_dir.glob("*.json")):
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                    occurred = parse_time(str(event["occurred_at"]))
                except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if isinstance(event, dict) and occurred >= since:
                    events.append(event)
        return sorted(events, key=lambda event: parse_time(event["occurred_at"]), reverse=True)

    def _ensure_base(self, target: Path) -> list[str]:
        """Write the Obsidian Bases file once; never overwrite a user's customized copy.

        An untouched earlier default is upgraded in place. A copy that still uses the old
        `agops_*` properties cannot match any note, so it is kept as `agops.old.base` (never
        overwritten) and replaced by the new default.
        """
        path = target / "agops.base"
        if not path.exists():
            _atomic_write(path, AGOPS_BASE)
            return []
        current = path.read_text(encoding="utf-8")
        if current == PREVIOUS_AGOPS_BASE:
            _atomic_write(path, AGOPS_BASE)
            return []
        if "agops_" not in current:
            return []
        if current != LEGACY_AGOPS_BASE:
            backup = target / "agops.old.base"
            if backup.exists():
                return [
                    "agops.base still uses the old agops_* properties and agops.old.base already "
                    "exists; update your views to tags/status/health/workspace/progress/"
                    "last_activity"
                ]
            os.replace(path, backup)
            _atomic_write(path, AGOPS_BASE)
            return ["agops.base used the old agops_* properties; kept it as agops.old.base"]
        _atomic_write(path, AGOPS_BASE)
        return []

    def _remove_legacy(self, target: Path, warnings: list[str]) -> list[str]:
        """Delete files an earlier layout generated and nothing refreshes any more.

        Only files that still carry that layout's markers go: `views/*.base` using the old
        `agops_*` properties, and `projects/*.md` notes with `agops_type: project`. A project
        note with personal notes is kept and reported.
        """
        removed: list[str] = []
        for relative in LEGACY_VIEWS:
            path = target / relative
            if (
                path.is_file()
                and not path.is_symlink()
                and "note.agops_" in path.read_text(encoding="utf-8")
            ):
                path.unlink()
                removed.append(relative)
        projects = target / LEGACY_PROJECTS_DIR
        if projects.is_dir() and not projects.is_symlink():
            for path in sorted(projects.glob("*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                metadata, _ = _split_frontmatter(text)
                if metadata.get("agops_type") != "project":
                    continue
                relative = path.relative_to(target).as_posix()
                personal = _PERSONAL.search(text)
                if personal and personal.group(1).strip():
                    warnings.append(f"{relative}: old project note kept for its personal notes")
                    continue
                path.unlink()
                removed.append(relative)
        for directory in (target / "views", projects):
            with contextlib.suppress(OSError):
                directory.rmdir()
        return removed

    def _mirror_docs(
        self, target: Path, sidecar: dict[str, Any], sources: list[str], warnings: list[str]
    ) -> list[dict[str, Any]]:
        """Copy each configured docs folder read-only into `docs/<folder>/`.

        Markdown and images only; dot files, symlinks, and files with credential patterns are
        skipped. A Markdown copy is named after its folder too (see `_doc_name`) so it never
        shares a plan's or a note's name, and links between the docs follow the new names. A
        copy edited in the vault is kept and no longer refreshed; a copy whose source is gone
        is removed unless it was edited.
        """
        known: dict[str, str] = sidecar.get("docs") or {}
        produced: dict[str, str] = {}
        docs: list[dict[str, Any]] = []
        root = target.resolve()
        for raw in sources:
            source = Path(raw).expanduser().resolve()
            if not source.is_dir():
                warnings.append(f"docs: {raw} is not a folder")
                continue
            if source.is_relative_to(root) or root.is_relative_to(source):
                warnings.append(f"docs: {raw} overlaps the notes folder")
                continue
            folder = _workspace_dir(source.name)
            files: list[Path] = []
            for directory, dirnames, filenames in os.walk(source):
                dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
                for filename in sorted(filenames):
                    file = Path(directory) / filename
                    suffix = file.suffix.lower()
                    if filename.startswith(".") or file.is_symlink() or suffix not in DOC_SUFFIXES:
                        continue
                    files.append(file)
            renamed: dict[str, str] = {}
            for file in files:
                if file.suffix.lower() == ".md":
                    inner = file.relative_to(source).as_posix()
                    renamed[inner] = _doc_name(inner, source.name)
            for file in files:
                suffix = file.suffix.lower()
                inner = file.relative_to(source).as_posix()
                relative = f"{DOCS_DIR}/{folder}/{renamed.get(inner, inner)}"
                data = file.read_bytes()
                if len(data) > MAX_DOC_BYTES:
                    warnings.append(f"{relative}: larger than {MAX_DOC_BYTES} bytes; skipped")
                    continue
                text = (
                    data.decode("utf-8", errors="replace")
                    if suffix in {".md", PAGE_SUFFIX}
                    else ""
                )
                if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                    warnings.append(f"{relative}: looks like it holds a credential; skipped")
                    continue
                if suffix == ".md":
                    data = _rewrite_doc_links(text, inner, renamed).encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                destination = target / relative
                if destination.is_symlink():
                    warnings.append(f"{relative}: is a symlink in the vault; skipped")
                    continue
                if destination.exists():
                    current = hashlib.sha256(destination.read_bytes()).hexdigest()
                    if current not in {digest, known.get(relative)}:
                        warnings.append(f"{relative}: edited in the vault; not refreshed")
                        if relative in known:
                            produced[relative] = known[relative]
                        continue
                    if current != digest:
                        destination.write_bytes(data)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                produced[relative] = digest
                if suffix in {".md", PAGE_SUFFIX}:
                    if suffix == PAGE_SUFFIX:
                        found = _PAGE_TITLE.search(text)
                        title = " ".join(html.unescape(found.group(1)).split()) if found else ""
                    else:
                        headings = (
                            line[2:].strip() for line in text.splitlines() if line.startswith("# ")
                        )
                        title = next(headings, "")
                    docs.append(
                        {
                            "relative": relative,
                            "kind": "page" if suffix == PAGE_SUFFIX else "doc",
                            "title": title or file.stem,
                            "stem": file.stem,
                            "tail": f"{file.parent.name}/{file.name}",
                            "group": posixpath.dirname(relative)[len(DOCS_DIR) + 1 :],
                            "modified": datetime.fromtimestamp(file.stat().st_mtime).date(),
                        }
                    )
        for relative in sorted(set(known) - set(produced)):
            path = target / relative
            if path.is_file() and not path.is_symlink():
                if hashlib.sha256(path.read_bytes()).hexdigest() != known[relative]:
                    warnings.append(f"{relative}: source removed; the edited copy is kept")
                    continue
                path.unlink()
        sidecar["docs"] = produced
        _prune_empty_dirs(target / DOCS_DIR)
        with contextlib.suppress(OSError):
            (target / DOCS_DIR).rmdir()
        return docs

    def _docs_index(self, context: _Context) -> str:
        lines = ["# Docs", ""]
        if not context.docs:
            lines.append(
                f"_No docs folders are configured. List them under `docs:` in `{SETTINGS_FILE}`; "
                "each sync then copies their Markdown and HTML pages here, read-only._"
            )
            return _tidy(lines)
        pages = sum(1 for doc in context.docs if doc["kind"] == "page")
        lines.append(
            f"_{_count(len(context.docs) - pages, 'document')} and {_count(pages, 'page')}, "
            f"copied read-only from the folders in `{SETTINGS_FILE}` on every sync. Edit the "
            "source; a copy edited here is no longer refreshed._"
        )
        used_by = context.doc_plans()
        groups: dict[str, list[dict[str, Any]]] = {}
        for doc in context.docs:
            groups.setdefault(doc["group"], []).append(doc)
        for group in sorted(groups):
            lines.extend(["", f"## {group}", ""])
            for doc in sorted(groups[group], key=lambda item: item["relative"]):
                plans = used_by.get(doc["relative"], [])
                suffix = ""
                if plans:
                    suffix = " · " + ", ".join(
                        _link(plan["title"], "Docs.md", plan["note_path"]) for plan in plans
                    )
                kind = " · page" if doc["kind"] == "page" else ""
                lines.append(
                    f"- {_link(doc['title'], 'Docs.md', doc['relative'])}{kind}{suffix}"
                )
        return _tidy(lines)


    # --- Read-only mirrors: knowledge, projects, and current agent activity -------------
    #
    # Unlike plan notes, these are never imported.  Agops owns everything but the
    # personal-notes region of a knowledge note, so a hub change always wins here.

    def _knowledge_entries(self) -> list[dict[str, Any]]:
        """The current revision of every active knowledge entry, newest scope order first."""
        root = self.hub.root / "memory" / "knowledge"
        if not root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for directory in sorted(path for path in root.rglob("*") if path.is_dir()):
            revisions = sorted(directory.glob("*.md"))
            if not revisions:
                continue
            path = revisions[-1]
            metadata, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            if metadata.get("status", "active") != "active":
                continue
            scope_path = directory.parent.relative_to(root).as_posix()
            key = str(metadata.get("key") or directory.name)
            entries.append(
                {
                    "id": metadata.get("id"),
                    "key": key,
                    "title": str(metadata.get("title") or key),
                    "scope": str(metadata.get("scope") or scope_path.replace("/", ":")),
                    "scope_path": scope_path,
                    "kind": str(metadata.get("kind", "fact")),
                    "created_at": metadata.get("created_at"),
                    "created_by": metadata.get("created_by"),
                    "revisions": len(revisions),
                    "body": body,
                    "relative": f"knowledge/{scope_path}/{key}.md",
                }
            )
        return sorted(entries, key=lambda entry: (entry["scope"], entry["key"]))

    def _render_knowledge(self, entry: dict[str, Any], path: Path, context: _Context) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        topics = entry.get("topics") or []
        owned = {"tags", "kind", "scope", "updated", "plan", "project", "topics"}
        front: dict[str, Any] = {
            "tags": _with_topic_tags(
                _as_list(custom.get("tags")), ["agops", "agops/knowledge"], topics
            ),
            "kind": entry["kind"],
            "scope": entry["scope"],
        }
        updated = _date_only(entry["created_at"])
        if updated is not None:
            front["updated"] = updated
        plan = context.plans.get(str(entry.get("plan") or ""))
        if entry.get("plan"):
            note_path = plan["note_path"] if plan else f"{entry['plan']}.md"
            front["plan"] = _wikilink(context.prefix, note_path, str(entry["plan"]))
        if entry["scope"].startswith("project:"):
            front["project"] = entry["scope"].split(":", 1)[1]
        if topics:
            front["topics"] = topics
        front.update({key: value for key, value in custom.items() if key not in owned})
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        body = entry["body"] or "_No body._"
        lines = ["---", managed, "---", "", f"# {entry['title']}", "", body]
        if plan:
            plan_link = _link(plan["title"], entry["relative"], plan["note_path"])
            lines.extend(["", f"_Plan: {plan_link}_"])
        lines.extend(["", "## My notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        lines.extend(
            [
                PERSONAL_END,
                "",
                "---",
                "_Mirrored from agops. Change it with `agops knowledge add`._",
                "",
            ]
        )
        return "\n".join(lines)

    def _knowledge_index(self, context: _Context) -> str:
        entries = context.entries
        if not entries:
            return "\n".join(["# Knowledge", "", "_None._", ""])
        kinds = Counter(entry["kind"] for entry in entries)
        tally = " · ".join(
            f"{count} {kind}{'s' if count != 1 else ''}"
            for kind, count in sorted(kinds.items(), key=lambda item: (-item[1], item[0]))
        )
        tips = ["`[kind:decision]`", "`[scope:global]`"]
        if context.topics:
            tips.insert(0, "`tag:#topic/<name>`")
        if context.docs:
            tips.append(f"`path:{DOCS_DIR}/` for the long-form documents")
        lines = [
            "# Knowledge",
            "",
            f"_{len(entries)} active entries · {tally}._",
            "",
            "Search: " + ", ".join(tips) + ".",
        ]
        sections: list[tuple[str, list[dict[str, Any]]]] = []
        for topic in context.topics:
            group = [entry for entry in entries if topic in (entry.get("topics") or [])]
            if group:
                sections.append((topic, group))
        by_scope: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if not entry.get("topics"):
                by_scope.setdefault(entry["scope"], []).append(entry)
        sections.extend(sorted(by_scope.items()))
        for heading, group in sections:
            lines.extend(["", f"## {heading} ({len(group)})", ""])
            ordered = sorted(
                group, key=lambda item: str(item.get("created_at") or ""), reverse=True
            )
            lines.extend(_entry_line(entry, "Knowledge.md", excerpt=True) for entry in ordered)
        return _tidy(lines)

    def _projects(self) -> list[dict[str, Any]]:
        root = self.hub.root / "memory" / "projects"
        if not root.exists():
            return []
        projects: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.yaml")):
            try:
                definition = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            if not isinstance(definition, dict):
                continue
            projects.append(
                {
                    "id": str(definition.get("id") or path.stem),
                    "remote": definition.get("remote"),
                    "workspace": definition.get("workspace"),
                    "registered_at": definition.get("registered_at"),
                }
            )
        return sorted(projects, key=lambda item: (str(item["workspace"] or ""), item["id"]))

    def _projects_index(self, projects: list[dict[str, Any]], context: _Context) -> str:
        if not projects:
            return "\n".join(["# Projects", "", "_None._", ""])
        plans_by_project: dict[str, list[dict[str, Any]]] = {}
        for plan in context.plans.values():
            if plan["status"] == "cancelled":
                continue
            for project_id in plan["projects"]:
                plans_by_project.setdefault(project_id, []).append(plan)
        knowledge_by_project: dict[str, list[dict[str, Any]]] = {}
        for entry in context.entries:
            if entry["scope"].startswith("project:"):
                project_id = entry["scope"].split(":", 1)[1]
                knowledge_by_project.setdefault(project_id, []).append(entry)
        involved = [
            item
            for item in projects
            if plans_by_project.get(item["id"]) or knowledge_by_project.get(item["id"])
        ]
        others = [item for item in projects if item not in involved]
        lines = [
            "# Projects",
            "",
            f"_{len(involved)} of {len(projects)} registered repositories have plans or "
            "knowledge._",
        ]
        workspaces: dict[str, list[dict[str, Any]]] = {}
        for project in involved:
            workspaces.setdefault(str(project["workspace"] or "unassigned"), []).append(project)
        for workspace, group in workspaces.items():
            lines.extend(["", f"## {workspace}"])
            for project in group:
                remote = str(project.get("remote") or "")
                name = remote.rstrip("/").rsplit("/", 1)[-1] if remote else project["id"]
                lines.extend(["", f"### {name}", ""])
                lines.extend([f"`{project['id']}`" + (f" · {remote}" if remote else ""), ""])
                for plan in plans_by_project.get(project["id"], []):
                    open_here = plan["open_by_project"].get(project["id"], 0)
                    lines.append(
                        f"- Plan: {_link(plan['title'], 'Projects.md', plan['note_path'])} · "
                        f"{_plan_status_cell(plan)} · {open_here} open tasks here"
                    )
                entries = sorted(
                    knowledge_by_project.get(project["id"], []),
                    key=lambda entry: str(entry.get("created_at") or ""),
                    reverse=True,
                )
                if entries:
                    lines.append(f"- Knowledge ({len(entries)})")
                    lines.extend("  " + _entry_line(entry, "Projects.md") for entry in entries)
        if others:
            lines.extend(
                [
                    "",
                    f"> [!note]- {len(others)} more registered "
                    f"{'repository' if len(others) == 1 else 'repositories'} without plans or "
                    "knowledge",
                ]
            )
            for project in others:
                remote = f" — {project['remote']}" if project.get("remote") else ""
                lines.append(f"> - `{project['id']}`{remote}")
        return _tidy(lines)

    def _activity(self, context: _Context, events: list[dict[str, Any]]) -> str:
        """What agents are doing now: live claims, blockers, what is next, and recent history."""
        state = load_state(self.hub.root)
        claimed: list[str] = []
        blocked: list[str] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan_state = state.plans.get(plan_id)
            if plan_state is None:
                continue
            for task_id, task in sorted(plan_state.tasks.items()):
                label = f"- `{plan_id}` / `{task_id}`"
                if task.status == "claimed":
                    live = "active" if task.actively_claimed() else "lease expired"
                    detail = [f"owner {task.owner or 'unknown'}", live]
                    if task.model:
                        detail.append(f"model {task.model}")
                    if task.tier:
                        detail.append(f"tier {task.tier}")
                    if task.lease_until:
                        detail.append(f"lease until {task.lease_until}")
                    claimed.append(f"{label} — " + " · ".join(detail))
                    if task.summary:
                        claimed.append(f"  - Latest: {task.summary}")
                    if task.evidence:
                        claimed.append("  - Evidence: " + "; ".join(task.evidence))
                elif task.status == "blocked":
                    blocked.append(f"{label} — {task.reason or 'no reason recorded'}")
        lines = ["# Activity", "", "## Claimed tasks", ""]
        lines.extend(claimed or ["_None._"])
        lines.extend(["", "## Blocked tasks", ""])
        lines.extend(blocked or ["_None._"])
        lines.extend(["", "## Ready next", ""])
        ready = self.hub.ready_tasks()
        if not ready:
            lines.append("_None._")
        for task in ready:
            lines.append(
                f"- `{task['plan_id']}` / `{task['id']}` — {task['title']}"
                + (f" · tier {task['tier']}" if task.get("tier") else "")
            )
        lines.extend(["", f"## Last {ACTIVITY_EVENT_DAYS} days", ""])
        history = self._event_lines(events, context, "Activity.md")
        lines.extend(history[:ACTIVITY_EVENT_LIMIT] or ["_Nothing recorded._"])
        return _tidy(lines)

    def _mirror(
        self,
        target: Path,
        sidecar: dict[str, Any],
        state: State,
        projects: list[dict[str, Any]],
        context: _Context,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, int], list[str]]:
        """Refresh the knowledge notes and the generated knowledge/project/activity indexes."""
        warnings: list[str] = []
        known: dict[str, Any] = sidecar.setdefault("knowledge", {})
        seen: set[str] = set()
        written = 0
        for entry in context.entries:
            relative = entry["relative"]
            seen.add(relative)
            path = target / relative
            plan_id = entry.get("plan")
            plan_status = _status(state.plans[plan_id]) if plan_id else None
            try:
                rendered = self._render_knowledge(entry, path, context)
            except ValueError as exc:
                warnings.append(f"knowledge {entry['scope']}/{entry['key']}: {exc}")
                continue
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                _atomic_write(path, rendered)
                written += 1
            known[relative] = {
                "id": entry["id"],
                "key": entry["key"],
                "scope": entry["scope"],
                "workspace": _knowledge_workspace(entry["scope"], projects),
                "revisions": entry["revisions"],
                "created_at": entry["created_at"],
                "created_by": entry["created_by"],
                "plan": plan_id,
                "plan_status": plan_status,
            }
        # A retired or superseded entry leaves the mirror. Personal notes are never discarded:
        # such a note is kept in place and reported instead.
        for relative in sorted(set(known) - seen):
            path = target / relative
            if path.exists():
                try:
                    _, personal = self._frontmatter_and_personal(path)
                except ValueError as exc:
                    warnings.append(f"{relative}: {exc}")
                    continue
                if personal:
                    warnings.append(f"{relative}: entry retired; note kept for its personal notes")
                    continue
                path.unlink()
            known.pop(relative, None)
        _atomic_write(target / "Knowledge.md", self._knowledge_index(context))
        _atomic_write(target / "Projects.md", self._projects_index(projects, context))
        _atomic_write(target / "Activity.md", self._activity(context, events))
        counts = {"knowledge": len(context.entries), "projects": len(projects), "written": written}
        return counts, warnings

    def status(self) -> dict[str, Any]:
        target = self.target()
        if target is None:
            return {"connected": False, "status": "unavailable"}
        if not target.is_dir():
            return {"connected": True, "target": str(target), "status": "unavailable"}
        try:
            sidecar = self._sidecar(target)
        except ValueError as exc:
            return {
                "connected": True,
                "target": str(target),
                "status": "invalid",
                "error": str(exc),
            }
        state = load_state(self.hub.root)
        output: list[dict[str, Any]] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            path = self._locate_plan(target, sidecar, plan_id)
            baseline = sidecar["plans"].get(plan_id)
            plan = load_plan(self.hub.root, plan_id)
            hub_hash = definition_hash(plan)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            try:
                found = self._read_definition(target, plan_id, path)
            except ValueError as exc:
                output.append({"id": plan_id, "status": "invalid", "error": str(exc)})
                continue
            if found is None:
                value = "missing"
            else:
                note_hash = definition_hash(found[0])
                base_hash = baseline.get("definition_hash") if baseline else None
                hub_changed = base_hash != hub_hash
                note_changed = base_hash != note_hash
                if hub_changed and note_changed and note_hash != hub_hash:
                    value = "conflict"
                elif note_changed and note_hash != hub_hash:
                    value = "edited"
                elif hub_changed:
                    value = "stale"
                else:
                    value = "clean"
            output.append({"id": plan_id, "status": value, "plan_status": _status(plan_state)})
        overall = "clean" if all(item["status"] == "clean" for item in output) else "attention"
        return {
            "connected": True,
            "target": str(target),
            "status": overall,
            "plans": output,
            "mirrored": {
                "knowledge": len(self._knowledge_entries()),
                "projects": len(self._projects()),
            },
        }

    def _safe_to_move(
        self,
        target: Path,
        plan_id: str,
        source: Path,
        destination: Path,
        baseline: dict[str, Any],
        hub_hash: str,
        force: bool,
    ) -> bool:
        """Move only a note whose definition has no unsynced edit (or one being force-resolved)."""
        if destination.exists():
            return False
        if force:
            return True
        try:
            found = self._read_definition(target, plan_id, source)
        except ValueError:
            return False
        if found is None:
            return True
        return definition_hash(found[0]) in {baseline.get("definition_hash"), hub_hash}

    def render_all(
        self, force: bool = False, plan_ids: set[str] | None = None
    ) -> dict[str, Any]:
        target = self.target()
        if target is None:
            return {"connected": False}
        if not target.is_dir():
            raise ValueError(f"Notes target is unavailable: {target}")
        sidecar = self._sidecar(target)
        state = load_state(self.hub.root)
        now = utc_now()
        projects = self._projects()
        written: list[str] = []
        settings, warnings = _load_settings(target)
        removed = self._remove_legacy(target, warnings)
        context = _Context(
            prefix=_vault_prefix(target),
            docs=self._mirror_docs(target, sidecar, settings["docs"], warnings),
            topics=settings["topics"],
        )
        loaded: list[tuple[dict[str, Any], PlanState, dict[str, Any], dict[str, Any]]] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan = load_plan(self.hub.root, plan_id)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            execution_plan = plan
            if plan_state.active and plan_state.approved_revision:
                execution_plan = load_plan(self.hub.root, plan_id, plan_state.approved_revision)
            overview = _plan_overview(plan, plan_state, execution_plan, projects, now)
            overview["topics"] = _topics(context.topics, overview["projects"], str(plan["title"]))
            overview["docs"] = context.plan_docs(plan)
            context.plans[plan_id] = overview
            loaded.append((plan, plan_state, execution_plan, overview))
        plan_list = list(state.plans)
        task_plans = {
            str(task["id"]): str(plan["id"]) for plan, *_ in loaded for task in plan["tasks"]
        }
        for entry in self._knowledge_entries():
            entry["plan"] = link_plan(entry, plan_list, task_plans)
            # A body mentions many neighbouring systems; only the entry's own project and its
            # title and key are specific enough to tag it.
            scope_kind, _, scope_id = entry["scope"].partition(":")
            entry["topics"] = _topics(
                context.topics,
                [scope_id] if scope_kind == "project" else [],
                f"{entry['key']}\n{entry['title']}",
            )
            context.entries.append(entry)
        overviews = list(context.plans.values())
        for plan, plan_state, execution_plan, overview in loaded:
            plan_id = str(plan["id"])
            desired = target / overview["note_path"]
            existing = self._locate_plan(target, sidecar, plan_id)
            path = existing or desired
            if existing is not None and existing != desired:
                overview["note_path"] = existing.relative_to(target).as_posix()
            if plan_ids is not None and plan_id not in plan_ids:
                continue
            hub_hash = definition_hash(plan)
            baseline = sidecar["plans"].get(plan_id, {})
            if existing is not None and existing != desired:
                if self._safe_to_move(
                    target, plan_id, existing, desired, baseline, hub_hash, force
                ):
                    desired.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(existing, desired)
                    path = desired
                    overview["note_path"] = desired.relative_to(target).as_posix()
                else:
                    warnings.append(
                        f"{plan_id}: left at {existing.relative_to(target).as_posix()} "
                        f"(not moved to {desired.relative_to(target).as_posix()})"
                    )
            # The note is generated output; the definition file is the editable part, so an
            # unsynced edit (or a definition still in an old note's fenced block) is kept.
            should_write = True
            edit_text: str | None = None
            if not force:
                try:
                    found = self._read_definition(target, plan_id, path)
                except ValueError as exc:
                    warnings.append(f"{plan_id}: {exc}")
                    should_write = False
                else:
                    if found is not None:
                        note_hash = definition_hash(found[0])
                        if baseline.get("definition_hash") != note_hash and note_hash != hub_hash:
                            edit_text = found[1]
            if should_write:
                rendered = self._render_plan(
                    plan, plan_state, path, execution_plan, overview, context
                )
                definition_path = self._definition_path(target, plan_id)
                if edit_text is None:
                    _write_if_changed(definition_path, _definition_yaml(plan))
                elif not definition_path.exists():
                    _atomic_write(definition_path, edit_text)
                _write_if_changed(path, rendered)
                written.append(plan_id)
                meta = self._plan_meta(plan, plan_state, overview)
                if edit_text is None:
                    sidecar["plans"][plan_id] = {
                        **meta,
                        "definition_hash": hub_hash,
                        "state_hash": _state_hash(plan_state),
                        "revision": int(plan["revision"]),
                    }
                elif plan_id in sidecar["plans"]:
                    # Keep the baseline the edit is measured against.
                    sidecar["plans"][plan_id].update(
                        {**meta, "state_hash": _state_hash(plan_state)}
                    )
            if path.exists() and plan_id in sidecar["plans"]:
                sidecar["plans"][plan_id]["path"] = path.relative_to(target).as_posix()
        _prune_empty_dirs(target / "plans")
        events = self._events_since(now - timedelta(days=ACTIVITY_EVENT_DAYS))
        _atomic_write(target / "Plans.md", self._index(overviews))
        _atomic_write(target / "Home.md", self._home(overviews, now, context, events))
        _atomic_write(target / "Docs.md", self._docs_index(context))
        warnings.extend(self._ensure_base(target))
        mirrored, mirror_warnings = self._mirror(target, sidecar, state, projects, context, events)
        mirrored["docs"] = len(context.docs)
        warnings.extend(mirror_warnings)
        self._write_sidecar(target, sidecar)
        return {
            "connected": True,
            "written": written,
            "warnings": warnings,
            "mirrored": mirrored,
            "removed": removed,
        }

    def sync(self, actor: str, session: str) -> dict[str, Any]:
        target = self.target()
        if target is None:
            raise ValueError("No notes target is connected")
        if not target.is_dir():
            raise ValueError(f"Notes target is unavailable: {target}")
        sidecar = self._sidecar(target)
        state = load_state(self.hub.root)
        imported: list[str] = []
        conflicts: list[str] = []
        invalid: list[dict[str, str]] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            if _status(plan_state) in {"completed", "cancelled"}:
                continue
            path = self._locate_plan(target, sidecar, plan_id)
            try:
                found = self._read_definition(target, plan_id, path)
            except ValueError as exc:
                invalid.append({"id": plan_id, "error": str(exc)})
                continue
            if found is None:
                continue
            baseline = sidecar["plans"].get(plan_id)
            if baseline is None:
                conflicts.append(plan_id)
                continue
            note = found[0]
            current = load_plan(self.hub.root, plan_id)
            note_hash, current_hash = definition_hash(note), definition_hash(current)
            base_hash = baseline["definition_hash"]
            hub_changed, note_changed = current_hash != base_hash, note_hash != base_hash
            if note_changed and hub_changed and note_hash != current_hash:
                conflicts.append(plan_id)
            elif note_changed and note_hash != current_hash:
                self.hub.draft_plan_definition(
                    note, actor, session, int(baseline["revision"]), base_hash
                )
                imported.append(plan_id)
        # Do not overwrite a concurrent edit: normal rendering refreshes hub-only changes,
        # imports, and missing notes while preserving invalid/edited/conflicting files.
        refreshed = self.render_all()
        return {"imported": imported, "conflicts": conflicts, "invalid": invalid, **refreshed}

    def resolve(self, plan_id: str, take: str, actor: str, session: str) -> dict[str, Any]:
        if take not in {"notes", "agops"}:
            raise ValueError("Resolution must be 'notes' or 'agops'")
        plan_id = slug(plan_id)
        target = self.target()
        if target is None:
            raise ValueError("No notes target is connected")
        path = self._locate_plan(target, self._sidecar(target), plan_id)
        found = self._read_definition(target, plan_id, path)
        if found is None:
            raise ValueError(f"Missing note for plan: {plan_id}")
        if take == "notes":
            note = found[0]
            current = load_plan(self.hub.root, plan_id)
            state = load_state(self.hub.root).plans.get(plan_id, PlanState(plan_id))
            if _status(state) in {"completed", "cancelled"}:
                raise ValueError(f"Cannot import notes for {_status(state)} plan: {plan_id}")
            if definition_hash(note) == definition_hash(current):
                return {"resolved": plan_id, "take": take, "imported": False}
            self.hub.draft_plan_definition(
                note, actor, session, int(current["revision"]), definition_hash(current)
            )
        # Resolution is deliberately scoped: unrelated edited/conflicting files are untouchable.
        self.render_all(force=True, plan_ids={plan_id})
        return {"resolved": plan_id, "take": take, "imported": take == "notes"}

    def _note_flags(self, target: Path | None, relative: str) -> tuple[bool, bool]:
        """(has_personal_notes, keep) for a mirrored knowledge note.

        `keep` is true only when the note's custom frontmatter has `keep: true` (YAML
        boolean) or the string "true"/"yes" (case-insensitive).
        """
        if target is None:
            return False, False
        path = target / relative
        if not path.exists():
            return False, False
        try:
            frontmatter, personal = self._frontmatter_and_personal(path)
        except ValueError:
            # An unreadable note may still hold personal notes: never make it retire-safe.
            return True, False
        keep_value = frontmatter.get("keep")
        keep = keep_value is True or (
            isinstance(keep_value, str) and keep_value.strip().lower() in {"true", "yes"}
        )
        return bool(personal), keep

    def review(self, now: datetime | None = None) -> dict[str, Any]:
        """A read-only triage report: which plans need attention, which knowledge can retire."""
        now = now or utc_now()
        state = load_state(self.hub.root)
        projects = self._projects()
        target = self.target()
        plans_out: list[dict[str, Any]] = []
        plan_counts = {"active": 0, "live": 0, "stalled": 0, "blocked": 0}
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan = load_plan(self.hub.root, plan_id)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            status = _status(plan_state)
            if status in {"completed", "cancelled"}:
                continue
            execution_plan = plan
            if plan_state.active and plan_state.approved_revision:
                execution_plan = load_plan(self.hub.root, plan_id, plan_state.approved_revision)
            overview = _plan_overview(plan, plan_state, execution_plan, projects, now)
            if status == "active":
                plan_counts["active"] += 1
                if overview["health"] in plan_counts:
                    plan_counts[overview["health"]] += 1
            last_activity_date = _date_only(overview["last_event_at"])
            plans_out.append(
                {
                    "id": plan_id,
                    "title": plan["title"],
                    "status": status,
                    "health": overview["health"],
                    "workspace": overview["workspace"],
                    "last_activity": last_activity_date.isoformat() if last_activity_date else None,
                    "idle_days": overview["idle_days"],
                    "tasks_open": overview["tasks_open"],
                    "tasks_blocked": overview["tasks_blocked"],
                    "blocked": overview["blocked"],
                    "pending_approval": overview["pending_approval"],
                }
            )
        plan_ids = list(state.plans)
        task_plans = {
            str(task["id"]): str(summary["id"])
            for summary in self.hub.list_plans()
            for task in load_plan(self.hub.root, summary["id"])["tasks"]
        }
        knowledge_out: list[dict[str, Any]] = []
        retire_safe = review_count = 0
        for entry in self._knowledge_entries():
            plan_id = link_plan(entry, plan_ids, task_plans)
            plan_status = _status(state.plans[plan_id]) if plan_id else None
            has_personal, keep = self._note_flags(target, entry["relative"])
            created_at = entry.get("created_at")
            age_days = (now - parse_time(created_at)).days if created_at else None
            verdict, reason = _knowledge_verdict(
                entry, plan_id, plan_status, has_personal, age_days, keep
            )
            if verdict == "retire_safe":
                retire_safe += 1
            elif verdict == "review":
                review_count += 1
            knowledge_out.append(
                {
                    "scope": entry["scope"],
                    "key": entry["key"],
                    "title": entry["title"],
                    "kind": entry["kind"],
                    "created_at": created_at,
                    "plan": plan_id,
                    "plan_status": plan_status,
                    "has_personal_notes": has_personal,
                    "kept": keep,
                    "verdict": verdict,
                    "reason": reason,
                }
            )
        return {
            "as_of": now.isoformat().replace("+00:00", "Z"),
            "stale_days": STALE_DAYS,
            "counts": {
                **plan_counts,
                "knowledge": len(knowledge_out),
                "retire_safe": retire_safe,
                "review": review_count,
            },
            "plans": plans_out,
            "knowledge": knowledge_out,
        }
