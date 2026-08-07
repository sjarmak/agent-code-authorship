"""Cross-file validation for Sourcegraph discovery execution artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery_execution import (
    EXECUTION_MANIFEST_VERSION,
    SUMMARY_SHARD_FIELDS,
    execution_manifest_sha256,
    shard_path_for,
    validate_result_manifest,
)


def _count_errors(document: Mapping[str, Any]) -> list[str]:
    units = document.get("units")
    if not isinstance(units, list):
        return ["units must be a list"]
    valid_count = sum(
        isinstance(unit, Mapping) and unit.get("valid") is True for unit in units
    )
    reused_count = sum(
        isinstance(unit, Mapping) and unit.get("reused") is True for unit in units
    )
    expected = {
        "unit_count": len(units),
        "valid_unit_count": valid_count,
        "invalid_unit_count": len(units) - valid_count,
        "executed_unit_count": len(units) - reused_count,
        "reused_unit_count": reused_count,
    }
    errors = [
        f"{field} does not match units"
        for field, value in expected.items()
        if document.get(field) != value
    ]
    expected_status = "complete" if valid_count == len(units) else "incomplete"
    if document.get("status") != expected_status:
        errors.append("status does not match units")
    return errors


def _shard_reference_errors(
    summary: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
) -> list[str]:
    relative_path = summary.get("shard_path")
    if not isinstance(relative_path, str):
        return ["unit shard_path must be a string"]
    try:
        expected_path = shard_path_for(summary).as_posix()
    except ValueError as error:
        return [str(error)]
    if relative_path != expected_path:
        return ["unit shard_path does not match unit identity"]
    try:
        path = (output_directory / relative_path).resolve()
        path.relative_to(output_directory.resolve())
        shard = json.loads(path.read_text())
    except ValueError:
        return [f"unit shard resolves outside output directory: {relative_path}"]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [f"unit shard is unreadable: {relative_path}"]
    if not isinstance(shard, Mapping):
        return [f"unit shard must contain an object: {relative_path}"]
    errors = validate_result_manifest(shard, specification)
    for field in SUMMARY_SHARD_FIELDS:
        if shard.get(field) != summary.get(field):
            errors.append(f"summary field does not match shard: {field}")
    return errors


def _frozen_unit_errors(
    units: Any, expected_units: Sequence[Mapping[str, Any]] | None
) -> list[str]:
    if expected_units is None or not isinstance(units, list):
        return []
    identity_fields = (
        "unit_id",
        "query_family_id",
        "canonical_repository_id",
        "sourcegraph_name",
        "cutoff_commit",
        "rendered_query",
        "rendered_query_sha256",
    )
    observed = [
        tuple(unit.get(field) for field in identity_fields)
        for unit in units
        if isinstance(unit, Mapping)
    ]
    expected = [
        tuple(unit.get(field) for field in identity_fields) for unit in expected_units
    ]
    return (
        []
        if observed == expected and len(observed) == len(units)
        else ["execution units do not match frozen population"]
    )


def validate_execution_manifest(
    document: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    expected_units: Sequence[Mapping[str, Any]] | None = None,
    expected_index_audit_sha256: str | None = None,
) -> list[str]:
    errors = []
    if document.get("execution_manifest_version") != EXECUTION_MANIFEST_VERSION:
        errors.append("execution_manifest_version must equal 3")
    if document.get("specification_sha256") != specification.get(
        "specification_sha256"
    ):
        errors.append("specification_sha256 does not match")
    if document.get("index_manifest_sha256") != specification.get(
        "index_manifest_sha256"
    ):
        errors.append("index_manifest_sha256 does not match specification")
    if (
        expected_index_audit_sha256 is not None
        and document.get("index_audit_sha256") != expected_index_audit_sha256
    ):
        errors.append("index_audit_sha256 does not match frozen audit")
    if document.get("outcomes_consulted") is not False:
        errors.append("execution manifest must be outcome blind")
    if document.get("execution_manifest_sha256") != execution_manifest_sha256(document):
        errors.append("execution_manifest_sha256 does not match")
    errors.extend(_count_errors(document))
    units = document.get("units")
    errors.extend(_frozen_unit_errors(units, expected_units))
    if not isinstance(units, list):
        return errors
    for summary in units:
        if not isinstance(summary, Mapping):
            errors.append("unit summaries must be objects")
            continue
        errors.extend(_shard_reference_errors(summary, specification, output_directory))
    return errors
