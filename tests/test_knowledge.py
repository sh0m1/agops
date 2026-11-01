from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import project

from agops.hub import BRIEF_MAX_BYTES, KNOWLEDGE_BUDGET, PLANS_BUDGET, Hub
from agops.security import validate_content


def test_knowledge_is_searchable_and_in_brief(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge(
        "global", "testing", "Testing", "Always preserve concrete test evidence.", "codex", "one"
    )
    assert hub.search("concrete evidence")
    # Global facts are titles only in a brief; project facts show their body.
    hub.add_knowledge(
        "project:acme-widgets", "local", "Local", "Keep the widget cache warm.", "codex", "one"
    )
    brief = hub.brief(project(tmp_path / "widgets"))
    assert "- Testing (fact, testing)" in brief
    assert "Always preserve concrete test evidence" not in brief
    assert "- Keep the widget cache warm." in brief


def test_brief_outside_a_project_lists_titles_not_bodies(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge("global", "testing", "Testing", "Keep test evidence.", "codex", "one")
    brief = hub.brief(Path("/tmp"))
    assert "- Testing (fact, testing)" in brief
    assert "Keep test evidence." not in brief


def test_brief_leaves_global_preferences_to_the_instruction_file(
    hub_repo: Path, tmp_path: Path
) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge(
        "global", "style", "Style", "Answer briefly.", "codex", "one", kind="preference"
    )
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="acme")
    hub.add_knowledge(
        "workspace:acme", "local", "Local", "Acme style.", "codex", "one", kind="preference"
    )
    brief = hub.brief(widgets)
    assert "Answer briefly." not in brief
    assert "1 global preference(s) are in your instruction file" in brief
    assert "Acme style." in brief


def test_brief_includes_knowledge_of_the_registered_workspace(
    hub_repo: Path, tmp_path: Path
) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="acme")
    hub.add_knowledge("workspace:acme", "shared", "Shared", "Acme-wide rule.", "codex", "one")
    hub.add_knowledge("workspace:other", "elsewhere", "Elsewhere", "Other rule.", "codex", "one")
    brief = hub.brief(widgets)
    assert "Acme-wide rule." in brief
    assert "Other rule." not in brief
    assert "Acme-wide rule." not in hub.brief(Path("/tmp"))


def test_project_knowledge_survives_workspace_overflow(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="acme")
    for index in range(30):
        hub.add_knowledge("workspace:acme", f"wide-{index}", "Wide", "word " * 90, "codex", "one")
    hub.add_knowledge(
        "project:acme-widgets", "local", "Local", "Widgets-only rule.", "codex", "one"
    )
    brief = hub.brief(widgets)
    assert "Widgets-only rule." in brief
    assert "[brief truncated]" not in brief
    section = brief[brief.index("## Knowledge") :]
    assert section.index("Widgets-only rule.") < section.index("word word")
    assert section.endswith("more entries: agops search <words>\n")
    assert len(section.encode()) <= KNOWLEDGE_BUDGET


def test_brief_still_reads_workspace_definition_files(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets)
    workspaces = hub_repo / "memory" / "workspaces"
    workspaces.mkdir(parents=True)
    (workspaces / "acme.yaml").write_text(
        yaml.safe_dump({"id": "acme", "projects": [{"id": "acme-widgets"}]}), encoding="utf-8"
    )
    hub.add_knowledge("workspace:acme", "shared", "Shared", "Acme-wide rule.", "codex", "one")
    assert "Acme-wide rule." in hub.brief(widgets)


def test_knowledge_revision_requires_explicit_supersession(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    scope = "project:acme-widgets"
    first = hub.add_knowledge(scope, "rule", "Rule", "First version.", "codex", "one")
    entry_id = first["payload"]["id"]
    with pytest.raises(ValueError, match="supersedes"):
        hub.add_knowledge(scope, "rule", "Rule", "Second version.", "claude", "two")
    second = hub.add_knowledge(scope, "rule", "Rule", "Second version.", "claude", "two", entry_id)
    brief = hub.brief(widgets)
    assert "Second version" in brief
    assert "First version" not in brief
    hub.retire_knowledge(scope, "rule", "No longer applies.", "codex", "three")
    assert "Second version" not in hub.brief(widgets)
    assert not hub.search("Second version")
    assert second["payload"]["status"] == "active"


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN " + "PRIVATE KEY-----\nsecret",
        "api_key=abcdefghijklmnopqrstuvwxyz123456",
        "ghp_" + "a" * 30,
    ],
)
def test_secret_like_content_is_rejected(content: str) -> None:
    with pytest.raises(ValueError, match="credential|private key"):
        validate_content(content)


