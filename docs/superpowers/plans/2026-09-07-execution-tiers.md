# Execution Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tasks declare which tier of model may claim them, sessions declare their model, and the hub rejects mismatched claims while recording model and tier on every claim.

**Architecture:** A committed `memory/policy/tiers.yaml` maps model-id globs to user-named tiers. Sessions declare their model through the existing `brief` call; the declaration is stored in the local state directory (never committed). `claim_task` resolves the session's model, derives its tier from the current policy, and rejects mismatches; the claim event carries `model`, `tier`, `tier_override`. Two new modules, `policy.py` and `sessions.py`, hold the new logic so `hub.py` grows by a few dozen lines only.

**Tech Stack:** Python 3.12, PyYAML, `fnmatch`, pytest, ruff (line length 100). Run everything with `uv run`.

**Spec:** `docs/superpowers/specs/2026-09-07-execution-tiers-design.md`

## Global Constraints

- Python `>=3.12,<3.14`; no new runtime dependencies.
- ruff: `line-length = 100`, rules `E, F, I, UP, B, SIM`. Run `uv run ruff check src tests` before every commit.
- Policy file path is exactly `memory/policy/tiers.yaml`; pseudo-tier name is exactly `unknown`.
- Error messages must be exactly:
  - `Session has not declared a model; call brief with model=<id> first`
  - `Model '<model>' is not mapped to a tier in memory/policy/tiers.yaml`
  - `Task <task_id> requires tier <required>; session model <model> is tier <tier>`
- When `tiers.yaml` is absent, every tier check is skipped and existing tests must pass unchanged.
- Commits use the identity already configured in the clone (`Milos Milanovic <sh0m1@users.noreply.github.com>`). Do not push; the user pushes.
- Working directory for every command: the agent-hub clone (the directory containing `pyproject.toml`).

---

## File structure

| File | Responsibility |
|---|---|
| `src/agops/policy.py` (new) | Parse and validate `tiers.yaml`; `TierPolicy.tier_for_model`, `TierPolicy.task_tier`; default file text; `load_policy`. |
| `src/agops/sessions.py` (new) | Local state root; session record read/write; `resolve_model` (env → explicit → record). |
| `src/agops/state.py` | `TaskState.model/tier/tier_override`; replay from `task_claimed`; `validate_plan(plan, policy)`. |
| `src/agops/hub.py` | `Hub.policy()`, `Hub.ensure_policy()`, scan validates policy, `brief` declares and filters, `claim_task` enforces and stamps. `_outbox_root` uses `sessions.state_root`. |
| `src/agops/cli.py` | `brief --model`, `task claim --allow-tier-mismatch`, `task ready --tier`, `policy show|validate`, doctor policy check. |
| `src/agops/mcp_server.py` | `hub_get_brief(cwd, model)`. |
| `src/agops/setup.py` | Instruction sentence; `setup()` calls `Hub.ensure_policy()`. |
| `docs/protocol.md`, `README.md` | Execution tiers section; `--model` in workflow. |
| `tests/test_policy.py` (new), `tests/test_sessions.py` (new), `tests/test_tiers.py` (new) | New behaviour. Existing test files gain small additions where noted. |

---

### Task 1: Policy module

**Files:**
- Create: `src/agops/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `agops.ids.slug(value: str) -> str`.
- Produces:
  - `POLICY_RELATIVE_PATH: Path` = `Path("memory") / "policy" / "tiers.yaml"`
  - `UNKNOWN_TIER: str` = `"unknown"`
  - `DEFAULT_POLICY_TEXT: str`
  - `class PolicyError(ValueError)`
  - `@dataclass(frozen=True) class TierPolicy: default_task_tier: str; tiers: dict[str, tuple[str, ...]]` with `tier_for_model(model: str) -> str` and `task_tier(task: dict[str, Any]) -> str`
  - `policy_path(root: Path) -> Path`
  - `parse_policy(text: str) -> TierPolicy`
  - `load_policy(root: Path) -> TierPolicy | None` (None when absent; raises `PolicyError` when invalid)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_policy.py
from __future__ import annotations

from pathlib import Path

import pytest

from agops.policy import (
    DEFAULT_POLICY_TEXT,
    UNKNOWN_TIER,
    PolicyError,
    load_policy,
    parse_policy,
    policy_path,
)


def test_default_policy_parses_and_maps_models() -> None:
    policy = parse_policy(DEFAULT_POLICY_TEXT)
    assert policy.default_task_tier == "standard"
    assert policy.tier_for_model("claude-sonnet-5") == "standard"
    assert policy.tier_for_model("claude-opus-5") == "frontier"
    assert policy.tier_for_model("CLAUDE-OPUS-5") == "frontier"
    assert policy.tier_for_model("some-unlisted-model") == UNKNOWN_TIER


def test_task_tier_falls_back_to_default() -> None:
    policy = parse_policy(DEFAULT_POLICY_TEXT)
    assert policy.task_tier({"id": "a"}) == "standard"
    assert policy.task_tier({"id": "a", "tier": "frontier"}) == "frontier"


def test_overlapping_patterns_are_rejected() -> None:
    text = """schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["claude-*"]
  standard:
    models: ["claude-sonnet-*"]
"""
    with pytest.raises(PolicyError, match="overlaps"):
        parse_policy(text)


def test_default_tier_must_exist() -> None:
    text = """schema_version: 1
default_task_tier: cheap
tiers:
  standard:
    models: ["claude-sonnet-*"]
"""
    with pytest.raises(PolicyError, match="default_task_tier"):
        parse_policy(text)


@pytest.mark.parametrize(
    "text",
    [
        "just a string",
        "schema_version: 2\ndefault_task_tier: a\ntiers:\n  a:\n    models: ['x']\n",
        "schema_version: 1\ndefault_task_tier: a\ntiers: {}\n",
        "schema_version: 1\ndefault_task_tier: a\ntiers:\n  a:\n    models: []\n",
        "schema_version: 1\ndefault_task_tier: unknown\ntiers:\n  unknown:\n    models: ['x']\n",
        "schema_version: 1\ndefault_task_tier: 'Bad Name'\ntiers:\n  'Bad Name':\n    models: ['x']\n",
    ],
)
def test_malformed_policies_are_rejected(text: str) -> None:
    with pytest.raises(PolicyError):
        parse_policy(text)


def test_load_policy_absent_and_invalid(tmp_path: Path) -> None:
    assert load_policy(tmp_path) is None
    path = policy_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="memory/policy/tiers.yaml"):
        load_policy(tmp_path)
    path.write_text(DEFAULT_POLICY_TEXT, encoding="utf-8")
    assert load_policy(tmp_path) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agops.policy'`

- [ ] **Step 3: Write the implementation**

