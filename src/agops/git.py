from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GitError(RuntimeError):
    pass


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return result


@contextmanager
def repository_lock(root: Path) -> Iterator[None]:
    lock_root = Path(os.environ.get("AGOPS_LOCK_DIR", root.parent))
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / ".agops.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_managed_clone(root: Path) -> bool:
    return (root / ".agops-managed").exists() or os.environ.get("AGOPS_TESTING") == "1"


def remote_url(root: Path) -> str | None:
    """The origin URL of a runtime clone, or None for a local-only hub."""
    result = run_git(root, "remote", "get-url", "origin", check=False)
    url = result.stdout.strip()
    return url if result.returncode == 0 and url else None


def has_remote(root: Path) -> bool:
    return remote_url(root) is not None


def assert_clean(root: Path) -> None:
    status = run_git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if status:
        raise GitError("agops runtime clone has tracked changes; run agops doctor")


def sync_from_remote(root: Path) -> None:
    if not has_remote(root):
        return
    run_git(root, "fetch", "origin", "main")
    local = run_git(root, "rev-parse", "HEAD").stdout.strip()
    remote = run_git(root, "rev-parse", "origin/main").stdout.strip()
    if local == remote:
        return
    base = run_git(root, "merge-base", "HEAD", "origin/main").stdout.strip()
    if base != local:
        raise GitError("Runtime clone contains unpushed or divergent commits")
    run_git(root, "merge", "--ff-only", "origin/main")


def commit_and_push(root: Path, message: str) -> bool:
    run_git(root, "add", "memory")
    staged = run_git(root, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        raise GitError("Operation produced no durable state")
    run_git(root, "commit", "-m", message)
    if not has_remote(root):
        return True  # local-only hub: the repository lock is the whole concurrency story
    pushed = run_git(root, "push", "origin", "HEAD:main", check=False)
    return pushed.returncode == 0


def recover_after_rejected_push(root: Path) -> None:
    if not is_managed_clone(root):
        raise GitError("Push race in a human checkout; use the managed runtime clone")
    run_git(root, "fetch", "origin", "main", check=False)
    run_git(root, "reset", "--hard", "origin/main")
