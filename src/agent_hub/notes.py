"""A deliberately small, app-neutral Markdown mirror for plan definitions.

The files are ordinary Markdown.  Obsidian, Logseq, Foam, and a plain editor can all
use them; agops only owns the marked regions and the small sidecar baseline.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .config import config_path, load_profiles, save_profiles
from .git import remote_url
from .ids import normalize_remote, slug
from .security import validate_content
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


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value is None else [value])


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


def link_plan(entry: dict[str, Any], plans: list[str]) -> str | None:
    """The plan a knowledge entry belongs to: by id mention first, else by key.

    A key matches a plan by either of two stems: the plan id's *core* (id minus its first
    `-` segment, minus a trailing `-YYYYMMDD`), or the full id minus that trailing date —
    so a key that repeats the whole plan id still links. The longest matching stem wins.
    """
    haystack = f"{entry.get('title') or ''}\n{entry.get('body') or ''}"
    direct = [plan_id for plan_id in plans if plan_id in haystack]
    if direct:
        return max(direct, key=len)
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
    return {
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
    ) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        latest = int(plan["revision"])
        status = _status(state)
        now = utc_now()
        last_activity = _date_only(state.last_event_at)
        progress = f"{overview['tasks_done']}/{overview['tasks_total']}"
        owned = {"tags", "status", "health", "workspace", "progress", "last_activity"}
        front: dict[str, Any] = {
            "tags": list(dict.fromkeys([*_as_list(custom.get("tags")), "agops", "agops/plan"])),
            "status": status,
            "health": overview["health"],
            "workspace": overview["workspace"],
            "progress": progress,
        }
        if last_activity is not None:
            front["last_activity"] = last_activity
        front.update({key: value for key, value in custom.items() if key not in owned})
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()

        summary = [overview["health"], overview["workspace"], f"{progress} done"]
        if last_activity is not None:
            summary.append(f"last {last_activity.isoformat()}")
        lines = ["---", managed, "---", "", f"# {plan['title']}", "", " · ".join(summary), ""]
        goal = str(plan.get("goal") or "").strip()
        if goal:
            separator = "\n\n" if "\n" in goal else " "
            lines.extend([f"**Goal:**{separator}{goal}", ""])

        tasks = execution_plan["tasks"]
        done = [t for t in tasks if (state.tasks.get(t["id"]) or TaskState()).status == "completed"]
        done_ids = {task["id"] for task in done}
        open_tasks = [task for task in tasks if task["id"] not in done_ids]
        finished = status in {"completed", "cancelled"}

        lines.extend(["## Needs you", ""])
        needs: list[str] = []
        if not finished and (not state.approved_revision or latest > state.approved_revision):
            needs.append(f"- Revision {latest} is waiting for approval")
        for item in overview["blocked"]:
            needs.append(f"- Blocked: `{item['task']}` — {_short(item['reason'], 160)}")
        lines.extend(needs or ["_Nothing._"])

        lines.extend(["", f"## Open ({len(open_tasks)})", ""])
        for task in open_tasks:
            current = state.tasks.get(task["id"])
            waiting = [dep for dep in task.get("depends_on", []) if dep not in done_ids]
            if current and current.status == "blocked":
                detail = "blocked" + (f": {_short(current.reason, 100)}" if current.reason else "")
            elif current and current.actively_claimed(now):
                detail = f"claimed by {current.owner or 'unknown'}"
            elif not state.approved_revision:
                detail = "draft"
            elif waiting:
                detail = "waiting on " + ", ".join(f"`{dep}`" for dep in waiting)
            else:
                detail = "ready"
            lines.append(f"- [ ] `{task['id']}` {task['title']} — {detail}")
            if current and current.summary:
                lines.append(f"  - {_short(current.summary, 160)}")
        if not open_tasks:
            lines.append("_Nothing open._")

        lines.extend(["", f"## Done ({len(done)})", ""])
        if done:
            lines.append(f"> [!done]- {len(done)} tasks")
            lines.extend(f"> - `{task['id']}` {task['title']}" for task in done)
        else:
            lines.append("_None yet._")

        criteria = plan.get("acceptance_criteria") or []
        if criteria:
            lines.extend(["", "## Acceptance", ""])
            for criterion in criteria:
                if isinstance(criterion, dict):
                    label = f"**{criterion['id']}** " if criterion.get("id") else ""
                    lines.append(f"- {label}{criterion.get('text', '')}".rstrip())
                else:
                    lines.append(f"- {criterion}")

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
            "draft": [], "active": [], "completed": [], "cancelled": []
        }
        for item in overviews:
            groups[item["status"]].append(item)
        lines = ["# Plans", "", "## Draft", ""]
        if not groups["draft"]:
            lines.append("_None._")
        for plan in groups["draft"]:
            lines.append(f"- [{plan['title']}]({plan['note_path']})")
        lines.extend(["", "## Active", ""])
        if not groups["active"]:
            lines.append("_None._")
        else:
            by_workspace: dict[str, list[dict[str, Any]]] = {}
            for plan in groups["active"]:
                by_workspace.setdefault(plan["workspace"], []).append(plan)
            for workspace in sorted(by_workspace):
                lines.extend([f"### {workspace}", ""])
                ordered = sorted(
                    by_workspace[workspace],
                    key=lambda plan: plan["last_event_at"] or "",
                    reverse=True,
                )
                for plan in ordered:
                    pending = " · pending approval" if plan["pending_approval"] else ""
                    last = _date_only(plan["last_event_at"])
                    last_text = f" · last {last.isoformat()}" if last else ""
                    idle = plan["idle_days"]
                    idle_text = f" · {idle}d idle" if idle is not None else ""
                    lines.append(
                        f"- [{plan['title']}]({plan['note_path']}) · {plan['health']}"
                        f"{last_text}{idle_text}{pending}"
                    )
                lines.append("")
        lines.extend(["## Completed", ""])
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
        lines.append("")
        return "\n".join(lines)

    def _home(self, overviews: list[dict[str, Any]], now: datetime) -> str:
        active = [item for item in overviews if item["status"] == "active"]
        live = sum(1 for item in active if item["health"] == "live")
        stalled = sum(1 for item in active if item["health"] == "stalled")
        blocked_tasks = sum(item["tasks_blocked"] for item in active)
        ready = len(self.hub.ready_tasks())
        lines = [
            "# agops home",
            "",
            f"_As of {now.strftime('%Y-%m-%d %H:%M')} UTC · {len(active)} active · {live} live · "
            f"{stalled} stalled · {blocked_tasks} blocked tasks · {ready} ready tasks_",
            "",
            "## Needs you",
            "",
        ]
        needs: list[str] = []
        for plan in active:
            if plan["pending_approval"]:
                needs.append(
                    f"- Pending approval: [{plan['title']}]({plan['note_path']}) · "
                    f"revision {plan['latest_revision']}"
                )
        for plan in active:
            for item in plan["blocked"]:
                reason = item["reason"]
                if len(reason) > 160:
                    reason = reason[:159].rstrip() + "…"
                needs.append(
                    f"- Blocked: [{plan['title']}]({plan['note_path']}) / "
                    f"`{item['task']}` — {reason}"
                )
        lines.extend(needs or ["_Nothing._"])
        lines.extend(["", "## Active plans", ""])
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for plan in active:
            by_workspace.setdefault(plan["workspace"], []).append(plan)
        if not by_workspace:
            lines.extend(["_None._"])

        def newest(workspace: str) -> str:
            return max(plan["last_event_at"] or "" for plan in by_workspace[workspace])

        for workspace in sorted(by_workspace, key=lambda name: (newest(name), name), reverse=True):
            lines.extend(
                [
                    f"### {workspace}",
                    "",
                    "| Plan | Health | Last activity | Idle | Open | Blocked |",
                    "|---|---|---|---|---|---|",
                ]
            )
            ordered = sorted(
                by_workspace[workspace], key=lambda item: item["last_event_at"] or "", reverse=True
            )
            for plan in ordered:
                title = plan["title"].replace("|", "\\|")
                last = _date_only(plan["last_event_at"])
                idle = plan["idle_days"]
                lines.append(
                    f"| [{title}]({plan['note_path']}) | {plan['health']} | "
                    f"{last.isoformat() if last else ''} | "
                    f"{f'{idle}d' if idle is not None else ''} | "
                    f"{plan['tasks_open']} | {plan['tasks_blocked']} |"
                )
            lines.append("")
        lines.extend(
            [
                "",
                "## Views",
                "",
                "![[agops.base#Active plans]]",
                "",
                "## Recently completed (14 days)",
                "",
            ]
        )
        cutoff = now - timedelta(days=14)
        recent = [
            item
            for item in overviews
            if item["status"] == "completed"
            and item["completed_at"]
            and parse_time(item["completed_at"]) >= cutoff
        ]
        recent.sort(key=lambda item: item["completed_at"], reverse=True)
        if recent:
            for plan in recent:
                date = _date_only(plan["completed_at"])
                lines.append(f"- [{plan['title']}]({plan['note_path']}) · {date.isoformat()}")
        else:
            lines.append("_None._")
        lines.extend(
            ["", "## Indexes", "", "[[Plans]] · [[Knowledge]] · [[Activity]] · [[Projects]]", ""]
        )
        return "\n".join(lines)

    def _ensure_base(self, target: Path) -> list[str]:
        """Write the Obsidian Bases file once; never overwrite a user's customized copy.

        An untouched copy of the previous default is upgraded; a customized one that still uses
        the old `agops_*` properties is left alone and reported.
        """
        path = target / "agops.base"
        if not path.exists():
            _atomic_write(path, AGOPS_BASE)
            return []
        current = path.read_text(encoding="utf-8")
        if current == LEGACY_AGOPS_BASE:
            _atomic_write(path, AGOPS_BASE)
        elif "agops_" in current:
            return [
                "agops.base still filters on the old agops_* properties, which notes no longer "
                "carry; update your views to tags/status/health/workspace/progress/last_activity "
                "(or delete the file to get the new default)"
            ]
        return []

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

    def _render_knowledge(
        self, entry: dict[str, Any], path: Path, plan_id: str | None
    ) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        owned = {"tags", "kind", "scope", "updated", "plan"}
        front: dict[str, Any] = {
            "tags": list(
                dict.fromkeys([*_as_list(custom.get("tags")), "agops", "agops/knowledge"])
            ),
            "kind": entry["kind"],
            "scope": entry["scope"],
        }
        updated = _date_only(entry["created_at"])
        if updated is not None:
            front["updated"] = updated
        if plan_id:
            front["plan"] = f"[[{plan_id}]]"
        front.update({key: value for key, value in custom.items() if key not in owned})
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        body = entry["body"] or "_No body._"
        lines = ["---", managed, "---", "", f"# {entry['title']}", "", body]
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

    def _knowledge_index(self, entries: list[dict[str, Any]]) -> str:
        if not entries:
            return "\n".join(["# Knowledge", "", "_None._", ""])
        scopes: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            scopes.setdefault(entry["scope"], []).append(entry)
        lines = ["# Knowledge", "", f"_{len(entries)} active entries._"]
        for scope, group in scopes.items():
            lines.extend(["", f"## {scope}", ""])
            for entry in group:
                lines.append(f"- [{entry['title']}]({entry['relative']}) · {entry['kind']}")
        lines.append("")
        return "\n".join(lines)

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

    def _projects_index(self, projects: list[dict[str, Any]]) -> str:
        if not projects:
            return "\n".join(["# Projects", "", "_None._", ""])
        workspaces: dict[str, list[dict[str, Any]]] = {}
        for project in projects:
            workspaces.setdefault(str(project["workspace"] or "unassigned"), []).append(project)
        lines = ["# Projects", "", f"_{len(projects)} registered repositories._"]
        for workspace, group in workspaces.items():
            lines.extend(["", f"## {workspace}", ""])
            for project in group:
                remote = f" — {project['remote']}" if project.get("remote") else ""
                lines.append(f"- `{project['id']}`{remote}")
        lines.append("")
        return "\n".join(lines)

    def _activity(self) -> str:
        """What the agents are doing right now: live claims, blockers, and what is next."""
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
        lines.append("")
        return "\n".join(lines)

    def _mirror(
        self, target: Path, sidecar: dict[str, Any], state: State
    ) -> tuple[dict[str, int], list[str]]:
        """Refresh the knowledge notes and the generated project/activity indexes."""
        warnings: list[str] = []
        entries = self._knowledge_entries()
        projects = self._projects()
        plan_ids = list(state.plans)
        known: dict[str, Any] = sidecar.setdefault("knowledge", {})
        seen: set[str] = set()
        written = 0
        for entry in entries:
            relative = entry["relative"]
            seen.add(relative)
            path = target / relative
            plan_id = link_plan(entry, plan_ids)
            plan_status = _status(state.plans[plan_id]) if plan_id else None
            try:
                rendered = self._render_knowledge(entry, path, plan_id)
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
        _atomic_write(target / "Knowledge.md", self._knowledge_index(entries))
        _atomic_write(target / "Projects.md", self._projects_index(projects))
        _atomic_write(target / "Activity.md", self._activity())
        return {"knowledge": len(entries), "projects": len(projects), "written": written}, warnings

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
        warnings: list[str] = []
        overviews: list[dict[str, Any]] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan = load_plan(self.hub.root, plan_id)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            execution_plan = plan
            if plan_state.active and plan_state.approved_revision:
                execution_plan = load_plan(self.hub.root, plan_id, plan_state.approved_revision)
            overview = _plan_overview(plan, plan_state, execution_plan, projects, now)
            overviews.append(overview)
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
                rendered = self._render_plan(plan, plan_state, path, execution_plan, overview)
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
        _atomic_write(target / "Plans.md", self._index(overviews))
        _atomic_write(target / "Home.md", self._home(overviews, now))
        warnings.extend(self._ensure_base(target))
        mirrored, mirror_warnings = self._mirror(target, sidecar, state)
        warnings.extend(mirror_warnings)
        self._write_sidecar(target, sidecar)
        return {
            "connected": True,
            "written": written,
            "warnings": warnings,
            "mirrored": mirrored,
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
        knowledge_out: list[dict[str, Any]] = []
        retire_safe = review_count = 0
        for entry in self._knowledge_entries():
            plan_id = link_plan(entry, plan_ids)
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
