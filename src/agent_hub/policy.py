from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from .ids import slug

POLICY_RELATIVE_PATH = Path("memory") / "policy" / "tiers.yaml"
UNKNOWN_TIER = "unknown"
# The Claude patterns lead with a wildcard because provider routes prefix the model id:
# Bedrock reports us.anthropic.claude-opus-5[1m], which "claude-opus-*" cannot match, and an
# unmatched model resolves to UNKNOWN_TIER, which rejects every claim. The gpt-5.6 patterns
# stay anchored on purpose - "*gpt-5.6-*" would make the pro and terra entries overlap, and
# overlapping patterns are rejected.
DEFAULT_POLICY_TEXT = """schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["*claude-opus-*", "*claude-fable-*", "*claude-mythos-*", "gpt-5.6-pro*"]
  standard:
    models: ["*claude-sonnet-*", "gpt-5.6-terra*", "gemini-*-flash*"]
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
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"Invalid YAML syntax: {exc}") from exc
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
        try:
            slugged = slug(tier_name)
        except ValueError as exc:
            raise PolicyError(f"Invalid tier name: {tier_name}") from exc
        if slugged != tier_name or tier_name == UNKNOWN_TIER:
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
