from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    config_path,
    load_profiles,
    profile_runtime,
    save_profiles,
    validate_profile_name,
)
from .git import has_remote, remote_url
from .health import doctor
from .hub import Hub
from .sessions import default_home

__all__ = [
    "INSTRUCTIONS",
    "config_path",
    "merge_managed_block",
    "render_instructions",
    "setup",
]

MANAGED_START = "<!-- BEGIN AGENT HUB MANAGED -->"
MANAGED_END = "<!-- END AGENT HUB MANAGED -->"
INSTRUCTIONS = f"""{MANAGED_START}
## Shared agops hub

At the start of each work session, call `agops brief --cwd \"$PWD\"` or the MCP
`hub_get_brief` tool. Pass your current model id to `hub_get_brief` (or
`agops brief --model`) at session start and again if the model changes; claims are limited
to tasks matching your tier. Before modifying files for an approved shared plan, claim a ready
task. Checkpoint meaningful progress and before handoff or context compaction. Record progress
in task checkpoints; keep knowledge for durable facts, decisions and preferences. Complete tasks
only with test, artifact, or commit evidence. Never store credentials, `.env` contents, or raw
transcripts. Keep one stable session ID across standalone CLI task calls. User instructions
always take precedence over agops state.

## Tool access order

Try a CLI or an MCP tool first for any external system (`gh`, `aws`, `az`, `gcloud`, `kubectl`,
`terraform`, `git`, or a configured MCP server). They are scriptable, quotable, and reproducible,
and their output can be cited as evidence. Use a browser only as a fallback: when no CLI or MCP
path exists, the available one is scoped or authenticated wrong, it fails, or it cannot answer the
question. Falling back to the browser is expected and fine — do not keep fighting a CLI that
clearly will not work. When you do fall back, state in one line why the CLI or MCP route was not
enough.
{MANAGED_END}
"""

PREFERENCE_HEADING = "## Standing preferences"


def render_instructions(hub: Hub | None) -> str:
    """INSTRUCTIONS plus the hub's active global preferences.

    The instruction files are generated from the hub, so a preference is recorded once with
    `agops knowledge add --scope global --kind preference` and reaches every client from
    there. Only global scope is rendered: these files are loaded for every session, whatever
    directory it starts in, so a workspace- or project-scoped preference does not belong here
    - the brief and the UserPromptSubmit hook deliver those.
    """
    if hub is None:
        return INSTRUCTIONS
    try:
        entries = [
            entry
            for entry in hub._current_knowledge()
            if entry["scope_path"] == "global" and entry["kind"] == "preference"
        ]
    except OSError:
        return INSTRUCTIONS
    if not entries:
        return INSTRUCTIONS
    lines = [PREFERENCE_HEADING, ""]
    for entry in sorted(entries, key=lambda item: item["key"]):
        lines.append(f"- {' '.join(entry['body'].split())}")
    section = "\n".join(lines)
    return INSTRUCTIONS.replace(MANAGED_END, f"{section}\n{MANAGED_END}")


# Codex starts MCP servers with a whitelisted environment; these are forwarded explicitly so a
# terminal's AGENT_HUB_* settings reach the server exactly as they do under Claude Code.
CODEX_FORWARDED_ENV = [
    "AGENT_HUB_PROFILE",
    "AGENT_HUB_REPO",
    "AGENT_HUB_HOME",
    "AGENT_HUB_STATE_DIR",
    "AGENT_HUB_MODEL",
    "AGENT_HUB_SESSION",
    "AGENT_HUB_ACTOR",
]

Which = Callable[[str], str | None]
Runner = Callable[..., subprocess.CompletedProcess]


def merge_managed_block(path: Path, content: str = INSTRUCTIONS, backup: bool = True) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    adopting = MANAGED_START not in existing
    if MANAGED_START in existing and MANAGED_END in existing:
        before, rest = existing.split(MANAGED_START, 1)
        _, after = rest.split(MANAGED_END, 1)
        prefix = before.rstrip()
        updated = (prefix + "\n\n" if prefix else "") + content.rstrip() + after
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + content
    if updated == existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Back up only when first adopting a file that already had content of its own. Once the
    # block is present the section is regenerated from the hub on every preference change, so
    # backing it up each time would just accumulate copies of generated text.
    if path.exists() and backup and adopting:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, path.with_name(f"{path.name}.bak.{stamp}"))
    path.write_text(updated, encoding="utf-8")


