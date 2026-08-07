"""Plan-level execution for resumable longitudinal Sourcegraph probes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from authorship.sg import api
from authorship.sourcegraph_longitudinal import LongitudinalExtractionError, _is_hex
from authorship.sourcegraph_longitudinal_batch import (
    BATCH_PLAN_VERSION,
    longitudinal_batch_plan_sha256,
)
from authorship.sourcegraph_longitudinal_probe import execute_probe_unit

PROBE_EXECUTION_VERSION = 3


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def probe_execution_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "probe_execution_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.utcoffset() is None:
        raise LongitudinalExtractionError("execution clock must be timezone aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_units(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
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
    if any(not _is_hex(identifier, 64) for identifier in identifiers):
        raise LongitudinalExtractionError("batch plan unit contract is invalid")
    if len(identifiers) != len(set(identifiers)):
        raise LongitudinalExtractionError("batch plan probe unit IDs are duplicated")
    return units


def _failure_summary(unit: Mapping[str, Any], error: Exception) -> dict[str, Any]:
    repository = unit.get("repository")
    repository_id = (
        repository.get("canonical_repository_id")
        if isinstance(repository, Mapping)
        else None
    )
    return {
        "probe_unit_id": unit.get("probe_unit_id"),
        "canonical_repository_id": repository_id,
        "valid": False,
        "reused": False,
        "error": str(error)[:500],
    }


def _execute_units(
    units: list[Mapping[str, Any]],
    api_runner: Callable[..., Mapping[str, Any]],
) -> list[dict[str, Any]]:
    summaries = []
    for unit in units:
        try:
            summary = execute_probe_unit(unit, api_runner=api_runner)
        except (LongitudinalExtractionError, RuntimeError, ValueError) as error:
            summaries.append(_failure_summary(unit, error))
        else:
            summaries.append({**summary, "valid": True})
    return summaries


def execute_probe_plan(
    plan: Mapping[str, Any],
    *,
    api_runner: Callable[..., Mapping[str, Any]] = api,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Execute every frozen probe unit and report partial failures durably."""
    units = _validated_units(plan)
    started_at = _timestamp(clock)
    summaries = _execute_units(units, api_runner)
    completed_at = _timestamp(clock)
    valid_count = sum(summary["valid"] for summary in summaries)
    reused_count = sum(summary["reused"] for summary in summaries)
    document = {
        "probe_execution_version": PROBE_EXECUTION_VERSION,
        "longitudinal_batch_plan_sha256": plan["longitudinal_batch_plan_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": "complete" if valid_count == len(units) else "incomplete",
        "unit_count": len(units),
        "valid_unit_count": valid_count,
        "invalid_unit_count": len(units) - valid_count,
        "executed_unit_count": len(units) - reused_count,
        "reused_unit_count": reused_count,
        "outcomes_consulted": False,
        "units": summaries,
    }
    return {
        **document,
        "probe_execution_sha256": probe_execution_sha256(document),
    }
