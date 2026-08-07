"""Extract deterministic timing and diff-process evidence from pinned Git."""

from __future__ import annotations

import hashlib
import subprocess
from collections import defaultdict, deque
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BURST_WINDOW = timedelta(minutes=15)


class CommitTimingEvidenceError(ValueError):
    """Pinned Git history cannot satisfy the timing evidence contract."""


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CommitTimingEvidenceError(
            f"git {' '.join(args)} failed: {detail}"
        )
    return completed.stdout


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CommitTimingEvidenceError(
            f"invalid ISO-8601 timestamp: {value}"
        ) from error
    if parsed.tzinfo is None:
        raise CommitTimingEvidenceError(f"timestamp has no UTC offset: {value}")
    return parsed


def _identity_hash(
    repository_id: str,
    name: str,
    email: str,
) -> str:
    normalized = "\0".join(
        (repository_id.casefold(), name.strip().casefold(), email.strip().casefold())
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def _metadata(
    repository_id: str,
    path: Path,
    cutoff_commit: str,
    start_at: str,
) -> tuple[dict[str, Any], ...]:
    raw = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        f"--since={start_at}",
        (
            "--format=%x1e%H%x00%P%x00%aI%x00%cI%x00"
            "%an%x00%ae%x00%cn%x00%ce"
        ),
        cutoff_commit,
    )
    records = []
    for chunk in raw.split("\x1e"):
        if not chunk.strip():
            continue
        fields = chunk.strip("\n").split("\x00")
        if len(fields) != 8:
            raise CommitTimingEvidenceError(
                "git log returned malformed timing metadata"
            )
        (
            commit,
            parents,
            authored_at,
            committed_at,
            author_name,
            author_email,
            committer_name,
            committer_email,
        ) = fields
        records.append(
            {
                "commit": commit,
                "parents": tuple(parents.split()),
                "authored_at": authored_at,
                "committed_at": committed_at,
                "author_identity_sha256": _identity_hash(
                    repository_id, author_name, author_email
                ),
                "committer_identity_sha256": _identity_hash(
                    repository_id, committer_name, committer_email
                ),
            }
        )
    return tuple(
        sorted(
            records,
            key=lambda record: (
                _time(record["committed_at"]).astimezone(timezone.utc),
                record["commit"],
            ),
        )
    )


def _empty_diff() -> dict[str, int]:
    return {
        "files_changed": 0,
        "lines_added": 0,
        "lines_deleted": 0,
        "binary_file_count": 0,
        "rename_entry_count": 0,
    }


def _diff_features(
    path: Path,
    cutoff_commit: str,
    start_at: str,
) -> dict[str, dict[str, int]]:
    raw = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        f"--since={start_at}",
        "--format=%x1e%H",
        "--numstat",
        "--find-renames=50%",
        cutoff_commit,
    )
    features: dict[str, dict[str, int]] = {}
    current: dict[str, int] | None = None
    for line in raw.split("\n"):
        if line.startswith("\x1e"):
            commit = line.removeprefix("\x1e").strip()
            current = _empty_diff()
            features[commit] = current
            continue
        if not line.strip():
            continue
        if current is None:
            raise CommitTimingEvidenceError(
                "numstat row appeared before its commit header"
            )
        parts = line.split("\t", 2)
        if len(parts) != 3:
            raise CommitTimingEvidenceError(f"malformed numstat row: {line}")
        added, deleted, changed_path = parts
        current["files_changed"] += 1
        current["rename_entry_count"] += int("=>" in changed_path)
        if added == "-" or deleted == "-":
            current["binary_file_count"] += 1
            continue
        current["lines_added"] += int(added)
        current["lines_deleted"] += int(deleted)
    return features


def _gap_seconds(current: datetime, previous: datetime | None) -> float | None:
    if previous is None:
        return None
    return (current - previous).total_seconds()


def _timing_rows(
    metadata: tuple[dict[str, Any], ...],
    diff_by_commit: dict[str, dict[str, int]],
) -> dict[str, dict[str, Any]]:
    rows = {}
    previous_repository_time: datetime | None = None
    previous_author_time: dict[str, datetime] = {}
    author_windows: dict[str, deque[datetime]] = defaultdict(deque)
    for record in metadata:
        authored = _time(record["authored_at"])
        committed = _time(record["committed_at"]).astimezone(timezone.utc)
        author_id = record["author_identity_sha256"]
        window = author_windows[author_id]
        while window and committed - window[0] > BURST_WINDOW:
            window.popleft()
        row = {
            "commit": record["commit"],
            "authored_at": record["authored_at"],
            "committed_at": record["committed_at"],
            "author_identity_sha256": author_id,
            "committer_identity_sha256": record["committer_identity_sha256"],
            "author_committer_same": (
                author_id == record["committer_identity_sha256"]
            ),
            "author_committer_delta_seconds": (
                committed - authored.astimezone(timezone.utc)
            ).total_seconds(),
            "repository_previous_gap_seconds": _gap_seconds(
                committed, previous_repository_time
            ),
            "same_author_previous_gap_seconds": _gap_seconds(
                committed, previous_author_time.get(author_id)
            ),
            "same_author_prior_15m_count": len(window),
            "author_local_hour": authored.hour,
            "author_weekday": authored.weekday(),
            "author_utc_offset_minutes": int(
                (authored.utcoffset() or timedelta()).total_seconds() / 60
            ),
            "parent_count": len(record["parents"]),
            **diff_by_commit.get(record["commit"], _empty_diff()),
        }
        rows[record["commit"]] = row
        previous_repository_time = committed
        previous_author_time[author_id] = committed
        window.append(committed)
    return rows


def extract_repository_evidence(
    *,
    repository_id: str,
    path: Path,
    cutoff_commit: str,
    start_at: str,
    eligible_commits: Collection[str],
) -> dict[str, dict[str, Any]]:
    """Return timing/process evidence for an exact set of eligible commits."""
    try:
        _git(path, "cat-file", "-e", f"{cutoff_commit}^{{commit}}")
    except CommitTimingEvidenceError as error:
        raise CommitTimingEvidenceError(
            f"{repository_id}: cutoff commit is unavailable"
        ) from error
    metadata = _metadata(repository_id, path, cutoff_commit, start_at)
    rows = _timing_rows(
        metadata,
        _diff_features(path, cutoff_commit, start_at),
    )
    missing = sorted(set(eligible_commits) - rows.keys())
    if missing:
        raise CommitTimingEvidenceError(
            f"{repository_id}: eligible commit {missing[0]} is absent from history"
        )
    return {
        record["commit"]: rows[record["commit"]]
        for record in metadata
        if record["commit"] in eligible_commits
    }
