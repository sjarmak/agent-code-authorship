"""Execution and pinned-Git IO for longitudinal Sourcegraph shards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from authorship.languages import SKIP_PATH
from authorship.sourcegraph_longitudinal import (
    LongitudinalExtractionError,
    _content_sha256,
    _group_introductions,
    _is_hex,
    _required_list,
    _required_mapping,
    _repository_fields,
    _timestamp,
    _validate_protocol,
    build_longitudinal_shard,
)
from authorship.sourcegraph_longitudinal_validation import (
    validate_longitudinal_shard,
)
from authorship.survival_git import SurvivalGitError, git

MAX_FILE_AGE_WORKERS = 8


def _shard_reference(reference: Mapping[str, Any], field: str) -> dict[str, str]:
    path = reference.get("path")
    sha256 = reference.get("sha256")
    if not isinstance(path, str) or not path or not _is_hex(sha256, 64):
        raise LongitudinalExtractionError(
            f"{field} requires a path and SHA-256 checksum"
        )
    return {"path": path, "sha256": sha256}


def build_longitudinal_unit(
    repository: Mapping[str, Any],
    *,
    introduction_shard: Mapping[str, Any],
    transition_shard: Mapping[str, Any],
    sourcegraph_result_manifest_sha256s: Sequence[str],
) -> dict[str, Any]:
    """Create an input-content-bound execution unit."""
    fields = _repository_fields(repository)
    introductions = _shard_reference(introduction_shard, "introduction_shard")
    transitions = _shard_reference(transition_shard, "transition_shard")
    result_shas = sorted(sourcegraph_result_manifest_sha256s)
    if (
        not result_shas
        or len(result_shas) != len(set(result_shas))
        or any(not _is_hex(value, 64) for value in result_shas)
    ):
        raise LongitudinalExtractionError(
            "Sourcegraph result manifest checksums are required"
        )
    identity = {
        **fields,
        "introduction_sha256": introductions["sha256"],
        "transition_sha256": transitions["sha256"],
        "sourcegraph_result_manifest_sha256s": result_shas,
    }
    return {
        "unit_id": _content_sha256(identity),
        "repository": dict(repository),
        "introduction_shard": introductions,
        "transition_shard": transitions,
        "sourcegraph_result_manifest_sha256s": result_shas,
    }


def shard_path_for(unit: Mapping[str, Any]) -> Path:
    unit_id = unit.get("unit_id")
    if not _is_hex(unit_id, 64):
        raise LongitudinalExtractionError("unit_id is invalid")
    return Path("shards") / f"{unit_id}.json"


def _validated_unit_repository(unit: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = _required_mapping(unit.get("repository"), "unit.repository")
    expected = build_longitudinal_unit(
        repository,
        introduction_shard=_required_mapping(
            unit.get("introduction_shard"), "unit.introduction_shard"
        ),
        transition_shard=_required_mapping(
            unit.get("transition_shard"), "unit.transition_shard"
        ),
        sourcegraph_result_manifest_sha256s=_required_list(
            unit.get("sourcegraph_result_manifest_sha256s"),
            "unit Sourcegraph result manifests",
        ),
    )
    if unit.get("unit_id") != expected["unit_id"]:
        raise LongitudinalExtractionError(
            "execution unit identity does not match inputs"
        )
    return repository


def _load_reusable(
    path: Path,
    protocol: Mapping[str, Any],
    repository: Mapping[str, str],
    unit_id: str,
) -> Mapping[str, Any] | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if validate_longitudinal_shard(document, protocol):
        return None
    expected = (
        unit_id,
        repository["canonical_repository_id"],
        repository["sourcegraph_name"],
        repository["cutoff_commit"],
    )
    actual = (
        document.get("execution_unit_id"),
        document.get("canonical_repository_id"),
        document.get("sourcegraph_name"),
        document.get("cutoff_commit"),
    )
    return document if actual == expected else None


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _execution_summary(
    document: Mapping[str, Any], path: Path, reused: bool
) -> dict[str, Any]:
    return {
        "canonical_repository_id": document["canonical_repository_id"],
        "shard_path": path.as_posix(),
        "longitudinal_shard_sha256": document["longitudinal_shard_sha256"],
        "hunk_count": document["hunk_count"],
        "excluded_hunk_count": document["excluded_hunk_count"],
        "sourcegraph_verification_status": document["sourcegraph"][
            "verification_status"
        ],
        "reused": reused,
    }


def _load_jsonl_reference(reference: Any, field: str) -> list[Mapping[str, Any]]:
    record = _required_mapping(reference, field)
    path_value = record.get("path")
    expected_sha256 = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
        raise LongitudinalExtractionError(f"{field} path and checksum are required")
    path = Path(path_value)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise LongitudinalExtractionError(f"{field} cannot be read: {error}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise LongitudinalExtractionError(f"{field} checksum does not match")
    records = []
    for line_number, raw_line in enumerate(payload.splitlines(), 1):
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as error:
            raise LongitudinalExtractionError(
                f"{field} line {line_number} is invalid JSON"
            ) from error
        records.append(_required_mapping(parsed, f"{field} line {line_number}"))
    return records


def _verify_pinned_repository(
    repository: Mapping[str, Any], fields: Mapping[str, str]
) -> Path:
    cache_path = repository.get("cache_path")
    if not isinstance(cache_path, str):
        raise LongitudinalExtractionError("repository cache_path is required")
    path = Path(cache_path)
    try:
        commit = git(path, "rev-parse", f"{fields['cutoff_commit']}^{{commit}}")
        tree = git(path, "rev-parse", f"{fields['cutoff_commit']}^{{tree}}")
    except (OSError, SurvivalGitError) as error:
        raise LongitudinalExtractionError(
            f"pinned Git verification failed: {error}"
        ) from error
    if commit != fields["cutoff_commit"] or tree != fields["cutoff_tree"]:
        raise LongitudinalExtractionError("pinned Git cutoff identity does not match")
    return path


def _records_repository_id(introductions: Sequence[Mapping[str, Any]]) -> str:
    identifiers = {record.get("repository_id") for record in introductions}
    if len(identifiers) != 1:
        raise LongitudinalExtractionError(
            "introductions must contain exactly one repository"
        )
    identifier = next(iter(identifiers))
    if not isinstance(identifier, str) or not identifier:
        raise LongitudinalExtractionError("introduction repository is required")
    return identifier


def _file_age_days(
    repository: Path, introductions: Sequence[Mapping[str, Any]]
) -> dict[str, float]:
    if not introductions:
        return {}
    groups = [
        (key, records)
        for _hunk_id_value, key, records in _group_introductions(
            _records_repository_id(introductions), introductions
        )
        if not SKIP_PATH(key[1])
    ]
    if not groups:
        return {}
    worker_count = min(MAX_FILE_AGE_WORKERS, len(groups))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        ages = executor.map(
            lambda group: _file_age_record(repository, *group),
            groups,
        )
    return dict(ages)


def _file_age_record(
    repository: Path,
    key: tuple[str, str],
    records: Sequence[Mapping[str, Any]],
) -> tuple[str, float]:
    dates = _file_history_dates(repository, key)
    if not dates:
        raise LongitudinalExtractionError("file history has no dated commit")
    age = (
        _timestamp(records[0].get("merged_at"), "merged_at")
        - _timestamp(dates[0], "file creation date")
    ).total_seconds() / 86_400
    if age < 0:
        raise LongitudinalExtractionError("file code age cannot be negative")
    return f"{key[0]}\0{key[1]}", age


def _file_history_dates(repository: Path, key: tuple[str, str]) -> list[str]:
    try:
        output = git(
            repository,
            "log",
            "--follow",
            "--reverse",
            "--format=%cI",
            key[0],
            "--",
            key[1],
        )
        dates = [raw for raw in output.splitlines() if raw]
        if dates:
            return dates
        git(repository, "cat-file", "-e", f"{key[0]}:{key[1]}")
        fallback = git(
            repository,
            "log",
            "--full-history",
            "--reverse",
            "--format=%cI",
            key[0],
            "--",
            key[1],
        )
    except (OSError, SurvivalGitError) as error:
        raise LongitudinalExtractionError(
            f"file history cannot be reconstructed: {error}"
        ) from error
    return [raw for raw in fallback.splitlines() if raw]


def extract_pinned_git_unit(unit: Mapping[str, Any]) -> dict[str, Any]:
    """Load checksummed v1 inputs and derive file age from pinned Git."""
    repository = _required_mapping(unit.get("repository"), "unit.repository")
    fields = _repository_fields(repository)
    introductions = _load_jsonl_reference(
        unit.get("introduction_shard"), "introduction_shard"
    )
    transitions = _load_jsonl_reference(
        unit.get("transition_shard"), "transition_shard"
    )
    path = _verify_pinned_repository(repository, fields)
    return {
        "introductions": introductions,
        "transitions": transitions,
        "file_age_days": _file_age_days(path, introductions),
    }


def _sourcegraph_payload(
    unit: Mapping[str, Any],
    probe: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> Mapping[str, Any]:
    payload = probe(unit)
    if not isinstance(payload, Mapping):
        raise LongitudinalExtractionError("Sourcegraph probe must return an object")
    expected_shas = _required_list(
        unit.get("sourcegraph_result_manifest_sha256s"),
        "unit Sourcegraph result manifests",
    )
    observed_shas = _required_list(
        payload.get("result_manifest_sha256s"),
        "Sourcegraph result manifests",
    )
    if sorted(observed_shas) != expected_shas:
        raise LongitudinalExtractionError(
            "Sourcegraph result manifests do not match execution unit"
        )
    return payload


def execute_longitudinal_unit(
    protocol: Mapping[str, Any],
    unit: Mapping[str, Any],
    output_directory: Path,
    *,
    sourcegraph_probe: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    git_extractor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute or reuse one identity-bound repository shard."""
    _validate_protocol(protocol)
    repository = _validated_unit_repository(unit)
    fields = _repository_fields(repository)
    unit_id = unit.get("unit_id")
    if not _is_hex(unit_id, 64):
        raise LongitudinalExtractionError("unit_id is invalid")
    path = output_directory / shard_path_for(unit)
    reusable = _load_reusable(path, protocol, fields, unit_id)
    if reusable is not None:
        return _execution_summary(reusable, path, True)
    extractor = git_extractor or extract_pinned_git_unit
    git_payload = extractor(unit)
    if not isinstance(git_payload, Mapping):
        raise LongitudinalExtractionError("git extractor must return an object")
    sourcegraph_payload = _sourcegraph_payload(unit, sourcegraph_probe)
    document = build_longitudinal_shard(
        protocol,
        repository,
        _required_list(git_payload.get("introductions"), "git introductions"),
        _required_list(git_payload.get("transitions"), "git transitions"),
        execution_unit_id=unit_id,
        file_age_days=_required_mapping(
            git_payload.get("file_age_days"), "git file_age_days"
        ),
        sourcegraph_observation=sourcegraph_payload,
    )
    _atomic_write(path, document)
    return _execution_summary(document, path, False)
