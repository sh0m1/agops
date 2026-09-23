from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from conftest import project as make_project

from agent_hub.config import config_path
from agent_hub.hub import Hub
from agent_hub.notes import (
    PERSONAL_START,
    RETIRE_MIN_AGE_DAYS,
    NotesBridge,
    hub_fingerprint,
    link_plan,
    plan_health,
    plan_workspace,
    project_workspace,
)
from agent_hub.state import PlanState, TaskState, load_plan


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
        note.read_text(encoding="utf-8").replace("tags:\n- agops", "tags: work"),
        encoding="utf-8",
    )
    bridge.render_all(force=True)
    assert "tags:\n- work\n- agops" in note.read_text(encoding="utf-8")


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
    assert "agops_scope: global" in text
    assert "Always keep concrete evidence." in text
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


def test_plan_frontmatter_has_overview_fields_with_unquoted_dates(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.register_project(make_project(tmp_path / "widgets"), workspace="Acme")
    hub.draft_plan(plan_file, "codex", "one")
    hub.approve_plan("shared-plan")
    _connected(hub, tmp_path / "vault")
    text = (tmp_path / "vault" / "plans" / "shared-plan.md").read_text(encoding="utf-8")

    assert "agops_title: Shared plan" in text
    assert "agops_workspace: Acme" in text
    assert "agops_projects:\n- acme-widgets" in text
    assert "agops_health: waiting" in text
    assert "agops_tasks: 2" in text
    assert "agops_tasks_done: 0" in text
    assert "agops_tasks_open: 2" in text
    assert "agops_tasks_blocked: 0" in text

    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(frontmatter["agops_created"], date)
    assert isinstance(frontmatter["agops_last_activity"], date)
    assert "agops_completed" not in frontmatter


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
    assert "[Shared plan](plans/shared-plan.md)" in home_text
    assert "![[agops.base#Active plans]]" in home_text


def test_agops_base_is_written_once_and_left_untouched(
    local_hub: Path, plan_file: Path, fake_home: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub, profile="default")
    hub.draft_plan(plan_file, "codex", "one")
    bridge = _connected(hub, tmp_path / "vault")
    base_path = tmp_path / "vault" / "agops.base"
    assert base_path.exists()
    parsed = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    assert [view["name"] for view in parsed["views"]] == [
        "Active plans",
        "Stalled",
        "By workspace",
        "Recently completed",
        "Knowledge",
        "Retire candidates",
    ]

    base_path.write_text("custom: true\n", encoding="utf-8")
    bridge.render_all(force=True)
    assert base_path.read_text(encoding="utf-8") == "custom: true\n"


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

    bridge = _connected(hub, tmp_path / "vault")
    now = datetime.now(UTC) + timedelta(days=RETIRE_MIN_AGE_DAYS)
    review = bridge.review(now=now)
    verdicts = {entry["key"]: entry["verdict"] for entry in review["knowledge"]}
    assert verdicts["team-decision"] == "keep"
    assert verdicts["progress-note"] == "retire_safe"

    note = tmp_path / "vault" / "knowledge" / "global" / "progress-note.md"
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
