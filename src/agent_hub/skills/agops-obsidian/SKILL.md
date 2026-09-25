---
name: agops-obsidian
description: Keep the Obsidian vault connected to agops in order and answer from it. Use when the user asks for an overview of their work, plans, tasks or knowledge; mentions Obsidian, the vault, or their notes; wants meeting notes, TODOs or ideas filed; or asks to tidy, structure or clean up notes.
---
<!-- agops:managed-skill — installed by `agops setup`; edit it in the agops repository -->

# Obsidian vault with agops

The vault has two zones. The **agops zone** is the folder `agops notes` is connected to; agops
generates it from the hub. The **personal zone** is everything else in the vault, and it belongs
to the user. Most mistakes come from treating one zone like the other.

## Locate the vault

Run `agops notes status`. Its `target` is the agops zone. The vault root is the nearest
ancestor of the target that contains `.obsidian/`. If `connected` is false, offer
`agops notes connect <vault>/agops` and wait for the user's go-ahead; never connect a folder
that already holds personal notes.

## The agops zone: read it, do not rearrange it

| Path | Who writes it | What you may change |
| --- | --- | --- |
| `Home.md` | agops | Nothing. It is the overview; refresh it with `agops sync`. |
| `Plans.md`, `Knowledge.md`, `Projects.md` | agops | Nothing. |
| `plans/<id>.md` | two-way | The fenced YAML definition (then `agops notes sync`, which drafts an **unapproved** revision), the personal-notes region, and non-`agops_` frontmatter. |
| `knowledge/<key>.md` (or `<key>--<scope>.md` when the key is not unique) | agops | The personal-notes region only. Change the entry with `agops knowledge add` or `agops knowledge retire`. |
| `projects/<id>.md`, only for repositories with plan tasks or knowledge | agops | The personal-notes region only. Writing personal notes into one keeps it even when the work is gone. |
| `views/*.base` | seeded by agops | Anything. Once the user customizes a view, agops stops updating it; deleting it restores the default. |
| `.agops-notes.json` | agops | Never touch. |

Never move, rename, or delete a file in the agops zone, and never edit text between the
`agops:` markers except as the table allows. After editing a plan note, run
`agops notes status`; `edited` means the change is waiting for `agops notes sync`, and
`conflict` needs `agops notes resolve <plan> --take notes|agops`, which is the user's call.

## Giving an overview

1. Refresh: `agops notes sync` if a plan note may have been edited by hand, otherwise
   `agops sync`.
2. Read `Home.md`. It already answers the usual questions: what needs a human (draft plans,
   pending approvals, blocked tasks, expired claims, notes agops could not refresh), what is
   in progress, what is ready, open plans with progress, recent knowledge, and which
   repositories have tracked work.
3. Follow its links into the plan, project, or knowledge note only as far as the question
   needs. Do not paste whole notes back.
4. If the user also wants their own open items, count unchecked tasks in the personal zone
   (see "Useful commands") and report them per file, oldest dated section first.
5. Answer conclusion first: the one or two things that need the user, then what is moving.
   Say which parts came from the hub and which from personal notes.

## The personal zone: a structure that stays small

Propose this layout when the user asks for structure. Create folders only as notes need them.

```
Home.md                 landing page: links to each area, embeds ![[agops/Home]]
Inbox.md                quick capture; emptied during a tidy
Todo/<Area>.md          one running list per area of work (Airflow, Platform, ...)
Meetings/<Series>.md    one note per recurring meeting, newest dated section at the top
Topics/<Topic>.md       lasting reference notes (a system, a design question, a vendor)
Archive/                finished or dead notes; nothing is ever deleted
```

Conventions to keep the vault navigable:

- Dated sections use ISO headings, `## 2026-09-21`. A line like `#21-09` is parsed by
  Obsidian as a tag, not a heading, so offer to convert such markers, but only with approval.
- Link to agops notes by file name, which is unique: `[[network-isolation]]` for a plan,
  `[[jeppesen-foreflight-airflow|airflow]]` for a repository. A repository without tracked work
  has no note yet (it is listed in `Projects.md`), so a link to it stays unresolved until it does. The link then shows up under
  "Linked mentions" on the agops note, which is how personal notes and hub state meet.
  Every agops note carries its title or repository name as an alias, so `[[` autocomplete
  finds it by that name too.
- Tags name themes, not dates or statuses: `#egress`, `#airflow/obo`.
- Keep the user's words and language exactly. Structure notes; never translate, reword,
  summarize in place, or tick and untick checkboxes on your own.
- Notes about people (candidate assessments, performance, HR) stay in the personal zone.
  Never copy them into agops knowledge, the hub, a commit, or any shared file.

## Tidying the personal zone

Moving notes is visible on every device the vault syncs to, so the plan is always approved
before any file moves.

1. **Inventory.** List every Markdown file outside the agops zone and `.obsidian/` with its
   size, last change, open and done checkbox counts, and top-level headings.
2. **Propose.** For each file give the current path, the proposed path, and a one-line
   reason. Also list suggested splits (for example a TODO list that contains a meeting
   series), merges, and notes to archive. Suggest the personal `Home.md` if there is none.
3. **Wait for approval.** Apply only the approved rows. Split and merge only with explicit
   approval for that note, and keep every line of the original text.
4. **Move with link updates.** Prefer `obsidian move path="<old>" to="<new folder or path>"`,
   which updates links. Without the CLI, move the file, then rewrite `[[Old name]]`,
   `[[Old name|`, `[[Old name#` and `](Old%20name.md)` links across the vault.
5. **Verify.** The Markdown file count is unchanged, `obsidian unresolved` (or a search for
   the old names) finds nothing new, and nothing in the agops zone changed.
6. **Promote, if the user agrees.** A durable fact that agents need (a decision, a verified
   system property) can become `agops knowledge add --scope ...`; keep it under 500
   characters and free of secrets, customer data, and personal data.

## Useful commands

Obsidian 1.12 and later ship a command-line interface. It is off by default: Settings →
General → Advanced → Command line interface, and the app must be running. On macOS it is
`obsidian` on PATH or `/Applications/Obsidian.app/Contents/MacOS/obsidian-cli`. Arguments
are `key=value`; run it with no arguments to list commands. Prefer it for moves and link
checks, and fall back to plain file operations when it is disabled; say which you used.

- `obsidian tasks todo verbose` lists open checkboxes grouped by file.
- `obsidian move path="TODO Airflow.md" to="Todo/Airflow.md"` moves and relinks.
- `obsidian unresolved` and `obsidian orphans` check link health after a tidy.
- `obsidian backlinks file="network-isolation"` shows which personal notes mention a plan.
- `obsidian base:query path="agops/views/Plans.base" view="Open plans" format=md` renders a
  Bases view as text.

Without the CLI, count open tasks with a search for lines starting with `- [ ]` in the
personal zone, excluding the agops zone and `.obsidian/`.
