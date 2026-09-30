from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from conftest import project as make_project

from agops.config import config_path
from agops.hub import Hub
from agops.notes import (
    AGOPS_BASE,
    LEGACY_AGOPS_BASE,
    PERSONAL_START,
    RETIRE_MIN_AGE_DAYS,
    NotesBridge,
    hub_fingerprint,
    link_plan,
    plan_health,
    plan_workspace,
    project_workspace,
)
from agops.state import PlanState, TaskState, load_plan


def _connected(hub: Hub, target: Path) -> NotesBridge:
    bridge = NotesBridge(hub)
    result = bridge.connect(target)
    assert result["connected"] == str(target)
    return bridge


def _definition_file(root: Path, plan_id: str = "shared-plan") -> Path:
    return root / "vault" / "plans" / ".definitions" / f"{plan_id}.yaml"


def _frontmatter(text: str) -> dict:
    return yaml.safe_load(text.split("---\n", 2)[1])


def _legacy_note(bridge: NotesBridge, root: Path, goal: str | None = None) -> Path:
    """Rewrite a freshly rendered plan note in the old layout: agops_* keys and a YAML fence."""
    note = root / "vault" / "plans" / "active" / "shared-plan.md"
    definition = _definition_file(root)
    raw = definition.read_text(encoding="utf-8")
    if goal:
        raw = raw.replace("Let agents cooperate.", goal)
    definition.unlink()
    definition.parent.rmdir()
    note.write_text(
        "---\nmine: 1\nagops_id: shared-plan\nagops_status: draft\nagops_tasks: 2\n"
        "tags:\n- agops\n---\n\n# Shared plan\n\n"
        f"<!-- agops:definition:start -->\n```yaml\n{raw}```\n<!-- agops:definition:end -->\n\n"
        "## Agops state\n\n- Status: draft\n\n## Personal notes\n\n"
        f"{PERSONAL_START}\nMy old thought.\n<!-- agops:personal:end -->\n",
        encoding="utf-8",
    )
    return note


def test_connect_renders_portable_hybrid_notes(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")

    note = tmp_path / "vault" / "plans" / "active" / "shared-plan.md"
    text = note.read_text(encoding="utf-8")
    assert "agops_" not in text and "```" not in text
    assert "<!-- agops:personal:start -->" in text
    assert "agops:definition" not in text
    for heading in ("# Shared plan", "**Goal:** Let agents cooperate.", "## Needs you",
                    "## Open (2)", "## Done (0)", "## Acceptance", "## My notes"):
        assert heading in text
    assert "- **tested** The implementation is tested." in text
    assert "- [ ] `first` First task — draft" in text
    assert "- Revision 1 is waiting for approval" in text
    assert "`plans/.definitions/shared-plan.yaml`" in text
    assert "> - write_scope: `src/first/**`" in text
    definition = yaml.safe_load(_definition_file(tmp_path).read_text(encoding="utf-8"))
    assert definition["id"] == "shared-plan" and len(definition["tasks"]) == 2
    assert "revision" not in definition
    plans_text = (tmp_path / "vault" / "Plans.md").read_text()
    assert "[Shared plan](plans/active/shared-plan.md)" in plans_text
    sidecar = json.loads((tmp_path / "vault" / ".agops-notes.json").read_text())
    assert sidecar["format_version"] == 1
    assert sidecar["plans"]["shared-plan"]["latest_revision"] == 1
    assert bridge.status()["plans"] == [
        {"id": "shared-plan", "status": "clean", "plan_status": "draft"}
    ]


def _details_note(local_hub: Path, tmp_path: Path, extra: str, instructions: str) -> str:
    plan = tmp_path / "details.yaml"
    plan.write_text(
        "id: detail-plan\ntitle: Detail plan\ngoal: G\n"
        "scope:\n  projects: [acme-widgets]\n"
        "execution_policy:\n  authority: Full text of authority.\n  coordination: Talk often.\n"
        "design_document: docs/design.md\nmilestones:\n  m1: Ship it\n"
        "acceptance_criteria:\n  - id: ok\n    text: Works.\n"
        "tasks:\n  - id: only\n    title: Only task\n    project: acme-widgets\n"
        f"    depends_on: []\n    write_scope: [src/a/**]\n    acceptance: [Tests pass]\n"
        f"    covers: []\n{extra}    instructions: |\n{instructions}",
        encoding="utf-8",
    )
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan, "codex", "one")
    _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "active" / "detail-plan.md"
    return note.read_text(encoding="utf-8")


