"""Outcome-blind sequential review for preregistered AI-ban controls."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from authorship.sourcegraph_ai_ban_review_contracts import (
    CONTROL_REPOSITORY_COUNT,
    DECISIONS,
    EXCLUSION_REASONS,
    FINAL_DECISIONS,
    RESPONSE_FIELDS,
    REVIEW_ROLES,
    REVIEW_VERSION,
    _content_sha256,
    ai_ban_ledger_sha256,
    ai_ban_response_sha256,
    ai_ban_target_manifest_sha256,
    ai_ban_task_sha256,
    ai_ban_worksheet_sha256,
)
from authorship.sourcegraph_discovery import evidence_packet_sha256
from authorship.sourcegraph_repository_cases import (
    case_index_sha256,
    repository_case_sha256,
)


class AiBanReviewError(ValueError):
    """Raised when the bounded AI-ban review frame drifts."""


def _control_repositories(control: Mapping[str, Any]) -> list[tuple[str, str]]:
    repos = control.get("repos")
    if isinstance(repos, Mapping):
        repository_ids = list(repos)
    elif isinstance(repos, list):
        repository_ids = repos
    else:
        repository_ids = []
    if len(repository_ids) != CONTROL_REPOSITORY_COUNT:
        raise AiBanReviewError("control evidence must contain exactly 17 repositories")
    normalized = [
        (value, value.lower())
        for value in repository_ids
        if isinstance(value, str) and value
    ]
    if len(normalized) != len(repository_ids) or len(
        {item[1] for item in normalized}
    ) != len(repository_ids):
        raise AiBanReviewError("control evidence repository identities are invalid")
    if not isinstance(control.get("since"), str):
        raise AiBanReviewError("control evidence date boundary is invalid")
    return normalized


def _manifest_records(
    manifest: Mapping[str, Any], control_ids: set[str]
) -> dict[str, Mapping[str, Any]]:
    if manifest.get("outcomes_consulted") is not False:
        raise AiBanReviewError("index manifest is outcome exposed")
    records = manifest.get("repositories")
    if not isinstance(records, list):
        raise AiBanReviewError("index manifest repositories are invalid")
    controls = [
        record
        for record in records
        if isinstance(record, Mapping)
        and "adoption_ai_ban_control_seed" in record.get("roles", [])
    ]
    indexed = {record.get("canonical_repository_id"): record for record in controls}
    if len(indexed) != len(controls) or any(key not in control_ids for key in indexed):
        raise AiBanReviewError("index manifest control scope is invalid")
    return indexed


def _case_records(case_index: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if case_index.get("case_index_sha256") != case_index_sha256(case_index):
        raise AiBanReviewError("case index checksum does not match")
    if case_index.get("outcomes_consulted") is not False:
        raise AiBanReviewError("case index is outcome exposed")
    records = case_index.get("repositories")
    if not isinstance(records, list):
        raise AiBanReviewError("case index repositories are invalid")
    indexed = {
        record.get("canonical_repository_id"): record
        for record in records
        if isinstance(record, Mapping)
    }
    if len(indexed) != len(records):
        raise AiBanReviewError("case index repository identities are duplicated")
    return indexed


def _exclusion(
    index_record: Mapping[str, Any] | None,
    case_record: Mapping[str, Any] | None,
    case: Mapping[str, Any] | None,
) -> str | None:
    if index_record is None:
        return "missing_index_manifest_record"
    sourcegraph = index_record.get("sourcegraph")
    mirror = sourcegraph.get("mirror") if isinstance(sourcegraph, Mapping) else None
    direct = sourcegraph.get("direct") if isinstance(sourcegraph, Mapping) else None
    mirror_ready = (
        isinstance(mirror, Mapping)
        and mirror.get("state") == "indexed"
        and mirror.get("cutoff_state") == "accessible"
        and str(mirror.get("name", "")).startswith("github.com/sg-evals/")
    )
    if not mirror_ready:
        direct_ready = (
            isinstance(direct, Mapping)
            and direct.get("state") == "indexed"
            and direct.get("cutoff_state") == "accessible"
        )
        return "direct_index_only" if direct_ready else "missing_sg_evals_mirror"
    if case_record is None or case is None:
        return "missing_frozen_case"
    candidates = case.get("queues", {}).get("ai_ban_candidates")
    if not isinstance(candidates, list) or not candidates:
        return "no_frozen_ai_ban_candidate"
    return None


def _validate_case(
    repository: str,
    index_record: Mapping[str, Any],
    case_record: Mapping[str, Any],
    case: Mapping[str, Any],
) -> None:
    if case.get("repository_case_sha256") != repository_case_sha256(case):
        raise AiBanReviewError("repository case checksum does not match")
    if (
        case.get("canonical_repository_id") != repository
        or case.get("case_id") != case_record.get("case_id")
        or case.get("outcomes_consulted") is not False
    ):
        raise AiBanReviewError("repository case identity binding does not match")
    mirror = index_record["sourcegraph"]["mirror"]
    if (
        case.get("sourcegraph_name") != mirror.get("name")
        or case.get("cutoff_commit") != index_record.get("cutoff_commit")
        or mirror.get("cutoff_oid") != index_record.get("cutoff_commit")
    ):
        raise AiBanReviewError("repository case mirror binding does not match")


def _candidates(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = case["queues"]["ai_ban_candidates"]
    ordered = sorted(
        candidates,
        key=lambda item: (
            item["candidate_event"]["observed_at"],
            item["candidate_event"]["commit_oid"],
            item["event_id"],
        ),
    )
    if any(
        item.get("packet_type") != "ai_ban_policy"
        or not item.get("packet_ids")
        or item.get("packet_count") != len(item["packet_ids"])
        for item in ordered
    ):
        raise AiBanReviewError("AI-ban candidate contract is invalid")
    return deepcopy(ordered)


def _target_record(
    source_id: str,
    repository: str,
    index_record: Mapping[str, Any] | None,
    case_record: Mapping[str, Any] | None,
    case: Mapping[str, Any] | None,
) -> dict[str, Any]:
    exclusion = _exclusion(index_record, case_record, case)
    candidates: list[dict[str, Any]] = []
    if exclusion not in {
        "missing_index_manifest_record",
        "direct_index_only",
        "missing_sg_evals_mirror",
        "missing_frozen_case",
    }:
        if index_record is None or case_record is None or case is None:
            raise AiBanReviewError("eligible target inputs are incomplete")
        _validate_case(repository, index_record, case_record, case)
        candidates = _candidates(case)
    mirror = (
        index_record.get("sourcegraph", {}).get("mirror", {})
        if index_record is not None
        else {}
    )
    return {
        "preregistered_repository_id": source_id,
        "canonical_repository_id": repository,
        "canonical_source_url": (
            index_record.get("canonical_source_url")
            if index_record is not None
            else f"https://github.com/{source_id}"
        ),
        "sourcegraph_name": mirror.get("name"),
        "cutoff_commit": (
            index_record.get("cutoff_commit") if index_record is not None else None
        ),
        "case_id": case.get("case_id") if case is not None else None,
        "repository_case_sha256": (
            case.get("repository_case_sha256") if case is not None else None
        ),
        "status": "excluded" if exclusion else "eligible",
        "exclusion_reason": exclusion,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _validate_predecessors(
    predecessors: Mapping[str, Any], case_index: Mapping[str, Any]
) -> dict[str, Any]:
    fields = {
        "control_evidence_file_sha256",
        "index_manifest_file_sha256",
        "case_index_file_sha256",
        "case_index_sha256",
        "packet_index_sha256",
        "specification_sha256",
        "workflow_sha256",
    }
    if set(predecessors) != fields or any(
        not isinstance(predecessors[field], str) or len(predecessors[field]) != 64
        for field in fields
    ):
        raise AiBanReviewError("target predecessor pins are invalid")
    bindings = {
        "case_index_sha256": case_index.get("case_index_sha256"),
        "packet_index_sha256": case_index.get("packet_index_sha256"),
        "specification_sha256": case_index.get("specification_sha256"),
        "workflow_sha256": case_index.get("workflow_sha256"),
    }
    if any(predecessors[field] != value for field, value in bindings.items()):
        raise AiBanReviewError("target predecessor binding does not match")
    return dict(predecessors)


def build_ai_ban_target_manifest(
    control_evidence: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    case_index: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    predecessors: Mapping[str, Any],
) -> dict[str, Any]:
    """Account for the bounded control frame without making policy decisions."""
    control = _control_repositories(control_evidence)
    control_ids = {item[1] for item in control}
    index_records = _manifest_records(index_manifest, control_ids)
    case_records = _case_records(case_index)
    records = [
        _target_record(
            source_id,
            repository,
            index_records.get(repository),
            case_records.get(repository),
            cases.get(repository),
        )
        for source_id, repository in control
    ]
    document = _target_document(
        records,
        control_evidence["since"],
        _validate_predecessors(predecessors, case_index),
    )
    return {
        **document,
        "target_manifest_sha256": ai_ban_target_manifest_sha256(document),
    }


def _target_document(
    records: Sequence[Mapping[str, Any]],
    control_since: str,
    predecessors: Mapping[str, Any],
) -> dict[str, Any]:
    excluded = Counter(
        record["exclusion_reason"]
        for record in records
        if record["exclusion_reason"] is not None
    )
    return {
        "$schema": "sourcegraph-ai-ban-target-manifest.schema.json",
        "manifest_version": REVIEW_VERSION,
        "predecessors": dict(predecessors),
        "control_since": control_since,
        "scope": "data/control_evidence.json:repos",
        "repository_count": len(records),
        "eligible_repository_count": sum(
            record["status"] == "eligible" for record in records
        ),
        "excluded_repository_count": sum(
            record["status"] == "excluded" for record in records
        ),
        "exclusion_policy": sorted(EXCLUSION_REASONS),
        "exclusion_counts": dict(sorted(excluded.items())),
        "repositories": records,
        "semantic_policy_decisions_made": False,
        "scip_required": False,
        "paid_api_used": False,
        "openai_api_key_used": False,
        "classifier_outcomes_consulted": False,
        "survival_outcomes_consulted": False,
        "outcomes_consulted": False,
    }


def _validate_target(
    target: Mapping[str, Any], expected_target_manifest_sha256: str
) -> None:
    if target.get("target_manifest_sha256") != ai_ban_target_manifest_sha256(target):
        raise AiBanReviewError("target manifest checksum does not match")
    if target.get("target_manifest_sha256") != expected_target_manifest_sha256:
        raise AiBanReviewError("target manifest differs from independent pin")
    if (
        target.get("outcomes_consulted") is not False
        or target.get("semantic_policy_decisions_made") is not False
    ):
        raise AiBanReviewError("target manifest is outcome or decision exposed")


def _decision_index(
    decisions: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    indexed = {}
    reviewers: dict[tuple[str, str], set[str]] = {}
    for decision in decisions:
        role = decision.get("review_role")
        value = decision.get("decision")
        if (
            set(decision) != RESPONSE_FIELDS
            or decision.get("response_version") != REVIEW_VERSION
            or role not in REVIEW_ROLES
            or value not in DECISIONS
            or decision.get("response_sha256") != ai_ban_response_sha256(decision)
            or decision.get("outcomes_consulted") is not False
        ):
            raise AiBanReviewError("decision ledger response is invalid")
        citations = decision.get("evidence_citations")
        if (
            _semantic_response_errors(decision, value)
            or not isinstance(decision.get("reviewer_id"), str)
            or not decision["reviewer_id"]
            or not isinstance(citations, list)
            or not citations
        ):
            raise AiBanReviewError("decision ledger response contract is invalid")
        if role == "resolver" and value not in FINAL_DECISIONS:
            raise AiBanReviewError("resolver decision must be final")
        key = (
            decision.get("canonical_repository_id"),
            decision.get("event_id"),
            role,
        )
        if key in indexed:
            raise AiBanReviewError("review role is duplicated for event")
        reviewer = decision.get("reviewer_id")
        event_reviewers = reviewers.setdefault(key[:2], set())
        if reviewer in event_reviewers:
            raise AiBanReviewError("event reviewers must be distinct")
        event_reviewers.add(reviewer)
        indexed[key] = decision
    return indexed


def _validated_decisions(
    ledger: Mapping[str, Any] | None, target_sha256: str
) -> list[Mapping[str, Any]]:
    if ledger is None:
        return []
    if ledger.get("decision_ledger_sha256") != ai_ban_ledger_sha256(ledger):
        raise AiBanReviewError("decision ledger checksum does not match")
    decisions = ledger.get("decisions")
    if (
        ledger.get("target_manifest_sha256") != target_sha256
        or not isinstance(decisions, list)
        or ledger.get("decision_count") != len(decisions)
        or ledger.get("outcomes_consulted") is not False
    ):
        raise AiBanReviewError("decision ledger binding is invalid")
    _decision_index(decisions)
    return decisions


def _event_state(
    repository: str,
    event_id: str,
    decisions: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> str:
    primary = decisions.get((repository, event_id, "primary"))
    secondary = decisions.get((repository, event_id, "secondary"))
    resolver = decisions.get((repository, event_id, "resolver"))
    if primary is None:
        if secondary is not None or resolver is not None:
            raise AiBanReviewError("review roles are out of order")
        return "primary"
    if primary["decision"] != "ambiguous":
        if secondary is not None or resolver is not None:
            raise AiBanReviewError("review roles are out of order")
        return primary["decision"]
    if secondary is None:
        if resolver is not None:
            raise AiBanReviewError("review roles are out of order")
        return "secondary"
    return "resolver" if resolver is None else resolver["decision"]


def _active_events(
    target: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], int, str]]:
    indexed = _decision_index(decisions)
    active = []
    for record in target["repositories"]:
        if record["status"] != "eligible":
            continue
        repository = record["canonical_repository_id"]
        for position, event in enumerate(record["candidates"], start=1):
            state = _event_state(repository, event["event_id"], indexed)
            if state == "accept_policy":
                break
            if state in {"reject", "insufficient"}:
                continue
            active.append((record, event, position, state))
            break
    return active


def required_ai_ban_packet_ids(
    target_manifest: Mapping[str, Any],
    decision_ledger: Mapping[str, Any] | None,
) -> set[str]:
    """Return packet IDs for each repository's next unresolved policy event."""
    expected_sha256 = target_manifest.get("target_manifest_sha256")
    _validate_target(target_manifest, expected_sha256)
    decisions = _validated_decisions(decision_ledger, expected_sha256)
    _validate_decision_scope(target_manifest, decisions)
    return {
        packet_id
        for _record, event, _position, _role in _active_events(
            target_manifest, decisions
        )
        for packet_id in event["packet_ids"]
    }


