"""Fail-closed validation for exact Sourcegraph authorship executions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_authorship_exact import exact_hunk_units


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rematerialized_units(
    document: Mapping[str, Any],
    task: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    metadata = document.get("commit_metadata")
    if not isinstance(metadata, Mapping):
        return None
    if metadata.get("commit_oid") != task.get("commit_oid") or metadata.get(
        "committed_at"
    ) != task.get("committed_at"):
        return None
    if not task.get("resolve_commit_metadata"):
        fields = (
            "commit_oid",
            "first_parent_oid",
            "parent_count",
            "is_root_commit",
            "committed_at",
        )
        if dict(metadata) != {field: task.get(field) for field in fields}:
            return None
    try:
        return exact_hunk_units(
            repository_id=task["repository_id"],
            sourcegraph_name=task["sourcegraph_name"],
            commit_oid=task["commit_oid"],
            first_parent_oid=metadata["first_parent_oid"],
            committed_at=metadata["committed_at"],
            language=task["language"],
            records=records,
            authorship_role=task["authorship_role"],
            evidence_tier=task["evidence_tier"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def validate_extraction_shard(
    document: Mapping[str, Any], task: Mapping[str, Any]
) -> list[str]:
    """Re-derive every exact unit from the shard's pinned raw records."""
    from authorship.sourcegraph_authorship_execution import (
        SHARD_VERSION,
        extraction_shard_sha256,
    )

    errors = []
    if document.get("extraction_shard_version") != SHARD_VERSION:
        errors.append("extraction shard version does not match")
    if document.get("task") != task:
        errors.append("extraction shard task does not match")
    if document.get("extraction_shard_sha256") != extraction_shard_sha256(document):
        errors.append("extraction shard SHA-256 does not match")
    records, units = document.get("raw_records"), document.get("units")
    if not isinstance(records, list) or not isinstance(units, list):
        return [*errors, "extraction shard records are invalid"]
    if document.get("counts") != {
        "raw_records": len(records),
        "units": len(units),
    }:
        errors.append("extraction shard counts do not match")
    expected = _rematerialized_units(document, task, records)
    if expected is None:
        errors.append("extraction shard commit metadata is invalid")
    elif units != expected:
        errors.append("extraction shard units do not match raw records")
    if document.get("status") != "complete":
        errors.append("extraction shard is incomplete")
    return errors


