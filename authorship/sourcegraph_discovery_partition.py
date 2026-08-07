"""Deterministic recovery for timed-out Sourcegraph history searches."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery import validate_discovery_specification
from authorship.sourcegraph_discovery_execution import (
    QueryRunner,
    atomic_write_json,
    discovery_unit_id,
    execute_discovery_unit,
    render_query,
    run_sourcegraph_query,
    shard_path_for,
    validate_result_manifest,
)

PARTITION_PLAN_VERSION = 3
PARTITION_EXECUTION_VERSION = 3
PARTITION_WINDOW_DAYS = 14
PARTITION_MAX_WORKERS = 12
PARTITION_STRATEGY = "fixed_14_day_windows_5m_timeout_with_one_day_boundary_overlap"
DATE_FILTER_PATTERN = {
    "after": re.compile(r'after:"([^"]+)"'),
    "before": re.compile(r'before:"([^"]+)"'),
}
TIMEOUT_FILTER_PATTERN = re.compile(r"timeout:\S+")
AI_BAN_FILE_FILTER = "file:(CONTRIBUTING|CODE_OF_CONDUCT|README|POLICY|AGENTS|NO_AI)"
AI_BAN_FILE_BRANCHES = (
    "CONTRIBUTING",
    "CODE_OF_CONDUCT",
    "README",
    "POLICY",
    "AGENTS",
    "NO_AI",
)
AI_BAN_FILE_QUERIES = tuple(
    f"file:(^|/){branch}(\\.[^/]*)?$" for branch in AI_BAN_FILE_BRANCHES
)
AI_BAN_POLICY_FILTER = "(prohibit|forbid|ban|reject|not.accept|do.not.use|not.allowed)"
AI_BAN_POLICY_BRANCHES = (
    "prohibit",
    "forbid",
    "ban",
    "reject",
    "not.accept",
    "do.not.use",
    "not.allowed",
)
AI_BAN_TERM_FILTER = "(AI|LLM|generative)"
AI_BAN_TERM_BRANCHES = ("AI", "LLM", "generative")
Clock = Callable[[], datetime]
CHILD_SUMMARY_FIELDS = frozenset(
    {
        "unit_id",
        "result_manifest_sha256",
        "sourcegraph_result_ids",
        "valid",
        "invalid_reasons",
        "shard_path",
    }
)
PARENT_SUMMARY_FIELDS = frozenset(
    {
        "parent_unit_id",
        "parent_result_manifest_sha256",
        "canonical_repository_id",
        "query_family_id",
        "valid",
        "invalid_reasons",
        "duplicate_result_ids",
        "duplicate_result_count",
        "deduplicated_result_count",
        "children",
    }
)
EXECUTION_FIELDS = frozenset(
    {
        "partition_execution_version",
        "partition_plan_sha256",
        "specification_sha256",
        "status",
        "parent_count",
        "valid_parent_count",
        "invalid_parent_count",
        "child_unit_count",
        "outcomes_consulted",
        "parents",
        "partition_execution_sha256",
    }
)


def _canonical_sha256(document: Mapping[str, Any], excluded_field: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded_field}
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def partition_plan_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "partition_plan_sha256")


def partition_execution_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "partition_execution_sha256")


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date") from error


def _partition_intervals(
    start: date, end: date, *, window_days: int = PARTITION_WINDOW_DAYS
) -> list[tuple[date, date]]:
    if start >= end:
        raise ValueError("partition history interval must be increasing")
    boundaries = [start]
    boundary = start + timedelta(days=window_days)
    while boundary < end:
        boundaries.append(boundary)
        boundary += timedelta(days=window_days)
    boundaries.append(end)
    intervals = []
    last_index = len(boundaries) - 2
    for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
        child_start = left if index == 0 else left - timedelta(days=1)
        child_end = right if index == last_index else right + timedelta(days=1)
        intervals.append((child_start, child_end))
    return intervals


def _replace_date_filter(query: str, name: str, value: date) -> str:
    pattern = DATE_FILTER_PATTERN[name]
    if len(pattern.findall(query)) != 1:
        raise ValueError(f"parent query must contain exactly one {name} filter")
    return pattern.sub(f'{name}:"{value.isoformat()}"', query)


def _replace_timeout_filter(query: str) -> str:
    if len(TIMEOUT_FILTER_PATTERN.findall(query)) != 1:
        raise ValueError("parent query must contain exactly one timeout filter")
    return TIMEOUT_FILTER_PATTERN.sub("timeout:5m", query)


def _child_unit(parent: Mapping[str, Any], after: date, before: date) -> dict[str, Any]:
    query = _replace_date_filter(parent["rendered_query"], "after", after)
    query = _replace_date_filter(query, "before", before)
    query = _replace_timeout_filter(query)
    return _unit_from_query(parent, query)


def _unit_from_query(parent: Mapping[str, Any], query: str) -> dict[str, Any]:
    rendered_sha256 = hashlib.sha256(query.encode()).hexdigest()
    unit = {
        "query_family_id": parent["query_family_id"],
        "result_type": parent["result_type"],
        "canonical_repository_id": parent["canonical_repository_id"],
        "sourcegraph_name": parent["sourcegraph_name"],
        "cutoff_commit": parent["cutoff_commit"],
        "rendered_query": query,
        "rendered_query_sha256": rendered_sha256,
    }
    identity = {
        key: unit[key]
        for key in (
            "query_family_id",
            "canonical_repository_id",
            "sourcegraph_name",
            "cutoff_commit",
            "rendered_query_sha256",
        )
    }
    unit_id = _canonical_sha256(identity, "")
    return {**unit, "unit_id": unit_id}


def _parent_plan(
    specification: Mapping[str, Any], parent: Mapping[str, Any]
) -> dict[str, Any]:
    if parent.get("result_type") not in {"commit", "diff"}:
        raise ValueError("partition parent must be a commit or diff search")
    errors = validate_result_manifest(parent, specification)
    if errors:
        raise ValueError("invalid parent result manifest: " + "; ".join(errors))
    family = next(
        (
            family
            for family in specification["query_families"]
            if family["id"] == parent["query_family_id"]
        ),
        None,
    )
    expected_query = (
        render_query(
            family,
            parent["sourcegraph_name"],
            parent["cutoff_commit"],
        )
        if family is not None
        else None
    )
    expected_unit = {
        "query_family_id": parent["query_family_id"],
        "canonical_repository_id": parent["canonical_repository_id"],
        "sourcegraph_name": parent["sourcegraph_name"],
        "cutoff_commit": parent["cutoff_commit"],
        "rendered_query_sha256": (
            hashlib.sha256(expected_query.encode()).hexdigest()
            if expected_query is not None
            else None
        ),
    }
    if parent.get("rendered_query") != expected_query or parent.get(
        "unit_id"
    ) != discovery_unit_id(expected_unit):
        raise ValueError("partition parent does not match its frozen query unit")
    if (
        parent.get("valid") is not False
        or parent.get("timed_out_repositories") != [parent.get("sourcegraph_name")]
        or parent.get("invalid_reasons") != ["timed_out_repositories"]
    ):
        raise ValueError(
            "partition parent must be a timed-out result manifest with evidence "
            "for its parent repository"
        )
    execution = specification["execution"]
    start = _date(execution.get("history_start", "")[:10], "history_start")
    end = _date(execution.get("before_exclusive", "")[:10], "before_exclusive")
    children = [
        {
            "after_exclusive": after.isoformat(),
            "before_exclusive": before.isoformat(),
            "unit": _child_unit(parent, after, before),
        }
        for after, before in _partition_intervals(start, end)
    ]
    return {
        "parent_unit_id": parent["unit_id"],
        "parent_result_manifest_sha256": parent["result_manifest_sha256"],
        "canonical_repository_id": parent["canonical_repository_id"],
        "query_family_id": parent["query_family_id"],
        "coverage_start_exclusive": start.isoformat(),
        "coverage_end_exclusive": end.isoformat(),
        "boundary_overlap_days": 1,
        "deduplication_key": "sourcegraph_result_id",
        "children": children,
    }


def build_partition_plan(
    specification: Mapping[str, Any],
    timed_out_parents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    errors = validate_discovery_specification(specification)
    if errors:
        raise ValueError("invalid discovery specification: " + "; ".join(errors))
    if not timed_out_parents:
        raise ValueError("at least one timed-out parent is required")
    parents = sorted(
        (_parent_plan(specification, parent) for parent in timed_out_parents),
        key=lambda parent: (
            parent["canonical_repository_id"],
            parent["query_family_id"],
        ),
    )
    parent_ids = [parent["parent_unit_id"] for parent in parents]
    if len(parent_ids) != len(set(parent_ids)):
        raise ValueError("partition parent unit IDs must be unique")
    document = {
        "partition_plan_version": PARTITION_PLAN_VERSION,
        "status": "frozen_before_partition_execution",
        "specification_sha256": specification["specification_sha256"],
        "strategy": PARTITION_STRATEGY,
        "parent_count": len(parents),
        "child_unit_count": sum(len(parent["children"]) for parent in parents),
        "outcomes_consulted": False,
        "parents": parents,
    }
    return {**document, "partition_plan_sha256": partition_plan_sha256(document)}


def build_refined_partition_plan(
    specification: Mapping[str, Any],
    source_plan: Mapping[str, Any],
    source_execution: Mapping[str, Any],
    output_directory: Path,
    *,
    source_parents: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replace only timed-out 14-day children with daily recovery windows."""
    expected_source_plan = build_partition_plan(specification, source_parents)
    if source_plan != expected_source_plan:
        raise ValueError("refinement source plan does not match source parents")
    errors = validate_partition_execution(
        source_execution,
        source_plan,
        specification,
        output_directory,
        source_parents=source_parents,
        allow_timed_out_children=True,
    )
    if errors:
        raise ValueError("refinement source execution is invalid: " + "; ".join(errors))
    parents = [
        _refined_parent(planned, executed, specification, output_directory)
        for planned, executed in zip(
            source_plan["parents"], source_execution["parents"], strict=True
        )
    ]
    if not any(
        len(refined["children"]) > len(original["children"])
        for refined, original in zip(parents, source_plan["parents"], strict=True)
    ):
        raise ValueError("refinement source execution has no timed-out children")
    document = {
        "partition_plan_version": PARTITION_PLAN_VERSION,
        "status": "frozen_before_partition_execution",
        "specification_sha256": specification["specification_sha256"],
        "strategy": "failed_14_day_windows_refined_to_1_day",
        "refinement_source_plan_sha256": source_plan["partition_plan_sha256"],
        "refinement_source_execution_sha256": source_execution[
            "partition_execution_sha256"
        ],
        "parent_count": len(parents),
        "child_unit_count": sum(len(parent["children"]) for parent in parents),
        "outcomes_consulted": False,
        "parents": parents,
    }
    return {**document, "partition_plan_sha256": partition_plan_sha256(document)}