```python
# src/agops/policy.py
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from .ids import slug

POLICY_RELATIVE_PATH = Path("memory") / "policy" / "tiers.yaml"
UNKNOWN_TIER = "unknown"
DEFAULT_POLICY_TEXT = """schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["claude-opus-*", "claude-fable-*", "gpt-5.6-pro*"]
  standard:
    models: ["claude-sonnet-*", "gpt-5.6-terra*", "gemini-*-flash*"]
"""


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TierPolicy:
    default_task_tier: str
    tiers: dict[str, tuple[str, ...]]

    def tier_for_model(self, model: str) -> str:
        lowered = model.strip().lower()
        matches = [
            name
            for name, patterns in self.tiers.items()
            if any(fnmatchcase(lowered, pattern) for pattern in patterns)
        ]
        if len(matches) > 1:
            joined = ", ".join(sorted(matches))
            raise PolicyError(f"Model '{model}' matches several tiers: {joined}")
        return matches[0] if matches else UNKNOWN_TIER

    def task_tier(self, task: dict[str, Any]) -> str:
        return str(task.get("tier") or self.default_task_tier)


def policy_path(root: Path) -> Path:
    return root / POLICY_RELATIVE_PATH


def parse_policy(text: str) -> TierPolicy:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PolicyError("tiers.yaml must be a YAML mapping")
    if data.get("schema_version") != 1:
        raise PolicyError("tiers.yaml schema_version must be 1")
    raw_tiers = data.get("tiers")
    if not isinstance(raw_tiers, dict) or not raw_tiers:
        raise PolicyError("tiers.yaml must define at least one tier under 'tiers'")
    tiers: dict[str, tuple[str, ...]] = {}
    for name, definition in raw_tiers.items():
        tier_name = str(name)
        if slug(tier_name) != tier_name or tier_name == UNKNOWN_TIER:
            raise PolicyError(f"Invalid tier name: {tier_name}")
        models = definition.get("models") if isinstance(definition, dict) else None
        valid = (
            isinstance(models, list)
            and bool(models)
            and all(isinstance(model, str) and model.strip() for model in models)
        )
        if not valid:
            raise PolicyError(f"Tier {tier_name} needs a non-empty 'models' list of strings")
        tiers[tier_name] = tuple(model.strip().lower() for model in models)
    default = data.get("default_task_tier")
    if not isinstance(default, str) or default not in tiers:
        raise PolicyError("default_task_tier must name a tier defined under 'tiers'")
    _reject_overlaps(tiers)
    return TierPolicy(default_task_tier=default, tiers=tiers)


def _reject_overlaps(tiers: dict[str, tuple[str, ...]]) -> None:
    names = list(tiers)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            for first in tiers[left]:
                for second in tiers[right]:
                    if fnmatchcase(first, second) or fnmatchcase(second, first):
                        raise PolicyError(
                            f"Pattern '{first}' in tier {left} overlaps "
                            f"'{second}' in tier {right}"
                        )


def load_policy(root: Path) -> TierPolicy | None:
    path = policy_path(root)
    if not path.exists():
        return None
    try:
        return parse_policy(path.read_text(encoding="utf-8"))
    except PolicyError as exc:
        raise PolicyError(f"{POLICY_RELATIVE_PATH.as_posix()}: {exc}") from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_policy.py -v && uv run ruff check src tests`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/agops/policy.py tests/test_policy.py
git commit -m "feat: add tier policy parsing and model-to-tier resolution"
```

---

### Task 2: Session records and model resolution

**Files:**
- Create: `src/agops/sessions.py`
- Modify: `src/agops/hub.py:617-624` (`_outbox_root`)
- Test: `tests/test_sessions.py`

**Interfaces:**
- Produces:
  - `MODEL_ENV: str` = `"AGOPS_MODEL"`
  - `@dataclass(frozen=True) class SessionRecord: session: str; actor: str; model: str; tier: str | None; declared_at: str`
  - `state_root() -> Path` (honours `AGOPS_STATE_DIR`, default `~/.local/state/agent-hub`)
  - `sessions_root() -> Path` = `state_root() / "sessions"`
  - `load_record(session: str) -> SessionRecord | None`
  - `record_session(session: str, actor: str, model: str, tier: str | None) -> SessionRecord`
  - `resolve_model(session: str | None, explicit: str | None = None) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sessions.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agops.sessions'`

- [ ] **Step 3: Write the implementation**

```python
# src/agops/sessions.py
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

MODEL_ENV = "AGOPS_MODEL"


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
    return Path("~/.local/state/agent-hub").expanduser()


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
```

Then in `src/agops/hub.py`, replace `_outbox_root` so both the outbox and session records share one root. Add `from .sessions import state_root` to the imports and change the method body to:

```python
    def _outbox_root(self) -> Path:
        return state_root() / "outbox"
```