def test_plan_details_section_sits_between_acceptance_and_notes(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    text = _details_note(local_hub, tmp_path, "    tier: high\n", "      Do it.\n")
    assert text.index("## Acceptance") < text.index("## Plan details") < text.index("## My notes")
    assert "- **authority:** Full text of authority." in text
    assert "- **projects:** acme-widgets" in text
    assert "- **design_document:** docs/design.md" in text
    assert "  - **m1:** Ship it" in text
    assert "**Revision:** revision 1 · created_at " in text
    assert "> [!todo]- `only` Only task" in text
    assert "> - tier: high" in text and "> - write_scope: `src/a/**`" in text
    assert "> - status: pending" in text


def test_plan_details_do_not_truncate_and_keep_multiline_inside_callout(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    long_line = "word " * 200
    body = f"      {long_line}\n\n      second paragraph\n"
    text = _details_note(local_hub, tmp_path, "", body)
    assert long_line.strip() in text
    section = text.split("### Tasks", 1)[1].split("## My notes", 1)[0]
    assert "\n>\n> second paragraph" in section
    callout = [ln for ln in section.strip().splitlines() if ln]
    assert all(ln.startswith(">") for ln in callout)


def test_plan_details_omit_absent_optional_fields(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    text = _details_note(local_hub, tmp_path, "", "      Do it.\n")
    for absent in ("read_only", "reasoning_effort", "> - model", "> - tier",
                   "review_document", "reviewed_heads", "depends_on", "covers"):
        assert absent not in text


def test_sync_imports_a_note_edit_as_unapproved_revision_and_preserves_personal(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "active" / "shared-plan.md"
    definition = _definition_file(tmp_path)
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "Let agents cooperate.", "Ship a markdown bridge."
        ),
        encoding="utf-8",
    )
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "<!-- agops:personal:start -->", "<!-- agops:personal:start -->\nA private thought."
        ),
        encoding="utf-8",
    )
    assert bridge.status()["plans"][0]["status"] == "edited"

    result = bridge.sync("human", "terminal")
    assert result["imported"] == ["shared-plan"]
    current = load_plan(local_hub, "shared-plan")
    assert current["revision"] == 2 and current["goal"] == "Ship a markdown bridge."
    rendered = note.read_text(encoding="utf-8")
    assert "A private thought." in rendered
    assert "**Goal:** Ship a markdown bridge." in rendered
    assert "Revision 2 is waiting for approval" in rendered
    assert bridge.status()["plans"][0]["status"] == "clean"
    sidecar = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]
    assert sidecar["latest_revision"] == 2


def test_concurrent_hub_and_note_edits_are_a_conflict(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = _definition_file(tmp_path)
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


def test_resolve_settles_a_conflict_in_either_direction(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    definition = _definition_file(tmp_path)

    def conflict(goal: str) -> None:
        definition.write_text(
            definition.read_text(encoding="utf-8").replace(
                load_plan(local_hub, "shared-plan")["goal"], "Note wins?"
            ),
            encoding="utf-8",
        )
        plan = load_plan(local_hub, "shared-plan")
        plan["goal"] = goal
        baseline = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]
        hub.draft_plan_definition(
            plan, "codex", "two", int(baseline["revision"]), baseline["definition_hash"]
        )
        assert bridge.status()["plans"][0]["status"] == "conflict"

    conflict("Hub wins?")
    bridge.resolve("shared-plan", "agops", "human", "terminal")
    assert "Hub wins?" in definition.read_text(encoding="utf-8")
    assert bridge.status()["plans"][0]["status"] == "clean"

    definition.write_text(
        definition.read_text(encoding="utf-8").replace("Hub wins?", "Note wins?"), encoding="utf-8"
    )
    plan = load_plan(local_hub, "shared-plan")
    plan["goal"] = "Hub changed again."
    baseline = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]
    hub.draft_plan_definition(
        plan, "codex", "three", int(baseline["revision"]), baseline["definition_hash"]
    )
    assert bridge.status()["plans"][0]["status"] == "conflict"
    bridge.resolve("shared-plan", "notes", "human", "terminal")
    assert load_plan(local_hub, "shared-plan")["goal"] == "Note wins?"
    assert bridge.status()["plans"][0]["status"] == "clean"


def test_invalid_note_is_left_untouched_and_disconnect_keeps_files(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = _definition_file(tmp_path)
    broken = note.read_text(encoding="utf-8").replace("id: shared-plan", "id: wrong-plan", 1)
    note.write_text(broken, encoding="utf-8")

    assert bridge.status()["plans"][0]["status"] == "invalid"
    result = bridge.render_all()
    assert note.read_text(encoding="utf-8") == broken
    assert any("must match" in warning for warning in result["warnings"])
    assert bridge.disconnect()["disconnected"] == str(tmp_path / "vault")
    assert note.exists()


def test_sync_rejects_an_edited_definition_with_missing_personal_marker(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "active" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("<!-- agops:personal:start -->", ""),
        encoding="utf-8",
    )
    definition = _definition_file(tmp_path)
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "Let agents cooperate.", "This edit must not import."
        ),
        encoding="utf-8",
    )

    result = bridge.sync("human", "terminal")
    assert result["imported"] == []
    assert [item["id"] for item in result["invalid"]] == ["shared-plan"]
    assert "personal-notes" in result["invalid"][0]["error"]
    assert load_plan(local_hub, "shared-plan")["revision"] == 1


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


