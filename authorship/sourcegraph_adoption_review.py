"""Build outcome-blind, sequential repository-adoption review tranches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_review_contracts import (
    DECISIONS,
    LEDGER_FIELDS,
    RESPONSE_FIELDS,
    REVIEW_ROLES,
    REVIEW_VERSION,
    AdoptionReviewError,
    _decision_index,
    adoption_review_ledger_sha256,
    adoption_review_response_sha256,
)
from authorship.sourcegraph_adoption_response_validation import (
    response_errors as _response_errors,
)
from authorship.sourcegraph_repository_case_stream import (
    validated_packet_stream_from_file,
)
from authorship.sourcegraph_repository_cases import (
    case_index_sha256,
    repository_case_sha256,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def adoption_review_task_sha256(document: Mapping[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "task_sha256"}
    return _content_sha256(content)


def adoption_review_tranche_sha256(document: Mapping[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "tranche_sha256"}
    return _content_sha256(content)


def adoption_review_protocol_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "protocol_sha256"
    }
    return _content_sha256(content)


def _validate_review_protocol(
    protocol: Mapping[str, Any],
    case_index: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> None:
    if protocol.get("protocol_sha256") != adoption_review_protocol_sha256(protocol):
        raise AdoptionReviewError("adoption review protocol checksum does not match")
    if protocol.get("outcomes_consulted") is not False:
        raise AdoptionReviewError("adoption review protocol is outcome exposed")
    if protocol.get("case_index", {}).get("case_index_sha256") != case_index.get(
        "case_index_sha256"
    ):
        raise AdoptionReviewError("frozen case index binding does not match")
    if protocol.get("workflow", {}).get(
        "workflow_sha256"
    ) != workflow_specification.get("workflow_sha256"):
        raise AdoptionReviewError("adoption review workflow binding does not match")
    if protocol.get("packet_index", {}).get("packet_index_sha256") != case_index.get(
        "packet_index_sha256"
    ):
        raise AdoptionReviewError("adoption review packet binding does not match")
    decision_chain = protocol.get("decision_chain")
    required_chain = {
        "responses_bind_tranche_id_and_number": True,
        "later_events_require_a_higher_tranche_number": True,
        "peer_roles_require_distinct_reviewers": True,
        "prior_ledger_checksum_required": True,
        "naked_decision_arrays_forbidden": True,
    }
    if decision_chain != required_chain:
        raise AdoptionReviewError("adoption review decision-chain policy is invalid")
    execution = protocol.get("review_execution")
    required_execution = {
        "local_or_subagent_only": True,
        "paid_batch_api": False,
        "user_openai_api_key": False,
        "semantic_auto_labeling": False,
    }
    if execution != required_execution:
        raise AdoptionReviewError("adoption review execution policy is invalid")


def _event_order(event: Mapping[str, Any]) -> tuple[str, str]:
    return event["candidate_event"]["observed_at"], event["event_id"]


def _candidate_sequence(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    queues = case.get("queues")
    if not isinstance(queues, Mapping):
        raise AdoptionReviewError("repository case queues are invalid")
    anchors = queues.get("adoption_anchor_candidates")
    challenges = queues.get("adoption_challenge_candidates")
    if not isinstance(anchors, list) or not isinstance(challenges, list):
        raise AdoptionReviewError("repository adoption queues are invalid")
    if not anchors:
        return []
    anchor = min(anchors, key=_event_order)
    anchor_time = anchor["candidate_event"]["observed_at"]
    candidates = [
        {
            **event,
            "candidate_kind": "adoption_challenge",
        }
        for event in challenges
        if event["candidate_event"]["observed_at"] <= anchor_time
    ]
    candidates.append(
        {
            **anchor,
            "candidate_kind": "explicit_provenance_anchor",
        }
    )
    return sorted(candidates, key=_event_order)


def _review_state(
    repository: str,
    event_id: str,
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> str:
    primary = decisions.get((repository, event_id, "primary"))
    secondary = decisions.get((repository, event_id, "secondary"))
    resolver = decisions.get((repository, event_id, "resolver"))
    if primary is None:
        if secondary is not None or resolver is not None:
            raise AdoptionReviewError("review roles are out of order")
        return "primary"
    primary_value = primary["decision"]
    if primary_value != "ambiguous":
        if secondary is not None or resolver is not None:
            raise AdoptionReviewError("review roles are out of order")
        return primary_value
    if secondary is None:
        if resolver is not None:
            raise AdoptionReviewError("review roles are out of order")
        return "secondary"
    if resolver is None:
        return "resolver"
    return resolver["decision"]


def _evidence(
    event: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    evidence = []
    for packet_id in event["packet_ids"]:
        packet = packets.get(packet_id)
        if packet is None:
            raise AdoptionReviewError(f"missing evidence packet {packet_id}")
        if packet.get("packet_id") != packet_id:
            raise AdoptionReviewError("evidence packet binding does not match")
        if packet.get("outcomes_consulted") is not False:
            raise AdoptionReviewError("evidence packet is outcome exposed")
        evidence.append(
            {
                "packet_id": packet_id,
                "packet_sha256": packet.get("packet_sha256"),
                "query_family_id": packet.get("query_family_id"),
                "raw_evidence": deepcopy(packet.get("raw_evidence")),
            }
        )
    return evidence


def _task(
    case_index: Mapping[str, Any],
    case: Mapping[str, Any],
    event: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    *,
    candidate_position: int,
    candidate_count: int,
    review_role: str,
    tranche_number: int,
) -> dict[str, Any]:
    identity = {
        "case_index_sha256": case_index["case_index_sha256"],
        "case_id": case["case_id"],
        "event_id": event["event_id"],
        "review_role": review_role,
        "tranche_number": tranche_number,
    }
    document = {
        "review_version": REVIEW_VERSION,
        "task_id": _content_sha256(identity),
        "case_index_sha256": case_index["case_index_sha256"],
        "workflow_sha256": case_index["workflow_sha256"],
        "case_id": case["case_id"],
        "repository_case_sha256": case["repository_case_sha256"],
        "canonical_repository_id": case["canonical_repository_id"],
        "canonical_source_url": case["canonical_source_url"],
        "sourcegraph_name": case["sourcegraph_name"],
        "cutoff_commit": case["cutoff_commit"],
        "event_id": event["event_id"],
        "candidate_event": deepcopy(event["candidate_event"]),
        "candidate_kind": event["candidate_kind"],
        "candidate_position": candidate_position,
        "candidate_count": candidate_count,
        "query_family_ids": deepcopy(event["query_family_ids"]),
        "packet_ids": deepcopy(event["packet_ids"]),
        "evidence": _evidence(event, packets),
        "review_role": review_role,
        "review_question": (
            "Does this event establish credible repository adoption of an AI "
            "coding agent at the observed date?"
        ),
        "allowed_decisions": sorted(DECISIONS),
        "outcomes_consulted": False,
    }
    return {**document, "task_sha256": adoption_review_task_sha256(document)}


def _validate_decision_scope(
    sequences: Mapping[str, Sequence[Mapping[str, Any]]],
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> None:
    valid_events = {
        (repository, event["event_id"])
        for repository, sequence in sequences.items()
        for event in sequence
    }
    if any(
        (repository, event_id) not in valid_events
        for repository, event_id, _ in decisions
    ):
        raise AdoptionReviewError(
            "review decision is outside the frozen candidate frame"
        )


def _event_review_roles(
    repository: str,
    event_id: str,
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> dict[str, Mapping[str, Any] | None]:
    return {role: decisions.get((repository, event_id, role)) for role in REVIEW_ROLES}


def _later_event_has_decisions(
    repository: str,
    later_events: Sequence[Mapping[str, Any]],
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> bool:
    return any(
        (repository, event["event_id"], role) in decisions
        for event in later_events
        for role in REVIEW_ROLES
    )


def _ambiguous_final_decision(
    roles: Mapping[str, Mapping[str, Any] | None],
    primary_tranche: int,
    later_has_decisions: bool,
) -> Mapping[str, Any] | None:
    secondary = roles["secondary"]
    if secondary is None:
        if roles["resolver"] is not None or later_has_decisions:
            raise AdoptionReviewError("review decision progression is invalid")
        return None
    if secondary["tranche_number"] <= primary_tranche:
        raise AdoptionReviewError("peer review requires a higher tranche number")
    resolver = roles["resolver"]
    if resolver is None:
        if later_has_decisions:
            raise AdoptionReviewError("review decision progression is invalid")
        return None
    if resolver["tranche_number"] <= secondary["tranche_number"]:
        raise AdoptionReviewError("resolution requires a higher tranche number")
    return resolver


def _validated_event_progression(
    roles: Mapping[str, Mapping[str, Any] | None],
    minimum_tranche: int,
    later_has_decisions: bool,
) -> tuple[int, bool]:
    primary = roles["primary"]
    if primary is None:
        if (
            roles["secondary"] is not None
            or roles["resolver"] is not None
            or later_has_decisions
        ):
            raise AdoptionReviewError("review decision progression skips an event")
        return minimum_tranche, True
    primary_tranche = primary["tranche_number"]
    if primary_tranche < minimum_tranche:
        raise AdoptionReviewError("later events require a higher tranche number")
    final = primary
    if primary["decision"] == "ambiguous":
        final = _ambiguous_final_decision(
            roles,
            primary_tranche,
            later_has_decisions,
        )
        if final is None:
            return minimum_tranche, True
    elif roles["secondary"] is not None or roles["resolver"] is not None:
        raise AdoptionReviewError("review decision progression is invalid")
    if final["decision"] in {"accept_confirmed", "accept_observed"}:
        if later_has_decisions:
            raise AdoptionReviewError(
                "review decision progression continues after acceptance"
            )
        return minimum_tranche, True
    return final["tranche_number"] + 1, False


def _validate_decision_progression(
    sequences: Mapping[str, Sequence[Mapping[str, Any]]],
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> None:
    for repository, sequence in sequences.items():
        minimum_tranche = 1
        for position, event in enumerate(sequence):
            roles = _event_review_roles(repository, event["event_id"], decisions)
            later_has_decisions = _later_event_has_decisions(
                repository,
                sequence[position + 1 :],
                decisions,
            )
            minimum_tranche, stop = _validated_event_progression(
                roles,
                minimum_tranche,
                later_has_decisions,
            )
            if stop:
                break


def _active_event_states(
    cases: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> tuple[
    list[tuple[Mapping[str, Any], Mapping[str, Any], int, int, str]], int, int, int
]:
    decision_index = _decision_index(decisions)
    sequences = {
        case["canonical_repository_id"]: _candidate_sequence(case) for case in cases
    }
    if len(sequences) != len(cases):
        raise AdoptionReviewError("repository cases must be unique")
    _validate_decision_scope(sequences, decision_index)
    _validate_decision_progression(sequences, decision_index)
    active = []
    completed = 0
    exhausted = 0
    eligible = 0
    for case in sorted(cases, key=lambda item: item["canonical_repository_id"]):
        sequence = sequences[case["canonical_repository_id"]]
        if not sequence:
            continue
        eligible += 1
        repository = case["canonical_repository_id"]
        for position, event in enumerate(sequence, start=1):
            state = _review_state(repository, event["event_id"], decision_index)
            if state in {"accept_confirmed", "accept_observed"}:
                completed += 1
                break
            if state in {"reject", "insufficient"}:
                continue
            active.append((case, event, position, len(sequence), state))
            break
        else:
            exhausted += 1
    return active, eligible, completed, exhausted


def required_evidence_packet_ids(
    cases: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return packet IDs needed for the next earliest-unresolved tranche."""
    active, _, _, _ = _active_event_states(cases, decisions)
    return {
        packet_id for _, event, _, _, _ in active for packet_id in event["packet_ids"]
    }


