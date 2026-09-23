---
name: agops-notes
description: Organize and review the agops notes mirror (Obsidian or any Markdown vault) — see what is really active, retire progress-log knowledge from finished plans, propose closing stalled or superseded plans, and keep the vault easy to scan. Use when the user asks to tidy, organize, clean up, triage or get an overview of agops notes, plans or knowledge, or asks "what's active" / "what is stale" in the hub.
metadata:
  managed-by: agops
---

# agops notes: overview and triage

The notes folder is a mirror of the agops hub. The hub is the truth; the mirror is a view.
Change hub state only with the `agops` CLI or the agent-hub MCP tools, never by editing
mirrored files. Keep one stable `--session` for every command in a run (`agops session`).

## Map of the folder

- `Home.md` — start here: tally, what needs the user, active plans by last activity, views.
- `agops.base` — Obsidian Bases views (Active plans, Stalled, By workspace, Recently
  completed, Knowledge, Retire candidates). Written once; the user may customize it.
- `Plans.md`, `Knowledge.md`, `Activity.md`, `Projects.md` — generated indexes.
- `plans/<id>.md` — two-way: the fenced YAML definition imports as a new *unapproved*
  revision on `agops notes sync`. `knowledge/**` — read-only mirror.
- Frontmatter `agops_*` fields are generated (`agops_health`, `agops_last_activity`,
  `agops_workspace`, task counts). Users may add their own frontmatter and write inside the
  `agops:personal` markers; both are preserved.

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
   Never retire `decision`, `preference` or `archive` entries on your own.
4. **Knowledge, ask:** list `verdict: review` entries in one short table. Offer to fold a
   plan's surviving log entries into one `archive` entry (`agops knowledge add --kind
   archive ... `) and retire the originals — only after the user agrees.
5. **Plans, ask:** from `plans`, list `stalled` and `blocked` plans, plus any plan you judge
   superseded (a later plan covers the same subject — compare titles, goals and blocked
   reasons). For each, give one line: plan, idle days, why, and the proposed action:
   - cancel: `agops plan cancel <id> --reason "<why>" --yes`
   - keep: it is waiting on something real (name it)
   Run cancellations only after the user confirms, one list, one confirmation.
   Never cancel a plan with a `live` task. Never complete tasks without real evidence.
6. `agops notes sync` again and report in 3–5 lines: knowledge N → M, active plans N → M,
   what still needs the user, and the link `Home.md`.

## Keeping it tidy

- Knowledge is for durable facts, decisions and preferences. Progress belongs in task
  checkpoints (`agops task checkpoint`), not in new knowledge entries.
- Supersede instead of piling up: `agops knowledge add --supersedes <key>`.
- Personal thoughts go between the `agops:personal` markers of any note.
- Never delete or move files in the mirror by hand; retire or cancel in the hub and sync.
- In Obsidian: pin `Home.md`, open `agops.base` for sortable tables, filter by tag `agops`.
