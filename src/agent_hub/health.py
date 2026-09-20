from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .git import is_managed_clone, remote_url, run_git
from .hub import Hub
from .policy import PolicyError
from .sessions import default_home

Which = Callable[[str], str | None]
Runner = Callable[..., subprocess.CompletedProcess]

# Keys in a doctor report that describe state rather than pass/fail checks.
INFORMATIONAL = frozenset(
    {"root", "queued_checkpoints", "policy", "remote", "profile", "mcp_pinned", "ok", "notes"}
)
_PINNED_ASSIGNMENT = re.compile(r"^\s*AGENT_HUB_REPO\s*=")


def doctor(
    hub: Hub,
    home: Path | None = None,
    *,
    which: Which = shutil.which,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Read-only health report for a runtime clone and the user's tool configuration."""
    home = home or default_home()
    checks: dict[str, Any] = {"root": str(hub.root), "managed_clone": is_managed_clone(hub.root)}
    checks["profile"] = hub.profile
    status = run_git(hub.root, "status", "--porcelain", "--untracked-files=no").stdout
    checks["git"] = status.strip() == ""
    try:
        checks["policy"] = "ok" if hub.policy() else "absent"
    except PolicyError as exc:
        checks["policy"] = f"invalid: {exc}"
    try:
        checks["scan"] = hub.scan()["errors"] == 0
    except ValueError:
        checks["scan"] = False
    checks["queued_checkpoints"] = len(list(hub._outbox_root().glob("*.json")))
    checks["codex_instructions"] = (home / ".codex" / "AGENTS.md").exists()
    checks["claude_instructions"] = (home / ".claude" / "CLAUDE.md").exists()
    checks["remote"] = remote_url(hub.root)
    checks["mcp_pinned"] = pinned_mcp_registrations(home, which=which, runner=runner)
    try:
        from .notes import NotesBridge

        checks["notes"] = NotesBridge(hub).status()
    except (OSError, ValueError) as exc:
        checks["notes"] = {"status": "invalid", "error": str(exc)}
    checks["ok"] = all(
        value for key, value in checks.items() if key not in INFORMATIONAL
    ) and not str(checks["policy"]).startswith("invalid")
    return checks


def pinned_mcp_registrations(
    home: Path, *, which: Which = shutil.which, runner: Runner = subprocess.run
) -> list[str]:
    """Tools whose agent-hub MCP registration still bakes in AGENT_HUB_REPO (pre-0.6 setups)."""
    pinned: list[str] = []
    if which("claude"):
        result = runner(
            ["claude", "mcp", "get", "agent-hub"], check=False, capture_output=True, text=True
        )
        if "AGENT_HUB_REPO=" in (result.stdout or ""):
            pinned.append("claude")
    if which("codex"):
        config = home / ".codex" / "config.toml"
        if config.exists():
            block = codex_agent_hub_block(config.read_text(encoding="utf-8"))
            # A pinned value is an assignment `AGENT_HUB_REPO = "..."` in the env sub-table;
            # the name also appears inside the `env_vars` forwarding list, which is fine.
            if any(_PINNED_ASSIGNMENT.match(line) for line in block.splitlines()):
                pinned.append("codex")
    return pinned


def codex_agent_hub_block(text: str) -> str:
    """The `[mcp_servers.agent-hub]` table and its sub-tables from a Codex config.toml."""
    lines = text.splitlines()
    try:
        start = lines.index("[mcp_servers.agent-hub]")
    except ValueError:
        return ""
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("[") and not line.startswith("[mcp_servers.agent-hub"):
            break
        block.append(line)
    return "\n".join(block)
