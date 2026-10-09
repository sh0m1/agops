"""Read one user-owned Markdown todo list (for example an Obsidian `Todo/ALL.md`).

agops never writes the file. Items link plans with wikilinks such as `[[network-isolation]]`;
a link on a heading, a group line or a parent item applies to the items below it.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from typing import Any

MAX_TODO_BYTES = 1 << 20
DEFAULT_INBOX = "Inbox"

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_ITEM = re.compile(
    r"^(?P<ind>[ \t]*)(?:[-*+]|\d+[.)])[ \t]+\[(?P<mark>.)\](?:[ \t]+(?P<text>.*?))?[ \t]*$"
)
_BULLET = re.compile(r"^(?P<ind>[ \t]*)(?:[-*+]|\d+[.)])[ \t]+(?P<text>.*?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_URL_LINE = re.compile(r"^<?https?://\S+>?$")
_WIKILINK = re.compile(r"(!?)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|([^\]]*))?\]\]")
_MDLINK = re.compile(r"(!?)\[([^\]]*)\]\(<?([^)>\s]+\.md)(?:#[^)>]*)?>?\)")
_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"`[^`]*`")
_TAG = re.compile(r"(?<![\w/&#])#([\w/-]*[^\W\d_][\w/-]*)")
_ANCHOR_UNSAFE = re.compile(r"[\[\]|#^]")
_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


@dataclass(frozen=True)
class TodoItem:
    line: int
    text: str
    state: str
    depth: int
    headings: tuple[str, ...]
    group: str | None
    plans: tuple[str, ...]
    own_plans: tuple[str, ...]
    tags: tuple[str, ...]
    links: tuple[str, ...]
    parent: int | None
    in_inbox: bool
    anchor: str | None = None
    date: str | None = None  # from the item's source link, a parent item or a heading
    key: str = ""  # a hash of the normalized text, stable when links or tags change

    @property
    def open(self) -> bool:
        return self.state == "open"


@dataclass
class TodoList:
    file: str
    sha256: str
    items: list[TodoItem] = field(default_factory=list)

    def for_plan(self, plan_id: str, state: str | None = "open") -> list[TodoItem]:
        return [
            item
            for item in self.items
            if plan_id in item.plans and (state is None or item.state == state)
        ]

    def dates(self, first_seen: dict[str, str], today: str) -> list[str]:
        """Each item's date: its source date, else the day agops first saw it, else today."""
        return [item.date or first_seen.get(item.key) or today for item in self.items]

    def first_seen(self, known: dict[str, str], today: str) -> dict[str, str]:
        """The first-seen map to keep: one entry for each current item without a source date."""
        return {
            item.key: known.get(item.key, today) for item in self.items if item.date is None
        }

    def counts(self) -> dict[str, int]:
        open_items = [item for item in self.items if item.open]
        return {
            "open": len(open_items),
            "done": sum(item.state == "done" for item in self.items),
            "cancelled": sum(item.state == "cancelled" for item in self.items),
            "inbox": sum(item.in_inbox for item in open_items),
            "linked": sum(bool(item.plans) for item in open_items),
        }


@dataclass
class _Scope:
    plans: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


def _plan_target(target: str, plan_ids: Collection[str]) -> str | None:
    target = target.strip()
    name = posixpath.basename(target).removesuffix(".md")
    if name in plan_ids and ("/" not in target or "plans/" in target):
        return name
    return None


_Found = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]


def _scan(text: str, plan_ids: Collection[str]) -> _Found:
    """Plan ids, other link targets and tags in one line of text."""
    plans: list[str] = []
    links: list[str] = []
    for match in _WIKILINK.finditer(text):
        if match.group(1):
            continue
        plan = _plan_target(match.group(2), plan_ids)
        (plans if plan else links).append(plan or match.group(2).strip())
    for match in _MDLINK.finditer(text):
        if match.group(1) or "://" in match.group(3):
            continue
        plan = _plan_target(match.group(3), plan_ids)
        (plans if plan else links).append(plan or match.group(3))
    bare = _URL.sub(" ", _CODE.sub(" ", _MDLINK.sub(" ", _WIKILINK.sub(" ", text))))
    tags = _TAG.findall(bare)
    return tuple(dict.fromkeys(plans)), tuple(dict.fromkeys(links)), tuple(dict.fromkeys(tags))


def _valid_date(text: str) -> str | None:
    for match in _DATE.finditer(text):
        try:
            return Date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue
    return None


def _source_date(text: str) -> str | None:
    """The first date in a link target or anchor, such as a dated meeting note."""
    for match in _WIKILINK.finditer(text):
        if not match.group(1):
            found = _valid_date(match.group(0).split("|", 1)[0])
            if found:
                return found
    for match in _MDLINK.finditer(text):
        found = _valid_date(match.group(3))
        if found:
            return found
    return None


