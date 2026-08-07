"""Submit frozen Sourcegraph adjudication shards to the OpenAI Batch API."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import fcntl

from authorship.sourcegraph_adjudication_batch import (
    AdjudicationBatchError,
    _safe_batch_file,
    _validated_manifest_files,
)

SUBMISSION_VERSION = 3
STUDY_ID = "sourcegraph-adjudication-v3"
COMPLETION_WINDOW = "24h"
JSONL_CONTENT_TYPE = "application/jsonl"
EXPECTED_SHARD_COUNT = 5
FILE_IMMUTABLE_FIELDS = (
    "shard",
    "path",
    "sha256",
    "byte_count",
    "request_count",
    "remote_filename",
    "openai_file_id",
    "openai_created_at",
)
BATCH_IMMUTABLE_FIELDS = (
    "reviewer_id",
    "shard",
    "input_sha256",
    "batch_manifest_sha256",
    "input_file_id",
    "batch_id",
    "endpoint",
    "completion_window",
    "metadata",
    "created_at",
)


class AdjudicationSubmissionError(ValueError):
    """Raised when a paid submission would violate the frozen contract."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise AdjudicationSubmissionError(f"cannot read {path}: {error}") from error
    return digest.hexdigest()


@contextmanager
def _submission_lock(journal_path: Path) -> Iterator[None]:
    identity = hashlib.sha256(str(journal_path.resolve()).encode()).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"agent-code-authorship-{identity}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
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


def _load_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdjudicationSubmissionError(
            f"cannot read submission journal: {error}"
        ) from error
    if not isinstance(document, dict):
        raise AdjudicationSubmissionError("submission journal must be an object")
    return document


def _value(remote: Any, key: str) -> Any:
    return (
        remote.get(key) if isinstance(remote, Mapping) else getattr(remote, key, None)
    )


def _page_records(page: Any) -> list[Any]:
    try:
        return list(page)
    except TypeError:
        data = _value(page, "data")
        return list(data) if isinstance(data, Sequence) else []


