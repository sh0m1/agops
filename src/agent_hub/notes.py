"""A deliberately small, app-neutral Markdown mirror for plan definitions.

The files are ordinary Markdown.  Obsidian, Logseq, Foam, and a plain editor can all
use them; agops only owns the marked regions and the small sidecar baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .config import config_path, load_profiles, save_profiles
from .git import remote_url
from .ids import normalize_remote, slug
from .security import validate_content
from .state import PlanState, load_plan, load_state, validate_plan

if TYPE_CHECKING:
    from .hub import Hub

FORMAT_VERSION = 1
SIDECAR = ".agops-notes.json"
PERSONAL_START = "<!-- agops:personal:start -->"
PERSONAL_END = "<!-- agops:personal:end -->"
DEFINITION_START = "<!-- agops:definition:start -->"
DEFINITION_END = "<!-- agops:definition:end -->"
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_DEFINITION = re.compile(
    re.escape(DEFINITION_START) + r"\s*```ya?ml\n(.*?)```\s*" + re.escape(DEFINITION_END),
    re.DOTALL,
)
_PERSONAL = re.compile(
    re.escape(PERSONAL_START) + r"\n?(.*?)" + re.escape(PERSONAL_END), re.DOTALL
)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as raw:
        raw.write(content)
        name = raw.name
    os.replace(name, path)


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


def _task_row(plan_state: PlanState, task: dict[str, Any], done: set[str]) -> dict[str, Any]:
    """One task as the notes show it, with a status that is true for its plan's state.

    Only an active plan has claimable tasks, so a task in a draft plan is "planned" rather
    than "ready", and one whose dependencies are unfinished is "waiting".
    """
    current = plan_state.tasks.get(task["id"])
    if current and current.status == "completed":
        status = "completed"
    elif current and current.status == "blocked":
        status = "blocked"
    elif current and current.status == "claimed":
        status = "claimed" if current.actively_claimed() else "lease expired"
    elif not plan_state.active:
        status = "planned" if _status(plan_state) == "draft" else "not started"
    elif any(dependency not in done for dependency in task.get("depends_on") or []):
        status = "waiting"
    else:
        status = "ready"
    return {
        "id": task["id"],
        "title": str(task.get("title") or task["id"]),
        "project": task.get("project"),
        "tier": task.get("tier"),
        "depends_on": list(task.get("depends_on") or []),
        "status": status,
        "owner": current.owner if current and status != "completed" else None,
        "summary": current.summary if current else None,
        "evidence": list(current.evidence) if current else [],
        "reason": current.reason if current else None,
        "model": current.model if current else None,
        "tier_claimed": current.tier if current else None,
        "lease_until": current.lease_until if current else None,
    }


def _write_if_changed(path: Path, content: str) -> bool:
    """Write only real changes, so a synced vault does not churn on every hub event."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    _atomic_write(path, content)
    return True


def _remove_empty_dirs(root: Path) -> None:
    """Drop folders a prune or a move left empty; the root folder itself stays."""
    if not root.is_dir():
        return
    folders = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for folder in folders:
        entries = list(folder.iterdir())
        # Finder's .DS_Store alone does not make a folder worth keeping.
        if all(entry.name == ".DS_Store" and entry.is_file() for entry in entries):
            for entry in entries:
                entry.unlink()
            folder.rmdir()


# Earlier versions generated Activity.md; Home.md now carries the same sections.
_LEGACY_ACTIVITY = "# Activity\n\n_Back to [Home](Home.md)._\n"


def _remove_legacy_activity(target: Path) -> None:
    path = target / "Activity.md"
    if path.is_file() and path.read_text(encoding="utf-8").startswith(_LEGACY_ACTIVITY):
        path.unlink()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _frontmatter(
    custom: dict[str, Any],
    managed: dict[str, Any],
    tags: list[str],
    aliases: list[str] = (),  # type: ignore[assignment]
) -> str:
    """Custom keys, then agops keys, then the merged aliases and tags, always in that order.

    A fixed order keeps a re-render byte-identical.  `agops_aliases` records which aliases
    agops added, so a renamed plan or entry drops its old title instead of accumulating
    every title it ever had.
    """
    front = {key: value for key, value in custom.items() if key not in {"aliases", "tags"}}
    front.update(managed)
    aliases = [alias for alias in aliases if alias]
    if aliases:
        front["agops_aliases"] = aliases
    merged = list(dict.fromkeys([*_as_list(custom.get("aliases")), *aliases]))
    if merged:
        front["aliases"] = merged
    front["tags"] = list(dict.fromkeys([*_as_list(custom.get("tags")), *tags]))
    return yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()


def _link(source: str, target: str, label: str) -> str:
    """A relative Markdown link between two notes, both given relative to the notes root."""
    href = posixpath.relpath(target, posixpath.dirname(source) or ".")
    return f"[{_label(label)}]({href})"


def _label(text: str) -> str:
    return str(text).replace("[", "\\[").replace("]", "\\]")


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _day(entry: dict[str, Any]) -> str:
    return (entry.get("created_at") or "undated")[:10]


