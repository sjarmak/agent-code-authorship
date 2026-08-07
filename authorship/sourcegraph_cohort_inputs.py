"""Structural identity validation for longitudinal cohort inputs."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Any


def _duplicate_identity(
    rows: Any,
    identity: Callable[[Mapping[str, Any]], Hashable],
) -> bool:
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return False
    identities = [identity(row) for row in rows]
    missing = any(
        value is None
        or (isinstance(value, tuple) and any(part is None for part in value))
        for value in identities
    )
    return missing or len(set(identities)) != len(identities)


def validate_unique_input_identities(
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
) -> list[str]:
    """Return errors for duplicate or missing identities at frozen input boundaries."""
    checks: Sequence[tuple[str, Any, Callable[[Mapping[str, Any]], Hashable]]] = (
        (
            "adoption repository",
            adoption.get("repositories"),
            lambda row: row.get("repository_id"),
        ),
        (
            "agent commit",
            agent_commits.get("commits"),
            lambda row: (row.get("repository_id"), row.get("commit_oid")),
        ),
        (
            "AI-ban repository",
            ai_ban.get("repositories"),
            lambda row: row.get("canonical_repository_id"),
        ),
        (
            "target repository",
            targets.get("repositories"),
            lambda row: row.get("id"),
        ),
        (
            "survival candidate",
            survival.get("candidates"),
            lambda row: (
                row.get("selection_key")
                or (row.get("repository_id"), row.get("agent_family"))
            ),
        ),
    )
    return [
        f"duplicate or missing {label} identity"
        for label, rows, identity in checks
        if _duplicate_identity(rows, identity)
    ]