def test_remote_clones_share_a_notes_fingerprint(
    hub_repo: Path, fake_home: Path, tmp_path: Path
) -> None:
    clone = tmp_path / "second-clone"
    subprocess.run(["git", "clone", str(hub_repo.parent / "origin.git"), str(clone)], check=True)
    first = Hub(hub_repo, profile="default")
    second = Hub(clone, profile="other")
    assert hub_fingerprint(first.root) == hub_fingerprint(second.root)
    target = tmp_path / "vault"
    _connected(first, target)
    assert NotesBridge(second)._sidecar(target)["hub_fingerprint"] == hub_fingerprint(second.root)


def test_connect_rolls_back_profile_config_when_initial_render_fails(
    local_hub: Path, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_: NotesBridge) -> None:
        raise OSError("disk")

    bridge = NotesBridge(Hub(local_hub, profile="default"))
    monkeypatch.setattr(NotesBridge, "render_all", unavailable)
    with pytest.raises(OSError, match="disk"):
        bridge.connect(tmp_path / "vault")
    assert not config_path(fake_home).exists()


def test_scalar_custom_tag_is_preserved_when_agops_tag_is_added(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "active" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("tags:\n- agops\n- agops/plan", "tags: work"),
        encoding="utf-8",
    )
    bridge.render_all(force=True)
    assert "tags:\n- work\n- agops\n- agops/plan" in note.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        (lambda text: text.replace("---\n", "---\n[broken\n", 1), "Invalid frontmatter"),
        (lambda text: text.replace("<!-- agops:personal:start -->", ""), "personal-notes"),
    ],
)
def test_bad_note_wrappers_are_preserved_and_reported_as_warnings(
    local_hub: Path,
    plan_file: Path,
    fake_home: Path,
    tmp_path: Path,
    broken,
    reason: str,
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "active" / "shared-plan.md"
    note.write_text(broken(note.read_text(encoding="utf-8")), encoding="utf-8")
    before = note.read_bytes()
    current = load_plan(local_hub, "shared-plan")
    current["goal"] = "A hub-side update."
    baseline = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]["definition_hash"]
    event = hub.draft_plan_definition(current, "codex", "two", 1, baseline)
    assert reason in event["notes_warning"]
    assert note.read_bytes() == before


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

    text = (tmp_path / "vault" / "plans" / "active" / "shared-plan.md").read_text(encoding="utf-8")
    assert "Revision 2 is waiting for approval" in text
    summary, details = text.split("## Plan details")
    assert "## Open (2)" in summary and "`unapproved`" not in summary
    assert "`unapproved`" in details
    assert "unapproved" in _definition_file(tmp_path).read_text(encoding="utf-8")
    assert bridge.status()["plans"][0]["status"] == "clean"


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
    other = _definition_file(tmp_path, "second-plan")
    other.write_text(
        other.read_text(encoding="utf-8").replace("Let agents cooperate.", "Dirty."),
        encoding="utf-8",
    )
    before = other.read_bytes()
    note_before = (tmp_path / "vault" / "plans" / "active" / "second-plan.md").read_bytes()

    bridge.resolve("shared-plan", "agops", "human", "terminal")
    assert other.read_bytes() == before
    assert (tmp_path / "vault" / "plans" / "active" / "second-plan.md").read_bytes() == note_before


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
    from agops import notes

    def unavailable(_: NotesBridge) -> None:
        raise OSError("disk")

    hub = Hub(local_hub, profile="default")
    _connected(hub, tmp_path / "vault")
    monkeypatch.setattr(notes.NotesBridge, "render_all", unavailable)
    event = hub.draft_plan(plan_file, "codex", "one")
    assert event["notes_warning"] == "Notes refresh failed: disk"
    assert load_plan(local_hub, "shared-plan")["revision"] == 1
    assert hub.sync()["notes_warning"] == "Notes refresh failed: disk"


