from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import project as make_project

from agent_hub.config import config_path
from agent_hub.hub import Hub
from agent_hub.notes import PERSONAL_START, NotesBridge, hub_fingerprint
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


def test_sync_rejects_an_edited_definition_with_missing_personal_marker(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8")
        .replace("Let agents cooperate.", "This edit must not import.")
        .replace("<!-- agops:personal:start -->", ""),
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
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
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
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
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


def test_knowledge_and_projects_are_mirrored(
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

    note = vault / "knowledge" / "testing.md"
    text = note.read_text(encoding="utf-8")
    assert "agops_scope: global" in text
    assert "Always keep concrete evidence." in text
    assert PERSONAL_START in text
    assert (vault / "knowledge" / "deploys.md").exists()
    assert [path for path in (vault / "knowledge").iterdir() if path.is_dir()] == []
    index = (vault / "Knowledge.md").read_text(encoding="utf-8")
    assert "[Testing](knowledge/testing.md)" in index
    assert "## project:acme-widgets" in index
    projects = (vault / "Projects.md").read_text(encoding="utf-8")
    assert "`acme-widgets`" in projects
    assert not (vault / "Activity.md").exists()
    assert bridge.status()["mirrored"] == {"knowledge": 2, "projects": 1}


def _section(text: str, heading: str) -> str:
    return text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def test_home_shows_claims_and_blocked_tasks(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    worktree = make_project(tmp_path / "widgets")
    hub.register_project(worktree)
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    hub.claim_task("shared-plan", "first", "codex", "session-one", worktree)
    home = (tmp_path / "vault" / "Home.md").read_text(encoding="utf-8")
    progress = _section(home, "## In progress")
    assert "[Shared plan](plans/shared-plan.md) / `first` — First task" in progress
    assert "owner codex" in progress
    # `second` waits on its dependency, so it is neither in progress nor ready yet.
    assert "`second`" not in progress + _section(home, "## Ready next")

    hub.block_task("shared-plan", "first", "codex", "session-one", "Waiting on access.")
    home = (tmp_path / "vault" / "Home.md").read_text(encoding="utf-8")
    assert "Waiting on access." in _section(home, "## Needs attention")


def test_retired_knowledge_leaves_the_mirror_but_personal_notes_survive(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "kept", "Kept", "Body one.", "codex", "one")
    hub.add_knowledge("global", "dropped", "Dropped", "Body two.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    kept = tmp_path / "vault" / "knowledge" / "kept.md"
    kept.write_text(
        kept.read_text(encoding="utf-8").replace(
            PERSONAL_START, f"{PERSONAL_START}\nMy own annotation."
        ),
        encoding="utf-8",
    )

    hub.retire_knowledge("global", "kept", "Obsolete.", "codex", "two")
    hub.retire_knowledge("global", "dropped", "Obsolete.", "codex", "two")
    result = bridge.render_all()
    assert not (tmp_path / "vault" / "knowledge" / "dropped.md").exists()
    assert "My own annotation." in kept.read_text(encoding="utf-8")
    assert any("kept.md" in warning for warning in result["warnings"])
    assert result["mirrored"]["knowledge"] == 0


def test_mirrored_knowledge_note_edits_are_not_imported(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "testing", "Testing", "Original body.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "knowledge" / "testing.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("Original body.", "Rewritten by hand."),
        encoding="utf-8",
    )

    bridge.sync("codex", "two")
    assert "Original body." in note.read_text(encoding="utf-8")
    assert "Rewritten by hand." not in note.read_text(encoding="utf-8")
    entry = next((local_hub / "memory" / "knowledge" / "global" / "testing").glob("*.md"))
    assert "Original body." in entry.read_text(encoding="utf-8")


def _overview_hub(local_hub: Path, plan_file: Path, tmp_path: Path) -> Hub:
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"))
    hub.draft_plan(plan_file, "codex", "one")
    hub.add_knowledge(
        "project:acme-widgets", "deploys", "Deploys", "Deploy from main only.", "codex", "one"
    )
    hub.add_knowledge(
        "workspace:acme", "shared-plan-review", "Plan review", "Reviewed.", "codex", "one"
    )
    return hub


def test_home_is_an_overview_that_links_plans_projects_and_knowledge(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = _overview_hub(local_hub, plan_file, tmp_path)
    _connected(hub, tmp_path / "vault")
    vault = tmp_path / "vault"

    home = (vault / "Home.md").read_text(encoding="utf-8")
    assert home.startswith("# Overview\n\n1 plans (1 draft)")
    assert "[Shared plan](plans/shared-plan.md) is a draft; approve it" in home
    assert (
        "| [Shared plan](plans/shared-plan.md) | draft | 0/2 "
        "| [widgets](projects/acme-widgets.md) |" in home
    )
    assert "[Deploys](knowledge/deploys.md) · fact · [widgets]" in home
    assert (
        "- [widgets](projects/acme-widgets.md) — 2 open of 2 plan tasks · 1 knowledge entry"
        in home
    )
    assert "[Plans](views/Plans.base)" in home

    plan = (vault / "plans" / "shared-plan.md").read_text(encoding="utf-8")
    assert "- `first`: planned — First task" in plan  # a draft's tasks are not claimable
    assert "  - [widgets](../projects/acme-widgets.md) · after `first`" in plan
    assert "## Related knowledge" in plan
    assert "[Plan review](../knowledge/shared-plan-review.md)" in plan
    assert "agops_tasks_total: 2" in plan and "- agops/plan" in plan

    project = (vault / "projects" / "acme-widgets.md").read_text(encoding="utf-8")
    assert "# widgets" in project and "aliases:\n- widgets" in project
    assert "- Repository: https://github.com/acme/widgets" in project
    assert "[Shared plan](../plans/shared-plan.md) · `second` — planned — Second task" in project
    assert "[Deploys](../knowledge/deploys.md)" in project
    assert PERSONAL_START in project

    knowledge = (vault / "knowledge" / "deploys.md").read_text(encoding="utf-8")
    assert "- Scope: project [widgets](../projects/acme-widgets.md)" in knowledge
    assert "- Related plans: [Shared plan](../plans/shared-plan.md)" in knowledge
    assert "### With tracked work" in (vault / "Projects.md").read_text(encoding="utf-8")


def test_active_plan_tasks_show_ready_and_waiting(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = _overview_hub(local_hub, plan_file, tmp_path)
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    plan = (tmp_path / "vault" / "plans" / "shared-plan.md").read_text(encoding="utf-8")
    assert "- `first`: ready — First task" in plan
    assert "- `second`: waiting — Second task" in plan
    home = (tmp_path / "vault" / "Home.md").read_text(encoding="utf-8")
    assert "_Nothing needs attention._" in home
    assert "- [Shared plan](plans/shared-plan.md) / `first` — First task · [widgets]" in home


def test_unsynced_plan_edit_is_flagged_on_home(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = _overview_hub(local_hub, plan_file, tmp_path)
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace("Let agents cooperate.", "Edited."),
        encoding="utf-8",
    )
    bridge.render_all()
    home = (tmp_path / "vault" / "Home.md").read_text(encoding="utf-8")
    assert "`plans/shared-plan.md` has definition edits that are not in the hub yet" in home


def test_unchanged_notes_are_not_rewritten(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = _overview_hub(local_hub, plan_file, tmp_path)
    bridge = _connected(hub, tmp_path / "vault")
    vault = tmp_path / "vault"
    notes = [path for path in vault.rglob("*") if path.suffix in {".md", ".base"}]
    before = {path: path.stat().st_mtime_ns for path in notes}
    assert bridge.render_all()["mirrored"]["written"] == 0
    after = {path: path.stat().st_mtime_ns for path in before}
    assert after == before


def test_views_are_seeded_and_customized_views_are_kept(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    import yaml

    from agent_hub.notes import VIEWS

    hub = _overview_hub(local_hub, plan_file, tmp_path)
    bridge = _connected(hub, tmp_path / "vault")
    views = tmp_path / "vault" / "views"
    assert sorted(path.name for path in views.iterdir()) == [
        "Knowledge.base", "Plans.base", "Projects.base"
    ]
    for name, content in VIEWS.items():
        parsed = yaml.safe_load(content)
        assert parsed["views"] and parsed["filters"], name
        for view in parsed["views"]:
            assert {"type", "name", "order"} <= set(view), (name, view.get("name"))

    custom = views / "Plans.base"
    custom.write_text(custom.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    bridge.render_all()
    assert custom.read_text(encoding="utf-8").endswith("# mine\n")
    custom.unlink()
    bridge.render_all()
    assert custom.read_text(encoding="utf-8") == VIEWS["Plans"]


def test_agops_aliases_follow_the_title_and_user_aliases_survive(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = _overview_hub(local_hub, plan_file, tmp_path)
    bridge = _connected(hub, tmp_path / "vault")
    note = tmp_path / "vault" / "plans" / "shared-plan.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "\naliases:\n- Shared plan", "\naliases:\n- Shared plan\n- My nickname"
        ),
        encoding="utf-8",
    )
    current = load_plan(local_hub, "shared-plan")
    current["title"] = "Renamed plan"
    baseline = bridge._sidecar(tmp_path / "vault")["plans"]["shared-plan"]["definition_hash"]
    hub.draft_plan_definition(current, "codex", "two", 1, baseline)
    text = note.read_text(encoding="utf-8")
    assert "aliases:\n- My nickname\n- Renamed plan" in text
    assert "- Shared plan" not in text


def test_only_projects_with_tracked_work_or_personal_notes_get_a_note(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"))
    hub.register_project(
        make_project(tmp_path / "gadgets", "https://github.com/acme/gadgets.git")
    )
    hub.add_knowledge(
        "project:acme-widgets", "deploys", "Deploys", "Deploy from main only.", "codex", "one"
    )
    bridge = _connected(hub, tmp_path / "vault")
    vault = tmp_path / "vault"
    assert not (vault / "projects" / "acme-gadgets.md").exists()
    projects = (vault / "Projects.md").read_text(encoding="utf-8")
    assert "- gadgets `acme-gadgets`" in projects  # listed, but no note to link to

    kept = vault / "projects" / "acme-widgets.md"
    kept.write_text(
        kept.read_text(encoding="utf-8").replace(PERSONAL_START, f"{PERSONAL_START}\nMine."),
        encoding="utf-8",
    )
    hub.retire_knowledge("project:acme-widgets", "deploys", "Obsolete.", "codex", "two")
    result = bridge.render_all()
    text = kept.read_text(encoding="utf-8")
    assert "Mine." in text and "agops_knowledge: 0" in text  # kept and still refreshed
    assert result["warnings"] == []
    assert "- [widgets](projects/acme-widgets.md) `acme-widgets`" in (
        vault / "Projects.md"
    ).read_text(encoding="utf-8")


def test_unregistered_project_note_is_pruned_unless_it_has_personal_notes(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"))
    hub.register_project(
        make_project(tmp_path / "gadgets", "https://github.com/acme/gadgets.git")
    )
    hub.add_knowledge("project:acme-widgets", "a", "A", "Body.", "codex", "one")
    hub.add_knowledge("project:acme-gadgets", "b", "B", "Body.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    kept = tmp_path / "vault" / "projects" / "acme-widgets.md"
    kept.write_text(
        kept.read_text(encoding="utf-8").replace(PERSONAL_START, f"{PERSONAL_START}\nMine."),
        encoding="utf-8",
    )
    for path in (local_hub / "memory" / "projects").glob("*.yaml"):
        path.unlink()
    result = bridge.render_all()
    assert not (tmp_path / "vault" / "projects" / "acme-gadgets.md").exists()
    assert "Mine." in kept.read_text(encoding="utf-8")
    assert any("project unregistered" in warning for warning in result["warnings"])


def test_nested_knowledge_notes_move_to_the_flat_folder_with_personal_notes(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.add_knowledge("global", "testing", "Testing", "Keep evidence.", "codex", "one")
    hub.add_knowledge("workspace:acme", "review", "Review", "Reviewed.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    vault = tmp_path / "vault"
    # Recreate what an earlier version left behind: nested notes, an old-style sidecar,
    # a retired entry's empty folder, and the Activity index.
    sidecar = bridge._sidecar(vault)
    for key, scope_path in (("testing", "global"), ("review", "workspace/acme")):
        flat = vault / "knowledge" / f"{key}.md"
        nested = vault / "knowledge" / scope_path / f"{key}.md"
        nested.parent.mkdir(parents=True)
        text = flat.read_text(encoding="utf-8")
        nested.write_text(
            text.replace(PERSONAL_START, f"{PERSONAL_START}\nMy {key} note."), encoding="utf-8"
        )
        flat.unlink()
        record = sidecar["knowledge"].pop(f"knowledge/{key}.md")
        sidecar["knowledge"][f"knowledge/{scope_path}/{key}.md"] = {
            "id": record["id"], "revisions": record["revisions"]
        }
    bridge._write_sidecar(vault, sidecar)
    (vault / "knowledge" / "project" / "gone").mkdir(parents=True)
    (vault / "knowledge" / "project" / "gone" / ".DS_Store").write_bytes(b"")
    (vault / "knowledge" / "mine").mkdir()
    (vault / "knowledge" / "mine" / "keep.md").write_text("Mine.\n", encoding="utf-8")
    (vault / "Activity.md").write_text(
        "# Activity\n\n_Back to [Home](Home.md)._\n\n## Claimed tasks\n", encoding="utf-8"
    )

    result = bridge.render_all()
    assert result["warnings"] == []
    assert "My testing note." in (vault / "knowledge" / "testing.md").read_text(encoding="utf-8")
    assert "My review note." in (vault / "knowledge" / "review.md").read_text(encoding="utf-8")
    folders = sorted(path.name for path in (vault / "knowledge").iterdir() if path.is_dir())
    assert folders == ["mine"]  # agops folders are gone; a folder with a user file stays
    assert not (vault / "Activity.md").exists()
    assert set(bridge._sidecar(vault)["knowledge"]) == {
        "knowledge/testing.md", "knowledge/review.md"
    }


def test_knowledge_names_stay_unique_and_notes_follow_a_rename(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    hub.add_knowledge("global", "deploys", "Deploys", "Global rule.", "codex", "one")
    hub.add_knowledge("workspace:acme", "shared-plan", "Plan notes", "About it.", "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    knowledge = tmp_path / "vault" / "knowledge"
    assert (knowledge / "deploys.md").exists()
    assert (knowledge / "shared-plan--workspace-acme.md").exists()  # the plan owns the name
    note = knowledge / "deploys.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(PERSONAL_START, f"{PERSONAL_START}\nMine."),
        encoding="utf-8",
    )

    hub.add_knowledge(
        "project:acme-widgets", "deploys", "Deploys", "Deploy from main only.", "codex", "two"
    )
    bridge.render_all()
    assert not note.exists()
    assert "Mine." in (knowledge / "deploys--global.md").read_text(encoding="utf-8")
    assert (knowledge / "deploys--project-acme-widgets.md").exists()


def test_hand_written_activity_note_is_left_alone(
    local_hub: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    bridge = _connected(hub, tmp_path / "vault")
    mine = tmp_path / "vault" / "Activity.md"
    mine.write_text("# Activity\n\nMy own log.\n", encoding="utf-8")
    bridge.render_all()
    assert mine.read_text(encoding="utf-8") == "# Activity\n\nMy own log.\n"
