from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .hub import Hub

MANAGED_START = "<!-- BEGIN AGENT HUB MANAGED -->"
MANAGED_END = "<!-- END AGENT HUB MANAGED -->"
INSTRUCTIONS = f"""{MANAGED_START}
## Shared agops hub

At the start of each work session, call `agops brief --cwd \"$PWD\"` or the MCP
`hub_get_brief` tool. Pass your current model id to `hub_get_brief` (or
`agops brief --model`) at session start and again if the model changes; claims are limited
to tasks matching your tier. Before modifying files for an approved shared plan, claim a ready
task. Checkpoint meaningful progress and before handoff or context compaction. Complete tasks
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


def merge_managed_block(path: Path, content: str = INSTRUCTIONS, backup: bool = True) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if MANAGED_START in existing and MANAGED_END in existing:
        before, rest = existing.split(MANAGED_START, 1)
        _, after = rest.split(MANAGED_END, 1)
        updated = before.rstrip() + "\n\n" + content.rstrip() + after
    else:
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + content
    if updated == existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and backup:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, path.with_name(f"{path.name}.bak.{stamp}"))
    path.write_text(updated, encoding="utf-8")


def setup(remote: str, runtime: Path, disable_claude_memory: bool = True) -> None:
    runtime = runtime.expanduser().resolve()
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", remote, str(runtime)], check=True)
    (runtime / ".agent-hub-managed").touch()
    Hub(runtime).ensure_policy()
    config = Path("~/.config/agent-hub/config.json").expanduser()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"repo": str(runtime), "remote": remote}, indent=2) + "\n")
    merge_managed_block(Path("~/.codex/AGENTS.md").expanduser())
    merge_managed_block(Path("~/.claude/CLAUDE.md").expanduser())
    if disable_claude_memory:
        settings_path = Path("~/.claude/settings.json").expanduser()
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        if settings.get("autoMemoryEnabled") is not False:
            if settings_path.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak.{stamp}"))
            settings["autoMemoryEnabled"] = False
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    executable = shutil.which("agent-hub-mcp")
    if not executable:
        raise RuntimeError("agent-hub-mcp is not installed on PATH")
    _replace_mcp(
        "codex",
        [
            "codex",
            "mcp",
            "add",
            "agent-hub",
            "--env",
            f"AGENT_HUB_REPO={runtime}",
            "--",
            executable,
        ],
    )
    _replace_mcp(
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
            "--env",
            f"AGENT_HUB_REPO={runtime}",
            "--",
            executable,
        ],
    )


def _replace_mcp(tool: str, add_command: list[str]) -> None:
    if not shutil.which(tool):
        return
    subprocess.run([tool, "mcp", "remove", "agent-hub"], check=False, capture_output=True)
    subprocess.run(add_command, check=True)
