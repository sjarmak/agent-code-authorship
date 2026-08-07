"""Constants and checksums for the Sourcegraph AI-ban review artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

REVIEW_VERSION = 3
CONTROL_REPOSITORY_COUNT = 17
DECISIONS = frozenset({"accept_policy", "reject", "insufficient", "ambiguous"})
FINAL_DECISIONS = DECISIONS - {"ambiguous"}
REVIEW_ROLES = ("primary", "secondary", "resolver")
RESPONSE_FIELDS = frozenset(
    {
        "response_version",
        "worksheet_sha256",
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "event_id",
        "review_role",
        "reviewer_id",
        "decision",
        "default_branch_supported",
        "policy_scope",
        "evidence_tier",
        "rationale",
        "evidence_citations",
        "outcomes_consulted",
        "response_sha256",
    }
)
EXCLUSION_REASONS = frozenset(
    {
        "direct_index_only",
        "missing_frozen_case",
        "missing_index_manifest_record",
        "missing_sg_evals_mirror",
        "no_frozen_ai_ban_candidate",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _sha_without(document: Mapping[str, Any], field: str) -> str:
    return _content_sha256(
        {key: value for key, value in document.items() if key != field}
    )


def ai_ban_target_manifest_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "target_manifest_sha256")


def ai_ban_worksheet_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "worksheet_sha256")


def ai_ban_task_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "task_sha256")


def ai_ban_response_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "response_sha256")


def ai_ban_ledger_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "decision_ledger_sha256")
