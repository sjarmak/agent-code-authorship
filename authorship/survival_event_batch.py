"""Resumable batch execution for exact survival event shards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from authorship.survival_event_materializer import (
    MaterializationResult,
    SurvivalEventMaterializationError,
    materialize_repository,
)

Materializer = Callable[..., MaterializationResult]
Progress = Callable[[Mapping[str, Any], int, int], None]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def survival_event_inventory_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "survival_event_inventory_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SurvivalEventMaterializationError(f"{label} is invalid") from error
    if not isinstance(document, Mapping):
        raise SurvivalEventMaterializationError(f"{label} must be an object")
    return document


def _repository_map(
    inventory: Mapping[str, Any], label: str
) -> dict[str, Mapping[str, Any]]:
    records = inventory.get("repositories")
    if not isinstance(records, list) or not records:
        raise SurvivalEventMaterializationError(f"{label} repositories are invalid")
    result = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise SurvivalEventMaterializationError(
                f"{label} repository must be an object"
            )
        repository_id = record.get("repository_id")
        if not isinstance(repository_id, str) or not repository_id:
            raise SurvivalEventMaterializationError(f"{label} repository_id is invalid")
        if repository_id in result:
            raise SurvivalEventMaterializationError(
                f"{label} repository_id is duplicated"
            )
        result[repository_id] = record
    return result


def _load_inventories(git_inventory_path: Path, lineage_inventory_path: Path) -> tuple[
    str,
    str,
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    git_inventory_sha256 = _sha256(git_inventory_path)
    lineage_inventory_sha256 = _sha256(lineage_inventory_path)
    git_inventory = _load_json(git_inventory_path, "git inventory")
    lineage_inventory = _load_json(lineage_inventory_path, "lineage inventory")
    if lineage_inventory.get("git_inventory_sha256") != git_inventory_sha256:
        raise SurvivalEventMaterializationError(
            "lineage git inventory checksum does not match"
        )
    git_records = _repository_map(git_inventory, "git inventory")
    lineage_records = _repository_map(lineage_inventory, "lineage inventory")
    if not set(lineage_records).issubset(git_records):
        raise SurvivalEventMaterializationError(
            "lineage inventory population is not pinned in the git inventory"
        )
    return (
        git_inventory_sha256,
        lineage_inventory_sha256,
        git_records,
        lineage_records,
    )


def _slug(repository_id: str) -> str:
    parts = repository_id.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise SurvivalEventMaterializationError("repository_id cannot form a shard")
    return "__".join(parts)


def _valid_entry(
    repository_id: str,
    result: MaterializationResult,
    manifest_path: Path,
) -> dict[str, Any]:
    manifest = result.manifest
    return {
        "repository_id": repository_id,
        "status": "valid",
        "line_count": manifest["line_count"],
        "event_shard_file": manifest["event_shard_file"],
        "event_shard_sha256": manifest["event_shard_sha256"],
        "manifest_file": manifest_path.name,
        "manifest_sha256": manifest["survival_event_shard_manifest_sha256"],
    }


def _execute_repository(
    *,
    repository_id: str,
    git_record: Mapping[str, Any],
    lineage_record: Mapping[str, Any],
    git_inventory_sha256: str,
    transition_root: Path,
    structural_event_root: Path,
    lineage_validation_path: Path,
    output_root: Path,
    work_root: Path,
    commit_batch_size: int,
    materializer: Materializer,
) -> dict[str, Any]:
    slug = _slug(repository_id)
    output_path = output_root / "shards" / f"{slug}.jsonl"
    manifest_path = output_root / "manifests" / f"{slug}.json"
    result = materializer(
        repository_id=repository_id,
        repository_path=Path(git_record["cache_path"]),
        transition_path=transition_root / f"{slug}.jsonl",
        structural_event_path=structural_event_root / f"{slug}.jsonl",
        lineage_validation_path=lineage_validation_path,
        git_inventory_record=git_record,
        lineage_inventory_record=lineage_record,
        git_inventory_sha256=git_inventory_sha256,
        output_path=output_path,
        manifest_path=manifest_path,
        work_root=work_root,
        commit_batch_size=commit_batch_size,
    )
    return _valid_entry(repository_id, result, manifest_path)


def _counts(entries: list[Mapping[str, Any]]) -> dict[str, int]:
    valid = [entry for entry in entries if entry["status"] == "valid"]
    return {
        "repositories": len(entries),
        "valid": len(valid),
        "invalid": len(entries) - len(valid),
        "lines": sum(entry["line_count"] for entry in valid),
    }


def _inventory(
    git_inventory_sha256: str,
    lineage_inventory_sha256: str,
    lineage_validation_sha256: str,
    entries: list[Mapping[str, Any]],
) -> dict[str, Any]:
    counts = _counts(entries)
    document = {
        "contract_version": 1,
        "status": "complete" if counts["invalid"] == 0 else "incomplete",
        "git_inventory_sha256": git_inventory_sha256,
        "lineage_inventory_sha256": lineage_inventory_sha256,
        "lineage_validation_sha256": lineage_validation_sha256,
        "counts": counts,
        "repositories": entries,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "survival_event_inventory_sha256": (survival_event_inventory_sha256(document)),
    }


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as destination:
            destination.write(f"{_canonical_json(document)}\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def execute_event_materialization(
    *,
    git_inventory_path: Path,
    lineage_inventory_path: Path,
    lineage_validation_path: Path,
    transition_root: Path,
    structural_event_root: Path,
    output_root: Path,
    work_root: Path,
    commit_batch_size: int = 100_000,
    materializer: Materializer = materialize_repository,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Execute every frozen repository and continue after repository failures."""
    (
        git_inventory_sha256,
        lineage_inventory_sha256,
        git_records,
        lineage_records,
    ) = _load_inventories(git_inventory_path, lineage_inventory_path)
    lineage_validation_sha256 = _sha256(lineage_validation_path)
    repository_ids = sorted(lineage_records)
    entries = []
    for index, repository_id in enumerate(repository_ids, start=1):
        try:
            entry = _execute_repository(
                repository_id=repository_id,
                git_record=git_records[repository_id],
                lineage_record=lineage_records[repository_id],
                git_inventory_sha256=git_inventory_sha256,
                transition_root=transition_root,
                structural_event_root=structural_event_root,
                lineage_validation_path=lineage_validation_path,
                output_root=output_root,
                work_root=work_root,
                commit_batch_size=commit_batch_size,
                materializer=materializer,
            )
        except (SurvivalEventMaterializationError, OSError, KeyError) as error:
            entry = {
                "repository_id": repository_id,
                "status": "invalid",
                "error": str(error),
            }
        entries.append(entry)
        if progress is not None:
            progress(entry, index, len(repository_ids))
    inventory = _inventory(
        git_inventory_sha256,
        lineage_inventory_sha256,
        lineage_validation_sha256,
        entries,
    )
    _atomic_json(output_root / "survival-event-inventory.v1.json", inventory)
    return inventory
