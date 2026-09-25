# agops

agops is a Git-native shared memory and execution ledger for local coding agents. It gives
Codex, Claude Code, Cursor, Gemini CLI, and shell-capable agents one readable source of truth for
knowledge, approved plans, task claims, checkpoints, and completion evidence.

It does not call a model API and does not depend on a particular subscription. Git is the durable
store; a small CLI provides safe state transitions, and a stdio MCP server exposes the same
operations to clients that support MCP.

agops was previously named Agent Hub. The Python package is still `agent_hub`, the runtime paths
still live under `agent-hub`, and the environment variables still use the `AGENT_HUB_` prefix; the
old `agent-hub` command remains as an alias for `agops`. The `agops` command ships from the first
release after 0.6.1, so an install pinned to an older tag — including the tag `install.sh` currently
defaults to — provides only `agent-hub`; every command below works under either name.

## Install

One command on a machine that already runs Claude Code and/or Codex CLI:

```sh
curl -fsSL https://raw.githubusercontent.com/sh0m1/agops/main/install.sh | sh
```

It installs `uv` if missing, installs `agops` pinned to a released tag, and runs
`agops setup`, which ends with a summary of what was configured. The memory lives in a local
Git repository at `~/.local/share/agent-hub/repo`; nothing leaves the machine.

To share the memory across machines, give the same command a Git remote — on the first machine
it pushes the existing local memory there, on the others it clones it:

```sh
curl -fsSL https://raw.githubusercontent.com/sh0m1/agops/main/install.sh \
  | sh -s -- --remote git@github.com:you/agent-hub-memory.git
```

To go back to a machine-local memory, `agops setup --local` detaches and forgets the remote
(your local history is kept). Other flags: `--ref <tag>` to pick a version, `--keep-claude-memory`
to leave Claude Code's automatic memory on, `--dry-run` to print the commands without touching
anything. Manual equivalent: `uv tool install git+https://github.com/sh0m1/agops@v0.6.1` then
`agops setup [--remote <url> | --local] [--profile NAME]`.

`setup` adds bounded managed blocks to the Codex and Claude user instruction files and registers
the MCP server with whichever of `codex` and `claude` are on `PATH` (others are reported as
skipped, not errors). Existing configuration is preserved and backed up before it is changed.
Re-run the one-liner (or plain `agops setup`) to upgrade; the remote is remembered.
`agops doctor` and `agops scan` remain available for later health checks; `--json` gives
machine-readable output.

The remote URL is stored verbatim in `~/.config/agent-hub/config.json` and echoed by `--dry-run`;
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
AGENT_HUB_PROFILE=private claude     # this session reads and writes only the private hub
claude                               # this one uses the team hub
```

The variable reaches the MCP server that Claude Code or Codex starts, so it applies to agent
sessions, not only to shell commands. The brief's header always names the hub
(`Hub: private · local only`). An unknown profile is an error rather than a fallback to the
default, and if `AGENT_HUB_REPO` is also set the brief warns that it overrides the profile.
`agops profile list` shows the profiles, the default, and the one the current terminal
resolves to; `agops profile default NAME` changes the default.

Upgrading from an earlier version: re-run `agops setup` once so the MCP registration stops
pinning a hub path; `agops doctor` reports `mcp_pinned` until you do.

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

`agops plan list` and `agops task ready [--tier NAME]` show what is available. Set
`AGENT_HUB_ACTOR` to name the agent and `AGENT_HUB_SESSION` to pin a session id explicitly.

Tasks carry a `tier` (default `standard`); `memory/policy/tiers.yaml` maps model ids to tiers.
Frontier models plan and review, cheaper models execute, and the hub rejects claims that cross
tiers. The shipped patterns lead with a wildcard so they also match the prefixed ids that provider
routes report (Bedrock sends `us.anthropic.claude-opus-5[1m]`); a model no pattern matches resolves
to the `unknown` tier, which rejects every claim, and the brief says so in its header. See
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

## Notes apps (Obsidian, Logseq, or any Markdown folder)

Connect one dedicated folder to a hub profile. The folder contains ordinary portable Markdown, so
Obsidian discovers it immediately and another notes app can use it too:

```sh
agops notes connect ~/Notes/agops
agops notes status
agops notes sync
```

The folder holds the whole working picture, and `Home.md` is the place to start: one page with
what needs a human (draft plans, pending approvals, blocked tasks, expired claims, notes agops
could not refresh), what is in progress and ready next, the open plans with their progress, the
most recent knowledge, and the repositories that have tracked work. Behind it:

```
Home.md                         the overview
Plans.md, plans/<id>.md         plans (two-way)
Knowledge.md, knowledge/<scope>/<key>.md
Projects.md, projects/<id>.md   one note per registered repository
Activity.md                     claims, blockers, ready tasks
views/*.base                    Obsidian Bases tables for plans, knowledge, and projects
```

Notes link to each other with relative Markdown links: a plan links the repositories its tasks
touch and the knowledge related to it (entries scoped to those repositories, or whose key starts
with the plan id); a repository note lists its plan tasks and knowledge; a knowledge note links
back to its repository and plans. Task statuses reflect the plan: tasks in a draft are `planned`,
and tasks in an active plan are `ready`, `waiting` on dependencies, `claimed`, `blocked`, or
`completed`. Every note carries `agops_*` properties and an `agops/plan`, `agops/knowledge`, or
`agops/project` tag, plus its title or repository name as an alias, so Bases, search, and `[[`
link completion all work. The `views/` files are seeded once; after you customize one in
Obsidian, agops leaves it alone, and deleting it restores the default.

Only plan notes are two-way; knowledge, projects, and activity are a read-only mirror of the
hub, so change knowledge with `agops knowledge add` rather than in the vault. Personal notes are
preserved in every note, and unchanged notes are not rewritten, so a synced vault only sees real
changes.

`agops setup` also installs the `agops-obsidian` skill for Claude Code and Codex. It tells an
agent how to answer "give me an overview" from `Home.md`, which files in the agops folder it may
edit, and how to keep the rest of the vault ordered (Inbox, Todo, Meetings, Topics, Archive)
without moving anything before the user approves the plan. A skill file you wrote yourself under
the same name is never overwritten.

Agops exports after successful hub changes and after `agops sync`. Editing a plan note is safe but
intentional: `agops notes sync` validates the fenced YAML and creates a new **unapproved** plan
revision. Personal notes and non-`agops_*` frontmatter are retained and never imported. Finished
or cancelled plans are history only. If agops and a note changed the same definition, resolve it
deliberately: `agops notes resolve PLAN --take notes|agops` (type the plan id, or add `--yes`).
`agops notes disconnect` only forgets the folder; it never deletes notes.

The sidecar is bound to its hub, so a folder belonging to another profile cannot be connected by
mistake. A notes write failure is an additive `notes_warning`: the hub event still succeeds and the
next sync retries it. When a newer revision is awaiting approval, the note clearly separates that
definition from the executable tasks of the approved revision.

## Tool access order

The managed instruction block tells agents to try a CLI or MCP tool first for any external system,
and to fall back to a browser only when no CLI or MCP route exists, it is scoped wrong, it fails, or
it cannot answer the question. The browser stays allowed; it is just not the first choice, and the
agent should say why it fell back.

See [docs/protocol.md](docs/protocol.md) for schemas, state transitions, concurrency semantics, and
the generic agent integration contract.
