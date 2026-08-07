"""Materialize explicit-anchor review after bounded prehistory screening."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_review import (
    AdoptionReviewError,
    _active_event_states,
    _candidate_sequence,
    _load_repository_cases,
    _selected_packets,
    _task,
    _validate_review_protocol,
    _response_errors,
    _review_state,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_adoption_review_contracts import _decision_index
from authorship.sourcegraph_repository_case_stream import (
    validated_packet_stream_from_file,
)

ANCHOR_COMPLETION_VERSION = 1
AMENDMENT_POLICY = {
    "$schema": "sourcegraph-adoption-anchor-amendment.schema.json",
    "amendment_version": 1,
    "effective_after_tranche": 7,
    "source_chain_sha256": (
        "4fbbfcd94e970112425a38d75160c4b5fc103d301f36ed618fd3e03909d4553d"
    ),
    "trigger": {
        "active_repositories_at_tranche_7": 111,
        "remaining_event_reviews_before_tranche_7": 4282,
        "maximum_single_repository_remaining_events": 675,
        "reason": (
            "One-event tranches spend most review effort rejecting generic "
            "challenge hits and do not change the requested first-explicit-use "
            "adoption estimand."
        ),
    },
    "selection_rule": {
        "eligible_state": (
            "still active after the tranche-7 ledger with no open peer review"
        ),
        "event": "first explicit-provenance anchor in the frozen repository sequence",
        "earlier_accepted_event_precedence": True,
        "anchor_review_is_semantic": True,
        "automatic_acceptance_forbidden": True,
        "unresolved_earlier_candidate_count_retained": True,
        "clean_prehistory_requires_zero_unresolved_earlier_candidates": True,
    },
    "interpretation": {
        "accepted_anchor_date": (
            "first observed explicit coding-agent use in the frozen Sourcegraph frame"
        ),
        "not_claimed": "earliest actual repository adoption",
        "pre_anchor_code": (
            "contemporary within-repository comparison code with possible "
            "earlier-agent contamination when clean_prehistory is false"
        ),
        "causal_interpretation": False,
    },
    "sensitivity": {
        "primary": (
            "all accepted first-observed explicit anchors with prehistory "
            "status disclosed"
        ),
        "clean_prehistory": (
            "restrict to repositories whose earlier challenge frame was fully resolved"
        ),
        "accepted_earlier_challenge": (
            "use the earlier accepted observed or confirmed date instead of the anchor"
        ),
        "unclean_prehistory_bias": (
            "treat possible pre-anchor agent contamination as bias toward "
            "smaller human-agent differences"
        ),
    },
    "review_execution": {
        "local_or_subagent_only": True,
        "paid_batch_api": False,
        "user_openai_api_key": False,
        "scip_required": False,
        "classifier_or_survival_outcomes_hidden": True,
    },
    "adjudication_decisions_available": True,
    "outcomes_consulted": False,
}


class AnchorCompletionError(AdoptionReviewError):
    """Raised when explicit-anchor completion would drift from frozen evidence."""


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def anchor_completion_manifest_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in document.items() if key != "manifest_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def anchor_completion_ledger_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in document.items()
        if key != "anchor_completion_ledger_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def anchor_amendment_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in document.items() if key != "amendment_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _anchor_task(
    case_index: Mapping[str, Any],
    case: Mapping[str, Any],
    anchor: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
    *,
    earliest_unresolved_position: int,
    anchor_position: int,
    candidate_count: int,
    unresolved_count: int,
    tranche_number: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if anchor["candidate_kind"] != "explicit_provenance_anchor":
        raise AnchorCompletionError("candidate sequence lacks an explicit anchor")
    task = _task(
        case_index,
        case,
        anchor,
        packets,
        candidate_position=anchor_position,
        candidate_count=candidate_count,
        review_role="primary",
        tranche_number=tranche_number,
    )
    prehistory = {
        "task_id": task["task_id"],
        "earliest_unresolved_position": earliest_unresolved_position,
        "unresolved_earlier_candidate_count": unresolved_count,
        "clean_prehistory": unresolved_count == 0,
    }
    return task, prehistory


def _anchor_context(
    case: Mapping[str, Any],
    decision_index: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> tuple[dict[str, Any], int, int, int, int]:
    sequence = _candidate_sequence(case)
    anchors = [
        event
        for event in sequence
        if event["candidate_kind"] == "explicit_provenance_anchor"
    ]
    if len(anchors) != 1:
        raise AnchorCompletionError("candidate sequence lacks one explicit anchor")
    anchor = anchors[0]
    anchor_time = anchor["candidate_event"]["observed_at"]
    earlier = [
        event
        for event in sequence
        if event["candidate_kind"] == "adoption_challenge"
        and event["candidate_event"]["observed_at"] < anchor_time
    ]
    repository = case["canonical_repository_id"]
    states = [
        _review_state(repository, event["event_id"], decision_index)
        for event in earlier
    ]
    if any(state in {"secondary", "resolver"} for state in states):
        raise AnchorCompletionError(
            "anchor completion cannot bypass unresolved peer review"
        )
    unresolved = [
        position for position, state in enumerate(states, start=1) if state == "primary"
    ]
    anchor_position = len(earlier) + 1
    earliest = unresolved[0] if unresolved else anchor_position
    return anchor, anchor_position, len(sequence), earliest, len(unresolved)


def _tasks(
    case_index: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    packets: Mapping[str, Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    tranche_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, int]:
    active, eligible, completed, exhausted = _active_event_states(cases, decisions)
    decision_index = _decision_index(decisions)
    selected = []
    anchor_exhausted = 0
    for case, _event, _position, _candidate_count, _state in active:
        anchor, anchor_position, candidate_count, earliest, unresolved = (
            _anchor_context(case, decision_index)
        )
        repository = case["canonical_repository_id"]
        anchor_state = _review_state(repository, anchor["event_id"], decision_index)
        if anchor_state in {"secondary", "resolver"}:
            raise AnchorCompletionError(
                "anchor completion cannot bypass unresolved peer review"
            )
        if anchor_state in {"reject", "insufficient"}:
            anchor_exhausted += 1
            continue
        if anchor_state != "primary":
            raise AnchorCompletionError("active repository has a finalized anchor")
        selected.append(
            _anchor_task(
                case_index,
                case,
                anchor,
                packets,
                earliest_unresolved_position=earliest,
                anchor_position=anchor_position,
                candidate_count=candidate_count,
                unresolved_count=unresolved,
                tranche_number=tranche_number,
            )
        )
    tasks = [task for task, _ in selected]
    prehistory = [record for _, record in selected]
    return tasks, prehistory, eligible, completed, exhausted + anchor_exhausted


def _validate_manifest_inputs(
    case_index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    tranche_number: int,
) -> None:
    if case_index.get("outcomes_consulted") is not False:
        raise AnchorCompletionError("case index is outcome exposed")
    if not isinstance(tranche_number, int) or tranche_number < 1:
        raise AnchorCompletionError("tranche number must be a positive integer")
    if decisions and tranche_number <= max(
        decision["tranche_number"] for decision in decisions
    ):
        raise AnchorCompletionError("anchor tranche must follow all prior decisions")


def _manifest_document(
    case_index: Mapping[str, Any],
    tasks: list[dict[str, Any]],
    prehistory: list[dict[str, Any]],
    counts: tuple[int, int, int],
    *,
    amendment_sha256: str,
    source_decision_ledger_sha256: str,
    tranche_number: int,
) -> dict[str, Any]:
    eligible, completed, exhausted = counts
    identity = {
        "amendment_sha256": amendment_sha256,
        "source_decision_ledger_sha256": source_decision_ledger_sha256,
        "tranche_number": tranche_number,
        "task_ids": [task["task_id"] for task in tasks],
    }
    return {
        "$schema": "sourcegraph-adoption-anchor-completion.schema.json",
        "anchor_completion_version": ANCHOR_COMPLETION_VERSION,
        "amendment_sha256": amendment_sha256,
        "source_decision_ledger_sha256": source_decision_ledger_sha256,
        "case_index_sha256": case_index["case_index_sha256"],
        "workflow_sha256": case_index["workflow_sha256"],
        "tranche_id": hashlib.sha256(_canonical_json(identity).encode()).hexdigest(),
        "tranche_number": tranche_number,
        "selection_mode": "explicit_anchor_completion",
        "eligible_repository_count": eligible,
        "completed_repository_count": completed,
        "exhausted_repository_count": exhausted,
        "task_count": len(tasks),
        "tasks": tasks,
        "prehistory": prehistory,
        "outcomes_consulted": False,
    }


def build_anchor_completion_manifest(
    case_index: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    packets: Mapping[str, Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    amendment_sha256: str,
    source_decision_ledger_sha256: str,
    tranche_number: int,
) -> dict[str, Any]:
    """Build a blind anchor tranche while retaining prehistory uncertainty."""

    _validate_manifest_inputs(case_index, decisions, tranche_number)
    try:
        tasks, prehistory, eligible, completed, exhausted = _tasks(
            case_index,
            cases,
            packets,
            decisions,
            tranche_number,
        )
    except AnchorCompletionError:
        raise
    except AdoptionReviewError as error:
        raise AnchorCompletionError(str(error)) from error
    document = _manifest_document(
        case_index,
        tasks,
        prehistory,
        (eligible, completed, exhausted),
        amendment_sha256=amendment_sha256,
        source_decision_ledger_sha256=source_decision_ledger_sha256,
        tranche_number=tranche_number,
    )
    return {
        **document,
        "manifest_sha256": anchor_completion_manifest_sha256(document),
    }


def compile_anchor_completion_responses(
    manifest: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate exact anchor responses and freeze a separate decision ledger."""

    if manifest.get("manifest_sha256") != anchor_completion_manifest_sha256(manifest):
        raise AnchorCompletionError(
            "anchor completion manifest checksum does not match"
        )
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise AnchorCompletionError("anchor completion tasks are invalid")
    tasks_by_id = {task.get("task_id"): task for task in tasks}
    responses_by_id = {response.get("task_id"): response for response in responses}
    if len(tasks_by_id) != len(tasks) or len(responses_by_id) != len(responses):
        raise AnchorCompletionError("anchor completion task identity is duplicated")
    if set(tasks_by_id) != set(responses_by_id):
        raise AnchorCompletionError("anchor responses must exactly cover tasks")
    errors = [
        error
        for task_id, task in tasks_by_id.items()
        for error in _response_errors(responses_by_id[task_id], task, manifest)
    ]
    if errors:
        raise AnchorCompletionError("; ".join(sorted(set(errors))))
    compiled = [responses_by_id[task["task_id"]] for task in tasks]
    _decision_index(compiled)
    document = {
        "$schema": "sourcegraph-adoption-anchor-decision-ledger.schema.json",
        "anchor_completion_ledger_version": ANCHOR_COMPLETION_VERSION,
        "manifest_sha256": manifest["manifest_sha256"],
        "amendment_sha256": manifest["amendment_sha256"],
        "source_decision_ledger_sha256": manifest["source_decision_ledger_sha256"],
        "tranche_id": manifest["tranche_id"],
        "tranche_number": manifest["tranche_number"],
        "decision_count": len(compiled),
        "decisions": compiled,
        "prehistory": manifest["prehistory"],
        "outcomes_consulted": False,
    }
    return {
        **document,
        "anchor_completion_ledger_sha256": anchor_completion_ledger_sha256(document),
    }


