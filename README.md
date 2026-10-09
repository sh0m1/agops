# agops

agops is a Git-native shared memory and execution ledger for local coding agents. It gives
Codex, Claude Code, Cursor, Gemini CLI, and shell-capable agents one readable source of truth for
knowledge, approved plans, task claims, checkpoints, and completion evidence.

It does not call a model API and does not depend on a particular subscription. Git is the durable
store; a small CLI provides safe state transitions, and a stdio MCP server exposes the same
operations to clients that support MCP.

![Agents reach agops through MCP or the CLI. agops commits each event to the hub Git repository and pushes to an optional remote. agops setup writes the instruction files, and the notes bridge exports to a notes folder.](docs/architecture.svg)

## Install

One command on a machine that already runs Claude Code and/or Codex CLI:

```sh
curl -fsSL https://raw.githubusercontent.com/sh0m1/agops/main/install.sh | sh
```

It installs `uv` if missing, installs `agops` from the current main branch, and runs
`agops setup`, which ends with a summary of what was configured. The memory lives in a local
Git repository at `~/.local/share/agops/repo`; nothing leaves the machine.

To share the memory across machines, give the same command a Git remote — on the first machine
it pushes the existing local memory there, on the others it clones it:

```sh
curl -fsSL https://raw.githubusercontent.com/sh0m1/agops/main/install.sh \
  | sh -s -- --remote git@github.com:you/agops-memory.git
```

To go back to a machine-local memory, `agops setup --local` detaches and forgets the remote
(your local history is kept). Other flags: `--ref <ref>` to pick a tag, branch, or commit,
`--keep-claude-memory` to leave Claude Code's automatic memory on, and `--dry-run` to print
the commands without touching anything. Manual equivalent:
`uv tool install git+https://github.com/sh0m1/agops@main` then
`agops setup [--remote <url> | --local] [--profile NAME]`. Add `--runtime <path>` to keep the
profile's Git clone somewhere other than the default (`~/.local/share/agops/repo`, or
`~/.local/share/agops/<profile>` for a named profile).

`setup` adds bounded managed blocks to the Codex and Claude user instruction files and registers
the MCP server with whichever of `codex` and `claude` are on `PATH` (others are reported as
skipped, not errors). Existing configuration is preserved and backed up before it is changed.
Re-run the one-liner (or plain `agops setup`) to upgrade; the remote is remembered.
`agops doctor` and `agops scan` remain available for later health checks; `--json` gives
machine-readable output.

The remote URL is stored verbatim in `~/.config/agops/config.json` and echoed by `--dry-run`;
prefer SSH or a credential helper over embedding a token in the URL.

Add repository-level instructions for tools that do not load the user configuration:

```sh
agops adapter install /path/to/project --tools agents,claude,gemini,cursor,copilot
```

Only a marked managed block is added or replaced; existing project instructions are preserved.

## Standing preferences

House style, wording rules, and other standing instructions are recorded in the hub rather than
hand-edited into each client's instruction file:

```sh
agops knowledge add --scope global --kind preference \
  --key response-style --title "Response style" --body-file style.md
agops setup                 # regenerates the managed block from the hub
```

`setup` renders every active `preference` entry at `global` scope into the managed block of the
Codex and Claude instruction files, so the hub is the single source of truth and the files are
generated. Retiring the entry removes it on the next `setup`:

```sh
agops knowledge retire --scope global --key response-style --reason "superseded"
```

Only `global` scope is rendered. Those files are loaded for every session whatever directory it
starts in, so a `workspace:` or `project:` preference would leak into unrelated work; scoped
preferences reach agents through the brief and the hook below, which resolve scope per session.

Keep an entry under 500 characters — the brief excerpts longer bodies.

## Hook

`hooks/agops-hook.mjs` reminds an agent to record durable state before it stops, and delivers
preferences mid-session. Register it with the events your client supports, pointing at the file in
this checkout — for Claude Code in `~/.claude/settings.json` under `hooks`, for Codex in
`~/.codex/hooks.json`:

```json
{ "type": "command", "command": "node \"$HOME/path/to/agops/hooks/agops-hook.mjs\"", "timeout": 10 }
```

Register it for `SessionStart`, `PostToolUse`, `Stop`, `SessionEnd`, and `UserPromptSubmit`. On
`Stop` it blocks once when the session edited files in a registered project without recording
anything in the hub, and releases after one continuation so it cannot loop; an agent that has
nothing durable to record ends its message with `Hub impact: none — <reason>`. On
`UserPromptSubmit` it injects the preferences whose scope covers the session, gated on a digest so
unchanged text is not re-sent every turn. Preferences are read from disk per event, so
`agops knowledge add` reaches the next prompt without restarting the client.