def _identity(text: str) -> str:
    text = _MDLINK.sub(r"\2", _WIKILINK.sub(" ", text))
    text = _TAG.sub(" ", _URL.sub(" ", text)).replace("→", " ")
    text = re.sub(r"\(\s*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .;:-").casefold()


def _clean_heading(text: str) -> str:
    return re.sub(r"[ \t]+#+[ \t]*$", "", text).strip()


def _drop_plan_links(text: str, plan_ids: Collection[str]) -> str:
    """Heading and group text for display: plan links are metadata, not part of the name."""

    def drop(match: re.Match[str]) -> str:
        return "" if _plan_target(match.group(2), plan_ids) else match.group(0)

    return re.sub(r"[ \t]{2,}", " ", _WIKILINK.sub(drop, text)).strip()


def _width(indent: str) -> int:
    return len(indent.expandtabs(4))


def _is_tag_line(text: str) -> bool:
    remainder = _TAG.sub(" ", _WIKILINK.sub(" ", text))
    return not remainder.strip() and bool(_TAG.search(text))


def parse_todos(
    text: str, plan_ids: Collection[str] = (), inbox: str = DEFAULT_INBOX, file: str = ""
) -> TodoList:
    """Parse checkbox items. Never raises; unknown shapes are context, not items."""
    data = text.encode("utf-8", errors="replace")
    result = TodoList(file=file, sha256=hashlib.sha256(data).hexdigest())
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() in {"---", "..."}:
                start = index + 1
                break
    headings: list[tuple[int, str, _Scope, str]] = []
    label: tuple[str, _Scope] | None = None
    tagline: tuple[str, _Scope] | None = None
    stack: list[tuple[int, _Scope, int | None]] = []
    fence: str | None = None
    inbox_name = inbox.strip().casefold()
    seen: Counter[str] = Counter()

    for number, line in enumerate(lines[start:], start + 1):
        fenced = _FENCE.match(line)
        if fence is not None:
            if fenced and fenced.group(1) == fence:
                fence = None
            continue
        if fenced:
            fence = fenced.group(1)
            continue
        if not line.strip():
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = _clean_heading(heading.group(2))
            plans, _, tags = _scan(title, plan_ids)
            headings = [entry for entry in headings if entry[0] < level]
            display = _drop_plan_links(title, plan_ids) or title
            headings.append((level, display, _Scope(plans, tags), title))
            label = tagline = None
            stack = []
            continue
        item = _ITEM.match(line)
        bullet = item or _BULLET.match(line)
        if bullet is None:
            stripped = line.strip()
            # Indented text continues an item; quotes, tables, rules and URLs are context.
            if line[0] in " \t" or stripped.startswith((">", "|")):
                continue
            if re.fullmatch(r"[-*_ ]{3,}", stripped) or _URL_LINE.match(stripped):
                continue
            plans, _, tags = _scan(stripped, plan_ids)
            display = _drop_plan_links(stripped, plan_ids) or stripped
            if _is_tag_line(stripped):
                tagline = (display, _Scope(plans, tags))
            else:
                label = (display, _Scope(plans, tags))
                tagline = None
            stack = []
            continue
        width = _width(bullet.group("ind"))
        while stack and stack[-1][0] >= width:
            stack.pop()
        body = (item.group("text") if item else bullet.group("text")) or ""
        plans, links, tags = _scan(body, plan_ids)
        scope = _Scope(plans, tags)
        if item is None or not body.strip():
            stack.append((width, scope, None))
            continue
        mark = item.group("mark")
        state = "open" if mark == " " else "cancelled" if mark == "-" else "done"
        chain = [entry[1] for entry in reversed(stack)]
        outer = [entry[1] for entry in (tagline, label) if entry is not None]
        outer.extend(entry[2] for entry in reversed(headings))
        effective: tuple[str, ...] = ()
        for candidate in (scope, *chain, *outer):
            if candidate.plans:
                effective = candidate.plans
                break
        scopes = (scope, *chain, *outer)
        all_tags = dict.fromkeys(tag for candidate in scopes for tag in candidate.tags)
        parent = next((entry[2] for entry in reversed(stack) if entry[2] is not None), None)
        group = " › ".join(entry[0] for entry in (label, tagline) if entry is not None) or None
        heading_titles = tuple(entry[1] for entry in headings)
        raw = headings[-1][3] if headings else None
        when = _source_date(body)
        if when is None and parent is not None:
            when = result.items[parent].date
        if when is None:
            when = next(
                (found for entry in reversed(headings) if (found := _valid_date(entry[3]))), None
            )
        identity = _identity(body)
        seen[identity] += 1
        key = hashlib.sha256(f"{identity}#{seen[identity]}".encode()).hexdigest()[:16]
        result.items.append(
            TodoItem(
                line=number,
                text=body.strip(),
                state=state,
                depth=len(stack),
                headings=heading_titles,
                group=group,
                plans=effective,
                own_plans=plans,
                tags=tuple(all_tags),
                links=links,
                parent=parent,
                in_inbox=any(title.casefold() == inbox_name for title in heading_titles),
                anchor=raw if raw and not _ANCHOR_UNSAFE.search(raw) else None,
                date=when,
                key=key,
            )
        )
        stack.append((width, scope, len(result.items) - 1))
    return result


