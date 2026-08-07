"""Build compact repository/event cases from immutable Sourcegraph packets."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery import validate_discovery_specification
from authorship.sourcegraph_evidence_pipeline import (
    packet_index_sha256,
    validate_packet_index,
)

CASE_VERSION = 3
CASE_FIELDS = frozenset(
    {
        "case_version",
        "case_id",
        "canonical_repository_id",
        "canonical_source_url",
        "sourcegraph_name",
        "cutoff_commit",
        "packet_count",
        "event_count",
        "events",
        "queues",
        "workflow",
        "outcomes_consulted",
        "repository_case_sha256",
    }
)
EVENT_FIELDS = frozenset(
    {
        "event_id",
        "packet_type",
        "candidate_event",
        "query_family_ids",
        "packet_ids",
        "packet_count",
    }
)
QUEUE_FIELDS = frozenset(
    {
        "explicit_provenance_candidates",
        "adoption_anchor_candidates",
        "adoption_challenge_candidates",
        "ai_ban_candidates",
    }
)
WORKFLOW_FIELDS = frozenset(
    {
        "adoption_stop_rule",
        "ai_ban_stop_rule",
        "post_adoption_implies_agent",
        "ambiguous_case_review",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "case_index_version",
        "workflow_sha256",
        "specification_sha256",
        "packet_index_sha256",
        "review_unit",
        "packet_level_exhaustive_review_required",
        "repository_count",
        "event_count",
        "packet_count",
        "repositories",
        "outcomes_consulted",
        "case_index_sha256",
    }
)
REPOSITORY_RECORD_FIELDS = frozenset(
    {
        "case_id",
        "canonical_repository_id",
        "case_file",
        "byte_count",
        "packet_count",
        "event_count",
        "sha256",
    }
)


class RepositoryCaseError(ValueError):
    """Raised when repository cases cannot be built without provenance loss."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def repository_case_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "repository_case_sha256"
    }
    return _content_sha256(content)


def case_index_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "case_index_sha256"
    }
    return _content_sha256(content)


def workflow_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "workflow_sha256"
    }
    return _content_sha256(content)


def _workflow_policy_errors(
    workflow_specification: Mapping[str, Any],
) -> list[str]:
    errors = []
    if workflow_specification.get("workflow_sha256") != workflow_sha256(
        workflow_specification
    ):
        errors.append("workflow specification checksum does not match")
    if workflow_specification.get("outcomes_consulted") is not False:
        errors.append("workflow specification must be outcome blind")
    model_execution = workflow_specification.get("model_execution")
    if not isinstance(model_execution, Mapping):
        errors.append("workflow model execution policy is invalid")
    else:
        model_rules = (
            (
                model_execution.get("paid_batch_api") is False,
                "workflow must forbid paid Batch API execution",
            ),
            (
                model_execution.get("user_openai_api_key") is False,
                "workflow must forbid user OpenAI API keys",
            ),
            (
                model_execution.get("local_or_subagent_review") is True,
                "workflow must require local or subagent review",
            ),
            (
                model_execution.get("semantic_decisions_delegated_to_reviewers")
                is True,
                "workflow must delegate semantic decisions to reviewers",
            ),
        )
        errors.extend(message for valid, message in model_rules if not valid)
    review_design = workflow_specification.get("review_design")
    if not isinstance(review_design, Mapping):
        errors.append("workflow review design is invalid")
    else:
        review_rules = (
            (
                review_design.get("review_unit") == "repository_event",
                "workflow review unit must be repository_event",
            ),
            (
                review_design.get("packet_level_exhaustive_review") is False,
                "workflow must not require exhaustive packet review",
            ),
            (
                review_design.get("post_adoption_implies_agent") is False,
                "workflow must not infer agent authorship from post-adoption timing",
            ),
        )
        errors.extend(message for valid, message in review_rules if not valid)
    return errors


