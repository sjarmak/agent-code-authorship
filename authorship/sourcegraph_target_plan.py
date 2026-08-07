"""Outcome-blind Sourcegraph plan for the fixed prevalence target population."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from authorship.languages import SKIP_PATH, ext_of
from authorship.survival_candidates import canonical_repository_id

PLAN_VERSION = 1
MAX_FILES_PER_REPOSITORY = 50
FILE_SAMPLING_SEED = "sourcegraph-target-units-v1"
LANGUAGE_BY_EXTENSION = {".py": "Python", ".go": "Go"}
TreeFetcher = Callable[[str, str], Mapping[str, Any]]


class TargetPlanError(RuntimeError):
    """Raised when the fixed target population cannot be frozen exactly."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def target_unit_plan_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "target_unit_plan_sha256"
    }
    return _sha256(content)


def _repository_index(
    index_manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    rows = index_manifest.get("repositories")
    if not isinstance(rows, list):
        raise TargetPlanError("Sourcegraph index repositories must be a list")
    return {
        canonical_repository_id(row["canonical_repository_id"]): row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("canonical_repository_id"), str)
    }


def _target_rows(target_manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = target_manifest.get("repositories")
    if not isinstance(rows, list) or not rows:
        raise TargetPlanError("target repositories must be a non-empty list")
    if any(
        not isinstance(row, Mapping)
        or row.get("role") != "target"
        or row.get("label") != "unlabeled"
        for row in rows
    ):
        raise TargetPlanError("target rows must remain unlabeled fixed targets")
    return sorted(rows, key=lambda row: str(row.get("id")))


def _eligible_tasks(
    target: Mapping[str, Any],
    sourcegraph_name: str,
    tree_result: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    languages = set(target.get("languages", []))
    paths = tree_result.get("paths")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
        raise TargetPlanError("Sourcegraph tree paths must be a sequence")
    base = {
        "repository_id": target["id"],
        "sourcegraph_name": sourcegraph_name,
        "cutoff_commit": target["snapshot"]["commit"],
        "cutoff_tree": target["snapshot"]["tree"],
        "effective_date_range": target["effective_date_range"],
    }
    eligible = []
    for path in sorted(set(paths)):
        language = (
            LANGUAGE_BY_EXTENSION.get(ext_of(path)) if isinstance(path, str) else None
        )
        if language not in languages or SKIP_PATH(path):
            continue
        eligible.append((path, language))
    ranked = sorted(
        eligible,
        key=lambda item: (
            hashlib.sha256(
                f"{FILE_SAMPLING_SEED}\0{target['id'].lower()}\0{item[0]}".encode()
            ).hexdigest(),
            item[0],
        ),
    )
    sampled = ranked[:MAX_FILES_PER_REPOSITORY]
    probability = len(sampled) / len(eligible) if eligible else 0.0
    weight = 1.0 / probability if probability else 0.0
    tasks = []
    for path, language in sorted(sampled):
        content = {
            **base,
            "path": path,
            "language": language,
            "file_inclusion_probability": probability,
            "file_sampling_weight": weight,
        }
        tasks.append({**content, "task_sha256": _sha256(content)})
    return tasks, len(eligible)


def _repository_tasks(
    target: Mapping[str, Any],
    indexed: Mapping[str, Any] | None,
    tree_fetcher: TreeFetcher,
) -> tuple[list[dict[str, Any]], dict[str, str] | None, int]:
    repository_id = str(target.get("id"))
    snapshot = target.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise TargetPlanError(f"{repository_id}: snapshot is invalid")
    if indexed is None:
        return [], _pending(target, None, "missing from Sourcegraph index manifest"), 0
    sourcegraph = indexed.get("sourcegraph")
    sourcegraph_name = (
        sourcegraph.get("selected_name") if isinstance(sourcegraph, Mapping) else None
    )
    if not isinstance(sourcegraph_name, str):
        return [], _pending(target, None, "no selected Sourcegraph repository"), 0
    if indexed.get("cutoff_commit") != snapshot.get("commit") or indexed.get(
        "cutoff_tree"
    ) != snapshot.get("tree"):
        return (
            [],
            _pending(target, sourcegraph_name, "index manifest snapshot mismatch"),
            0,
        )
    try:
        result = tree_fetcher(sourcegraph_name, snapshot["commit"])
        if result.get("oid") != snapshot["commit"]:
            raise TargetPlanError("commit mismatch")
        tasks, eligible_count = _eligible_tasks(target, sourcegraph_name, result)
        return tasks, None, eligible_count
    except (RuntimeError, TargetPlanError, KeyError, TypeError) as error:
        return [], _pending(target, sourcegraph_name, str(error)), 0


def _pending(
    target: Mapping[str, Any], sourcegraph_name: str | None, reason: str
) -> dict[str, str]:
    return {
        "repository_id": str(target.get("id")),
        "sourcegraph_name": sourcegraph_name or "",
        "cutoff_commit": str((target.get("snapshot") or {}).get("commit", "")),
        "reason": reason[:300],
    }


def build_target_unit_plan(
    target_manifest: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    feature_manifest: Mapping[str, Any],
    tree_fetcher: TreeFetcher,
) -> dict[str, Any]:
    targets = _target_rows(target_manifest)
    indexed = _repository_index(index_manifest)
    tasks: list[dict[str, Any]] = []
    pending: list[dict[str, str]] = []
    repositories: list[dict[str, Any]] = []
    for target in targets:
        index_row = indexed.get(canonical_repository_id(str(target.get("id"))))
        additions, failure, eligible_count = _repository_tasks(
            target, index_row, tree_fetcher
        )
        tasks.extend(additions)
        if failure is not None:
            pending.append(failure)
        else:
            sourcegraph = index_row.get("sourcegraph") if index_row else {}
            probability = len(additions) / eligible_count if eligible_count else 0.0
            repositories.append(
                {
                    "repository_id": target["id"],
                    "sourcegraph_name": sourcegraph["selected_name"],
                    "cutoff_commit": target["snapshot"]["commit"],
                    "cutoff_tree": target["snapshot"]["tree"],
                    "eligible_file_count": eligible_count,
                    "file_count": len(additions),
                    "file_inclusion_probability": probability,
                    "file_sampling_weight": (1.0 / probability if probability else 0.0),
                }
            )
    document = {
        "target_unit_plan_version": PLAN_VERSION,
        "status": (
            "blocked_missing_indexed_revision"
            if pending
            else "frozen_before_target_outcome_extraction"
        ),
        "target_manifest_sha256": _sha256(target_manifest),
        "sourcegraph_index_manifest_sha256": _sha256(index_manifest),
        "feature_manifest_sha256": _sha256(feature_manifest),
        "repository_count": len(targets),
        "ready_repository_count": len(targets) - len(pending),
        "eligible_file_count": sum(row["eligible_file_count"] for row in repositories),
        "file_count": len(tasks),
        "unit_definition": "contiguous_current_snapshot_blame_hunk",
        "minimum_lines": 1,
        "primary_weight": "line_count_times_file_sampling_weight",
        "secondary_weight": "equal_repository",
        "file_sampling": {
            "design": "deterministic_uniform_sha256_priority_sample",
            "seed": FILE_SAMPLING_SEED,
            "maximum_files_per_repository": MAX_FILES_PER_REPOSITORY,
            "inclusion_probability": "min(1,maximum_files/eligible_files)",
            "analysis_weight": "line_count/file_inclusion_probability",
        },
        "outcomes_consulted": False,
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "pending_repositories": pending,
        "repositories": repositories,
        "tasks": tasks,
    }
    return {**document, "target_unit_plan_sha256": target_unit_plan_sha256(document)}


def validate_target_unit_plan(
    plan: Mapping[str, Any],
    target_manifest: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    feature_manifest: Mapping[str, Any],
    tree_fetcher: TreeFetcher,
) -> list[str]:
    errors = []
    if plan.get("target_unit_plan_sha256") != target_unit_plan_sha256(plan):
        errors.append("target unit plan SHA-256 does not match")
    expected = build_target_unit_plan(
        target_manifest, index_manifest, feature_manifest, tree_fetcher
    )
    if _canonical_json(plan) != _canonical_json(expected):
        errors.append("plan differs from independent Sourcegraph enumeration")
    return errors
