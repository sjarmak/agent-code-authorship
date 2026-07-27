"""Reconstruct attributable source lines from merged Git diffs."""
from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from authorship.languages import SKIP_PATH, lang_of
from authorship.survival_git import git


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def pr_diff_base(
    repository: Path,
    merge_commit: str,
    source_commits: list[str],
    first_parent_positions: dict[str, int],
) -> str:
    revision = git(repository, "rev-list", "--parents", "-n", "1", merge_commit)
    parts = revision.split()
    parents = parts[1:]
    if not parents:
        raise ValueError(f"merge result {merge_commit} has no parent")
    if len(parents) > 1:
        return parents[0]
    rebased = sorted(
        (commit for commit in source_commits if commit in first_parent_positions),
        key=first_parent_positions.__getitem__,
    )
    if rebased:
        return git(repository, "rev-parse", f"{rebased[0]}^")
    return parents[0]


def extract_added_lines(
    repository: Path, base: str, head: str, language: str
) -> list[dict[str, Any]]:
    patch = git(
        repository,
        "diff",
        "--no-color",
        "--unified=0",
        "--find-renames",
        base,
        head,
        "--",
    )
    return _parse_added_patch(patch, language)


def _parse_added_patch(patch: str, language: str) -> list[dict[str, Any]]:
    path: str | None = None
    new_line: int | None = None
    records = []
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            token = shlex.split(raw[4:])[0]
            path = None if token == "/dev/null" else token.removeprefix("b/")
            continue
        match = HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            text = raw[1:]
            if (
                text.strip()
                and lang_of(path) == language
                and not SKIP_PATH(path.lower())
            ):
                records.append(
                    {
                        "path": path,
                        "line_number": new_line,
                        "text": text,
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                )
            new_line += 1
        elif raw.startswith("-"):
            continue
        elif not raw.startswith("\\"):
            new_line += 1
    return records


def extract_first_parent_many(
    repository: Path, heads: list[str], language: str
) -> dict[str, list[dict[str, Any]]]:
    if not heads:
        return {}
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository.resolve()}",
            "-C",
            str(repository),
            "log",
            "--no-walk",
            "--stdin",
            "--format=__COMMIT__%H",
            "--first-parent",
            "-m",
            "-p",
            "--unified=0",
            "--find-renames",
        ],
        input="\n".join(heads) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "bulk git log failed")
    output: dict[str, list[dict[str, Any]]] = {}
    current: str | None = None
    parts: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("__COMMIT__"):
            if current is not None:
                output[current] = _parse_added_patch("\n".join(parts), language)
            current = line.removeprefix("__COMMIT__")
            parts = []
        else:
            parts.append(line)
    if current is not None:
        output[current] = _parse_added_patch("\n".join(parts), language)
    return output


def commit_parents_many(
    repository: Path, commits: list[str]
) -> dict[str, list[str]]:
    if not commits:
        return {}
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repository.resolve()}",
            "-C",
            str(repository),
            "rev-list",
            "--no-walk",
            "--parents",
            "--stdin",
        ],
        input="\n".join(commits) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "bulk parent lookup failed")
    return {
        parts[0]: parts[1:]
        for raw in result.stdout.splitlines()
        if (parts := raw.split())
    }
