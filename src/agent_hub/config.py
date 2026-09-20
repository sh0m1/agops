"""Hub profiles: which runtime repository a session talks to, and how that is chosen."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sessions import default_home

PROFILE_ENV = "AGENT_HUB_PROFILE"
REPO_ENV = "AGENT_HUB_REPO"
SCHEMA_VERSION = 3
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class Profiles:
    default: str | None
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    profile: str
    root: Path
    source: str  # explicit | env | default | legacy
    overridden_profile: str | None = None


def config_path(home: Path | None = None) -> Path:
    return (home or default_home()) / ".config" / "agent-hub" / "config.json"


def load_profiles(home: Path | None = None) -> Profiles:
    """Read config.json. A pre-profile file ({repo, remote}) reads as one profile, `default`."""
    path = config_path(home)
    if not path.exists():
        return Profiles(None, {})
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return Profiles(None, {})
    if data.get("schema_version") in {2, SCHEMA_VERSION}:
        raw = data.get("profiles") or {}
        profiles = {
            str(name): dict(entry) for name, entry in raw.items() if isinstance(entry, dict)
        }
        default = data.get("default")
        return Profiles(default if default in profiles else None, profiles)
    if "repo" in data:
        entry = {"repo": str(data["repo"]), "remote": data.get("remote")}
        return Profiles("default", {"default": entry})
    return Profiles(None, {})


def save_profiles(profiles: Profiles, home: Path | None = None) -> None:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "default": profiles.default,
        "profiles": profiles.profiles,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def validate_profile_name(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(
            f"Invalid profile name: {name!r} (lowercase letters, digits, '-' and '_'; max 64)"
        )
    return name


def profile_runtime(name: str, home: Path | None = None) -> Path:
    """Where a profile's runtime clone lives. `default` keeps the historical path."""
    validate_profile_name(name)
    base = (home or default_home()) / ".local" / "share" / "agent-hub"
    return base / ("repo" if name == "default" else name)


def resolve_repo(explicit: str | None = None, home: Path | None = None) -> Resolution:
    """Pick the runtime repository for this process.

    Order: explicit path (`--repo` / AGENT_HUB_REPO) → AGENT_HUB_PROFILE (must exist; fails closed)
    → the config's default profile → the historical default path.
    """
    env_profile = os.environ.get(PROFILE_ENV, "").strip() or None
    configured = explicit or os.environ.get(REPO_ENV, "").strip() or None
    if configured:
        return Resolution("explicit", Path(configured).expanduser(), "explicit", env_profile)
    profiles = load_profiles(home)
    if env_profile:
        if env_profile not in profiles.profiles:
            names = ", ".join(sorted(profiles.profiles)) or "none"
            raise ValueError(f"Unknown hub profile '{env_profile}'; configured: {names}")
        root = Path(profiles.profiles[env_profile]["repo"]).expanduser()
        return Resolution(env_profile, root, "env")
    if profiles.default and profiles.default in profiles.profiles:
        root = Path(profiles.profiles[profiles.default]["repo"]).expanduser()
        return Resolution(profiles.default, root, "default")
    return Resolution("default", profile_runtime("default", home), "legacy")
