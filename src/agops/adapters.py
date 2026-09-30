from __future__ import annotations

from pathlib import Path

from .setup import INSTRUCTIONS, merge_managed_block

ADAPTER_PATHS = {
    "agents": Path("AGENTS.md"),
    "claude": Path("CLAUDE.md"),
    "gemini": Path("GEMINI.md"),
    "copilot": Path(".github/copilot-instructions.md"),
    "cursor": Path(".cursor/rules/agops.mdc"),
}


def install_adapters(root: Path, tools: list[str]) -> list[str]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    unknown = sorted(set(tools) - ADAPTER_PATHS.keys())
    if unknown:
        raise ValueError(f"Unknown adapter(s): {', '.join(unknown)}")
    written: list[str] = []
    for tool in tools:
        relative = ADAPTER_PATHS[tool]
        path = root / relative
        if tool == "cursor" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("---\nalwaysApply: true\n---\n", encoding="utf-8")
        merge_managed_block(path, INSTRUCTIONS, backup=False)
        written.append(str(relative))
    return written
