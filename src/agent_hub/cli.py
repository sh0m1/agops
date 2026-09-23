from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .adapters import install_adapters
from .config import load_profiles, resolve_repo, save_profiles
from .health import INFORMATIONAL, doctor
from .hub import Hub, read_frontmatter
from .notes import NotesBridge
from .policy import PolicyError, policy_path
from .sessions import record_session, resolve_model
from .setup import setup
from .state import load_plan


def default_session() -> str:
    """AGENT_HUB_SESSION when set; otherwise an id stable for the life of this terminal."""
    return os.environ.get("AGENT_HUB_SESSION") or f"shell-{os.getppid()}"


def actor_session(args: argparse.Namespace) -> tuple[str, str]:
    actor = getattr(args, "actor", None) or os.environ.get("AGENT_HUB_ACTOR", "agent")
    session = getattr(args, "session", None) or default_session()
    return actor, session


def emit(value: Any, as_json: bool = False) -> None:
    if as_json or not isinstance(value, str):
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(value, end="" if value.endswith("\n") else "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-hub")
    parser.add_argument("--repo", help="Agent Hub runtime clone")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("sync")
    commands.add_parser("doctor")
    commands.add_parser("scan")
    notes = commands.add_parser("notes")
    notes_commands = notes.add_subparsers(dest="notes_command", required=True)
    notes_connect = notes_commands.add_parser("connect")
    notes_connect.add_argument("target")
    notes_commands.add_parser("status")
    notes_commands.add_parser("review")
    notes_commands.add_parser("sync")
    notes_resolve = notes_commands.add_parser("resolve")
    notes_resolve.add_argument("plan_id")
    notes_resolve.add_argument("--take", required=True, choices=("notes", "agops"))
    notes_resolve.add_argument("--yes", action="store_true")
    notes_commands.add_parser("disconnect")
    session_parser = commands.add_parser("session")
    session_parser.add_argument("--actor", default="agent")
    session_parser.add_argument("--value", action="store_true")
    brief = commands.add_parser("brief")
    brief.add_argument("--cwd", default=".")
    brief.add_argument("--model", help="Model id of this session, e.g. claude-sonnet-5")

    policy_parser = commands.add_parser("policy")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_commands.add_parser("show")
    policy_commands.add_parser("validate")

    search = commands.add_parser("search")
    search.add_argument("query")

    migrate = commands.add_parser("migrate-remember")
    migrate.add_argument("path")
    migrate.add_argument("--project", required=True)
    migrate.add_argument("--actor", default="migration")

    setup_parser = commands.add_parser("setup")
    where = setup_parser.add_mutually_exclusive_group()
    where.add_argument("--remote", help="Memory repository URL; remembered after the first run")
    where.add_argument(
        "--local",
        action="store_true",
        help="Keep the memory on this machine only; detaches and forgets any remote",
    )
    setup_parser.add_argument(
        "--profile", help="Hub profile to create or refresh (default: the default profile)"
    )
    setup_parser.add_argument(
        "--default", action="store_true", help="Make this profile the default"
    )
    setup_parser.add_argument("--runtime", help="Override the profile's runtime clone path")
    setup_parser.add_argument("--keep-claude-memory", action="store_true")

    profile_parser = commands.add_parser("profile")
    profile_commands = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list")
    profile_default = profile_commands.add_parser("default")
    profile_default.add_argument("name")

    adapter = commands.add_parser("adapter")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_install = adapter_commands.add_parser("install")
    adapter_install.add_argument("path")
    adapter_install.add_argument(
        "--tools", default="agents,claude", help="Comma-separated adapter names"
    )

    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    register = project_commands.add_parser("register")
    register.add_argument("path")
    register.add_argument("--workspace")

    knowledge = commands.add_parser("knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    add = knowledge_commands.add_parser("add")
    add.add_argument("--scope", required=True)
    add.add_argument("--key", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--body-file", required=True)
    add.add_argument("--supersedes")
    add.add_argument(
        "--kind", choices=("fact", "decision", "preference", "archive"), default="fact"
    )
    add.add_argument("--actor")
    add.add_argument("--session")
    retire = knowledge_commands.add_parser("retire")
    retire.add_argument("--scope", required=True)
    retire.add_argument("--key", required=True)
    retire.add_argument("--reason", required=True)
    retire.add_argument("--actor")
    retire.add_argument("--session")

    plan = commands.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    draft = plan_commands.add_parser("draft")
    draft.add_argument("source")
    draft.add_argument("--actor")
    draft.add_argument("--session")
    approve = plan_commands.add_parser("approve")
    approve.add_argument("plan_id")
    approve.add_argument("--revision", type=int)
    approve.add_argument("--yes", action="store_true")
    cancel = plan_commands.add_parser("cancel")
    cancel.add_argument("plan_id")
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--yes", action="store_true")
    plan_commands.add_parser("list")
    show = plan_commands.add_parser("show")
    show.add_argument("plan_id")
    show.add_argument("--revision", type=int)

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    ready = task_commands.add_parser("ready")
    ready.add_argument("--plan")
    ready.add_argument("--tier")
    for name in ("claim", "release"):
        command = task_commands.add_parser(name)
        command.add_argument("plan_id")
        command.add_argument("task_id")
        command.add_argument("--actor")
        command.add_argument("--session")
        if name == "claim":
            command.add_argument("--cwd", default=".")
            command.add_argument("--allow-tier-mismatch", action="store_true")
    checkpoint = task_commands.add_parser("checkpoint")
    checkpoint.add_argument("plan_id")
    checkpoint.add_argument("task_id")
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--evidence", action="append", default=[])
    checkpoint.add_argument("--actor")
    checkpoint.add_argument("--session")
    block = task_commands.add_parser("block")
    block.add_argument("plan_id")
    block.add_argument("task_id")
    block.add_argument("--reason", required=True)
    block.add_argument("--actor")
    block.add_argument("--session")
    unblock = task_commands.add_parser("unblock")
    unblock.add_argument("plan_id")
    unblock.add_argument("task_id")
    unblock.add_argument("--resolution", required=True)
    unblock.add_argument("--actor")
    unblock.add_argument("--session")
    complete = task_commands.add_parser("complete")
    complete.add_argument("plan_id")
    complete.add_argument("task_id")
    complete.add_argument("--summary", default="Completed")
    complete.add_argument("--evidence", action="append", default=[])
    complete.add_argument("--actor")
    complete.add_argument("--session")

    run = commands.add_parser("run")
    run.add_argument("tool")
    run.add_argument("--actor")
    run.add_argument("--plan")
    run.add_argument("--task")
    run.add_argument("--cwd", default=".")
    run.add_argument("--model", help="Model id the launched tool runs, e.g. gpt-5.6-terra")
    run.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = dispatch(args)
        if result is not None:
            emit(result, args.json)
    except Exception as exc:
        if args.json:
            emit({"ok": False, "error": str(exc)}, True)
        else:
            print(f"agent-hub: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "setup":
        summary = setup(
            args.remote,
            Path(args.runtime) if args.runtime else None,
            not args.keep_claude_memory,
            local=args.local,
            profile=args.profile,
            make_default=args.default,
        )
        return summary if args.json else format_setup_summary(summary)
    if args.command == "profile":
        if args.profile_command == "default":
            return set_default_profile(args.name)
        return profile_report(as_json=args.json)
    if args.command == "adapter":
        tools = [tool.strip() for tool in args.tools.split(",") if tool.strip()]
        return {"written": install_adapters(Path(args.path), tools)}
    hub = Hub.from_environment(args.repo)
    if args.command == "sync":
        return hub.sync()
    if args.command == "session":
        session = str(uuid.uuid4())
        return session if args.value else {"actor": args.actor, "session": session}
    if args.command == "doctor":
        return doctor(hub)
    if args.command == "scan":
        return hub.scan()
    if args.command == "notes":
        bridge = NotesBridge(hub)
        if args.notes_command == "connect":
            return bridge.connect(Path(args.target))
        if args.notes_command == "status":
            return bridge.status()
        if args.notes_command == "review":
            return bridge.review()
        if args.notes_command == "disconnect":
            return bridge.disconnect()
        actor, session = actor_session(args)
        if args.notes_command == "sync":
            sync_result = hub.sync()
            result = bridge.sync(actor, session)
            if "notes_warning" in sync_result:
                result["notes_warning"] = sync_result["notes_warning"]
            return result
        require_human_confirmation(args.plan_id, args.yes, "resolve", noun="plan")
        return bridge.resolve(args.plan_id, args.take, actor, session)
    if args.command == "brief":
        return hub.brief(
            Path(args.cwd),
            model=args.model,
            actor=os.environ.get("AGENT_HUB_ACTOR", "agent"),
            session=default_session(),
        )
    if args.command == "policy":
        return policy_report(hub, validate_only=args.policy_command == "validate")
    if args.command == "search":
        return hub.search(args.query)
    if args.command == "migrate-remember":
        return migrate_remember(hub, Path(args.path), args.project, args.actor)
    if args.command == "project":
        return hub.register_project(Path(args.path), args.workspace)
    if args.command == "knowledge":
        actor, session = actor_session(args)
        if args.knowledge_command == "retire":
            return hub.retire_knowledge(args.scope, args.key, args.reason, actor, session)
        return hub.add_knowledge(
            args.scope,
            args.key,
            args.title,
            Path(args.body_file).read_text(encoding="utf-8"),
            actor,
            session,
            args.supersedes,
            kind=args.kind,
        )
    if args.command == "plan":
        if args.plan_command == "draft":
            actor, session = actor_session(args)
            return hub.draft_plan(Path(args.source), actor, session)
        if args.plan_command == "approve":
            if os.environ.get("AGENT_HUB_AGENT_SESSION"):
                raise ValueError("Plan approval is unavailable inside a managed agent session")
            if not args.yes:
                if not sys.stdin.isatty():
                    raise ValueError("Plan approval requires an interactive terminal or --yes")
                answer = input(f"Approve plan {args.plan_id}? Type its id: ")
                if answer != args.plan_id:
                    raise ValueError("Approval cancelled")
            return hub.approve_plan(args.plan_id, args.revision)
        if args.plan_command == "cancel":
            if os.environ.get("AGENT_HUB_AGENT_SESSION"):
                raise ValueError("Plan cancellation is unavailable inside an agent session")
            require_human_confirmation(args.plan_id, args.yes, "cancel")
            return hub.cancel_plan(args.plan_id, args.reason)
        if args.plan_command == "list":
            return hub.list_plans()
        return load_plan(hub.root, args.plan_id, args.revision)
    if args.command == "task":
        if args.task_command == "ready":
            tasks = hub.ready_tasks(args.plan)
            if args.tier:
                policy = hub.policy()
                if policy is None:
                    raise ValueError("task ready --tier requires memory/policy/tiers.yaml")
                if args.tier not in policy.tiers:
                    raise ValueError(f"Unknown tier: {args.tier}")
                tasks = [task for task in tasks if policy.task_tier(task) == args.tier]
            return tasks
        actor, session = actor_session(args)
        if args.task_command == "claim":
            if args.allow_tier_mismatch:
                if os.environ.get("AGENT_HUB_AGENT_SESSION"):
                    raise ValueError("Tier override is unavailable inside a managed agent session")
                require_human_confirmation(
                    args.task_id,
                    False,
                    "override tier for",
                    noun="task",
                    flag_hint=None,
                    label="Tier override",
                    prompt=f"Override tier for task {args.task_id}? Type its id: ",
                )
            return hub.claim_task(
                args.plan_id,
                args.task_id,
                actor,
                session,
                Path(args.cwd),
                allow_tier_mismatch=args.allow_tier_mismatch,
            )
        if args.task_command == "checkpoint":
            return hub.checkpoint_task(
                args.plan_id, args.task_id, actor, session, args.summary, args.evidence
            )
        if args.task_command == "block":
            return hub.block_task(args.plan_id, args.task_id, actor, session, args.reason)
        if args.task_command == "unblock":
            return hub.unblock_task(args.plan_id, args.task_id, actor, session, args.resolution)
        if args.task_command == "release":
            return hub.release_task(args.plan_id, args.task_id, actor, session)
        return hub.complete_task(
            args.plan_id, args.task_id, actor, session, args.summary, args.evidence
        )
    if args.command == "run":
        return run_agent(
            hub,
            args.tool,
            args.args,
            args.actor or args.tool,
            args.plan,
            args.task,
            Path(args.cwd),
            args.model,
        )
    raise ValueError(f"Unsupported command: {args.command}")


def _remote_line(summary: dict[str, Any]) -> str:
    if summary["remote"]:
        return summary["remote"]
    if summary.get("remote_removed"):
        return f"local only (detached from {summary['remote_removed']})"
    return "local only (share it later: agent-hub setup --remote <url>)"


def format_setup_summary(summary: dict[str, Any]) -> str:
    tool_words = {"configured": "configured", "skipped-not-installed": "not installed, skipped"}
    report = summary["doctor"]
    failing = sorted(
        key for key, value in report.items() if key not in INFORMATIONAL and not value
    )
    if str(report["policy"]).startswith("invalid"):
        failing.append("policy")
    default_marker = " (default)" if summary["profile"] == summary["default_profile"] else ""
    lines = [
        "Agent Hub setup " + ("complete" if summary["ok"] else "finished with problems"),
        f"  profile:  {summary['profile']}{default_marker}",
        f"  runtime:  {summary['runtime']}",
        "  remote:   " + _remote_line(summary),
        f"  codex:    {tool_words[summary['tools']['codex']]}",
        f"  claude:   {tool_words[summary['tools']['claude']]}",
        f"  policy:   {summary['policy'].replace('-', ' ')}",
        f"  scan:     {summary['scan']['files']} files, {summary['scan']['errors']} errors",
        "  doctor:   " + ("ok" if summary["ok"] else "NOT OK (" + ", ".join(failing) + ")"),
    ]
    skipped = [tool for tool, state in summary["tools"].items() if state != "configured"]
    if skipped:
        lines.append(
            f"Next: install {', '.join(skipped)} and re-run `agent-hub setup` to register the "
            "MCP server there."
        )
    pinned = report.get("mcp_pinned") or []
    if pinned:
        lines.append(
            f"Next: the {', '.join(pinned)} MCP registration still pins a hub path; re-run "
            "`agent-hub setup` after upgrading so AGENT_HUB_PROFILE takes effect."
        )
    return "\n".join(lines) + "\n"


def profile_report(as_json: bool) -> Any:
    profiles = load_profiles()
    error: str | None = None
    resolution = None
    try:
        resolution = resolve_repo(None)
    except ValueError as exc:
        error = str(exc)
    current = resolution.profile if resolution else None
    entries = [
        {
            "name": name,
            "repo": entry.get("repo"),
            "remote": entry.get("remote"),
            "default": name == profiles.default,
            "current": name == current,
        }
        for name, entry in sorted(profiles.profiles.items())
    ]
    data = {
        "profiles": entries,
        "default": profiles.default,
        "current": current,
        "source": resolution.source if resolution else None,
        "overridden_profile": resolution.overridden_profile if resolution else None,
        "error": error,
    }
    if as_json:
        return data
    lines: list[str] = []
    if not entries:
        lines.append("No hub profiles configured; run `agent-hub setup [--profile NAME]`.")
    for entry in entries:
        markers = ("* " if entry["default"] else "  ") + ("← " if entry["current"] else "  ")
        where = entry["remote"] or "local only"
        lines.append(f"{markers}{entry['name']:<12} {where:<44} {entry['repo']}")
    if resolution and resolution.source == "explicit":
        lines.append(f"Current hub: explicit path {resolution.root} (AGENT_HUB_REPO or --repo)")
        if resolution.overridden_profile:
            lines.append(
                f"Warning: this overrides AGENT_HUB_PROFILE={resolution.overridden_profile}"
            )
    elif resolution:
        lines.append(f"Current hub: {resolution.profile} (via {resolution.source})")
    if error:
        lines.append(f"Error: {error}")
    return "\n".join(lines) + "\n"


def set_default_profile(name: str) -> dict[str, Any]:
    profiles = load_profiles()
    if name not in profiles.profiles:
        names = ", ".join(sorted(profiles.profiles)) or "none"
        raise ValueError(f"Unknown hub profile '{name}'; configured: {names}")
    profiles.default = name
    save_profiles(profiles)
    return {"default": name}


def require_human_confirmation(
    identifier: str,
    yes: bool,
    action: str,
    noun: str = "plan",
    flag_hint: str | None = "--yes",
    label: str | None = None,
    prompt: str | None = None,
) -> None:
    if yes:
        return
    subject = label if label is not None else f"{noun.title()} {action}"
    if not sys.stdin.isatty():
        hint = f" or {flag_hint}" if flag_hint else ""
        raise ValueError(f"{subject} requires an interactive terminal{hint}")
    if prompt is not None:
        question = prompt
    else:
        question = f"{action.title()} {noun} {identifier}? Type its id: "
    answer = input(question)
    if answer != identifier:
        raise ValueError(f"{subject} cancelled")


def policy_report(hub: Hub, validate_only: bool) -> dict[str, Any]:
    path = policy_path(hub.root)
    report: dict[str, Any] = {"path": str(path), "present": path.exists()}
    if not path.exists():
        report["valid"] = None
        return report
    try:
        policy = hub.policy()
    except PolicyError as exc:
        if validate_only:
            raise ValueError(str(exc)) from exc
        report.update({"valid": False, "error": str(exc)})
        return report
    report["valid"] = True
    if validate_only or policy is None:
        return report
    report["default_task_tier"] = policy.default_task_tier
    report["tiers"] = {name: list(patterns) for name, patterns in policy.tiers.items()}
    session = default_session()
    model = resolve_model(session)
    report["session"] = {
        "session": session,
        "model": model,
        "tier": policy.tier_for_model(model) if model else None,
    }
    return report


def run_agent(
    hub: Hub,
    tool: str,
    trailing: list[str],
    actor: str,
    plan_id: str | None,
    task_id: str | None,
    cwd: Path,
    model: str | None = None,
) -> dict[str, Any]:
    if bool(plan_id) != bool(task_id):
        raise ValueError("--plan and --task must be supplied together")
    resolved = resolve_model(None, model)
    policy = hub.policy()
    if not resolved and policy and plan_id and task_id:
        raise ValueError(
            "agent-hub run needs --model <id> (or AGENT_HUB_MODEL) to claim a tiered task"
        )
    hub.sync()
    session = str(uuid.uuid4())
    if resolved:
        tier = policy.tier_for_model(resolved) if policy else None
        record_session(session, actor, resolved, tier)
    if plan_id and task_id:
        hub.claim_task(plan_id, task_id, actor, session, cwd)
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_HUB_REPO": str(hub.root),
            "AGENT_HUB_ACTOR": actor,
            "AGENT_HUB_SESSION": session,
            "AGENT_HUB_AGENT_SESSION": "1",
        }
    )
    if resolved:
        environment["AGENT_HUB_MODEL"] = resolved
    stopped = threading.Event()

    def heartbeat() -> None:
        while not stopped.wait(600):
            try:
                if plan_id and task_id:
                    hub.heartbeat_task(plan_id, task_id, actor, session)
                else:
                    hub.sync()
            except Exception:
                continue

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        completed = subprocess.run([tool, *trailing], check=False, env=environment, cwd=cwd)
    finally:
        stopped.set()
        thread.join(timeout=1)
    if plan_id and task_id:
        hub.checkpoint_task(
            plan_id,
            task_id,
            actor,
            session,
            f"Managed {tool} session exited with code {completed.returncode}",
            [f"process-exit:{completed.returncode}"],
        )
    return {"session": session, "exit_code": completed.returncode}


def migrate_remember(hub: Hub, source: Path, project: str, actor: str) -> dict[str, Any]:
    files = sorted(
        path for path in source.glob("*.md") if path.name != "now.md" or path.stat().st_size
    )
    sections: list[str] = [
        "Imported as historical provenance. Status words in this archive are not current "
        "task state."
    ]
    for path in files:
        if path.name == "RESUME.md":
            continue
        content = path.read_text(encoding="utf-8").strip()
        if content:
            sections.extend([f"## {path.name}", content])
    if len(sections) == 1:
        raise ValueError("No remember journal content found")
    directory = hub.root / "memory" / "knowledge" / "project" / project / "legacy-remember-archive"
    revisions = sorted(directory.glob("*.md")) if directory.exists() else []
    supersedes = None
    if revisions:
        supersedes = str(read_frontmatter(revisions[-1])["id"])
    return hub.add_knowledge(
        f"project:{project}",
        "legacy-remember-archive",
        "Legacy .remember archive",
        "\n\n".join(sections),
        actor,
        str(uuid.uuid4()),
        supersedes=supersedes,
        kind="archive",
    )