def _newest_first(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: entry.get("created_at") or "", reverse=True)


def _project_counts(project: dict[str, Any]) -> str:
    parts = []
    if project["tasks"]:
        count = len(project["tasks"])
        noun = "plan tasks" if count != 1 else "plan task"
        parts.append(f"{project['open_tasks']} open of {count} {noun}")
    if project["knowledge"]:
        count = len(project["knowledge"])
        parts.append(f"{count} knowledge entr{'ies' if count != 1 else 'y'}")
    return " · ".join(parts)


def _remote_url(remote: str) -> str:
    if "://" in remote or remote.startswith("git@"):
        return remote
    return f"https://{remote}" if "/" in remote else remote


RECENT_KNOWLEDGE = 10

# Obsidian Bases views, seeded into views/ once and left alone after the user customizes one.
# They select notes by tag rather than folder, so they work wherever the notes folder sits
# inside a vault.  Other notes apps ignore .base files.
VIEWS = {
    "Plans": """filters:
  and:
    - file.hasTag("agops/plan")
properties:
  note.agops_title:
    displayName: Plan
  note.agops_status:
    displayName: Status
  note.agops_tasks_done:
    displayName: Done
  note.agops_tasks_total:
    displayName: Tasks
  note.agops_pending_approval:
    displayName: Pending approval
  note.agops_projects:
    displayName: Projects
views:
  - type: table
    name: Open plans
    filters:
      and:
        - 'note.agops_status != "completed"'
        - 'note.agops_status != "cancelled"'
    groupBy:
      property: note.agops_status
      direction: ASC
    order:
      - file.name
      - note.agops_title
      - note.agops_tasks_done
      - note.agops_tasks_total
      - note.agops_pending_approval
      - note.agops_projects
  - type: table
    name: All plans
    groupBy:
      property: note.agops_status
      direction: ASC
    order:
      - file.name
      - note.agops_title
      - note.agops_tasks_done
      - note.agops_tasks_total
""",
    "Knowledge": """filters:
  and:
    - file.hasTag("agops/knowledge")
properties:
  note.agops_title:
    displayName: Entry
  note.agops_scope:
    displayName: Scope
  note.agops_kind:
    displayName: Kind
  note.agops_created_at:
    displayName: Recorded
  note.agops_created_by:
    displayName: By
views:
  - type: table
    name: Recent
    sort:
      - property: note.agops_created_at
        direction: DESC
    limit: 30
    order:
      - note.agops_title
      - note.agops_kind
      - note.agops_scope
      - note.agops_created_at
      - note.agops_created_by
  - type: table
    name: By scope
    groupBy:
      property: note.agops_scope
      direction: ASC
    sort:
      - property: note.agops_created_at
        direction: DESC
    order:
      - note.agops_title
      - note.agops_kind
      - note.agops_created_at
  - type: table
    name: Decisions and preferences
    filters:
      or:
        - 'note.agops_kind == "decision"'
        - 'note.agops_kind == "preference"'
    groupBy:
      property: note.agops_kind
      direction: ASC
    order:
      - note.agops_title
      - note.agops_scope
      - note.agops_created_at
""",
    "Projects": """filters:
  and:
    - file.hasTag("agops/project")
properties:
  note.agops_name:
    displayName: Repository
  note.agops_workspace:
    displayName: Workspace
  note.agops_open_tasks:
    displayName: Open tasks
  note.agops_tasks:
    displayName: Plan tasks
  note.agops_knowledge:
    displayName: Knowledge
  note.agops_plans:
    displayName: Plans
views:
  - type: table
    name: With tracked work
    filters:
      or:
        - "note.agops_tasks > 0"
        - "note.agops_knowledge > 0"
    sort:
      - property: note.agops_open_tasks
        direction: DESC
    order:
      - note.agops_name
      - note.agops_open_tasks
      - note.agops_tasks
      - note.agops_knowledge
      - note.agops_plans
  - type: table
    name: All repositories
    groupBy:
      property: note.agops_workspace
      direction: ASC
    order:
      - note.agops_name
      - file.name
      - note.agops_open_tasks
      - note.agops_knowledge
""",
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
        # Aliases agops added are regenerated; only the user's own aliases are custom.
        owned = {str(alias) for alias in _as_list(loaded.get("agops_aliases"))}
        if owned and "aliases" in frontmatter:
            kept = [a for a in _as_list(frontmatter["aliases"]) if str(a) not in owned]
            if kept:
                frontmatter["aliases"] = kept
            else:
                frontmatter.pop("aliases")
        personal = _PERSONAL.search(text)
        if not personal:
            raise ValueError(f"Missing personal-notes markers in {path}")
        return frontmatter, personal.group(1).strip("\n")

    def _note_definition(self, path: Path) -> dict[str, Any]:
        # Imports accept only the full hybrid-note envelope, not an isolated YAML fence.
        self._frontmatter_and_personal(path)
        text = path.read_text(encoding="utf-8")
        match = _DEFINITION.search(text)
        if not match:
            raise ValueError(f"Missing managed YAML definition in {path}")
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Definition in {path} must be a YAML object")
        validate_content(match.group(1), path.name)
        validate_plan(data, self.hub.policy())
        if data.get("id") != path.stem:
            raise ValueError(f"Plan ID in {path} must match its filename")
        return data

    # --- The overview model ---------------------------------------------------------------
    #
    # Every note is rendered from one snapshot of the hub, so plans, projects, and knowledge
    # can link to each other and the indexes agree with the notes they point at.

    def _overview(self) -> dict[str, Any]:
        state = load_state(self.hub.root)
        projects = self._projects()
        registered = {project["id"] for project in projects}
        knowledge = self._knowledge_entries()
        plans: list[dict[str, Any]] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            plan = load_plan(self.hub.root, plan_id)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            execution = plan
            if plan_state.active and plan_state.approved_revision:
                execution = load_plan(self.hub.root, plan_id, plan_state.approved_revision)
            done = {
                task_id for task_id, task in plan_state.tasks.items() if task.status == "completed"
            }
            tasks = [_task_row(plan_state, task, done) for task in execution["tasks"]]
            project_ids = list(dict.fromkeys(task["project"] for task in tasks if task["project"]))
            related = [
                entry
                for entry in knowledge
                if entry["project"] in project_ids
                or entry["key"] == plan_id
                or entry["key"].startswith(f"{plan_id}-")
            ]
            plans.append(
                {
                    "id": plan_id,
                    "title": str(plan["title"]),
                    "plan": plan,
                    "state": plan_state,
                    "execution": execution,
                    "status": _status(plan_state),
                    "pending": bool(
                        plan_state.approved_revision
                        and int(plan["revision"]) > plan_state.approved_revision
                    ),
                    "tasks": tasks,
                    "done": sum(1 for task in tasks if task["status"] == "completed"),
                    "projects": project_ids,
                    "knowledge": related,
                    "relative": f"plans/{plan_id}.md",
                }
            )
        for project in projects:
            project["tasks"] = [
                (plan, task)
                for plan in plans
                for task in plan["tasks"]
                if task["project"] == project["id"]
            ]
            project["open_tasks"] = sum(
                1
                for plan, task in project["tasks"]
                if plan["status"] in {"draft", "active"} and task["status"] != "completed"
            )
            project["knowledge"] = [
                entry for entry in knowledge if entry["project"] == project["id"]
            ]
            project["plans"] = list(dict.fromkeys(plan["id"] for plan, _ in project["tasks"]))
            project["tracked"] = bool(project["tasks"] or project["knowledge"])
        for entry in knowledge:
            entry["plans"] = [
                plan
                for plan in plans
                if any(related is entry for related in plan["knowledge"])
            ]
        self._name_knowledge(knowledge, {plan["id"] for plan in plans} | registered)
        return {
            "plans": plans,
            "projects": projects,
            "project_ids": registered,
            "project_names": {project["id"]: project["name"] for project in projects},
            "knowledge": knowledge,
            "ready": self.hub.ready_tasks(),
        }

    def _project_notes(self, target: Path, overview: dict[str, Any]) -> set[str]:
        """Projects that get a note: those with tracked work, and any whose note holds
        personal notes, which keeps that note refreshed instead of orphaned."""
        notes = {project["id"] for project in overview["projects"] if project["tracked"]}
        for project in overview["projects"]:
            path = target / project["relative"]
            if project["id"] in notes or not path.is_file():
                continue
            try:
                _, personal = self._frontmatter_and_personal(path)
            except ValueError:
                continue
            if personal:
                notes.add(project["id"])
        return notes

    def _project_link(self, overview: dict[str, Any], source: str, project_id: str | None) -> str:
        if not project_id:
            return ""
        if project_id not in overview["project_ids"]:
            return f"`{project_id}`"
        name = overview["project_names"][project_id]
        if project_id not in overview.get("project_notes", overview["project_ids"]):
            return f"`{name}`"
        return _link(source, f"projects/{project_id}.md", name)

    # --- Plan notes (two-way: only the definition fence is ever imported) -----------------

    def _render_plan(
        self, item: dict[str, Any], overview: dict[str, Any], path: Path
    ) -> str:
        plan, state = item["plan"], item["state"]
        source = item["relative"]
        custom, personal = self._frontmatter_and_personal(path)
        latest = int(plan["revision"])
        managed = _frontmatter(
            custom,
            {
                "agops_type": "plan",
                "agops_id": plan["id"],
                "agops_title": item["title"],
                "agops_latest_revision": latest,
                "agops_approved_revision": state.approved_revision,
                "agops_status": item["status"],
                "agops_pending_approval": item["pending"],
                "agops_tasks_total": len(item["tasks"]),
                "agops_tasks_done": item["done"],
                "agops_projects": item["projects"],
            },
            tags=["agops", "agops/plan"],
            aliases=[item["title"]],
        )
        definition = yaml.safe_dump(_definition(plan), sort_keys=False, allow_unicode=True).rstrip()
        lines = [
            "---", managed, "---", "", f"# {plan['title']}", "", DEFINITION_START,
            "```yaml", definition, "```", DEFINITION_END, "", "## Agops state", "",
            f"- Status: {item['status']}",
            f"- Progress: {item['done']} of {len(item['tasks'])} tasks completed",
            f"- Latest revision: {latest}",
            f"- Approved revision: {state.approved_revision or 'none'}",
        ]
        if item["projects"]:
            links = [self._project_link(overview, source, pid) for pid in item["projects"]]
            lines.append("- Projects: " + ", ".join(links))
        lines.append(f"- Overview: {_link(source, 'Home.md', 'Home')}")
        if plan.get("goal"):
            lines.extend(["", "## Goal", "", str(plan["goal"])])
        heading = "## Execution tasks"
        if item["pending"]:
            heading += f" (approved revision {state.approved_revision})"
        lines.extend(["", heading, ""])
        if item["pending"]:
            lines.append(
                f"_Revision {latest} is pending approval; its new tasks are not executable._"
            )
            lines.append("")
        if item["status"] == "draft":
            lines.append("_This plan is a draft: no task can be claimed until it is approved._")
            lines.append("")
        for task in item["tasks"]:
            owner = f" — {task['owner']}" if task["owner"] else ""
            lines.append(f"- `{task['id']}`: {task['status']}{owner} — {task['title']}")
            details = [self._project_link(overview, source, task["project"])]
            if task["tier"]:
                details.append(f"tier {task['tier']}")
            if task["depends_on"]:
                details.append("after " + ", ".join(f"`{dep}`" for dep in task["depends_on"]))
            if any(details):
                lines.append("  - " + " · ".join(detail for detail in details if detail))
            if task["summary"]:
                lines.append(f"  - Latest: {task['summary']}")
            if task["reason"] and task["status"] == "blocked":
                lines.append(f"  - Blocked: {task['reason']}")
            if task["evidence"]:
                lines.append("  - Evidence: " + "; ".join(task["evidence"]))
        if plan.get("acceptance_criteria"):
            lines.extend(["", "## Acceptance criteria", ""])
            for criterion in plan["acceptance_criteria"]:
                text = (
                    criterion.get("text", criterion) if isinstance(criterion, dict) else criterion
                )
                lines.append(f"- {text}")
        if item["knowledge"]:
            lines.extend(["", "## Related knowledge", ""])
            for entry in _newest_first(item["knowledge"]):
                lines.append(
                    f"- {_link(source, entry['relative'], entry['title'])} · {entry['kind']}"
                    f" · {_day(entry)}"
                )
        lines.extend(["", "## Personal notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        lines.extend([PERSONAL_END, ""])
        return "\n".join(lines)

    def _index(self, overview: dict[str, Any]) -> str:
        groups: dict[str, list[dict[str, Any]]] = {
            "active": [], "draft": [], "completed": [], "cancelled": []
        }
        for item in overview["plans"]:
            groups[item["status"]].append(item)
        lines = ["# Plans", "", f"_{len(overview['plans'])} plans. Back to [Home](Home.md)._", ""]
        for status in ("active", "draft", "completed", "cancelled"):
            lines.extend([f"## {status.title()}", ""])
            if not groups[status]:
                lines.append("_None._")
            for item in groups[status]:
                pending = " · pending approval" if item["pending"] else ""
                lines.append(
                    f"- [{_label(item['title'])}](plans/{item['id']}.md){pending}"
                    f" · {item['done']}/{len(item['tasks'])} tasks done"
                )
            lines.append("")
        return "\n".join(lines)

    # --- Read-only mirrors: knowledge, projects, and current agent activity -------------
    #
    # Unlike plan notes, these are never imported.  Agops owns everything but the
    # personal-notes region of a knowledge or project note, so a hub change always wins here.

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
            scope = str(metadata.get("scope") or scope_path.replace("/", ":"))
            kind, _, owner = scope.partition(":")
            entries.append(
                {
                    "id": metadata.get("id"),
                    "key": key,
                    "title": str(metadata.get("title") or key),
                    "scope": scope,
                    "scope_path": scope_path,
                    "project": owner if kind == "project" else None,
                    "workspace": owner if kind == "workspace" else None,
                    "kind": str(metadata.get("kind", "fact")),
                    "created_at": _text(metadata.get("created_at")),
                    "created_by": metadata.get("created_by"),
                    "revisions": len(revisions),
                    "body": body,
                    "relative": f"knowledge/{key}.md",
                    # Where earlier versions put the note; `_mirror` moves it from there.
                    "legacy_relative": f"knowledge/{scope_path}/{key}.md",
                }
            )
        return sorted(entries, key=lambda entry: (entry["scope"], entry["key"]))

    @staticmethod
    def _name_knowledge(knowledge: list[dict[str, Any]], reserved: set[str]) -> None:
        """Keep knowledge in one flat folder while every note's file name stays unique.

        Obsidian resolves `[[name]]` by file name, so a key shared by two scopes, or equal to
        a plan or project id, gets its scope appended.
        """
        uses: dict[str, int] = {}
        for entry in knowledge:
            uses[entry["key"]] = uses.get(entry["key"], 0) + 1
        for entry in knowledge:
            name = entry["key"]
            if uses[name] > 1 or name in reserved:
                name = f"{name}--{slug(entry['scope'])}"
            entry["relative"] = f"knowledge/{name}.md"

    def _render_knowledge(
        self, entry: dict[str, Any], overview: dict[str, Any], path: Path
    ) -> str:
        source = entry["relative"]
        custom, personal = self._frontmatter_and_personal(path)
        managed = _frontmatter(
            custom,
            {
                "agops_type": "knowledge",
                "agops_knowledge_id": entry["id"],
                "agops_key": entry["key"],
                "agops_title": entry["title"],
                "agops_scope": entry["scope"],
                "agops_kind": entry["kind"],
                "agops_revisions": entry["revisions"],
                "agops_created_at": entry["created_at"],
                "agops_created_by": entry["created_by"],
            },
            tags=["agops", "agops/knowledge"],
            aliases=[entry["title"]],
        )
        if entry["project"]:
            scope = "project " + self._project_link(overview, source, entry["project"])
        elif entry["workspace"]:
            scope = f"workspace `{entry['workspace']}`"
        else:
            scope = "global"
        lines = [
            "---", managed, "---", "", f"# {entry['title']}", "",
            f"_Mirrored from the agops hub ({entry['scope']} · {entry['kind']}). "
            "Edits outside the personal-notes region are overwritten; use "
            "`agops knowledge add` to change the entry._", "",
            entry["body"] or "_No body._",
            "", "## Context", "",
            f"- Scope: {scope}",
        ]
        if entry["plans"]:
            links = [_link(source, plan["relative"], plan["title"]) for plan in entry["plans"]]
            lines.append("- Related plans: " + ", ".join(links))
        recorded = f"- Recorded {_day(entry)}"
        if entry["created_by"]:
            recorded += f" by {entry['created_by']}"
        revisions = entry["revisions"]
        lines.append(f"{recorded} · {revisions} revision{'s' if revisions != 1 else ''}")
        lines.extend(["", "## Personal notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        lines.extend([PERSONAL_END, ""])
        return "\n".join(lines)

    def _knowledge_index(self, overview: dict[str, Any]) -> str:
        entries = overview["knowledge"]
        if not entries:
            return "\n".join(["# Knowledge", "", "_None._", ""])
        kinds: dict[str, int] = {}
        scopes: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
            scopes.setdefault(entry["scope"], []).append(entry)
        breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
        lines = [
            "# Knowledge", "",
            f"_{len(entries)} active entries ({breakdown}). Back to [Home](Home.md)._",
        ]
        for scope, group in scopes.items():
            lines.extend(["", f"## {scope}", ""])
            project = group[0]["project"]
            if project and project in overview["project_ids"]:
                lines.extend(
                    [f"Repository: {self._project_link(overview, 'Knowledge.md', project)}", ""]
                )
            for entry in _newest_first(group):
                lines.append(
                    f"- [{_label(entry['title'])}]({entry['relative']}) · {entry['kind']}"
                    f" · {_day(entry)}"
                )
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
            project_id = str(definition.get("id") or path.stem)
            remote = definition.get("remote")
            projects.append(
                {
                    "id": project_id,
                    "name": str(remote).rstrip("/").rsplit("/", 1)[-1] if remote else project_id,
                    "remote": remote,
                    "workspace": definition.get("workspace"),
                    "registered_at": definition.get("registered_at"),
                    "relative": f"projects/{project_id}.md",
                }
            )
        return sorted(projects, key=lambda item: (str(item["workspace"] or ""), item["id"]))

    def _render_project(
        self, project: dict[str, Any], overview: dict[str, Any], path: Path
    ) -> str:
        source = project["relative"]
        custom, personal = self._frontmatter_and_personal(path)
        managed = _frontmatter(
            custom,
            {
                "agops_type": "project",
                "agops_project": project["id"],
                "agops_name": project["name"],
                "agops_workspace": project["workspace"],
                "agops_remote": project["remote"],
                "agops_plans": project["plans"],
                "agops_tasks": len(project["tasks"]),
                "agops_open_tasks": project["open_tasks"],
                "agops_knowledge": len(project["knowledge"]),
            },
            tags=["agops", "agops/project"],
            aliases=[project["name"]] if project["name"] != project["id"] else [],
        )
        lines = [
            "---", managed, "---", "", f"# {project['name']}", "",
            "_Mirrored from the agops hub. Edits outside the personal-notes region are "
            "overwritten._", "",
            f"- Project id: `{project['id']}`",
        ]
        if project["remote"]:
            lines.append(f"- Repository: {_remote_url(str(project['remote']))}")
        lines.append(f"- Workspace: {project['workspace'] or 'unassigned'}")
        lines.extend(["", "## Plan tasks", ""])
        if not project["tasks"]:
            lines.append("_None._")
        for plan, task in project["tasks"]:
            lines.append(
                f"- {_link(source, plan['relative'], plan['title'])} · `{task['id']}`"
                f" — {task['status']} — {task['title']}"
            )
        lines.extend(["", "## Knowledge", ""])
        if not project["knowledge"]:
            lines.append("_None._")
        for entry in _newest_first(project["knowledge"]):
            lines.append(
                f"- {_link(source, entry['relative'], entry['title'])} · {entry['kind']}"
                f" · {_day(entry)}"
            )
        lines.extend(["", "## Personal notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        lines.extend([PERSONAL_END, ""])
        return "\n".join(lines)

    def _projects_index(self, overview: dict[str, Any]) -> str:
        projects = overview["projects"]
        if not projects:
            return "\n".join(["# Projects", "", "_None._", ""])
        tracked = sum(1 for project in projects if project["tracked"])
        workspaces: dict[str, list[dict[str, Any]]] = {}
        for project in projects:
            workspaces.setdefault(str(project["workspace"] or "unassigned"), []).append(project)
        lines = [
            "# Projects", "",
            f"_{len(projects)} registered repositories, {tracked} with tracked work. "
            "Back to [Home](Home.md)._",
        ]
        for workspace, group in workspaces.items():
            lines.extend(["", f"## {workspace}"])
            active = [project for project in group if project["tracked"]]
            quiet = [project for project in group if not project["tracked"]]
            notes = overview.get("project_notes", overview["project_ids"])
            if active:
                lines.extend(["", "### With tracked work", ""])
                for project in active:
                    lines.append(
                        f"- [{_label(project['name'])}]({project['relative']})"
                        f" `{project['id']}` — {_project_counts(project)}"
                    )
            if quiet:
                # Only repositories with something to show get a note of their own.
                lines.extend(["", "### Other repositories", ""])
                for project in quiet:
                    name = (
                        f"[{_label(project['name'])}]({project['relative']})"
                        if project["id"] in notes
                        else _label(project["name"])
                    )
                    lines.append(f"- {name} `{project['id']}`")
        lines.append("")
        return "\n".join(lines)

    def _now(self, overview: dict[str, Any], source: str) -> dict[str, list[str]]:
        """Claimed, blocked, and ready task lines, linked from the note at `source`."""
        claimed: list[str] = []
        blocked: list[str] = []
        for plan in overview["plans"]:
            for task in plan["tasks"]:
                label = f"- {_link(source, plan['relative'], plan['title'])} / `{task['id']}`"
                if task["status"] in {"claimed", "lease expired"}:
                    live = "active" if task["status"] == "claimed" else "lease expired"
                    detail = [f"owner {task['owner'] or 'unknown'}", live]
                    if task["model"]:
                        detail.append(f"model {task['model']}")
                    if task["tier_claimed"]:
                        detail.append(f"tier {task['tier_claimed']}")
                    if task["lease_until"]:
                        detail.append(f"lease until {task['lease_until']}")
                    claimed.append(f"{label} — {task['title']} — " + " · ".join(detail))
                    if task["summary"]:
                        claimed.append(f"  - Latest: {task['summary']}")
                    if task["evidence"]:
                        claimed.append("  - Evidence: " + "; ".join(task["evidence"]))
                elif task["status"] == "blocked":
                    blocked.append(
                        f"{label} — {task['title']} — {task['reason'] or 'no reason recorded'}"
                    )
        titles = {plan["id"]: plan for plan in overview["plans"]}
        ready: list[str] = []
        for task in overview["ready"]:
            plan = titles.get(task["plan_id"])
            where = (
                _link(source, plan["relative"], plan["title"]) if plan else f"`{task['plan_id']}`"
            )
            project = self._project_link(overview, source, task.get("project"))
            ready.append(
                f"- {where} / `{task['id']}` — {task['title']}"
                + (f" · {project}" if project else "")
                + (f" · tier {task['tier']}" if task.get("tier") else "")
            )
        return {"claimed": claimed, "blocked": blocked, "ready": ready}

    def _home(self, overview: dict[str, Any], attention: list[str]) -> str:
        """The entry point: one screen that says what needs a human and what is moving."""
        plans, projects = overview["plans"], overview["projects"]
        counts: dict[str, int] = {}
        for plan in plans:
            counts[plan["status"]] = counts.get(plan["status"], 0) + 1
        now = self._now(overview, "Home.md")
        tracked = [project for project in projects if project["tracked"]]
        plan_counts = ", ".join(
            f"{counts[status]} {status}"
            for status in ("active", "draft", "completed", "cancelled")
            if counts.get(status)
        )
        glance = [
            f"{len(plans)} plans" + (f" ({plan_counts})" if plan_counts else ""),
            f"{sum(1 for line in now['claimed'] if line.startswith('- '))} in progress",
            f"{len(now['blocked'])} blocked",
            f"{len(now['ready'])} ready",
            f"{len(overview['knowledge'])} knowledge entries",
            f"{len(projects)} repositories ({len(tracked)} with tracked work)",
        ]
        lines = ["# Overview", "", " · ".join(glance), "", "## Needs attention", ""]
        needs: list[str] = []
        for plan in plans:
            link = _link("Home.md", plan["relative"], plan["title"])
            if plan["status"] == "draft":
                needs.append(
                    f"- {link} is a draft; approve it before its {len(plan['tasks'])} tasks "
                    f"can be claimed (`agops plan approve {plan['id']}`)"
                )
            elif plan["pending"]:
                needs.append(
                    f"- {link} revision {plan['plan']['revision']} awaits approval"
                )
        needs.extend(now["blocked"])
        needs.extend(line for line in now["claimed"] if "lease expired" in line)
        needs.extend(f"- {line}" for line in attention)
        lines.extend(needs or ["_Nothing needs attention._"])
        lines.extend(["", "## In progress", ""])
        lines.extend(now["claimed"] or ["_Nothing is claimed._"])
        lines.extend(["", "## Ready next", ""])
        lines.extend(now["ready"] or ["_No task is ready._"])
        lines.extend(["", "## Plans", ""])
        open_plans = [plan for plan in plans if plan["status"] in {"active", "draft"}]
        if open_plans:
            lines.extend(["| Plan | Status | Done | Projects |", "| --- | --- | --- | --- |"])
            order = {"active": 0, "draft": 1}
            for plan in sorted(open_plans, key=lambda item: (order[item["status"]], item["id"])):
                status = plan["status"] + (" · pending approval" if plan["pending"] else "")
                links = [
                    self._project_link(overview, "Home.md", pid) for pid in plan["projects"]
                ]
                lines.append(
                    "| "
                    + " | ".join(
                        _cell(value)
                        for value in (
                            _link("Home.md", plan["relative"], plan["title"]),
                            status,
                            f"{plan['done']}/{len(plan['tasks'])}",
                            ", ".join(links) or "—",
                        )
                    )
                    + " |"
                )
        else:
            lines.append("_No open plans._")
        finished = len(plans) - len(open_plans)
        if finished:
            lines.extend(["", f"{finished} finished or cancelled: see [Plans](Plans.md)."])
        lines.extend(["", "## Recent knowledge", ""])
        recent = _newest_first(overview["knowledge"])[:RECENT_KNOWLEDGE]
        if not recent:
            lines.append("_None._")
        for entry in recent:
            where = (
                self._project_link(overview, "Home.md", entry["project"])
                if entry["project"]
                else entry["scope"]
            )
            lines.append(
                f"- {_day(entry)} · {_link('Home.md', entry['relative'], entry['title'])}"
                f" · {entry['kind']} · {where}"
            )
        if len(overview["knowledge"]) > len(recent):
            total = len(overview["knowledge"])
            lines.extend(["", f"All {total} entries: [Knowledge](Knowledge.md)."])
        lines.extend(["", "## Repositories with tracked work", ""])
        if not tracked:
            lines.append("_None._")
        workspaces: dict[str, list[dict[str, Any]]] = {}
        for project in tracked:
            workspaces.setdefault(str(project["workspace"] or "unassigned"), []).append(project)
        for workspace, group in workspaces.items():
            if len(workspaces) > 1:
                lines.extend([f"### {workspace}", ""])
            for project in sorted(group, key=lambda item: (-item["open_tasks"], item["name"])):
                lines.append(
                    f"- {_link('Home.md', project['relative'], project['name'])}"
                    f" — {_project_counts(project)}"
                )
            if len(workspaces) > 1:
                lines.append("")
        lines.extend(
            [
                "",
                "## Browse",
                "",
                "- Indexes: [Plans](Plans.md) · [Knowledge](Knowledge.md) · "
                "[Projects](Projects.md)",
                "- Obsidian Bases: "
                + " · ".join(f"[{name}](views/{name}.base)" for name in VIEWS),
                "",
            ]
        )
        return "\n".join(lines)

    def _write_views(self, target: Path, sidecar: dict[str, Any]) -> None:
        """Seed the Obsidian Bases views; never overwrite one the user has customized."""
        known: dict[str, str] = sidecar.setdefault("views", {})
        for name, content in VIEWS.items():
            relative = f"views/{name}.base"
            path = target / relative
            if path.exists():
                current = hashlib.sha256(path.read_bytes()).hexdigest()
                if current != known.get(relative):
                    continue
            _write_if_changed(path, content)
            known[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _prune(
        self, target: Path, known: dict[str, Any], seen: set[str], reason: str
    ) -> list[str]:
        """Remove notes whose hub record is gone, unless they hold personal notes."""
        warnings: list[str] = []
        for relative in sorted(set(known) - seen):
            path = target / relative
            if path.exists():
                try:
                    _, personal = self._frontmatter_and_personal(path)
                except ValueError as exc:
                    warnings.append(f"{relative}: {exc}")
                    continue
                if personal:
                    warnings.append(f"{relative}: {reason}; note kept for its personal notes")
                    continue
                path.unlink()
            known.pop(relative, None)
        return warnings

    @staticmethod
    def _move_knowledge_note(target: Path, known: dict[str, Any], entry: dict[str, Any]) -> None:
        """Carry an entry's note to its current path, so personal notes survive a rename.

        The old path is either where the sidecar last recorded this scope and key, or where
        earlier versions nested knowledge by scope.
        """
        path = target / entry["relative"]
        if path.exists():
            return
        candidates = [
            relative
            for relative, record in known.items()
            if relative != entry["relative"]
            and record.get("scope") == entry["scope"]
            and record.get("key") == entry["key"]
        ]
        if entry["legacy_relative"] != entry["relative"]:
            candidates.append(entry["legacy_relative"])
        for relative in candidates:
            old = target / relative
            if old.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, path)
                known.pop(relative, None)
                return

    def _mirror(
        self, target: Path, sidecar: dict[str, Any], overview: dict[str, Any]
    ) -> tuple[dict[str, int], list[str]]:
        """Refresh the knowledge and project notes and the generated indexes."""
        warnings: list[str] = []
        written = 0
        notes = overview["project_notes"]
        for kind, items, render, reason in (
            ("knowledge", overview["knowledge"], self._render_knowledge, "entry retired"),
            (
                "projects",
                [project for project in overview["projects"] if project["id"] in notes],
                self._render_project,
                "no tracked work or project unregistered",
            ),
        ):
            known: dict[str, Any] = sidecar.setdefault(kind, {})
            seen: set[str] = set()
            for item in items:
                relative = item["relative"]
                seen.add(relative)
                path = target / relative
                if kind == "knowledge":
                    self._move_knowledge_note(target, known, item)
                try:
                    rendered = render(item, overview, path)
                except ValueError as exc:
                    warnings.append(f"{relative}: {exc}")
                    continue
                if _write_if_changed(path, rendered):
                    written += 1
                known[relative] = (
                    {
                        "id": item["id"],
                        "revisions": item["revisions"],
                        "scope": item["scope"],
                        "key": item["key"],
                    }
                    if kind == "knowledge"
                    else {"id": item["id"]}
                )
            warnings.extend(self._prune(target, known, seen, reason))
            _remove_empty_dirs(target / kind)
        _write_if_changed(target / "Knowledge.md", self._knowledge_index(overview))
        _write_if_changed(target / "Projects.md", self._projects_index(overview))
        _remove_legacy_activity(target)
        self._write_views(target, sidecar)
        mirrored = {
            "knowledge": len(overview["knowledge"]),
            "projects": len(overview["projects"]),
            "written": written,
        }
        return mirrored, warnings

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
            path = target / "plans" / f"{plan_id}.md"
            baseline = sidecar["plans"].get(plan_id)
            plan = load_plan(self.hub.root, plan_id)
            hub_hash = definition_hash(plan)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            if not path.exists():
                value = "missing"
            else:
                try:
                    note_hash = definition_hash(self._note_definition(path))
                except ValueError as exc:
                    output.append({"id": plan_id, "status": "invalid", "error": str(exc)})
                    continue
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

    def render_all(
        self, force: bool = False, plan_ids: set[str] | None = None
    ) -> dict[str, Any]:
        target = self.target()
        if target is None:
            return {"connected": False}
        if not target.is_dir():
            raise ValueError(f"Notes target is unavailable: {target}")
        sidecar = self._sidecar(target)
        overview = self._overview()
        overview["project_notes"] = self._project_notes(target, overview)
        written: list[str] = []
        warnings: list[str] = []
        attention: list[str] = []
        for item in overview["plans"]:
            plan_id, plan = item["id"], item["plan"]
            if plan_ids is not None and plan_id not in plan_ids:
                continue
            path = target / item["relative"]
            hub_hash = definition_hash(plan)
            baseline = sidecar["plans"].get(plan_id, {})
            should_write = force or not path.exists()
            if path.exists() and not force:
                try:
                    note_hash = definition_hash(self._note_definition(path))
                    note_changed = baseline.get("definition_hash") != note_hash
                    if not note_changed or note_hash == hub_hash:
                        should_write = True
                    else:
                        attention.append(
                            f"`{item['relative']}` has definition edits that are not in the "
                            "hub yet: run `agops notes sync` (or resolve the conflict)"
                        )
                except ValueError as exc:
                    warnings.append(f"{plan_id}: {exc}")
                    should_write = False
            if should_write:
                _write_if_changed(path, self._render_plan(item, overview, path))
                written.append(plan_id)
                sidecar["plans"][plan_id] = {
                    "definition_hash": hub_hash,
                    "state_hash": _state_hash(item["state"]),
                    "revision": int(plan["revision"]),
                }
        _write_if_changed(target / "Plans.md", self._index(overview))
        mirrored, mirror_warnings = self._mirror(target, sidecar, overview)
        warnings.extend(mirror_warnings)
        notices = attention + [f"Notes not refreshed: {warning}" for warning in warnings]
        _write_if_changed(target / "Home.md", self._home(overview, notices))
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
            path = target / "plans" / f"{plan_id}.md"
            if not path.exists():
                continue
            baseline = sidecar["plans"].get(plan_id)
            if baseline is None:
                conflicts.append(plan_id)
                continue
            try:
                note = self._note_definition(path)
            except ValueError as exc:
                invalid.append({"id": plan_id, "error": str(exc)})
                continue
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
        path = target / "plans" / f"{plan_id}.md"
        if not path.exists():
            raise ValueError(f"Missing note for plan: {plan_id}")
        if take == "notes":
            note = self._note_definition(path)
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
