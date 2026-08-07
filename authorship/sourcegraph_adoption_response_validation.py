"""Validate outcome-blind Sourcegraph adoption-review responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from authorship.sourcegraph_adoption_review_contracts import (
    DECISIONS,
    FINAL_DECISIONS,
    RESPONSE_FIELDS,
    REVIEW_VERSION,
    adoption_review_response_sha256,
)


def _contract_errors(response: Mapping[str, Any]) -> list[str]:
    errors = []
    if response.get("response_version") != REVIEW_VERSION:
        errors.append("review response contract is invalid")
    if response.get("response_sha256") != adoption_review_response_sha256(response):
        errors.append("review response checksum does not match")
    return errors


def _binding_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
    tranche: Mapping[str, Any],
) -> list[str]:
    errors = []
    bindings = (
        ("task_id", "task_id"),
        ("task_sha256", "task_sha256"),
        ("canonical_repository_id", "canonical_repository_id"),
        ("event_id", "event_id"),
        ("review_role", "review_role"),
    )
    if any(response.get(left) != task.get(right) for left, right in bindings):
        errors.append("review response task binding does not match")
    if response.get("tranche_id") != tranche.get("tranche_id") or response.get(
        "tranche_number"
    ) != tranche.get("tranche_number"):
        errors.append("review response tranche binding does not match")
    return errors


def _reviewer_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
) -> list[str]:
    errors = []
    decision = response.get("decision")
    if decision not in DECISIONS:
        errors.append("review response decision is invalid")
    if task.get("review_role") == "resolver" and decision not in FINAL_DECISIONS:
        errors.append("resolver decision must be final")
    if response.get("outcomes_consulted") is not False:
        errors.append("review response is outcome exposed")
    if (
        not isinstance(response.get("reviewer_id"), str)
        or not response["reviewer_id"].strip()
    ):
        errors.append("review response reviewer is invalid")
    if (
        not isinstance(response.get("rationale"), str)
        or not response["rationale"].strip()
    ):
        errors.append("review response rationale is required")
    return errors


def _evidence_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
) -> list[str]:
    errors = []
    decision = response.get("decision")
    citations = response.get("evidence_citations")
    available_citations = {
        item["source_url"]
        for packet in task.get("evidence", [])
        for item in packet.get("raw_evidence", [])
    }
    if (
        not isinstance(citations, list)
        or not citations
        or any(citation not in available_citations for citation in citations)
    ):
        errors.append("review response evidence citations are invalid")
    expected_tier = {
        "accept_confirmed": "confirmed",
        "accept_observed": "observed",
    }.get(decision, "not_applicable")
    if response.get("evidence_tier") != expected_tier:
        errors.append("review response evidence tier does not match decision")
    if (
        decision in {"accept_confirmed", "accept_observed"}
        and response.get("default_branch_supported") is not True
    ):
        errors.append("accepted review lacks default-branch support")
    if not isinstance(response.get("default_branch_supported"), bool):
        errors.append("review response default-branch field is invalid")
    return errors


def response_errors(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
    tranche: Mapping[str, Any],
) -> list[str]:
    """Return every response-contract error in stable validation order."""
    if set(response) != RESPONSE_FIELDS:
        return ["review response contract is invalid"]
    return [
        *_contract_errors(response),
        *_binding_errors(response, task, tranche),
        *_reviewer_errors(response, task),
        *_evidence_errors(response, task),
    ]
