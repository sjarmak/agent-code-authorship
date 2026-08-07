"""Compile independently pinned Sourcegraph adoption audit responses."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from authorship.sourcegraph_adoption_audit import (
    AUDIT_DECISIONS,
    AUDIT_RESPONSE_FIELDS,
    AUDIT_VERSION,
    AdoptionAuditError,
    _content_sha256,
    audit_key_sha256,
    audit_response_sha256,
    audit_worksheet_sha256,
    build_adoption_reliability_audit,
    compiled_audit_sha256,
)


def _unique_index(value: Any, field: str, label: str) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise AdoptionAuditError(f"{label} are invalid")
    indexed = {item.get(field): item for item in value}
    if len(indexed) != len(value) or None in indexed:
        raise AdoptionAuditError(f"{label} are duplicated")
    return indexed


def _validate_artifacts(
    worksheet: Mapping[str, Any], key: Mapping[str, Any]
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    if worksheet.get("worksheet_sha256") != audit_worksheet_sha256(worksheet):
        raise AdoptionAuditError("worksheet checksum does not match")
    if key.get("key_sha256") != audit_key_sha256(key):
        raise AdoptionAuditError("key checksum does not match")
    if (
        worksheet.get("outcomes_consulted") is not False
        or key.get("outcomes_consulted") is not False
    ):
        raise AdoptionAuditError("audit artifact is outcome exposed")
    _validate_artifact_binding(worksheet, key)
    tasks = _unique_index(worksheet.get("tasks"), "audit_task_id", "worksheet tasks")
    items = _unique_index(key.get("items"), "audit_task_id", "key items")
    _validate_task_key_binding(worksheet, key, tasks, items)
    return tasks, items


def _validate_artifact_binding(
    worksheet: Mapping[str, Any], key: Mapping[str, Any]
) -> None:
    if (
        key.get("worksheet_sha256") != worksheet.get("worksheet_sha256")
        or key.get("predecessors") != worksheet.get("predecessors")
        or key.get("seed") != worksheet.get("seed")
    ):
        raise AdoptionAuditError("worksheet and key binding does not match")


def _validate_task_key_binding(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
    items: Mapping[str, Mapping[str, Any]],
) -> None:
    if set(tasks) != set(items):
        raise AdoptionAuditError("worksheet and key must exactly cover audit tasks")
    if worksheet.get("sample_count") != len(tasks) or key.get("sample_count") != len(
        items
    ):
        raise AdoptionAuditError("audit artifact counts do not match")
    identity = ("task_id", "task_sha256", "canonical_repository_id", "event_id")
    if any(
        any(task.get(field) != items[audit_id].get(field) for field in identity)
        for audit_id, task in tasks.items()
    ):
        raise AdoptionAuditError("worksheet and key task binding does not match")
    _validate_strata(key, items)


def _validate_strata(
    key: Mapping[str, Any], items: Mapping[str, Mapping[str, Any]]
) -> None:
    strata = _unique_index(key.get("strata"), "stratum_id", "key strata")
    labels = ("primary_decision", "evidence_channel", "language")
    if key.get("joint_stratum_count") != len(strata) or any(
        item.get("stratum_id") not in strata
        or any(
            item.get(field) != strata[item["stratum_id"]].get(field) for field in labels
        )
        for item in items.values()
    ):
        raise AdoptionAuditError("key stratum binding does not match")


def _rematerialize(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    language_inventory: Mapping[str, Any],
    *,
    seed: str,
    expected_worksheet_sha256: str,
    expected_key_sha256: str,
) -> None:
    if worksheet.get("worksheet_sha256") != expected_worksheet_sha256:
        raise AdoptionAuditError(
            "worksheet differs from independently expected checksum"
        )
    if key.get("key_sha256") != expected_key_sha256:
        raise AdoptionAuditError("key differs from independently expected checksum")
    expected_worksheet, expected_key = build_adoption_reliability_audit(
        ledger, tranches, language_inventory, seed=seed
    )
    if worksheet != expected_worksheet:
        raise AdoptionAuditError("worksheet differs from rematerialized frozen sources")
    if key != expected_key:
        raise AdoptionAuditError("key differs from rematerialized frozen sources")


def _expected_auditors(
    expected: Mapping[str, str],
    tasks: Mapping[str, Mapping[str, Any]],
    items: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    if not isinstance(expected, Mapping) or set(expected) != set(tasks):
        raise AdoptionAuditError("expected auditor identities must exactly cover tasks")
    if any(
        not isinstance(value, str) or not value.strip() for value in expected.values()
    ):
        raise AdoptionAuditError("expected auditor identity is invalid")
    if any(
        expected[audit_id] == items[audit_id].get("primary_reviewer_id")
        for audit_id in tasks
    ):
        raise AdoptionAuditError("expected auditor must be distinct from primary")
    return dict(expected)


def _response_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
    key_item: Mapping[str, Any],
    worksheet: Mapping[str, Any],
    expected_auditor: str,
) -> list[str]:
    if (
        set(response) != AUDIT_RESPONSE_FIELDS
        or response.get("response_version") != AUDIT_VERSION
    ):
        return ["audit response contract is invalid"]
    errors = []
    if response.get("response_sha256") != audit_response_sha256(response):
        errors.append("audit response checksum does not match")
    errors.extend(_response_binding_errors(response, task, worksheet))
    if response.get("decision") not in AUDIT_DECISIONS:
        errors.append("audit response decision is invalid")
    errors.extend(_reviewer_errors(response, key_item, expected_auditor))
    errors.extend(_evidence_errors(response, task))
    errors.extend(_decision_consistency_errors(response))
    if response.get("outcomes_consulted") is not False:
        errors.append("audit response is outcome exposed")
    return errors


def _response_binding_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
    worksheet: Mapping[str, Any],
) -> list[str]:
    fields = (
        "audit_task_id",
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "event_id",
    )
    errors = []
    if any(response.get(field) != task.get(field) for field in fields):
        errors.append("audit response task binding does not match")
    if response.get("worksheet_sha256") != worksheet.get("worksheet_sha256"):
        errors.append("audit response worksheet binding does not match")
    return errors


def _reviewer_errors(
    response: Mapping[str, Any],
    key_item: Mapping[str, Any],
    expected_auditor: str,
) -> list[str]:
    reviewer = response.get("reviewer_id")
    errors = []
    if not isinstance(reviewer, str) or not reviewer.strip():
        errors.append("audit reviewer is invalid")
    if reviewer != expected_auditor:
        errors.append("audit response auditor identity does not match expected")
    if reviewer == key_item.get("primary_reviewer_id"):
        errors.append("audit reviewer must be distinct from primary")
    rationale = response.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("audit rationale is required")
    return errors


def _evidence_errors(response: Mapping[str, Any], task: Mapping[str, Any]) -> list[str]:
    available = {
        raw.get("source_url")
        for packet in task.get("evidence", [])
        if isinstance(packet, Mapping)
        for raw in packet.get("raw_evidence", [])
        if isinstance(raw, Mapping)
    }
    citations = response.get("evidence_citations")
    if (
        not isinstance(citations, list)
        or not citations
        or len(set(citations)) != len(citations)
        or any(citation not in available for citation in citations)
    ):
        return ["audit response citations must be task-local"]
    return []


def _decision_consistency_errors(response: Mapping[str, Any]) -> list[str]:
    decision = response.get("decision")
    expected_tier = {
        "accept_confirmed": "confirmed",
        "accept_observed": "observed",
    }.get(decision, "not_applicable")
    errors = []
    if response.get("evidence_tier") != expected_tier:
        errors.append("audit response evidence tier does not match decision")
    expected_default = decision in {"accept_confirmed", "accept_observed"}
    if response.get("default_branch_supported") is not expected_default:
        errors.append("audit response default-branch value does not match decision")
    return errors


def _metric(responses: Sequence[Mapping[str, Any]], keys: Mapping[str, Any]) -> dict:
    count = len(responses)
    agreement = sum(
        response["decision"] == keys[response["audit_task_id"]]["primary_decision"]
        for response in responses
    )
    unresolved = sum(response["decision"] == "unresolved" for response in responses)
    return {
        "sample_count": count,
        "exact_agreement_count": agreement,
        "exact_agreement_rate": agreement / count,
        "unresolved_count": unresolved,
        "unresolved_rate": unresolved / count,
    }


def _by_stratum(
    responses: Sequence[Mapping[str, Any]],
    items: Mapping[str, Mapping[str, Any]],
    strata: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for response in responses:
        grouped[items[response["audit_task_id"]]["stratum_id"]].append(response)
    labels = {item["stratum_id"]: item for item in strata}
    return [
        {
            **{
                field: labels[stratum_id][field]
                for field in (
                    "stratum_id",
                    "primary_decision",
                    "evidence_channel",
                    "language",
                )
            },
            **_metric(grouped[stratum_id], items),
        }
        for stratum_id in sorted(grouped)
    ]


def _compiled_predecessors(
    worksheet: Mapping[str, Any], key: Mapping[str, Any]
) -> dict[str, Any]:
    source = worksheet["predecessors"]
    fields = (
        "decision_ledger_sha256",
        "tranche_set_sha256",
        "language_inventory_sha256",
    )
    if any(not isinstance(source.get(field), str) for field in fields):
        raise AdoptionAuditError("audit predecessor hashes are invalid")
    return {
        "worksheet_sha256": worksheet["worksheet_sha256"],
        "key_sha256": key["key_sha256"],
        "decision_ledger_sha256": source["decision_ledger_sha256"],
        "tranche_set_sha256": source["tranche_set_sha256"],
        "language_inventory_sha256": source["language_inventory_sha256"],
    }


def compile_adoption_reliability_responses(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    language_inventory: Mapping[str, Any],
    *,
    seed: str,
    expected_worksheet_sha256: str,
    expected_key_sha256: str,
    expected_auditor_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Rematerialize frozen inputs, then compile independently pinned responses."""
    tasks, items = _validate_artifacts(worksheet, key)
    _rematerialize(
        worksheet,
        key,
        ledger,
        tranches,
        language_inventory,
        seed=seed,
        expected_worksheet_sha256=expected_worksheet_sha256,
        expected_key_sha256=expected_key_sha256,
    )
    expected = _expected_auditors(expected_auditor_ids, tasks, items)
    ordered = _validated_responses(worksheet, key, responses, tasks, items, expected)
    document = _compiled_document(worksheet, key, ordered, items)
    return {**document, "compiled_audit_sha256": compiled_audit_sha256(document)}