def setup(
    remote: str | None,
    runtime: Path | None = None,
    disable_claude_memory: bool = True,
    *,
    home: Path | None = None,
    local: bool = False,
    profile: str | None = None,
    make_default: bool = False,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Install or refresh one hub profile. Idempotent; returns a summary of what was done."""
    if local and remote:
        raise ValueError("--local and --remote cannot be combined")
    home = (home or default_home()).expanduser()
    profiles = load_profiles(home)
    name = validate_profile_name(profile or profiles.default or "default")
    entry = profiles.profiles.get(name, {})
    runtime = (runtime or Path(entry.get("repo") or profile_runtime(name, home))).expanduser()
    runtime = runtime.resolve()
    remote = None if local else (remote or entry.get("remote"))
    executable = which("agent-hub-mcp")
    if not executable:
        raise RuntimeError(
            "agent-hub-mcp is not installed on PATH; add ~/.local/bin to PATH (uv tool installs "
            "there) and re-run"
        )

    removed: str | None = None
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        if remote:
            runner(["git", "clone", remote, str(runtime)], check=True)
        else:
            _init_local_repo(runtime, runner)
    elif local and has_remote(runtime):
        removed = remote_url(runtime)
        runner(["git", "-C", str(runtime), "remote", "remove", "origin"], check=True)
    elif remote and not has_remote(runtime):
        runner(["git", "-C", str(runtime), "remote", "add", "origin", remote], check=True)
        runner(["git", "-C", str(runtime), "push", "-q", "-u", "origin", "main"], check=True)
    (runtime / ".agent-hub-managed").touch()
    hub = Hub(runtime, profile=name)
    policy = "created" if hub.ensure_policy() else "already-present"

    profiles.profiles[name] = {
        "repo": str(runtime),
        "remote": remote,
        **({"notes_target": entry["notes_target"]} if entry.get("notes_target") else {}),
    }
    if make_default or not profiles.default:
        profiles.default = name
    save_profiles(profiles, home)

    rendered = render_instructions(hub)
    instructions = {
        "codex_agents_md": _merge_and_report(home / ".codex" / "AGENTS.md", rendered),
        "claude_md": _merge_and_report(home / ".claude" / "CLAUDE.md", rendered),
    }
    memory_disabled = disable_claude_memory and _disable_claude_memory(home)

    tools = {
        "codex": _replace_mcp(
            "codex",
            ["codex", "mcp", "add", "agent-hub", "--", executable],
            which=which,
            runner=runner,
        ),
        "claude": _replace_mcp(
            "claude",
            [
                "claude",
                "mcp",
                "add",
                "--transport",
                "stdio",
                "--scope",
                "user",
                "agent-hub",
                "--",
                executable,
            ],
            which=which,
            runner=runner,
        ),
    }
    if tools["codex"] == "configured":
        _ensure_codex_env_vars(home / ".codex" / "config.toml")

    report = doctor(hub, home=home, which=which, runner=runner)
    return {
        "ok": bool(report["ok"]),
        "profile": name,
        "default_profile": profiles.default,
        "runtime": str(runtime),
        "remote": remote,
        "config_path": str(config_path(home)),
        "tools": tools,
        "instructions": instructions,
        "claude_memory_disabled": memory_disabled,
        "policy": policy,
        "doctor": report,
        "scan": hub.scan(),
        "remote_removed": removed,
    }


def _init_local_repo(runtime: Path, runner: Runner) -> None:
    runner(["git", "init", "-q", "-b", "main", str(runtime)], check=True)
    memory = runtime / "memory"
    memory.mkdir()
    (memory / "README.md").write_text(MEMORY_README, encoding="utf-8")
    runner(["git", "-C", str(runtime), "add", "memory"], check=True)
    runner(
        ["git", "-C", str(runtime), "commit", "-q", "-m", "hub: initialize local memory"],
        check=True,
    )


MEMORY_README = """# Agent Hub memory

This directory is the canonical readable state. Do not edit event files or approved plan revisions
in place; use the CLI or MCP tools so concurrent changes are validated and published atomically.

- `knowledge/` contains versioned Markdown entries.
- `plans/` contains immutable YAML plan revisions.
- `events/` contains immutable JSON state transitions.
- `policy/` contains the execution tier policy.
- `workspaces/` describes groups of related projects.

No credentials, `.env` contents, private keys, or raw agent transcripts belong here.
"""


def _merge_and_report(path: Path, content: str = INSTRUCTIONS) -> str:
    before = path.read_bytes() if path.exists() else None
    merge_managed_block(path, content)
    return "unchanged" if path.read_bytes() == before else "updated"


def _disable_claude_memory(home: Path) -> bool:
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if settings.get("autoMemoryEnabled") is False:
        return False
    if settings_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak.{stamp}"))
    settings["autoMemoryEnabled"] = False
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return True


def _replace_mcp(
    tool: str,
    add_command: list[str],
    *,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
) -> str:
    if not which(tool):
        return "skipped-not-installed"
    runner([tool, "mcp", "remove", "agent-hub"], check=False, capture_output=True)
    runner(add_command, check=True)
    return "configured"


def _ensure_codex_env_vars(config: Path) -> bool:
    """Insert or refresh `env_vars` in Codex's `[mcp_servers.agent-hub]` table. Idempotent."""
    if not config.exists():
        return False
    lines = config.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("[mcp_servers.agent-hub]")
    except ValueError:
        return False
    wanted = f"env_vars = {json.dumps(CODEX_FORWARDED_ENV)}"
    end = start + 1
    while end < len(lines) and not lines[end].startswith("["):
        if lines[end].split("=", 1)[0].strip() == "env_vars":
            if lines[end] == wanted:
                return True
            lines[end] = wanted
            config.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
        end += 1
    lines.insert(start + 1, wanted)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
