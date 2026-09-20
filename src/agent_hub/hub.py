from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import resolve_repo
from .git import (
    GitError,
    assert_clean,
    commit_and_push,
    recover_after_rejected_push,
    remote_url,
    repository_lock,
    sync_from_remote,
)
from .ids import event_id, normalize_remote, project_id_from_remote, slug
from .notes import NotesBridge, definition_hash
from .policy import (
    DEFAULT_POLICY_TEXT,
    POLICY_RELATIVE_PATH,
    UNKNOWN_TIER,
    TierPolicy,
    load_policy,
    parse_policy,
    policy_path,
)
from .security import validate_content
from .sessions import record_session, resolve_model, state_root
from .state import PlanState, State, load_plan, load_state, validate_plan

Mutation = Callable[[State], tuple[dict[str, Any], str]]


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def task_definition(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in plan["tasks"]:
        if task["id"] == task_id:
            return task
    raise ValueError(f"Unknown task: {task_id}")


def scopes_overlap(left: list[str], right: list[str]) -> bool:
    a = left or ["**"]
    b = right or ["**"]
    for first in a:
        for second in b:
            if "**" in {first, second}:
                return True
            first_base = first.split("*", 1)[0].rstrip("/")
            second_base = second.split("*", 1)[0].rstrip("/")
            if (
                first == second
                or first_base == second_base
                or first_base.startswith(second_base + "/")
                or second_base.startswith(first_base + "/")
            ):
                return True
    return False


class Hub:
    def __init__(self, root: Path, profile: str | None = None) -> None:
        self.root = root.resolve()
        self.profile = profile
        self.overridden_profile: str | None = None

    @classmethod
    def from_environment(cls, root: str | None = None) -> Hub:
        resolution = resolve_repo(root)
        hub = cls(resolution.root, profile=resolution.profile)
        hub.overridden_profile = resolution.overridden_profile
        return hub

    def _write_event(self, event: dict[str, Any]) -> Path:
        validate_content(json.dumps(event), "event.json")
        date = datetime.now(UTC)
        directory = self.root / "memory" / "events" / date.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{event['id']}.json"
        path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _mutate(self, operation: Mutation) -> dict[str, Any]:
        last_error: Exception | None = None
        with repository_lock(self.root):
            for _ in range(5):
                assert_clean(self.root)
                sync_from_remote(self.root)
                state = load_state(self.root)
                event, message = operation(state)
                self._write_event(event)
                if commit_and_push(self.root, message):
                    # A notes outage must never turn a published hub event into a failure.
                    warning = self._refresh_notes()
                    if warning:
                        event["notes_warning"] = warning
                    return event
                recover_after_rejected_push(self.root)
                last_error = GitError("Remote changed during operation")
                time.sleep(0.05)
        raise GitError(f"Could not publish operation after five retries: {last_error}")

    def sync(self) -> dict[str, Any]:
        with repository_lock(self.root):
            assert_clean(self.root)
            sync_from_remote(self.root)
        self.flush_outbox()
        warning = self._refresh_notes()
        result: dict[str, Any] = {"ok": True}
        if warning:
            result["notes_warning"] = warning
        return result

    def _refresh_notes(self) -> str | None:
        try:
            NotesBridge(self).render_all()
        except (OSError, ValueError) as exc:
            # `notes status`/`doctor` exposes the stale target; the next hub sync retries.
            return f"Notes refresh failed: {exc}"
        return None

    def draft_plan_definition(
        self,
        definition: dict[str, Any],
        actor: str,
        session: str,
        expected_revision: int,
        expected_hash: str,
    ) -> dict[str, Any]:
        """Create an unapproved revision after a notes-side compare-and-swap check."""
        validate_plan(definition, self.policy())
        plan_id = slug(str(definition["id"]))

        def operation(_: State) -> tuple[dict[str, Any], str]:
            current = load_plan(self.root, plan_id)
            if (
                int(current["revision"]) != expected_revision
                or definition_hash(current) != expected_hash
            ):
                raise ValueError(f"Plan changed while importing notes: {plan_id}")
            revisions = self.root / "memory" / "plans" / plan_id / "revisions"
            revision = expected_revision + 1
            imported = dict(definition)
            imported.update({
                "id": plan_id,
                "revision": revision,
                "created_at": timestamp(),
                "created_by": actor,
            })
            target = revisions / f"{revision:04d}.yaml"
            target.write_text(
                yaml.safe_dump(imported, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            event = self._base_event("plan_drafted", actor, session)
            event.update(
                {"plan_id": plan_id, "payload": {"revision": revision, "imported": "notes"}}
            )
            return event, f"hub: import notes revision {plan_id} revision {revision}"

        return self._mutate(operation)

    def scan(self) -> dict[str, int]:
        memory = self.root / "memory"
        files = [path for path in memory.rglob("*") if path.is_file()]
        for path in files:
            content = path.read_text(encoding="utf-8")
            validate_content(content, path.name)
            if path == policy_path(self.root):
                parse_policy(content)
            if "/plans/" in path.as_posix() and path.suffix == ".yaml":
                plan = yaml.safe_load(content)
                if not isinstance(plan, dict):
                    raise ValueError(f"Invalid plan file: {path}")
                validate_plan(plan)
        load_state(self.root)
        return {"files": len(files), "errors": 0}

    def policy(self) -> TierPolicy | None:
        return load_policy(self.root)

    def ensure_policy(self) -> dict[str, Any] | None:
        if policy_path(self.root).exists():
            return None

        def operation(_: State) -> tuple[dict[str, Any], str]:
            path = policy_path(self.root)
            if path.exists():
                raise ValueError("Tier policy already exists")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_POLICY_TEXT, encoding="utf-8")
            event = self._base_event("policy_initialized", "human", "interactive")
            event["payload"] = {"path": POLICY_RELATIVE_PATH.as_posix()}
            return event, "hub: initialize tier policy"

        return self._mutate(operation)

    def project_for_path(self, cwd: Path) -> tuple[str | None, str | None]:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            return None, None
        remote = normalize_remote(result.stdout.strip())
        return project_id_from_remote(remote), remote

    def register_project(self, path: Path, workspace: str | None = None) -> dict[str, Any]:
        project_id, remote = self.project_for_path(path)
        if not project_id or not remote:
            raise ValueError(f"No origin remote found for {path}")

        def operation(_: State) -> tuple[dict[str, Any], str]:
            projects = self.root / "memory" / "projects"
            projects.mkdir(parents=True, exist_ok=True)
            definition = {
                "id": project_id,
                "remote": remote,
                "workspace": workspace,
                "registered_at": timestamp(),
            }
            (projects / f"{project_id}.yaml").write_text(
                yaml.safe_dump(definition, sort_keys=False), encoding="utf-8"
            )
            event = self._base_event("project_registered", "human", "interactive")
            event["project_id"] = project_id
            event["payload"] = {"remote": remote, "workspace": workspace}
            return event, f"hub: register project {project_id}"

        return self._mutate(operation)

    def draft_plan(self, source: Path, actor: str, session: str) -> dict[str, Any]:
        text = source.read_text(encoding="utf-8")
        validate_content(text, source.name)
        plan = yaml.safe_load(text)
        if not isinstance(plan, dict):
            raise ValueError("Plan input must be a YAML object")
        validate_plan(plan, self.policy())
        plan_id = slug(str(plan["id"]))

        def operation(_: State) -> tuple[dict[str, Any], str]:
            revisions = self.root / "memory" / "plans" / plan_id / "revisions"
            existing = sorted(revisions.glob("*.yaml")) if revisions.exists() else []
            revision = len(existing) + 1
            plan["id"] = plan_id
            plan["revision"] = revision
            plan["created_at"] = timestamp()
            plan["created_by"] = actor
            revisions.mkdir(parents=True, exist_ok=True)
            target = revisions / f"{revision:04d}.yaml"
            target.write_text(
                yaml.safe_dump(plan, sort_keys=False, allow_unicode=True), encoding="utf-8"
            )
            event = self._base_event("plan_drafted", actor, session)
            event.update({"plan_id": plan_id, "payload": {"revision": revision}})
            return event, f"hub: draft {plan_id} revision {revision}"

        return self._mutate(operation)

    def approve_plan(self, plan_id: str, revision: int | None = None) -> dict[str, Any]:
        plan_id = slug(plan_id)

        def operation(_: State) -> tuple[dict[str, Any], str]:
            plan = load_plan(self.root, plan_id, revision)
            active_revision = int(plan["revision"])
            event = self._base_event("plan_approved", "human", "interactive")
            event.update({"plan_id": plan_id, "payload": {"revision": active_revision}})
            return event, f"hub: approve {plan_id} revision {active_revision}"

        return self._mutate(operation)

    def cancel_plan(self, plan_id: str, reason: str) -> dict[str, Any]:
        plan_id = slug(plan_id)
        validate_content(reason, "reason.md")

        def operation(state: State) -> tuple[dict[str, Any], str]:
            plan_state = state.plans.get(plan_id)
            if not plan_state or not plan_state.active:
                raise ValueError(f"Plan is not active: {plan_id}")
            if any(task.actively_claimed() for task in plan_state.tasks.values()):
                raise ValueError("Release active task claims before cancelling the plan")
            event = self._base_event("plan_cancelled", "human", "interactive")
            event.update({"plan_id": plan_id, "payload": {"reason": reason}})
            return event, f"hub: cancel plan {plan_id}"

        return self._mutate(operation)

    def list_plans(self) -> list[dict[str, Any]]:
        state = load_state(self.root)
        plans_root = self.root / "memory" / "plans"
        output: list[dict[str, Any]] = []
        if not plans_root.exists():
            return output
        for path in sorted(plans_root.iterdir()):
            if not path.is_dir():
                continue
            plan = load_plan(self.root, path.name)
            plan_state = state.plans.get(path.name, PlanState(path.name))
            status = "draft"
            if plan_state.active:
                status = "active"
            elif plan_state.completed:
                status = "completed"
            elif plan_state.cancelled:
                status = "cancelled"
            output.append(
                {
                    "id": path.name,
                    "title": plan["title"],
                    "latest_revision": plan["revision"],
                    "approved_revision": plan_state.approved_revision,
                    "pending_revision": (
                        plan["revision"]
                        if plan_state.approved_revision
                        and int(plan["revision"]) > plan_state.approved_revision
                        else None
                    ),
                    "status": status,
                }
            )
        return output

    def ready_tasks(self, plan_id: str | None = None) -> list[dict[str, Any]]:
        state = load_state(self.root)
        output: list[dict[str, Any]] = []
        for plan_summary in self.list_plans():
            current_id = plan_summary["id"]
            if plan_id and current_id != slug(plan_id):
                continue
            plan_state = state.plans.get(current_id)
            if not plan_state or not plan_state.active:
                continue
            plan = load_plan(self.root, current_id, plan_state.approved_revision)
            for task in plan["tasks"]:
                task_state = plan_state.tasks.get(task["id"])
                if task_state and (
                    task_state.actively_claimed() or task_state.status in {"completed", "blocked"}
                ):
                    continue
                dependencies = task.get("depends_on", [])
                if any(
                    plan_state.tasks.get(dependency, None) is None
                    or plan_state.tasks[dependency].status != "completed"
                    for dependency in dependencies
                ):
                    continue
                output.append({"plan_id": current_id, **task})
        return output

    def claim_task(
        self,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        cwd: Path,
        allow_tier_mismatch: bool = False,
    ) -> dict[str, Any]:
        plan_id = slug(plan_id)
        self.policy()  # fail fast on an invalid policy before touching git

        def operation(state: State) -> tuple[dict[str, Any], str]:
            plan_state, plan, task = self._active_task(state, plan_id, task_id)
            current = plan_state.tasks.get(task_id)
            if current and current.actively_claimed():
                raise ValueError(f"Task is already claimed by {current.owner}")
            for dependency in task.get("depends_on", []):
                dependency_state = plan_state.tasks.get(dependency)
                if not dependency_state or dependency_state.status != "completed":
                    raise ValueError(f"Dependency is not complete: {dependency}")
            model, tier, override_used = self._check_tier(
                task, task_id, session, allow_tier_mismatch
            )
            project = task.get("project")
            read_only = bool(task.get("read_only"))
            if read_only:
                worktree = None
                worktree_project = project
            else:
                worktree, worktree_project = self._worktree_identity(cwd)
            if not read_only and worktree_project != project:
                raise ValueError(
                    f"Task targets project {project}, but checkout is {worktree_project}"
                )
            scope = task.get("write_scope", [])
            for other_plan_id, other_plan_state in state.plans.items():
                if not other_plan_state.active:
                    continue
                other_plan = load_plan(self.root, other_plan_id, other_plan_state.approved_revision)
                for other_task in other_plan["tasks"]:
                    other_state = other_plan_state.tasks.get(other_task["id"])
                    if not other_state or not other_state.actively_claimed():
                        continue
                    if read_only or other_task.get("read_only"):
                        continue
                    if other_state.worktree == worktree:
                        raise ValueError("Another writing task is active in this checkout")
                    if other_task.get("project") == project and scopes_overlap(
                        scope, other_task.get("write_scope", [])
                    ):
                        raise ValueError(f"Write scope overlaps {other_plan_id}/{other_task['id']}")
            lease = timestamp_after(minutes=60)
            event = self._base_event("task_claimed", actor, session)
            event.update(
                {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "payload": {
                        "lease_until": lease,
                        "worktree": worktree,
                        "model": model,
                        "tier": tier,
                        "tier_override": override_used,
                    },
                }
            )
            return event, f"hub: claim {plan_id}/{task_id} for {actor}"

        return self._mutate(operation)

    def _check_tier(
        self,
        task: dict[str, Any],
        task_id: str,
        session: str,
        allow_tier_mismatch: bool,
    ) -> tuple[str | None, str | None, bool]:
        policy = self.policy()
        model = resolve_model(session)
        if policy is None:
            return model, None, False
        if not model:
            raise ValueError("Session has not declared a model; call brief with model=<id> first")
        tier = policy.tier_for_model(model)
        if tier == UNKNOWN_TIER:
            raise ValueError(f"Model '{model}' is not mapped to a tier in memory/policy/tiers.yaml")
        required = policy.task_tier(task)
        if required not in policy.tiers:
            raise ValueError(
                f"Task {task_id} requires tier '{required}', which is not defined in "
                "memory/policy/tiers.yaml"
            )
        if tier == required:
            return model, tier, False
        if not allow_tier_mismatch:
            raise ValueError(
                f"Task {task_id} requires tier {required}; session model {model} is tier {tier}"
            )
        return model, tier, True

    def checkpoint_task(
        self,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        summary: str,
        evidence: list[str],
    ) -> dict[str, Any]:
        validate_content(summary + "\n" + "\n".join(evidence), "checkpoint.md")
        payload = {
            "summary": summary,
            "evidence": evidence,
            "lease_until": timestamp_after(minutes=60),
        }
        try:
            return self._owned_task_event(
                "task_checkpoint", plan_id, task_id, actor, session, payload
            )
        except GitError as exc:
            return self._queue_checkpoint(
                plan_id, task_id, actor, session, payload, reason=str(exc)
            )

    def heartbeat_task(
        self, plan_id: str, task_id: str, actor: str, session: str
    ) -> dict[str, Any]:
        return self._owned_task_event(
            "task_heartbeat",
            plan_id,
            task_id,
            actor,
            session,
            {"lease_until": timestamp_after(minutes=60)},
        )

    def release_task(self, plan_id: str, task_id: str, actor: str, session: str) -> dict[str, Any]:
        return self._owned_task_event("task_released", plan_id, task_id, actor, session, {})

    def block_task(
        self, plan_id: str, task_id: str, actor: str, session: str, reason: str
    ) -> dict[str, Any]:
        validate_content(reason, "reason.md")
        return self._owned_task_event(
            "task_blocked", plan_id, task_id, actor, session, {"reason": reason}
        )

    def unblock_task(
        self, plan_id: str, task_id: str, actor: str, session: str, resolution: str
    ) -> dict[str, Any]:
        validate_content(resolution, "resolution.md")
        plan_id = slug(plan_id)

        def operation(state: State) -> tuple[dict[str, Any], str]:
            plan_state, _, _ = self._active_task(state, plan_id, task_id)
            current = plan_state.tasks.get(task_id)
            if not current or current.status != "blocked":
                raise ValueError("Task is not blocked")
            event = self._base_event("task_unblocked", actor, session)
            event.update(
                {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "payload": {"resolution": resolution},
                }
            )
            return event, f"hub: unblock {plan_id}/{task_id}"

        return self._mutate(operation)

    def complete_task(
        self,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        summary: str,
        evidence: list[str],
    ) -> dict[str, Any]:
        if not evidence:
            raise ValueError("Completion requires at least one evidence item")
        validate_content(summary + "\n" + "\n".join(evidence), "completion.md")
        return self._owned_task_event(
            "task_completed",
            plan_id,
            task_id,
            actor,
            session,
            {"summary": summary, "evidence": evidence},
        )

    def add_knowledge(
        self,
        scope: str,
        key: str,
        title: str,
        body: str,
        actor: str,
        session: str,
        supersedes: str | None = None,
        status: str = "active",
        kind: str = "fact",
    ) -> dict[str, Any]:
        validate_content(f"{title}\n{body}", "knowledge.md")
        if scope != "global" and not scope.startswith(("project:", "workspace:")):
            raise ValueError("Scope must be global, project:<id>, or workspace:<id>")
        if kind not in {"fact", "decision", "preference", "archive"}:
            raise ValueError("Knowledge kind must be fact, decision, preference, or archive")
        entry_id = event_id()

        def operation(_: State) -> tuple[dict[str, Any], str]:
            scope_path = scope.replace(":", "/")
            directory = self.root / "memory" / "knowledge" / scope_path / slug(key)
            existing = sorted(directory.glob("*.md")) if directory.exists() else []
            if existing:
                latest = read_frontmatter(existing[-1]).get("id")
                if supersedes != latest:
                    raise ValueError(f"Knowledge key already exists; supersedes must be {latest}")
            elif supersedes:
                raise ValueError("Cannot supersede an unknown knowledge entry")
            directory.mkdir(parents=True, exist_ok=True)
            metadata = {
                "id": entry_id,
                "key": slug(key),
                "title": title,
                "scope": scope,
                "created_at": timestamp(),
                "created_by": actor,
                "supersedes": supersedes,
                "status": status,
                "kind": kind,
            }
            content = f"---\n{yaml.safe_dump(metadata, sort_keys=False)}---\n\n{body.strip()}\n"
            (directory / f"{entry_id}.md").write_text(content, encoding="utf-8")
            event = self._base_event("knowledge_added", actor, session)
            event["payload"] = metadata
            return event, f"hub: add knowledge {scope}/{slug(key)}"

        return self._mutate(operation)

    def retire_knowledge(
        self,
        scope: str,
        key: str,
        reason: str,
        actor: str,
        session: str,
    ) -> dict[str, Any]:
        directory = self.root / "memory" / "knowledge" / scope.replace(":", "/") / slug(key)
        existing = sorted(directory.glob("*.md")) if directory.exists() else []
        if not existing:
            raise ValueError(f"Unknown knowledge key: {scope}/{key}")
        latest = read_frontmatter(existing[-1]).get("id")
        return self.add_knowledge(
            scope,
            key,
            f"Retired: {key}",
            reason,
            actor,
            session,
            supersedes=str(latest),
            status="retired",
            kind="fact",
        )

    def search(self, query: str, limit: int = 20) -> list[dict[str, str]]:
        words = [word.lower() for word in query.split() if word]
        if not words:
            return []
        output: list[tuple[int, dict[str, str]]] = []
        root = self.root / "memory" / "knowledge"
        if not root.exists():
            return []
        for entry in self._current_knowledge(include_archives=True):
            path = entry["path"]
            text = entry["text"]
            lowered = text.lower()
            score = sum(lowered.count(word) for word in words)
            if score:
                output.append(
                    (score, {"path": str(path.relative_to(self.root)), "excerpt": text[:500]})
                )
        ranked = sorted(output, key=lambda item: (-item[0], item[1]["path"]))
        return [item for _, item in ranked[:limit]]

    def brief(
        self,
        cwd: Path,
        max_bytes: int = 12_000,
        model: str | None = None,
        actor: str = "agent",
        session: str | None = None,
    ) -> str:
        project_id, remote = self.project_for_path(cwd)
        state = load_state(self.root)
        policy = self.policy()
        resolved_model = resolve_model(session, model)
        session_tier: str | None = None
        if resolved_model and policy is not None:
            session_tier = policy.tier_for_model(resolved_model)
        if resolved_model and session:
            record_session(session, actor, resolved_model, session_tier)
        filtering = policy is not None and session_tier not in {None, UNKNOWN_TIER}

        lines = ["# Agent Hub brief", "", f"Project: {project_id or 'unregistered'}"]
        if self.profile:
            lines.append(f"Hub: {self.profile} · {remote_url(self.root) or 'local only'}")
        if self.overridden_profile:
            lines.append(
                f"Warning: AGENT_HUB_REPO overrides AGENT_HUB_PROFILE={self.overridden_profile}; "
                f"this session writes to {self.root}"
            )
        if remote:
            lines.append(f"Remote: {remote}")
        if resolved_model:
            tier_display = session_tier or "unenforced"
            lines.append(f"Session: {actor} · {resolved_model} · tier={tier_display}")
            if session_tier == UNKNOWN_TIER:
                lines.append(
                    f"Warning: model {resolved_model} is not mapped in "
                    "memory/policy/tiers.yaml; claims will be rejected"
                )
        else:
            lines.append(
                "Session: undeclared — pass model=<your model id> to hub_get_brief before claiming"
            )
        lines.extend(["", "## Active plans"])
        for summary in self.list_plans():
            plan_state = state.plans.get(summary["id"])
            if not plan_state or not plan_state.active:
                continue
            plan = load_plan(self.root, summary["id"], plan_state.approved_revision)
            relevant = [
                task
                for task in plan["tasks"]
                if not project_id or task.get("project") in {None, project_id, remote}
            ]
            if not relevant:
                continue
            lines.append(f"- {summary['id']}: {summary['title']}")
            hidden: dict[str, int] = {}
            for task in relevant:
                if filtering and policy is not None:
                    task_tier = policy.task_tier(task)
                    if task_tier != session_tier:
                        hidden[task_tier] = hidden.get(task_tier, 0) + 1
                        continue
                task_state = plan_state.tasks.get(task["id"])
                status = task_state.status if task_state else "ready"
                owner = f" ({task_state.owner})" if task_state and task_state.owner else ""
                lines.append(f"  - {task['id']}: {status}{owner} — {task['title']}")
                if task_state and task_state.summary:
                    lines.append(f"    Latest: {task_state.summary}")
            if hidden:
                detail = ", ".join(f"{count} {tier}" for tier, count in sorted(hidden.items()))
                lines.append(f"  - {sum(hidden.values())} task(s) hidden by tier: {detail}")
        lines.extend(["", "## Knowledge"])
        allowed = {"global"}
        if project_id:
            allowed.add(f"project/{project_id}")
            for workspace in self._workspaces_for_project(project_id):
                allowed.add(f"workspace/{workspace}")
        scoped: dict[str, dict[str, Any]] = {}
        scope_order = ["global"]
        scope_order.extend(sorted(value for value in allowed if value.startswith("workspace/")))
        scope_order.extend(sorted(value for value in allowed if value.startswith("project/")))
        for selected_scope in scope_order:
            for entry in self._current_knowledge():
                if entry["scope_path"] == selected_scope:
                    scoped[entry["key"]] = entry
        for entry in scoped.values():
            lines.append(f"- {entry['body'][:500]}")
        result = "\n".join(lines).strip() + "\n"
        encoded = result.encode("utf-8")
        if len(encoded) <= max_bytes:
            return result
        return encoded[: max_bytes - 32].decode("utf-8", errors="ignore") + "\n[brief truncated]\n"

    def _current_knowledge(self, include_archives: bool = False) -> list[dict[str, Any]]:
        root = self.root / "memory" / "knowledge"
        if not root.exists():
            return []
        entries: list[dict[str, Any]] = []
        for directory in sorted(path for path in root.rglob("*") if path.is_dir()):
            revisions = sorted(directory.glob("*.md"))
            if not revisions:
                continue
            path = revisions[-1]
            metadata = read_frontmatter(path)
            if metadata.get("status", "active") != "active":
                continue
            if metadata.get("kind") == "archive" and not include_archives:
                continue
            text = path.read_text(encoding="utf-8")
            body = text.split("---", 2)[-1].strip() if text.startswith("---") else text.strip()
            entries.append(
                {
                    "path": path,
                    "text": text,
                    "body": body,
                    "key": metadata.get("key", directory.name),
                    "scope_path": str(directory.parent.relative_to(root)),
                    "kind": metadata.get("kind", "fact"),
                }
            )
        return entries

    def _workspaces_for_project(self, project_id: str) -> list[str]:
        root = self.root / "memory" / "workspaces"
        if not root.exists():
            return []
        matches: list[str] = []
        for path in root.glob("*.yaml"):
            definition = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            projects = definition.get("projects", [])
            if any(project.get("id") == project_id for project in projects):
                matches.append(str(definition["id"]))
        return matches

    def _worktree_identity(self, cwd: Path) -> tuple[str, str | None]:
        resolved = cwd.expanduser().resolve()
        top = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
            check=False,
            text=True,
            capture_output=True,
        )
        if top.returncode:
            raise ValueError(f"Task claims require a Git worktree: {resolved}")
        project_id, _ = self.project_for_path(resolved)
        return str(Path(top.stdout.strip()).resolve()), project_id

    def _outbox_root(self) -> Path:
        return state_root() / "outbox"

    def _queue_checkpoint(
        self,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        payload: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        queued = {
            "id": event_id(),
            "queued_at": timestamp(),
            "plan_id": slug(plan_id),
            "task_id": task_id,
            "actor": actor,
            "session": session,
            "payload": payload,
            "reason": reason,
        }
        root = self._outbox_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{queued['id']}.json").write_text(
            json.dumps(queued, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {"queued": True, **queued}

    def flush_outbox(self) -> list[dict[str, Any]]:
        root = self._outbox_root()
        if not root.exists():
            return []
        published: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            queued = json.loads(path.read_text(encoding="utf-8"))
            payload = dict(queued["payload"])
            payload["queued_at"] = queued["queued_at"]
            payload["delayed"] = True
            try:
                event = self._owned_task_event(
                    "task_checkpoint",
                    queued["plan_id"],
                    queued["task_id"],
                    queued["actor"],
                    queued["session"],
                    payload,
                )
            except ValueError:
                event = self._delayed_checkpoint_event(queued, payload)
            path.unlink()
            published.append(event)
        return published

    def _delayed_checkpoint_event(
        self, queued: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        def operation(_: State) -> tuple[dict[str, Any], str]:
            event = self._base_event("task_delayed_checkpoint", queued["actor"], queued["session"])
            event.update(
                {
                    "plan_id": queued["plan_id"],
                    "task_id": queued["task_id"],
                    "payload": payload,
                }
            )
            return event, (f"hub: delayed checkpoint {queued['plan_id']}/{queued['task_id']}")

        return self._mutate(operation)

    def _base_event(self, kind: str, actor: str, session: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": event_id(),
            "occurred_at": timestamp(),
            "type": kind,
            "actor": actor,
            "session": session,
            "payload": {},
        }

    def _active_task(
        self, state: State, plan_id: str, task_id: str
    ) -> tuple[PlanState, dict[str, Any], dict[str, Any]]:
        plan_state = state.plans.get(plan_id)
        if not plan_state or not plan_state.active:
            raise ValueError(f"Plan is not active: {plan_id}")
        plan = load_plan(self.root, plan_id, plan_state.approved_revision)
        task = task_definition(plan, task_id)
        return plan_state, plan, task

    def _owned_task_event(
        self,
        kind: str,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        plan_id = slug(plan_id)

        def operation(state: State) -> tuple[dict[str, Any], str]:
            plan_state, plan, _ = self._active_task(state, plan_id, task_id)
            current = plan_state.tasks.get(task_id)
            if not current or not current.actively_claimed():
                raise ValueError("Task does not have an active claim")
            if current.owner != actor or current.session != session:
                raise ValueError(f"Task is owned by {current.owner}/{current.session}")
            event = self._base_event(kind, actor, session)
            event.update({"plan_id": plan_id, "task_id": task_id, "payload": payload})

            if kind == "task_completed" and self._would_complete_plan(plan_state, plan, task_id):
                payload["completes_plan"] = True
            return event, f"hub: {kind.removeprefix('task_')} {plan_id}/{task_id}"

        return self._mutate(operation)

    def _would_complete_plan(
        self, plan_state: PlanState, plan: dict[str, Any], completing_task: str
    ) -> bool:
        completed = {
            task_id for task_id, state in plan_state.tasks.items() if state.status == "completed"
        }
        completed.add(completing_task)
        if any(task["id"] not in completed for task in plan["tasks"]):
            return False
        required = {item["id"] for item in plan.get("acceptance_criteria", [])}
        covered = {
            criterion
            for task in plan["tasks"]
            if task["id"] in completed
            for criterion in task.get("covers", [])
        }
        return required <= covered


def timestamp_after(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def read_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    frontmatter = text.split("---", 2)[1]
    parsed = yaml.safe_load(frontmatter)
    return parsed if isinstance(parsed, dict) else {}
