# Execution tiers

Status: approved design, not yet implemented.

## Problem

Agent Hub coordinates several coding agents that run on different models and subscriptions.
Planning, review, and architectural decisions should be done by frontier models; routine,
well-specified execution should be done by cheaper models. Today the hub has no notion of
which model a session runs on or which model a task is meant for, so:

- a frontier session can pick up routine execution work and spend tokens it should not;
- a cheap session can pick up a review task it is not suited for;
- users encode the routing policy as free-form plan fields (`execution_policy:`) and prose
  knowledge entries that the hub cannot read or enforce;
- nothing in the ledger records which model actually did which task.

## Goals

1. Tasks declare the tier of model that may claim them.
2. Sessions declare the model they run on; the hub derives the tier from a user-controlled
   mapping, so agents never assign their own privilege level.
3. Claims are rejected when tiers do not match. Override is a human, interactive-only action.
4. The claim event records model and tier, so the ledger answers "which model did this".
5. The startup brief shows a session only the tasks it can claim, saving tokens.
6. No extra Git commits per terminal session. No migration of existing data.

## Non-goals

- Detecting the model automatically. MCP servers and CLI processes cannot see the parent
  tool's model; declaration is trusted.
- Re-validating tier after claim. The guard is at pickup.
- Cost accounting. Recording the model enables it later; this design does not compute it.
- A policy editor. The policy file is YAML in a Git repository the user already edits.

## Design

### 1. Policy file

`memory/policy/tiers.yaml` is committed and human-edited. `setup` writes a default when the
file is absent, through the normal `_mutate` path so the write is synchronized and pushed.

```yaml
schema_version: 1
default_task_tier: standard
tiers:
  frontier:
    models: ["claude-opus-*", "claude-fable-*", "gpt-5.6-pro*"]
  standard:
    models: ["claude-sonnet-*", "gpt-5.6-terra*", "gemini-*-flash*"]
```

Rules:

- Tier names are user-defined slugs. `frontier` and `standard` are only the shipped defaults.
- `models` entries are `fnmatch` globs matched case-insensitively against the full model id.
- A model id matching more than one tier is a validation error.
- `default_task_tier` must name a tier in `tiers`.
- A model that matches no tier resolves to the pseudo-tier `unknown`. Sessions on an unknown
  model cannot claim anything; the error names the model so the fix is one line in this file.

Validation runs in `scan`, `doctor`, and a new `agops policy validate`.

File states:

| `tiers.yaml` | Behaviour |
|---|---|
| absent | Tier checks skipped. `doctor` reports a warning. Existing installs keep working until `setup` is re-run. |
| present and valid | Tier checks enforced. |
| present and invalid | `scan` and `doctor` fail. All claims are rejected. A broken policy must not mean "no policy". |

### 2. Task schema

Tasks gain one optional field:

```yaml
tasks:
  - id: p04
    title: Roll out to dev
    project: acme-widgets
    tier: standard
```

- Missing `tier` means `default_task_tier`. Existing approved plans therefore behave as
  `standard`, which is the intended guard, with no migration.
- `validate_plan` rejects a `tier` that is not defined in `tiers.yaml`. When the policy file is
  absent, `tier` values are accepted without checking.
- `read_only` tasks take a tier like any other task. Review tasks are the main case where a
  `frontier` tier is wanted.

### 3. Session declaration

Declaration is local state, not a committed event. The record lives at
`<AGOPS_STATE_DIR or ~/.local/state/agent-hub>/sessions/<session-id>.json`:

```json
{
  "session": "6f0c…",
  "actor": "mcp-agent",
  "model": "claude-sonnet-5",
  "tier": "standard",
  "declared_at": "2026-09-07T08:41:00Z"
}
```

Model resolution order for a session, first hit wins:

1. `AGOPS_MODEL` environment variable.
2. `model` argument passed to `brief` / `hub_get_brief`.
3. Existing session record on disk.
4. None: the session is undeclared.

Tier is always derived from the resolved model and the current `tiers.yaml` at the moment of
use; the `tier` stored in the record is informational and is refreshed on every brief.

The brief is the declaration point. `hub_get_brief(cwd=".", model=None)` and
`agops brief --cwd … [--model ID]`:

- When a model resolves, write or refresh the session record.
- Header line: `Session: <actor> · <model> · tier=<tier>`, or when undeclared:
  `Session: undeclared — pass model=<your model id> to hub_get_brief before claiming`.
- Ready-task filtering: when declared, list only tasks whose tier matches the session. Tasks of
  other tiers collapse into one line per plan, e.g. `3 task(s) hidden by tier: 3 frontier`.
  Undeclared sessions see the full list plus the nudge.

Re-declaration: calling brief again with a different model overwrites the record. This is how a
mid-session model switch is reported. No dedicated declare tool is added; the one call already
required at session start covers it.

