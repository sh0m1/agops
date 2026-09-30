from __future__ import annotations

from pathlib import Path

from agops.cli import migrate_remember
from agops.hub import Hub


def test_remember_migration_is_archived_and_omits_empty_now(hub_repo: Path, tmp_path: Path) -> None:
    source = tmp_path / ".remember"
    source.mkdir()
    (source / "now.md").write_text("", encoding="utf-8")
    (source / "recent.md").write_text("## 2026-01-01\nFinished work.\n", encoding="utf-8")
    event = migrate_remember(Hub(hub_repo), source, "acme-widgets", "migration")
    assert event["type"] == "knowledge_added"
    entries = list((hub_repo / "memory" / "knowledge").rglob("*.md"))
    text = entries[0].read_text(encoding="utf-8")
    assert "historical provenance" in text
    assert "Finished work" in text
    assert "now.md" not in text
    assert "kind: archive" in text
    assert "Finished work" not in Hub(hub_repo).brief(Path("/tmp"))
    assert Hub(hub_repo).search("Finished work")
