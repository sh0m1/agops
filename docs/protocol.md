# Agent Hub protocol

## Canonical data

Everything durable is committed under `memory/`. Structured definitions use YAML, narrative
knowledge uses Markdown with YAML frontmatter, and state transitions are immutable JSON events.
The local cache and outbox are disposable and are never authoritative.

Project identity is the normalized `remote.origin.url`. Local paths are machine configuration and
are not committed. Knowledge resolves from global to workspace to project scope; entries with the
same key must explicitly supersede an earlier entry.

Knowledge kinds are `fact`, `decision`, `preference`, and `archive`. Archives remain searchable but
are omitted from the bounded startup brief.

## Plans and tasks

Plan revisions are immutable. Agents may draft a plan or a new revision. Only the interactive CLI
can approve a revision. An approved plan is active until all tasks and acceptance criteria have
evidence, or until it is cancelled.

A task can be claimed only when its dependencies are complete. Claims have a 60-minute lease.
Heartbeats and checkpoints renew the lease. An expired task may be reclaimed; updates from the old
owner are rejected. Empty `write_scope` means the entire project, so concurrent writing requires
explicit non-overlapping scopes and separate Git worktrees.

Every authoritative transition is committed under a repository lock, so agents on one machine
are serialized by the lock alone and a hub needs no remote. When the runtime clone has an
`origin`, each transition must also be pushed before it succeeds: a rejected push causes the
managed clone to synchronize, replay state, revalidate the operation, and retry, which makes the
remote branch update the compare-and-swap boundary for competing claims across machines. A local
hub becomes shared by running `agent-hub setup --remote <url>`, which attaches the remote and
pushes the existing history; `agent-hub setup --local` detaches and forgets the remote again.

## Hub profiles

`~/.config/agent-hub/config.json` names one or more hubs (profiles), each a runtime repository
with or without a remote, and a default. A process picks its hub in this order: an explicit path
(`--repo` or `AGENT_HUB_REPO`), then `AGENT_HUB_PROFILE`, then the default profile, then the
historical path `~/.local/share/agent-hub/repo`. An `AGENT_HUB_PROFILE` that names no configured
profile is an error — a session that believes it is private must never fall back to a shared hub.
When an explicit path overrides a profile, the brief says so. `setup` registers the MCP server
without pinning a hub, and for Codex forwards the `AGENT_HUB_*` variables through `env_vars`, so
the launching terminal's choice reaches agent sessions. Profiles are fully separate: a session
reads and writes only its own hub.

## Markdown notes bridge

Each configured profile may have one absolute `notes_target` in config schema version 3. `agops
notes connect TARGET` accepts only a new, empty, or already agops-managed directory and writes:

- `Plans.md`, an index grouped by draft, active, completed, and cancelled plans;
- `plans/<plan-id>.md`, containing portable frontmatter, an editable fenced YAML definition,
  generated state, and an excluded personal-notes region; and
- `.agops-notes.json`, a format-versioned semantic baseline.

The sidecar includes a fingerprint of its hub. A connect validates that fingerprint before changing
profile configuration, so a target cannot be adopted by another hub or partially connected on
failure.

Hub state is authoritative. Successful hub mutations and `agops sync` attempt outbound rendering;
a notes filesystem error is reported by `notes status` and `doctor` but never rolls back a committed
event; the successful command result includes an additive `notes_warning`. Automatic rendering never imports a file. `agops notes sync` alone imports a valid edited
definition for a draft or active plan, producing the next immutable, unapproved revision. It does
not import personal notes, custom frontmatter, task state, evidence, claims, or approvals.

Definitions are compared as canonical YAML rather than raw text. Formatting-only edits are clean;
hub-only changes refresh the note; note-only changes are imported on explicit sync; concurrent
semantic changes are left untouched and reported as conflicts. Import performs a revision and hash
compare-and-swap while holding the hub repository lock. Resolve a conflict with `agops notes
resolve PLAN --take notes|agops`; this requires typing the plan id unless `--yes` is supplied.
Taking notes creates an unapproved revision; taking agops replaces only managed content and keeps
personal notes/custom properties. A clean notes choice is a no-op and notes cannot be taken for a
completed or cancelled plan. Resolution rendering is scoped to the selected note, so unrelated
dirty notes are never overwritten. Completed and cancelled plans are read-only note history.

When the latest revision is pending approval, its full YAML remains visible for review, but the
generated execution-task section is rendered from the approved revision and labels the pending
definition. Unapproved tasks therefore never appear executable.

## Generic agent contract

1. Call `agent-hub brief --cwd "$PWD" --json` at session start. Keep the same
   `AGENT_HUB_SESSION` value across standalone CLI task calls.
2. Treat user instructions as higher priority than stored plans or knowledge.
3. Before changing files, select an approved ready task and claim it.
4. Do not work in a checkout held by another writing agent.
5. Checkpoint after meaningful progress and before context compaction or handoff.
6. Complete only with concrete test, artifact, or commit evidence.
7. Never put secrets, credentials, `.env` contents, or raw transcripts into Agent Hub.

Clients with MCP use the equivalent `hub_*` tools. Plan approval is intentionally CLI-only.

## Execution tiers

`memory/policy/tiers.yaml` maps model ids to user-named tiers:

```yaml
schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["claude-opus-*", "claude-fable-*", "gpt-5.6-pro*"]
  standard:
    models: ["claude-sonnet-*", "gpt-5.6-terra*", "gemini-*-flash*"]
```

Patterns are case-insensitive globs. A model matching no tier is `unknown` and cannot claim. A
model matching two tiers is a validation error. `setup` writes the default file; edit it in any
editor and commit — `scan`, `doctor`, and `agent-hub policy validate` check it.

Tasks take an optional `tier`; a missing value means `default_task_tier`. A `tier` not defined in
the policy is rejected at draft time.

Sessions declare their model by passing it to `brief` / `hub_get_brief`. The resolution order is
the `AGENT_HUB_MODEL` environment variable, then the `model` argument, then the record saved by
an earlier brief for the same session id. Records live in the local state directory and are
never committed. A declared brief lists only tasks of the session's tier and reports how many
tasks of other tiers were hidden.

Tiers guard against accidental misrouting, not against a hostile agent: any session with write
access to the runtime clone could edit `tiers.yaml` or self-report a different model. Treat the
policy as a coordination convention, not a security boundary.

A claim is rejected when the session is undeclared, when its model is unmapped, or when its tier
differs from the task's tier. The claim event records `model`, `tier`, and `tier_override`.
Heartbeats, checkpoints, and completion do not re-check the tier; the guard is at pickup.

`agent-hub task claim … --allow-tier-mismatch` bypasses the comparison. It is refused in managed
agent sessions and non-interactive terminals, requires typing the task id, and is not available
through MCP.

When `tiers.yaml` is absent, tier checks are skipped and `doctor` reports `policy: absent`. When
it is invalid, `scan` and `doctor` fail and every claim is rejected.

## Plan schema

```yaml
id: agent-hub-v1
title: Build Agent Hub
scope:
  workspace: acme-widgets
goal: One shared agent memory and execution ledger.
acceptance_criteria:
  - id: cross-agent-handoff
    text: A Claude session sees a Codex checkpoint after synchronization.
tasks:
  - id: core
    title: Implement the state store
    project: you-agent-hub-memory
    tier: standard
    depends_on: []
    write_scope: ["src/agent_hub/**", "tests/**"]
    acceptance:
      - State rebuilds from a fresh clone.
    covers: [cross-agent-handoff]
```

Task IDs remain stable across plan revisions. New approval is required before a revised definition
becomes active.