Session identity caveat: MCP sessions have a stable session id for the life of the server
process, so the record is found. CLI calls without `AGOPS_SESSION` exported get a fresh
UUID per call, will not find a record, and will be treated as undeclared unless
`AGOPS_MODEL` is set. This matches the existing contract ("keep one stable session ID").

### 4. Claim enforcement

In `claim_task`, after the existing "already claimed" and "dependencies complete" checks and
before the worktree and write-scope checks:

1. Resolve the session model. Undeclared:
   `Session has not declared a model; call brief with model=<id> first`.
2. Derive the tier. Unknown:
   `Model 'x' is not mapped to a tier in memory/policy/tiers.yaml`.
3. Compare with the task tier (or default). Mismatch:
   `Task p04 requires tier standard; session model claude-opus-5 is tier frontier`.

When `tiers.yaml` is absent, steps 1–3 are skipped and the claim payload records
`model` (if any) with `tier: null`.

The claim event payload gains three fields:

```json
{
  "lease_until": "…",
  "worktree": "…",
  "model": "claude-sonnet-5",
  "tier": "standard",
  "tier_override": false
}
```

`TaskState` gains `model`, `tier`, and `tier_override`, set from `task_claimed` and preserved
through `task_heartbeat`. Older events without these fields replay with `None`/`False`.

Heartbeat, checkpoint, block, release, and complete do not re-check tier. Ownership already
gates them, and re-checking would break checkpoints after a mid-task model switch. The tier
that matters is the one recorded at claim time.

Override: `agops task claim … --allow-tier-mismatch`.

- Refused when `AGOPS_AGENT_SESSION` is set.
- Refused when stdin is not a TTY.
- Requires typing the task id, like `plan approve`.
- Still requires a declared, mapped model; it only bypasses the comparison.
- Stamps `tier_override: true` on the claim event.
- Not available through MCP.

`ready_tasks` stays tier-agnostic: it answers "claimable in principle". `brief` applies the
per-session filter. `agops task ready --tier NAME` is added for scripting.

### 5. Surface changes

CLI:

| Command | Change |
|---|---|
| `brief --cwd … [--model ID]` | declares the session, filters by tier |
| `task claim … [--allow-tier-mismatch]` | interactive-only override |
| `task ready [--tier NAME]` | optional filter |
| `policy show` | prints `tiers.yaml` and the tier the current environment/session resolves to |
| `policy validate` | validates `tiers.yaml`; also run by `scan` and `doctor` |
| `setup` | writes the default `tiers.yaml` when absent |

MCP: `hub_get_brief` gains `model: str | None = None`. No other signature changes.
`hub_claim_task` can now reject on tier. No new tools.

Managed instruction block (`setup.INSTRUCTIONS`, propagated by `setup` and
`adapter install`) gains:

> Pass your current model id to `hub_get_brief` (or `agops brief --model`) at session
> start and again if the model changes; claims are limited to tasks matching your tier.

Docs: `protocol.md` gains an "Execution tiers" section covering the policy file, default,
resolution order, claim rule, and override. README's everyday workflow shows `--model`.

### 6. Code placement

- `src/agops/policy.py` (new): load and validate `tiers.yaml`; `tier_for_model`;
  default file content.
- `src/agops/sessions.py` (new): session record read/write; `resolve_model`.
- `hub.py`: `brief` declaration and filtering; `claim_task` checks and payload;
  `_default_policy` write in setup path.
- `state.py`: new `TaskState` fields; `validate_plan` accepts an optional policy for tier
  checking.
- `cli.py`, `mcp_server.py`, `setup.py`: surface changes above.

## Testing

All through the existing `conftest` fixtures.

Policy:
- default file parses and validates;
- overlapping patterns rejected; `default_task_tier` not in `tiers` rejected;
- case-insensitive glob match; unmatched model resolves to `unknown`;
- absent file: checks skipped, `doctor` warns;
- invalid file: `scan` fails, every claim rejected.

Declaration:
- env beats argument beats record;
- brief with `model` writes the record; brief again with a new model overwrites it;
- undeclared brief shows the nudge and the full task list;
- declared brief hides off-tier tasks and shows the per-plan hidden count.

Claim:
- matching tier succeeds and the event carries `model`, `tier`, `tier_override: false`;
- mismatch rejected with the exact message; undeclared rejected; unknown model rejected;
- `--allow-tier-mismatch` refused under `AGOPS_AGENT_SESSION` and when stdin is not a TTY;
- override succeeds interactively and stamps `tier_override: true`;
- MCP path has no override.

Replay:
- claim events without the new fields load and produce `model=None`.

Concurrency:
- two sessions of different tiers race for one task; only the matching tier can win, and the
  loser's error is the tier message, not a stale-claim message.

## Open follow-ups (out of scope)

- Overview view over sessions, claims, and tiers (sub-project C).
- Policy layer for safeguards and instructions, and the status-as-knowledge token leak
  (sub-project B).
- Cost reporting from recorded model ids.
