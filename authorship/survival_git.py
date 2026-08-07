"""Pinned Git snapshot and ancestry operations for survival cohorts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class SurvivalGitError(RuntimeError):
    """Raised when a repository cannot support cutoff-aware lineage."""


def _environment() -> dict[str, str]:
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if key not in {"GH_TOKEN", "GITHUB_TOKEN"}
        },
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(
    repo: Path,
    arguments: tuple[str, ...],
    *,
    input_text: str | None,
    check: bool,
) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo.resolve()}",
            "-c",
            "core.quotePath=false",
            "-C",
            str(repo),
            *arguments,
        ],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_environment(),
    )
    if check and result.returncode:
        raise SurvivalGitError(
            result.stderr.strip() or f"git {' '.join(arguments)} failed"
        )
    return result.stdout.strip()


def git(
    repo: Path,
    *arguments: str,
    check: bool = True,
) -> str:
    return _run_git(repo, arguments, input_text=None, check=check)


def git_with_stdin(
    repo: Path,
    input_text: str,
    *arguments: str,
    check: bool = True,
) -> str:
    """Run one Git process with revision data supplied on standard input."""
    return _run_git(repo, arguments, input_text=input_text, check=check)


def clone_or_fetch(repository_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / ".git").exists():
        git(destination, "fetch", "--quiet", "--prune", "origin")
        return
    result = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            f"{repository_url.rstrip('/')}.git",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
    )
    if result.returncode:
        raise SurvivalGitError(result.stderr.strip() or "git clone failed")


def inspect_repository(
    repository: Path,
    *,
    default_branch: str,
    cutoff: str,
    attributed_commits: list[str],
) -> dict[str, Any]:
    branch = default_branch
    if git(
        repository, "show-ref", "--verify", f"refs/remotes/origin/{branch}", check=False
    ):
        branch = f"origin/{branch}"
    cutoff_commit = git(repository, "rev-list", "-1", f"--before={cutoff}", branch)
    if not cutoff_commit:
        raise SurvivalGitError("default branch has no commit at or before cutoff")
    cutoff_tree = git(repository, "show", "-s", "--format=%T", cutoff_commit)
    default_history = set(git(repository, "rev-list", cutoff_commit).splitlines())
    all_history = set(git(repository, "rev-list", "--all").splitlines())
    attributed = sorted(set(attributed_commits))
    reachable = [commit for commit in attributed if commit in default_history]
    unreachable = [
        commit
        for commit in attributed
        if commit in all_history and commit not in default_history
    ]
    missing = [commit for commit in attributed if commit not in all_history]
    return {
        "default_branch": default_branch,
        "cutoff": cutoff,
        "cutoff_commit": cutoff_commit,
        "cutoff_tree": cutoff_tree,
        "reachable_attributed_commits": reachable,
        "unreachable_attributed_commits": unreachable,
        "missing_attributed_commits": missing,
    }
