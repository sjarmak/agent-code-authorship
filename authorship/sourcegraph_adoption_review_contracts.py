"""Shared identity and decision contracts for Sourcegraph adoption review."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

REVIEW_VERSION = 3
DECISIONS = frozenset(
    {
        "accept_confirmed",
        "accept_observed",
        "reject",
        "insufficient",
        "ambiguous",
    }
)
FINAL_DECISIONS = DECISIONS - {"ambiguous"}
REVIEW_ROLES = frozenset({"primary", "secondary", "resolver"})
RESPONSE_FIELDS = frozenset(
    {
        "response_version",
        "tranche_id",
        "tranche_number",
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "event_id",
        "reviewer_id",
        "review_role",
        "decision",
        "default_branch_supported",
        "evidence_tier",
        "rationale",
        "evidence_citations",
        "outcomes_consulted",
        "response_sha256",
    }
)
LEDGER_FIELDS = frozenset(
    {
        "decision_ledger_version",
        "tranche_id",
        "tranche_sha256",
        "case_index_sha256",
        "workflow_sha256",
        "prior_decision_ledger_sha256",
        "prior_decision_count",
        "response_count",
        "decision_count",
        "decisions",
        "outcomes_consulted",
        "decision_ledger_sha256",
    }
)


class AdoptionReviewError(ValueError):
    """Raised when adoption review cannot proceed without study drift."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha_without(document: Mapping[str, Any], field: str) -> str:
    content = {key: value for key, value in document.items() if key != field}
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def adoption_review_response_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "response_sha256")


def adoption_review_ledger_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "decision_ledger_sha256")


def _decision_index(
    decisions: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    indexed = {}
    reviewers: dict[tuple[str, str], set[str]] = {}
    for decision in decisions:
        if decision.get("outcomes_consulted") is not False:
            raise AdoptionReviewError("review decision is outcome exposed")
        role = decision.get("review_role")
        value = decision.get("decision")
        if role not in REVIEW_ROLES or value not in DECISIONS:
            raise AdoptionReviewError("review decision contract is invalid")
        if role == "resolver" and value not in FINAL_DECISIONS:
            raise AdoptionReviewError("resolver decision must be final")
        reviewer_id = decision.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            raise AdoptionReviewError("review decision reviewer is invalid")
        tranche_number = decision.get("tranche_number")
        if not isinstance(tranche_number, int) or tranche_number < 1:
            raise AdoptionReviewError("review decision tranche number is invalid")
        key = (
            decision.get("canonical_repository_id"),
            decision.get("event_id"),
            role,
        )
        if key in indexed:
            raise AdoptionReviewError("duplicate review role for repository event")
        event_key = key[:2]
        event_reviewers = reviewers.setdefault(event_key, set())
        if reviewer_id in event_reviewers:
            raise AdoptionReviewError(
                "primary, secondary, and resolver reviewers must be distinct"
            )
        event_reviewers.add(reviewer_id)
        indexed[key] = decision
    return indexed
