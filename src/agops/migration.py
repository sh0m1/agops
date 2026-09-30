"""One-time migration of a pre-rename install to agops paths and names.

The tool used to be called "agent-hub". `agops setup` calls `migrate` so an existing install
keeps its memory, profiles and preferences; `agops doctor` calls `pending` to report what is left.
Nothing is ever deleted: when both an old and a new path exist, they are not merged and a
warning is reported instead. This is the only module that may mention the legacy names.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

Which = Callable[[str], str | None]
Runner = Callable[..., subprocess.CompletedProcess]

LEGACY = "agent-hub"
CURRENT = "agops"
LEGACY_MARKER = ".agent-hub-managed"
CURRENT_MARKER = ".agops-managed"
LEGACY_BLOCK_WORDS = ("BEGIN AGENT HUB MANAGED", "END AGENT HUB MANAGED")
CURRENT_BLOCK_WORDS = ("BEGIN AGOPS MANAGED", "END AGOPS MANAGED")
INSTRUCTION_FILES = (Path(".codex") / "AGENTS.md", Path(".claude") / "CLAUDE.md")
MCP_TOOLS = ("claude", "codex")

# (kind, relative parent) of the three directories that carry the tool's name.
_DIRS = (
    ("config", Path(".config")),
    ("data", Path(".local") / "share"),
    ("state", Path(".local") / "state"),
)


def _pair(home: Path, parent: Path) -> tuple[Path, Path]:
    return home / parent / LEGACY, home / parent / CURRENT


def _legacy_registered(tool: str, which: Which, runner: Runner) -> bool:
    if not which(tool):
        return False
    result = runner([tool, "mcp", "get", LEGACY], check=False, capture_output=True, text=True)
    return getattr(result, "returncode", 1) == 0


def pending(
    home: Path, *, which: Which = shutil.which, runner: Runner = subprocess.run
) -> list[str]:
    """Human-readable leftovers of the old install; empty when fully migrated."""
    items: list[str] = []
    for kind, parent in _DIRS:
        old, new = _pair(home, parent)
        if old.exists() and new.exists():
            items.append(f"conflict: {old} and {new} both exist; merge by hand, nothing was moved")
        elif old.exists():
            items.append(f"{kind} directory {old} awaits `agops setup` to move it to {new}")
    for tool in MCP_TOOLS:
        if _legacy_registered(tool, which, runner):
            items.append(f"legacy MCP registration in {tool} awaits `agops setup`")
    return items


def _rewrite_config(config_dir: Path, old_share: Path, new_share: Path) -> bool:
    path = config_dir / "config.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    prefix = str(old_share)
    changed = False

    def fix(value: Any) -> Any:
        nonlocal changed
        if isinstance(value, str) and (value == prefix or value.startswith(prefix + "/")):
            changed = True
            return str(new_share) + value[len(prefix) :]
        return value

    if isinstance(data, dict):
        profiles = data.get("profiles")
        entries = list(profiles.values()) if isinstance(profiles, dict) else [data]
        for entry in entries:
            if isinstance(entry, dict) and "repo" in entry:
                entry["repo"] = fix(entry["repo"])
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return changed


def _rename_markers(share: Path) -> list[str]:
    renamed: list[str] = []
    if not share.is_dir():
        return renamed
    for child in sorted(share.iterdir()):
        old, new = child / LEGACY_MARKER, child / CURRENT_MARKER
        if old.exists() and not new.exists():
            old.rename(new)
            renamed.append(str(new))
    return renamed


def _rewrite_instruction_blocks(home: Path) -> list[str]:
    updated: list[str] = []
    for relative in INSTRUCTION_FILES:
        path = home / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        new_text = text
        for old, new in zip(LEGACY_BLOCK_WORDS, CURRENT_BLOCK_WORDS, strict=True):
            new_text = new_text.replace(old, new)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            updated.append(str(path))
    return updated


def migrate(
    home: Path, *, which: Which = shutil.which, runner: Runner = subprocess.run
) -> dict[str, Any]:
    """Move legacy directories, rewrite references, drop the old MCP registration. Idempotent."""
    moved: list[str] = []
    warnings: list[str] = []
    _, new_config = _pair(home, Path(".config"))
    old_share, new_share = _pair(home, Path(".local") / "share")
    for _kind, parent in _DIRS:
        old, new = _pair(home, parent)
        if not old.exists():
            continue
        if new.exists():
            warnings.append(f"{old} and {new} both exist; left both untouched, merge by hand")
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        moved.append(f"{old} -> {new}")
    share_moved = not old_share.exists() and new_share.exists()
    rewritten = share_moved and _rewrite_config(new_config, old_share, new_share)
    markers = _rename_markers(new_share) if share_moved else []
    blocks = _rewrite_instruction_blocks(home)
    mcp_removed = []
    for tool in MCP_TOOLS:
        if _legacy_registered(tool, which, runner):
            runner([tool, "mcp", "remove", LEGACY], check=False, capture_output=True)
            mcp_removed.append(tool)
    return {
        "moved": moved,
        "config_rewritten": bool(rewritten),
        "markers_renamed": markers,
        "instruction_blocks_updated": blocks,
        "mcp_removed": mcp_removed,
        "warnings": warnings,
    }