def _load_repository_cases(
    case_index: Mapping[str, Any],
    case_root: Path,
) -> list[Mapping[str, Any]]:
    if case_index.get("case_index_sha256") != case_index_sha256(case_index):
        raise AdoptionReviewError("case index checksum does not match")
    records = case_index.get("repositories")
    if not isinstance(records, list):
        raise AdoptionReviewError("case index repositories are invalid")
    root = case_root.resolve()
    cases = []
    for record in records:
        if not isinstance(record, Mapping):
            raise AdoptionReviewError("case index repository record is invalid")
        path = (case_root / str(record.get("case_file", ""))).resolve()
        if not path.is_relative_to(root):
            raise AdoptionReviewError("repository case path escapes case root")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise AdoptionReviewError(
                f"cannot read repository case: {error}"
            ) from error
        if len(payload) != record.get("byte_count"):
            raise AdoptionReviewError("repository case byte count does not match")
        if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
            raise AdoptionReviewError("repository case checksum does not match")
        try:
            case = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdoptionReviewError("repository case JSON is invalid") from error
        if not isinstance(case, Mapping):
            raise AdoptionReviewError("repository case contract is invalid")
        if case.get("repository_case_sha256") != repository_case_sha256(case):
            raise AdoptionReviewError("repository case content checksum does not match")
        if case.get("case_id") != record.get("case_id") or case.get(
            "canonical_repository_id"
        ) != record.get("canonical_repository_id"):
            raise AdoptionReviewError("repository case index binding does not match")
        cases.append(case)
    return cases


