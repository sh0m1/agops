from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import project

from agops import cli, mcp_server, views
from agops.hub import Hub
from agops.sessions import load_record, record_session

GOAL = (
    "Make the example project ship faster. "
    "This goal is deliberately long and spread over several lines.\n"
    + " ".join(f"Detail number {n} explains one more point." for n in range(20))
)
PLAN = f"""id: plan-a
title: Compact output plan
goal: {json.dumps(GOAL)}
acceptance_criteria:
  - id: done
    text: Everything is done.
tasks:
  - id: task-1
    title: First task
    project: acme-widgets
    write_scope: [a/**]
    acceptance: [First check passes, "First   docs\\nare updated"]
    covers: []
  - id: task-2
    title: Second task
    project: acme-widgets
    depends_on: [task-1]
    write_scope: [b/**]
    tier: frontier
    acceptance: [Second check passes]
    covers: [done]
"""


def _args(hub_repo: Path, *argv: str, as_json: bool = False):
    prefix = ["--json"] if as_json else []
    return cli.build_parser().parse_args(["--repo", str(hub_repo), *prefix, *argv])


def _text(hub_repo: Path, *argv: str):
    return cli.dispatch(_args(hub_repo, *argv))


def _json(hub_repo: Path, *argv: str):
    return cli.dispatch(_args(hub_repo, *argv, as_json=True))