def _validate_decision_scope(
    target: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
) -> None:
    events = {
        (record["canonical_repository_id"], event["event_id"])
        for record in target["repositories"]
        if record["status"] == "eligible"
        for event in record["candidates"]
    }
    for decision in decisions:
        repository = decision["canonical_repository_id"]
        event_id = decision["event_id"]
        role = decision["review_role"]
        identity = {
            "target_manifest_sha256": target["target_manifest_sha256"],
            "canonical_repository_id": repository,
            "event_id": event_id,
            "review_role": role,
        }
        if (repository, event_id) not in events or decision[
            "task_id"
        ] != _content_sha256(identity):
            raise AiBanReviewError("decision ledger is outside target scope")


def _packet_index(
    packets: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    indexed = {packet.get("packet_id"): packet for packet in packets}
    if len(indexed) != len(packets) or None in indexed:
        raise AiBanReviewError("evidence packet IDs are duplicated")
    for packet in packets:
        if (
            packet.get("packet_sha256") != evidence_packet_sha256(packet)
            or packet.get("outcomes_consulted") is not False
            or packet.get("packet_type") != "ai_ban_policy"
            or not str(packet.get("sourcegraph_name", "")).startswith(
                "github.com/sg-evals/"
            )
        ):
            raise AiBanReviewError("AI-ban evidence packet is invalid")
    return indexed


def _task(
    target: Mapping[str, Any],
    record: Mapping[str, Any],
    event: Mapping[str, Any],
    position: int,
    role: str,
    packets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    evidence = _task_evidence(record, event, packets)
    document = _task_document(target, record, event, position, role, evidence)
    return {**document, "task_sha256": ai_ban_task_sha256(document)}


def _task_evidence(
    record: Mapping[str, Any],
    event: Mapping[str, Any],
    packets: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    evidence = []
    for packet_id in event["packet_ids"]:
        packet = packets.get(packet_id)
        if packet is None:
            raise AiBanReviewError(f"missing evidence packet {packet_id}")
        if (
            packet.get("canonical_repository_id") != record["canonical_repository_id"]
            or packet.get("candidate_event") != event["candidate_event"]
            or packet.get("sourcegraph_name") != record["sourcegraph_name"]
        ):
            raise AiBanReviewError("evidence packet task binding does not match")
        evidence.append(
            {
                "packet_id": packet_id,
                "packet_sha256": packet["packet_sha256"],
                "query_family_id": packet["query_family_id"],
                "raw_evidence": deepcopy(packet["raw_evidence"]),
            }
        )
    return evidence


def _task_document(
    target: Mapping[str, Any],
    record: Mapping[str, Any],
    event: Mapping[str, Any],
    position: int,
    role: str,
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    identity = {
        "target_manifest_sha256": target["target_manifest_sha256"],
        "canonical_repository_id": record["canonical_repository_id"],
        "event_id": event["event_id"],
        "review_role": role,
    }
    return {
        "task_id": _content_sha256(identity),
        "target_manifest_sha256": target["target_manifest_sha256"],
        "canonical_repository_id": record["canonical_repository_id"],
        "canonical_source_url": record["canonical_source_url"],
        "sourcegraph_name": record["sourcegraph_name"],
        "cutoff_commit": record["cutoff_commit"],
        "event_id": event["event_id"],
        "candidate_event": deepcopy(event["candidate_event"]),
        "candidate_position": position,
        "candidate_count": record["candidate_count"],
        "packet_ids": list(event["packet_ids"]),
        "query_family_ids": list(event["query_family_ids"]),
        "evidence": evidence,
        "review_role": role,
        "review_question": (
            "Does this default-branch event introduce a repository-wide "
            "prohibition on AI-generated code contributions? Training or "
            "data-use restrictions alone do not qualify."
        ),
        "allowed_decisions": sorted(
            FINAL_DECISIONS if role == "resolver" else DECISIONS
        ),
        "outcomes_consulted": False,
    }


def build_ai_ban_review_worksheet(
    target_manifest: Mapping[str, Any],
    packets: Sequence[Mapping[str, Any]],
    *,
    decision_ledger: Mapping[str, Any] | None,
    expected_target_manifest_sha256: str,
) -> dict[str, Any]:
    """Expose only the earliest unresolved policy event per eligible repository."""
    _validate_target(target_manifest, expected_target_manifest_sha256)
    decisions = _validated_decisions(
        decision_ledger, target_manifest["target_manifest_sha256"]
    )
    _validate_decision_scope(target_manifest, decisions)
    packet_index = _packet_index(packets)
    tasks = [
        _task(target_manifest, record, event, position, role, packet_index)
        for record, event, position, role in _active_events(target_manifest, decisions)
    ]
    document = {
        "$schema": "sourcegraph-ai-ban-review-worksheet.schema.json",
        "review_version": REVIEW_VERSION,
        "target_manifest_sha256": target_manifest["target_manifest_sha256"],
        "predecessors": {
            **target_manifest["predecessors"],
            "prior_decision_ledger_sha256": (
                decision_ledger.get("decision_ledger_sha256")
                if decision_ledger is not None
                else None
            ),
        },
        "task_count": len(tasks),
        "tasks": tasks,
        "stopping_rule": "earliest_accepted_policy_event",
        "ambiguous_resolution": "distinct_secondary_then_distinct_resolver",
        "semantic_policy_decisions_made": False,
        "scip_required": False,
        "paid_api_used": False,
        "openai_api_key_used": False,
        "classifier_outcomes_consulted": False,
        "survival_outcomes_consulted": False,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "worksheet_sha256": ai_ban_worksheet_sha256(document),
    }


def _response_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
    worksheet: Mapping[str, Any],
    expected_reviewer: str,
) -> list[str]:
    errors = []
    if set(response) != RESPONSE_FIELDS:
        errors.append("response contract fields do not match")
    if response.get("response_version") != REVIEW_VERSION:
        errors.append("response version is invalid")
    if response.get("response_sha256") != ai_ban_response_sha256(response):
        errors.append("response checksum does not match")
    bindings = (
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "event_id",
        "review_role",
    )
    if any(
        response.get(field) != task.get(field) for field in bindings
    ) or response.get("worksheet_sha256") != worksheet.get("worksheet_sha256"):
        errors.append("response task binding does not match")
    if response.get("reviewer_id") != expected_reviewer:
        errors.append("response reviewer identity does not match expected")
    decision = response.get("decision")
    if decision not in task["allowed_decisions"]:
        errors.append("response decision is invalid")
    errors.extend(_semantic_response_errors(response, decision))
    citations = response.get("evidence_citations")
    available = {
        item["source_url"]
        for packet in task["evidence"]
        for item in packet["raw_evidence"]
    }
    if (
        not isinstance(citations, list)
        or not citations
        or any(citation not in available for citation in citations)
    ):
        errors.append("response citations must be task-local")
    if response.get("outcomes_consulted") is not False:
        errors.append("response is outcome exposed")
    return errors


def _semantic_response_errors(response: Mapping[str, Any], decision: Any) -> list[str]:
    accepted = decision == "accept_policy"
    expected_scope = (
        "repository_wide_ai_generated_contribution_prohibition"
        if accepted
        else "not_applicable"
    )
    expected_tier = "datable_default_branch_policy" if accepted else "not_applicable"
    errors = []
    if response.get("policy_scope") != expected_scope:
        errors.append("response policy scope does not match decision")
    if response.get("evidence_tier") != expected_tier:
        errors.append("response evidence tier does not match decision")
    if response.get("default_branch_supported") is not accepted:
        errors.append("response default-branch support does not match decision")
    if (
        not isinstance(response.get("rationale"), str)
        or not response["rationale"].strip()
    ):
        errors.append("response rationale is required")
    return errors


def compile_ai_ban_review_responses(
    worksheet: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    *,
    prior_ledger: Mapping[str, Any] | None,
    expected_worksheet_sha256: str,
    expected_reviewer_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Validate a complete fresh-review tranche and append its decisions."""
    compiled = _validated_response_tranche(
        worksheet,
        responses,
        expected_worksheet_sha256,
        expected_reviewer_ids,
    )
    prior = _validated_decisions(prior_ledger, worksheet["target_manifest_sha256"])
    decisions = [*prior, *compiled]
    _decision_index(decisions)
    document = _ledger_document(worksheet, prior_ledger, prior, compiled, decisions)
    return {
        **document,
        "decision_ledger_sha256": ai_ban_ledger_sha256(document),
    }


def _validated_response_tranche(
    worksheet: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    expected_worksheet_sha256: str,
    expected_reviewer_ids: Mapping[str, str],
) -> list[Mapping[str, Any]]:
    if worksheet.get("worksheet_sha256") != ai_ban_worksheet_sha256(worksheet):
        raise AiBanReviewError("worksheet checksum does not match")
    if worksheet.get("worksheet_sha256") != expected_worksheet_sha256:
        raise AiBanReviewError("worksheet differs from independent pin")
    tasks = {task["task_id"]: task for task in worksheet.get("tasks", [])}
    indexed = {response.get("task_id"): response for response in responses}
    if (
        len(tasks) != worksheet.get("task_count")
        or len(indexed) != len(responses)
        or set(indexed) != set(tasks)
    ):
        raise AiBanReviewError("responses must exactly cover worksheet tasks")
    if set(expected_reviewer_ids) != set(tasks):
        raise AiBanReviewError("expected reviewer identities must exactly cover tasks")
    errors = [
        error
        for task_id, task in tasks.items()
        for error in _response_errors(
            indexed[task_id],
            task,
            worksheet,
            expected_reviewer_ids[task_id],
        )
    ]
    if errors:
        raise AiBanReviewError("; ".join(sorted(set(errors))))
    return [indexed[task["task_id"]] for task in worksheet["tasks"]]


def _ledger_document(
    worksheet: Mapping[str, Any],
    prior_ledger: Mapping[str, Any] | None,
    prior: Sequence[Mapping[str, Any]],
    compiled: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "$schema": "sourcegraph-ai-ban-decision-ledger.schema.json",
        "decision_ledger_version": REVIEW_VERSION,
        "target_manifest_sha256": worksheet["target_manifest_sha256"],
        "worksheet_sha256": worksheet["worksheet_sha256"],
        "prior_decision_ledger_sha256": (
            prior_ledger.get("decision_ledger_sha256")
            if prior_ledger is not None
            else None
        ),
        "prior_decision_count": len(prior),
        "response_count": len(compiled),
        "decision_count": len(decisions),
        "decisions": deepcopy(decisions),
        "outcomes_consulted": False,
    }
