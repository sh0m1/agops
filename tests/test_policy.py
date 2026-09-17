from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.policy import (
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


def test_default_policy_matches_provider_prefixed_model_ids() -> None:
    """Bedrock, Vertex and gateway routes prefix the model id.

    An anchored "claude-opus-*" leaves every such session on UNKNOWN_TIER, which rejects all
    claims, so the shipped policy must match the prefixed spellings too.
    """
    policy = parse_policy(DEFAULT_POLICY_TEXT)
    assert policy.tier_for_model("us.anthropic.claude-opus-5[1m]") == "frontier"
    assert policy.tier_for_model("anthropic.claude-fable-5-1") == "frontier"
    assert policy.tier_for_model("claude-mythos-5-1") == "frontier"
    assert policy.tier_for_model("eu.anthropic.claude-sonnet-5") == "standard"
    # Haiku stays unmapped on purpose: it should not be able to claim plan tasks.
    assert policy.tier_for_model("us.anthropic.claude-haiku-4-5") == UNKNOWN_TIER


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
        (
            "schema_version: 2\ndefault_task_tier: a\ntiers:\n  a:\n    models: ['x']\n"
        ),
        "schema_version: 1\ndefault_task_tier: a\ntiers: {}\n",
        "schema_version: 1\ndefault_task_tier: a\ntiers:\n  a:\n    models: []\n",
        (
            "schema_version: 1\ndefault_task_tier: unknown\n"
            "tiers:\n  unknown:\n    models: ['x']\n"
        ),
        (
            "schema_version: 1\ndefault_task_tier: 'Bad Name'\n"
            "tiers:\n  'Bad Name':\n    models: ['x']\n"
        ),
        "tiers: [unclosed",
        (
            "schema_version: 1\ndefault_task_tier: '***'\n"
            "tiers:\n  '***':\n    models: ['x']\n"
        ),
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
