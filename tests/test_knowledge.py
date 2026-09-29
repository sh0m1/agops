from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import project

from agent_hub.hub import Hub
from agent_hub.security import validate_content


def test_knowledge_is_searchable_and_in_brief(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    hub.add_knowledge(
        "global", "testing", "Testing", "Always preserve concrete test evidence.", "codex", "one"
    )
    assert hub.search("concrete evidence")
    assert "Always preserve concrete test evidence" in hub.brief(Path("/tmp"))


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
    assert brief.endswith("[brief truncated]\n")


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


def test_knowledge_revision_requires_explicit_supersession(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    first = hub.add_knowledge("global", "rule", "Rule", "First version.", "codex", "one")
    entry_id = first["payload"]["id"]
    with pytest.raises(ValueError, match="supersedes"):
        hub.add_knowledge("global", "rule", "Rule", "Second version.", "claude", "two")
    second = hub.add_knowledge(
        "global", "rule", "Rule", "Second version.", "claude", "two", entry_id
    )
    brief = hub.brief(Path("/tmp"))
    assert "Second version" in brief
    assert "First version" not in brief
    hub.retire_knowledge("global", "rule", "No longer applies.", "codex", "three")
    assert "Second version" not in hub.brief(Path("/tmp"))
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