(`os` remains used elsewhere in `hub.py` — do not remove the import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sessions.py tests/test_workflow.py -v && uv run ruff check src tests`
Expected: all PASS (including the existing outbox test `test_checkpoint_queues_during_sync_failure_and_flushes_later`), ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/agops/sessions.py src/agops/hub.py tests/test_sessions.py
git commit -m "feat: add local session model declarations"
```

---

### Task 3: State replay and plan validation

**Files:**
- Modify: `src/agops/state.py:20-35` (`TaskState`), `:78-84` (claim replay), `:149-170` (`validate_plan`)
- Test: `tests/test_tiers.py` (new file; later tasks append to it)

**Interfaces:**
- Consumes: `agops.policy.TierPolicy`, `parse_policy`, `DEFAULT_POLICY_TEXT`.
- Produces:
  - `TaskState.model: str | None`, `TaskState.tier: str | None`, `TaskState.tier_override: bool`
  - `validate_plan(plan: dict[str, Any], policy: TierPolicy | None = None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tiers.py
from __future__ import annotations

import pytest

from agops.policy import DEFAULT_POLICY_TEXT, parse_policy
from agops.state import State, validate_plan


def _claim_event(payload: dict) -> dict:
    return {
        "schema_version": 1,
        "id": "e1",
        "occurred_at": "2026-09-07T00:00:00Z",
        "type": "task_claimed",
        "actor": "codex",
        "session": "one",
        "plan_id": "p",
        "task_id": "t",
        "payload": payload,
    }


def test_claim_replay_records_model_and_tier() -> None:
    state = State()
    state.apply(
        _claim_event(
            {
                "lease_until": "2999-01-01T00:00:00Z",
                "model": "claude-sonnet-5",
                "tier": "standard",
                "tier_override": True,
            }
        )
    )
    task = state.plans["p"].tasks["t"]
    assert (task.model, task.tier, task.tier_override) == ("claude-sonnet-5", "standard", True)


def test_legacy_claim_replays_without_model() -> None:
    state = State()
    state.apply(_claim_event({"lease_until": "2999-01-01T00:00:00Z"}))
    task = state.plans["p"].tasks["t"]
    assert (task.model, task.tier, task.tier_override) == (None, None, False)


def test_heartbeat_preserves_claim_model() -> None:
    state = State()
    state.apply(
        _claim_event({"lease_until": "2999-01-01T00:00:00Z", "model": "m", "tier": "standard"})
    )
    heartbeat = _claim_event({"lease_until": "2999-01-02T00:00:00Z"})
    heartbeat["type"] = "task_heartbeat"
    state.apply(heartbeat)
    assert state.plans["p"].tasks["t"].model == "m"


def _plan(tier: object) -> dict:
    return {
        "id": "p",
        "title": "P",
        "goal": "g",
        "tasks": [{"id": "t", "title": "T", "project": "acme-widgets", "tier": tier}],
    }


def test_validate_plan_checks_tier_against_policy() -> None:
    policy = parse_policy(DEFAULT_POLICY_TEXT)
    validate_plan(_plan("frontier"), policy)
    with pytest.raises(ValueError, match="unknown tier"):
        validate_plan(_plan("cheap"), policy)
    with pytest.raises(ValueError, match="must be a string"):
        validate_plan(_plan(3), policy)


def test_validate_plan_without_policy_accepts_any_tier_string() -> None:
    validate_plan(_plan("anything"))
    with pytest.raises(ValueError, match="must be a string"):
        validate_plan(_plan(["x"]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tiers.py -v`
Expected: `test_claim_replay_records_model_and_tier` FAILS with `AttributeError: 'TaskState' object has no attribute 'model'`; `test_validate_plan_checks_tier_against_policy` FAILS with `TypeError: validate_plan() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement**

In `src/agops/state.py`:

Add the import at the top (after `import yaml`):

```python
from .policy import TierPolicy
```

Extend `TaskState`:

```python
@dataclass
class TaskState:
    status: str = "ready"
    owner: str | None = None
    session: str | None = None
    lease_until: str | None = None
    worktree: str | None = None
    summary: str | None = None
    evidence: list[str] = field(default_factory=list)
    reason: str | None = None
    model: str | None = None
    tier: str | None = None
    tier_override: bool = False
```

Replace the claim/heartbeat branch in `State.apply`:

```python
        if kind in {"task_claimed", "task_heartbeat"}:
            task.status = "claimed"
            task.owner = event["actor"]
            task.session = event["session"]
            task.lease_until = payload["lease_until"]
            task.worktree = payload.get("worktree", task.worktree)
            if kind == "task_claimed":
                task.model = payload.get("model")
                task.tier = payload.get("tier")
                task.tier_override = bool(payload.get("tier_override", False))
            return
```

Replace `validate_plan`:

```python
def validate_plan(plan: dict[str, Any], policy: TierPolicy | None = None) -> None:
    required = {"id", "title", "goal", "tasks"}
    missing = sorted(required - plan.keys())
    if missing:
        raise ValueError(f"Plan is missing: {', '.join(missing)}")
    if not isinstance(plan["tasks"], list) or not plan["tasks"]:
        raise ValueError("Plan must contain at least one task")
    task_ids: set[str] = set()
    for task in plan["tasks"]:
        if not isinstance(task, dict) or not task.get("id") or not task.get("title"):
            raise ValueError("Every task requires id and title")
        if not task.get("read_only") and not task.get("project"):
            raise ValueError(f"Writing task {task['id']} requires a project id")
        task_id = str(task["id"])
        if task_id in task_ids:
            raise ValueError(f"Duplicate task id: {task_id}")
        task_ids.add(task_id)
        tier = task.get("tier")
        if tier is not None and not isinstance(tier, str):
            raise ValueError(f"Task {task_id} tier must be a string")
        if policy is not None and tier is not None and tier not in policy.tiers:
            raise ValueError(f"Task {task_id} uses unknown tier: {tier}")
    for task in plan["tasks"]:
        unknown = set(task.get("depends_on", [])) - task_ids
        if unknown:
            raise ValueError(f"Task {task['id']} has unknown dependencies: {sorted(unknown)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/agops/state.py tests/test_tiers.py
git commit -m "feat: replay model and tier from claims; validate task tiers"
```

---

### Task 4: Hub loads, initializes, and scans the policy

**Files:**
- Modify: `src/agops/hub.py` (imports; `scan`; `draft_plan`; new `policy`, `ensure_policy`)
- Modify: `tests/conftest.py` (new `policy_hub` fixture)
- Test: `tests/test_tiers.py` (append)

**Interfaces:**
- Consumes: `load_policy`, `parse_policy`, `policy_path`, `DEFAULT_POLICY_TEXT`, `POLICY_RELATIVE_PATH`, `PolicyError` from `agops.policy`.
- Produces:
  - `Hub.policy() -> TierPolicy | None`
  - `Hub.ensure_policy() -> dict[str, Any] | None` (returns the `policy_initialized` event, or None if the file already existed)
  - fixture `policy_hub` (a `hub_repo` with the default policy committed and `AGOPS_STATE_DIR` isolated)

- [ ] **Step 1: Add the fixture and failing tests**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def policy_hub(hub_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from agops.hub import Hub

    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    Hub(hub_repo).ensure_policy()
    return hub_repo
```

Append to `tests/test_tiers.py`:

```python
from pathlib import Path

from agops.hub import Hub
from agops.policy import PolicyError, policy_path


def test_ensure_policy_writes_and_publishes_default(hub_repo: Path) -> None:
    hub = Hub(hub_repo)
    assert hub.policy() is None
    event = hub.ensure_policy()
    assert event is not None and event["type"] == "policy_initialized"
    assert policy_path(hub_repo).read_text(encoding="utf-8") == DEFAULT_POLICY_TEXT
    assert hub.ensure_policy() is None
    policy = hub.policy()
    assert policy is not None and policy.default_task_tier == "standard"


def test_scan_rejects_invalid_policy(policy_hub: Path) -> None:
    hub = Hub(policy_hub)
    assert hub.scan()["errors"] == 0
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        hub.scan()


def test_draft_rejects_unknown_tier_when_policy_present(policy_hub: Path, tmp_path: Path) -> None:
    plan = tmp_path / "tiered.yaml"
    plan.write_text(
        """id: tiered
title: Tiered
goal: g
tasks:
  - {id: a, title: A, project: acme-widgets, tier: cheap}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown tier"):
        Hub(policy_hub).draft_plan(plan, "codex", "draft")
```

(Keep the existing top-of-file imports; add these new ones alongside them and let ruff's `I` rule tell you the final order.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tiers.py -v`
Expected: the three new tests FAIL with `AttributeError: 'Hub' object has no attribute 'policy'` / `'ensure_policy'`.

- [ ] **Step 3: Implement**

In `src/agops/hub.py` imports, add:

```python
from .policy import (
    DEFAULT_POLICY_TEXT,
    POLICY_RELATIVE_PATH,
    TierPolicy,
    load_policy,
    parse_policy,
    policy_path,
)
```

Add two methods to `Hub` directly after `scan`:

```python
    def policy(self) -> TierPolicy | None:
        return load_policy(self.root)

    def ensure_policy(self) -> dict[str, Any] | None:
        if policy_path(self.root).exists():
            return None

        def operation(_: State) -> tuple[dict[str, Any], str]:
            path = policy_path(self.root)
            if path.exists():
                raise ValueError("Tier policy already exists")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_POLICY_TEXT, encoding="utf-8")
            event = self._base_event("policy_initialized", "human", "interactive")
            event["payload"] = {"path": POLICY_RELATIVE_PATH.as_posix()}
            return event, "hub: initialize tier policy"

        return self._mutate(operation)
```

In `scan`, inside the `for path in files:` loop, after `validate_content(content, path.name)`, add:

```python
            if path == policy_path(self.root):
                parse_policy(content)
```

In `draft_plan`, change `validate_plan(plan)` to:

```python
        validate_plan(plan, self.policy())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/agops/hub.py tests/conftest.py tests/test_tiers.py
git commit -m "feat: load, initialize, and scan the tier policy"
```

---

### Task 5: Brief declares the session and filters by tier

**Files:**
- Modify: `src/agops/hub.py:514-561` (`brief`)
- Test: `tests/test_tiers.py` (append)

**Interfaces:**
- Consumes: `resolve_model`, `record_session` from `agops.sessions`; `UNKNOWN_TIER` from `agops.policy`; `Hub.policy()`.
- Produces: `Hub.brief(cwd: Path, max_bytes: int = 12_000, model: str | None = None, actor: str = "agent", session: str | None = None) -> str`

Header lines produced (exact text):
- declared: `Session: <actor> · <model> · tier=<tier>` where `<tier>` is the derived tier, or `unenforced` when no policy file exists
- declared but unmapped: the line above with `tier=unknown` followed by `Warning: model <model> is not mapped in memory/policy/tiers.yaml; claims will be rejected`
- undeclared: `Session: undeclared — pass model=<your model id> to hub_get_brief before claiming`

Hidden-task line, per plan, only when a policy exists and the session tier is known:
`  - <N> task(s) hidden by tier: <count> <tier>[, <count> <tier>...]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tiers.py`:

```python
from agops.sessions import load_record

TIERED_PLAN = """id: tiered
title: Tiered
goal: g
tasks:
  - {id: exec, title: Execute, project: acme-widgets, write_scope: [a/**]}
  - {id: review, title: Review, read_only: true, tier: frontier}
"""


def _activate_tiered(hub: Hub, tmp_path: Path) -> None:
    plan = tmp_path / "tiered.yaml"
    plan.write_text(TIERED_PLAN, encoding="utf-8")
    hub.draft_plan(plan, "codex", "draft")
    hub.approve_plan("tiered")


def test_brief_declares_session_and_hides_other_tiers(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "Session: codex · claude-sonnet-5 · tier=standard" in brief
    assert "exec: ready" in brief
    assert "review: ready" not in brief
    assert "1 task(s) hidden by tier: 1 frontier" in brief
    record = load_record("one")
    assert record is not None and (record.model, record.tier) == ("claude-sonnet-5", "standard")


def test_brief_redeclaration_overwrites_record(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    brief = hub.brief(Path("/tmp"), model="claude-opus-5", actor="codex", session="one")
    assert "tier=frontier" in brief
    assert "review: ready" in brief
    assert "1 task(s) hidden by tier: 1 standard" in brief
    record = load_record("one")
    assert record is not None and record.model == "claude-opus-5"


def test_brief_uses_record_when_no_model_passed(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    brief = hub.brief(Path("/tmp"), actor="codex", session="one")
    assert "tier=standard" in brief


def test_brief_undeclared_shows_nudge_and_everything(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), actor="codex", session="one")
    assert "Session: undeclared — pass model=<your model id> to hub_get_brief" in brief
    assert "exec: ready" in brief and "review: ready" in brief
    assert "hidden by tier" not in brief


def test_brief_unmapped_model_warns_and_hides_nothing(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="mystery-9", actor="codex", session="one")
    assert "tier=unknown" in brief
    assert "Warning: model mystery-9 is not mapped in memory/policy/tiers.yaml" in brief
    assert "exec: ready" in brief and "review: ready" in brief


def test_brief_without_policy_is_unenforced(hub_repo: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    hub = Hub(hub_repo)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "tier=unenforced" in brief
    assert "exec: ready" in brief and "review: ready" in brief


def test_env_model_overrides_argument(policy_hub: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGOPS_MODEL", "claude-opus-5")
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    brief = hub.brief(Path("/tmp"), model="claude-sonnet-5", actor="codex", session="one")
    assert "claude-opus-5 · tier=frontier" in brief
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tiers.py -v -k brief`
Expected: FAIL with `TypeError: Hub.brief() got an unexpected keyword argument 'model'`.

- [ ] **Step 3: Implement**

Add to `hub.py` imports: `UNKNOWN_TIER` in the `.policy` import block, and

```python
from .sessions import record_session, resolve_model, state_root
```

(replacing the `state_root`-only import from Task 2).

Replace `brief` entirely:

```python
    def brief(
        self,
        cwd: Path,
        max_bytes: int = 12_000,
        model: str | None = None,
        actor: str = "agent",
        session: str | None = None,
    ) -> str:
        project_id, remote = self.project_for_path(cwd)
        state = load_state(self.root)
        policy = self.policy()
        resolved_model = resolve_model(session, model)
        session_tier: str | None = None
        if resolved_model and policy is not None:
            session_tier = policy.tier_for_model(resolved_model)
        if resolved_model and session:
            record_session(session, actor, resolved_model, session_tier)
        filtering = policy is not None and session_tier not in {None, UNKNOWN_TIER}

        lines = ["# Agent Hub brief", "", f"Project: {project_id or 'unregistered'}"]
        if remote:
            lines.append(f"Remote: {remote}")
        if resolved_model:
            lines.append(f"Session: {actor} · {resolved_model} · tier={session_tier or 'unenforced'}")
            if session_tier == UNKNOWN_TIER:
                lines.append(
                    f"Warning: model {resolved_model} is not mapped in "
                    "memory/policy/tiers.yaml; claims will be rejected"
                )
        else:
            lines.append(
                "Session: undeclared — pass model=<your model id> to hub_get_brief before claiming"
            )
        lines.extend(["", "## Active plans"])
        for summary in self.list_plans():
            plan_state = state.plans.get(summary["id"])
            if not plan_state or not plan_state.active:
                continue
            plan = load_plan(self.root, summary["id"], plan_state.approved_revision)
            relevant = [
                task
                for task in plan["tasks"]
                if not project_id or task.get("project") in {None, project_id, remote}
            ]
            if not relevant:
                continue
            lines.append(f"- {summary['id']}: {summary['title']}")
            hidden: dict[str, int] = {}
            for task in relevant:
                if filtering and policy is not None:
                    task_tier = policy.task_tier(task)
                    if task_tier != session_tier:
                        hidden[task_tier] = hidden.get(task_tier, 0) + 1
                        continue
                task_state = plan_state.tasks.get(task["id"])
                status = task_state.status if task_state else "ready"
                owner = f" ({task_state.owner})" if task_state and task_state.owner else ""
                lines.append(f"  - {task['id']}: {status}{owner} — {task['title']}")
                if task_state and task_state.summary:
                    lines.append(f"    Latest: {task_state.summary}")
            if hidden:
                detail = ", ".join(f"{count} {tier}" for tier, count in sorted(hidden.items()))
                lines.append(f"  - {sum(hidden.values())} task(s) hidden by tier: {detail}")
        lines.extend(["", "## Knowledge"])
        allowed = {"global"}
        if project_id:
            allowed.add(f"project/{project_id}")
            for workspace in self._workspaces_for_project(project_id):
                allowed.add(f"workspace/{workspace}")
        scoped: dict[str, dict[str, Any]] = {}
        scope_order = ["global"]
        scope_order.extend(sorted(value for value in allowed if value.startswith("workspace/")))
        scope_order.extend(sorted(value for value in allowed if value.startswith("project/")))
        for selected_scope in scope_order:
            for entry in self._current_knowledge():
                if entry["scope_path"] == selected_scope:
                    scoped[entry["key"]] = entry
        for entry in scoped.values():
            lines.append(f"- {entry['body'][:500]}")
        result = "\n".join(lines).strip() + "\n"
        encoded = result.encode("utf-8")
        if len(encoded) <= max_bytes:
            return result
        return encoded[: max_bytes - 32].decode("utf-8", errors="ignore") + "\n[brief truncated]\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all PASS (the existing `test_knowledge` brief tests pass unchanged because they never pass `model`/`session` and have no policy file), ruff clean.

- [ ] **Step 5: Commit**

```bash
git add src/agops/hub.py tests/test_tiers.py
git commit -m "feat: brief declares session model and filters tasks by tier"
```

---

### Task 6: Claim enforcement

**Files:**
- Modify: `src/agops/hub.py:269-327` (`claim_task`)
- Test: `tests/test_tiers.py` (append), `tests/test_concurrency.py` (append)

**Interfaces:**
- Consumes: `Hub.policy()`, `resolve_model`, `UNKNOWN_TIER`, `TierPolicy.task_tier`, `TierPolicy.tier_for_model`.
- Produces: `Hub.claim_task(plan_id, task_id, actor, session, cwd, allow_tier_mismatch: bool = False)`; claim payload keys `model`, `tier`, `tier_override`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tiers.py`:

```python
from conftest import project

from agops.sessions import record_session
from agops.state import load_state


def test_matching_tier_claims_and_stamps_event(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    event = hub.claim_task("tiered", "exec", "codex", "one", worktree)
    assert event["payload"]["model"] == "claude-sonnet-5"
    assert event["payload"]["tier"] == "standard"
    assert event["payload"]["tier_override"] is False
    task = load_state(policy_hub).plans["tiered"].tasks["exec"]
    assert (task.model, task.tier) == ("claude-sonnet-5", "standard")


def test_mismatched_tier_is_rejected_both_ways(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("frontier-session", "claude", "claude-opus-5", "frontier")
    record_session("standard-session", "codex", "claude-sonnet-5", "standard")
    with pytest.raises(
        ValueError,
        match="Task exec requires tier standard; session model claude-opus-5 is tier frontier",
    ):
        hub.claim_task("tiered", "exec", "claude", "frontier-session", worktree)
    with pytest.raises(
        ValueError,
        match="Task review requires tier frontier; session model claude-sonnet-5 is tier standard",
    ):
        hub.claim_task("tiered", "review", "codex", "standard-session", worktree)


def test_undeclared_and_unmapped_sessions_are_rejected(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    with pytest.raises(ValueError, match="Session has not declared a model; call brief"):
        hub.claim_task("tiered", "exec", "codex", "nobody", worktree)
    record_session("odd", "codex", "mystery-9", "unknown")
    with pytest.raises(
        ValueError, match="Model 'mystery-9' is not mapped to a tier in memory/policy/tiers.yaml"
    ):
        hub.claim_task("tiered", "exec", "codex", "odd", worktree)


def test_override_bypasses_comparison_and_is_stamped(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "claude", "claude-opus-5", "frontier")
    event = hub.claim_task("tiered", "exec", "claude", "one", worktree, allow_tier_mismatch=True)
    assert event["payload"]["tier_override"] is True
    assert event["payload"]["tier"] == "frontier"
    with pytest.raises(ValueError, match="not declared"):
        hub.claim_task("tiered", "review", "claude", "nobody", worktree, allow_tier_mismatch=True)


def test_invalid_policy_rejects_every_claim(policy_hub: Path, tmp_path: Path) -> None:
    hub = Hub(policy_hub)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        hub.claim_task("tiered", "exec", "codex", "one", worktree)


def test_without_policy_claims_record_model_and_null_tier(
    hub_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AGOPS_MODEL", raising=False)
    hub = Hub(hub_repo)
    _activate_tiered(hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "codex", "claude-sonnet-5", None)
    event = hub.claim_task("tiered", "exec", "codex", "one", worktree)
    assert event["payload"]["model"] == "claude-sonnet-5"
    assert event["payload"]["tier"] is None
    undeclared = hub.claim_task("tiered", "review", "codex", "two", worktree)
    assert undeclared["payload"]["model"] is None
```

Note on `test_invalid_policy_rejects_every_claim`: the policy file is edited in the working tree, so `assert_clean` inside `_mutate` would normally fail first. `claim_task` must therefore load the policy **before** entering `_mutate` so the `PolicyError` is what the caller sees. The implementation below does that and then re-loads inside the operation for freshness after sync.

Append to `tests/test_concurrency.py`:

```python
def test_only_matching_tier_can_win_a_race(
    policy_hub: Path, plan_file: Path, tmp_path: Path, monkeypatch
) -> None:
    from agops.sessions import record_session

    first = Hub(policy_hub)
    first.draft_plan(plan_file, "codex", "draft")
    first.approve_plan("shared-plan")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    record_session("two", "claude", "claude-opus-5", "frontier")

    remote = git(policy_hub, "remote", "get-url", "origin")
    second_path = tmp_path / "machine-two" / "runtime"
    second_path.parent.mkdir()
    subprocess.run(
        ["git", "clone", "-b", "main", remote, str(second_path)],
        check=True,
        capture_output=True,
    )
    git(second_path, "config", "user.email", "test@example.com")
    git(second_path, "config", "user.name", "Agent Hub Test")
    (second_path / ".agent-hub-managed").touch()
    second = Hub(second_path)
    first_worktree = project(tmp_path / "work-one")
    second_worktree = project(tmp_path / "work-two")

    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim(hub: Hub, actor: str, session: str, worktree: str) -> None:
        barrier.wait()
        try:
            hub.claim_task("shared-plan", "first", actor, session, Path(worktree))
            outcomes[actor] = "won"
        except Exception as exc:
            outcomes[actor] = str(exc)

    monkeypatch.delenv("AGOPS_LOCK_DIR")
    threads = [
        threading.Thread(target=claim, args=(first, "codex", "one", str(first_worktree))),
        threading.Thread(target=claim, args=(second, "claude", "two", str(second_worktree))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes["codex"] == "won"
    assert "requires tier standard" in outcomes["claude"]
    first.sync()
    task = load_state(policy_hub).plans["shared-plan"].tasks["first"]
    assert (task.owner, task.tier) == ("codex", "standard")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tiers.py tests/test_concurrency.py -v`
Expected: `test_matching_tier_claims_and_stamps_event` FAILS with `KeyError: 'model'`; `test_override_bypasses_comparison_and_is_stamped` FAILS with `TypeError: ... unexpected keyword argument 'allow_tier_mismatch'`; mismatch tests FAIL with `DID NOT RAISE`.

- [ ] **Step 3: Implement**

Replace `claim_task` in `hub.py`:

```python
    def claim_task(
        self,
        plan_id: str,
        task_id: str,
        actor: str,
        session: str,
        cwd: Path,
        allow_tier_mismatch: bool = False,
    ) -> dict[str, Any]:
        plan_id = slug(plan_id)
        self.policy()  # fail fast on an invalid policy before touching git

        def operation(state: State) -> tuple[dict[str, Any], str]:
            plan_state, plan, task = self._active_task(state, plan_id, task_id)
            current = plan_state.tasks.get(task_id)
            if current and current.actively_claimed():
                raise ValueError(f"Task is already claimed by {current.owner}")
            for dependency in task.get("depends_on", []):
                dependency_state = plan_state.tasks.get(dependency)
                if not dependency_state or dependency_state.status != "completed":
                    raise ValueError(f"Dependency is not complete: {dependency}")
            model, tier, override_used = self._check_tier(task, task_id, session, allow_tier_mismatch)
            project = task.get("project")
            read_only = bool(task.get("read_only"))
            if read_only:
                worktree = None
                worktree_project = project
            else:
                worktree, worktree_project = self._worktree_identity(cwd)
            if not read_only and worktree_project != project:
                raise ValueError(
                    f"Task targets project {project}, but checkout is {worktree_project}"
                )
            scope = task.get("write_scope", [])
            for other_plan_id, other_plan_state in state.plans.items():
                if not other_plan_state.active:
                    continue
                other_plan = load_plan(self.root, other_plan_id, other_plan_state.approved_revision)
                for other_task in other_plan["tasks"]:
                    other_state = other_plan_state.tasks.get(other_task["id"])
                    if not other_state or not other_state.actively_claimed():
                        continue
                    if read_only or other_task.get("read_only"):
                        continue
                    if other_state.worktree == worktree:
                        raise ValueError("Another writing task is active in this checkout")
                    if other_task.get("project") == project and scopes_overlap(
                        scope, other_task.get("write_scope", [])
                    ):
                        raise ValueError(f"Write scope overlaps {other_plan_id}/{other_task['id']}")
            lease = timestamp_after(minutes=60)
            event = self._base_event("task_claimed", actor, session)
            event.update(
                {
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "payload": {
                        "lease_until": lease,
                        "worktree": worktree,
                        "model": model,
                        "tier": tier,
                        "tier_override": override_used,
                    },
                }
            )
            return event, f"hub: claim {plan_id}/{task_id} for {actor}"

        return self._mutate(operation)

    def _check_tier(
        self,
        task: dict[str, Any],
        task_id: str,
        session: str,
        allow_tier_mismatch: bool,
    ) -> tuple[str | None, str | None, bool]:
        policy = self.policy()
        model = resolve_model(session)
        if policy is None:
            return model, None, False
        if not model:
            raise ValueError("Session has not declared a model; call brief with model=<id> first")
        tier = policy.tier_for_model(model)
        if tier == UNKNOWN_TIER:
            raise ValueError(f"Model '{model}' is not mapped to a tier in memory/policy/tiers.yaml")
        required = policy.task_tier(task)
        if tier == required:
            return model, tier, False
        if not allow_tier_mismatch:
            raise ValueError(
                f"Task {task_id} requires tier {required}; session model {model} is tier {tier}"
            )
        return model, tier, True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all PASS, ruff clean. The pre-existing `test_remote_push_serializes_competing_claims` still passes because `hub_repo` has no policy file.

- [ ] **Step 5: Commit**

```bash
git add src/agops/hub.py tests/test_tiers.py tests/test_concurrency.py
git commit -m "feat: enforce execution tiers on claim and record model on the event"
```

---

### Task 7: CLI, MCP, setup, and doctor surface

**Files:**
- Modify: `src/agops/cli.py` (parser, dispatch, `doctor`, `require_human_confirmation`, new `policy_report`)
- Modify: `src/agops/mcp_server.py:26-29` (`hub_get_brief`)
- Modify: `src/agops/setup.py:41-49` (`setup` calls `ensure_policy`)
- Test: `tests/test_cli_tiers.py` (new)

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces:
  - `agops brief --cwd DIR [--model ID]`
  - `agops task claim PLAN TASK [--allow-tier-mismatch]`
  - `agops task ready [--plan ID] [--tier NAME]`
  - `agops policy show` / `agops policy validate`
  - `doctor()["policy"]` ∈ `"ok" | "absent" | "invalid: <message>"`
  - `require_human_confirmation(identifier: str, yes: bool, action: str, noun: str = "plan") -> None`
  - `policy_report(hub: Hub, validate_only: bool) -> dict[str, Any]`
  - MCP `hub_get_brief(cwd: str = ".", model: str | None = None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_tiers.py
from __future__ import annotations

import builtins
import inspect
import sys
from pathlib import Path

import pytest
from conftest import project

from agops import cli
from agops.hub import Hub
from agops.policy import policy_path
from agops.sessions import load_record, record_session

TIERED_PLAN = """id: tiered
title: Tiered
goal: g
tasks:
  - {id: exec, title: Execute, project: acme-widgets, write_scope: [a/**]}
  - {id: review, title: Review, read_only: true, tier: frontier}
"""


def _run(hub_repo: Path, *argv: str):
    args = cli.build_parser().parse_args(["--repo", str(hub_repo), *argv])
    return cli.dispatch(args)


def _activate(hub_repo: Path, tmp_path: Path) -> Hub:
    hub = Hub(hub_repo)
    plan = tmp_path / "tiered.yaml"
    plan.write_text(TIERED_PLAN, encoding="utf-8")
    hub.draft_plan(plan, "codex", "draft")
    hub.approve_plan("tiered")
    return hub


def test_brief_model_flag_declares_env_session(policy_hub: Path, tmp_path: Path, monkeypatch) -> None:
    _activate(policy_hub, tmp_path)
    monkeypatch.setenv("AGOPS_SESSION", "cli-one")
    monkeypatch.setenv("AGOPS_ACTOR", "codex")
    out = _run(policy_hub, "brief", "--cwd", "/tmp", "--model", "claude-sonnet-5")
    assert "Session: codex · claude-sonnet-5 · tier=standard" in out
    record = load_record("cli-one")
    assert record is not None and record.model == "claude-sonnet-5"


def test_brief_without_session_env_does_not_write_a_record(
    policy_hub: Path, tmp_path: Path, monkeypatch
) -> None:
    _activate(policy_hub, tmp_path)
    monkeypatch.delenv("AGOPS_SESSION", raising=False)
    _run(policy_hub, "brief", "--cwd", "/tmp", "--model", "claude-sonnet-5")
    from agops.sessions import sessions_root

    assert not sessions_root().exists() or not list(sessions_root().iterdir())


def test_task_ready_tier_filter(policy_hub: Path, tmp_path: Path) -> None:
    _activate(policy_hub, tmp_path)
    assert [t["id"] for t in _run(policy_hub, "task", "ready")] == ["exec", "review"]
    assert [t["id"] for t in _run(policy_hub, "task", "ready", "--tier", "frontier")] == ["review"]
    assert [t["id"] for t in _run(policy_hub, "task", "ready", "--tier", "standard")] == ["exec"]


def test_override_requires_tty_and_confirmation(policy_hub: Path, tmp_path: Path, monkeypatch) -> None:
    _activate(policy_hub, tmp_path)
    worktree = project(tmp_path / "work")
    record_session("one", "claude", "claude-opus-5", "frontier")
    base = [
        "task", "claim", "tiered", "exec",
        "--actor", "claude", "--session", "one", "--cwd", str(worktree),
    ]
    with pytest.raises(ValueError, match="requires tier standard"):
        _run(policy_hub, *base)

    monkeypatch.setenv("AGOPS_AGENT_SESSION", "1")
    with pytest.raises(ValueError, match="unavailable inside a managed agent session"):
        _run(policy_hub, *base, "--allow-tier-mismatch")
    monkeypatch.delenv("AGOPS_AGENT_SESSION")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(ValueError, match="interactive terminal"):
        _run(policy_hub, *base, "--allow-tier-mismatch")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "wrong")
    with pytest.raises(ValueError, match="cancelled"):
        _run(policy_hub, *base, "--allow-tier-mismatch")

    monkeypatch.setattr(builtins, "input", lambda _prompt: "exec")
    event = _run(policy_hub, *base, "--allow-tier-mismatch")
    assert event["payload"]["tier_override"] is True


def test_policy_show_and_validate(policy_hub: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGOPS_SESSION", "one")
    record_session("one", "codex", "claude-sonnet-5", "standard")
    report = _run(policy_hub, "policy", "show")
    assert report["present"] is True and report["valid"] is True
    assert report["default_task_tier"] == "standard"
    assert "frontier" in report["tiers"]
    assert report["session"] == {"session": "one", "model": "claude-sonnet-5", "tier": "standard"}
    assert _run(policy_hub, "policy", "validate")["valid"] is True

    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    broken = _run(policy_hub, "policy", "show")
    assert broken["valid"] is False and "tiers" in broken["error"]
    with pytest.raises(ValueError):
        _run(policy_hub, "policy", "validate")


def test_policy_report_when_absent(hub_repo: Path) -> None:
    report = _run(hub_repo, "policy", "show")
    assert report == {"path": str(policy_path(hub_repo)), "present": False, "valid": None}
    assert _run(hub_repo, "policy", "validate")["valid"] is None


def test_doctor_reports_policy_state(policy_hub: Path, hub_repo: Path) -> None:
    assert cli.doctor(Hub(policy_hub))["policy"] == "ok"
    policy_path(policy_hub).write_text("schema_version: 1\n", encoding="utf-8")
    report = cli.doctor(Hub(policy_hub))
    assert report["policy"].startswith("invalid:")
    assert report["ok"] is False


def test_doctor_reports_absent_policy(hub_repo: Path) -> None:
    report = cli.doctor(Hub(hub_repo))
    assert report["policy"] == "absent"


def test_mcp_brief_accepts_model_and_claim_has_no_override() -> None:
    from agops import mcp_server

    assert "model" in inspect.signature(mcp_server.hub_get_brief).parameters
    assert "allow_tier_mismatch" not in inspect.signature(mcp_server.hub_claim_task).parameters
```

Note: `policy_hub` and `hub_repo` in `test_doctor_reports_policy_state` are the same directory (the fixture builds on `hub_repo`); requesting both is fine.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_tiers.py -v`
Expected: FAIL with `argparse` errors (`unrecognized arguments: --model`, `invalid choice: 'policy'`, `--tier`, `--allow-tier-mismatch`) and `KeyError: 'policy'` in the doctor tests.

- [ ] **Step 3: Implement the CLI**

In `src/agops/cli.py` imports, add:

```python
from .policy import PolicyError, policy_path
from .sessions import resolve_model
```

In `build_parser`:

```python
    brief = commands.add_parser("brief")
    brief.add_argument("--cwd", default=".")
    brief.add_argument("--model", help="Model id of this session, e.g. claude-sonnet-5")
```

```python
    policy_parser = commands.add_parser("policy")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_commands.add_parser("show")
    policy_commands.add_parser("validate")
```

```python
    ready = task_commands.add_parser("ready")
    ready.add_argument("--plan")
    ready.add_argument("--tier")
    for name in ("claim", "release"):
        command = task_commands.add_parser(name)
        command.add_argument("plan_id")
        command.add_argument("task_id")
        command.add_argument("--actor")
        command.add_argument("--session")
        if name == "claim":
            command.add_argument("--cwd", default=".")
            command.add_argument("--allow-tier-mismatch", action="store_true")
```

In `dispatch`, replace the `brief` branch:

```python
    if args.command == "brief":
        return hub.brief(
            Path(args.cwd),
            model=args.model,
            actor=os.environ.get("AGOPS_ACTOR", "agent"),
            session=os.environ.get("AGOPS_SESSION") or None,
        )
    if args.command == "policy":
        return policy_report(hub, validate_only=args.policy_command == "validate")
```

Replace the `task ready` and `task claim` branches:

```python
    if args.command == "task":
        if args.task_command == "ready":
            tasks = hub.ready_tasks(args.plan)
            if args.tier:
                policy = hub.policy()
                tasks = [
                    task
                    for task in tasks
                    if (policy.task_tier(task) if policy else task.get("tier")) == args.tier
                ]
            return tasks
        actor, session = actor_session(args)
        if args.task_command == "claim":
            if args.allow_tier_mismatch:
                if os.environ.get("AGOPS_AGENT_SESSION"):
                    raise ValueError("Tier override is unavailable inside a managed agent session")
                require_human_confirmation(args.task_id, False, "override tier for", noun="task")
            return hub.claim_task(
                args.plan_id,
                args.task_id,
                actor,
                session,
                Path(args.cwd),
                allow_tier_mismatch=args.allow_tier_mismatch,
            )
```

Replace `doctor`:

```python
def doctor(hub: Hub) -> dict[str, Any]:
    checks: dict[str, Any] = {"root": str(hub.root), "managed_clone": is_managed_clone(hub.root)}
    checks["git"] = run_git(hub.root, "status", "--porcelain").stdout.strip() == ""
    try:
        checks["policy"] = "ok" if hub.policy() else "absent"
    except PolicyError as exc:
        checks["policy"] = f"invalid: {exc}"
    try:
        checks["scan"] = hub.scan()["errors"] == 0
    except ValueError:
        checks["scan"] = False
    checks["queued_checkpoints"] = len(list(hub._outbox_root().glob("*.json")))
    checks["codex_instructions"] = Path("~/.codex/AGENTS.md").expanduser().exists()
    checks["claude_instructions"] = Path("~/.claude/CLAUDE.md").expanduser().exists()
    informational = {"root", "queued_checkpoints", "policy"}
    checks["ok"] = all(
        value for key, value in checks.items() if key not in informational
    ) and not str(checks["policy"]).startswith("invalid")
    return checks
```

Replace `require_human_confirmation`:

```python
def require_human_confirmation(identifier: str, yes: bool, action: str, noun: str = "plan") -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise ValueError(f"{noun.title()} {action} requires an interactive terminal or --yes")
    answer = input(f"{action.title()} {noun} {identifier}? Type its id: ")
    if answer != identifier:
        raise ValueError(f"{noun.title()} {action} cancelled")
```

Add `policy_report` after `doctor`:

```python
def policy_report(hub: Hub, validate_only: bool) -> dict[str, Any]:
    path = policy_path(hub.root)
    report: dict[str, Any] = {"path": str(path), "present": path.exists()}
    if not path.exists():
        report["valid"] = None
        return report
    try:
        policy = hub.policy()
    except PolicyError as exc:
        if validate_only:
            raise ValueError(str(exc)) from exc
        report.update({"valid": False, "error": str(exc)})
        return report
    report["valid"] = True
    if validate_only or policy is None:
        return report
    report["default_task_tier"] = policy.default_task_tier
    report["tiers"] = {name: list(patterns) for name, patterns in policy.tiers.items()}
    session = os.environ.get("AGOPS_SESSION") or None
    model = resolve_model(session)
    report["session"] = {
        "session": session,
        "model": model,
        "tier": policy.tier_for_model(model) if model else None,
    }
    return report
```

Note: the `plan cancel` branch already calls `require_human_confirmation(args.plan_id, args.yes, "cancel")`; the new `noun` default keeps its prompt text `Cancel plan <id>? Type its id:` unchanged.

- [ ] **Step 4: Implement MCP and setup**

`src/agops/mcp_server.py`:

```python
@mcp.tool()
def hub_get_brief(cwd: str = ".", model: str | None = None) -> str:
    """Get bounded context, active plans, and the tasks this session's tier may claim.

    Pass your current model id (for example claude-sonnet-5) so the hub can record the
    session's tier; call again if the model changes.
    """
    return hub().brief(Path(cwd), model=model, actor=ACTOR, session=SESSION)
```

`src/agops/setup.py`: add `from .hub import Hub` to the imports, and in `setup()` insert directly after `(runtime / ".agent-hub-managed").touch()`:

```python
    Hub(runtime).ensure_policy()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -v && uv run ruff check src tests`
Expected: all PASS, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add src/agops/cli.py src/agops/mcp_server.py src/agops/setup.py tests/test_cli_tiers.py
git commit -m "feat: expose execution tiers through the CLI, MCP brief, setup, and doctor"
```

---

### Task 8: Instructions and documentation

**Files:**
- Modify: `src/agops/setup.py:11-21` (`INSTRUCTIONS`)
- Modify: `tests/test_setup.py`
- Modify: `docs/protocol.md`, `README.md`

**Interfaces:**
- Produces: the managed block text that `setup` and `adapter install` write into `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.cursor/rules/agent-hub.mdc`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_setup.py`:

```python
def test_managed_instructions_require_a_model_declaration() -> None:
    assert "model id" in INSTRUCTIONS
    assert "hub_get_brief" in INSTRUCTIONS
    assert "matching your tier" in INSTRUCTIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_setup.py -v`
Expected: `test_managed_instructions_require_a_model_declaration` FAILS with `AssertionError`.

- [ ] **Step 3: Update the instruction block**

Replace `INSTRUCTIONS` in `src/agops/setup.py`:

```python
INSTRUCTIONS = f"""{MANAGED_START}
## Shared Agent Hub

At the start of each work session, call `agops brief --cwd \"$PWD\"` or the MCP
`hub_get_brief` tool. Pass your current model id to `hub_get_brief` (or
`agops brief --model`) at session start and again if the model changes; claims are limited
to tasks matching your tier. Before modifying files for an approved shared plan, claim a ready
task. Checkpoint meaningful progress and before handoff or context compaction. Complete tasks
only with test, artifact, or commit evidence. Never store credentials, `.env` contents, or raw
transcripts. Keep one stable session ID across standalone CLI task calls. User instructions
always take precedence over Agent Hub state.
{MANAGED_END}
"""
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_setup.py -v`
Expected: PASS (the existing `stable session ID` assertion still holds).

- [ ] **Step 5: Document the protocol**

Append to `docs/protocol.md` before the `## Plan schema` heading:

````markdown
## Execution tiers

`memory/policy/tiers.yaml` maps model ids to user-named tiers:

```yaml
schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["claude-opus-*", "claude-fable-*", "gpt-5.6-pro*"]
  standard:
    models: ["claude-sonnet-*", "gpt-5.6-terra*", "gemini-*-flash*"]
```

Patterns are case-insensitive globs. A model matching no tier is `unknown` and cannot claim. A
model matching two tiers is a validation error. `setup` writes the default file; edit it in any
editor and commit — `scan`, `doctor`, and `agops policy validate` check it.

Tasks take an optional `tier`; a missing value means `default_task_tier`. A `tier` not defined in
the policy is rejected at draft time.

Sessions declare their model by passing it to `brief` / `hub_get_brief`. The resolution order is
the `AGOPS_MODEL` environment variable, then the `model` argument, then the record saved by
an earlier brief for the same session id. Records live in the local state directory and are
never committed. A declared brief lists only tasks of the session's tier and reports how many
tasks of other tiers were hidden.

A claim is rejected when the session is undeclared, when its model is unmapped, or when its tier
differs from the task's tier. The claim event records `model`, `tier`, and `tier_override`.
Heartbeats, checkpoints, and completion do not re-check the tier; the guard is at pickup.

`agops task claim … --allow-tier-mismatch` bypasses the comparison. It is refused in managed
agent sessions and non-interactive terminals, requires typing the task id, and is not available
through MCP.

When `tiers.yaml` is absent, tier checks are skipped and `doctor` reports `policy: absent`. When
it is invalid, `scan` and `doctor` fail and every claim is rejected.
````

Also add `tier: standard` to the example task in the existing `## Plan schema` block, directly under `project: you-agent-hub-memory`.

- [ ] **Step 6: Update the README**

In `README.md`, replace the "Everyday workflow" code block with:

```sh
export AGOPS_ACTOR=codex
export AGOPS_SESSION="$(agent-hub session --actor codex --value)"
agent-hub brief --cwd "$PWD" --model gpt-5.6-terra
agent-hub plan list
agent-hub task ready --tier standard
agent-hub task claim PLAN TASK --cwd "$PWD"
agent-hub task checkpoint PLAN TASK --summary "Implemented parser" --evidence "pytest: 12 passed"
agent-hub task complete PLAN TASK --evidence "commit: abc123" --evidence "pytest: 12 passed"
```

And add after that block:

```markdown
Tasks carry a `tier` (default `standard`); `memory/policy/tiers.yaml` maps model ids to tiers.
Frontier models plan and review, cheaper models execute, and the hub rejects claims that cross
tiers. See [docs/protocol.md](docs/protocol.md#execution-tiers).
```

- [ ] **Step 7: Full verification**

Run: `uv run pytest -v && uv run ruff check src tests && uv build --quiet && rm -rf dist`
Expected: all tests PASS, ruff clean, build succeeds.

- [ ] **Step 8: Commit**

```bash
git add src/agops/setup.py tests/test_setup.py docs/protocol.md README.md
git commit -m "docs: describe execution tiers and require model declaration in instructions"
```

---

## Self-review

**Spec coverage**

| Spec section | Task |
|---|---|
| §1 policy file, rules, file states | 1 (parse/validate), 4 (absent/invalid behaviour in scan, ensure_policy), 6 (invalid → claims rejected), 7 (doctor/validate) |
| §2 task schema, default, draft validation, read_only tiers | 3, 4; `TIERED_PLAN` in 5/6/7 uses a `read_only` frontier task |
| §3 session declaration, resolution order, record, brief header, filtering, re-declaration | 2, 5 |
| §4 claim checks in order, exact messages, payload, TaskState, no re-check after claim, override semantics, `ready_tasks` tier-agnostic + `--tier` | 3 (state), 6 (claim), 7 (`--tier`, TTY gating, no MCP override) |
| §5 CLI table, MCP signature, instruction sentence, docs | 7, 8 |
| §6 code placement (`policy.py`, `sessions.py`) | 1, 2 |
| Testing list | policy → 1; declaration → 2, 5; claim → 6, 7; replay → 3; concurrency → 6 |

**Placeholder scan:** none.

**Type consistency:** `TierPolicy.tiers: dict[str, tuple[str, ...]]` (Task 1) is what `validate_plan` (Task 3) indexes with `tier not in policy.tiers` and what `policy_report` (Task 7) lists. `SessionRecord.tier: str | None` (Task 2) matches `record_session(..., session_tier)` where `session_tier: str | None` (Task 5). `_check_tier` returns `(str | None, str | None, bool)` and the payload uses those three positions (Task 6). `require_human_confirmation` gains `noun` with default `"plan"` so the existing `plan cancel` call site compiles unchanged (Task 7).
