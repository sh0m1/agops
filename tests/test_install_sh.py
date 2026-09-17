from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"


def test_install_sh_exists_and_is_posix_syntax() -> None:
    assert SCRIPT.exists()
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
    if shutil.which("dash"):
        subprocess.run(["dash", "-n", str(SCRIPT)], check=True)


@pytest.fixture
def empty_path(tmp_path: Path) -> dict[str, str]:
    """A PATH with only the POSIX shell utilities the script needs, no uv/agent-hub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sh", "cat", "printf"):
        found = shutil.which(name)
        if found:
            os.symlink(found, bin_dir / name)
    return {"PATH": str(bin_dir), "HOME": str(tmp_path / "home")}


def test_install_sh_dry_run_prints_commands_and_touches_nothing(
    empty_path: dict[str, str], tmp_path: Path
) -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT), "--remote", "https://example.invalid/m.git", "--dry-run"],
        env=empty_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "[dry-run] sh -c curl -fsSL https://astral.sh/uv/install.sh | sh" in out
    assert "[dry-run] uv tool install --force git+https://github.com/sh0m1/agops@v0.6.1" in out
    assert "[dry-run] agent-hub setup --remote https://example.invalid/m.git" in out
    assert not (tmp_path / "home").exists()


def test_install_sh_ref_and_passthrough_flags(empty_path: dict[str, str]) -> None:
    result = subprocess.run(
        [
            "sh",
            str(SCRIPT),
            "--ref=v9.9.9",
            "--keep-claude-memory",
            "--dry-run",
        ],
        env=empty_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "agops@v9.9.9" in result.stdout
    assert "[dry-run] agent-hub setup --keep-claude-memory" in result.stdout
    assert "--remote" not in result.stdout.split("agent-hub setup", 1)[1]


def test_install_sh_rejects_unknown_flag(empty_path: dict[str, str]) -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT), "--bogus"], env=empty_path, text=True, capture_output=True
    )
    assert result.returncode == 1
    assert "unknown argument: --bogus" in result.stderr
    assert "Usage:" in result.stderr


def test_install_sh_local_passthrough_and_conflict(empty_path: dict[str, str]) -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT), "--local", "--dry-run"], env=empty_path, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert "[dry-run] agent-hub setup --local" in result.stdout
    conflict = subprocess.run(
        ["sh", str(SCRIPT), "--local", "--remote", "https://x.invalid/r.git", "--dry-run"],
        env=empty_path,
        text=True,
        capture_output=True,
    )
    assert conflict.returncode == 1
    assert "--local and --remote" in conflict.stderr


def test_install_sh_profile_passthrough(empty_path: dict[str, str]) -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT), "--profile", "team", "--dry-run"],
        env=empty_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "[dry-run] agent-hub setup --profile team" in result.stdout
