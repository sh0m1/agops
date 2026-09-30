from pathlib import Path

import pytest

from agops.adapters import install_adapters


def test_adapters_preserve_existing_instructions_and_are_idempotent(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Existing project rules\n", encoding="utf-8")
    first = install_adapters(tmp_path, ["agents", "claude", "gemini", "cursor", "copilot"])
    second = install_adapters(tmp_path, ["agents", "claude", "gemini", "cursor", "copilot"])
    assert first == second
    assert "Existing project rules" in agents.read_text(encoding="utf-8")
    assert agents.read_text(encoding="utf-8").count("BEGIN AGOPS MANAGED") == 1
    assert (tmp_path / "CLAUDE.md").exists()
    assert "alwaysApply: true" in (tmp_path / ".cursor/rules/agops.mdc").read_text()


def test_unknown_adapter_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown adapter"):
        install_adapters(tmp_path, ["imaginary"])