def _anchor_packet_ids(
    cases: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> set[str]:
    active, _, _, _ = _active_event_states(cases, decisions)
    decision_index = _decision_index(decisions)
    required = set()
    for case, _event, _position, _count, _state in active:
        anchor, _anchor_position, _count, _earliest, _unresolved = _anchor_context(
            case, decision_index
        )
        anchor_state = _review_state(
            case["canonical_repository_id"],
            anchor["event_id"],
            decision_index,
        )
        if anchor_state not in {"reject", "insufficient"}:
            required.update(anchor["packet_ids"])
    return required


def _validate_amendment(
    amendment: Mapping[str, Any],
    decision_ledger: Mapping[str, Any],
    expected_amendment_sha256: str,
) -> None:
    actual_sha256 = anchor_amendment_sha256(amendment)
    if amendment.get("amendment_sha256") != actual_sha256:
        raise AnchorCompletionError("anchor amendment checksum does not match")
    if actual_sha256 != expected_amendment_sha256:
        raise AnchorCompletionError("anchor amendment differs from frozen checksum")
    source_ledger_sha256 = decision_ledger.get("decision_ledger_sha256")
    expected = {
        **AMENDMENT_POLICY,
        "source_decision_ledger_sha256": source_ledger_sha256,
    }
    payload = {
        key: value for key, value in amendment.items() if key != "amendment_sha256"
    }
    if payload != expected:
        raise AnchorCompletionError("anchor amendment policy does not match")
    if amendment.get("source_decision_ledger_sha256") != source_ledger_sha256:
        raise AnchorCompletionError("anchor amendment ledger binding does not match")


def materialize_anchor_completion_manifest(
    case_index: Mapping[str, Any],
    review_protocol: Mapping[str, Any],
    amendment: Mapping[str, Any],
    case_root: Path,
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index_path: Path,
    decision_ledger: Mapping[str, Any],
    *,
    expected_decision_ledger_sha256: str,
    expected_amendment_sha256: str,
    tranche_number: int,
) -> dict[str, Any]:
    """Load frozen inputs and materialize the evidence-complete anchor tranche."""

    _validate_review_protocol(review_protocol, case_index, workflow_specification)
    if decision_ledger.get("decision_ledger_sha256") != expected_decision_ledger_sha256:
        raise AnchorCompletionError("decision ledger differs from frozen checksum")
    _validate_amendment(
        amendment,
        decision_ledger,
        expected_amendment_sha256,
    )
    cases = _load_repository_cases(case_index, case_root)
    decisions = validate_adoption_decision_ledger(
        decision_ledger,
        case_index_sha256_value=case_index["case_index_sha256"],
        workflow_sha256_value=case_index["workflow_sha256"],
    )
    required_ids = _anchor_packet_ids(cases, decisions)
    header, packet_stream = validated_packet_stream_from_file(
        discovery_specification,
        workflow_specification,
        packet_index_path,
    )
    if case_index.get("packet_index_sha256") != header.get("packet_index_sha256"):
        raise AnchorCompletionError("case index packet binding does not match")
    packets = _selected_packets(packet_stream, required_ids)
    return build_anchor_completion_manifest(
        case_index,
        cases,
        packets,
        decisions,
        amendment_sha256=amendment["amendment_sha256"],
        source_decision_ledger_sha256=decision_ledger["decision_ledger_sha256"],
        tranche_number=tranche_number,
    )