def _safe_shard(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path


def _manifest_references(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    fixed, h3 = document.get("fixed_shards"), document.get("h3_repositories")
    if not isinstance(fixed, list) or not isinstance(h3, list):
        return []
    return [
        *fixed,
        *[
            reference
            for repository in h3
            if isinstance(repository, Mapping)
            for reference in repository.get("shards", [])
        ],
    ]


def _reference_matches_shard(
    reference: Mapping[str, Any], shard: Mapping[str, Any]
) -> bool:
    task = shard.get("task")
    if not isinstance(task, Mapping):
        return False
    fields = {
        "task_id": "task_id",
        "repository_id": "repository_id",
        "language": "language",
        "authorship_role": "authorship_role",
        "evidence_tier": "evidence_tier",
    }
    return (
        all(
            reference.get(key) == task.get(task_key) for key, task_key in fields.items()
        )
        and reference.get("extraction_shard_sha256")
        == shard.get("extraction_shard_sha256")
        and reference.get("unit_count") == len(shard.get("units", []))
    )


def _verified_shards(
    references: Sequence[Mapping[str, Any]], root: Path
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    shards, errors = {}, []
    for reference in references:
        path = _safe_shard(root, reference.get("shard"))
        if path is None or not path.is_file():
            errors.append("execution shard path is invalid or missing")
            continue
        if _file_sha256(path) != reference.get("file_sha256"):
            errors.append(
                f"execution shard file hash differs: {reference.get('shard')}"
            )
            continue
        try:
            shard = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            errors.append(f"execution shard is unreadable: {reference.get('shard')}")
            continue
        if not _reference_matches_shard(reference, shard):
            errors.append(
                f"execution shard reference does not match content: {reference.get('shard')}"
            )
            continue
        if shard.get("extraction_shard_sha256") != reference.get(
            "extraction_shard_sha256"
        ) or validate_extraction_shard(shard, shard.get("task", {})):
            errors.append(
                f"execution shard content is invalid: {reference.get('shard')}"
            )
            continue
        shards[str(reference.get("task_id"))] = shard
    return shards, errors


def _frame_errors(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
) -> list[str]:
    from authorship.sourcegraph_authorship_execution import build_fixed_tasks

    errors = []
    fixed = document.get("fixed_shards")
    expected_fixed = sorted(
        task["task_id"] for task in build_fixed_tasks(plan, candidates)
    )
    observed_fixed = (
        [row.get("task_id") for row in fixed] if isinstance(fixed, list) else []
    )
    if observed_fixed != expected_fixed:
        errors.append("authorship execution fixed task frame does not match")
    h3 = document.get("h3_repositories")
    observed_h3 = (
        [row.get("repository_id") for row in h3] if isinstance(h3, list) else []
    )
    expected_h3 = sorted(row["repository_id"] for row in plan.get("h3_pairs", []))
    if observed_h3 != expected_h3:
        errors.append("authorship execution H3 repository frame does not match")
    return errors


def _ordered_h3_tasks(
    repository_id: str,
    candidates: Sequence[Mapping[str, Any]],
    required: Counter[tuple[str, str, int]],
) -> list[dict[str, Any]]:
    from authorship.sourcegraph_authorship_execution import _h3_task

    ordered = sorted(
        (
            row
            for row in candidates
            if row.get("repository_id") == repository_id
            and row.get("evidence_tier") == "H3_contemporary_pre_adoption"
        ),
        key=lambda row: (row["committed_at"], row["commit_oid"]),
        reverse=True,
    )
    languages = sorted({stratum[0] for stratum in required})
    return [
        _h3_task(candidate, language)
        for candidate in ordered
        for language in languages
        if language in candidate["languages"]
    ]


def _h3_summary_errors(
    summary: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    agent_units: Sequence[Mapping[str, Any]],
    shards: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    from authorship.sourcegraph_authorship_execution import (
        _capacity_document,
        _capacity_satisfied,
        _stratum,
    )

    repository = summary.get("repository_id")
    required = Counter(
        _stratum(unit)
        for unit in agent_units
        if unit.get("repository_id") == repository
    )
    observed: Counter[tuple[str, str, int]] = Counter()
    references = summary.get("shards", [])
    tasks = _ordered_h3_tasks(str(repository), candidates, required)
    consumed = 0
    for task in tasks:
        if _capacity_satisfied(required, observed):
            break
        if consumed >= len(references):
            break
        reference = references[consumed]
        if reference.get("task_id") != task["task_id"]:
            return ["authorship execution H3 scan order does not match"]
        shard = shards.get(task["task_id"])
        if shard is None:
            return ["authorship execution H3 scan shard is invalid"]
        observed.update(_stratum(unit) for unit in shard["units"])
        consumed += 1
    satisfied = _capacity_satisfied(required, observed)
    expected = {
        "required_capacity": _capacity_document(required),
        "observed_capacity": _capacity_document(observed),
        "capacity_satisfied": satisfied,
        "exhausted_window": not satisfied,
        "scanned_task_count": consumed,
    }
    if consumed != len(references) or any(
        summary.get(key) != value for key, value in expected.items()
    ):
        return ["authorship execution H3 adaptive scan contract does not match"]
    return []


def _h3_contract_errors(
    document: Mapping[str, Any],
    candidates: Mapping[str, Any],
    shards: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    fixed = document.get("fixed_shards", [])
    agent_units = [
        unit
        for reference in fixed
        for unit in shards.get(str(reference.get("task_id")), {}).get("units", [])
        if unit.get("authorship_role") == "agent"
    ]
    errors = []
    for summary in document.get("h3_repositories", []):
        errors.extend(
            _h3_summary_errors(
                summary, candidates.get("commits", []), agent_units, shards
            )
        )
    return errors


def _expected_counts(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
    shards: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    from authorship.sourcegraph_authorship_execution import build_fixed_tasks

    fixed_refs = document.get("fixed_shards", [])
    h3_refs = [
        reference
        for summary in document.get("h3_repositories", [])
        for reference in summary.get("shards", [])
    ]
    fixed_units = [
        unit
        for reference in fixed_refs
        for unit in shards.get(str(reference.get("task_id")), {}).get("units", [])
    ]
    h3_units = [
        unit
        for reference in h3_refs
        for unit in shards.get(str(reference.get("task_id")), {}).get("units", [])
    ]
    failures = document.get("failures", [])
    return {
        "fixed_tasks": len(build_fixed_tasks(plan, candidates)),
        "h3_scanned_tasks": len(h3_refs),
        "successful_tasks": len(fixed_refs) + len(h3_refs),
        "failed_tasks": len(failures) if isinstance(failures, list) else -1,
        "agent_units": sum(
            unit.get("authorship_role") == "agent" for unit in fixed_units
        ),
        "H2_units": sum(
            unit.get("evidence_tier") == "H2_policy_human" for unit in fixed_units
        ),
        "H3_candidate_units": len(h3_units),
    }


def _parent_errors(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
) -> list[str]:
    from authorship.sourcegraph_authorship_execution import (
        EXECUTION_VERSION,
        authorship_execution_sha256,
    )

    errors = []
    if document.get("authorship_execution_version") != EXECUTION_VERSION:
        errors.append("authorship execution version does not match")
    if document.get("authorship_unit_plan_sha256") != plan.get(
        "authorship_unit_plan_sha256"
    ):
        errors.append("authorship execution plan does not match")
    if document.get("candidate_manifest_sha256") != candidates.get(
        "candidate_manifest_sha256"
    ):
        errors.append("authorship execution candidates do not match")
    if document.get("authorship_execution_sha256") != authorship_execution_sha256(
        document
    ):
        errors.append("authorship execution SHA-256 does not match")
    return errors


def validate_execution_manifest(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
    output_root: Path,
) -> list[str]:
    """Validate parents, exact required frames, every shard, and adaptive scans."""
    errors = _parent_errors(document, plan, candidates)
    errors.extend(_frame_errors(document, plan, candidates))
    references = _manifest_references(document)
    if len(references) != len({row.get("task_id") for row in references}):
        errors.append("authorship execution task references are duplicated")
    shards, shard_errors = _verified_shards(references, output_root)
    errors.extend(shard_errors)
    if not shard_errors:
        errors.extend(_h3_contract_errors(document, candidates, shards))
        if document.get("counts") != _expected_counts(
            document, plan, candidates, shards
        ):
            errors.append("authorship execution counts do not match")
    failures = document.get("failures")
    expected_status = (
        "complete" if isinstance(failures, list) and not failures else "incomplete"
    )
    if document.get("status") != expected_status:
        errors.append("authorship execution status does not match failures")
    return errors


def load_execution_shards(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
    output_root: Path,
) -> list[dict[str, Any]]:
    """Load every semantically re-derived shard from a complete execution."""
    from authorship.sourcegraph_authorship_execution import AuthorshipExecutionError

    errors = validate_execution_manifest(document, plan, candidates, output_root)
    if document.get("status") != "complete":
        errors.append("authorship execution is not complete")
    if errors:
        raise AuthorshipExecutionError(
            "authorship execution is invalid: " + "; ".join(errors)
        )
    shards, shard_errors = _verified_shards(_manifest_references(document), output_root)
    if shard_errors:
        raise AuthorshipExecutionError(
            "authorship execution is invalid: " + "; ".join(shard_errors)
        )
    return [
        shards[str(reference["task_id"])]
        for reference in _manifest_references(document)
    ]