def validate_repository_case_workflow(
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> None:
    """Fail closed unless the workflow matches the locked review policy."""
    discovery_errors = validate_discovery_specification(discovery_specification)
    if discovery_errors:
        raise RepositoryCaseError(
            f"discovery specification is invalid: {'; '.join(discovery_errors)}"
        )
    policy_errors = _workflow_policy_errors(workflow_specification)
    if policy_errors:
        raise RepositoryCaseError("; ".join(policy_errors))
    expected_specification_sha = workflow_specification.get(
        "discovery_specification", {}
    ).get("specification_sha256")
    if expected_specification_sha != discovery_specification.get(
        "specification_sha256"
    ):
        raise RepositoryCaseError("workflow discovery specification does not match")


def _validated_packets(
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    validate_repository_case_workflow(
        discovery_specification,
        workflow_specification,
    )
    if packet_index.get("outcomes_consulted") is not False:
        raise RepositoryCaseError("packet index must be outcome blind")
    index_errors = validate_packet_index(packet_index, discovery_specification)
    if index_errors:
        raise RepositoryCaseError(f"packet index is invalid: {'; '.join(index_errors)}")
    expected_index = workflow_specification.get("packet_index", {})
    if expected_index.get("packet_index_sha256") != packet_index.get(
        "packet_index_sha256"
    ):
        raise RepositoryCaseError("workflow packet index checksum does not match")
    if expected_index.get("packet_count") != packet_index.get("packet_count"):
        raise RepositoryCaseError("workflow packet count does not match")
    if packet_index.get("pending_file_enrichment_count") != 0:
        raise RepositoryCaseError("packet index has pending file enrichments")
    packets = packet_index.get("packets")
    if not isinstance(packets, list) or any(
        not isinstance(packet, Mapping) for packet in packets
    ):
        raise RepositoryCaseError("packet index packets are invalid")
    return packets


def _event_identity(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "packet_type": packet["packet_type"],
        "canonical_repository_id": packet["canonical_repository_id"],
        "commit_oid": packet["candidate_event"]["commit_oid"],
    }


def _new_event(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": _content_sha256(_event_identity(packet)),
        "packet_type": packet["packet_type"],
        "candidate_event": dict(packet["candidate_event"]),
        "query_family_ids": [packet["query_family_id"]],
        "packet_ids": [packet["packet_id"]],
        "packet_count": 1,
    }


def _event_order(event: Mapping[str, Any]) -> tuple[str, str]:
    return event["candidate_event"]["observed_at"], event["event_id"]


def _metadata(packet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "canonical_repository_id": packet["canonical_repository_id"],
        "canonical_source_url": packet["canonical_source_url"],
        "sourcegraph_name": packet["sourcegraph_name"],
        "cutoff_commit": packet["cutoff_commit"],
    }


def _workflow_fields(workflow_specification: Mapping[str, Any]) -> dict[str, Any]:
    review = workflow_specification["review_design"]
    return {
        "adoption_stop_rule": review["adoption_stop_rule"],
        "ai_ban_stop_rule": review["ai_ban_stop_rule"],
        "post_adoption_implies_agent": review["post_adoption_implies_agent"],
        "ambiguous_case_review": review["ambiguous_case_review"],
    }


def _updated_event(
    event: Mapping[str, Any],
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        event["packet_type"] != packet["packet_type"]
        or event["candidate_event"] != packet["candidate_event"]
    ):
        raise RepositoryCaseError("grouped packets contain inconsistent event metadata")
    return {
        **event,
        "query_family_ids": sorted(
            {*event["query_family_ids"], packet["query_family_id"]}
        ),
        "packet_ids": sorted([*event["packet_ids"], packet["packet_id"]]),
        "packet_count": event["packet_count"] + 1,
    }


def _repository_groups(
    packets: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for packet in packets:
        repository = packet["canonical_repository_id"]
        group = groups.get(repository)
        if group is None:
            group = {"metadata": _metadata(packet), "events": {}}
            groups[repository] = group
        elif group["metadata"] != _metadata(packet):
            raise RepositoryCaseError("repository events contain inconsistent metadata")
        event_id = _content_sha256(_event_identity(packet))
        event = group["events"].get(event_id)
        next_event = (
            _new_event(packet) if event is None else _updated_event(event, packet)
        )
        group["events"][event_id] = next_event
    return groups


def _repository_case(
    group: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = group["metadata"]
    events = sorted(group["events"].values(), key=_event_order)
    review = workflow_specification["review_design"]
    explicit_families = set(review["explicit_provenance_families"])
    challenge_families = set(review["adoption_challenge_families"])
    ban_families = set(review["ai_ban_families"])
    explicit = [
        event
        for event in events
        if explicit_families.intersection(event["query_family_ids"])
    ]
    challenges = [
        event
        for event in events
        if event["packet_type"] == "adoption_event"
        and challenge_families.intersection(event["query_family_ids"])
        and event not in explicit
    ]
    bans = [
        event
        for event in events
        if event["packet_type"] == "ai_ban_policy"
        and ban_families.intersection(event["query_family_ids"])
    ]
    case_id = _content_sha256(metadata["canonical_repository_id"])
    document = {
        "case_version": CASE_VERSION,
        "case_id": case_id,
        **metadata,
        "packet_count": sum(event["packet_count"] for event in events),
        "event_count": len(events),
        "events": events,
        "queues": {
            "explicit_provenance_candidates": explicit,
            "adoption_anchor_candidates": explicit,
            "adoption_challenge_candidates": challenges,
            "ai_ban_candidates": bans,
        },
        "workflow": _workflow_fields(workflow_specification),
        "outcomes_consulted": False,
    }
    return {**document, "repository_case_sha256": repository_case_sha256(document)}


def _write_case(case: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
    case_id = case["case_id"]
    relative_path = Path("cases") / case_id[:2] / f"{case_id}.json"
    destination = output_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{_canonical_json(case)}\n".encode()
    _atomic_write(destination, payload)
    return {
        "case_id": case_id,
        "canonical_repository_id": case["canonical_repository_id"],
        "case_file": relative_path.as_posix(),
        "byte_count": len(payload),
        "packet_count": case["packet_count"],
        "event_count": case["event_count"],
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_repository_case_index_from_validated_packets(
    packets: Iterable[Mapping[str, Any]],
    workflow_specification: Mapping[str, Any],
    output_root: Path,
    *,
    specification_sha256: str,
    packet_index_sha256_value: str,
) -> dict[str, Any]:
    """Build cases from a packet stream validated by the caller."""
    repository_groups = _repository_groups(packets)
    cases = [
        _repository_case(repository_groups[repository], workflow_specification)
        for repository in sorted(repository_groups)
    ]
    repository_records = [_write_case(case, output_root) for case in cases]
    document = {
        "case_index_version": CASE_VERSION,
        "workflow_sha256": workflow_specification["workflow_sha256"],
        "specification_sha256": specification_sha256,
        "packet_index_sha256": packet_index_sha256_value,
        "review_unit": "repository_event",
        "packet_level_exhaustive_review_required": False,
        "repository_count": len(repository_records),
        "event_count": sum(record["event_count"] for record in repository_records),
        "packet_count": sum(record["packet_count"] for record in repository_records),
        "repositories": repository_records,
        "outcomes_consulted": False,
    }
    return {**document, "case_index_sha256": case_index_sha256(document)}


def build_repository_case_index(
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    """Build deterministic repository cases while preserving packet coverage."""
    packets = _validated_packets(
        discovery_specification,
        workflow_specification,
        packet_index,
    )
    return build_repository_case_index_from_validated_packets(
        packets,
        workflow_specification,
        output_root,
        specification_sha256=discovery_specification["specification_sha256"],
        packet_index_sha256_value=packet_index["packet_index_sha256"],
    )


def _event_errors(event: Any) -> list[str]:
    if not isinstance(event, Mapping) or set(event) != EVENT_FIELDS:
        return ["repository case contract is invalid"]
    if event.get("packet_count") != len(event.get("packet_ids", [])):
        return ["repository case contract is invalid"]
    return []


def _case_contract_errors(case: Any) -> list[str]:
    if not isinstance(case, Mapping) or set(case) != CASE_FIELDS:
        return ["repository case contract is invalid"]
    queues = case.get("queues")
    workflow = case.get("workflow")
    if not isinstance(queues, Mapping) or set(queues) != QUEUE_FIELDS:
        return ["repository case contract is invalid"]
    if not isinstance(workflow, Mapping) or set(workflow) != WORKFLOW_FIELDS:
        return ["repository case contract is invalid"]
    if (
        workflow.get("post_adoption_implies_agent") is not False
        or case.get("outcomes_consulted") is not False
    ):
        return ["repository case contract is invalid"]
    events = case.get("events")
    if not isinstance(events, list) or any(_event_errors(event) for event in events):
        return ["repository case contract is invalid"]
    if case.get("event_count") != len(events):
        return ["repository case contract is invalid"]
    if case.get("packet_count") != sum(
        event.get("packet_count", 0) for event in events
    ):
        return ["repository case contract is invalid"]
    if case.get("repository_case_sha256") != repository_case_sha256(case):
        return ["repository case contract is invalid"]
    return []


def _read_case_record(
    record: Any,
    output_root: Path,
) -> tuple[list[str], Mapping[str, Any] | None]:
    if not isinstance(record, Mapping) or set(record) != REPOSITORY_RECORD_FIELDS:
        return ["case index repository record is invalid"], None
    path = output_root / record["case_file"]
    if not path.is_file():
        return ["repository case file is missing"], None
    payload = path.read_bytes()
    errors = []
    if len(payload) != record["byte_count"]:
        errors.append("repository case byte count does not match")
    if hashlib.sha256(payload).hexdigest() != record["sha256"]:
        errors.append("repository case checksum does not match")
    try:
        case = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [*errors, "repository case contract is invalid"], None
    errors.extend(_case_contract_errors(case))
    if (
        case.get("case_id") != record["case_id"]
        or case.get("canonical_repository_id") != record["canonical_repository_id"]
        or case.get("packet_count") != record["packet_count"]
        or case.get("event_count") != record["event_count"]
    ):
        errors.append("repository case index binding does not match")
    return errors, case


def _aggregate_errors(
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors = []
    if manifest.get("repository_count") != len(records):
        errors.append("repository count does not match")
    for field in ("event_count", "packet_count"):
        observed = sum(record.get(field, 0) for record in records)
        if manifest.get(field) != observed:
            errors.append(f"{field.replace('_', ' ')} does not match")
    return errors


def _packet_coverage_errors(
    manifest: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    case_packet_ids: Sequence[str],
) -> list[str]:
    errors = []
    if packet_index.get("packet_index_sha256") != packet_index_sha256(packet_index):
        errors.append("frozen packet index checksum does not match")
    if manifest.get("packet_index_sha256") != packet_index.get("packet_index_sha256"):
        errors.append("case index packet binding does not match")
    packets = packet_index.get("packets")
    if not isinstance(packets, list):
        return [*errors, "frozen packet index packets are invalid"]
    expected_packet_ids = [
        packet.get("packet_id") for packet in packets if isinstance(packet, Mapping)
    ]
    if len(case_packet_ids) != len(set(case_packet_ids)) or sorted(
        case_packet_ids
    ) != sorted(expected_packet_ids):
        errors.append("case packet IDs do not equal frozen packet index")
    return errors


def _frozen_case_errors(
    cases: Sequence[Mapping[str, Any]],
    packet_index: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> list[str]:
    packets = packet_index.get("packets")
    if not isinstance(packets, list) or any(
        not isinstance(packet, Mapping) for packet in packets
    ):
        return ["frozen packet index packets are invalid"]
    groups = _repository_groups(packets)
    expected = {
        repository: _repository_case(group, workflow_specification)
        for repository, group in groups.items()
    }
    observed = {case.get("canonical_repository_id"): case for case in cases}
    if observed != expected:
        return ["repository case differs from frozen packet evidence"]
    return []


def _workflow_contract_errors(
    manifest: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> list[str]:
    errors = _workflow_policy_errors(workflow_specification)
    if manifest.get("workflow_sha256") != workflow_specification.get("workflow_sha256"):
        errors.append("case index workflow binding does not match")
    return errors


def validate_repository_case_index(
    manifest: Mapping[str, Any],
    output_root: Path,
    packet_index: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> list[str]:
    """Return fail-closed validation errors for an index and its case files."""
    errors = _workflow_contract_errors(manifest, workflow_specification)
    if set(manifest) != MANIFEST_FIELDS:
        errors.append("case index contract is invalid")
    if manifest.get("case_index_sha256") != case_index_sha256(manifest):
        errors.append("case index checksum does not match")
    records = manifest.get("repositories")
    if not isinstance(records, list):
        return [*errors, "case index repositories are invalid"]
    cases: list[Mapping[str, Any]] = []
    for record in records:
        record_errors, case = _read_case_record(record, output_root)
        errors.extend(record_errors)
        if case is not None:
            cases.append(case)
    valid_records = [record for record in records if isinstance(record, Mapping)]
    errors.extend(_aggregate_errors(manifest, valid_records))
    packet_ids = [
        packet_id
        for case in cases
        for event in case.get("events", [])
        for packet_id in event.get("packet_ids", [])
    ]
    errors.extend(_packet_coverage_errors(manifest, packet_index, packet_ids))
    errors.extend(
        _frozen_case_errors(
            cases,
            packet_index,
            workflow_specification,
        )
    )
    return sorted(set(errors))


def write_case_index(path: Path, manifest: Mapping[str, Any]) -> None:
    """Atomically write a validated case-index manifest."""
    _atomic_write(path, f"{_canonical_json(manifest)}\n".encode())
