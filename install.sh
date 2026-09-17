#!/bin/sh
# Agent Hub bootstrap: installs uv if needed, installs agent-hub, runs setup.
#
#   curl -fsSL https://raw.githubusercontent.com/sh0m1/agops/main/install.sh | sh
#
# Add `-s -- --remote <git-url>` to sync the memory across machines. Re-running is safe:
# setup is idempotent and remembers the remote.
set -eu

DEFAULT_REF="v0.6.1"
REPO_URL="https://github.com/sh0m1/agops"

REMOTE=""
LOCAL=0
PROFILE=""
REF="$DEFAULT_REF"
DRY_RUN=0
KEEP_CLAUDE_MEMORY=0

usage() {
    cat <<EOF
Usage: install.sh [--remote <git-url> | --local] [--profile <name>] [--ref <tag>]
                  [--keep-claude-memory] [--dry-run]

  --remote <git-url>     Git remote to sync the memory repository with. Without it (and with
                         none remembered from an earlier run) the hub is local to this machine.
  --local                Keep the memory on this machine only; detaches and forgets any remote.
  --profile <name>       Hub profile to create or refresh (e.g. team, private).
  --ref <tag>            agent-hub version to install (default: $DEFAULT_REF).
  --keep-claude-memory   Leave Claude Code's automatic memory enabled.
  --dry-run              Print the commands that would run; touch neither network nor disk.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --remote) REMOTE="$2"; shift 2 ;;
        --remote=*) REMOTE="${1#--remote=}"; shift ;;
        --local) LOCAL=1; shift ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --profile=*) PROFILE="${1#--profile=}"; shift ;;
        --ref) REF="$2"; shift 2 ;;
        --ref=*) REF="${1#--ref=}"; shift ;;
        --keep-claude-memory) KEEP_CLAUDE_MEMORY=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install.sh: unknown argument: %s\n' "$1" >&2; usage >&2; exit 1 ;;
    esac
done

info() { printf '==> %s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

if [ -n "$REMOTE" ] && [ "$LOCAL" = 1 ]; then
    fail "--local and --remote cannot be combined"
fi
run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

LOCAL_BIN="$HOME/.local/bin"

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
    info "uv not found; installing it"
    run sh -c "curl -fsSL https://astral.sh/uv/install.sh | sh"
    PATH="$LOCAL_BIN:$PATH"
    export PATH
    if [ "$DRY_RUN" != 1 ] && ! command -v uv >/dev/null 2>&1; then
        fail "uv was installed but is not on PATH; add $LOCAL_BIN to PATH and re-run"
    fi
fi

# 2. agent-hub
info "Installing agent-hub $REF"
run uv tool install --force "git+${REPO_URL}@${REF}"
case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *)
        PATH="$LOCAL_BIN:$PATH"
        export PATH
        printf '\nNote: %s is not on your PATH. Add this to your shell profile:\n' "$LOCAL_BIN"
        printf '  export PATH="$HOME/.local/bin:$PATH"\n\n'
        ;;
esac
if [ "$DRY_RUN" != 1 ] && ! command -v agent-hub >/dev/null 2>&1; then
    fail "agent-hub was installed but is not on PATH; see the note above and re-run"
fi

# 3. setup
info "Running agent-hub setup"
set --
if [ -n "$REMOTE" ]; then
    set -- "$@" --remote "$REMOTE"
fi
if [ "$LOCAL" = 1 ]; then
    set -- "$@" --local
fi
if [ -n "$PROFILE" ]; then
    set -- "$@" --profile "$PROFILE"
fi
if [ "$KEEP_CLAUDE_MEMORY" = 1 ]; then
    set -- "$@" --keep-claude-memory
fi
run agent-hub setup "$@"
