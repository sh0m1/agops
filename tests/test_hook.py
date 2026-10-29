from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "agops-hook.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def prompt_context(loaded: str, preferences: list[dict], tmp_path: Path) -> str | None:
    """Run the hook's UserPromptSubmit path with stubbed hub and CLAUDE.md content."""
    script = f"""
const {{ evaluateHook }} = await import({json.dumps(HOOK.as_uri())});
let state = null;
const result = evaluateHook(
  {{ session_id: 's', hook_event_name: 'UserPromptSubmit', cwd: {json.dumps(str(tmp_path))} }},
  {{
    projects: [],
    loadState: () => state,
    saveState: (_, next) => {{ state = next; }},
    repositoryFingerprint: () => null,
    hubHead: () => null,
    instructionText: () => {json.dumps(loaded)},
    activePreferences: () => {json.dumps(preferences)},
  }},
);
process.stdout.write(JSON.stringify(result));
"""
    output = subprocess.run(
        [shutil.which("node") or "node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    ).stdout
    return json.loads(output).get("hookSpecificOutput", {}).get("additionalContext")


def test_prompt_hook_skips_preferences_already_in_claude_md(tmp_path: Path) -> None:
    preferences = [
        {"key": "a", "scope": "global", "body": "Answer\n  briefly."},
        {"key": "b", "scope": "global", "body": "Run the tests."},
    ]
    context = prompt_context("## Standing preferences - Answer briefly.", preferences, tmp_path)
    assert context is not None
    assert "Run the tests." in context
    assert "Answer" not in context


def test_prompt_hook_is_silent_when_claude_md_has_every_preference(tmp_path: Path) -> None:
    preferences = [{"key": "a", "scope": "global", "body": "Answer briefly."}]
    assert prompt_context("- Answer briefly.", preferences, tmp_path) is None
