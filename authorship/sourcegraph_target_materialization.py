"""Aggregate validated target shards into the frozen prevalence population."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from authorship.sourcegraph_target_execution import validate_target_execution

MATERIALIZATION_VERSION = 1


class TargetMaterializationError(RuntimeError):
    """Raised when target units cannot be aggregated without drift."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def target_materialization_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "target_materialization_sha256"
    }
    return _sha256(content)


def _read_units(
    execution: Mapping[str, Any], output_directory: Path
) -> list[dict[str, Any]]:
    units = []
    for reference in execution["references"]:
        path = output_directory / reference["shard_path"]
        shard = json.loads(path.read_text())
        units.extend(shard["units"])
    return sorted(
        units,
        key=lambda row: (
            row["repository_id"],
            row["path"],
            row["start_line"],
            row["introducing_commit_oid"],
        ),
    )


def _planned_repositories(plan: Mapping[str, Any]) -> list[str]:
    rows = plan.get("repositories")
    if isinstance(rows, list):
        names = [row.get("repository_id") for row in rows if isinstance(row, Mapping)]
        if names and all(isinstance(name, str) for name in names):
            return sorted(set(names))
    return sorted(
        {
            task["repository_id"]
            for task in plan.get("tasks", [])
            if isinstance(task, Mapping) and isinstance(task.get("repository_id"), str)
        }
    )


def materialize_target_units(
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    errors = validate_target_execution(plan, execution, output_directory)
    if errors:
        raise TargetMaterializationError("; ".join(errors))
    if execution.get("status") != "complete":
        raise TargetMaterializationError("target execution is incomplete")
    units = _read_units(execution, output_directory)
    repositories = _planned_repositories(plan)
    counts = Counter(unit["repository_id"] for unit in units)
    repository_counts = {repository: counts[repository] for repository in repositories}
    if len({unit["unit_sha256"] for unit in units}) != len(units):
        raise TargetMaterializationError("target unit identities are not unique")
    document = {
        "target_materialization_version": MATERIALIZATION_VERSION,
        "status": "complete",
        "target_unit_plan_sha256": plan.get("target_unit_plan_sha256"),
        "target_execution_sha256": execution.get("target_execution_sha256"),
        "repository_count": len(repositories),
        "processed_repository_count": len(repositories),
        "observed_repository_count": sum(
            bool(count) for count in repository_counts.values()
        ),
        "zero_unit_repositories": [
            repository for repository, count in repository_counts.items() if not count
        ],
        "unit_count": len(units),
        "line_count": sum(unit["line_count"] for unit in units),
        "weighted_line_count": sum(unit["weighted_line_count"] for unit in units),
        "repository_unit_counts": repository_counts,
        "units": units,
    }
    return {
        **document,
        "target_materialization_sha256": target_materialization_sha256(document),
    }


def validate_target_materialization(
    materialization: Mapping[str, Any],
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    output_directory: Path,
) -> list[str]:
    errors = []
    if materialization.get("target_materialization_sha256") != (
        target_materialization_sha256(materialization)
    ):
        errors.append("target materialization SHA-256 does not match")
    try:
        expected = materialize_target_units(plan, execution, output_directory)
    except TargetMaterializationError as error:
        return [*errors, str(error)]
    if _canonical_json(materialization) != _canonical_json(expected):
        errors.append("target units differ from independent shard materialization")
    return errors
