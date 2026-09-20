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
    before = note.read_bytes()
    plan = load_plan(local_hub, "shared-plan")
    plan["goal"] = "Hub changed too."
    expected_hash = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]["definition_hash"]
    hub.draft_plan_definition(plan, "codex", "two", 1, expected_hash)

    assert note.read_bytes() == before
    result = bridge.sync("human", "terminal")
    assert result["conflicts"] == ["shared-plan"]
    assert bridge.status()["plans"][0]["status"] == "conflict"
    assert note.read_bytes() == before


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


def test_connect_rejects_another_hubs_sidecar_without_changing_config(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    (target / ".agops-notes.json").write_text(
        json.dumps({"format_version": 1, "hub_fingerprint": "another-hub", "plans": {}}),
        encoding="utf-8",
    )
    bridge = NotesBridge(Hub(local_hub, profile="default"))
    with pytest.raises(ValueError, match="different hub"):
        bridge.connect(target)
    assert bridge.target() is None


def test_pending_definition_displays_only_approved_execution_tasks(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    bridge = _connected(hub, tmp_path / "vault")
    current = load_plan(local_hub, "shared-plan")
    current["tasks"].append(
        {"id": "unapproved", "title": "Not executable", "project": "acme-widgets"}
    )
    expected_hash = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]["definition_hash"]
    hub.draft_plan_definition(current, "codex", "two", 1, expected_hash)

    text = (tmp_path / "vault" / "plans" / "shared-plan.md").read_text(encoding="utf-8")
    assert "Execution tasks (approved revision 1)" in text
    assert "pending approval; its new tasks are not executable" in text
    assert "`unapproved`:" not in text


def test_resolve_only_writes_its_selected_note(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    second = tmp_path / "second.yaml"
    second.write_text(
        plan_file.read_text(encoding="utf-8")
        .replace("shared-plan", "second-plan")
        .replace("Shared plan", "Second plan"),
        encoding="utf-8",
    )
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    hub.draft_plan(second, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    other = tmp_path / "vault" / "plans" / "second-plan.md"
    other.write_text(
        other.read_text(encoding="utf-8") + "\nunaltered dirty text\n", encoding="utf-8"
    )
    before = other.read_bytes()

    bridge.resolve("shared-plan", "agops", "human", "terminal")
    assert other.read_bytes() == before


def test_resolve_notes_rejects_finished_plans_and_clean_notes(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    clean = bridge.resolve("shared-plan", "notes", "human", "terminal")
    assert clean["imported"] is False
    assert load_plan(local_hub, "shared-plan")["revision"] == 1

    def complete(_):
        event = hub._base_event("plan_completed", "human", "terminal")
        event["plan_id"] = "shared-plan"
        return event, "hub: finish shared-plan"

    hub._mutate(complete)
    with pytest.raises(ValueError, match="Cannot import notes for completed"):
        bridge.resolve("shared-plan", "notes", "human", "terminal")


def test_hub_returns_notes_warnings_after_a_durable_event(
    local_hub: Path,
    plan_file: Path,
    fake_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub import notes

    def unavailable(_: NotesBridge) -> None:
        raise OSError("disk")

    hub = Hub(local_hub, profile="default")
    _connected(hub, tmp_path / "vault")
    monkeypatch.setattr(notes.NotesBridge, "render_all", unavailable)
    event = hub.draft_plan(plan_file, "codex", "one")
    assert event["notes_warning"] == "Notes refresh failed: disk"
    assert load_plan(local_hub, "shared-plan")["revision"] == 1
    assert hub.sync()["notes_warning"] == "Notes refresh failed: disk"
