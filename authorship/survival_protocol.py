"""Load and validate the preregistered agent-code survival protocol."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class SurvivalProtocolError(ValueError):
    """Raised when the survival protocol violates a locked design decision."""


def _get(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def protocol_sha256(document: dict[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "protocol_sha256"}
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_survival_protocol(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "protocol_version",
        "status",
        "purpose",
        "snapshot",
        "estimand",
        "provenance",
        "sourcegraph",
        "lineage",
        "sampling",
        "contextual_comparison",
        "analysis",
        "validation",
        "identification_gates",
        "storage",
        "reporting",
        "protocol_sha256",
    }
    for section in sorted(required - document.keys()):
        errors.append(f"missing required section: {section}")

    exact = {
        "protocol_version": 1,
        "status": "preregistered",
        "snapshot.cutoff": "2026-07-24T23:59:59Z",
        "estimand.languages": ["Python", "Go"],
        "estimand.horizons_days": [30, 90, 180, 365],
        "estimand.primary_weighting": "repository",
        "estimand.secondary_weighting": "line",
        "provenance.pool_tiers_for_headline": False,
        "sourcegraph.authoritative_lineage_source": "pinned_git_history",
        "lineage.states": [
            "introduced",
            "unchanged",
            "modified",
            "deleted",
            "unobservable",
        ],
        "sampling.maximum_repositories_per_stratum": 25,
        "sampling.minimum_attributable_lines_per_repository": 200,
        "sampling.include_all_eligible_tier_1": True,
        "sampling.outcome_blind": True,
        "contextual_comparison.label": "non_agent_attributed",
        "analysis.model": "multistate_survival",
        "analysis.bootstrap_unit": "repository",
        "reporting.failed_gate_result": "not_identified",
    }
    for path, expected in exact.items():
        actual = _get(document, path)
        if actual != expected:
            if path == "provenance.pool_tiers_for_headline":
                errors.append("provenance tiers must not be pooled for headline results")
            elif path == "contextual_comparison.label":
                errors.append(
                    "contextual comparison must be labeled non_agent_attributed"
                )
            else:
                errors.append(f"{path} must equal {expected!r}")

    lower_bounds = {
        "identification_gates.minimum_lineage_coverage": 0.8,
        "identification_gates.minimum_repositories_per_stratum": 5,
        "validation.blinded_decisions": 200,
        "validation.minimum_structural_precision": 0.9,
    }
    for path, minimum in lower_bounds.items():
        value = _get(document, path)
        if not isinstance(value, (int, float)) or value < minimum:
            errors.append(f"{path} must be >= {minimum}")

    allowed_transitions = {
        ("introduced", "unchanged"),
        ("introduced", "modified"),
        ("introduced", "deleted"),
        ("introduced", "unobservable"),
        ("unchanged", "unchanged"),
        ("unchanged", "modified"),
        ("unchanged", "deleted"),
        ("unchanged", "unobservable"),
        ("modified", "modified"),
        ("modified", "deleted"),
        ("modified", "unobservable"),
        ("unobservable", "unobservable"),
    }
    transitions = _get(document, "lineage.allowed_transitions")
    if not isinstance(transitions, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or tuple(pair) not in allowed_transitions
        for pair in transitions
    ):
        errors.append("lineage.allowed_transitions contains forbidden transitions")

    expected_digest = document.get("protocol_sha256")
    if expected_digest != protocol_sha256(document):
        errors.append("protocol_sha256 does not match canonical protocol content")
    return errors


def load_survival_protocol(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_survival_protocol(document)
    if errors:
        raise SurvivalProtocolError("; ".join(errors))
    return document
