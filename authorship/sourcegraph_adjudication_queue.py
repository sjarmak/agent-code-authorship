"""Build an outcome-blind, event-grouped Sourcegraph adjudication queue."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery import validate_discovery_specification
from authorship.sourcegraph_evidence_pipeline import validate_packet_index

WORK_QUEUE_VERSION = 3
DEFAULT_SHARD_COUNT = 256
MAX_SHARD_COUNT = 4_096
TASK_FIELDS = frozenset(
    {
        "task_version",
        "task_id",
        "packet_type",
        "canonical_repository_id",
        "canonical_source_url",
        "sourcegraph_name",
        "cutoff_commit",
        "candidate_event",
        "packet_count",
        "observations",
        "outcomes_consulted",
        "task_sha256",
    }
)
OBSERVATION_FIELDS = frozenset(
    {
        "packet_id",
        "packet_sha256",
        "query_family_id",
        "rendered_query",
        "rendered_query_sha256",
        "sourcegraph_result_ids",
        "raw_evidence",
    }
)
RAW_EVIDENCE_FIELDS = frozenset(
    {
        "kind",
        "commit_oid",
        "path",
        "line",
        "value",
        "source_url",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "work_queue_version",
        "specification_sha256",
        "packet_index_sha256",
        "packet_count",
        "task_count",
        "configured_shard_count",
        "emitted_shard_count",
        "shards",
        "outcomes_consulted",
        "queue_manifest_sha256",
    }
)
SHARD_FIELDS = frozenset(
    {
        "shard_id",
        "shard_file",
        "task_count",
        "packet_count",
        "first_task_id",
        "last_task_id",
        "sha256",
    }
)


class AdjudicationQueueError(ValueError):
    """Raised when frozen packets cannot form a blinded review queue."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _is_hex(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def task_sha256(document: Mapping[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "task_sha256"}
    return _content_sha256(content)


def queue_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "queue_manifest_sha256"
    }
    return _content_sha256(content)


