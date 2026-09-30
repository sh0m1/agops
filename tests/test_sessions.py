from __future__ import annotations

from pathlib import Path

import pytest

from agops.sessions import (
    load_record,
    record_session,
    resolve_model,
    sessions_root,
    state_root,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    return tmp_path / "state"


def test_state_root_honours_environment(state_dir: Path) -> None:
    assert state_root() == state_dir
    assert sessions_root() == state_dir / "sessions"


def test_record_round_trip(state_dir: Path) -> None:
    assert load_record("abc") is None
    written = record_session("abc", "codex", "claude-sonnet-5", "standard")
    loaded = load_record("abc")
    assert loaded == written
    assert loaded is not None and loaded.declared_at.endswith("Z")
    assert (state_dir / "sessions" / "abc.json").exists()


def test_record_with_null_tier(state_dir: Path) -> None:
    record_session("abc", "codex", "claude-sonnet-5", None)
    loaded = load_record("abc")
    assert loaded is not None and loaded.tier is None


def test_record_path_is_sanitized(state_dir: Path) -> None:
    record_session("../evil/../../x", "codex", "m", None)
    files = list((state_dir / "sessions").iterdir())
    assert len(files) == 1
    assert "/" not in files[0].name and ".." not in files[0].name


def test_resolution_order(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_model("abc") is None
    record_session("abc", "codex", "from-record", "standard")
    assert resolve_model("abc") == "from-record"
    assert resolve_model("abc", "from-arg") == "from-arg"
    monkeypatch.setenv("AGOPS_MODEL", "from-env")
    assert resolve_model("abc", "from-arg") == "from-env"
    assert resolve_model(None) == "from-env"
    monkeypatch.setenv("AGOPS_MODEL", "   ")
    assert resolve_model(None, " from-arg ") == "from-arg"
