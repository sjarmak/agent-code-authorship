"""Semantic validation for the contemporary-label protocol amendment."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any


class AmendmentError(ValueError):
    """Raised when the additive protocol amendment weakens a locked control."""


def _get(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_amendment(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    exact = {
        "amendment_version": 2,
        "status": "preregistered",
        "parent.protocol_version": 1,
        "parent.result_status": "not_identified",
        "selection.outcome_blind": True,
        "selection.classifier_outputs_consulted": False,
        "labels.human.unit": "commit_set",
        "labels.human.introduced_on_or_after": "2024-01-01",
        "labels.human.introduced_on_or_before": "2026-07-24",
        "labels.human.evidence_tier": 1,
        "labels.agent.evidence_tier": 1,
        "grouping.unit": "repository",
        "roles.allow_target_overlap": False,
        "roles.allow_cross_label_overlap": False,
        "historical_code.introduced_before": "2023-01-01",
        "historical_code.role": "diagnostic_and_sensitivity_only",
    }
    for path, expected in exact.items():
        actual = _get(document, path)
        if actual == expected:
            continue
        messages = {
            "status": "amendment status must be preregistered",
            "labels.human.introduced_on_or_after": (
                "primary human labels must be introduced on or after 2024-01-01"
            ),
            "labels.human.evidence_tier": "human evidence must be Tier 1",
            "grouping.unit": "labels must be grouped by repository",
            "roles.allow_target_overlap": (
                "target/reference overlap must remain forbidden"
            ),
            "historical_code.role": (
                "pre-2023 code may be used only for diagnostics and sensitivity"
            ),
        }
        errors.append(messages.get(path, f"{path} must equal {expected!r}"))

    human_claims = _get(document, "labels.human.required_claims")
    if not isinstance(human_claims, list) or "no_ai_or_llm_assistance" not in human_claims:
        errors.append(
            "human attestation must explicitly claim no AI or LLM assistance"
        )
    required_human_claims = {
        "attestor_identity_and_authority",
        "exact_repository",
        "exact_commit_ids",
        "no_ai_or_llm_assistance",
        "scope_includes_code_and_tests",
    }
    if isinstance(human_claims, list) and not required_human_claims.issubset(
        human_claims
    ):
        errors.append("human attestation is missing required scope claims")

    minimum = _get(
        document, "identification.minimum_substantial_groups_per_side"
    )
    if not isinstance(minimum, int) or minimum < 5:
        errors.append("minimum substantial groups per side must be >= 5")
    validation = _get(
        document, "identification.reserved_validation_groups_per_side"
    )
    if not isinstance(validation, int) or validation < 1:
        errors.append("at least one validation group per side is required")
    lines = _get(document, "identification.minimum_surviving_lines_per_group")
    if not isinstance(lines, int) or lines < 2000:
        errors.append("minimum surviving lines per group must be >= 2000")

    parent_hashes = _get(document, "parent.sha256")
    required_hashes = {"protocol", "failed_result", "target_manifest"}
    if not isinstance(parent_hashes, dict) or set(parent_hashes) != required_hashes:
        errors.append("parent hashes must pin protocol, failed result, and targets")
    elif any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in parent_hashes.values()
    ):
        errors.append("parent hashes must be lowercase SHA-256 values")
    return errors


def load_amendment(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_amendment(document)
    if errors:
        raise AmendmentError("; ".join(errors))
    return document


def validate_parent_artifacts(
    document: dict[str, Any], repository_root: Path
) -> list[str]:
    paths = {
        "protocol": repository_root / "study" / "protocol.v1.json",
        "failed_result": repository_root / "results" / "replication.v1.json",
        "target_manifest": repository_root / "study" / "targets.v1.json",
    }
    expected = _get(document, "parent.sha256")
    errors = []
    if not isinstance(expected, dict):
        return ["parent hashes are unavailable"]
    for name, path in paths.items():
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            errors.append(f"parent {name} artifact unavailable: {error}")
            continue
        if expected.get(name) != actual:
            errors.append(f"parent {name} SHA-256 mismatch")
    return errors
