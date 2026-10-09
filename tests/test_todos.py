from __future__ import annotations

from pathlib import Path

import pytest

from agops.todos import (
    display_text,
    heading_link,
    load_todos,
    normalize_settings,
    parse_todos,
    resolve_todo_file,
)

PLANS = {"net-iso", "cloud-wan", "old-plan"}

# The shapes of a real hand-written list: tabs, 1- and 2-space nesting, tag group lines,
# caps labels, plain-bullet answers, blank lines inside groups and non-ASCII text.
SAMPLE = """﻿## Inbox
- [ ] Send the diagram [[net-iso]] ([[2026-10-05 Standup|10-05 standup]])
- [ ] Prepare the design [[cloud-wan]]
\t- [ ] add details with Kevin
\t- [x] book the room

## Airflow

- [x] Pogledaj JCT depl i pokupi sve detalje
- [ ] refresh token - where it happens?
\t- [x]  i kako se radi sa consentom?
\t-  da, r token je samo potreban
- [ ] sa workshopa
\t- [ ] agent permissions?


PLAN AFTER PARIS
#problem #aocc
- request takes long time
#plan
- [ ] improve airflow - scrap managed harness

## Platform [[net-iso]]

 - [x] signing key repo failing pipeline
 - [ ] review dp-aws-tf-module-auth0-api#43

#egress #stockholm [[cloud-wan]]
 - [ ] share ips with mike s
  - [ ] talk to fredrik + brad
  - [-] dropped idea
 - [ ] shield vs nfw

OPEN Q

- [ ] Question on IGO session
\t- [ ] customer dns - how that works?
\t\t- [ ] conditional forwarder?
- [ ] own link wins [[old-plan]]
\t- [ ] child of own link

LINKS
https://example.com/board/968

```
- [ ] not an item inside a fence
```
- [ ]
"""


def _find(todos, text: str):
    return next(item for item in todos.items if item.text.startswith(text))


def _children(todos, item):
    index = todos.items.index(item)
    return [other for other in todos.items if other.parent == index]


def test_counts_states_and_inbox() -> None:
    todos = parse_todos(SAMPLE, PLANS)
    assert todos.counts() == {
        "open": 16,
        "done": 4,
        "cancelled": 1,
        "inbox": 3,
        "linked": 12,
    }
    assert not any("fence" in item.text for item in todos.items)


def test_nesting_uses_relative_indentation() -> None:
    todos = parse_todos(SAMPLE, PLANS)
    share = _find(todos, "share ips")
    assert [child.text for child in _children(todos, share)] == [
        "talk to fredrik + brad",
        "dropped idea",
    ]
    assert _find(todos, "conditional forwarder?").depth == 2
    assert len(_children(todos, _find(todos, "Question on IGO"))) == 1


def test_plain_bullets_are_context_not_items() -> None:
    todos = parse_todos(SAMPLE, PLANS)
    assert not any(item.text.startswith("da, r token") for item in todos.items)
    assert not any(item.text.startswith("request takes") for item in todos.items)
    refresh = _find(todos, "refresh token")
    assert [child.state for child in _children(todos, refresh)] == ["done"]


def test_tag_lines_and_labels_form_groups_not_headings() -> None:
    todos = parse_todos(SAMPLE, PLANS)
    share = _find(todos, "share ips")
    assert share.headings == ("Platform",)
    assert share.group == "#egress #stockholm"
    assert {"egress", "stockholm"} <= set(share.tags)
    assert _find(todos, "improve airflow").group == "PLAN AFTER PARIS › #plan"
    assert _find(todos, "Question on IGO").group == "OPEN Q"
    # A heading resets groups; the URL under LINKS creates nothing.
    assert _find(todos, "review dp-aws").group is None
    assert todos.items[-1].text == "child of own link"


def test_plan_links_inherit_nearest_scope_first() -> None:
    todos = parse_todos(SAMPLE, PLANS)
    assert _find(todos, "Send the diagram").plans == ("net-iso",)
    assert _find(todos, "add details with Kevin").plans == ("cloud-wan",)
    assert _find(todos, "review dp-aws").plans == ("net-iso",)  # from the heading
    assert _find(todos, "share ips").plans == ("cloud-wan",)  # the group line wins
    assert _find(todos, "conditional forwarder?").plans == ("net-iso",)
    assert _find(todos, "own link wins").plans == ("old-plan",)
    assert _find(todos, "child of own link").plans == ("old-plan",)
    assert _find(todos, "sa workshopa").plans == ()
    assert _find(todos, "Send the diagram").links == ("2026-10-05 Standup",)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("- [ ] a [[net-iso]]", ("net-iso",)),
        ("- [ ] a [[net-iso|NI]]", ("net-iso",)),
        ("- [ ] a [[agops/plans/active/net-iso|x]]", ("net-iso",)),
        ("- [ ] a [[agops/plans/archive/ws/net-iso]]", ("net-iso",)),
        ("- [ ] a [[net-iso#Tasks]]", ("net-iso",)),
        ("- [ ] a [plan](agops/plans/active/net-iso.md)", ("net-iso",)),
        ("- [ ] a ![[net-iso]]", ()),
        ("- [ ] a [[docs/net-iso]]", ()),
        ("- [ ] a [[unknown-plan]]", ()),
    ],
)
def test_plan_link_forms(line: str, expected: tuple[str, ...]) -> None:
    assert parse_todos(line, PLANS).items[0].plans == expected