def _refined_parent(
    planned_parent: Mapping[str, Any],
    executed_parent: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    children = []
    for planned_child, summary in zip(
        planned_parent["children"], executed_parent["children"], strict=True
    ):
        if summary["valid"]:
            children.append(planned_child)
            continue
        shard, load_error = _load_partition_child(
            output_directory / _child_path(planned_child["unit"]),
            output_directory,
        )
        if shard is None:
            raise ValueError(f"timed-out child shard is unreadable: {load_error}")
        if (
            validate_result_manifest(shard, specification)
            or any(
                shard.get(field) != value
                for field, value in planned_child["unit"].items()
            )
            or shard.get("invalid_reasons") != ["timed_out_repositories"]
        ):
            raise ValueError("refinement child is not a validated timeout")
        start = _date(planned_child["after_exclusive"], "after_exclusive")
        end = _date(planned_child["before_exclusive"], "before_exclusive")
        children.extend(
            {
                "after_exclusive": after.isoformat(),
                "before_exclusive": before.isoformat(),
                "unit": _child_unit(shard, after, before),
            }
            for after, before in _partition_intervals(start, end, window_days=1)
        )
    return {**planned_parent, "children": children}


def build_query_branch_refined_partition_plan(
    specification: Mapping[str, Any],
    source_plan: Mapping[str, Any],
    source_execution: Mapping[str, Any],
    output_directory: Path,
    *,
    source_parents: Sequence[Mapping[str, Any]],
    ancestor_plan: Mapping[str, Any],
    ancestor_execution: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace failed daily AI-ban leaves with filename-specific branches."""
    expected_source_plan = build_refined_partition_plan(
        specification,
        ancestor_plan,
        ancestor_execution,
        output_directory,
        source_parents=source_parents,
    )
    if source_plan != expected_source_plan:
        raise ValueError("branch refinement source plan does not match ancestry")
    errors = validate_partition_execution(
        source_execution,
        source_plan,
        specification,
        output_directory,
        source_parents=source_parents,
        allow_timed_out_children=True,
        expected_plan=expected_source_plan,
    )
    if errors:
        raise ValueError(
            "branch refinement source execution is invalid: " + "; ".join(errors)
        )
    parents = [
        _query_branch_refined_parent(planned, executed, specification, output_directory)
        for planned, executed in zip(
            source_plan["parents"], source_execution["parents"], strict=True
        )
    ]
    if not any(
        len(refined["children"]) > len(original["children"])
        for refined, original in zip(parents, source_plan["parents"], strict=True)
    ):
        raise ValueError("branch refinement has no timed-out children")
    document = {
        "partition_plan_version": PARTITION_PLAN_VERSION,
        "status": "frozen_before_partition_execution",
        "specification_sha256": specification["specification_sha256"],
        "strategy": ("failed_daily_ai_ban_windows_refined_by_filename_policy_and_term"),
        "refinement_source_plan_sha256": source_plan["partition_plan_sha256"],
        "refinement_source_execution_sha256": source_execution[
            "partition_execution_sha256"
        ],
        "parent_count": len(parents),
        "child_unit_count": sum(len(parent["children"]) for parent in parents),
        "outcomes_consulted": False,
        "parents": parents,
    }
    return {**document, "partition_plan_sha256": partition_plan_sha256(document)}


def _query_branch_refined_parent(
    planned_parent: Mapping[str, Any],
    executed_parent: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    children = []
    for planned_child, summary in zip(
        planned_parent["children"], executed_parent["children"], strict=True
    ):
        if summary["valid"]:
            children.append(planned_child)
            continue
        shard, load_error = _load_partition_child(
            output_directory / _child_path(planned_child["unit"]),
            output_directory,
        )
        if shard is None:
            raise ValueError(f"branch refinement shard is unreadable: {load_error}")
        query = shard.get("rendered_query")
        if (
            validate_result_manifest(shard, specification)
            or shard.get("query_family_id") != "ai_ban_policy_introduction_diffs"
            or shard.get("invalid_reasons") != ["timed_out_repositories"]
            or not isinstance(query, str)
            or query.count(AI_BAN_FILE_FILTER) != 1
        ):
            raise ValueError("branch refinement child is not an eligible timeout")
        if query.count(AI_BAN_POLICY_FILTER) != 2:
            raise ValueError("branch refinement policy filter is not frozen")
        if query.count(AI_BAN_TERM_FILTER) != 2:
            raise ValueError("branch refinement AI-term filter is not frozen")
        children.extend(
            {
                "after_exclusive": planned_child["after_exclusive"],
                "before_exclusive": planned_child["before_exclusive"],
                "unit": _unit_from_query(
                    shard,
                    query.replace(AI_BAN_FILE_FILTER, file_query)
                    .replace(AI_BAN_POLICY_FILTER, policy)
                    .replace(AI_BAN_TERM_FILTER, term),
                ),
            }
            for file_query in AI_BAN_FILE_QUERIES
            for policy in AI_BAN_POLICY_BRANCHES
            for term in AI_BAN_TERM_BRANCHES
        )
    return {**planned_parent, "children": children}


def _child_path(unit: Mapping[str, Any]) -> Path:
    relative = shard_path_for(unit).relative_to("shards")
    return Path("partition-shards") / relative


def _child_summary(shard: Mapping[str, Any], relative_path: Path) -> dict[str, Any]:
    return {
        "unit_id": shard["unit_id"],
        "result_manifest_sha256": shard["result_manifest_sha256"],
        "sourcegraph_result_ids": shard["sourcegraph_result_ids"],
        "valid": shard["valid"],
        "invalid_reasons": shard["invalid_reasons"],
        "shard_path": relative_path.as_posix(),
    }


def _execute_child(
    child: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    query_runner: QueryRunner,
    clock: Clock,
    retry_execution_errors: bool,
) -> dict[str, Any]:
    unit = child["unit"]
    relative_path = _child_path(unit)
    shard, _reused = execute_discovery_unit(
        unit,
        output_directory / relative_path,
        specification,
        query_runner,
        clock,
        retry_execution_errors_only=retry_execution_errors,
        reuse_terminal_failures=not retry_execution_errors,
    )
    return _child_summary(shard, relative_path)


def _execute_parent(
    parent: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    query_runner: QueryRunner,
    clock: Clock,
    max_workers: int,
    retry_execution_errors: bool,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _execute_child,
                child,
                specification,
                output_directory,
                query_runner,
                clock,
                retry_execution_errors,
            )
            for child in parent["children"]
        ]
        children = [future.result() for future in futures]
    seen_result_ids: set[str] = set()
    duplicate_result_ids: set[str] = set()
    for child in children:
        result_ids = set(child["sourcegraph_result_ids"])
        duplicate_result_ids.update(seen_result_ids & result_ids)
        seen_result_ids.update(result_ids)
    invalid_reasons = []
    if any(not child["valid"] for child in children):
        invalid_reasons.append("invalid_child_unit")
    return {
        "parent_unit_id": parent["parent_unit_id"],
        "parent_result_manifest_sha256": parent["parent_result_manifest_sha256"],
        "canonical_repository_id": parent["canonical_repository_id"],
        "query_family_id": parent["query_family_id"],
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "duplicate_result_ids": sorted(duplicate_result_ids),
        "duplicate_result_count": len(duplicate_result_ids),
        "deduplicated_result_count": len(seen_result_ids),
        "children": children,
    }


def _load_partition_child(
    path: Path, output_directory: Path
) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        path.resolve().relative_to(output_directory.resolve())
        document = json.loads(path.read_text())
    except ValueError:
        return None, "child shard resolves outside output directory"
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, str(error)
    if not isinstance(document, Mapping):
        return None, "child shard must be an object"
    return document, None


def _partition_child_errors(
    summary: Any,
    planned_child: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    allow_timed_out: bool,
    allow_execution_error: bool,
) -> tuple[list[str], set[str], bool]:
    if not isinstance(summary, Mapping):
        return ["partition child summary must be an object"], set(), False
    unit = planned_child["unit"]
    expected_path = _child_path(unit)
    errors = []
    if set(summary) != CHILD_SUMMARY_FIELDS:
        errors.append("partition child summary has missing or unexpected fields")
    if summary.get("shard_path") != expected_path.as_posix():
        errors.append("partition child shard path does not match frozen unit")
    shard, load_error = _load_partition_child(
        output_directory / expected_path, output_directory
    )
    if shard is None:
        errors.append(f"partition child shard is unreadable: {load_error}")
        return errors, set(), False
    for field in (
        "unit_id",
        "result_manifest_sha256",
        "sourcegraph_result_ids",
        "valid",
        "invalid_reasons",
    ):
        if summary.get(field) != shard.get(field):
            errors.append("partition child summary does not match shard")
            break
    if any(shard.get(field) != value for field, value in unit.items()):
        errors.append("partition child shard does not match frozen unit")
    result_errors = validate_result_manifest(shard, specification)
    errors.extend(f"partition child shard: {error}" for error in result_errors)
    valid = shard.get("valid") is True
    tolerated_timeout = (
        allow_timed_out
        and shard.get("valid") is False
        and shard.get("invalid_reasons") == ["timed_out_repositories"]
    )
    tolerated_execution_error = (
        allow_execution_error
        and shard.get("valid") is False
        and shard.get("invalid_reasons") == ["execution_error"]
    )
    if not valid and not tolerated_timeout and not tolerated_execution_error:
        errors.append("partition child shard is invalid")
    result_ids = shard.get("sourcegraph_result_ids")
    valid_result_ids = isinstance(result_ids, list) and all(
        isinstance(result_id, str) for result_id in result_ids
    )
    if not valid_result_ids:
        errors.append("partition child result IDs must be strings")
    return errors, set(result_ids) if valid_result_ids else set(), valid


def _partition_parent_errors(
    parent: Any,
    planned_parent: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    allow_timed_out_children: bool,
    allow_execution_error_children: bool,
) -> tuple[list[str], int, bool]:
    if not isinstance(parent, Mapping):
        return ["partition parent summary must be an object"], 0, False
    errors = []
    if set(parent) != PARENT_SUMMARY_FIELDS:
        errors.append("partition parent summary has missing or unexpected fields")
    for field in (
        "parent_unit_id",
        "parent_result_manifest_sha256",
        "canonical_repository_id",
        "query_family_id",
    ):
        if parent.get(field) != planned_parent.get(field):
            errors.append("partition parent summary does not match frozen plan")
            break
    children = parent.get("children")
    planned_children = planned_parent["children"]
    if not isinstance(children, list) or len(children) != len(planned_children):
        return [*errors, "partition parent child count does not match"], 0, False
    seen: set[str] = set()
    duplicates: set[str] = set()
    children_valid = True
    for summary, planned_child in zip(children, planned_children):
        child_errors, result_ids, child_valid = _partition_child_errors(
            summary,
            planned_child,
            specification,
            output_directory,
            allow_timed_out=allow_timed_out_children,
            allow_execution_error=allow_execution_error_children,
        )
        errors.extend(child_errors)
        duplicates.update(seen & result_ids)
        seen.update(result_ids)
        children_valid = children_valid and child_valid
    expected_invalid_reasons = [] if children_valid else ["invalid_child_unit"]
    if (
        parent.get("valid") is not children_valid
        or parent.get("invalid_reasons") != expected_invalid_reasons
        or parent.get("duplicate_result_ids") != sorted(duplicates)
        or parent.get("duplicate_result_count") != len(duplicates)
        or parent.get("deduplicated_result_count") != len(seen)
    ):
        errors.append("partition parent aggregate does not match child shards")
    return errors, len(children), children_valid


def validate_partition_execution(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    source_parents: Sequence[Mapping[str, Any]],
    allow_timed_out_children: bool = False,
    allow_execution_error_children: bool = False,
    expected_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate a persisted recovery execution against its frozen sources."""
    errors = []
    if document.get("partition_execution_version") != PARTITION_EXECUTION_VERSION:
        errors.append(
            f"partition_execution_version must equal {PARTITION_EXECUTION_VERSION}"
        )
    if set(document) != EXECUTION_FIELDS:
        errors.append("partition execution has missing or unexpected fields")
    if expected_plan is None:
        try:
            validated_plan = build_partition_plan(specification, source_parents)
        except (KeyError, TypeError, ValueError) as error:
            return [f"partition source parent validation failed: {error}"]
    else:
        validated_plan = expected_plan
        if validated_plan.get("partition_plan_sha256") != partition_plan_sha256(
            validated_plan
        ):
            errors.append("expected partition plan SHA-256 does not match")
        if validated_plan.get("specification_sha256") != specification.get(
            "specification_sha256"
        ):
            errors.append("expected partition plan does not match specification")
    if plan != validated_plan:
        errors.append("partition plan does not match source parent manifests")
    if document.get("partition_execution_sha256") != partition_execution_sha256(
        document
    ):
        errors.append("partition execution SHA-256 does not match")
    for field in ("partition_plan_sha256", "specification_sha256"):
        if document.get(field) != validated_plan.get(field):
            errors.append(f"partition execution {field} does not match")
    parents = document.get("parents")
    planned_parents = validated_plan["parents"]
    if not isinstance(parents, list) or len(parents) != len(planned_parents):
        return [*errors, "partition execution parent count does not match"]
    child_count = 0
    valid_parent_count = 0
    for parent, planned_parent in zip(parents, planned_parents):
        parent_errors, children, parent_valid = _partition_parent_errors(
            parent,
            planned_parent,
            specification,
            output_directory,
            allow_timed_out_children=allow_timed_out_children,
            allow_execution_error_children=allow_execution_error_children,
        )
        errors.extend(parent_errors)
        child_count += children
        valid_parent_count += parent_valid
    expected_counts = {
        "parent_count": len(parents),
        "valid_parent_count": valid_parent_count,
        "invalid_parent_count": len(parents) - valid_parent_count,
        "child_unit_count": child_count,
    }
    if any(document.get(field) != value for field, value in expected_counts.items()):
        errors.append("partition execution counts do not match")
    expected_status = "complete" if valid_parent_count == len(parents) else "incomplete"
    if document.get("status") != expected_status:
        errors.append("partition execution status does not match parent validity")
    if document.get("outcomes_consulted") is not False:
        errors.append("partition execution must be outcome blind")
    return errors


def execute_partition_plan(
    plan: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    source_parents: Sequence[Mapping[str, Any]],
    query_runner: QueryRunner = run_sourcegraph_query,
    clock: Clock = lambda: datetime.now(timezone.utc),
    max_workers: int = PARTITION_MAX_WORKERS,
) -> dict[str, Any]:
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
    ):
        raise ValueError("max_workers must be a positive integer")
    if plan.get("partition_plan_sha256") != partition_plan_sha256(plan):
        raise ValueError("partition plan SHA-256 does not match")
    if plan.get("specification_sha256") != specification.get("specification_sha256"):
        raise ValueError("partition plan does not match discovery specification")
    validated_plan = build_partition_plan(specification, source_parents)
    if plan != validated_plan:
        raise ValueError(
            "partition plan semantics do not match its validated source parent manifests"
        )
    return _execute_validated_plan(
        validated_plan,
        specification,
        output_directory,
        query_runner,
        clock,
        max_workers,
        retry_execution_errors=True,
    )


def execute_refined_partition_plan(
    plan: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    source_plan: Mapping[str, Any],
    source_execution: Mapping[str, Any],
    source_parents: Sequence[Mapping[str, Any]],
    query_runner: QueryRunner = run_sourcegraph_query,
    clock: Clock = lambda: datetime.now(timezone.utc),
    max_workers: int = PARTITION_MAX_WORKERS,
) -> dict[str, Any]:
    if plan.get("partition_plan_sha256") != partition_plan_sha256(plan):
        raise ValueError("partition plan SHA-256 does not match")
    validated_plan = build_refined_partition_plan(
        specification,
        source_plan,
        source_execution,
        output_directory,
        source_parents=source_parents,
    )
    if plan != validated_plan:
        raise ValueError("refined plan does not match its validated timeout sources")
    return _execute_validated_plan(
        validated_plan,
        specification,
        output_directory,
        query_runner,
        clock,
        max_workers,
        retry_execution_errors=True,
    )


def execute_query_branch_refined_partition_plan(
    plan: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    *,
    source_plan: Mapping[str, Any],
    source_execution: Mapping[str, Any],
    source_parents: Sequence[Mapping[str, Any]],
    ancestor_plan: Mapping[str, Any],
    ancestor_execution: Mapping[str, Any],
    query_runner: QueryRunner = run_sourcegraph_query,
    clock: Clock = lambda: datetime.now(timezone.utc),
    max_workers: int = PARTITION_MAX_WORKERS,
    retry_terminal_execution_errors: bool = False,
) -> dict[str, Any]:
    validated_plan = build_query_branch_refined_partition_plan(
        specification,
        source_plan,
        source_execution,
        output_directory,
        source_parents=source_parents,
        ancestor_plan=ancestor_plan,
        ancestor_execution=ancestor_execution,
    )
    if plan != validated_plan:
        raise ValueError("branch-refined plan does not match timeout sources")
    return _execute_validated_plan(
        validated_plan,
        specification,
        output_directory,
        query_runner,
        clock,
        max_workers,
        retry_execution_errors=retry_terminal_execution_errors,
    )


def _execute_validated_plan(
    validated_plan: Mapping[str, Any],
    specification: Mapping[str, Any],
    output_directory: Path,
    query_runner: QueryRunner,
    clock: Clock,
    max_workers: int,
    *,
    retry_execution_errors: bool,
) -> dict[str, Any]:
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
    ):
        raise ValueError("max_workers must be a positive integer")
    parents = [
        _execute_parent(
            parent,
            specification,
            output_directory,
            query_runner,
            clock,
            max_workers,
            retry_execution_errors,
        )
        for parent in validated_plan["parents"]
    ]
    valid_parent_count = sum(parent["valid"] for parent in parents)
    children = [child for parent in parents for child in parent["children"]]
    document = {
        "partition_execution_version": PARTITION_EXECUTION_VERSION,
        "partition_plan_sha256": validated_plan["partition_plan_sha256"],
        "specification_sha256": specification["specification_sha256"],
        "status": ("complete" if valid_parent_count == len(parents) else "incomplete"),
        "parent_count": len(parents),
        "valid_parent_count": valid_parent_count,
        "invalid_parent_count": len(parents) - valid_parent_count,
        "child_unit_count": len(children),
        "outcomes_consulted": False,
        "parents": parents,
    }
    manifest = {
        **document,
        "partition_execution_sha256": partition_execution_sha256(document),
    }
    atomic_write_json(
        output_directory / "sourcegraph-discovery-partition-execution.v3.json",
        manifest,
    )
    return manifest
