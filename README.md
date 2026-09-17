# agops

agops is a Git-native shared memory and execution ledger for local coding agents. It gives
Codex, Claude Code, Cursor, Gemini CLI, and shell-capable agents one readable source of truth for
knowledge, approved plans, task claims, checkpoints, and completion evidence.

It does not call a model API and does not depend on a particular subscription. Git is the durable
store; a small CLI provides safe state transitions, and a stdio MCP server exposes the same
operations to clients that support MCP.

agops was previously named Agent Hub. The Python package is still `agent_hub`, the runtime paths
still live under `agent-hub`, and the environment variables still use the `AGENT_HUB_` prefix; the
old `agent-hub` command remains as an alias for `agops`.

## Install

```sh
uv tool install .
agops setup --remote https://github.com/you/agent-hub-memory.git
agops doctor
agops scan
```

`setup` creates a dedicated runtime clone at `~/.local/share/agent-hub/repo`, adds bounded managed
blocks to the Codex and Claude user instruction files, and configures the local MCP server. Existing
configuration is preserved and backed up before it is changed.

Add repository-level instructions for tools that do not load the user configuration:

```sh
agops adapter install /path/to/project --tools agents,claude,gemini,cursor,copilot
```

Only a marked managed block is added or replaced; existing project instructions are preserved.

## Everyday workflow

```sh
export AGENT_HUB_ACTOR=codex
export AGENT_HUB_SESSION="$(agops session --actor codex --value)"
agops brief --cwd "$PWD" --model gpt-5.6-terra
agops plan list
agops task ready --tier standard
agops task claim PLAN TASK --cwd "$PWD"
agops task checkpoint PLAN TASK --summary "Implemented parser" --evidence "pytest: 12 passed"
agops task complete PLAN TASK --evidence "commit: abc123" --evidence "pytest: 12 passed"
```

## Tool access order

The managed instruction block tells agents to try a CLI or MCP tool first for any external system,
and to fall back to a browser only when no CLI or MCP route exists, it is scoped wrong, it fails, or
it cannot answer the question. The browser stays allowed; it is just not the first choice, and the
agent should say why it fell back.

Tasks carry a `tier` (default `standard`); `memory/policy/tiers.yaml` maps model ids to tiers.
Frontier models plan and review, cheaper models execute, and the hub rejects claims that cross
tiers. See [docs/protocol.md](docs/protocol.md#execution-tiers).

For supported tools, prefer the managed wrapper. Its options precede the tool name:

```sh
agops run --plan PLAN --task TASK --cwd /path/to/worktree --model gpt-5.6-terra codex
```

The wrapper identifies the session, claims the task, synchronizes before launch, and renews the
lease while the process is alive.

Plans are drafted from YAML and become executable only after interactive approval:

```sh
agops plan draft plan.yaml
agops plan approve my-plan
```

See [docs/protocol.md](docs/protocol.md) for schemas, state transitions, concurrency semantics, and
the generic agent integration contract.