def test_brief_is_bounded(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge("global", "large", "Large", "word " * 3000, "codex", "one")
    assert len(hub.brief(Path("/tmp"), max_bytes=1000).encode()) <= 1000


def test_scan_checks_the_complete_readable_store(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge("global", "safe", "Safe", "Ordinary text.", "codex", "one")
    assert hub.scan()["errors"] == 0


def _plan_with_tasks(
    hub: Hub, tmp_path: Path, plan_id: str, tasks: list[tuple[str, str]], *, name: str = ""
) -> None:
    """Draft and approve a plan whose tasks are read-only (no checkout or scope needed)."""
    rows = "\n".join(
        f'  - {{id: {task_id}, title: "{title}", project: acme-widgets, read_only: true}}'
        for task_id, title in tasks
    )
    source = tmp_path / f"{plan_id}{name}.yaml"
    source.write_text(f"id: {plan_id}\ntitle: Plan {plan_id}\ngoal: g\ntasks:\n{rows}\n", "utf-8")
    hub.draft_plan(source, "codex", "draft")
    hub.approve_plan(plan_id)


def test_brief_lists_open_tasks_only_and_stays_small(local_hub: Path, tmp_path: Path) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    done = [(f"done-{n:02d}", f"Finished step {n}") for n in range(50)]
    openers = [("next-a", "Next A"), ("next-b", "Next B"), ("active", "Active work")]
    _plan_with_tasks(hub, tmp_path, "busy", [*done, *openers])
    for task_id, _ in done:
        hub.claim_task("busy", task_id, "codex", "one", widgets)
        hub.complete_task(
            "busy", task_id, "codex", "one", "FINISHED-SUMMARY " + "long text " * 60, ["tests pass"]
        )
    hub.claim_task("busy", "active", "codex", "one", widgets)
    hub.checkpoint_task("busy", "active", "codex", "one", "Progress\n  note " * 80, [])
    hub.add_knowledge("project:acme-widgets", "local", "Local", "Widgets-only rule.", "c", "1")
    hub.add_knowledge("global", "habit", "Habit", "Global habit body.", "codex", "one")

    brief = hub.brief(widgets)

    assert "[brief truncated]" not in brief
    assert "## Knowledge" in brief
    assert "- busy: Plan busy (1 claimed, 50 completed, 2 ready)" in brief
    assert "done-" not in brief and "FINISHED-SUMMARY" not in brief
    assert "  - next-a: ready — Next A" in brief and "  - active: claimed (codex)" in brief
    latest = [line for line in brief.splitlines() if "Latest:" in line]
    assert len(latest) == 1 and brief.index("active: claimed") < brief.index(latest[0])
    assert latest[0].startswith("    Latest: Progress note Progress note")
    assert len(latest[0]) <= len("    Latest: ") + 300 and latest[0].endswith("…")
    assert len(brief.encode()) < 6_000


def test_brief_shows_latest_for_blocked_tasks_only_besides_claimed(
    local_hub: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    _plan_with_tasks(hub, tmp_path, "mix", [("stuck", "Stuck"), ("idle", "Idle")])
    hub.claim_task("mix", "stuck", "codex", "one", widgets)
    hub.checkpoint_task("mix", "stuck", "codex", "one", "Waiting on review", [])
    hub.block_task("mix", "stuck", "codex", "one", "needs a decision")
    brief = hub.brief(widgets)
    assert "  - stuck: blocked — Stuck\n    Latest: Waiting on review" in brief
    assert "  - idle: ready — Idle\n" in brief
    assert brief.count("Latest:") == 1


def test_brief_closes_the_plans_section_with_a_more_line(local_hub: Path, tmp_path: Path) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    title = "A rather long task title that fills the line " * 2
    tasks = [(f"open-{n:03d}", title) for n in range(120)]
    _plan_with_tasks(hub, tmp_path, "crowded", tasks)
    hub.add_knowledge("project:acme-widgets", "local", "Local", "Widgets-only rule.", "c", "1")

    brief = hub.brief(widgets)

    plans = brief[brief.index("## Active plans") : brief.index("## Knowledge")].rstrip("\n")
    shown = plans.count("  - open-")
    assert 0 < shown < 120
    expected = f"- … {120 - shown} more open task(s): agops task ready --plan crowded"
    assert plans.splitlines()[-1] == expected
    assert len(brief[: brief.index("## Knowledge")].encode()) <= PLANS_BUDGET
    assert "Widgets-only rule." in brief and "[brief truncated]" not in brief
    assert len(brief.encode()) <= BRIEF_MAX_BYTES


def test_brief_names_the_cut_when_several_plans_overflow(local_hub: Path, tmp_path: Path) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    title = "A rather long task title that fills the line " * 2
    for plan_id in ("plan-a", "plan-b"):
        _plan_with_tasks(hub, tmp_path, plan_id, [(f"{plan_id}-{n}", title) for n in range(40)])
    plans = hub.brief(widgets).split("## Knowledge")[0].rstrip("\n").splitlines()
    shown = sum(line.startswith("  - plan-") for line in plans)
    assert (
        plans[-1] == f"- … {80 - shown} more open task(s) in 2 plans: agops task ready --plan <id>"
    )


def test_brief_counts_plan_lines_it_drops_besides_open_tasks(
    local_hub: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    title = "A rather long task title that fills the line " * 2
    _plan_with_tasks(hub, tmp_path, "aaa-big", [(f"open-{n:03d}", title) for n in range(80)])
    # A task of another project keeps this plan active after its own task is completed.
    (tmp_path / "done.yaml").write_text(
        "id: zzz-done\ntitle: Done plan\ngoal: g\ntasks:\n"
        "  - {id: only, title: Only task, project: acme-widgets, read_only: true}\n"
        "  - {id: away, title: Away, project: other-app, read_only: true}\n",
        encoding="utf-8",
    )
    hub.draft_plan(tmp_path / "done.yaml", "codex", "draft")
    hub.approve_plan("zzz-done")
    hub.claim_task("zzz-done", "only", "codex", "one", widgets)
    hub.complete_task("zzz-done", "only", "codex", "one", "Done.", ["tests pass"])
    plans = hub.brief(widgets).split("## Knowledge")[0].rstrip("\n").splitlines()
    shown = sum(line.startswith("  - open-") for line in plans)
    command = "agops task ready --plan aaa-big"
    assert plans[-1] == f"- … {80 - shown} more open task(s) and 1 plan(s): {command}"
    assert not any(line.startswith("- zzz-done") for line in plans)


def test_brief_lists_newest_knowledge_first_inside_each_scope(
    hub_repo: Path, tmp_path: Path
) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    for key in ("m-first", "a-second", "z-third"):
        hub.add_knowledge("project:acme-widgets", key, key, f"Body of {key}.", "codex", "one")
        hub.add_knowledge("global", key, key, "Global body.", "codex", "one")
    section = hub.brief(widgets).split("## Knowledge")[1]
    project_order = [
        section.index(f"- Body of {key}.") for key in ("z-third", "a-second", "m-first")
    ]
    assert project_order == sorted(project_order)
    # The project entries hide the global ones with the same keys; check global separately.
    hub.add_knowledge("global", "b-old", "b-old", "x", "codex", "one")
    hub.add_knowledge("global", "c-new", "c-new", "x", "codex", "one")
    section = hub.brief(widgets).split("## Knowledge")[1]
    assert section.index("- c-new (") < section.index("- b-old (")


def test_brief_orders_equal_timestamps_by_key(
    hub_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agops.hub.timestamp", lambda: "2026-01-01T00:00:00Z")
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    for key in ("b-two", "a-one", "c-three"):
        hub.add_knowledge("project:acme-widgets", key, key, f"Body of {key}.", "codex", "one")
    section = hub.brief(widgets).split("## Knowledge")[1]
    order = [section.index(f"- Body of {key}.") for key in ("a-one", "b-two", "c-three")]
    assert order == sorted(order)


def test_brief_orders_knowledge_project_workspace_global(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="acme")
    hub.add_knowledge("global", "habit", "Habit", "Global habit body.", "codex", "one")
    hub.add_knowledge("workspace:acme", "shared", "Shared", "Workspace body.", "codex", "one")
    hub.add_knowledge("project:acme-widgets", "local", "Local", "Project body.", "codex", "one")
    section = hub.brief(widgets).split("## Knowledge")[1]
    assert section.index("- Project body.") < section.index("- Workspace body.")
    assert section.index("- Workspace body.") < section.index("- Habit (fact, habit)")
    assert "Global habit body." not in section


def test_brief_entry_bodies_are_collapsed_and_cut_at_a_word(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    body = "First line.\n\n  second\tline " + "alpha beta gamma " * 60
    hub.add_knowledge("project:acme-widgets", "long", "Long", body, "codex", "one")
    entry = [line for line in hub.brief(widgets).splitlines() if line.startswith("- First line.")]
    assert len(entry) == 1
    text = entry[0][2:]
    assert text.startswith("First line. second line alpha beta gamma")
    assert len(text) <= 300 and text.endswith(("gamma…", "beta…", "alpha…"))


def test_small_max_bytes_scales_the_budgets_so_every_section_shows(
    local_hub: Path, tmp_path: Path
) -> None:
    hub = Hub(local_hub)
    widgets = project(tmp_path / "widgets")
    _plan_with_tasks(hub, tmp_path, "small", [(f"t-{n}", "Task title " * 5) for n in range(30)])
    for index in range(10):
        hub.add_knowledge(
            "project:acme-widgets", f"k-{index}", "K", "body words " * 40, "codex", "one"
        )
    brief = hub.brief(widgets, max_bytes=3_000)
    assert len(brief.encode()) <= 3_000 and "[brief truncated]" not in brief
    assert "more open task(s)" in brief and "more entries" in brief


def _entries(hub: Hub, count: int, scope: str = "global", word: str = "needle") -> None:
    for index in range(count):
        hub.add_knowledge(scope, f"{word}-{index:02d}", f"Entry {index}", f"{word} text", "c", "1")


def test_search_excerpt_is_the_collapsed_body_without_frontmatter(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    body = "Needle in\n\na   haystack. " + "padding " * 100
    hub.add_knowledge("project:acme-widgets", "hay", "Hay title", body, "codex", "one")
    (hit,) = hub.search("needle")
    assert set(hit) == {"path", "key", "title", "scope", "kind", "excerpt"}
    assert all(isinstance(value, str) for value in hit.values())
    assert (hit["key"], hit["title"], hit["scope"], hit["kind"]) == (
        "hay",
        "Hay title",
        "project/acme-widgets",
        "fact",
    )
    assert hit["path"].startswith("memory/knowledge/project/acme-widgets/hay/")
    assert hit["excerpt"].startswith("Needle in a haystack. padding padding")
    assert len(hit["excerpt"]) <= 300 and "\n" not in hit["excerpt"]
    for leak in ("---", "created_by", "schema", "supersedes"):
        assert leak not in hit["excerpt"]


def test_search_matches_title_key_and_body_but_not_other_frontmatter(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge("global", "plain", "Plain title", "Plain body.", "zebra-bot", "one")
    assert not hub.search("zebra")  # created_by only
    assert not hub.search("supersedes")  # a frontmatter field name
    assert not hub.search("global")  # scope only
    assert hub.search("plain")
    hub.add_knowledge("global", "other", "Findable heading", "Unrelated body.", "codex", "one")
    assert [hit["key"] for hit in hub.search("findable")] == ["other"]
    assert [hit["key"] for hit in hub.search("other")] == ["other"]


def test_search_returns_eight_hits_by_default(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    _entries(hub, 12)
    assert len(hub.search("needle")) == 8
    assert len(hub.search("needle", limit=3)) == 3
    assert len(hub.search("needle", limit=20)) == 12


def test_search_skips_archives_unless_asked(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge("global", "live", "Live", "needle fact", "codex", "one")
    hub.add_knowledge("global", "old", "Old", "needle history", "codex", "one", kind="archive")
    assert [hit["key"] for hit in hub.search("needle")] == ["live"]
    both = hub.search("needle", include_archives=True)
    assert sorted(hit["key"] for hit in both) == ["live", "old"]
    assert [hit["kind"] for hit in both if hit["key"] == "old"] == ["archive"]


def test_search_ranks_entries_for_the_cwd_first(hub_repo: Path, tmp_path: Path) -> None:
    hub = Hub(hub_repo)
    widgets = project(tmp_path / "widgets")
    hub.register_project(widgets, workspace="acme")
    hub.add_knowledge("project:other-app", "other", "Other", "needle " * 9, "codex", "one")
    hub.add_knowledge("workspace:elsewhere", "away", "Away", "needle " * 8, "codex", "one")
    hub.add_knowledge("global", "habit", "Habit", "needle " * 3, "codex", "one")
    hub.add_knowledge("workspace:acme", "shared", "Shared", "needle " * 2, "codex", "one")
    hub.add_knowledge("project:acme-widgets", "local", "Local", "needle", "codex", "one")
    assert [hit["key"] for hit in hub.search("needle")] == [
        "other",
        "away",
        "habit",
        "shared",
        "local",
    ]
    ranked = [hit["key"] for hit in hub.search("needle", cwd=widgets)]
    # Applicable scopes first (score order inside the group), then the rest.
    assert ranked == ["habit", "shared", "local", "other", "away"]
    outside = [hit["key"] for hit in hub.search("needle", cwd=tmp_path)]
    assert outside[0] == "habit"
