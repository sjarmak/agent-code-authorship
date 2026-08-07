"""Command-line entry point for partitioned discovery timeout recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sg import check_auth
from authorship.sourcegraph_discovery_execution import (
    atomic_write_json,
    build_execution_units,
)
from authorship.sourcegraph_discovery_inventory import build_effective_inventory
from authorship.sourcegraph_discovery_partition import (
    build_partition_plan,
    build_query_branch_refined_partition_plan,
    build_refined_partition_plan,
    execute_partition_plan,
    execute_query_branch_refined_partition_plan,
    execute_refined_partition_plan,
)
from authorship.sourcegraph_discovery_validation import validate_execution_manifest


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def _safe_relative_path(value: Any) -> Path:
    if not isinstance(value, str):
        raise SystemExit("unsafe shard path in execution manifest")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe shard path in execution manifest: {value}")
    return path


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise SystemExit(f"cannot load {path}: {error}") from error


def _repository_exclusion_source(
    specification: Mapping[str, Any], path: Path
) -> bytes | None:
    execution = specification.get("execution")
    exclusions = (
        execution.get("repository_exclusions")
        if isinstance(execution, Mapping)
        else None
    )
    repositories = (
        exclusions.get("repositories") if isinstance(exclusions, Mapping) else None
    )
    if not isinstance(repositories, list) or not repositories:
        return None
    try:
        return path.read_bytes()
    except OSError as error:
        raise SystemExit(f"cannot load {path}: {error}") from error


def _timed_out_parents(
    execution: Mapping[str, Any], result_root: Path
) -> list[Mapping[str, Any]]:
    units = execution.get("units")
    if not isinstance(units, list):
        raise SystemExit("execution manifest units must be a list")
    parents = []
    unsupported = []
    for summary in units:
        if not isinstance(summary, Mapping) or summary.get("valid") is True:
            continue
        reasons = summary.get("invalid_reasons")
        if not isinstance(reasons, list):
            raise SystemExit("invalid execution unit has malformed reasons")
        if reasons != ["timed_out_repositories"]:
            unsupported.append(summary.get("unit_id"))
            continue
        path = result_root / _safe_relative_path(summary.get("shard_path"))
        try:
            path.resolve().relative_to(result_root.resolve())
        except ValueError as error:
            raise SystemExit(
                f"shard path resolves outside result root: {path}"
            ) from error
        parent = _load_json(path)
        for field in ("unit_id", "result_manifest_sha256", "valid", "invalid_reasons"):
            if parent.get(field) != summary.get(field):
                raise SystemExit(f"{path} does not match execution manifest")
        parents.append(parent)
    if unsupported:
        raise SystemExit("execution has non-timeout invalid units")
    if not parents:
        raise SystemExit("execution has no timed-out units to partition")
    return parents


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Partition timed-out frozen Sourcegraph discovery searches."
    )
    parser.add_argument(
        "--specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
    )
    parser.add_argument(
        "--index-audit",
        type=Path,
        default=Path("study/sourcegraph-index-audit.v3.json"),
    )
    parser.add_argument(
        "--repository-exclusion-source",
        type=Path,
        default=Path("study/sg-evals-action-plan.v3.json"),
    )
    parser.add_argument(
        "--execution-manifest",
        type=Path,
        default=Path(
            "results/sourcegraph-discovery-v3/"
            "sourcegraph-discovery-execution.v3.json"
        ),
    )
    parser.add_argument(
        "--result-root",
        type=Path,
        default=Path("results/sourcegraph-discovery-v3"),
    )
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=Path("study/sourcegraph-discovery-partition-plan.v3.json"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("results/sourcegraph-discovery-partitions-v3"),
    )
    parser.add_argument(
        "--inventory-output",
        type=Path,
        default=Path("study/sourcegraph-effective-discovery-inventory.v3.json"),
    )
    parser.add_argument(
        "--retry-terminal-execution-errors",
        action="store_true",
        help="Retry final branch shards that previously recorded execution errors.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    specification = _load_json(arguments.specification)
    execution = _load_json(arguments.execution_manifest)
    if execution.get("specification_sha256") != specification.get(
        "specification_sha256"
    ):
        raise SystemExit("execution manifest does not match discovery specification")
    index_manifest = _load_json(arguments.index_manifest)
    index_audit = _load_json(arguments.index_audit)
    index_manifest_sha256 = _file_sha256(arguments.index_manifest)
    index_audit_sha256 = _file_sha256(arguments.index_audit)
    try:
        expected_units = build_execution_units(
            specification,
            index_manifest,
            index_audit,
            index_manifest_sha256=index_manifest_sha256,
            repository_exclusion_source=_repository_exclusion_source(
                specification, arguments.repository_exclusion_source
            ),
        )
    except ValueError as error:
        raise SystemExit(
            f"cannot reconstruct frozen discovery units: {error}"
        ) from error
    validation_errors = validate_execution_manifest(
        execution,
        specification,
        arguments.result_root,
        expected_units=expected_units,
        expected_index_audit_sha256=index_audit_sha256,
    )
    if validation_errors:
        raise SystemExit(
            "base execution manifest is invalid: " + "; ".join(validation_errors)
        )
    parents = _timed_out_parents(execution, arguments.result_root)
    plan = build_partition_plan(specification, parents)
    atomic_write_json(arguments.plan_output, plan)
    check_auth()
    partition_execution = execute_partition_plan(
        plan,
        specification,
        arguments.output_directory,
        source_parents=parents,
    )
    refinement_source_plan = None
    refinement_source_execution = None
    refinement_ancestor_plan = None
    refinement_ancestor_execution = None
    if partition_execution["status"] == "incomplete":
        refinement_source_plan = plan
        refinement_source_execution = partition_execution
        atomic_write_json(
            arguments.plan_output.with_name(
                "sourcegraph-discovery-partition-source-plan.v3.json"
            ),
            refinement_source_plan,
        )
        atomic_write_json(
            arguments.output_directory
            / "sourcegraph-discovery-partition-source-execution.v3.json",
            refinement_source_execution,
        )
        plan = build_refined_partition_plan(
            specification,
            refinement_source_plan,
            refinement_source_execution,
            arguments.output_directory,
            source_parents=parents,
        )
        atomic_write_json(arguments.plan_output, plan)
        partition_execution = execute_refined_partition_plan(
            plan,
            specification,
            arguments.output_directory,
            source_plan=refinement_source_plan,
            source_execution=refinement_source_execution,
            source_parents=parents,
        )
        if partition_execution["status"] == "incomplete":
            refinement_ancestor_plan = refinement_source_plan
            refinement_ancestor_execution = refinement_source_execution
            refinement_source_plan = plan
            refinement_source_execution = partition_execution
            atomic_write_json(
                arguments.plan_output.with_name(
                    "sourcegraph-discovery-partition-daily-source-plan.v3.json"
                ),
                refinement_source_plan,
            )
            atomic_write_json(
                arguments.output_directory
                / "sourcegraph-discovery-partition-daily-source-execution.v3.json",
                refinement_source_execution,
            )
            plan = build_query_branch_refined_partition_plan(
                specification,
                refinement_source_plan,
                refinement_source_execution,
                arguments.output_directory,
                source_parents=parents,
                ancestor_plan=refinement_ancestor_plan,
                ancestor_execution=refinement_ancestor_execution,
            )
            atomic_write_json(arguments.plan_output, plan)
            partition_execution = execute_query_branch_refined_partition_plan(
                plan,
                specification,
                arguments.output_directory,
                source_plan=refinement_source_plan,
                source_execution=refinement_source_execution,
                source_parents=parents,
                ancestor_plan=refinement_ancestor_plan,
                ancestor_execution=refinement_ancestor_execution,
                retry_terminal_execution_errors=(
                    arguments.retry_terminal_execution_errors
                ),
            )
    terminal_timeout_strategy = (
        plan.get("strategy")
        == "failed_daily_ai_ban_windows_refined_by_filename_policy_and_term"
    )
    inventory = None
    if partition_execution["status"] == "complete" or terminal_timeout_strategy:
        inventory, _result_manifests = build_effective_inventory(
            specification,
            execution,
            arguments.result_root,
            plan,
            partition_execution,
            arguments.output_directory,
            expected_units,
            index_audit_sha256,
            refinement_source_plan=refinement_source_plan,
            refinement_source_execution=refinement_source_execution,
            refinement_ancestor_plan=refinement_ancestor_plan,
            refinement_ancestor_execution=refinement_ancestor_execution,
        )
        atomic_write_json(arguments.inventory_output, inventory)
    print(json.dumps(partition_execution, indent=2, sort_keys=True))
    return 0 if inventory is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