def _validated_responses(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
    items: Mapping[str, Mapping[str, Any]],
    expected: Mapping[str, str],
) -> list[Mapping[str, Any]]:
    indexed = _unique_index(responses, "audit_task_id", "audit responses")
    if set(indexed) != set(tasks):
        raise AdoptionAuditError("audit responses must exactly cover worksheet tasks")
    errors = [
        error
        for audit_id, response in indexed.items()
        for error in _response_errors(
            response, tasks[audit_id], items[audit_id], worksheet, expected[audit_id]
        )
    ]
    if errors:
        raise AdoptionAuditError("; ".join(sorted(set(errors))))
    return [indexed[task["audit_task_id"]] for task in worksheet["tasks"]]


def _compiled_document(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    items: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "$schema": "sourcegraph-adoption-audit-compiled.schema.json",
        "audit_version": AUDIT_VERSION,
        "predecessors": _compiled_predecessors(worksheet, key),
        "response_count": len(responses),
        "response_set_sha256": _content_sha256(
            [response["response_sha256"] for response in responses]
        ),
        "overall": _metric(responses, items),
        "by_stratum": _by_stratum(responses, items, key["strata"]),
        "responses": deepcopy(list(responses)),
        "scip_required": False,
        "model_or_api_calls": False,
        "outcomes_consulted": False,
    }
