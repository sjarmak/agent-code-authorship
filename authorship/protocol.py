"""Load and validate the preregistered replication protocol."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_SECTIONS = (
    "estimand",
    "snapshot",
    "provenance",
    "repository_roles",
    "sampling",
    "contamination_controls",
    "features",
    "models",
    "quantification",
    "evaluation",
    "identification_gates",
    "uncertainty",
    "storage",
    "reporting",
    "workflow",
)


class ProtocolError(ValueError):
    """Raised when a study protocol violates a locked design decision."""


def _get(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_protocol(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for section in REQUIRED_SECTIONS:
        if section not in document:
            errors.append(f"missing required section: {section}")

    exact = {
        "protocol_version": 1,
        "status": "preregistered",
        "estimand.languages": ["Python", "Go"],
        "estimand.unit": "surviving_nonvendored_lines",
        "estimand.introduced_on_or_after": "2024-01-01",
        "snapshot.cutoff": "2026-07-24T23:59:59Z",
        "repository_roles.allow_role_overlap": False,
        "models.primary.kind": "l2_logistic_regression",
        "models.challenger.kind": "gradient_boosted_trees",
        "quantification.primary": "full_score_mixture",
        "quantification.robustness": "threshold_adjusted_count",
        "storage.canonical_gather_backend": "local_git",
        "storage.commit_third_party_source": False,
        "storage.nas.offload_after_verification": True,
        "reporting.failed_gate_result": "not_identified",
    }
    for path, expected in exact.items():
        actual = _get(document, path)
        if actual != expected:
            if path == "estimand.languages":
                errors.append("estimand.languages must contain exactly Python and Go")
            elif path == "repository_roles.allow_role_overlap":
                errors.append("repository role overlap must be forbidden")
            else:
                errors.append(f"{path} must equal {expected!r}")

    lower_bounds = {
        "sampling.minimum_eligible_modern_lines_per_repository_group": 2000,
        "identification_gates.minimum_repository_groups_per_side": 5,
        "identification_gates.minimum_auc": 0.70,
        "evaluation.synthetic_mixtures.minimum_interval_coverage": 0.90,
    }
    for path, minimum in lower_bounds.items():
        actual = _get(document, path)
        if not isinstance(actual, (int, float)) or actual < minimum:
            errors.append(f"{path} must be >= {minimum}")

    upper_bounds = {
        "identification_gates.maximum_estimator_disagreement": 0.10,
        "identification_gates.maximum_leave_one_group_out_shift": 0.10,
        "identification_gates.maximum_bootstrap_interval_width": 0.25,
        "evaluation.synthetic_mixtures.maximum_mean_absolute_error": 0.10,
    }
    for path, maximum in upper_bounds.items():
        actual = _get(document, path)
        if not isinstance(actual, (int, float)) or actual > maximum:
            errors.append(f"{path} must be <= {maximum}")

    if _get(document, "evaluation.primary_metric") != "mixture_share_recovery":
        errors.append("evaluation.primary_metric must be mixture_share_recovery")
    if _get(document, "features.classifier_inputs") != ["source_text"]:
        errors.append("classifier inputs must be source text only")
    if _get(document, "reporting.generalize_to_all_github"):
        errors.append("reporting must not generalize to all GitHub code")
    return errors


def load_protocol(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_protocol(document)
    if errors:
        raise ProtocolError("; ".join(errors))
    return document
