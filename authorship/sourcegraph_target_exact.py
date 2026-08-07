"""Exact current-snapshot target units derived from Sourcegraph blob and blame."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.language_features import primary_features

SHARD_VERSION = 1
SHA1_PATTERN = re.compile(r"[0-9a-f]{40}")


class TargetExactError(RuntimeError):
    """Raised when raw Sourcegraph target evidence is incomplete or drifts."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def target_file_shard_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "target_file_shard_sha256"
    }
    return _sha256(content)


def _path_type(path: str) -> str:
    lowered = path.lower()
    name = Path(lowered).name
    if (
        "/test" in f"/{lowered}"
        or "/tests/" in f"/{lowered}/"
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
    ):
        return "test"
    return "source"


def _validated_lines(response: Mapping[str, Any]) -> list[str]:
    content = response.get("content")
    if not isinstance(content, str):
        raise TargetExactError("Sourcegraph blob content is missing")
    if not content:
        return []
    return content.splitlines()


def _validated_blame(
    response: Mapping[str, Any], line_count: int
) -> list[Mapping[str, Any]]:
    blame = response.get("blame")
    if not isinstance(blame, list):
        raise TargetExactError("Sourcegraph blame hunks are missing")
    ordered = sorted(blame, key=lambda row: row.get("startLine", 0))
    expected_start = 1
    for hunk in ordered:
        start, end = hunk.get("startLine"), hunk.get("endLine")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start != expected_start
            or end <= start
        ):
            raise TargetExactError("blame must cover every content line exactly once")
        expected_start = end
    if expected_start != line_count + 1:
        raise TargetExactError("blame must cover every content line exactly once")
    return ordered


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise TargetExactError("blame commit timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TargetExactError("blame commit timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise TargetExactError("blame commit timestamp lacks timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _commit_fields(hunk: Mapping[str, Any]) -> tuple[str, str]:
    commit = hunk.get("commit")
    if not isinstance(commit, Mapping):
        raise TargetExactError("blame commit is missing")
    oid = commit.get("oid")
    if not isinstance(oid, str) or not SHA1_PATTERN.fullmatch(oid):
        raise TargetExactError("blame commit OID is invalid")
    author = commit.get("author")
    committer = commit.get("committer")
    value = author.get("date") if isinstance(author, Mapping) else None
    if value is None and isinstance(committer, Mapping):
        value = committer.get("date")
    return oid, _timestamp(value)


def _in_range(timestamp: str, bounds: Sequence[str]) -> bool:
    if len(bounds) != 2:
        raise TargetExactError("effective date range is invalid")
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    lower = datetime.fromisoformat(bounds[0].replace("Z", "+00:00"))
    upper = datetime.fromisoformat(bounds[1].replace("Z", "+00:00"))
    return lower <= value <= upper


def _unit(
    task: Mapping[str, Any],
    response: Mapping[str, Any],
    hunk: Mapping[str, Any],
    lines: Sequence[str],
) -> dict[str, Any]:
    start, end = hunk["startLine"], hunk["endLine"]
    source_lines = list(lines[start - 1 : end - 1])
    oid, introduced_at = _commit_fields(hunk)
    identity = {
        "repository_id": task["repository_id"],
        "snapshot_commit": task["cutoff_commit"],
        "path": task["path"],
        "introducing_commit_oid": oid,
        "start_line": start,
        "end_line_exclusive": end,
    }
    normalized = "\n".join(line.rstrip() for line in source_lines)
    sampling_weight = float(task.get("file_sampling_weight", 1.0))
    return {
        **identity,
        "sourcegraph_name": task["sourcegraph_name"],
        "introduced_at": introduced_at,
        "calendar_time": introduced_at,
        "language": task["language"],
        "line_numbers": list(range(start, end)),
        "line_count": len(source_lines),
        "file_inclusion_probability": float(
            task.get("file_inclusion_probability", 1.0)
        ),
        "file_sampling_weight": sampling_weight,
        "weighted_line_count": len(source_lines) * sampling_weight,
        "path_type": _path_type(task["path"]),
        "target_role": "fixed_confirmatory_target",
        "label": "unknown",
        "unit_sha256": _sha256(identity),
        "content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "sourcegraph_record_sha256": _sha256(response),
        "feature_values": primary_features(source_lines, task["language"]),
    }


def _validate_identity(task: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    expected = {
        "repository_name": task.get("sourcegraph_name"),
        "commit_oid": task.get("cutoff_commit"),
        "path": task.get("path"),
    }
    for field, value in expected.items():
        if response.get(field) != value:
            raise TargetExactError(f"Sourcegraph {field} does not match target task")


def materialize_target_file(
    task: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any]:
    _validate_identity(task, response)
    lines = _validated_lines(response)
    blame = _validated_blame(response, len(lines))
    units = [
        _unit(task, response, hunk, lines)
        for hunk in blame
        if _in_range(_commit_fields(hunk)[1], task["effective_date_range"])
    ]
    document = {
        "target_file_shard_version": SHARD_VERSION,
        "status": "success",
        "task_sha256": task.get("task_sha256"),
        "repository_id": task.get("repository_id"),
        "path": task.get("path"),
        "raw_response": response,
        "raw_blame_hunk_count": len(blame),
        "retained_unit_count": len(units),
        "units": units,
    }
    return {**document, "target_file_shard_sha256": target_file_shard_sha256(document)}


def validate_target_file_shard(
    shard: Mapping[str, Any], task: Mapping[str, Any]
) -> list[str]:
    errors = []
    if shard.get("target_file_shard_sha256") != target_file_shard_sha256(shard):
        errors.append("target file shard SHA-256 does not match")
    try:
        expected = materialize_target_file(task, shard.get("raw_response") or {})
    except TargetExactError as error:
        return [*errors, str(error)]
    if _canonical_json(shard.get("units")) != _canonical_json(expected["units"]):
        errors.append("derived target units do not match raw Sourcegraph response")
    for field in (
        "status",
        "task_sha256",
        "repository_id",
        "path",
        "raw_blame_hunk_count",
        "retained_unit_count",
    ):
        if shard.get(field) != expected.get(field):
            errors.append(f"target file shard {field} does not match")
    return errors
