"""Effective discovery inventory after deterministic timeout recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery_execution import (
    SUMMARY_SHARD_FIELDS,
    execution_manifest_sha256,
    validate_result_manifest,
)
from authorship.sourcegraph_discovery_partition import (
    build_partition_plan,
    build_query_branch_refined_partition_plan,
    build_refined_partition_plan,
    validate_partition_execution,
)
from authorship.sourcegraph_discovery_validation import validate_execution_manifest

INVENTORY_VERSION = 3


class DiscoveryInventoryError(ValueError):
    """Frozen discovery artifacts cannot form a complete effective inventory."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def effective_inventory_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "effective_inventory_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def terminal_failure_status(timeout_count: int, execution_error_count: int) -> str:
    """Name the inventory state without treating unresolved shards as negatives."""
    if timeout_count and execution_error_count:
        return "complete_with_terminal_failures"
    if timeout_count:
        return "complete_with_terminal_timeouts"
    if execution_error_count:
        return "complete_with_terminal_execution_errors"
    return "complete"


def terminal_failure_resolution(timeout_count: int, execution_error_count: int) -> str:
    """Name one partition resolution from its terminal failure counts."""
    return terminal_failure_status(timeout_count, execution_error_count).replace(
        "complete", "partition", 1
    )


def _safe_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str):
        raise DiscoveryInventoryError(f"{label} path is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DiscoveryInventoryError(f"{label} path is unsafe")
    candidate = root / relative
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise DiscoveryInventoryError(
            f"{label} path resolves outside output directory"
        ) from error
    return candidate


def _load_shard(
    root: Path, summary: Mapping[str, Any], label: str
) -> Mapping[str, Any]:
    path = _safe_path(root, summary.get("shard_path"), label)
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise DiscoveryInventoryError(f"{label} shard is unreadable") from error
    if not isinstance(document, Mapping):
        raise DiscoveryInventoryError(f"{label} shard must be an object")
    return document


def _reference(
    location: str, summary: Mapping[str, Any], shard: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "location": location,
        "shard_path": summary["shard_path"],
        "unit_id": shard["unit_id"],
        "result_manifest_sha256": shard["result_manifest_sha256"],
        "result_count": shard["result_count"],
        "result_object_count": shard["result_object_count"],
    }


def _base_artifacts(
    specification: Mapping[str, Any],
    execution: Mapping[str, Any],
    root: Path,
    expected_units: Sequence[Mapping[str, Any]],
    expected_index_audit_sha256: str,
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    errors = validate_execution_manifest(
        execution,
        specification,
        root,
        expected_units=expected_units,
        expected_index_audit_sha256=expected_index_audit_sha256,
    )
    if errors:
        raise DiscoveryInventoryError(
            "base execution manifest is invalid: " + "; ".join(errors)
        )
    expected_by_id = {unit["unit_id"]: unit for unit in expected_units}
    parents = []
    valid_shards = {}
    for summary in execution["units"]:
        shard = _load_shard(root, summary, "base")
        expected = expected_by_id[summary["unit_id"]]
        shard_errors = validate_result_manifest(shard, specification)
        if any(shard.get(field) != value for field, value in expected.items()):
            shard_errors.append("base shard does not match frozen unit")
        if any(
            shard.get(field) != summary.get(field) for field in SUMMARY_SHARD_FIELDS
        ):
            shard_errors.append("base shard does not match execution summary")
        if shard_errors:
            raise DiscoveryInventoryError(
                "base shard is invalid: " + "; ".join(shard_errors)
            )
        if summary["valid"]:
            valid_shards[summary["unit_id"]] = shard
            continue
        if summary.get("invalid_reasons") != ["timed_out_repositories"]:
            raise DiscoveryInventoryError("base execution has an unrecoverable unit")
        parents.append(shard)
    return parents, valid_shards


def _validated_partition(
    specification: Mapping[str, Any],
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    root: Path,
    source_parents: Sequence[Mapping[str, Any]],
    *,
    refinement_source_plan: Mapping[str, Any] | None,
    refinement_source_execution: Mapping[str, Any] | None,
    refinement_ancestor_plan: Mapping[str, Any] | None,
    refinement_ancestor_execution: Mapping[str, Any] | None,
) -> dict[str, tuple[Mapping[str, Any], list[Mapping[str, Any]]]]:
    try:
        strategy = plan.get("strategy")
        allow_terminal_failures = False
        if (
            strategy
            == "failed_daily_ai_ban_windows_refined_by_filename_policy_and_term"
        ):
            allow_terminal_failures = True
            if (
                refinement_source_plan is None
                or refinement_source_execution is None
                or refinement_ancestor_plan is None
                or refinement_ancestor_execution is None
            ):
                raise ValueError("branch-refined plan source artifacts are required")
            expected_plan = build_query_branch_refined_partition_plan(
                specification,
                refinement_source_plan,
                refinement_source_execution,
                root,
                source_parents=source_parents,
                ancestor_plan=refinement_ancestor_plan,
                ancestor_execution=refinement_ancestor_execution,
            )
        elif strategy == "failed_14_day_windows_refined_to_1_day":
            if refinement_source_plan is None or refinement_source_execution is None:
                raise ValueError("refined plan source artifacts are required")
            expected_plan = build_refined_partition_plan(
                specification,
                refinement_source_plan,
                refinement_source_execution,
                root,
                source_parents=source_parents,
            )
        else:
            expected_plan = build_partition_plan(specification, source_parents)
    except (KeyError, TypeError, ValueError) as error:
        raise DiscoveryInventoryError(f"partition plan is invalid: {error}") from error
    if plan != expected_plan:
        raise DiscoveryInventoryError("partition plan does not match base execution")
    errors = validate_partition_execution(
        execution,
        plan,
        specification,
        root,
        source_parents=source_parents,
        expected_plan=expected_plan,
        allow_timed_out_children=allow_terminal_failures,
        allow_execution_error_children=allow_terminal_failures,
    )
    if errors:
        raise DiscoveryInventoryError(
            "partition execution is invalid: " + "; ".join(errors)
        )
    validated = {}
    for parent, planned_parent in zip(
        execution["parents"], expected_plan["parents"], strict=True
    ):
        shards = _validated_partition_shards(
            parent,
            planned_parent,
            specification,
            root,
            allow_terminal_failures=allow_terminal_failures,
        )
        validated[parent["parent_unit_id"]] = (parent, shards)
    return validated


def _validated_partition_shards(
    parent: Mapping[str, Any],
    planned_parent: Mapping[str, Any],
    specification: Mapping[str, Any],
    root: Path,
    *,
    allow_terminal_failures: bool,
) -> list[Mapping[str, Any]]:
    shards = []
    summary_fields = (
        "unit_id",
        "result_manifest_sha256",
        "sourcegraph_result_ids",
        "valid",
        "invalid_reasons",
    )
    for summary, planned_child in zip(
        parent["children"], planned_parent["children"], strict=True
    ):
        shard = _load_shard(root, summary, "partition child")
        child_errors = validate_result_manifest(shard, specification)
        if any(
            shard.get(field) != value for field, value in planned_child["unit"].items()
        ):
            child_errors.append("partition child does not match frozen unit")
        if any(shard.get(field) != summary.get(field) for field in summary_fields):
            child_errors.append("partition child does not match execution summary")
        terminal_failure = (
            allow_terminal_failures
            and shard.get("valid") is False
            and shard.get("invalid_reasons")
            in (["timed_out_repositories"], ["execution_error"])
        )
        if shard.get("valid") is not True and not terminal_failure:
            child_errors.append("partition child is invalid")
        if child_errors:
            raise DiscoveryInventoryError(
                "partition child is invalid: " + "; ".join(child_errors)
            )
        shards.append(shard)
    return shards


def _base_entry(
    summary: Mapping[str, Any], shard: Mapping[str, Any]
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    reference = _reference("base", summary, shard)
    entry = {
        "logical_unit_id": summary["unit_id"],
        "canonical_repository_id": summary["canonical_repository_id"],
        "query_family_id": summary["query_family_id"],
        "resolution": "base",
        "source_result_manifest_sha256": shard["result_manifest_sha256"],
        "effective_result_manifests": [reference],
        "terminal_timeout_manifests": [],
        "terminal_execution_error_manifests": [],
    }
    return entry, [shard]


def _partition_entry(
    summary: Mapping[str, Any],
    parent: Mapping[str, Any],
    shards: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    references = []
    terminal_timeouts = []
    terminal_execution_errors = []
    effective_shards = []
    for child, shard in zip(parent["children"], shards, strict=True):
        reference = _reference("partition", child, shard)
        if shard["valid"]:
            references.append(reference)
            effective_shards.append(shard)
        elif shard["invalid_reasons"] == ["timed_out_repositories"]:
            terminal_timeouts.append(reference)
        else:
            terminal_execution_errors.append(reference)
    entry = {
        "logical_unit_id": summary["unit_id"],
        "canonical_repository_id": summary["canonical_repository_id"],
        "query_family_id": summary["query_family_id"],
        "resolution": terminal_failure_resolution(
            len(terminal_timeouts), len(terminal_execution_errors)
        ),
        "source_result_manifest_sha256": summary["result_manifest_sha256"],
        "effective_result_manifests": references,
        "terminal_timeout_manifests": terminal_timeouts,
        "terminal_execution_error_manifests": terminal_execution_errors,
    }
    return entry, effective_shards


def _effective_units(
    base_execution: Mapping[str, Any],
    valid_base_shards: Mapping[str, Mapping[str, Any]],
    partition_parents: Mapping[str, tuple[Mapping[str, Any], list[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
    units = []
    manifests = []
    used_partition_parents = set()
    for summary in base_execution["units"]:
        if summary["valid"]:
            entry, shards = _base_entry(summary, valid_base_shards[summary["unit_id"]])
        else:
            recovery = partition_parents.get(summary["unit_id"])
            if recovery is None:
                raise DiscoveryInventoryError("base failure has no partition recovery")
            used_partition_parents.add(summary["unit_id"])
            parent, partition_shards = recovery
            entry, shards = _partition_entry(summary, parent, partition_shards)
        units.append(entry)
        manifests.extend(shards)
    if used_partition_parents != set(partition_parents):
        raise DiscoveryInventoryError("partition execution contains an orphan parent")
    return units, manifests


def build_effective_inventory(
    specification: Mapping[str, Any],
    base_execution: Mapping[str, Any],
    base_root: Path,
    partition_plan: Mapping[str, Any],
    partition_execution: Mapping[str, Any],
    partition_root: Path,
    expected_units: Sequence[Mapping[str, Any]],
    expected_index_audit_sha256: str,
    *,
    refinement_source_plan: Mapping[str, Any] | None = None,
    refinement_source_execution: Mapping[str, Any] | None = None,
    refinement_ancestor_plan: Mapping[str, Any] | None = None,
    refinement_ancestor_execution: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    """Validate and combine base and recovered result manifests."""
    source_parents, valid_base_shards = _base_artifacts(
        specification,
        base_execution,
        base_root,
        expected_units,
        expected_index_audit_sha256,
    )
    partition_parents = _validated_partition(
        specification,
        partition_plan,
        partition_execution,
        partition_root,
        source_parents,
        refinement_source_plan=refinement_source_plan,
        refinement_source_execution=refinement_source_execution,
        refinement_ancestor_plan=refinement_ancestor_plan,
        refinement_ancestor_execution=refinement_ancestor_execution,
    )
    units, manifests = _effective_units(
        base_execution, valid_base_shards, partition_parents
    )
    partition_children = sum(
        len(parent["children"]) for parent in partition_execution["parents"]
    )
    terminal_timeout_count = sum(
        len(unit["terminal_timeout_manifests"]) for unit in units
    )
    terminal_execution_error_count = sum(
        len(unit["terminal_execution_error_manifests"]) for unit in units
    )
    document = {
        "effective_inventory_version": INVENTORY_VERSION,
        "status": terminal_failure_status(
            terminal_timeout_count, terminal_execution_error_count
        ),
        "specification_sha256": specification["specification_sha256"],
        "base_execution_manifest_sha256": execution_manifest_sha256(base_execution),
        "partition_plan_sha256": partition_plan["partition_plan_sha256"],
        "partition_execution_sha256": partition_execution["partition_execution_sha256"],
        "logical_unit_count": len(units),
        "base_result_manifest_count": len(valid_base_shards),
        "recovered_parent_count": len(partition_parents),
        "partition_child_result_manifest_count": partition_children,
        "terminal_timeout_result_manifest_count": terminal_timeout_count,
        "terminal_execution_error_result_manifest_count": (
            terminal_execution_error_count
        ),
        "effective_result_manifest_count": len(manifests),
        "outcomes_consulted": False,
        "units": units,
    }
    return (
        {
            **document,
            "effective_inventory_sha256": effective_inventory_sha256(document),
        },
        manifests,
    )


def load_effective_result_manifests(
    inventory: Mapping[str, Any],
    *,
    specification: Mapping[str, Any],
    base_root: Path,
    partition_root: Path,
) -> list[Mapping[str, Any]]:
    """Load every valid result shard bound by a complete effective inventory."""
    if inventory.get("effective_inventory_sha256") != effective_inventory_sha256(
        inventory
    ):
        raise DiscoveryInventoryError("effective inventory SHA-256 does not match")
    if (
        inventory.get("effective_inventory_version") != INVENTORY_VERSION
        or inventory.get("status")
        not in {
            "complete",
            "complete_with_terminal_timeouts",
            "complete_with_terminal_execution_errors",
            "complete_with_terminal_failures",
        }
        or inventory.get("outcomes_consulted") is not False
    ):
        raise DiscoveryInventoryError("effective inventory is not admissible")
    if inventory.get("specification_sha256") != specification.get(
        "specification_sha256"
    ):
        raise DiscoveryInventoryError(
            "effective inventory does not match discovery specification"
        )
    units = inventory.get("units")
    if not isinstance(units, list) or inventory.get("logical_unit_count") != len(units):
        raise DiscoveryInventoryError("effective inventory unit count does not match")
    manifests = []
    terminal_timeout_count = 0
    terminal_execution_error_count = 0
    logical_ids = set()
    for unit in units:
        (
            unit_manifests,
            logical_id,
            unit_terminal_timeouts,
            unit_terminal_execution_errors,
        ) = _load_inventory_unit(
            unit,
            specification=specification,
            base_root=base_root,
            partition_root=partition_root,
        )
        if logical_id in logical_ids:
            raise DiscoveryInventoryError("effective inventory logical units duplicate")
        logical_ids.add(logical_id)
        manifests.extend(unit_manifests)
        terminal_timeout_count += unit_terminal_timeouts
        terminal_execution_error_count += unit_terminal_execution_errors
    if inventory.get("effective_result_manifest_count") != len(manifests):
        raise DiscoveryInventoryError(
            "effective inventory result manifest count does not match"
        )
    if (
        inventory.get("terminal_timeout_result_manifest_count")
        != terminal_timeout_count
    ):
        raise DiscoveryInventoryError(
            "effective inventory terminal timeout count does not match"
        )
    if (
        inventory.get("terminal_execution_error_result_manifest_count")
        != terminal_execution_error_count
    ):
        raise DiscoveryInventoryError(
            "effective inventory terminal execution-error count does not match"
        )
    return manifests


def _load_inventory_unit(
    unit: Any,
    *,
    specification: Mapping[str, Any],
    base_root: Path,
    partition_root: Path,
) -> tuple[list[Mapping[str, Any]], Any, int, int]:
    if not isinstance(unit, Mapping):
        raise DiscoveryInventoryError("effective inventory unit must be an object")
    references = unit.get("effective_result_manifests")
    if not isinstance(references, list):
        raise DiscoveryInventoryError("effective inventory references are invalid")
    terminal_references = unit.get("terminal_timeout_manifests")
    if not isinstance(terminal_references, list):
        raise DiscoveryInventoryError(
            "effective inventory terminal references are invalid"
        )
    terminal_execution_error_references = unit.get("terminal_execution_error_manifests")
    if not isinstance(terminal_execution_error_references, list):
        raise DiscoveryInventoryError(
            "effective inventory terminal execution-error references are invalid"
        )
    if not references and not (
        terminal_references or terminal_execution_error_references
    ):
        raise DiscoveryInventoryError("effective inventory references are missing")
    expected_location = "base" if unit.get("resolution") == "base" else "partition"
    manifests = [
        _load_inventory_reference(
            unit,
            reference,
            expected_location=expected_location,
            specification=specification,
            base_root=base_root,
            partition_root=partition_root,
            allowed_invalid_reason=None,
        )
        for reference in references
    ]
    for reference in terminal_references:
        _load_inventory_reference(
            unit,
            reference,
            expected_location="partition",
            specification=specification,
            base_root=base_root,
            partition_root=partition_root,
            allowed_invalid_reason="timed_out_repositories",
        )
    for reference in terminal_execution_error_references:
        _load_inventory_reference(
            unit,
            reference,
            expected_location="partition",
            specification=specification,
            base_root=base_root,
            partition_root=partition_root,
            allowed_invalid_reason="execution_error",
        )
    expected_resolution = terminal_failure_resolution(
        len(terminal_references), len(terminal_execution_error_references)
    )
    if unit.get("resolution") == "base" and expected_resolution == "partition":
        expected_resolution = "base"
    if unit.get("resolution") != expected_resolution:
        raise DiscoveryInventoryError(
            "effective inventory resolution does not match references"
        )
    return (
        manifests,
        unit.get("logical_unit_id"),
        len(terminal_references),
        len(terminal_execution_error_references),
    )


def _load_inventory_reference(
    unit: Mapping[str, Any],
    reference: Any,
    *,
    expected_location: str,
    specification: Mapping[str, Any],
    base_root: Path,
    partition_root: Path,
    allowed_invalid_reason: str | None,
) -> Mapping[str, Any]:
    if not isinstance(reference, Mapping):
        raise DiscoveryInventoryError("effective inventory reference is invalid")
    location = reference.get("location")
    if location != expected_location:
        raise DiscoveryInventoryError(
            "effective inventory reference location does not match resolution"
        )
    root = base_root if location == "base" else partition_root
    shard = _load_shard(root, reference, "effective inventory reference")
    shard_errors = validate_result_manifest(shard, specification)
    if shard_errors:
        raise DiscoveryInventoryError(
            "effective inventory reference shard is invalid: " + "; ".join(shard_errors)
        )
    if reference != _reference(location, reference, shard):
        raise DiscoveryInventoryError(
            "effective inventory reference does not match shard"
        )
    admissible_validity = (
        shard.get("valid") is True
        if allowed_invalid_reason is None
        else (
            shard.get("valid") is False
            and shard.get("invalid_reasons") == [allowed_invalid_reason]
        )
    )
    if (
        not admissible_validity
        or shard.get("canonical_repository_id") != unit.get("canonical_repository_id")
        or shard.get("query_family_id") != unit.get("query_family_id")
    ):
        raise DiscoveryInventoryError(
            "effective inventory reference shard is inadmissible"
        )
    if location == "base" and shard.get("unit_id") != unit.get("logical_unit_id"):
        raise DiscoveryInventoryError(
            "effective inventory base reference changes logical unit"
        )
    return shard