def _selected_packets(
    packet_stream: Sequence[Mapping[str, Any]] | Any,
    required_ids: set[str],
) -> dict[str, Mapping[str, Any]]:
    selected = {}
    for packet in packet_stream:
        packet_id = packet.get("packet_id")
        if packet_id in required_ids:
            if packet_id in selected:
                raise AdoptionReviewError("selected packet ID is duplicated")
            selected[packet_id] = packet
    missing = required_ids - set(selected)
    if missing:
        raise AdoptionReviewError(f"missing evidence packet {sorted(missing)[0]}")
    return selected


def compile_adoption_review_responses(
    tranche: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Validate complete reviewer output and preserve it as the decision ledger."""
    if tranche.get("tranche_sha256") != adoption_review_tranche_sha256(tranche):
        raise AdoptionReviewError("review tranche checksum does not match")
    tasks = tranche.get("tasks")
    if not isinstance(tasks, list):
        raise AdoptionReviewError("review tranche tasks are invalid")
    tasks_by_id = {task.get("task_id"): task for task in tasks}
    if len(tasks_by_id) != len(tasks):
        raise AdoptionReviewError("review tranche task IDs are duplicated")
    responses_by_id = {response.get("task_id"): response for response in responses}
    if len(responses_by_id) != len(responses):
        raise AdoptionReviewError("review response task IDs are duplicated")
    if set(responses_by_id) != set(tasks_by_id):
        raise AdoptionReviewError("review responses must exactly cover tranche tasks")
    errors = [
        error
        for task_id, task in tasks_by_id.items()
        for error in _response_errors(responses_by_id[task_id], task, tranche)
    ]
    if errors:
        raise AdoptionReviewError("; ".join(sorted(set(errors))))
    return [responses_by_id[task["task_id"]] for task in tasks]


def build_adoption_decision_ledger(
    tranche: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    prior_decisions: Sequence[Mapping[str, Any]],
    *,
    prior_decision_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    """Append one complete tranche to the checksummed sequential decision ledger."""
    compiled = compile_adoption_review_responses(tranche, responses)
    decisions = [*prior_decisions, *compiled]
    _decision_index(decisions)
    document = {
        "decision_ledger_version": REVIEW_VERSION,
        "tranche_id": tranche["tranche_id"],
        "tranche_sha256": tranche["tranche_sha256"],
        "case_index_sha256": tranche["case_index_sha256"],
        "workflow_sha256": tranche["workflow_sha256"],
        "prior_decision_ledger_sha256": prior_decision_ledger_sha256,
        "prior_decision_count": len(prior_decisions),
        "response_count": len(compiled),
        "decision_count": len(decisions),
        "decisions": decisions,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "decision_ledger_sha256": adoption_review_ledger_sha256(document),
    }


def validate_adoption_decision_ledger(
    ledger: Mapping[str, Any],
    *,
    case_index_sha256_value: str,
    workflow_sha256_value: str,
) -> list[Mapping[str, Any]]:
    """Validate a cumulative ledger before it may drive the next tranche."""
    if set(ledger) != LEDGER_FIELDS:
        raise AdoptionReviewError("prior decision ledger contract is invalid")
    if ledger.get("decision_ledger_sha256") != adoption_review_ledger_sha256(ledger):
        raise AdoptionReviewError("prior decision ledger checksum does not match")
    if ledger.get("outcomes_consulted") is not False:
        raise AdoptionReviewError("prior decision ledger is outcome exposed")
    if (
        ledger.get("case_index_sha256") != case_index_sha256_value
        or ledger.get("workflow_sha256") != workflow_sha256_value
    ):
        raise AdoptionReviewError("prior decision ledger study binding does not match")
    decisions = ledger.get("decisions")
    if not isinstance(decisions, list) or any(
        not isinstance(decision, Mapping) for decision in decisions
    ):
        raise AdoptionReviewError("prior decision ledger decisions are invalid")
    if ledger.get("decision_count") != len(decisions):
        raise AdoptionReviewError("prior decision ledger count does not match")
    counts = (
        ledger.get("prior_decision_count"),
        ledger.get("response_count"),
        ledger.get("decision_count"),
    )
    if any(not isinstance(count, int) or count < 0 for count in counts):
        raise AdoptionReviewError("prior decision ledger counts are invalid")
    if counts[0] + counts[1] != counts[2]:
        raise AdoptionReviewError("prior decision ledger count relation is invalid")
    for decision in decisions:
        if set(decision) != RESPONSE_FIELDS:
            raise AdoptionReviewError("prior decision response contract is invalid")
        if decision.get("response_sha256") != adoption_review_response_sha256(decision):
            raise AdoptionReviewError("prior decision response checksum does not match")
    _decision_index(decisions)
    return decisions


def _materialized_decisions(
    decision_ledger: Mapping[str, Any] | None,
    expected_decision_ledger_sha256: str | None,
    case_index: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], str | None]:
    if decision_ledger is None:
        if expected_decision_ledger_sha256 is not None:
            raise AdoptionReviewError("expected decision ledger has no input")
        return [], None
    if expected_decision_ledger_sha256 is None:
        raise AdoptionReviewError(
            "decision ledger requires its frozen expected checksum"
        )
    if decision_ledger.get("decision_ledger_sha256") != expected_decision_ledger_sha256:
        raise AdoptionReviewError(
            "decision ledger differs from frozen expected checksum"
        )
    decisions = validate_adoption_decision_ledger(
        decision_ledger,
        case_index_sha256_value=case_index["case_index_sha256"],
        workflow_sha256_value=case_index["workflow_sha256"],
    )
    return decisions, decision_ledger["decision_ledger_sha256"]


def _materialized_packets(
    case_index: Mapping[str, Any],
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index_path: Path,
    required_ids: set[str],
) -> dict[str, Mapping[str, Any]]:
    header, packet_stream = validated_packet_stream_from_file(
        discovery_specification,
        workflow_specification,
        packet_index_path,
    )
    if case_index.get("packet_index_sha256") != header.get("packet_index_sha256"):
        raise AdoptionReviewError("case index packet index binding does not match")
    return _selected_packets(packet_stream, required_ids)


def materialize_adoption_review_tranche(
    case_index: Mapping[str, Any],
    review_protocol: Mapping[str, Any],
    case_root: Path,
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index_path: Path,
    decision_ledger: Mapping[str, Any] | None,
    *,
    expected_decision_ledger_sha256: str | None = None,
    tranche_number: int,
) -> dict[str, Any]:
    """Load frozen files and emit the next evidence-complete review tranche."""
    _validate_review_protocol(
        review_protocol,
        case_index,
        workflow_specification,
    )
    if case_index.get("workflow_sha256") != workflow_specification.get(
        "workflow_sha256"
    ):
        raise AdoptionReviewError("case index workflow binding does not match")
    cases = _load_repository_cases(case_index, case_root)
    decisions, prior_ledger_sha256 = _materialized_decisions(
        decision_ledger,
        expected_decision_ledger_sha256,
        case_index,
    )
    required_ids = required_evidence_packet_ids(cases, decisions)
    packets = _materialized_packets(
        case_index,
        discovery_specification,
        workflow_specification,
        packet_index_path,
        required_ids,
    )
    return build_adoption_review_tranche(
        case_index,
        cases,
        packets,
        decisions,
        prior_decision_ledger_sha256=prior_ledger_sha256,
        tranche_number=tranche_number,
    )


def _validate_new_tranche(
    case_index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    tranche_number: int,
) -> None:
    if case_index.get("outcomes_consulted") is not False:
        raise AdoptionReviewError("case index is outcome exposed")
    if not isinstance(tranche_number, int) or tranche_number < 1:
        raise AdoptionReviewError("tranche number must be a positive integer")
    if decisions and tranche_number <= max(
        decision["tranche_number"] for decision in decisions
    ):
        raise AdoptionReviewError("new tranche must be newer than all prior decisions")


def _review_tasks(
    case_index: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    active: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], int, int, str]],
    tranche_number: int,
) -> list[dict[str, Any]]:
    return [
        _task(
            case_index,
            case,
            event,
            packets,
            candidate_position=position,
            candidate_count=candidate_count,
            review_role=review_role,
            tranche_number=tranche_number,
        )
        for case, event, position, candidate_count, review_role in active
    ]


def _tranche_document(
    case_index: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    *,
    tranche_number: int,
    prior_decision_ledger_sha256: str | None,
    eligible: int,
    completed: int,
    exhausted: int,
) -> dict[str, Any]:
    identity = {
        "case_index_sha256": case_index["case_index_sha256"],
        "tranche_number": tranche_number,
        "task_ids": [task["task_id"] for task in tasks],
    }
    return {
        "review_version": REVIEW_VERSION,
        "tranche_id": _content_sha256(identity),
        "tranche_number": tranche_number,
        "case_index_sha256": case_index["case_index_sha256"],
        "workflow_sha256": case_index["workflow_sha256"],
        "prior_decision_ledger_sha256": prior_decision_ledger_sha256,
        "eligible_repository_count": eligible,
        "completed_repository_count": completed,
        "exhausted_repository_count": exhausted,
        "active_repository_count": len(tasks),
        "repository_count": len(tasks),
        "task_count": len(tasks),
        "tasks": tasks,
        "outcomes_consulted": False,
    }


def build_adoption_review_tranche(
    case_index: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    packets: Mapping[str, Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    prior_decision_ledger_sha256: str | None = None,
    tranche_number: int,
) -> dict[str, Any]:
    """Emit one earliest-unresolved review task per eligible repository."""
    _validate_new_tranche(case_index, decisions, tranche_number)
    active, eligible, completed, exhausted = _active_event_states(cases, decisions)
    tasks = _review_tasks(case_index, packets, active, tranche_number)
    document = _tranche_document(
        case_index,
        tasks,
        tranche_number=tranche_number,
        prior_decision_ledger_sha256=prior_decision_ledger_sha256,
        eligible=eligible,
        completed=completed,
        exhausted=exhausted,
    )
    return {
        **document,
        "tranche_sha256": adoption_review_tranche_sha256(document),
    }
