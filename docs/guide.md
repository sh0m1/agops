# agops guide: concepts and input syntax

Audience: engineers who run or integrate agops. Reference for what each command expects and
how the pieces relate. Derived from `src/agops/*.py`; where the README or `protocol.md` differ
from the code, the code is described and the difference is flagged under
[Known gaps](#known-gaps).

Contents

1. [Concepts](#1-concepts)
2. [Identifiers and naming](#2-identifiers-and-naming)
3. [Knowledge](#3-knowledge)
4. [Projects and workspaces](#4-projects-and-workspaces)
5. [Plans: authoring, revisions, approval](#5-plans)
6. [Tasks: claim to completion](#6-tasks)
7. [Notes vault and docs folders](#7-notes-vault-and-docs-folders)
8. [Sessions, actors, models, tiers](#8-sessions-actors-models-tiers)
9. [File formats](#9-file-formats)
10. [Command reference](#10-command-reference)
11. [Environment variables](#11-environment-variables)
12. [Known gaps](#known-gaps)

---

## 1. Concepts

| Term | What it is | Stored in |
|---|---|---|
| **Hub** | A git repository (the *runtime clone*) holding all shared state under `memory/`. Optional `origin` remote makes it shared. | `~/.local/share/agops/repo` or `~/.local/share/agops/<profile>` |
| **Profile** | A named hub on this machine (`team`, `private`, ...). Selected by `AGOPS_PROFILE`, else the default. | `~/.config/agops/config.json` |
| **Event** | Immutable JSON record of one state transition (claim, checkpoint, approval...). Plan/task state is *derived* by replaying events. | `memory/events/YYYY/MM/DD/<id>.json` |
| **Knowledge** | Short durable statements (fact / decision / preference / archive), versioned per key. | `memory/knowledge/<scope>/<key>/<id>.md` |
| **Plan** | YAML definition with tasks. Every edit is a new immutable *revision*. Executable only after human approval. | `memory/plans/<id>/revisions/NNNN.yaml` |
| **Task** | Unit of work inside a plan. Claimed with a 60-minute lease, checkpointed, completed with evidence. | state in events |
| **Project** | A git repository, identified by its normalized `origin` URL. | `memory/projects/<project-id>.yaml` |
| **Workspace** | A free-form group name for projects. See [section 4](#4-projects-and-workspaces). | field on the project file |
| **Notes vault** | Optional Markdown folder (Obsidian etc.) that mirrors the hub for humans. Only plan definitions flow back. | folder you choose |
| **Brief** | Bounded text (max 8,000 bytes) an agent reads at session start: project, active plans, open tasks, knowledge. | generated |

Data flow:

```
 agent / human --CLI or MCP--> hub (git: memory/)  --render-->  notes vault (Markdown)
                                   ^                                  |
                                   +---- agops notes sync <-----------+
                                         (plan definitions only)
```

The hub is the authority. Nothing in the vault is trusted except an edited plan definition,
and that only becomes a new *unapproved* revision.

---

## 2. Identifiers and naming

| Thing | Rule |
|---|---|
| Plan id, knowledge key | Slugged: lowercased, runs of non-`[a-z0-9]` become `-`, trimmed, max 96 chars. `"My Plan!"` becomes `my-plan`. Commands slug plan ids too, so case does not matter. |
| Task id | Used as written (not slugged). Must be unique within the plan. Keep stable across revisions: state is keyed by task id. |
| Profile name | `^[a-z0-9][a-z0-9_-]{0,63}$` |
| Tier name | Slug form, not `unknown` |
| Project id | Derived from the origin URL: last two path segments, slugged. `git@github.com:sh0m1/agops.git` becomes `sh0m1-agops`. |
| Knowledge scope | `global`, `project:<project-id>` or `workspace:<name>` (see [3](#3-knowledge)) |

---

## 3. Knowledge

### What belongs here

Short statements an agent should have in context in later sessions: a decision and its reason,
a constraint, a convention, a pointer to a longer document. Not progress logs (use task
checkpoints), not long-form documents (see [section 7](#7-notes-vault-and-docs-folders)).

Soft limit 500 bytes per body (a warning is printed, the entry is still stored). Hard limit
256 KiB. Content matching credential patterns (private keys, `sk_`/`ghp_`/`AKIA...` tokens,
`api_key=...`) is rejected.

### Kinds

| Kind | Meaning | In the brief | In `agops search` |
|---|---|---|---|
| `fact` (default) | Something true now | Body, for the current project and its workspaces; title only for global | yes |
| `decision` | A choice made, with rationale | same as fact | yes |
| `preference` | Standing instruction | Global ones are **not** repeated (they are rendered into `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` by `agops setup`) | yes |
| `archive` | Historical material | never | only with `--archives` |

Only `global` + `preference` is written into instruction files. Scoped preferences reach
agents through the brief and the hook (`hooks/agops-hook.mjs`).

### Scopes and resolution

An entry applies to a session according to the session's project (derived from `cwd`):

```
global  <  workspace:<name> (each workspace the project belongs to)  <  project:<id>
```

Later scopes win on the same `key`. The scope string is not validated against registered
projects or workspaces. A typo creates an entry that never applies anywhere. `search --cwd`
ranks entries that apply to that directory first.

### `knowledge add`

```sh
agops knowledge add --scope SCOPE --key KEY --title TITLE --body-file FILE \
    [--kind fact|decision|preference|archive] [--supersedes ENTRY_ID] [--actor A] [--session S]
```

| Input | Syntax |
|---|---|
| `--scope` | `global`, `project:sh0m1-agops`, `workspace:acme` |
| `--key` | Any string; slugged. Unique within the scope. |
| `--title` | One line. Shown in indexes and the vault. |
| `--body-file` | Path to a UTF-8 Markdown file. Plain body only: no frontmatter, agops adds it. `-` is not special (it is read as a file named `-`). |
| `--supersedes` | Required when the key already exists in that scope: the **id of the latest revision**. Forbidden when the key is new. |

Updating an entry:

```sh
# 1. find the latest id (the file name under the key directory, without .md)
ls memory/knowledge/project/sh0m1-agops/build-flags/      # in the hub clone
#    or capture it when you add: the JSON event has payload.id
agops --json knowledge add --scope project:sh0m1-agops --key build-flags \
    --title "Build flags" --body-file flags.md | jq -r .payload.id
# 2. add the new revision
agops knowledge add --scope project:sh0m1-agops --key build-flags \
    --title "Build flags" --body-file flags.md --supersedes 20261111T101500123456Z-ab12cd34ef56
```

A wrong or missing `--supersedes` fails with the id it expected.

### `knowledge retire`

```sh
agops knowledge retire --scope SCOPE --key KEY --reason "text"
```

Writes a new revision with `status: retired`. History stays in git, the entry leaves the
brief, search and the vault mirror.

### `search`

```sh
agops search WORD [WORD...] [--cwd DIR] [--archives]
```

Plain word counting over title + key + body, max 8 hits, no stemming, case-insensitive.
Searches **knowledge only**, not mirrored docs.

---

## 4. Projects and workspaces

### Project

A project is a git repository, identified by `remote.origin.url` of the checkout. Local paths are
never stored. Register once per repository (any clone path works):

```sh
agops project register /path/to/checkout [--workspace NAME]
```

Fails if the checkout has no `origin` remote. Registering again overwrites the project file
(including its workspace). Unregistered repositories still work for claims, but have no
workspace knowledge scope, and the brief says `Project: unregistered`.

### Workspace: a logical name

A workspace is **an arbitrary string you choose**, not a path and not a VS Code
`.code-workspace` file. agops never reads one. It is stored as `workspace:` in the project's
YAML, and is used for exactly three things:

1. **Knowledge scope.** `workspace:<name>` entries apply to every project registered with that
   name. This is the main reason to use workspaces: conventions shared by several repositories
   (an `acme` microservice family, say).
2. **Plan grouping in the vault.** A plan's workspace is derived from the projects it touches
   (`scope.projects` plus each task's `project`). One workspace gives that name, several give a
   comma-joined list, none falls back to the plan id's first `-` segment. Open plans are shown
   by workspace, finished plans are archived under `plans/archive/<workspace>/`.
3. **Sub-project inheritance.** An unregistered project id inherits the workspace of the
   longest registered project id that is a prefix of it plus `-`
   (`acme-stack-backend` inherits from `acme-stack`).

Convention: use one lowercase token per product or team (`acme`, `platform`). A project
registered via `--workspace` has exactly one workspace. A project can additionally be listed
in several `memory/workspaces/<name>.yaml` group files (`id: <name>`, `projects: [{id: <project-id>}]`).
The code honours these, but there is no CLI to create them: edit and commit by hand.

Plans do not take a workspace field. The `scope.workspace` shown in `protocol.md` is ignored
(see [Known gaps](#known-gaps)).

---

## 5. Plans

### Authors and responsibilities

| Action | Who | How it is enforced |
|---|---|---|
| Write a plan or a new revision | Agent or human | `plan draft`, `hub_draft_plan` (MCP), or edit the vault definition and `notes sync` |
| Approve a revision | Human | `plan approve`: interactive id confirmation. Refused when `AGOPS_AGENT_SESSION` is set (under `agops run`). No MCP tool exists. |
| Cancel a plan | Human | `plan cancel`, same confirmation |
| Claim, checkpoint, block, unblock, release, complete tasks | Agent (or human acting as one) | owner and session must match for checkpoint/complete/release/block |
| Edit tier policy, `notes.yaml`, personal notes, todo file | Human | plain files |

The guards are conventions, not security. `plan approve --yes` skips the prompt and is only
blocked when `AGOPS_AGENT_SESSION` is set, and anyone with write access to the clone can edit
files directly.

### Lifecycle

```
plan draft ──> draft (rev 1) ──approve──> active ──last task done + criteria covered──> completed
                  │                          │  ^
                  │                          │  └── approve of a later revision
                  └──cancel──> cancelled <───┘
                                         (cancel needs all claims released)
```

- `draft`: has revisions but no approval. No task is claimable.
- `active`: an approved revision exists. **Tasks run from the approved revision**, not the latest.
- `completed`: set automatically by the `task complete` that finishes the last task, provided
  every `acceptance_criteria` id is listed in some task's `covers`.
- `cancelled`: terminal. Optional `--superseded-by OTHER_PLAN` links the replacement.

`agops plan list` shows `latest_revision`, `approved_revision` and `pending_revision` (set when
the latest is newer than the approved one).

### Maintaining a plan: the revision loop

A plan is never edited in place. Each change is a new revision file; approving it switches
which revision is executable.

```
1. author a change        -> new revision N+1, unapproved   (draft or notes sync)
2. human reviews it       -> agops plan show ID --revision N+1   /  vault "Needs you"
3. human approves         -> agops plan approve ID [--revision N+1]
4. agents continue        -> claims now read revision N+1; finished tasks stay finished
```

Two ways to produce a revision:

**A. From a YAML file (CLI or MCP)**

```sh
agops plan draft plan.yaml          # same id as an existing plan => next revision number
```

Creates `revisions/000N.yaml` where N is the number of existing revisions plus one. There is
no base-revision check on this path: if two people draft from stale copies, the later draft
silently wins as the latest revision. Start from the current definition:

```sh
agops --json plan show ID --full > plan.yaml     # JSON is valid YAML; or copy the vault's
                                                 # plans/.definitions/ID.yaml
```

Remove `revision`, `created_at` and `created_by` (agops overwrites them) before re-drafting.

**B. From the notes vault (human, two-way)**

`plans/.definitions/<id>.yaml` in the vault holds the latest revision, including unapproved
ones. Edit it, then run:

```sh
agops notes sync
```

This validates the file and, if it differs from the hub, creates the next revision as an
**unapproved** draft. This path is compare-and-swap: it only imports if the hub's latest
revision and hash still equal what the vault was rendered from. The plan note itself
(`plans/active/<id>.md`) is generated output. Do not edit it, except inside the
`My notes` region, which is preserved.

Sync states per plan (`agops notes status`):

| Status | Meaning | Action |
|---|---|---|
| `clean` | Vault and hub agree | none |
| `edited` | Vault changed, hub did not | `notes sync` imports it |
| `stale` | Hub changed, vault did not | refreshes automatically on next render |
| `conflict` | Both changed differently | `agops notes resolve ID --take notes\|agops` |
| `invalid` / `missing` | Definition does not parse/validate, or is absent | fix the file |

Formatting-only edits are ignored (definitions are compared as canonical YAML). Finished
plans are read-only history and are never imported.

### What a new revision does to running work

- Task state is keyed by task id: a task completed in rev 1 stays completed in rev 2 if the
  id is kept.
- Changed task text takes effect for claims made after approval. An existing claim is not
  interrupted.
- A task removed from the revision is ignored (its state stays in history, it no longer counts).
- Approving a new revision of a *completed* plan clears its `completed` flag, so the plan
  reopens when the new revision adds unfinished tasks.
- Until approval the vault shows the new definition under "Needs you" with the added,
  removed and changed task ids. The task list is still rendered from the approved revision.

### Plan YAML

Required: `id`, `title`, `goal`, `tasks` (non-empty list).

```yaml
id: network-isolation                 # slugged; the plan's identity
title: Isolate egress traffic
goal: |                               # first paragraph's lead shows in the vault
  Why this exists and what done looks like.
scope:
  projects: [acme-vpc, acme-nlb]      # optional; adds to the plan's project list
acceptance_criteria:                  # optional
  - id: egress-blocked                # use {id, text} mappings, see Known gaps
    text: Unapproved egress is dropped and logged.
tasks:
  - id: vpc-rules                     # required, unique
    title: Add egress deny rules      # required
    project: acme-vpc                 # required unless read_only: true
    tier: standard                    # optional, must exist in tiers.yaml; default per policy
    depends_on: []                    # task ids; unknown ids are rejected
    write_scope: ["modules/net/**"]   # globs; empty = whole project
    acceptance:                       # shown as "Done when"
      - Plan output shows deny rules.
    covers: [egress-blocked]          # criteria this task satisfies
    # optional, display only: summary, notes, instructions, phase, model, reasoning_effort
  - id: review
    title: Review findings
    read_only: true                   # no project/worktree needed, never conflicts
    depends_on: [vpc-rules]
```

Validation (`plan draft`, `notes sync`, `scan`): required keys, task `id` and `title`,
`project` for writing tasks, duplicate ids, `tier` is a string and defined in the policy,
`depends_on` ids exist. Content with credential patterns is rejected. Cycles in `depends_on`
are not checked: a cyclic task is never ready.

Display-only fields: `phase` groups tasks into sections in the vault in order of first
appearance. `summary` is the one-line gist, otherwise the first sentences of `notes` or
`instructions` are used.

### `plan` commands

```sh
agops plan draft FILE.yaml [--actor A] [--session S]
agops plan approve PLAN [--revision N] [--yes]       # default: latest revision
agops plan cancel PLAN --reason TEXT [--superseded-by OTHER] [--yes]
agops plan list
agops plan show PLAN [--revision N] [--task TASK] [--full]
```

`approve` and `cancel` prompt `Type its id:` on a TTY; without a TTY they need `--yes`.
`cancel` fails while any task has a live claim (release first), and fails for a plan that is
already completed or cancelled. A never-approved draft can be cancelled.

`show` is compact by default (goal cut to 300 chars, `Completed: N of M`, open tasks with
acceptance). `--task` prints one task in full. `--full` and `--json` print the whole
definition.

---

## 6. Tasks

```
ready ──claim──> claimed ──checkpoint (renews lease)──> claimed ──complete(evidence)──> completed
  ^                 │  │
  │                 │  └──block(reason)──> blocked ──unblock(resolution)──> ready
  └──release / lease expiry (60 min without checkpoint or heartbeat)
```

A task is *ready* when its plan is active, all `depends_on` tasks are completed, and it is not
claimed, blocked or completed.

### Claim checks (`task claim PLAN TASK --cwd DIR`)

1. Plan active, task exists, no live claim by someone else, dependencies completed.
2. **Tier**: the session's declared model must map to the task's tier (see [8](#8-sessions-actors-models-tiers)).
3. **Project** (writing tasks): `--cwd` must be inside a git worktree whose `origin` yields the
   task's `project` id. `read_only` tasks skip this.
4. **Exclusivity** (writing tasks): no other live writing claim in the same checkout, and no
   live claim in the same project whose `write_scope` overlaps. Overlap is path-prefix based
   on the part before the first `*`; an empty scope or `**` overlaps everything.

### Progress and finish

```sh
agops task checkpoint PLAN TASK --summary "text" [--evidence REF]...    # renews the lease
agops task complete   PLAN TASK [--summary "text"] --evidence REF...    # >= 1 evidence
agops task block      PLAN TASK --reason "text"                         # owner only; releases the claim
agops task unblock    PLAN TASK --resolution "text"                     # any session
agops task release    PLAN TASK                                         # owner only
agops task ready [--plan PLAN] [--tier TIER]
```

- `--evidence` is repeatable free text; use references someone can check:
  `"commit: abc123"`, `"pytest: 12 passed"`, `"page: /path/to/page.html"`.
- `--summary` on `complete` defaults to `Completed`.
- Checkpoint, complete, block and release require the **same actor and session** as the claim.
  The default session is `shell-<parent pid>`, stable per terminal. Across terminals set
  `AGOPS_SESSION` and `AGOPS_ACTOR` explicitly.
- If the hub cannot be reached (git error) a checkpoint is queued in
  `~/.local/state/agops/outbox` and published by the next `agops sync`.
- `agops run` claims, sets `AGOPS_AGENT_SESSION=1` (which blocks approve, cancel and tier
  override inside the child), renews the lease every 10 minutes, and writes a final
  checkpoint with the exit code:

```sh
agops run [--plan PLAN --task TASK] [--cwd DIR] [--model ID] [--actor NAME] TOOL [tool args...]
```

  `--plan` and `--task` go together. Options go before `TOOL`.

---

## 7. Notes vault and docs folders

### How knowledge, notes and docs relate

| | Knowledge | Notes vault | Docs folders |
|---|---|---|---|
| Lives in | hub (git, `memory/knowledge`) | a local folder, rendered from the hub | your own folders (often inside project repos) |
| Written by | `knowledge add` | agops (generated); you only add personal notes | you, in the source folder |
| Direction | source of truth | hub to vault, read-only (plans excepted) | source folder to vault, read-only copy |
| Reaches agents via | brief, `search`, instruction files | not read by agents, except the todo file in the brief | not read by agents |
| Length | about 500 bytes | n/a | any (10 MiB per file cap) |

So: knowledge is what agents are told, the vault is what you read, docs are long-form
material you want findable and linked from plans. They connect only in the vault:
a knowledge note shows its related plan, and a plan note lists related knowledge and docs.

### Should you add repo `docs/` folders?

Yes, if you want your reading environment (Obsidian search, plan notes) to reach them. They
are not stored in the hub and agents do not see them through agops. Points from the code:

- **Copy, not link.** Every sync copies `.md`, `.html` and image/`.drawio` files into
  `<vault>/docs/<folder>/`. Source is authoritative: a copy you edit in the vault is kept but
  never refreshed again. A deleted source deletes the copy unless it was edited.
- **Whole tree each sync.** Dot files and dot directories, symlinks, files over 10 MiB, and
  files matching credential patterns are skipped. There is no include/exclude option, so point
  at a dedicated docs directory, not a repo root.
- **Folder names: use a mapping when two folders are called the same.** With a list, the
  destination directory is the slug of the source folder's *own name*, so two repos that both
  use `docs/` map to the same `docs/docs/`. Files with different relative paths merge into
  that one folder. Files with the same relative path collide: the first sync skips the second
  with an "edited in the vault" warning, later syncs let each source overwrite the other in
  turn, so the content flips. agops warns when two list entries resolve to the same name, and
  still merges them. To keep them apart, give each folder a name with the mapping form of
  `docs:` (see [`notes.yaml`](#notesyaml-in-the-vault-root-yours)): the key is the folder
  under `docs/`, the value is the source.

  ```yaml
  docs:
    projecta: ~/work/projectA/docs
    projectb: ~/work/projectB/docs
  ```

  Keys are slugged. Two keys that slug to the same name warn and the later one is skipped.
  A list behaves as it always has. When you switch a list to a mapping, the unedited copies
  under the old folder are removed on the next sync, and an edited one is kept with a warning.
- **Markdown copies are renamed** `x.md` to `x (<parent folder>).md`, and relative links
  between mirrored docs are rewritten, so a doc named after a plan does not hijack
  `[[plan-id]]` links.
- **A doc must live outside the vault folder** and must not contain it.

Recommended split: the document stays in the repo (reviewed and versioned there), the vault
mirrors it for reading, and a short knowledge entry carries the conclusion plus the path
(`Why we chose X: see docs/adr/0007-x.md`).

### Connecting a vault

```sh
agops notes connect ~/Notes/agops   # new, empty, or already agops-managed directory only
agops notes status                  # per-plan clean/edited/stale/conflict, todo file status
agops notes sync                    # import edited plan definitions, then re-render
agops notes resolve PLAN --take notes|agops [--yes]
agops notes review                  # read-only JSON triage report (stale plans, retire candidates)
agops notes disconnect              # forget the folder; deletes nothing
```

Notes need a configured profile. They are unavailable with a bare `--repo`/`AGOPS_REPO` hub.
One vault per profile, bound to its hub by a fingerprint. Rendering happens automatically
after hub changes and `agops sync`; a vault failure is reported as `notes_warning` and
never fails the hub operation.

### `notes.yaml` (in the vault root, yours)

Written once as a commented template, then only read.

```yaml
docs:                                 # a list: each folder is named after itself ...
  - ~/work/acme-vpc/docs/network      # absolute or ~ paths; one entry per folder
  - ~/work/agent-knowledge
# ... or a mapping of name to folder (use one form, not both). The name is the folder
# under <vault>/docs/, so same-named folders no longer collide:
# docs:
#   projecta: ~/work/projectA/docs
#   projectb: ~/work/projectB/docs
topics:                               # optional tagging/grouping
  networking:
    projects: ["*-nlb", "*-vpc"]      # fnmatch globs on project ids
    keywords: [nlb, vpc, vpn]
todos:                                # optional, read-only user todo list
  file: Todo/ALL.md                   # relative to the Obsidian vault root
  inbox: Inbox
  sweep_exclude: ["Meetings/Zoom/**"]
```

### Linking docs to plans

A mirrored doc, page or diagram is attached to a plan when any of:

1. Its file name is `<plan-id>.*` or `<plan-id>-*` (longest matching plan id wins).
2. The plan definition text contains the source path tail `<parent folder>/<file name>`
   (e.g. `network/flow.md`).
3. HTML only: `<meta name="agops-plan" content="plan-a plan-b">`. Add
   `<meta name="agops-task" content="task-id">` to show it on that task as well.

### Pages

```sh
agops page add FILE.html --plan PLAN [--task TASK] [--title T] [--slug S]
```

Stores `<pages dir>/<plan-id>-<slug>.html` with both meta tags set, rejects non-HTML, files
over 10 MiB and credential-like content, warns on remote `<script src>`, re-renders the vault
and prints the vault path to cite in a checkpoint. Registering the same slug again replaces the
page. The plan must exist, and so must the task if `--task` is given.

#### Where pages live (the "pages dir")

Pages are **not stored in the hub**. They are plain HTML files in one local folder, so they do
not travel with `agops sync` and exist only on the machine that ran `page add`. The hub only
learns about a page if someone cites its path in a checkpoint's `--evidence`.

Which folder that is, in order:

1. The first `docs:` entry in `notes.yaml` named `pages`. This is the only place a name is
   special: there is no setting for it, the name `pages` is the switch. In the list form the
   name is the folder's own name. In the mapping form it is the key, whatever the folder is
   called (`pages: ~/work/agent-output`); a key such as `agent-pages` is never the pages dir,
   even for a folder called `pages`.
2. Otherwise `~/.local/share/agops/pages` (under `AGOPS_HOME` if set).

Whether the pages show up in the vault depends on which case applies:

| Case | `page add` writes to | In the vault? | Brief lists pages? |
|---|---|---|---|
| A `docs:` entry named `pages` | that folder | yes, `docs/pages/<file>`, after the sync that `page add` triggers | yes |
| No such entry (default folder) | `~/.local/share/agops/pages` | no, and `page add` warns `Add <folder> to docs: ...` | no |
| No notes vault connected | the folder above | no, and `page add` warns that nothing is linked | no |

Consequences worth knowing:

- Since `page add` writes into the pages dir, pointing a `pages` entry at a folder inside a
  repo puts agent-generated HTML into that working tree. Use a dedicated folder.
- Only the first matching entry is the pages dir. A second `pages` folder is mirrored as
  ordinary docs, but `page add` ignores it.
- Pages and docs are linked to plans by the same rules (file name `<plan-id>-*`, or the
  `agops-plan` meta tag), so a page stays linked wherever it came from. `page add` sets both
  the name and the tag.
- The vault path that `page add` prints and the brief cites is only correct while the pages dir
  is mirrored. With the default folder it prints the plain file path instead.

In the mapping form the vault folder is the key: with `pages: ~/work/agent-output`, pages
land in `<vault>/docs/pages/` and that is the path `page add` prints and the brief cites.

---

## 8. Sessions, actors, models, tiers

- **Actor**: a label on events. `--actor`, else `AGOPS_ACTOR`, else `agent`
  (MCP: `AGOPS_ACTOR` else `mcp-agent`).
- **Session**: `--session`, else `AGOPS_SESSION`, else `shell-<ppid>`.
  Ownership of a claim is the (actor, session) pair.
- **Model**: declared with `agops session --model ID` or `agops brief --model ID`
  (MCP: `hub_get_brief(model=...)`). Resolution order: `AGOPS_MODEL`, then the argument,
  then the record saved earlier for that session in `~/.local/state/agops/sessions`.
- **Tier**: `memory/policy/tiers.yaml` maps model-id globs to tier names. A task has a `tier`
  (default `default_task_tier`). A claim is accepted only if the session's tier equals the
  task's tier. An unmapped model is tier `unknown` and cannot claim. A model matching two
  tiers is an error. Patterns are case-insensitive `fnmatch` globs; overlapping patterns across
  tiers are rejected at validation.

```yaml
schema_version: 1
default_task_tier: standard
tiers:
  frontier: {models: ["*claude-opus-*", "gpt-5.6-pro*"]}
  standard: {models: ["*claude-sonnet-*", "gpt-5.6-terra*"]}
```

Check with `agops policy show` (tiers plus the current session's tier) and
`agops policy validate`. A human may override once with
`agops task claim ... --allow-tier-mismatch` (type the task id; refused under `agops run`,
without a TTY and via MCP). When `tiers.yaml` is missing, tier checks are skipped.
Once a session is declared, the brief hides tasks of other tiers and counts them.

---

## 9. File formats

| Path (under `memory/`) | Format | Edit by hand? |
|---|---|---|
| `events/Y/M/D/<id>.json` | JSON, immutable | no |
| `plans/<id>/revisions/NNNN.yaml` | YAML, immutable per revision | no; draft a new revision |
| `knowledge/<scope path>/<key>/<id>.md` | YAML frontmatter (`id, key, title, scope, created_at, created_by, supersedes, status, kind`) + Markdown body | no; use `knowledge add` |
| `projects/<project-id>.yaml` | `id, remote, workspace, registered_at` | via `project register` |
| `workspaces/<name>.yaml` | `id`, `projects: [{id}]` | yes (no CLI) |
| `policy/tiers.yaml` | as above | yes, then commit |

`<scope path>` is the scope with `:` replaced by `/` (`project/sh0m1-agops`).
`agops doctor` and `agops scan` validate all of it; `scan` also rejects secrets.

---

## 10. Command reference

`--json` goes **before** the subcommand (`agops --json task ready`); on `doctor` and `scan` it
may also follow. `--repo PATH` before the subcommand selects a hub path explicitly.

| Command | Input | Notes |
|---|---|---|
| `setup` | `[--remote URL \| --local] [--profile N] [--default] [--runtime PATH] [--keep-claude-memory]` | Idempotent: creates/clones the hub, writes instruction blocks, registers MCP, installs skills. |
| `profile list` / `profile default NAME` | | |
| `adapter install PATH` | `[--tools agents,claude,gemini,cursor,copilot]` (default `agents,claude`) | Writes the managed block into repo-level instruction files. |
| `skills install` | | |
| `sync` | | Fetch remote, fast-forward, publish queued checkpoints, refresh vault. |
| `doctor` / `scan` | | Health report / content validation. |
| `session` | `[--model ID] [--session ID] [--actor A] [--value]` | Without `--model`: prints a fresh UUID. With: records model and tier. |
| `brief` | `[--cwd DIR] [--model ID]` | Default cwd `.`. |
| `policy show\|validate` | | |
| `search` | `WORD... [--cwd DIR] [--archives]` | |
| `project register` | `PATH [--workspace NAME]` | |
| `knowledge add` / `retire` | see [section 3](#3-knowledge) | |
| `plan draft\|approve\|cancel\|list\|show` | see [section 5](#5-plans) | |
| `task ready\|claim\|checkpoint\|block\|unblock\|release\|complete` | see [section 6](#6-tasks) | `claim` also takes `--cwd DIR` (default `.`) and `--allow-tier-mismatch`. |
| `run` | `[--plan P --task T] [--cwd D] [--model ID] [--actor N] TOOL [args]` | |
| `notes connect\|status\|review\|sync\|resolve\|disconnect` | see [section 7](#7-notes-vault-and-docs-folders) | |
| `page add` | `FILE --plan P [--task T] [--title X] [--slug S]` | |
| `migrate-remember` | `PATH --project ID [--actor A]` | Imports a `.remember` journal's `*.md` into one `archive` entry of that project; a rerun supersedes the previous import. |

MCP equivalents (`agops-mcp`, stdio): `hub_get_brief`, `hub_search_knowledge`, `hub_list_plans`,
`hub_read_plan`, `hub_list_ready_tasks`, `hub_draft_plan`, `hub_claim_task`,
`hub_checkpoint_task`, `hub_heartbeat_task`, `hub_block_task`, `hub_unblock_task`,
`hub_release_task`, `hub_complete_task`, `hub_add_knowledge`, `hub_retire_knowledge`,
`hub_add_page`. Not exposed: approve, cancel, tier override, `project register`, notes sync.

---

## 11. Environment variables

| Variable | Effect |
|---|---|
| `AGOPS_PROFILE` | Hub profile for this process. Unknown name is an error (no fallback). |
| `AGOPS_REPO` / `--repo` | Explicit hub path; overrides the profile (the brief warns). Notes features are unavailable. |
| `AGOPS_ACTOR`, `AGOPS_SESSION` | Claim identity. Keep both stable across calls of one task. |
| `AGOPS_MODEL` | Declared model; wins over `--model`. |
| `AGOPS_AGENT_SESSION` | Set by `agops run`; blocks approve, cancel and tier override. |
| `AGOPS_HOME` | Replaces `~` for agops config (tests, sandboxes). |
| `AGOPS_STATE_DIR` | Parent of session records and the checkpoint outbox. |
| `AGOPS_LOCK_DIR` | Where `.agops.lock` lives (default: parent of the hub clone). |

---

## Known gaps

Differences and traps found while reading the code. Verify before relying on them.

1. **`scope.workspace` in plans is ignored.** `protocol.md`'s plan example has it, but the
   workspace is only derived from `scope.projects` and task `project` ids.
2. **`acceptance_criteria` must be `{id, text}` mappings** for plan completion to work.
   The vault renderer accepts plain strings, but the completion check reads `item["id"]`
   and would fail on a string item.
3. **`plan draft` has no base-revision check.** Only the notes path does. Re-drafting from a
   stale file overwrites another author's newer revision as "latest".
4. **Approval guard is soft.** `plan approve --yes` works anywhere `AGOPS_AGENT_SESSION` is not set.
5. **A `docs:` list still merges two same-named folders** (with a warning). Only the
   mapping form keeps them apart (see section 7).
6. **Knowledge update needs the previous entry id**, which text-mode `knowledge add`
   does not print. Use `--json` or read the key directory.
7. **`depends_on` cycles are not detected** at validation.
8. **Files not reviewed for this guide:** `todos.py` (todo list syntax is taken from the
   README) and `migration.py`.
