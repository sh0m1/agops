"""A deliberately small, app-neutral Markdown mirror for plan definitions.

The files are ordinary Markdown.  Obsidian, Logseq, Foam, and a plain editor can all
use them; agops only owns the marked regions and the small sidecar baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
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

    def _render_plan(
        self,
        plan: dict[str, Any],
        state: PlanState,
        path: Path,
        execution_plan: dict[str, Any] | None = None,
    ) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        latest = int(plan["revision"])
        raw_tags = custom.get("tags")
        custom_tags = (
            raw_tags if isinstance(raw_tags, list) else ([] if raw_tags is None else [raw_tags])
        )
        front = {
            **custom,
            "agops_id": plan["id"],
            "agops_latest_revision": latest,
            "agops_approved_revision": state.approved_revision,
            "agops_status": _status(state),
            "agops_pending_approval": bool(
                state.approved_revision and latest > state.approved_revision
            ),
            "tags": list(dict.fromkeys([*custom_tags, "agops"])),
        }
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        definition = yaml.safe_dump(_definition(plan), sort_keys=False, allow_unicode=True).rstrip()
        lines = [
            "---", managed, "---", "", f"# {plan['title']}", "", DEFINITION_START,
            "```yaml", definition, "```", DEFINITION_END, "", "## Agops state", "",
            f"- Status: {_status(state)}",
            f"- Latest revision: {latest}",
            f"- Approved revision: {state.approved_revision or 'none'}",
        ]
        if plan.get("goal"):
            lines.extend(["", "## Goal", "", str(plan["goal"])])
        execution_plan = execution_plan or plan
        pending = state.approved_revision and latest > state.approved_revision
        heading = "## Execution tasks"
        if pending:
            heading += f" (approved revision {state.approved_revision})"
        lines.extend(["", heading, ""])
        if pending:
            lines.append(
                f"_Revision {latest} is pending approval; its new tasks are not executable._"
            )
            lines.append("")
        for task in execution_plan["tasks"]:
            current = state.tasks.get(task["id"])
            state_text = current.status if current else "ready"
            owner = f" — {current.owner}" if current and current.owner else ""
            lines.append(f"- `{task['id']}`: {state_text}{owner} — {task['title']}")
            if current and current.summary:
                lines.append(f"  - Latest: {current.summary}")
            if current and current.evidence:
                lines.append("  - Evidence: " + "; ".join(current.evidence))
        if plan.get("acceptance_criteria"):
            lines.extend(["", "## Acceptance criteria", ""])
            for criterion in plan["acceptance_criteria"]:
                text = (
                    criterion.get("text", criterion) if isinstance(criterion, dict) else criterion
                )
                lines.append(f"- {text}")
        lines.extend(["", "## Personal notes", "", PERSONAL_START])
        if personal:
            lines.append(personal)
        lines.extend([PERSONAL_END, ""])
        return "\n".join(lines)

    def _index(self, summaries: list[dict[str, Any]]) -> str:
        groups = {"draft": [], "active": [], "completed": [], "cancelled": []}
        for item in summaries:
            groups[item["status"]].append(item)
        lines = ["# Plans", ""]
        for status in ("draft", "active", "completed", "cancelled"):
            lines.extend([f"## {status.title()}", ""])
            if not groups[status]:
                lines.append("_None._")
            for plan in groups[status]:
                pending = " · pending approval" if plan["pending_revision"] else ""
                lines.append(f"- [{plan['title']}](plans/{plan['id']}.md){pending}")
            lines.append("")
        return "\n".join(lines)

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

    def _render_knowledge(self, entry: dict[str, Any], path: Path) -> str:
        custom, personal = self._frontmatter_and_personal(path)
        raw_tags = custom.get("tags")
        custom_tags = (
            raw_tags if isinstance(raw_tags, list) else ([] if raw_tags is None else [raw_tags])
        )
        front = {
            **custom,
            "agops_knowledge_id": entry["id"],
            "agops_key": entry["key"],
            "agops_scope": entry["scope"],
            "agops_kind": entry["kind"],
            "agops_revisions": entry["revisions"],
            "agops_created_at": entry["created_at"],
            "agops_created_by": entry["created_by"],
            "tags": list(dict.fromkeys([*custom_tags, "agops", "agops/knowledge"])),
        }
        managed = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip()
        lines = [
            "---", managed, "---", "", f"# {entry['title']}", "",
            f"_Mirrored from the agops hub ({entry['scope']} · {entry['kind']}). "
            "Edits outside the personal-notes region are overwritten; use "
            "`agops knowledge add` to change the entry._", "",
            entry["body"] or "_No body._",
            "", "## Personal notes", "", PERSONAL_START,
        ]
        if personal:
            lines.append(personal)
        lines.extend([PERSONAL_END, ""])
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

    def _mirror(self, target: Path, sidecar: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
        """Refresh the knowledge notes and the generated project/activity indexes."""
        warnings: list[str] = []
        entries = self._knowledge_entries()
        known: dict[str, Any] = sidecar.setdefault("knowledge", {})
        seen: set[str] = set()
        written = 0
        for entry in entries:
            relative = entry["relative"]
            seen.add(relative)
            path = target / relative
            try:
                rendered = self._render_knowledge(entry, path)
            except ValueError as exc:
                warnings.append(f"knowledge {entry['scope']}/{entry['key']}: {exc}")
                continue
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                _atomic_write(path, rendered)
                written += 1
            known[relative] = {"id": entry["id"], "revisions": entry["revisions"]}
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
        projects = self._projects()
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
        state = load_state(self.hub.root)
        written: list[str] = []
        warnings: list[str] = []
        for summary in self.hub.list_plans():
            plan_id = summary["id"]
            if plan_ids is not None and plan_id not in plan_ids:
                continue
            plan = load_plan(self.hub.root, plan_id)
            plan_state = state.plans.get(plan_id, PlanState(plan_id))
            execution_plan = plan
            if plan_state.active and plan_state.approved_revision:
                execution_plan = load_plan(self.hub.root, plan_id, plan_state.approved_revision)
            path = target / "plans" / f"{plan_id}.md"
            hub_hash = definition_hash(plan)
            baseline = sidecar["plans"].get(plan_id, {})
            should_write = force or not path.exists()
            note: dict[str, Any] | None = None
            note_changed = False
            if path.exists() and not force:
                try:
                    note = self._note_definition(path)
                    note_hash = definition_hash(note)
                    note_changed = baseline.get("definition_hash") != note_hash
                    if not note_changed or note_hash == hub_hash:
                        should_write = True
                except ValueError as exc:
                    warnings.append(f"{plan_id}: {exc}")
                    should_write = False
            if should_write:
                _atomic_write(path, self._render_plan(plan, plan_state, path, execution_plan))
                written.append(plan_id)
                sidecar["plans"][plan_id] = {
                    "definition_hash": hub_hash,
                    "state_hash": _state_hash(plan_state),
                    "revision": int(plan["revision"]),
                }
        _atomic_write(target / "Plans.md", self._index(self.hub.list_plans()))
        mirrored, mirror_warnings = self._mirror(target, sidecar)
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
