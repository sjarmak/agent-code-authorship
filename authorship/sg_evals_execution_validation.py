"""Pure validation helpers for legacy and current executor commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def safe_branch(branch: Any) -> bool:
    return (
        isinstance(branch, str)
        and bool(BRANCH_PATTERN.fullmatch(branch))
        and ".." not in branch
        and "@{" not in branch
    )


def legacy_sync_argv(action: Mapping[str, Any], destination: str) -> list[str]:
    return [
        "gh",
        "repo",
        "sync",
        destination,
        "--source",
        action["canonical_repository_id"],
    ]
