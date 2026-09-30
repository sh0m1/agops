from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

MODEL_ENV = "AGOPS_MODEL"
HOME_ENV = "AGOPS_HOME"


def default_home() -> Path:
    """The user's home for tool configuration; AGOPS_HOME overrides it (tests, sandboxes)."""
    configured = os.environ.get(HOME_ENV)
    return Path(configured).expanduser() if configured else Path.home()


@dataclass(frozen=True)
class SessionRecord:
    session: str
    actor: str
    model: str
    tier: str | None
    declared_at: str


def state_root() -> Path:
    configured = os.environ.get("AGOPS_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path("~/.local/state/agops").expanduser()


def sessions_root() -> Path:
    return state_root() / "sessions"


def _record_path(session: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session)
    return sessions_root() / f"{safe or 'session'}.json"


def load_record(session: str) -> SessionRecord | None:
    path = _record_path(session)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return SessionRecord(**data)


def record_session(session: str, actor: str, model: str, tier: str | None) -> SessionRecord:
    record = SessionRecord(
        session=session,
        actor=actor,
        model=model,
        tier=tier,
        declared_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    path = _record_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def resolve_model(session: str | None, explicit: str | None = None) -> str | None:
    env = os.environ.get(MODEL_ENV, "").strip()
    if env:
        return env
    if explicit and explicit.strip():
        return explicit.strip()
    if session:
        record = load_record(session)
        if record:
            return record.model
    return None