def test_knowledge_projects_and_activity_are_mirrored(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    hub.add_knowledge(
        "global", "testing", "Testing", "Always keep concrete evidence.", "codex", "one"
    )
    hub.add_knowledge(
        "project:acme-widgets", "deploys", "Deploys", "Deploy from main only.", "codex", "one"
    )
    hub.register_project(make_project(tmp_path / "widgets"))
    bridge = _connected(hub, tmp_path / "vault")
    vault = tmp_path / "vault"

    note = vault / "knowledge" / "global" / "testing.md"
    text = note.read_text(encoding="utf-8")
    assert "agops_" not in text
    front = _frontmatter(text)
    assert list(front) == ["tags", "kind", "scope", "updated"]
    assert front["tags"] == ["agops", "agops/knowledge"]
    assert front["kind"] == "fact" and front["scope"] == "global"
    assert isinstance(front["updated"], date)
    assert "Always keep concrete evidence." in text
    assert text.rstrip().endswith("_Mirrored from agops. Change it with `agops knowledge add`._")
    sidecar = json.loads((vault / ".agops-notes.json").read_text())
    assert sidecar["knowledge"]["knowledge/global/testing.md"]["created_by"] == "codex"
    assert PERSONAL_START in text
    assert (vault / "knowledge" / "project" / "acme-widgets" / "deploys.md").exists()
    index = (vault / "Knowledge.md").read_text(encoding="utf-8")
    assert "[Testing](knowledge/global/testing.md)" in index
    assert "## project:acme-widgets" in index
    projects = (vault / "Projects.md").read_text(encoding="utf-8")
    assert "`acme-widgets`" in projects
    assert "# Activity" in (vault / "Activity.md").read_text(encoding="utf-8")
    assert bridge.status()["mirrored"] == {"knowledge": 2, "projects": 1}


def test_activity_shows_claims_and_blocked_tasks(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    worktree = make_project(tmp_path / "widgets")
    hub.register_project(worktree)
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    hub.claim_task("shared-plan", "first", "codex", "session-one", worktree)
    activity = (tmp_path / "vault" / "Activity.md").read_text(encoding="utf-8")
    assert "`shared-plan` / `first`" in activity
    assert "owner codex" in activity
    assert "`second`" not in activity  # blocked behind its dependency, so not ready yet

    hub.block_task("shared-plan", "first", "codex", "session-one", "Waiting on access.")
    activity = (tmp_path / "vault" / "Activity.md").read_text(encoding="utf-8")
    assert "Waiting on access." in activity


def test_retired_knowledge_leaves_the_mirror_but_personal_notes_survive(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "kept", "Kept", "Body one.", "codex", "one")
    hub.add_knowledge("global", "dropped", "Dropped", "Body two.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    kept = tmp_path / "vault" / "knowledge" / "global" / "kept.md"
    kept.write_text(
        kept.read_text(encoding="utf-8").replace(
            PERSONAL_START, f"{PERSONAL_START}\nMy own annotation."
        ),
        encoding="utf-8",
    )

    hub.retire_knowledge("global", "kept", "Obsolete.", "codex", "two")
    hub.retire_knowledge("global", "dropped", "Obsolete.", "codex", "two")
    result = bridge.render_all()
    assert not (tmp_path / "vault" / "knowledge" / "global" / "dropped.md").exists()
    assert "My own annotation." in kept.read_text(encoding="utf-8")
    assert any("kept.md" in warning for warning in result["warnings"])
    assert result["mirrored"]["knowledge"] == 0


def test_mirrored_knowledge_note_edits_are_not_imported(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "testing", "Testing", "Original body.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "knowledge" / "global" / "testing.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("Original body.", "Rewritten by hand."),
        encoding="utf-8",
    )

    bridge.sync("codex", "two")
    assert "Original body." in note.read_text(encoding="utf-8")
    assert "Rewritten by hand." not in note.read_text(encoding="utf-8")
    entry = next((local_hub / "memory" / "knowledge" / "global" / "testing").glob("*.md"))
    assert "Original body." in entry.read_text(encoding="utf-8")


def test_plan_frontmatter_is_slim_with_unquoted_dates(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"), workspace="Acme")
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    text = (tmp_path / "vault" / "plans" / "active" / "shared-plan.md").read_text(encoding="utf-8")

    front = _frontmatter(text)
    assert list(front) == ["tags", "status", "health", "workspace", "progress", "last_activity"]
    assert front["tags"] == ["agops", "agops/plan"]
    assert front["status"] == "active" and front["health"] == "waiting"
    assert front["workspace"] == "Acme" and front["progress"] == "0/2"
    assert isinstance(front["last_activity"], date)
    assert "agops_" not in text


def test_plan_health_transitions() -> None:
    execution_plan = {"tasks": [{"id": "only"}]}
    now = datetime(2026, 9, 23, tzinfo=UTC)
    recent = now.isoformat().replace("+00:00", "Z")

    draft = PlanState("p")
    assert plan_health(draft, execution_plan, now) == "draft"

    done = PlanState("p", approved_revision=1, completed=True)
    assert plan_health(done, execution_plan, now) == "done"

    live = PlanState("p", approved_revision=1, last_event_at=recent)
    live.tasks["only"] = TaskState(
        status="claimed", lease_until=(now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    assert plan_health(live, execution_plan, now) == "live"

    expired = PlanState("p", approved_revision=1, last_event_at=recent)
    expired.tasks["only"] = TaskState(
        status="claimed", lease_until=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    assert plan_health(expired, execution_plan, now) != "live"

    blocked = PlanState("p", approved_revision=1, last_event_at=recent)
    blocked.tasks["only"] = TaskState(status="blocked")
    assert plan_health(blocked, execution_plan, now) == "blocked"

    stalled_at = (now - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    stalled = PlanState("p", approved_revision=1, last_event_at=stalled_at)
    assert plan_health(stalled, execution_plan, now) == "stalled"


def test_unregistered_sub_project_inherits_parent_workspace() -> None:
    projects = [
        {"id": "quinceai-stack", "workspace": "quince-stack"},
        {"id": "quinceai-stack-extra", "workspace": "other"},
    ]
    assert project_workspace("quinceai-stack-backend", projects) == "quince-stack"
    assert project_workspace("quinceai-stack-extra-ui", projects) == "other"
    assert project_workspace("quinceai-stackish", projects) is None
    plan = {"id": "quince-x", "tasks": [{"id": "a", "project": "quinceai-stack-frontend"}]}
    assert plan_workspace(plan, projects) == "quince-stack"


def test_link_plan_prefers_direct_mention_then_key_core() -> None:
    direct = {"title": "Notes for agops-notes-overview", "body": "", "key": "misc"}
    assert link_plan(direct, ["agops", "agops-notes-overview"]) == "agops-notes-overview"

    by_key = {"title": "Progress", "body": "", "key": "graph-rollout-app-pushed-2026-09-07"}
    assert link_plan(by_key, ["miningvisuals-graph-rollout"]) == "miningvisuals-graph-rollout"

    unrelated = {"title": "Unrelated", "body": "", "key": "totally-unrelated-thing"}
    assert link_plan(unrelated, ["agops-notes-overview"]) is None

    # A key equal to the full plan id (core alone would miss it) still links.
    plans = ["quince-chat-outage-recovery-20260921"]
    full_id_key = {"title": "Progress", "body": "", "key": "quince-chat-outage-recovery-20260921"}
    assert link_plan(full_id_key, plans) == "quince-chat-outage-recovery-20260921"

    suffixed = {"title": "Progress", "body": "", "key": "quince-chat-outage-recovery-foo"}
    assert link_plan(suffixed, plans) == "quince-chat-outage-recovery-20260921"

    # An unrelated key that only shares a shorter prefix must not link.
    not_recovery = {"title": "Progress", "body": "", "key": "quince-chat-outage-20260921"}
    assert link_plan(not_recovery, plans) is None


def test_plans_and_home_show_workspace_health_and_activity(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    widgets = make_project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="Acme")
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    hub.claim_task("shared-plan", "first", "codex", "session-one", widgets)

    plans_text = (tmp_path / "vault" / "Plans.md").read_text(encoding="utf-8")
    assert "### Acme" in plans_text
    assert "· live ·" in plans_text

    home_text = (tmp_path / "vault" / "Home.md").read_text(encoding="utf-8")
    assert "1 active · 1 live" in home_text
    assert "[Shared plan](plans/active/shared-plan.md)" in home_text
    assert "![[agops.base#Active plans]]" in home_text


def test_agops_base_is_written_once_and_left_untouched(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    base_path = tmp_path / "vault" / "agops.base"
    assert base_path.read_text(encoding="utf-8") == AGOPS_BASE
    assert "agops_" not in AGOPS_BASE and 'file.hasTag("agops/plan")' in AGOPS_BASE
    parsed = yaml.safe_load(AGOPS_BASE)
    assert [view["name"] for view in parsed["views"]] == [
        "Active plans",
        "Stalled",
        "By workspace",
        "Recently completed",
        "Knowledge",
        "Plan-linked facts",
    ]

    base_path.write_text("custom: true\n", encoding="utf-8")
    result = bridge.render_all(force=True)
    assert base_path.read_text(encoding="utf-8") == "custom: true\n"
    assert result["warnings"] == []


def test_agops_base_upgrades_only_the_untouched_old_default(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    base_path = tmp_path / "vault" / "agops.base"

    base_path.write_text(LEGACY_AGOPS_BASE, encoding="utf-8")
    assert bridge.render_all()["warnings"] == []
    assert base_path.read_text(encoding="utf-8") == AGOPS_BASE

    customized = LEGACY_AGOPS_BASE.replace("Active plans", "My plans")
    base_path.write_text(customized, encoding="utf-8")
    result = bridge.render_all()
    backup = tmp_path / "vault" / "agops.old.base"
    assert base_path.read_text(encoding="utf-8") == AGOPS_BASE
    assert backup.read_text(encoding="utf-8") == customized
    assert any("agops.old.base" in warning for warning in result["warnings"])

    base_path.write_text(customized, encoding="utf-8")
    result = bridge.render_all()
    assert base_path.read_text(encoding="utf-8") == customized
    assert backup.read_text(encoding="utf-8") == customized
    assert any("update your views" in warning for warning in result["warnings"])


def test_notes_review_verdicts(local_hub: Path, fake_home: Path, tmp_path: Path) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge(
        "global", "team-decision", "Decision", "We chose X.", "codex", "one", kind="decision"
    )

    plan_yaml = tmp_path / "plan.yaml"
    plan_yaml.write_text(
        "id: rollout-demo\n"
        "title: Rollout demo\n"
        "goal: Ship it.\n"
        "tasks:\n"
        "  - id: only\n"
        "    title: Only task\n"
        "    read_only: true\n",
        encoding="utf-8",
    )
    hub.draft_plan(plan_yaml, "codex", "one")
    hub.approve_plan("rollout-demo")
    hub.cancel_plan("rollout-demo", "no longer needed")
    hub.add_knowledge(
        "global",
        "progress-note",
        "Progress",
        "Pushed the change for rollout-demo, ready for the next step.",
        "codex",
        "one",
        kind="fact",
    )
    hub.add_knowledge(
        "global",
        "orphan-fact-2026-01-01",
        "Orphan",
        "A standalone fact tied to no plan.",
        "codex",
        "one",
        kind="fact",
    )

    bridge = _connected(hub, tmp_path / "vault")
    now = datetime.now(UTC) + timedelta(days=RETIRE_MIN_AGE_DAYS)
    review = bridge.review(now=now)
    verdicts = {entry["key"]: entry["verdict"] for entry in review["knowledge"]}
    assert verdicts["team-decision"] == "keep"
    assert verdicts["progress-note"] == "retire_safe"
    assert verdicts["orphan-fact-2026-01-01"] == "review"

    note = tmp_path / "vault" / "knowledge" / "global" / "progress-note.md"
    original_note = note.read_text(encoding="utf-8")

    # `keep: true` overrides retire_safe too.
    note.write_text(original_note.replace("---\n", "---\nkeep: true\n", 1), encoding="utf-8")
    kept_review = bridge.review(now=now)
    kept_verdicts = {entry["key"]: entry["verdict"] for entry in kept_review["knowledge"]}
    assert kept_verdicts["progress-note"] == "keep"
    note.write_text(original_note, encoding="utf-8")

    note.write_text(
        note.read_text(encoding="utf-8").replace(PERSONAL_START, f"{PERSONAL_START}\nKeep this."),
        encoding="utf-8",
    )
    blocked_review = bridge.review(now=now)
    blocked_verdicts = {entry["key"]: entry["verdict"] for entry in blocked_review["knowledge"]}
    assert blocked_verdicts["progress-note"] == "review"

    # A note whose markers were damaged might still hold personal notes: never retire-safe.
    note.write_text("no frontmatter at all", encoding="utf-8")
    damaged = {entry["key"]: entry["verdict"] for entry in bridge.review(now=now)["knowledge"]}
    assert damaged["progress-note"] == "review"

    # `keep: true` in a note's frontmatter blocks review from re-flagging it, and survives a
    # re-render since non-agops_* frontmatter is preserved.
    orphan_note = tmp_path / "vault" / "knowledge" / "global" / "orphan-fact-2026-01-01.md"
    orphan_note.write_text(
        orphan_note.read_text(encoding="utf-8").replace("---\n", "---\nkeep: true\n", 1),
        encoding="utf-8",
    )
    kept_review = bridge.review(now=now)
    kept_entry = next(
        entry for entry in kept_review["knowledge"] if entry["key"] == "orphan-fact-2026-01-01"
    )
    assert kept_entry["verdict"] == "keep"
    assert kept_entry["reason"] == "marked keep in notes"
    assert kept_entry["kept"] is True

    bridge.render_all()
    assert "keep: true" in orphan_note.read_text(encoding="utf-8")
    rerendered = {entry["key"]: entry["verdict"] for entry in bridge.review(now=now)["knowledge"]}
    assert rerendered["orphan-fact-2026-01-01"] == "keep"


def _vault_note(tmp_path: Path, relative: str) -> Path:
    return tmp_path / "vault" / relative


def _add_personal_and_frontmatter(note: Path) -> None:
    text = note.read_text(encoding="utf-8")
    text = text.replace("---\n", "---\nmood: keep-me\n", 1)
    text = text.replace(PERSONAL_START, PERSONAL_START + "\nA private thought.", 1)
    note.write_text(text, encoding="utf-8")


def test_new_plan_renders_under_active_and_is_linked_from_indexes(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    _connected(hub, tmp_path / "vault")
    assert _vault_note(tmp_path, "plans/active/shared-plan.md").is_file()
    assert not _vault_note(tmp_path, "plans/shared-plan.md").exists()
    home = _vault_note(tmp_path, "Home.md").read_text(encoding="utf-8")
    assert "(plans/active/shared-plan.md)" not in home  # drafts are not in Home's active table
    plans_text = _vault_note(tmp_path, "Plans.md").read_text(encoding="utf-8")
    assert "[Shared plan](plans/active/shared-plan.md)" in plans_text
    sidecar = json.loads(_vault_note(tmp_path, ".agops-notes.json").read_text())
    assert sidecar["plans"]["shared-plan"]["path"] == "plans/active/shared-plan.md"


@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
def test_finished_plan_moves_to_archive_preserving_user_content(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path, outcome: str
) -> None:
    hub = Hub(local_hub, profile="default")
    widgets = make_project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="Acme Corp")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    active = _vault_note(tmp_path, "plans/active/shared-plan.md")
    _add_personal_and_frontmatter(active)

    if outcome == "cancelled":
        hub.approve_plan("shared-plan")
        hub.cancel_plan("shared-plan", "not needed")
    else:

        def complete(_):
            event = hub._base_event("plan_completed", "human", "terminal")
            event["plan_id"] = "shared-plan"
            return event, "hub: finish shared-plan"

        hub._mutate(complete)
    result = bridge.render_all()
    workspace = bridge.status()  # status must find the note wherever it lives
    assert workspace["plans"][0]["status"] == "clean"

    archived = next((tmp_path / "vault" / "plans" / "archive").rglob("shared-plan.md"))
    assert archived.parent.parent.name == "archive"
    assert not active.exists()
    assert not (tmp_path / "vault" / "plans" / "active").exists()  # emptied dir pruned
    text = archived.read_text(encoding="utf-8")
    assert "A private thought." in text and "mood: keep-me" in text
    assert _frontmatter(text)["status"] == outcome
    assert _definition_file(tmp_path).is_file()  # the definition does not move with the note
    assert result["warnings"] == []
    rel = archived.relative_to(tmp_path / "vault").as_posix()
    assert f"({rel})" in _vault_note(tmp_path, "Plans.md").read_text(encoding="utf-8")


def test_legacy_flat_note_migrates_on_next_render(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    active = _vault_note(tmp_path, "plans/active/shared-plan.md")
    _add_personal_and_frontmatter(active)
    legacy = _vault_note(tmp_path, "plans/shared-plan.md")
    active.replace(legacy)
    (tmp_path / "vault" / "plans" / "active").rmdir()
    sidecar = json.loads(_vault_note(tmp_path, ".agops-notes.json").read_text())
    del sidecar["plans"]["shared-plan"]["path"]
    _vault_note(tmp_path, ".agops-notes.json").write_text(json.dumps(sidecar), encoding="utf-8")

    assert bridge.status()["plans"][0]["status"] == "clean"
    result = bridge.render_all()
    assert result["warnings"] == []
    assert not legacy.exists() and active.is_file()
    text = active.read_text(encoding="utf-8")
    assert "A private thought." in text and "mood: keep-me" in text


def test_edited_note_is_not_moved_but_still_syncs(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    active = _vault_note(tmp_path, "plans/active/shared-plan.md")
    definition = _definition_file(tmp_path)
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "Let agents cooperate.", "Ship a markdown bridge."
        ),
        encoding="utf-8",
    )
    before = definition.read_bytes()
    hub.approve_plan("shared-plan")
    hub.cancel_plan("shared-plan", "stop")  # desired path becomes archive/...

    result = bridge.render_all()
    assert definition.read_bytes() == before
    assert active.is_file()
    assert not (tmp_path / "vault" / "plans" / "archive").exists()
    assert any("not moved" in warning for warning in result["warnings"])
    assert bridge.status()["plans"][0]["status"] == "edited"


def test_sync_imports_an_edit_from_a_legacy_location(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    active = _vault_note(tmp_path, "plans/active/shared-plan.md")
    definition = _definition_file(tmp_path)
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "Let agents cooperate.", "Ship a markdown bridge."
        ),
        encoding="utf-8",
    )
    elsewhere = _vault_note(tmp_path, "plans/somewhere/shared-plan.md")
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
    active.unlink()

    result = bridge.sync("human", "terminal")
    assert result["imported"] == ["shared-plan"]
    assert load_plan(local_hub, "shared-plan")["goal"] == "Ship a markdown bridge."


def test_home_groups_active_plans_by_workspace(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    second = tmp_path / "second.yaml"
    second.write_text(
        plan_file.read_text(encoding="utf-8")
        .replace("shared-plan", "second-plan")
        .replace("Shared plan", "Second plan")
        .replace("acme-widgets", "acme-gadgets"),
        encoding="utf-8",
    )
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"), workspace="Acme")
    gadgets = make_project(tmp_path / "gadgets", "https://github.com/acme/gadgets.git")
    hub.register_project(gadgets, workspace="Globex")
    hub.draft_plan(plan_file, "codex", "one")
    hub.draft_plan(second, "codex", "one")
    hub.approve_plan("shared-plan")
    hub.approve_plan("second-plan")
    _connected(hub, tmp_path / "vault")
    home = _vault_note(tmp_path, "Home.md").read_text(encoding="utf-8")
    section = home.split("## Active plans")[1].split("## Views")[0]
    assert "### Acme" in section and "### Globex" in section
    assert "(plans/active/second-plan.md)" in section


def test_plan_note_reads_like_a_page(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    widgets = make_project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="Acme")
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    hub.claim_task("shared-plan", "first", "codex", "session-one", widgets)
    hub.checkpoint_task("shared-plan", "first", "codex", "session-one", "Half way " + "x" * 300, [])
    text = _vault_note(tmp_path, "plans/active/shared-plan.md").read_text(encoding="utf-8")

    assert "live · Acme · 0/2 done · last " in text
    assert "## Needs you\n\n_Nothing._" in text
    assert "- [ ] `first` First task — claimed by codex" in text
    assert "- [ ] `second` Second task — waiting on `first`" in text
    summary = next(line for line in text.splitlines() if line.startswith("  - Half way"))
    assert len(summary) <= 165 and summary.endswith("…")
    assert "_None yet._" in text

    hub.block_task("shared-plan", "first", "codex", "session-one", "Waiting on access.")
    text = _vault_note(tmp_path, "plans/active/shared-plan.md").read_text(encoding="utf-8")
    assert "- Blocked: `first` — Waiting on access." in text
    assert "- [ ] `first` First task — blocked: Waiting on access." in text

    hub.claim_task("shared-plan", "first", "codex", "session-one", widgets)
    hub.complete_task("shared-plan", "first", "codex", "session-one", "Done.", ["tests pass"])
    text = _vault_note(tmp_path, "plans/active/shared-plan.md").read_text(encoding="utf-8")
    assert "## Done (1)\n\n> [!done]- 1 tasks\n> - `first` First task" in text
    assert "## Open (1)" in text and "- [ ] `second` Second task — ready" in text


def test_old_format_note_migrates_keeping_personal_notes_and_custom_keys(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = _legacy_note(bridge, tmp_path)
    assert not _definition_file(tmp_path).exists()

    result = bridge.render_all()
    assert result["warnings"] == []
    text = note.read_text(encoding="utf-8")
    front = _frontmatter(text)
    assert front["mine"] == 1
    assert front["tags"] == ["agops", "agops/plan"]
    assert not any(str(key).startswith("agops_") for key in front)
    assert "My old thought." in text and "```" not in text and "agops:definition" not in text
    definition = yaml.safe_load(_definition_file(tmp_path).read_text(encoding="utf-8"))
    assert definition["goal"] == "Let agents cooperate."
    assert bridge.status()["plans"][0]["status"] == "clean"


def test_old_format_note_with_an_unsynced_edit_keeps_it_in_the_definition_file(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = _legacy_note(bridge, tmp_path, goal="Ship a markdown bridge.")
    assert bridge.status()["plans"][0]["status"] == "edited"

    result = bridge.render_all()
    assert result["warnings"] == []
    assert "Ship a markdown bridge." in _definition_file(tmp_path).read_text(encoding="utf-8")
    text = note.read_text(encoding="utf-8")
    assert "**Goal:** Let agents cooperate." in text  # the page still shows the hub's truth
    assert "My old thought." in text and "```" not in text
    assert load_plan(local_hub, "shared-plan")["revision"] == 1
    assert bridge.status()["plans"][0]["status"] == "edited"

    result = bridge.sync("human", "terminal")
    assert result["imported"] == ["shared-plan"]
    assert load_plan(local_hub, "shared-plan")["goal"] == "Ship a markdown bridge."
    assert bridge.status()["plans"][0]["status"] == "clean"


def test_old_format_note_edited_on_both_sides_stays_a_conflict_after_migration(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    _legacy_note(bridge, tmp_path, goal="Note wins?")
    plan = load_plan(local_hub, "shared-plan")
    plan["goal"] = "Hub wins?"
    baseline = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]
    hub.draft_plan_definition(plan, "codex", "two", 1, baseline["definition_hash"])

    bridge.render_all()
    assert "Note wins?" in _definition_file(tmp_path).read_text(encoding="utf-8")
    assert bridge.status()["plans"][0]["status"] == "conflict"


def test_user_keep_and_tags_survive_and_legacy_keys_are_stripped(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "testing", "Testing", "Body.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "knowledge" / "global" / "testing.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "---\n", "---\nkeep: true\nagops_old: 1\ntags: [mine]\n", 1
        ).replace("tags:\n- agops\n- agops/knowledge\n", ""),
        encoding="utf-8",
    )
    bridge.render_all()
    front = _frontmatter(note.read_text(encoding="utf-8"))
    assert front["keep"] is True and "agops_old" not in front
    assert front["tags"] == ["mine", "agops", "agops/knowledge"]
    entry = bridge.review()["knowledge"][0]
    assert entry["kept"] is True and entry["verdict"] == "keep"
