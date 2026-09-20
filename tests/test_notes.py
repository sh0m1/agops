from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_hub.hub import Hub
from agent_hub.notes import NotesBridge
from agent_hub.state import load_plan


def _connected(hub: Hub, target: Path) -> NotesBridge:
    bridge = NotesBridge(hub)
    result = bridge.connect(target)
    assert result["connected"] == str(target)
    return bridge


def test_connect_renders_portable_hybrid_notes(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")

    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    text = note.read_text(encoding="utf-8")
    assert "agops_id: shared-plan" in text
    assert "<!-- agops:definition:start -->" in text
    assert "<!-- agops:personal:start -->" in text
    assert "[Shared plan](plans/shared-plan.md)" in (tmp_path / "vault" / "Plans.md").read_text()
    sidecar = json.loads((tmp_path / "vault" / ".agops-notes.json").read_text())
    assert sidecar["format_version"] == 1
    assert bridge.status()["plans"] == [
        {"id": "shared-plan", "status": "clean", "plan_status": "draft"}
    ]


def test_sync_imports_a_note_edit_as_unapproved_revision_and_preserves_personal(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    text = note.read_text(encoding="utf-8").replace(
        "Let agents cooperate.", "Ship a markdown bridge."
    )
    text = text.replace(
        "<!-- agops:personal:start -->", "<!-- agops:personal:start -->\nA private thought."
    )
    note.write_text(text, encoding="utf-8")

    result = bridge.sync("human", "terminal")
    assert result["imported"] == ["shared-plan"]
    current = load_plan(local_hub, "shared-plan")
    assert current["revision"] == 2 and current["goal"] == "Ship a markdown bridge."
    rendered = note.read_text(encoding="utf-8")
    assert "A private thought." in rendered and "agops_latest_revision: 2" in rendered


def test_concurrent_hub_and_note_edits_are_a_conflict(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("Let agents cooperate.", "Note wins?"),
        encoding="utf-8",
    )
    plan = load_plan(local_hub, "shared-plan")
    plan["goal"] = "Hub changed too."
    expected_hash = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]["definition_hash"]
    hub.draft_plan_definition(plan, "codex", "two", 1, expected_hash)

    result = bridge.sync("human", "terminal")
    assert result["conflicts"] == ["shared-plan"]
    assert bridge.status()["plans"][0]["status"] == "conflict"


def test_invalid_note_is_left_untouched_and_disconnect_keeps_files(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    broken = note.read_text(encoding="utf-8").replace("\nid: shared-plan", "\nid: wrong-plan", 1)
    note.write_text(broken, encoding="utf-8")

    assert bridge.status()["plans"][0]["status"] == "invalid"
    bridge.render_all()
    assert note.read_text(encoding="utf-8") == broken
    assert bridge.disconnect()["disconnected"] == str(tmp_path / "vault")
    assert note.exists()


def test_connect_rejects_unmanaged_nonempty_directory(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    (target / "mine.md").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        NotesBridge(Hub(local_hub, profile="default")).connect(target)
