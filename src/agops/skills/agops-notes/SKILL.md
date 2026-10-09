---
name: agops-notes
description: Organize and review the agops notes mirror (Obsidian or any Markdown vault) — see what is really active, retire progress-log knowledge from finished plans, propose closing stalled or superseded plans, and keep the vault easy to scan, and triage the todo Inbox and sweep stray checkboxes into the todo list. Use when the user asks to tidy, organize, clean up, triage or get an overview of agops notes, plans, knowledge or todos, or asks "what's active" / "what is stale" in the hub.
metadata:
  managed-by: agops
---

# agops notes: overview and triage

The notes folder is a mirror of the agops hub. The hub is the truth; the mirror is a view.
Change hub state only with the `agops` CLI or the agops MCP tools, never by editing
mirrored files. Keep one stable `--session` for every command in a run (`agops session`).

## Map of the folder

- `Home.md` — start here: tally, what needs the user (approvals, blocked, stalled, drafts),
  open plans per workspace with their next task, the last 7 days, the newest knowledge.
- `agops.base` — Obsidian Bases views (Open plans, Stalled, By workspace, Recently
  completed, Knowledge, Decisions and preferences, Plan-linked facts). Written once; the
  user may customize it.
- `Plans.md`, `Knowledge.md` (by topic, then scope), `Activity.md` (claims, blockers, ready,
  last 30 days), `Projects.md` (projects with plans or knowledge), `Docs.md` — generated
  indexes.
- `notes.yaml` — the user's settings: `docs:` folders mirrored read-only into `docs/`
  (Markdown docs and HTML pages; a plan links those named after it), and `topics:` rules that
  tag notes `topic/<name>`, and `todos:` (the user's single todo file outside this folder,
  read only by agops; plan notes get a generated `## Todos` section from it). Never edit it
  without being asked.
- `plans/active/<id>.md` (open plans) and `plans/archive/<workspace>/<id>.md` (completed or
  cancelled; moved automatically unless its definition has an unsynced edit) — readable
  pages. The plan definition is `plans/.definitions/<id>.yaml` (hidden in Obsidian): edits
  there import as a new *unapproved* revision on `agops notes sync`. `knowledge/**` —
  read-only mirror.
- Frontmatter `status`, `health`, `workspace`, `progress`, `last_activity`, `topics` (plans)
  and `kind`, `scope`, `updated`, `plan`, `project`, `topics` (knowledge) are generated, as
  are the `agops` and `topic/*` tags. Users may add
  their own frontmatter, including `keep: true` on a knowledge note, and write under
  "My notes" (inside the `agops:personal` markers); both are preserved. Ids and hashes live
  in `.agops-notes.json`.

Health, as of the last sync: `live` (a task has an active lease), `waiting` (recent
activity, nobody working), `blocked` (every open task is blocked), `stalled` (no live claim
and idle 7+ days), `done`, `cancelled`, `draft`.

## Triage run

1. `agops notes sync`, then `agops notes status`. If a plan is `edited` or `conflict`,
   report it and stop for that plan; the user resolves with
   `agops notes resolve PLAN --take notes|agops`.
2. `agops notes review` (JSON). Note the counts before changes.
3. **Knowledge, automatic:** retire every entry with `verdict: retire_safe` (a `fact` whose
   plan is completed or cancelled, no personal notes, 3+ days old):
   `agops knowledge retire --scope <scope> --key <key> --reason "<reason from review>" --session <S>`.
   Never retire `decision`, `preference` or `archive` entries on your own. Skim each title
   first: if it states a lasting rule or warning ("don't", "always", "must", a standing
   config fact), treat it as `review` instead.
4. **Knowledge, ask:** list `verdict: review` entries in one short table. Offer to fold a
   plan's surviving log entries into one `archive` entry (`agops knowledge add --kind
   archive ... `) and retire the originals — only after the user agrees. For an entry the
   user wants to keep, add `keep: true` to that note's frontmatter (any key not
   listed above); it is preserved and makes `review` report `keep`.
5. **Plans, ask:** from `plans`, list `stalled` and `blocked` plans, drafts nobody will
   approve, plus any plan you judge superseded (a later plan covers the same subject — compare titles, goals and blocked
   reasons). For each, give one line: plan, idle days, why, and the proposed action:
   - cancel: `agops plan cancel <id> --reason "<why>" --yes`
   - keep: it is waiting on something real (name it)
   Run cancellations only after the user confirms, one list, one confirmation.
   Never cancel a plan with a `live` task. Never complete tasks without real evidence.
6. **Todos, ask** (only when `review` has a `todos` key; skip on `error`): the file is the
   user's, so propose and wait for one confirmation before any edit.
   - Inbox: one table with each Inbox line, a target heading or group in the file, and a
     `[[plan-id]]` when a plan in `Plans.md` clearly matches. Keep the source link: its date
     orders the item. Move a confirmed line with two Edits (remove, then insert under the
     target), keeping its text and checkbox state. The Inbox stays newest first.
   - `linked_to_finished`: propose a new plan link or none. Only the user ticks items.
   - Sweep: search the vault for open checkboxes (`- [ ]`) outside the todo file, this
     folder, `.obsidian/`, `.trash/`, `sweep_exclude` and notes the user marked personal.
     Propose to move each into the Inbox, at its date position (newest first), as
     `- [ ] <text> ([[<note>#<YYYY-MM-DD section, if any>|<MM-DD short>]])`, unless
     the todo file already has an open item on the same subject, and to change the original
     to `- <text> → [[<todo file name>]]`. Leave `[x]` items alone.
   - Edit rules: read the file right before each Edit, anchor inserts on the Inbox heading
     line, never use Write, never tick, reorder or delete other lines, and read the file again
     to confirm. Then run `agops notes sync`.
7. `agops notes sync` again and report in 3–5 lines: knowledge N → M, active plans N → M,
   todos (open, Inbox), what still needs the user, and the link `Home.md`.

## Keeping it tidy

- Knowledge is for durable facts, decisions and preferences. Progress belongs in task
  checkpoints (`agops task checkpoint`), not in new knowledge entries.
- Supersede instead of piling up: `agops knowledge add --supersedes <key>`.
- Personal thoughts go between the `agops:personal` markers of any note.
- Never delete or move files in the mirror by hand; retire or cancel in the hub and sync.
- Open work lives only in the todo file. Elsewhere, write it as a bullet that links the file.
- In Obsidian: pin `Home.md`, open `agops.base` for sortable tables, filter by tag `agops`.