def _validated_packets(
    specification: Mapping[str, Any], packet_index: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise AdjudicationQueueError(
            f"discovery specification is invalid: {'; '.join(specification_errors)}"
        )
    if packet_index.get("outcomes_consulted") is not False:
        raise AdjudicationQueueError("packet index must be outcome blind")
    index_errors = validate_packet_index(packet_index, specification)
    if index_errors:
        raise AdjudicationQueueError(
            f"packet index is invalid: {'; '.join(index_errors)}"
        )
    if packet_index.get("pending_file_enrichment_count") != 0:
        raise AdjudicationQueueError("packet index has pending file enrichments")
    packets = packet_index.get("packets")
    if not isinstance(packets, list) or any(
        not isinstance(packet, Mapping) for packet in packets
    ):
        raise AdjudicationQueueError("packet index packets are invalid")
    return packets


def _event_identity(packet: Mapping[str, Any]) -> dict[str, Any]:
    candidate_event = packet.get("candidate_event")
    if not isinstance(candidate_event, Mapping):
        raise AdjudicationQueueError("packet candidate_event is invalid")
    commit_oid = candidate_event.get("commit_oid")
    if not isinstance(commit_oid, str):
        raise AdjudicationQueueError("packet event commit is invalid")
    return {
        "packet_type": packet.get("packet_type"),
        "canonical_repository_id": packet.get("canonical_repository_id"),
        "commit_oid": commit_oid,
    }


def _group_metadata(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "packet_type": packet.get("packet_type"),
        "canonical_repository_id": packet.get("canonical_repository_id"),
        "canonical_source_url": packet.get("canonical_source_url"),
        "sourcegraph_name": packet.get("sourcegraph_name"),
        "cutoff_commit": packet.get("cutoff_commit"),
        "candidate_event": dict(packet.get("candidate_event", {})),
    }


def _observation(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"],
        "query_family_id": packet["query_family_id"],
        "rendered_query": packet["rendered_query"],
        "rendered_query_sha256": packet["rendered_query_sha256"],
        "sourcegraph_result_ids": list(packet["sourcegraph_result_ids"]),
        "raw_evidence": [dict(record) for record in packet["raw_evidence"]],
    }


def _task(packets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(packets, key=lambda packet: packet["packet_id"])
    metadata = _group_metadata(ordered[0])
    if any(_group_metadata(packet) != metadata for packet in ordered[1:]):
        raise AdjudicationQueueError(
            "grouped packets contain inconsistent event metadata"
        )
    identity = _event_identity(ordered[0])
    task_id = _content_sha256(identity)
    document = {
        "task_version": WORK_QUEUE_VERSION,
        "task_id": task_id,
        **metadata,
        "packet_count": len(ordered),
        "observations": [_observation(packet) for packet in ordered],
        "outcomes_consulted": False,
    }
    return {**document, "task_sha256": task_sha256(document)}


def _tasks(packets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for packet in packets:
        grouped[_content_sha256(_event_identity(packet))].append(packet)
    return sorted(
        (_task(group) for group in grouped.values()),
        key=lambda task: task["task_id"],
    )


def _validated_shard_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_SHARD_COUNT
    ):
        raise AdjudicationQueueError(
            f"shard_count must be between 1 and {MAX_SHARD_COUNT}"
        )
    return value


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _shard_payload(tasks: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(f"{_canonical_json(task)}\n" for task in tasks).encode()


def _write_shards(
    tasks: Sequence[Mapping[str, Any]], output_root: Path, shard_count: int
) -> list[dict[str, Any]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for task in tasks:
        grouped[int(task["task_id"][:8], 16) % shard_count].append(task)
    width = max(4, len(str(shard_count - 1)))
    records = []
    for shard_number in sorted(grouped):
        shard_tasks = sorted(grouped[shard_number], key=lambda task: task["task_id"])
        shard_id = f"{shard_number:0{width}d}"
        shard_file = f"shards/{shard_id}.jsonl"
        payload = _shard_payload(shard_tasks)
        _atomic_bytes(output_root / shard_file, payload)
        records.append(
            {
                "shard_id": shard_id,
                "shard_file": shard_file,
                "task_count": len(shard_tasks),
                "packet_count": sum(task["packet_count"] for task in shard_tasks),
                "first_task_id": shard_tasks[0]["task_id"],
                "last_task_id": shard_tasks[-1]["task_id"],
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return records


def build_adjudication_work_queue(
    specification: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    output_root: Path,
    *,
    shard_count: int = DEFAULT_SHARD_COUNT,
) -> dict[str, Any]:
    """Group frozen packets into review events and write deterministic shards."""
    selected_shard_count = _validated_shard_count(shard_count)
    packets = _validated_packets(specification, packet_index)
    tasks = _tasks(packets)
    shards = _write_shards(tasks, output_root, selected_shard_count)
    document = {
        "work_queue_version": WORK_QUEUE_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "packet_index_sha256": packet_index["packet_index_sha256"],
        "packet_count": len(packets),
        "task_count": len(tasks),
        "configured_shard_count": selected_shard_count,
        "emitted_shard_count": len(shards),
        "shards": shards,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "queue_manifest_sha256": queue_manifest_sha256(document),
    }


def _task_errors(task: Mapping[str, Any]) -> list[str]:
    observations = task.get("observations")
    if not isinstance(observations, list) or any(
        not isinstance(observation, Mapping) for observation in observations
    ):
        return ["task contract is invalid"]
    packet_ids = [observation.get("packet_id") for observation in observations]
    observations_valid = all(
        set(observation) == OBSERVATION_FIELDS
        and _is_hex(observation.get("packet_id"), 64)
        and _is_hex(observation.get("packet_sha256"), 64)
        and isinstance(observation.get("query_family_id"), str)
        and isinstance(observation.get("rendered_query"), str)
        and _is_hex(observation.get("rendered_query_sha256"), 64)
        and hashlib.sha256(observation["rendered_query"].encode()).hexdigest()
        == observation.get("rendered_query_sha256")
        and isinstance(observation.get("sourcegraph_result_ids"), list)
        and len(observation["sourcegraph_result_ids"])
        == len(set(observation["sourcegraph_result_ids"]))
        and all(
            isinstance(result_id, str)
            for result_id in observation["sourcegraph_result_ids"]
        )
        and isinstance(observation.get("raw_evidence"), list)
        and all(
            isinstance(evidence, Mapping) and set(evidence) == RAW_EVIDENCE_FIELDS
            for evidence in observation["raw_evidence"]
        )
        for observation in observations
    )
    try:
        expected_task_id = _content_sha256(_event_identity(task))
    except AdjudicationQueueError:
        return ["task contract is invalid"]
    candidate_event = task.get("candidate_event")
    valid = (
        set(task) == TASK_FIELDS,
        task.get("task_version") == WORK_QUEUE_VERSION,
        task.get("task_id") == expected_task_id,
        task.get("packet_type") in {"adoption_event", "ai_ban_policy"},
        isinstance(task.get("canonical_repository_id"), str),
        isinstance(task.get("canonical_source_url"), str),
        isinstance(task.get("sourcegraph_name"), str),
        _is_hex(task.get("cutoff_commit"), 40),
        isinstance(candidate_event, Mapping),
        set(candidate_event or {}) == {"commit_oid", "observed_at"},
        _is_hex((candidate_event or {}).get("commit_oid"), 40),
        isinstance((candidate_event or {}).get("observed_at"), str),
        task.get("packet_count") == len(observations) > 0,
        task.get("outcomes_consulted") is False,
        task.get("task_sha256") == task_sha256(task),
        len(packet_ids) == len(set(packet_ids)),
        observations_valid,
    )
    return [] if all(valid) else ["task contract is invalid"]


def _shard_path(output_root: Path, value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "shards"
        or ".." in relative.parts
    ):
        return None
    return output_root / relative


def _load_shard(
    output_root: Path, record: Mapping[str, Any]
) -> tuple[list[Mapping[str, Any]], list[str]]:
    path = _shard_path(output_root, record.get("shard_file"))
    if path is None:
        return [], ["shard path is invalid"]
    try:
        payload = path.read_bytes()
    except OSError:
        return [], ["shard cannot be read"]
    errors = []
    if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
        errors.append("shard checksum does not match")
    tasks = []
    for raw_line in payload.splitlines():
        try:
            task = json.loads(raw_line)
        except json.JSONDecodeError:
            errors.append("task JSON is invalid")
            continue
        if not isinstance(task, Mapping):
            errors.append("task contract is invalid")
            continue
        task_errors = _task_errors(task)
        errors.extend(task_errors)
        if not task_errors:
            tasks.append(task)
    return tasks, errors


def _shard_errors(
    record: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    configured_shard_count: Any,
) -> list[str]:
    if not tasks:
        return ["shard contract is invalid"]
    task_ids = [task.get("task_id") for task in tasks]
    packet_count = sum(
        len(task.get("observations", []))
        for task in tasks
        if isinstance(task.get("observations"), list)
    )
    shard_id = record.get("shard_id")
    try:
        shard_number = int(shard_id)
    except (TypeError, ValueError):
        return ["shard contract is invalid"]
    if (
        isinstance(configured_shard_count, bool)
        or not isinstance(configured_shard_count, int)
        or configured_shard_count < 1
        or any(not isinstance(task_id, str) for task_id in task_ids)
    ):
        return ["shard contract is invalid"]
    valid = (
        set(record) == SHARD_FIELDS,
        task_ids == sorted(task_ids),
        record.get("task_count") == len(tasks),
        record.get("packet_count") == packet_count,
        record.get("first_task_id") == task_ids[0],
        record.get("last_task_id") == task_ids[-1],
        all(
            int(task_id[:8], 16) % configured_shard_count == shard_number
            for task_id in task_ids
        ),
    )
    return [] if all(valid) else ["shard contract is invalid"]


def validate_adjudication_work_queue(
    manifest: Mapping[str, Any], output_root: Path
) -> list[str]:
    """Stream all queue shards and report integrity or coverage failures."""
    shards = manifest.get("shards")
    configured = manifest.get("configured_shard_count")
    errors = []
    if (
        set(manifest) != MANIFEST_FIELDS
        or not _is_hex(manifest.get("specification_sha256"), 64)
        or not _is_hex(manifest.get("packet_index_sha256"), 64)
        or manifest.get("queue_manifest_sha256") != queue_manifest_sha256(manifest)
        or manifest.get("work_queue_version") != WORK_QUEUE_VERSION
        or manifest.get("outcomes_consulted") is not False
        or not isinstance(shards, list)
        or any(not isinstance(shard, Mapping) for shard in shards)
        or manifest.get("emitted_shard_count") != len(shards or [])
        or not isinstance(configured, int)
        or not 1 <= configured <= MAX_SHARD_COUNT
    ):
        errors.append("queue manifest contract is invalid")
    if not isinstance(shards, list):
        return errors
    tasks = []
    for record in shards:
        if not isinstance(record, Mapping):
            continue
        shard_tasks, shard_load_errors = _load_shard(output_root, record)
        errors.extend(shard_load_errors)
        errors.extend(_shard_errors(record, shard_tasks, configured))
        tasks.extend(shard_tasks)
    task_ids = [task.get("task_id") for task in tasks]
    packet_ids = [
        observation.get("packet_id")
        for task in tasks
        for observation in task.get("observations", [])
        if isinstance(observation, Mapping)
    ]
    if (
        manifest.get("task_count") != len(tasks)
        or len(task_ids) != len(set(task_ids))
        or manifest.get("packet_count") != len(packet_ids)
        or len(packet_ids) != len(set(packet_ids))
        or sum(record.get("task_count", 0) for record in shards) != len(tasks)
        or sum(record.get("packet_count", 0) for record in shards) != len(packet_ids)
    ):
        errors.append("queue coverage does not match manifest")
    return sorted(set(errors))


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Write a canonical JSON document atomically."""
    _atomic_bytes(path, f"{_canonical_json(document)}\n".encode())
