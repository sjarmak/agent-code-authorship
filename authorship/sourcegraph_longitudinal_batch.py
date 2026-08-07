"""Outcome-blind batch planning for longitudinal Sourcegraph extraction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from authorship.sourcegraph_longitudinal import (
    HORIZONS,
    LongitudinalExtractionError,
    _is_hex,
    _repository_fields,
)
from authorship.sourcegraph_longitudinal_execution import build_longitudinal_unit

BATCH_PLAN_VERSION = 3


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def longitudinal_batch_plan_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "longitudinal_batch_plan_sha256"
    }
    return _sha256(content)


def _repositories(document: Mapping[str, Any], name: str) -> dict[str, Mapping]:
    records = document.get("repositories")
    if not isinstance(records, list):
        raise LongitudinalExtractionError(f"{name} repositories must be a list")
    by_id = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise LongitudinalExtractionError(f"{name} repository must be an object")
        identifier = record.get("repository_id") or record.get(
            "canonical_repository_id"
        )
        if not isinstance(identifier, str) or identifier in by_id:
            raise LongitudinalExtractionError(
                f"{name} repository identity is invalid or duplicated"
            )
        by_id[identifier] = record
    return by_id


def _validate_population(
    git_inventory: Mapping[str, Any],
    cohort_inventory: Mapping[str, Any],
    lineage_inventory: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
) -> None:
    candidate_shas = {
        document.get("candidate_frame_sha256")
        for document in (git_inventory, cohort_inventory, lineage_inventory)
    }
    if len(candidate_shas) != 1 or not _is_hex(next(iter(candidate_shas)), 64):
        raise LongitudinalExtractionError("candidate frame checksums do not match")
    if tuple(lineage_inventory.get("horizons_days", [])) != HORIZONS:
        raise LongitudinalExtractionError("lineage horizons do not match v3")
    if any(
        document.get("outcomes_consulted") is not False
        for document in (git_inventory, cohort_inventory, index_manifest)
    ):
        raise LongitudinalExtractionError("batch inputs must be outcome blind")


def _sourcegraph_name(index_record: Mapping[str, Any]) -> str:
    sourcegraph = index_record.get("sourcegraph")
    name = (
        sourcegraph.get("selected_name") if isinstance(sourcegraph, Mapping) else None
    )
    if not isinstance(name, str) or not name.startswith("github.com/sg-evals/"):
        raise LongitudinalExtractionError("selected sg-evals repository is required")
    return name


def _repository(
    git_record: Mapping[str, Any], index_record: Mapping[str, Any]
) -> dict[str, Any]:
    fields = {
        "canonical_repository_id": git_record["repository_id"],
        "sourcegraph_name": _sourcegraph_name(index_record),
        "cutoff_commit": git_record.get("cutoff_commit"),
        "cutoff_tree": git_record.get("cutoff_tree"),
        "bundle_sha256": git_record.get("bundle_sha256"),
        "cache_path": git_record.get("cache_path"),
    }
    if (
        git_record.get("status") != "pinned"
        or index_record.get("cutoff_commit") != fields["cutoff_commit"]
        or index_record.get("cutoff_tree") != fields["cutoff_tree"]
    ):
        raise LongitudinalExtractionError("pinned and indexed cutoffs do not match")
    _repository_fields(fields)
    if not isinstance(fields["cache_path"], str) or not fields["cache_path"]:
        raise LongitudinalExtractionError("pinned repository cache path is required")
    return fields


def _unit(
    repository_id: str,
    git_record: Mapping[str, Any],
    cohort_record: Mapping[str, Any],
    lineage_record: Mapping[str, Any],
    index_record: Mapping[str, Any],
    transition_root: Path,
    probe_root: Path,
) -> dict[str, Any]:
    slug = repository_id.replace("/", "__")
    repository = _repository(git_record, index_record)
    introduction = {
        "path": cohort_record.get("shard_path"),
        "sha256": cohort_record.get("shard_sha256"),
    }
    transition = {
        "path": (transition_root / f"{slug}.jsonl").as_posix(),
        "sha256": lineage_record.get("transition_sha256"),
    }
    if (
        cohort_record.get("status") != "reconstructed"
        or cohort_record.get("cutoff_commit") != repository["cutoff_commit"]
        or not isinstance(introduction["path"], str)
        or not _is_hex(introduction["sha256"], 64)
        or not _is_hex(transition["sha256"], 64)
    ):
        raise LongitudinalExtractionError("cohort or lineage shard is invalid")
    identity = {
        **{
            field: repository[field]
            for field in (
                "canonical_repository_id",
                "sourcegraph_name",
                "cutoff_commit",
                "cutoff_tree",
                "bundle_sha256",
            )
        },
        "introduction_sha256": introduction["sha256"],
        "transition_sha256": transition["sha256"],
    }
    return {
        "probe_unit_id": _sha256(identity),
        "repository": repository,
        "introduction_shard": introduction,
        "transition_shard": transition,
        "probe_status": "pending_sourcegraph",
        "probe_manifest_path": (probe_root / f"{slug}.json").as_posix(),
    }


def build_batch_plan(
    git_inventory: Mapping[str, Any],
    cohort_inventory: Mapping[str, Any],
    lineage_inventory: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    *,
    transition_root: Path,
    probe_root: Path,
) -> dict[str, Any]:
    """Build the deterministic pre-probe plan for the lineage population."""
    _validate_population(
        git_inventory, cohort_inventory, lineage_inventory, index_manifest
    )
    sources = (
        _repositories(git_inventory, "Git inventory"),
        _repositories(cohort_inventory, "cohort inventory"),
        _repositories(lineage_inventory, "lineage inventory"),
        _repositories(index_manifest, "index manifest"),
    )
    population = set(sources[2])
    if any(not population <= set(source) for source in sources):
        raise LongitudinalExtractionError("lineage population is missing from an input")
    units = [
        _unit(
            repository_id,
            *(source[repository_id] for source in sources),
            transition_root,
            probe_root,
        )
        for repository_id in sorted(population)
    ]
    document = {
        "longitudinal_batch_plan_version": BATCH_PLAN_VERSION,
        "candidate_frame_sha256": git_inventory["candidate_frame_sha256"],
        "input_inventory_sha256s": {
            "git": _sha256(git_inventory),
            "cohort": _sha256(cohort_inventory),
            "lineage": _sha256(lineage_inventory),
            "sourcegraph_index": _sha256(index_manifest),
        },
        "horizons_days": list(HORIZONS),
        "repository_count": len(units),
        "units": units,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "longitudinal_batch_plan_sha256": longitudinal_batch_plan_sha256(document),
    }


def materialize_execution_unit(
    planned_unit: Mapping[str, Any], probe_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    repository = planned_unit.get("repository")
    if not isinstance(repository, Mapping):
        raise LongitudinalExtractionError("planned repository is required")
    expected = (
        repository.get("canonical_repository_id"),
        repository.get("sourcegraph_name"),
        repository.get("cutoff_commit"),
    )
    actual = (
        probe_manifest.get("canonical_repository_id"),
        probe_manifest.get("sourcegraph_name"),
        probe_manifest.get("cutoff_commit"),
    )
    if actual != expected:
        raise LongitudinalExtractionError("probe manifest does not match planned unit")
    manifest_sha256 = probe_manifest.get("probe_manifest_sha256")
    if not _is_hex(manifest_sha256, 64):
        raise LongitudinalExtractionError("probe manifest checksum is invalid")
    return build_longitudinal_unit(
        repository,
        introduction_shard=planned_unit.get("introduction_shard", {}),
        transition_shard=planned_unit.get("transition_shard", {}),
        sourcegraph_result_manifest_sha256s=[manifest_sha256],
    )
