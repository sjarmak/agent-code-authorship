"""Offline materialization of frozen longitudinal Sourcegraph probes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.sourcegraph_longitudinal import (
    LongitudinalExtractionError,
    _is_hex,
)
from authorship.sourcegraph_longitudinal_batch import (
    BATCH_PLAN_VERSION,
    longitudinal_batch_plan_sha256,
    materialize_execution_unit,
)
from authorship.sourcegraph_longitudinal_execution import (
    execute_longitudinal_unit,
)
from authorship.sourcegraph_longitudinal_probe import manifest_observation
from authorship.sourcegraph_longitudinal_probe_execution import (
    PROBE_EXECUTION_VERSION,
    probe_execution_sha256,
)

MATERIALIZATION_VERSION = 3
UnitExecutor = Callable[..., dict[str, Any]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def materialization_sha256(document: Mapping[str, Any]) -> str:
    """Hash an aggregate materialization manifest without its own hash."""
    content = {
        key: value for key, value in document.items() if key != "materialization_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.utcoffset() is None:
        raise LongitudinalExtractionError("execution clock must be timezone aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_plan_units(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if plan.get("longitudinal_batch_plan_sha256") != longitudinal_batch_plan_sha256(
        plan
    ):
        raise LongitudinalExtractionError("batch plan checksum does not match")
    units = plan.get("units")
    if (
        plan.get("longitudinal_batch_plan_version") != BATCH_PLAN_VERSION
        or plan.get("outcomes_consulted") is not False
        or not isinstance(units, list)
        or plan.get("repository_count") != len(units)
        or any(not isinstance(unit, Mapping) for unit in units)
    ):
        raise LongitudinalExtractionError("batch plan contract is invalid")
    identifiers = [unit.get("probe_unit_id") for unit in units]
    if any(not _is_hex(identifier, 64) for identifier in identifiers) or len(
        identifiers
    ) != len(set(identifiers)):
        raise LongitudinalExtractionError("batch plan unit contract is invalid")
    return units


def _validated_probe_summaries(
    probe_execution: Mapping[str, Any],
    plan: Mapping[str, Any],
    planned_units: list[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if probe_execution.get("probe_execution_sha256") != probe_execution_sha256(
        probe_execution
    ):
        raise LongitudinalExtractionError("probe execution checksum does not match")
    summaries = probe_execution.get("units")
    if (
        probe_execution.get("probe_execution_version") != PROBE_EXECUTION_VERSION
        or probe_execution.get("longitudinal_batch_plan_sha256")
        != plan["longitudinal_batch_plan_sha256"]
        or probe_execution.get("outcomes_consulted") is not False
        or not isinstance(summaries, list)
        or any(not isinstance(summary, Mapping) for summary in summaries)
        or probe_execution.get("unit_count") != len(summaries)
        or len(summaries) != len(planned_units)
    ):
        raise LongitudinalExtractionError("probe execution contract is invalid")
    identifiers = [summary.get("probe_unit_id") for summary in summaries]
    expected = {unit["probe_unit_id"] for unit in planned_units}
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != expected:
        raise LongitudinalExtractionError(
            "probe execution units do not match batch plan"
        )
    planned_repositories = {
        unit["probe_unit_id"]: _repository_id(unit) for unit in planned_units
    }
    if any(
        summary.get("canonical_repository_id")
        != planned_repositories[summary["probe_unit_id"]]
        for summary in summaries
    ):
        raise LongitudinalExtractionError(
            "probe execution repository identities do not match batch plan"
        )
    valid_count = sum(summary.get("valid") is True for summary in summaries)
    invalid_count = len(summaries) - valid_count
    expected_status = "complete" if invalid_count == 0 else "incomplete"
    if (
        any(not isinstance(summary.get("valid"), bool) for summary in summaries)
        or probe_execution.get("valid_unit_count") != valid_count
        or probe_execution.get("invalid_unit_count") != invalid_count
        or probe_execution.get("status") != expected_status
    ):
        raise LongitudinalExtractionError("probe execution counts are invalid")
    return {summary["probe_unit_id"]: summary for summary in summaries}


def _repository_id(unit: Mapping[str, Any]) -> str | None:
    repository = unit.get("repository")
    if not isinstance(repository, Mapping):
        return None
    identifier = repository.get("canonical_repository_id")
    return identifier if isinstance(identifier, str) else None


def _probe_failure(
    unit: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, Any]:
    error = summary.get("error")
    if not isinstance(error, str) or not error:
        error = "Sourcegraph probe unavailable without a recorded reason"
    return {
        "probe_unit_id": unit.get("probe_unit_id"),
        "canonical_repository_id": _repository_id(unit),
        "valid": False,
        "reused": False,
        "failure_stage": "sourcegraph_probe",
        "error": error[:500],
    }


def _load_probe_manifest(
    planned_unit: Mapping[str, Any], summary: Mapping[str, Any]
) -> Mapping[str, Any]:
    expected_path = planned_unit.get("probe_manifest_path")
    observed_path = summary.get("probe_manifest_path")
    if not isinstance(expected_path, str) or observed_path != expected_path:
        raise LongitudinalExtractionError(
            "probe manifest path does not match planned unit"
        )
    try:
        manifest = json.loads(Path(expected_path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LongitudinalExtractionError(
            f"probe manifest cannot be read: {error}"
        ) from error
    if not isinstance(manifest, Mapping):
        raise LongitudinalExtractionError("probe manifest must be an object")
    expected_identity = (
        planned_unit.get("probe_unit_id"),
        _repository_id(planned_unit),
        summary.get("probe_manifest_sha256"),
    )
    actual_identity = (
        manifest.get("probe_unit_id"),
        manifest.get("canonical_repository_id"),
        manifest.get("probe_manifest_sha256"),
    )
    if actual_identity != expected_identity:
        raise LongitudinalExtractionError(
            "probe manifest identity does not match plan and execution"
        )
    return manifest


def _materialization_failure(
    unit: Mapping[str, Any], error: Exception
) -> dict[str, Any]:
    return {
        "probe_unit_id": unit.get("probe_unit_id"),
        "canonical_repository_id": _repository_id(unit),
        "valid": False,
        "reused": False,
        "failure_stage": "longitudinal_materialization",
        "error": str(error)[:500],
    }


def _materialize_valid_unit(
    protocol: Mapping[str, Any],
    planned_unit: Mapping[str, Any],
    probe_summary: Mapping[str, Any],
    output_directory: Path,
    unit_executor: UnitExecutor,
) -> dict[str, Any]:
    manifest = _load_probe_manifest(planned_unit, probe_summary)
    observation = manifest_observation(manifest)
    execution_unit = materialize_execution_unit(planned_unit, manifest)
    summary = unit_executor(
        protocol,
        execution_unit,
        output_directory,
        sourcegraph_probe=lambda _unit: observation,
    )
    if not isinstance(summary, Mapping):
        raise LongitudinalExtractionError(
            "longitudinal unit executor must return an object"
        )
    return {
        "probe_unit_id": planned_unit["probe_unit_id"],
        "canonical_repository_id": _repository_id(planned_unit),
        "valid": True,
        "execution_unit_id": execution_unit["unit_id"],
        "shard_path": summary["shard_path"],
        "longitudinal_shard_sha256": summary["longitudinal_shard_sha256"],
        "hunk_count": summary["hunk_count"],
        "excluded_hunk_count": summary["excluded_hunk_count"],
        "sourcegraph_verification_status": summary["sourcegraph_verification_status"],
        "reused": summary["reused"],
    }


def _execute_units(
    planned_units: list[Mapping[str, Any]],
    probe_summaries: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
    output_directory: Path,
    unit_executor: UnitExecutor,
) -> list[dict[str, Any]]:
    results = []
    for unit in planned_units:
        probe_summary = probe_summaries[unit["probe_unit_id"]]
        if probe_summary["valid"] is False:
            results.append(_probe_failure(unit, probe_summary))
            continue
        try:
            result = _materialize_valid_unit(
                protocol,
                unit,
                probe_summary,
                output_directory,
                unit_executor,
            )
        except (LongitudinalExtractionError, OSError, ValueError, KeyError) as error:
            results.append(_materialization_failure(unit, error))
        else:
            results.append(result)
    return results


def execute_materialization_plan(
    plan: Mapping[str, Any],
    probe_execution: Mapping[str, Any],
    protocol: Mapping[str, Any],
    output_directory: Path,
    *,
    unit_executor: UnitExecutor = execute_longitudinal_unit,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Materialize every valid probe and retain all unavailable units."""
    planned_units = _validated_plan_units(plan)
    probe_summaries = _validated_probe_summaries(probe_execution, plan, planned_units)
    started_at = _timestamp(clock)
    units = _execute_units(
        planned_units,
        probe_summaries,
        protocol,
        output_directory,
        unit_executor,
    )
    completed_at = _timestamp(clock)
    materialized_count = sum(unit["valid"] for unit in units)
    reused_count = sum(unit["reused"] for unit in units if unit["valid"])
    skipped_count = sum(
        unit.get("failure_stage") == "sourcegraph_probe" for unit in units
    )
    document = {
        "materialization_version": MATERIALIZATION_VERSION,
        "longitudinal_batch_plan_sha256": plan["longitudinal_batch_plan_sha256"],
        "probe_execution_sha256": probe_execution["probe_execution_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": ("complete" if materialized_count == len(units) else "incomplete"),
        "unit_count": len(units),
        "materialized_unit_count": materialized_count,
        "invalid_unit_count": len(units) - materialized_count,
        "attempted_unit_count": len(units) - skipped_count,
        "reused_unit_count": reused_count,
        "skipped_probe_unit_count": skipped_count,
        "outcomes_consulted": False,
        "units": units,
    }
    return {
        **document,
        "materialization_sha256": materialization_sha256(document),
    }