def _jsonable(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    return value


def _validate_immutable_record(
    prior: Mapping[str, Any],
    current: Mapping[str, Any],
    fields: Sequence[str],
    label: str,
) -> None:
    if any(prior.get(field) != current.get(field) for field in fields):
        raise AdjudicationSubmissionError(f"immutable {label} journal drift detected")


def _remote_content_identity(client: Any, file_id: str) -> tuple[str, int]:
    response = client.files.content(file_id)
    iterator = getattr(response, "iter_bytes", None)
    if not callable(iterator):
        raise AdjudicationSubmissionError(
            f"cannot stream recovered remote file {file_id}"
        )
    digest = hashlib.sha256()
    byte_count = 0
    for chunk in iterator():
        digest.update(chunk)
        byte_count += len(chunk)
    return digest.hexdigest(), byte_count


@contextmanager
def _validated_snapshot(local: Mapping[str, Any]) -> Iterator[BinaryIO]:
    digest = hashlib.sha256()
    byte_count = 0
    path = Path(local["path"])
    try:
        with path.open("rb") as source, tempfile.TemporaryFile(
            mode="w+b", dir=path.parent
        ) as snapshot:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                byte_count += len(chunk)
                snapshot.write(chunk)
            if (
                digest.hexdigest() != local["sha256"]
                or byte_count != local["byte_count"]
            ):
                raise AdjudicationSubmissionError(
                    f"batch input changed before upload for shard {local['shard']}"
                )
            snapshot.flush()
            snapshot.seek(0)
            yield snapshot
    except OSError as error:
        raise AdjudicationSubmissionError(
            f"cannot snapshot batch input {path}: {error}"
        ) from error


def _remote_filename(shard: str, sha256: str) -> str:
    return f"{STUDY_ID}-{shard}-{sha256[:16]}.jsonl"


def _manifest_identity(
    manifest: Mapping[str, Any], path: Path, file_sha256: str
) -> dict[str, Any]:
    return {
        "path": str(path),
        "file_sha256": file_sha256,
        "batch_manifest_sha256": manifest["batch_manifest_sha256"],
        "reviewer_id": manifest["reviewer_id"],
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "prompt_sha256": manifest["prompt_sha256"],
    }


def _validate_local_contract(
    manifests: Sequence[Mapping[str, Any]],
    manifest_paths: Sequence[Path],
    manifest_file_sha256s: Sequence[str],
    input_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(manifests) != 2 or len(manifest_paths) != 2:
        raise AdjudicationSubmissionError("exactly two reviewer manifests are required")
    if len(manifest_file_sha256s) != 2:
        raise AdjudicationSubmissionError("exactly two manifest checksums are required")
    try:
        manifest_files = [list(_validated_manifest_files(item)) for item in manifests]
    except AdjudicationBatchError as error:
        raise AdjudicationSubmissionError(str(error)) from error
    reviewers = [item["reviewer_id"] for item in manifests]
    common_fields = (
        "specification_sha256",
        "queue_manifest_sha256",
        "stage",
        "reviewer_kind",
        "reviewer_version",
        "provider",
        "model",
        "reasoning_effort",
        "prompt_sha256",
        "endpoint",
        "request_count",
    )
    if (
        len(set(reviewers)) != 2
        or manifest_files[0] != manifest_files[1]
        or len(manifest_files[0]) != EXPECTED_SHARD_COUNT
        or any(
            manifests[0].get(field) != manifests[1].get(field)
            for field in common_fields
        )
    ):
        raise AdjudicationSubmissionError(
            "reviewer manifests do not share one frozen request contract"
        )
    inputs = []
    for record in manifest_files[0]:
        path = _safe_batch_file(input_root, record["file"])
        try:
            byte_count = path.stat().st_size
        except OSError as error:
            raise AdjudicationSubmissionError(
                f"cannot stat batch input {path}: {error}"
            ) from error
        sha256 = _sha256_file(path)
        if byte_count != record["byte_count"] or sha256 != record["sha256"]:
            raise AdjudicationSubmissionError(
                f"batch input drift detected for {record['file']}"
            )
        shard = path.stem
        inputs.append(
            {
                "shard": shard,
                "path": str(path),
                "sha256": sha256,
                "byte_count": byte_count,
                "request_count": record["request_count"],
                "remote_filename": _remote_filename(shard, sha256),
            }
        )
    if (
        len({item["shard"] for item in inputs}) != EXPECTED_SHARD_COUNT
        or len({item["sha256"] for item in inputs}) != EXPECTED_SHARD_COUNT
    ):
        raise AdjudicationSubmissionError(
            "the frozen contract requires five unique request shards"
        )
    identities = [
        _manifest_identity(manifest, path, file_sha256)
        for manifest, path, file_sha256 in zip(
            manifests, manifest_paths, manifest_file_sha256s, strict=True
        )
    ]
    return identities, inputs


def _submission_key(
    reviewer_id: str, shard: str, input_sha256: str, manifest_sha256: str
) -> str:
    value = ":".join((STUDY_ID, reviewer_id, shard, input_sha256, manifest_sha256))
    return hashlib.sha256(value.encode()).hexdigest()


def _batch_metadata(
    reviewer_id: str, shard: str, input_sha256: str, manifest_sha256: str
) -> dict[str, str]:
    return {
        "study": STUDY_ID,
        "reviewer": reviewer_id,
        "shard": shard,
        "input_sha256": input_sha256,
        "batch_manifest_sha256": manifest_sha256,
        "submission_key": _submission_key(
            reviewer_id, shard, input_sha256, manifest_sha256
        ),
    }


def _file_record(local: Mapping[str, Any], remote: Any) -> dict[str, Any]:
    if (
        _value(remote, "filename") != local["remote_filename"]
        or _value(remote, "bytes") != local["byte_count"]
        or _value(remote, "purpose") != "batch"
    ):
        raise AdjudicationSubmissionError(
            f"remote file does not match frozen shard {local['shard']}"
        )
    return {
        **dict(local),
        "openai_file_id": _value(remote, "id"),
        "openai_created_at": _value(remote, "created_at"),
        "openai_status": _value(remote, "status"),
    }


def _request_counts(remote: Any) -> dict[str, Any] | None:
    counts = _value(remote, "request_counts")
    if counts is None:
        return None
    return {
        "total": _value(counts, "total"),
        "completed": _value(counts, "completed"),
        "failed": _value(counts, "failed"),
    }


def _batch_record(
    remote: Any,
    reviewer_id: str,
    shard: str,
    input_sha256: str,
    manifest_sha256: str,
    file_id: str,
) -> dict[str, Any]:
    metadata = _batch_metadata(reviewer_id, shard, input_sha256, manifest_sha256)
    if (
        _value(remote, "input_file_id") != file_id
        or _value(remote, "endpoint") != "/v1/responses"
        or _value(remote, "completion_window") != COMPLETION_WINDOW
        or _value(remote, "metadata") != metadata
    ):
        raise AdjudicationSubmissionError(
            f"remote batch does not match reviewer {reviewer_id} shard {shard}"
        )
    return {
        "reviewer_id": reviewer_id,
        "shard": shard,
        "input_sha256": input_sha256,
        "batch_manifest_sha256": manifest_sha256,
        "input_file_id": file_id,
        "batch_id": _value(remote, "id"),
        "endpoint": _value(remote, "endpoint"),
        "completion_window": _value(remote, "completion_window"),
        "metadata": metadata,
        "status": _value(remote, "status"),
        "created_at": _value(remote, "created_at"),
        "output_file_id": _value(remote, "output_file_id"),
        "error_file_id": _value(remote, "error_file_id"),
        "request_counts": _request_counts(remote),
        "errors": _jsonable(_value(remote, "errors")),
    }


def _new_journal(
    manifest_identities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "submission_version": SUBMISSION_VERSION,
        "study_id": STUDY_ID,
        "endpoint": "/v1/responses",
        "completion_window": COMPLETION_WINDOW,
        "manifests": list(manifest_identities),
        "input_files": [],
        "batches": [],
        "updated_at": _utc_now(),
    }


def _validate_journal(
    journal: Mapping[str, Any],
    manifest_identities: Sequence[Mapping[str, Any]],
) -> None:
    expected = {
        "submission_version": SUBMISSION_VERSION,
        "study_id": STUDY_ID,
        "endpoint": "/v1/responses",
        "completion_window": COMPLETION_WINDOW,
        "manifests": list(manifest_identities),
    }
    if any(journal.get(key) != value for key, value in expected.items()):
        raise AdjudicationSubmissionError(
            "submission journal does not match the frozen manifests"
        )
    if not isinstance(journal.get("input_files"), list) or not isinstance(
        journal.get("batches"), list
    ):
        raise AdjudicationSubmissionError("submission journal records are invalid")


def _save(path: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = _utc_now()
    _atomic_json(path, journal)


def _select_unique(records: Sequence[Any], predicate, label: str) -> Any | None:
    matches = [record for record in records if predicate(record)]
    if len(matches) > 1:
        raise AdjudicationSubmissionError(f"multiple remote {label} objects match")
    return matches[0] if matches else None


def _validate_journal_coverage(
    journal: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    manifests: Sequence[Mapping[str, Any]],
) -> None:
    file_records = journal["input_files"]
    batch_records = journal["batches"]
    if any(
        not isinstance(record, Mapping)
        or not {"sha256", "openai_file_id"}.issubset(record)
        for record in file_records
    ) or any(
        not isinstance(record, Mapping)
        or not {"reviewer_id", "shard", "batch_id"}.issubset(record)
        for record in batch_records
    ):
        raise AdjudicationSubmissionError("submission journal records are malformed")
    file_keys = [record["sha256"] for record in file_records]
    expected_file_keys = {local["sha256"] for local in inputs}
    batch_keys = [(record["reviewer_id"], record["shard"]) for record in batch_records]
    expected_batch_keys = {
        (manifest["reviewer_id"], local["shard"])
        for manifest in manifests
        for local in inputs
    }
    if (
        len(file_keys) != len(set(file_keys))
        or not set(file_keys).issubset(expected_file_keys)
        or len(batch_keys) != len(set(batch_keys))
        or not set(batch_keys).issubset(expected_batch_keys)
    ):
        raise AdjudicationSubmissionError("submission journal coverage is invalid")


def _preflight_files(
    client: Any,
    inputs: Sequence[Mapping[str, Any]],
    journal: Mapping[str, Any],
) -> dict[str, Any]:
    known = {record["sha256"]: record for record in journal["input_files"]}
    remote_files = _page_records(client.files.list(purpose="batch", limit=100))
    resolved: dict[str, Any] = {}
    for local in inputs:
        prior = known.get(local["sha256"])
        if prior is not None:
            remote = client.files.retrieve(prior["openai_file_id"])
        else:
            same_name = [
                item
                for item in remote_files
                if _value(item, "filename") == local["remote_filename"]
            ]
            if same_name and any(
                _value(item, "bytes") != local["byte_count"] for item in same_name
            ):
                raise AdjudicationSubmissionError(
                    f"remote filename collision for shard {local['shard']}"
                )
            remote = _select_unique(
                same_name,
                lambda item: _value(item, "purpose") == "batch",
                f"file for shard {local['shard']}",
            )
        if remote is not None:
            current = _file_record(local, remote)
            if prior is not None:
                _validate_immutable_record(
                    prior,
                    current,
                    FILE_IMMUTABLE_FIELDS,
                    f"file {local['shard']}",
                )
            else:
                sha256, byte_count = _remote_content_identity(
                    client, current["openai_file_id"]
                )
                if sha256 != local["sha256"] or byte_count != local["byte_count"]:
                    raise AdjudicationSubmissionError(
                        f"recovered remote file content drift for shard {local['shard']}"
                    )
            resolved[local["sha256"]] = remote
    return resolved


def _create_files(
    client: Any,
    inputs: Sequence[Mapping[str, Any]],
    resolved: Mapping[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    known = {record["sha256"]: record for record in journal["input_files"]}
    for local in inputs:
        prior = known.get(local["sha256"])
        remote = resolved.get(local["sha256"])
        if remote is None:
            with _validated_snapshot(local) as stream:
                remote = client.files.create(
                    file=(
                        local["remote_filename"],
                        stream,
                        JSONL_CONTENT_TYPE,
                    ),
                    purpose="batch",
                )
        record = _file_record(local, remote)
        if prior is None:
            journal["input_files"].append(record)
        else:
            journal["input_files"] = [
                record if item["sha256"] == local["sha256"] else item
                for item in journal["input_files"]
            ]
        _save(journal_path, journal)


def _submission_key_from_remote(remote: Any) -> Any:
    metadata = _value(remote, "metadata")
    return metadata.get("submission_key") if isinstance(metadata, Mapping) else None


def _preflight_batches(
    client: Any,
    manifests: Sequence[Mapping[str, Any]],
    inputs: Sequence[Mapping[str, Any]],
    remote_file_records: Mapping[str, Mapping[str, Any]],
    journal: Mapping[str, Any],
) -> dict[tuple[str, str], Any]:
    known_records = journal["batches"]
    known = {
        (record["reviewer_id"], record["shard"]): record for record in known_records
    }
    remote_batches = _page_records(client.batches.list(limit=100))
    resolved: dict[tuple[str, str], Any] = {}
    for manifest in manifests:
        reviewer_id = manifest["reviewer_id"]
        manifest_sha256 = manifest["batch_manifest_sha256"]
        for local in inputs:
            shard = local["shard"]
            key = (reviewer_id, shard)
            prior = known.get(key)
            metadata = _batch_metadata(
                reviewer_id, shard, local["sha256"], manifest_sha256
            )
            if prior is not None:
                remote = client.batches.retrieve(prior["batch_id"])
            else:
                remote = _select_unique(
                    remote_batches,
                    lambda item: _submission_key_from_remote(item)
                    == metadata["submission_key"],
                    f"batch for reviewer {reviewer_id} shard {shard}",
                )
            if remote is not None:
                file_record = remote_file_records.get(shard)
                if file_record is None:
                    raise AdjudicationSubmissionError(
                        f"remote batch exists without frozen file for shard {shard}"
                    )
                current = _batch_record(
                    remote,
                    reviewer_id,
                    shard,
                    local["sha256"],
                    manifest_sha256,
                    file_record["openai_file_id"],
                )
                if prior is not None:
                    _validate_immutable_record(
                        prior,
                        current,
                        BATCH_IMMUTABLE_FIELDS,
                        f"batch {reviewer_id}/{shard}",
                    )
                resolved[key] = remote
    return resolved


def _create_batches(
    client: Any,
    manifests: Sequence[Mapping[str, Any]],
    resolved: Mapping[tuple[str, str], Any],
    journal: dict[str, Any],
    journal_path: Path,
) -> None:
    known = {
        (record["reviewer_id"], record["shard"]): record
        for record in journal["batches"]
    }
    files = {record["shard"]: record for record in journal["input_files"]}
    for manifest in manifests:
        reviewer_id = manifest["reviewer_id"]
        manifest_sha256 = manifest["batch_manifest_sha256"]
        for shard, local in sorted(files.items()):
            key = (reviewer_id, shard)
            prior = known.get(key)
            metadata = _batch_metadata(
                reviewer_id, shard, local["sha256"], manifest_sha256
            )
            remote = resolved.get(key)
            if remote is None:
                remote = client.batches.create(
                    input_file_id=local["openai_file_id"],
                    endpoint="/v1/responses",
                    completion_window=COMPLETION_WINDOW,
                    metadata=metadata,
                )
            record = _batch_record(
                remote,
                reviewer_id,
                shard,
                local["sha256"],
                manifest_sha256,
                local["openai_file_id"],
            )
            if prior is None:
                journal["batches"].append(record)
            else:
                journal["batches"] = [
                    record if (item["reviewer_id"], item["shard"]) == key else item
                    for item in journal["batches"]
                ]
            _save(journal_path, journal)


def _submit_adjudication_batches(
    client: Any,
    *,
    manifests: Sequence[Mapping[str, Any]],
    manifest_paths: Sequence[Path],
    expected_manifest_file_sha256s: Sequence[str],
    input_root: Path,
    journal_path: Path,
) -> dict[str, Any]:
    """Upload five frozen inputs once and ensure two reviewer batches per shard."""
    identities, inputs = _validate_local_contract(
        manifests,
        manifest_paths,
        expected_manifest_file_sha256s,
        input_root,
    )
    journal = _load_journal(journal_path) or _new_journal(identities)
    _validate_journal(journal, identities)
    _validate_journal_coverage(journal, inputs, manifests)
    remote_files = _preflight_files(client, inputs, journal)
    preflight_file_records = {
        local["shard"]: _file_record(local, remote_files[local["sha256"]])
        for local in inputs
        if local["sha256"] in remote_files
    }
    _preflight_batches(
        client,
        manifests,
        inputs,
        preflight_file_records,
        journal,
    )
    _create_files(client, inputs, remote_files, journal, journal_path)
    complete_file_records = {
        record["shard"]: record for record in journal["input_files"]
    }
    remote_batches = _preflight_batches(
        client,
        manifests,
        inputs,
        complete_file_records,
        journal,
    )
    _create_batches(client, manifests, remote_batches, journal, journal_path)
    if len(journal["input_files"]) != len(inputs) or len(journal["batches"]) != (
        len(inputs) * len(manifests)
    ):
        raise AdjudicationSubmissionError("submission coverage is incomplete")
    journal["input_files"].sort(key=lambda item: item["shard"])
    journal["batches"].sort(key=lambda item: (item["reviewer_id"], item["shard"]))
    _save(journal_path, journal)
    return journal


def submit_adjudication_batches(
    client: Any,
    *,
    manifests: Sequence[Mapping[str, Any]],
    manifest_paths: Sequence[Path],
    expected_manifest_file_sha256s: Sequence[str],
    input_root: Path,
    journal_path: Path,
) -> dict[str, Any]:
    """Upload five frozen inputs once and ensure two reviewer batches per shard."""
    with _submission_lock(journal_path):
        return _submit_adjudication_batches(
            client,
            manifests=manifests,
            manifest_paths=manifest_paths,
            expected_manifest_file_sha256s=expected_manifest_file_sha256s,
            input_root=input_root,
            journal_path=journal_path,
        )