def test_tags_ignore_refs_numbers_and_urls() -> None:
    item = parse_todos(
        "- [ ] fix repo#105 and #123 see https://x.io/a#frag `#code` #sec-net-inc #č-tag", PLANS
    ).items[0]
    assert item.tags == ("sec-net-inc", "č-tag")


def test_frontmatter_crlf_and_custom_inbox() -> None:
    text = "---\ntitle: x\n---\r\n## Neu\r\n- [X] done\r\n- [ ] new\r\n"
    todos = parse_todos(text, PLANS, inbox="neu")
    assert [(item.state, item.in_inbox) for item in todos.items] == [
        ("done", True),
        ("open", True),
    ]


def test_heading_link_and_display_text() -> None:
    todos = parse_todos(SAMPLE, PLANS, file="Todo/ALL.md")
    share = _find(todos, "share ips")
    # The heading holds a link, so Obsidian has no stable anchor: link the file only.
    assert heading_link("Todo/ALL.md", share) == "[[Todo/ALL|ALL › Platform › #egress #stockholm]]"
    airflow = _find(todos, "refresh token")
    assert heading_link("Todo/ALL.md", airflow) == "[[Todo/ALL#Airflow|ALL › Airflow]]"
    unsafe = parse_todos("## Net [[net-iso]]\n- [ ] x\n", PLANS).items[0]
    assert heading_link("Todo/ALL.md", unsafe) == "[[Todo/ALL|ALL › Net]]"
    other = parse_todos("## Net [[Some note|alias]]\n- [ ] x\n", PLANS).items[0]
    assert heading_link("Todo/ALL.md", other) == "[[Todo/ALL|ALL › Net alias]]"
    diagram = _find(todos, "Send the diagram")
    assert display_text(diagram, "net-iso") == (
        "Send the diagram ([[2026-10-05 Standup|10-05 standup]])"
    )


def test_normalize_settings() -> None:
    assert normalize_settings(None) == (None, [])
    assert normalize_settings("Todo/ALL.md")[0] == {
        "file": "Todo/ALL.md",
        "inbox": "Inbox",
        "sweep_exclude": [],
    }
    for bad in ("/abs/ALL.md", "~/ALL.md", "../ALL.md", "Todo/ALL.txt", "a\\b.md"):
        settings, warnings = normalize_settings({"file": bad})
        assert settings is None and warnings, bad
    assert normalize_settings({"file": "x.md", "sweep_exclude": "Meetings"})[0] is None


def test_resolve_and_load_guard_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Todo").mkdir(parents=True)
    (vault / "agops").mkdir()
    (vault / "Todo" / "ALL.md").write_text("- [ ] one [[net-iso]]\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("- [ ] secret\n", encoding="utf-8")
    (vault / "Todo" / "link.md").symlink_to(outside)
    (vault / "agops" / "own.md").write_text("- [ ] x\n", encoding="utf-8")

    settings = normalize_settings("Todo/ALL.md")[0]
    todos = load_todos(vault, settings, PLANS, vault / "agops")
    assert todos.file == "Todo/ALL.md" and len(todos.sha256) == 64
    assert todos.for_plan("net-iso")[0].text == "one [[net-iso]]"
    with pytest.raises(ValueError, match="symlink"):
        resolve_todo_file(vault, "Todo/link.md")
    with pytest.raises(ValueError, match="agops notes folder"):
        resolve_todo_file(vault, "agops/own.md", vault / "agops")
    with pytest.raises(FileNotFoundError):
        load_todos(vault, normalize_settings("Todo/missing.md")[0], PLANS)


def test_dates_come_from_source_links_parents_and_headings() -> None:
    text = (
        "## Inbox\n"
        "- [ ] a ([[2026-10-05 0900 Standup|10-05 standup]])\n"
        "\t- [ ] child of a\n"
        "- [ ] b ([[Team weekly#2026-09-28|09-28 weekly]])\n"
        "- [ ] c no date ([[Some note|2026-01-01 alias only]])\n"
        "## 2026-09-01\n"
        "- [ ] d under a dated heading\n"
        "- [ ] e [bad](2026-13-40.md) [link](Meetings/2026-08-17%20x.md)\n"
    )
    dates = [item.date for item in parse_todos(text, PLANS).items]
    # An invalid date is skipped; the next link's date wins over the heading's.
    assert dates == ["2026-10-05", "2026-10-05", "2026-09-28", None, "2026-09-01", "2026-08-17"]


def test_keys_are_stable_across_links_and_tags_and_unique_per_occurrence() -> None:
    one = parse_todos("- [ ] Fix the NLB\n- [ ] Fix the NLB\n", PLANS).items
    two = parse_todos("- [x] Fix the NLB [[net-iso]] #egress\n", PLANS).items
    assert one[0].key != one[1].key
    assert one[0].key == two[0].key


def test_first_seen_keeps_known_dates_and_drops_gone_items() -> None:
    todos = parse_todos("- [ ] old\n- [ ] new\n- [ ] dated ([[2026-10-05 x]])\n", PLANS)
    old, new, dated = todos.items
    seen = todos.first_seen({old.key: "2026-01-01", "gone": "2025-01-01"}, "2026-10-08")
    assert seen == {old.key: "2026-01-01", new.key: "2026-10-08"}
    assert todos.dates(seen, "2026-10-08") == ["2026-01-01", "2026-10-08", "2026-10-05"]