def normalize_settings(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """The `todos` key of notes.yaml: a mapping, a bare file path, or absent (off)."""
    if raw is None or raw == {}:
        return None, []
    if isinstance(raw, str):
        raw = {"file": raw}
    if not isinstance(raw, dict):
        return None, ["notes.yaml: todos must be a mapping with a file path"]
    file = raw.get("file")
    inbox = raw.get("inbox", DEFAULT_INBOX)
    exclude = raw.get("sweep_exclude", [])
    if not isinstance(file, str) or not file.strip():
        return None, ["notes.yaml: todos.file must be a path relative to the vault root"]
    file = file.strip()
    parts = file.split("/")
    if (
        file.startswith(("/", "~"))
        or "\\" in file
        or ".." in parts
        or not file.endswith(".md")
    ):
        return None, [f"notes.yaml: todos.file {file!r} must be a vault-relative .md path"]
    if not isinstance(inbox, str) or not inbox.strip():
        return None, ["notes.yaml: todos.inbox must be a heading name"]
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        return None, ["notes.yaml: todos.sweep_exclude must be a list of glob patterns"]
    return {"file": file, "inbox": inbox.strip(), "sweep_exclude": exclude}, []


def resolve_todo_file(vault_root: Path, file: str, notes_target: Path | None = None) -> Path:
    """The todo file inside the vault. Raises ValueError for a path that escapes it."""
    root = vault_root.resolve()
    path = vault_root / file
    if path.is_symlink():
        raise ValueError(f"todos: {file} is a symlink")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"todos: {file} is outside the vault")
    if notes_target is not None and resolved.is_relative_to(notes_target.resolve()):
        raise ValueError(f"todos: {file} is inside the agops notes folder")
    return path


def load_todos(
    vault_root: Path,
    settings: dict[str, Any],
    plan_ids: Collection[str],
    notes_target: Path | None = None,
) -> TodoList:
    """Read and parse the configured file. Raises OSError or ValueError."""
    path = resolve_todo_file(vault_root, settings["file"], notes_target)
    if path.stat().st_size > MAX_TODO_BYTES:
        raise ValueError(f"todos: {settings['file']} is larger than {MAX_TODO_BYTES} bytes")
    data = path.read_bytes()
    parsed = parse_todos(
        data.decode("utf-8", errors="replace"), plan_ids, settings["inbox"], settings["file"]
    )
    parsed.sha256 = hashlib.sha256(data).hexdigest()
    return parsed


def file_link(file: str, label: str | None = None, anchor: str | None = None) -> str:
    target = file.removesuffix(".md")
    name = label or posixpath.basename(target)
    return f"[[{target}{'#' + anchor if anchor else ''}|{name}]]"


def heading_link(file: str, item: TodoItem) -> str:
    """`[[Todo/ALL#Platform|ALL › Platform › #egress #stockholm]]` for an item."""
    stem = posixpath.basename(file.removesuffix(".md"))
    path = [*item.headings, *([item.group] if item.group else [])]
    label = _plain(" › ".join([stem, *path]))
    return file_link(file, label, item.anchor)


def _plain(text: str) -> str:
    """Link syntax as plain words, so the text can sit inside a wikilink label."""

    def words(match: re.Match[str]) -> str:
        return (match.group(3) or posixpath.basename(match.group(2))).strip()

    text = _WIKILINK.sub(words, text)
    return re.sub(r"\s+", " ", re.sub(r"[\[\]|]", " ", text)).strip()


def display_text(item: TodoItem, drop_plan: str | None = None) -> str:
    """The item's text without its link to `drop_plan` (the plan whose note shows it)."""
    text = item.text
    if drop_plan is not None:

        def strip(match: re.Match[str]) -> str:
            return "" if _plan_target(match.group(2), {drop_plan}) else match.group(0)

        text = _WIKILINK.sub(strip, text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def plain_text(item: TodoItem, drop_plan: str | None = None) -> str:
    """Display text with links as plain words, for places that do not render Markdown."""
    return _plain(display_text(item, drop_plan))