@pytest.fixture
def active_hub(policy_hub: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("AGOPS_SESSION", raising=False)
    monkeypatch.delenv("AGOPS_ACTOR", raising=False)
    source = tmp_path / "plan-a.yaml"
    source.write_text(PLAN, encoding="utf-8")
    hub = Hub(policy_hub)
    hub.draft_plan(source, "codex", "draft")
    hub.approve_plan("plan-a")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    return policy_hub


def _claim_args(tmp_path: Path) -> list[str]:
    worktree = tmp_path / "work"
    if not worktree.exists():
        project(worktree)
    return ["--actor", "codex", "--session", "one", "--cwd", str(worktree)]


# --- session --model ---------------------------------------------------------------------


def test_session_model_declares_then_claim_works_without_a_brief(
    active_hub: Path, tmp_path: Path
) -> None:
    out = _text(
        active_hub,
        "session",
        "--model",
        "claude-sonnet-5",
        "--session",
        "declared",
        "--actor",
        "codex",
    )
    assert out == "session declared · claude-sonnet-5 · tier=standard"
    record = load_record("declared")
    assert record is not None and (record.model, record.tier) == ("claude-sonnet-5", "standard")
    claim = _text(
        active_hub,
        "task",
        "claim",
        "plan-a",
        "task-1",
        "--actor",
        "codex",
        "--session",
        "declared",
        "--cwd",
        str(project(tmp_path / "work")),
    )
    assert claim.startswith("claimed plan-a/task-1 · lease until ")


def test_session_model_json_and_environment_defaults(
    active_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGOPS_SESSION", "from-env")
    monkeypatch.setenv("AGOPS_ACTOR", "claude-sonnet-5")
    result = _json(active_hub, "session", "--model", "claude-opus-5")
    assert result == {
        "session": "from-env",
        "actor": "claude-sonnet-5",
        "model": "claude-opus-5",
        "tier": "frontier",
    }
    assert load_record("from-env").tier == "frontier"  # type: ignore[union-attr]


def test_session_model_without_a_session_makes_a_new_id(
    active_hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _json(active_hub, "session", "--model", "claude-sonnet-5")
    second = _json(active_hub, "session", "--model", "claude-sonnet-5")
    assert first["session"] != second["session"]
    assert load_record(first["session"]) is not None
    assert cli.dispatch(_args(active_hub, "session", "--model", "claude-sonnet-5", "--value"))


def test_session_model_unmapped_records_and_warns(active_hub: Path) -> None:
    text = _text(active_hub, "session", "--model", "mystery-model-1", "--session", "odd")
    assert text.splitlines()[0] == "session odd · mystery-model-1 · tier=unknown"
    assert "Warning: model mystery-model-1 is not mapped" in text
    record = load_record("odd")
    assert record is not None and record.tier == "unknown"
    assert "not mapped" in _json(active_hub, "session", "--model", "mystery-model-1")["warning"]


def test_session_without_model_keeps_the_old_behavior(active_hub: Path) -> None:
    result = _text(active_hub, "session")
    assert set(result) == {"actor", "session"} and result["actor"] == "agent"
    assert len(_text(active_hub, "session", "--value")) == 36


# --- task commands -----------------------------------------------------------------------


def test_task_ready_is_one_line_per_task_and_json_stays_full(active_hub: Path) -> None:
    assert _text(active_hub, "task", "ready") == "plan-a/task-1 · standard · First task"
    full = _json(active_hub, "task", "ready")
    assert [task["id"] for task in full] == ["task-1"]
    assert full[0]["acceptance"][0] == "First check passes"
    assert _text(active_hub, "task", "ready", "--tier", "frontier") == "No ready tasks."
    assert _text(active_hub, "task", "ready", "--plan", "no-such-plan") == "No ready tasks."


def test_task_commands_print_one_line_and_json_keeps_the_event(
    active_hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    who = _claim_args(tmp_path)
    claim = _text(active_hub, "task", "claim", "plan-a", "task-1", *who)
    assert claim.startswith("claimed plan-a/task-1 · lease until 20")
    assert "." not in claim.split("lease until ")[1]  # no fractional seconds
    assert "\n" not in claim
    # A notes refresh warning goes to stderr so stdout stays one line.
    assert "warning: Notes refresh failed" in capsys.readouterr().err

    note = _text(
        active_hub,
        "task",
        "checkpoint",
        "plan-a",
        "task-1",
        "--summary",
        "Halfway",
        "--evidence",
        "ref",
        *who[:4],
    )
    assert note == "checkpointed plan-a/task-1"
    assert _text(
        active_hub, "task", "block", "plan-a", "task-1", "--reason", "Stuck", *who[:4]
    ) == ("blocked plan-a/task-1")
    assert _text(
        active_hub, "task", "unblock", "plan-a", "task-1", "--resolution", "Fixed", *who[:4]
    ) == ("unblocked plan-a/task-1")
    _text(active_hub, "task", "claim", "plan-a", "task-1", *who)
    assert _text(active_hub, "task", "release", "plan-a", "task-1", *who[:4]) == (
        "released plan-a/task-1"
    )

    _text(active_hub, "task", "claim", "plan-a", "task-1", *who)
    done = _text(
        active_hub,
        "task",
        "complete",
        "plan-a",
        "task-1",
        "--summary",
        "Done",
        "--evidence",
        "commit abc",
        *who[:4],
    )
    assert done == "completed plan-a/task-1"


def test_task_json_output_is_the_full_event(active_hub: Path, tmp_path: Path) -> None:
    who = _claim_args(tmp_path)
    event = _json(active_hub, "task", "claim", "plan-a", "task-1", *who)
    assert event["type"] == "task_claimed"
    assert event["payload"]["tier"] == "standard" and event["session"] == "one"
    checkpoint = _json(
        active_hub, "task", "checkpoint", "plan-a", "task-1", "--summary", "x", *who[:4]
    )
    assert checkpoint["type"] == "task_checkpoint"
    assert checkpoint["payload"]["summary"] == "x"
    done = _json(active_hub, "task", "complete", "plan-a", "task-1", "--evidence", "e", *who[:4])
    assert done["type"] == "task_completed"


def test_main_prints_text_and_json_for_the_same_command(
    active_hub: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["agops", "--repo", str(active_hub), "task", "ready"])
    cli.main()
    assert capsys.readouterr().out == "plan-a/task-1 · standard · First task\n"
    monkeypatch.setattr("sys.argv", ["agops", "--repo", str(active_hub), "--json", "task", "ready"])
    cli.main()
    assert json.loads(capsys.readouterr().out)[0]["id"] == "task-1"


# --- plan show ---------------------------------------------------------------------------


def test_plan_show_default_is_compact_text(active_hub: Path) -> None:
    out = _text(active_hub, "plan", "show", "plan-a")
    lines = out.splitlines()
    assert lines[0] == "plan-a · Compact output plan"
    goal = lines[1]
    assert goal.startswith("Goal: Make the example project ship faster. This goal is")
    assert len(goal) - len("Goal: ") <= 300 and goal.endswith("…") and "\n" not in goal
    assert lines[2:] == [
        "- task-1 [ready, standard] First task",
        "    - First check passes",
        "    - First docs are updated",
        "- task-2 [ready, frontier] Second task",
        "    - Second check passes",
    ]
    assert len(out) < len(json.dumps(_json(active_hub, "plan", "show", "plan-a"))) / 2


def test_plan_show_drops_acceptance_of_completed_tasks_and_reports_status(
    active_hub: Path, tmp_path: Path
) -> None:
    who = _claim_args(tmp_path)
    _text(active_hub, "task", "claim", "plan-a", "task-1", *who)
    claimed = _text(active_hub, "plan", "show", "plan-a")
    assert "- task-1 [claimed, standard] First task" in claimed
    _text(active_hub, "task", "complete", "plan-a", "task-1", "--evidence", "e", *who[:4])
    done = _text(active_hub, "plan", "show", "plan-a").splitlines()
    assert done[2:] == [
        "- task-1 [completed, standard] First task",
        "- task-2 [ready, frontier] Second task",
        "    - Second check passes",
    ]


def test_plan_show_task_prints_every_field_and_the_latest_summary(
    active_hub: Path, tmp_path: Path
) -> None:
    who = _claim_args(tmp_path)
    _text(active_hub, "task", "claim", "plan-a", "task-1", *who)
    _text(
        active_hub,
        "task",
        "checkpoint",
        "plan-a",
        "task-1",
        "--summary",
        "Wrote the parser",
        *who[:4],
    )
    out = _text(active_hub, "plan", "show", "plan-a", "--task", "task-1")
    lines = out.splitlines()
    assert lines[0] == "plan-a/task-1 · First task"
    for expected in (
        "project: acme-widgets",
        "write_scope: a/**",
        "acceptance:",
        "  - First check passes",
        "status: claimed",
        "owner: codex",
        "summary: Wrote the parser",
    ):
        assert expected in lines
    assert lines.index("status: claimed") > lines.index("project: acme-widgets")

    blocked = _text(active_hub, "plan", "show", "plan-a", "--task", "task-2")
    assert "depends_on: task-1" in blocked.splitlines()
    assert "status: ready" in blocked.splitlines() and "owner:" not in blocked
    with pytest.raises(ValueError, match="Unknown task: nope"):
        _text(active_hub, "plan", "show", "plan-a", "--task", "nope")


def test_plan_show_full_and_json_return_the_whole_plan(active_hub: Path) -> None:
    plan = _text(active_hub, "plan", "show", "plan-a", "--full")
    assert isinstance(plan, dict) and plan["goal"] == GOAL
    assert plan["tasks"][0]["acceptance"][1] == "First   docs\nare updated"
    assert _json(active_hub, "plan", "show", "plan-a") == plan
    assert _json(active_hub, "plan", "show", "plan-a", "--full") == plan
    assert _text(active_hub, "plan", "show", "plan-a", "--revision", "1").startswith("plan-a · ")


def test_plan_show_json_task_is_the_task_with_status(active_hub: Path) -> None:
    detail = _json(active_hub, "plan", "show", "plan-a", "--task", "task-1")
    assert detail["id"] == "task-1" and detail["status"] == "ready"
    assert detail["write_scope"] == ["a/**"] and "owner" not in detail


# --- knowledge and pages -----------------------------------------------------------------


def _body(tmp_path: Path, size: int) -> str:
    path = tmp_path / f"body-{size}.md"
    path.write_text("x" * size, encoding="utf-8")
    return str(path)


def test_knowledge_add_and_retire_print_one_line(
    active_hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = "project:acme-widgets"
    added = _text(
        active_hub,
        "knowledge",
        "add",
        "--scope",
        scope,
        "--key",
        "note-1",
        "--title",
        "Note",
        "--body-file",
        _body(tmp_path, 40),
    )
    assert added == "knowledge project:acme-widgets/note-1 added"
    assert capsys.readouterr().err == ""
    retired = _text(
        active_hub,
        "knowledge",
        "retire",
        "--scope",
        scope,
        "--key",
        "note-1",
        "--reason",
        "Old",
    )
    assert retired == "knowledge project:acme-widgets/note-1 retired"


def test_knowledge_add_warns_on_stderr_when_the_body_is_over_500_bytes(
    active_hub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scope = "project:acme-widgets"

    def add(key: str, size: int, *flags: str):
        return cli.dispatch(
            _args(
                active_hub,
                "knowledge",
                "add",
                "--scope",
                scope,
                "--key",
                key,
                "--title",
                "Note",
                "--body-file",
                _body(tmp_path, size),
                as_json="--json" in flags,
            )
        )

    add("exact", 500)
    assert capsys.readouterr().err == ""
    add("long", 501)
    assert capsys.readouterr().err == (
        "warning: knowledge body is 501 bytes; keep it under 500 and link a page for detail\n"
    )
    event = add("long-json", 700, "--json")
    assert event["type"] == "knowledge_added"
    assert "700 bytes" in capsys.readouterr().err


class _FakeBridge:
    """Stands in for NotesBridge: an explicit --repo hub has no notes profile to store pages."""

    result: dict = {}

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def add_page(self, *_args):
        return self.result


def test_page_add_prints_one_path_and_json_keeps_the_result(
    active_hub: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "NotesBridge", _FakeBridge)
    page = tmp_path / "flow.html"
    page.write_text("<html></html>")
    argv = ("page", "add", str(page), "--plan", "plan-a")
    _FakeBridge.result = {
        "page": "/pages/plan-a-flow.html",
        "vault_path": "/vault/docs/pages/plan-a-flow.html",
        "warnings": ["Page loads a remote script; inline it so the page works offline"],
        "notes": 3,
    }
    assert _text(active_hub, *argv) == "/vault/docs/pages/plan-a-flow.html"
    assert capsys.readouterr().err == (
        "warning: Page loads a remote script; inline it so the page works offline\n"
    )
    assert _json(active_hub, *argv) == _FakeBridge.result
    _FakeBridge.result = {"page": "/pages/plan-a-flow.html", "warnings": []}
    assert _text(active_hub, *argv) == "/pages/plan-a-flow.html"
    assert capsys.readouterr().err == ""


# --- MCP ---------------------------------------------------------------------------------


@pytest.fixture
def mcp_hub(active_hub: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGOPS_REPO", str(active_hub))
    return active_hub


def test_mcp_instructions_are_one_sentence() -> None:
    assert mcp_server.INSTRUCTIONS == (
        "Follow the agops rules in your instruction file: read hub_get_brief once per session "
        "with your model id, claim before you write, and complete only with evidence."
    )


def test_mcp_read_plan_is_compact_by_default(mcp_hub: Path) -> None:
    result = mcp_server.hub_read_plan("plan-a")
    assert set(result) == {"id", "title", "goal", "tasks"}
    assert result["id"] == "plan-a" and len(result["goal"]) <= 300
    assert result["tasks"] == [
        {
            "id": "task-1",
            "title": "First task",
            "tier": "standard",
            "status": "ready",
            "depends_on": [],
            "acceptance": ["First check passes", "First docs are updated"],
        },
        {
            "id": "task-2",
            "title": "Second task",
            "tier": "frontier",
            "status": "ready",
            "depends_on": ["task-1"],
            "acceptance": ["Second check passes"],
        },
    ]


def test_mcp_read_plan_leaves_acceptance_off_completed_tasks(mcp_hub: Path, tmp_path: Path) -> None:
    hub = Hub(mcp_hub)
    worktree = project(tmp_path / "work")
    hub.claim_task("plan-a", "task-1", "codex", "one", worktree)
    hub.complete_task("plan-a", "task-1", "codex", "one", "Done", ["commit abc"])
    first = mcp_server.hub_read_plan("plan-a")["tasks"][0]
    assert first["status"] == "completed" and "acceptance" not in first


def test_mcp_read_plan_task_id_and_full(mcp_hub: Path) -> None:
    task = mcp_server.hub_read_plan("plan-a", task_id="task-2")
    assert task["status"] == "ready" and task["write_scope"] == ["b/**"]
    assert task["covers"] == ["done"] and task["acceptance"] == ["Second check passes"]
    full = mcp_server.hub_read_plan("plan-a", full=True)
    assert full["goal"] == GOAL and full["acceptance_criteria"][0]["id"] == "done"
    assert mcp_server.hub_read_plan("plan-a", revision=1, full=True) == full
    with pytest.raises(ValueError, match="Unknown task: nope"):
        mcp_server.hub_read_plan("plan-a", task_id="nope")


def test_mcp_list_ready_tasks_is_compact(mcp_hub: Path) -> None:
    assert mcp_server.hub_list_ready_tasks() == [
        {
            "plan_id": "plan-a",
            "id": "task-1",
            "title": "First task",
            "tier": "standard",
            "project": "acme-widgets",
            "depends_on": [],
        }
    ]
    assert mcp_server.hub_list_ready_tasks("plan-a") == mcp_server.hub_list_ready_tasks()
    assert mcp_server.hub_list_ready_tasks("other") == []


def test_mcp_add_knowledge_warns_over_500_bytes(mcp_hub: Path) -> None:
    short = mcp_server.hub_add_knowledge("project:acme-widgets", "short", "Short", "y" * 500)
    assert "warning" not in short
    long = mcp_server.hub_add_knowledge("project:acme-widgets", "long", "Long", "y" * 501)
    assert long["type"] == "knowledge_added"
    assert long["warning"] == (
        "knowledge body is 501 bytes; keep it under 500 and link a page for detail"
    )


# --- pure helpers ------------------------------------------------------------------------


def test_collapse_joins_whitespace_and_cuts_with_an_ellipsis() -> None:
    assert views.collapse("  a\n\tb   c ") == "a b c"
    assert views.collapse("x" * 10, 10) == "x" * 10
    cut = views.collapse("word " * 100, 20)
    assert len(cut) == 20 and cut.endswith("…")
    assert views.collapse(None) == ""


def test_format_helpers_handle_edge_events() -> None:
    queued = {"queued": True, "plan_id": "plan-a", "task_id": "task-1", "reason": "offline\n  now"}
    assert views.format_task_event(queued) == (
        "queued checkpoint plan-a/task-1; the hub was unreachable (offline now)"
    )
    finished = {
        "type": "task_completed",
        "plan_id": "plan-a",
        "task_id": "task-1",
        "payload": {"completes_plan": True},
    }
    assert views.format_task_event(finished) == "completed plan-a/task-1 · plan complete"
    assert views.format_page({"page": "/p/a.html"}) == "/p/a.html"
    assert views.format_page({"page": "/p/a.html", "vault_path": "/v/a.html"}) == "/v/a.html"
    assert views.format_ready_tasks([], None) == "No ready tasks."
    assert views.format_ready_tasks([{"plan_id": "p", "id": "t", "title": "A\nB"}], None) == (
        "p/t · - · A B"
    )
    assert views.knowledge_size_warning(" " + "z" * 500 + "\n") is None
    assert views.short_time("2026-01-02T03:04:05.123456Z") == "2026-01-02T03:04:05Z"