Routing resolves through the hub's registered projects, so run `agops project register <path>
[--workspace NAME]` for the repositories you want watched. A hook defect never blocks a session:
errors are reported and the event is allowed. `AGOPS_HOOK_STATE_DIR` overrides where per-session
state is kept.

## Team hub and private sessions

A machine can hold several hubs, called profiles. A typical pair is a `team` hub shared through
a remote and a `private` hub that never leaves the machine:

```sh
agops setup --profile team --remote git@github.com:you/team-memory.git --default
agops setup --profile private --local
```

Sessions use the default profile unless the terminal says otherwise:

```sh
AGOPS_PROFILE=private claude     # this session reads and writes only the private hub
claude                               # this one uses the team hub
```

The variable reaches the MCP server that Claude Code or Codex starts, so it applies to agent
sessions, not only to shell commands. The brief's header always names the hub
(`Hub: private · local only`). An unknown profile is an error rather than a fallback to the
default, and if `AGOPS_REPO` is also set the brief warns that it overrides the profile.
`agops profile list` shows the profiles, the default, and the one the current terminal
resolves to; `agops profile default NAME` changes the default.

Upgrading from an earlier version: re-run `agops setup` once so the MCP registration stops
pinning a hub path; `agops doctor` reports `mcp_pinned` until you do. An install made before
the tool had its current name is migrated by the same `setup` run. It moves the old config,
data and state directories, rewrites the managed blocks, and removes the old MCP registration.
It deletes no data: if an old and a new path both exist, it leaves both untouched and warns.
`agops doctor` reports `legacy_install` until nothing is left to move.

## Everyday workflow

With Claude Code or Codex there is nothing to type. The managed instruction block tells the agent
to call `hub_get_brief` at session start, claim a task before writing, checkpoint progress, and
complete with evidence — all through the MCP server that `setup` registered. You approve plans
and edit the tier policy; the agents do the rest.

From a shell — for humans, or for agents without MCP — the same contract is four commands. The
session id defaults to one stable for the terminal, so nothing needs exporting:

```sh
agops brief --cwd "$PWD" --model gpt-5.6-terra
agops task claim PLAN TASK --cwd "$PWD"
agops task checkpoint PLAN TASK --summary "Implemented parser" --evidence "pytest: 12 passed"
agops task complete PLAN TASK --evidence "commit: abc123" --evidence "pytest: 12 passed"
```

![Task life cycle: a human approves a draft plan; a claim checks the tier and takes a 60-minute lease; checkpoints renew it; completion needs evidence; block, unblock, release and lease expiry return a task to ready.](docs/task-lifecycle.svg)

`agops plan list` and `agops task ready [--tier NAME]` show what is available, and
`agops plan show PLAN [--revision N]` prints one plan's definition. The other task transitions:

```sh
agops task block PLAN TASK --reason "Waiting for VPC peering"  # owner stops; task is blocked
agops task unblock PLAN TASK --resolution "Peering approved"   # task is ready again
agops task release PLAN TASK                                   # owner gives the claim back
```

Set `AGOPS_ACTOR` to name the agent and `AGOPS_SESSION` to pin a session id explicitly;
`agops session` prints a new id (`--value` prints the id only). `AGOPS_MODEL`, when set, takes
precedence over `--model` for `brief` and `run`. Per-session records, such as the declared
model, are kept in `~/.local/state/agops/sessions`; `AGOPS_STATE_DIR` moves the parent directory.

`agops search "words"` ranks the knowledge entries, archives included, by how often the words
appear in them. `agops migrate-remember PATH --project ID` imports the Markdown files of an old
`.remember` journal into one `archive` entry of that project. A second import supersedes the
first.

Tasks carry a `tier` (default `standard`); `memory/policy/tiers.yaml` maps model ids to tiers.
Frontier models plan and review, cheaper models execute, and the hub rejects claims that cross
tiers. The shipped patterns lead with a wildcard so they also match the prefixed ids that provider
routes report (Bedrock sends `us.anthropic.claude-opus-5[1m]`); a model no pattern matches resolves
to the `unknown` tier, which rejects every claim, and the brief says so in its header.
`agops policy show` prints the tiers and the tier of the current session; `agops policy validate`
only checks the file. A human can bypass the tier check with
`agops task claim PLAN TASK --allow-tier-mismatch`. It asks for the task id, and it is refused under
`agops run`, in non-interactive terminals, and through MCP. See
[docs/protocol.md](docs/protocol.md#execution-tiers).

To launch a tool with a task already claimed and the lease renewed while it runs, use the managed
wrapper. Its options precede the tool name:

```sh
agops run --plan PLAN --task TASK --cwd /path/to/worktree --model gpt-5.6-terra codex
```

Plans are drafted from YAML and become executable only after interactive approval:

```sh
agops plan draft plan.yaml
agops plan approve my-plan
```

`agops plan cancel my-plan --reason "..."` retires a plan, active or still a draft; it is
interactive like approval. Two optional task fields only change how notes read: `summary`, a
one-line gist shown first when the task is unfolded (otherwise the first sentences of `notes`
or `instructions` are), and `phase`, which groups a plan note's tasks into sections in the order the
phases first appear.

## Notes apps (Obsidian, Logseq, or any Markdown folder)

Connect one dedicated folder to a hub profile. The folder contains ordinary portable Markdown, so
Obsidian discovers it immediately and another notes app can use it too:

```sh
agops notes connect ~/Notes/agops
agops notes status
agops notes sync
```

The folder holds the whole working picture: `Home.md`, `Plans.md` and the plan notes
(`plans/active/<id>.md` while a plan is open, `plans/archive/<workspace>/<id>.md` once it is
completed or cancelled; agops moves a note only when it has no unsynced edit),
`knowledge/<scope>/<key>.md` with a `Knowledge.md` index for the knowledge entries, `Projects.md`
for the repositories that have plans or knowledge (the rest folded), `Activity.md` for who has
claimed what, what is blocked, what is ready next, and the last 30 days of events, and `Docs.md`
for long-form documents mirrored from folders you choose. Only plans are two-way; knowledge,
projects, activity, and docs are a read-only mirror, so change knowledge with
`agops knowledge add` rather than in the vault. Personal notes (the "My notes" section) are
preserved in every note.

A plan note reads top-down: the goal's lead sentences (the full goal folds below), links to its docs
and projects, what needs you (a pending revision lists the tasks it adds, removes, or changes), then
the tasks once each, grouped by `phase` or else by where they stand (in progress, blocked, next,
waiting on other tasks, done). Each task is a folded Obsidian callout coloured by status (`todo`
open, `tip` in progress, `failure` blocked, `done` complete); unfold it for the gist, then the rest
of its text, the done-when line, project, tier and dependencies, and the latest checkpoint. Below
come acceptance criteria, related knowledge (entries linked to the plan or mentioning it), and the
plan-level details. The plan definition itself lives in a hidden file,
`plans/.definitions/<id>.yaml`; edit that, not the note. Agops owns these plain frontmatter keys and
rewrites them on every export: plan notes `tags`, `status`, `health`, `workspace`, `progress`,
`last_activity`, `topics`; knowledge notes `tags`, `kind`, `scope`, `updated`, `plan`, `project`,
`topics`. Your own keys and tags (such as `keep: true`) are kept; `topic/*` tags are agops-owned.
Inside an Obsidian vault the `plan` property is a full-path link, so a mirrored doc with the same
file name never captures it. Ids, revisions and hashes are in `.agops-notes.json`. Notes from the
older layout (an `agops_*` frontmatter and a YAML block in the note) are converted on the next sync;
an unsynced edit in the old block moves to the `.yaml` file.

Agops exports after successful hub changes and after `agops sync`. Editing a plan definition is safe
but intentional: `agops notes sync` validates `plans/.definitions/<id>.yaml` and creates a new
**unapproved** plan revision. Personal notes and your own frontmatter are retained and never
imported. Finished or cancelled plans are history only. If agops and a note changed the same
definition, resolve it deliberately: `agops notes resolve PLAN --take notes|agops` (type the plan
id, or add `--yes`). `agops notes disconnect` only forgets the folder; it never deletes notes.

The sidecar is bound to its hub, so a folder belonging to another profile cannot be connected by
mistake. A notes write failure is an additive `notes_warning`: the hub event still succeeds and the
next sync retries it. When a newer revision is awaiting approval, the note lists the tasks of the
approved revision and says "Revision N is waiting for approval"; the `.yaml` file holds the newer
definition.

`Home.md` is the overview: a one-line tally; what needs you (pending approvals, blocked tasks,
stalled plans, drafts to approve or cancel); one table of open plans per workspace with status,
progress, the next task, and last activity; the last 7 days of events; the newest knowledge; and
recently completed work. Every plan note and `Plans.md` carry a computed `health` (`live`,
`waiting`, `blocked`, `stalled` after 7 idle days, `done`, `cancelled`, or `draft`), plus its
workspace and progress, so the vault sorts and filters instead of just listing. `agops.base` is an
Obsidian Bases file with ready-made views (open plans including drafts, stalled, by workspace,
recently completed, knowledge, decisions and preferences, plan-linked facts) that filter on the
`agops/plan` and `agops/knowledge` tags; agops writes it once and never overwrites your edits (an
untouched earlier default is upgraded; a copy that still uses the old `agops_*` properties is kept
as `agops.old.base` and replaced by the new default).

`notes.yaml` in the folder is yours: agops writes a commented template once and only reads it after
that. `docs:` lists folders whose Markdown and images are copied read-only into `docs/<folder>/` on
every sync, so vault search and plan notes reach long-form documents. HTML files are mirrored too,
as pages: an agent's web page with diagrams sits beside the docs, keeps its name, takes its title
from `<title>`, and opens in the browser from Obsidian. A plan links the docs and pages named
`<plan-id>.*` or `<plan-id>-*` and any whose path its definition mentions, on separate `Docs:` and
`Pages:` lines; `Home.md` lists the newest pages. Each Markdown copy carries its folder in its name
(`workstreams/x.md` becomes `workstreams/x (workstreams).md`, with links between the docs
rewritten), so a doc named after a plan never captures a `[[plan-id]]` link. Dot files, symlinks,
and files matching credential patterns are skipped, and a copy edited in the vault is kept and no
longer refreshed. `topics:` maps a topic name to project-id globs and keywords; a note gets
`topic/<name>` when one of its projects matches or its title or key has a keyword, and
`Knowledge.md` groups entries by topic (untagged ones by scope). Files an earlier layout generated
(`views/*.base` on `agops_*` properties, `projects/*.md` notes with `agops_type: project`) are
removed on sync; a project note with personal notes is kept and reported. `agops notes review`
prints a read-only JSON triage report - stale or blocked plans and which knowledge entries are safe
to retire - for a human or the `agops-notes` skill to act on. Add `keep: true` to a knowledge note's
frontmatter to mark it kept; `agops notes review` then reports it as `keep` instead of flagging it
again.

`todos:` names one Markdown todo list in the vault, for example `todos: {file: Todo/ALL.md}` (the
path is relative to the folder with `.obsidian`). It is the only file outside the notes folder
that agops reads, and agops never writes it or stores its text: the sidecar keeps only counts, a
hash, and first-seen dates keyed by hashes of the item text. An item links a plan with a bare wikilink such as `[[network-isolation]]`, and a link on a
heading, a tag-only group line (`#egress #stockholm`), a label line or a parent item applies to
the items below it; the nearest link wins. Each item has a date: the first date in its source
link (a dated meeting note or a `Team weekly#2026-10-05` section), else its parent's, else its
heading's, else the day agops first saw it. Each plan note then gets a `## Todos` section after
"Needs you" with the open items that link it, newest first with their dates, as plain bullets
with a link back to their heading (never checkboxes, so you tick only in the todo file). `Home.md` shows the counts, `agops notes
status` reports `todos.stale` when the file changed after the last render, `agops notes review`
lists the Inbox (`inbox:`, default `Inbox`) and open items that link finished plans, and
`agops brief` lists up to 10 open items that link open plans (the current project's plans
first, then newest first), read live and labelled as the user's data. The section refreshes on each hub change and on `agops notes sync`. An unreadable
or invalid file is a warning, never a failed render.

## Skills

`agops setup` installs the packaged `agops-notes` skill for both Claude Code and Codex
(`~/.claude/skills/` and `~/.codex/skills/`), so either agent can triage the notes vault - see
"Notes apps" above. Re-run just this step with `agops skills install`. It never overwrites a
skill directory you edited by hand (no `managed-by: agops` marker in its `SKILL.md`); such a
directory is reported "skipped (unmanaged)" instead. With `todos:` set, the skill also triages
the todo Inbox and proposes to sweep stray open checkboxes from other notes into it; it changes
the todo file only after you confirm.

## Tool access order

The managed instruction block tells agents to try a CLI or MCP tool first for any external system,
and to fall back to a browser only when no CLI or MCP route exists, it is scoped wrong, it fails, or
it cannot answer the question. The browser stays allowed; it is just not the first choice, and the
agent should say why it fell back.

See [docs/protocol.md](docs/protocol.md) for schemas, state transitions, concurrency semantics, and
the generic agent integration contract.
