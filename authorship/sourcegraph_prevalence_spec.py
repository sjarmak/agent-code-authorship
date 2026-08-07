"""Outcome-blind frozen specification for Sourcegraph prevalence estimation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SPEC_VERSION = 1
SEED = 20260730


class PrevalenceSpecificationError(ValueError):
    """Raised when prerequisite artifacts cannot define the frozen analysis."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def prevalence_analysis_spec_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in document.items()
        if key != "prevalence_analysis_spec_sha256"
    }
    return _sha256(payload)


def _validate_inputs(
    protocol: Mapping[str, Any],
    features: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> list[str]:
    errors = []
    if protocol.get("status") != "preregistered":
        errors.append("protocol is not preregistered")
    names = features.get("names")
    if not isinstance(names, list) or not names:
        errors.append("feature manifest has no frozen feature names")
    if target_plan.get("outcomes_consulted") is not False:
        errors.append("target plan is not outcome blind")
    if target_plan.get("primary_weight") != ("line_count_times_file_sampling_weight"):
        errors.append("target plan primary weight differs from the frozen estimand")
    if authorship.get("status") != "complete":
        errors.append("authorship materialization is incomplete")
    for document, field, label in (
        (authorship, "authorship_materialization_sha256", "authorship materialization"),
        (era, "era_study_execution_sha256", "era study"),
        (cohort, "cohort_freeze_sha256", "cohort freeze"),
    ):
        if not isinstance(document.get(field), str):
            errors.append(f"{label} hash is missing")
    return errors


def _evidence_tiers() -> dict[str, Any]:
    return {
        "H1": {
            "human_reference": "H1_attested_human",
            "agent_reference": "all_confirmed_agent_units",
        },
        "H2": {
            "human_reference": "H2_policy_human",
            "agent_reference": "all_confirmed_agent_units",
        },
        "H3": {
            "human_reference": "H3_contemporary_pre_adoption",
            "agent_reference": "matched_agent_hunk_sha256s_only",
        },
    }


def _model() -> dict[str, Any]:
    return {
        "family": "l2_logistic_regression",
        "ridge": 3.0,
        "cross_validation_unit": "repository",
        "cross_fit_folds": 5,
        "score_mixture_bins": 20,
        "seed": SEED,
        "nonlinear_models_role": "diagnostic_only",
    }


def _diagnostics() -> dict[str, Any]:
    return {
        "leave_one_target_repository_out": True,
        "synthetic_mixture": {
            "design": "repository_held_out_before_model_fit",
            "shares": [0.0, 0.25, 0.5, 0.75, 1.0],
            "groups_per_mixture": 8,
            "trials_per_share": 20,
            "interval_replicates": 200,
        },
        "required_era_diagnostics": True,
    }


def _estimands() -> dict[str, Any]:
    return {
        "primary": {
            "name": "line_weighted_current_code_prevalence",
            "unit_weight": "line_count_times_file_sampling_weight",
        },
        "secondary": {
            "name": "equal_repository_current_code_prevalence",
            "repository_total_weight": 1.0,
        },
    }


def _uncertainty() -> dict[str, Any]:
    return {
        "bootstrap_unit": "repository",
        "bootstrap_replicates": 2000,
        "interval": "percentile_95",
    }


def _era_adjustment() -> dict[str, Any]:
    return {
        "required_for_headline": True,
        "failed_or_unavailable_result": "not_identified",
        "unadjusted_estimates_role": "diagnostic_only",
    }


def _contamination_grid(lower: float, upper: float, step: float) -> list[float]:
    count = round((upper - lower) / step)
    return [round(lower + index * step, 10) for index in range(count + 1)]


def _partial_identification(protocol: Mapping[str, Any]) -> dict[str, Any]:
    frozen = protocol["authorship"]["partial_identification"]
    tiers = protocol["human_evidence"]["tiers"]
    step = frozen["contamination_grid_step"]
    return {
        **dict(frozen),
        "headline_estimand": "primary_line_weighted_only",
        "input_intervals": "identified_tier_bootstrap_intervals",
        "contamination_transform": "p=c+(1-c)*q",
        "tier_contamination_grids": {
            tier: _contamination_grid(
                evidence["contamination_range"][0],
                evidence["contamination_range"][1],
                step,
            )
            for tier, evidence in tiers.items()
        },
    }


def _historical_anchors() -> dict[str, Any]:
    return {
        "primary_role": "excluded",
        "sensitivity_role": "era_diagnostic_only",
    }


def _sourcegraph_settings() -> dict[str, Any]:
    return {
        "target_measurement": "exact_blob_and_blame_at_pinned_revision",
        "SCIP_used": False,
        "precise_code_intelligence_used": False,
    }


def _identification_gates(protocol: Mapping[str, Any]) -> dict[str, Any]:
    frozen = protocol["identification_gates"]
    return {
        "minimum_adopters_per_language": frozen["minimum_adopters_per_language"],
        "minimum_controls_per_language": frozen["minimum_controls_per_language"],
        "minimum_agent_family_repositories": frozen[
            "minimum_agent_family_repositories"
        ],
        "minimum_repository_held_out_auc": 0.70,
        "maximum_estimator_disagreement": 0.10,
        "maximum_leave_one_repository_out_shift": 0.10,
        "maximum_bootstrap_interval_width": 0.25,
        "maximum_synthetic_mean_absolute_error": 0.10,
        "minimum_synthetic_interval_coverage": 0.90,
        "era_adjustment_diagnostics_must_pass": True,
        "failed_gate_result": "not_identified",
    }


def build_prevalence_analysis_spec(
    protocol: Mapping[str, Any],
    features: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze every discretionary analysis choice before target extraction."""
    errors = _validate_inputs(protocol, features, target_plan, authorship, era, cohort)
    if errors:
        raise PrevalenceSpecificationError("; ".join(errors))
    document = {
        "prevalence_analysis_spec_version": SPEC_VERSION,
        "status": "frozen_before_target_outcome_extraction",
        "outcomes_consulted": False,
        "protocol_sha256": protocol.get("protocol_sha256"),
        "feature_manifest_sha256": _sha256(features),
        "target_unit_plan_sha256": target_plan.get("target_unit_plan_sha256"),
        "target_plan_document_sha256": _sha256(target_plan),
        "input_pins": {
            "authorship_materialization_sha256": authorship[
                "authorship_materialization_sha256"
            ],
            "era_study_execution_sha256": era["era_study_execution_sha256"],
            "cohort_freeze_sha256": cohort["cohort_freeze_sha256"],
        },
        "languages": list(protocol["languages"]),
        "features": list(features["names"]),
        "evidence_tiers": _evidence_tiers(),
        "estimands": _estimands(),
        "model": _model(),
        "uncertainty": _uncertainty(),
        "diagnostics": _diagnostics(),
        "identification_gates": _identification_gates(protocol),
        "era_adjustment": _era_adjustment(),
        "partial_identification": _partial_identification(protocol),
        "historical_anchors": _historical_anchors(),
        "sourcegraph": _sourcegraph_settings(),
    }
    return {
        **document,
        "prevalence_analysis_spec_sha256": prevalence_analysis_spec_sha256(document),
    }


def validate_prevalence_analysis_spec(
    specification: Mapping[str, Any],
    protocol: Mapping[str, Any],
    features: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> list[str]:
    errors = []
    if specification.get("prevalence_analysis_spec_sha256") != (
        prevalence_analysis_spec_sha256(specification)
    ):
        errors.append("prevalence analysis specification SHA-256 does not match")
    try:
        expected = build_prevalence_analysis_spec(
            protocol, features, target_plan, authorship, era, cohort
        )
    except PrevalenceSpecificationError as error:
        return [*errors, str(error)]
    if specification.get("feature_manifest_sha256") != _sha256(features):
        errors.append("feature manifest does not match the frozen specification")
    if _canonical_json(specification) != _canonical_json(expected):
        errors.append("frozen specification differs from independently rebuilt form")
    return errors


def _expected_structure(features: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "prevalence_analysis_spec_version": SPEC_VERSION,
        "status": "frozen_before_target_outcome_extraction",
        "outcomes_consulted": False,
        "languages": ["Python", "Go"],
        "features": list(features.get("names", [])),
        "evidence_tiers": _evidence_tiers(),
        "estimands": _estimands(),
        "model": _model(),
        "uncertainty": _uncertainty(),
        "diagnostics": _diagnostics(),
        "identification_gates": {
            "minimum_adopters_per_language": 20,
            "minimum_controls_per_language": 10,
            "minimum_agent_family_repositories": 8,
            "minimum_repository_held_out_auc": 0.70,
            "maximum_estimator_disagreement": 0.10,
            "maximum_leave_one_repository_out_shift": 0.10,
            "maximum_bootstrap_interval_width": 0.25,
            "maximum_synthetic_mean_absolute_error": 0.10,
            "minimum_synthetic_interval_coverage": 0.90,
            "era_adjustment_diagnostics_must_pass": True,
            "failed_gate_result": "not_identified",
        },
        "era_adjustment": _era_adjustment(),
        "partial_identification": {
            "enabled": True,
            "method": "union_over_evidence_tiers_and_contamination_grid",
            "contamination_grid_step": 0.05,
            "maximum_headline_width": 0.3,
            "failed_width_result": "not_identified",
            "headline_estimand": "primary_line_weighted_only",
            "input_intervals": "identified_tier_bootstrap_intervals",
            "contamination_transform": "p=c+(1-c)*q",
            "tier_contamination_grids": {
                "H1": [0.0],
                "H2": [0.0, 0.05, 0.1, 0.15, 0.2],
                "H3": [0.0, 0.05, 0.1, 0.15, 0.2, 0.25],
            },
        },
        "historical_anchors": _historical_anchors(),
        "sourcegraph": _sourcegraph_settings(),
    }


def validate_prevalence_analysis_spec_structure(
    specification: Mapping[str, Any], features: Mapping[str, Any]
) -> list[str]:
    """Reject a self-rehashed specification that changes frozen constants."""
    errors = [
        f"frozen specification {field} differs"
        for field, expected in _expected_structure(features).items()
        if specification.get(field) != expected
    ]
    if specification.get("feature_manifest_sha256") != _sha256(features):
        errors.append("feature manifest does not match the frozen specification")
    pins = specification.get("input_pins")
    required_pins = {
        "authorship_materialization_sha256",
        "era_study_execution_sha256",
        "cohort_freeze_sha256",
    }
    if (
        not isinstance(pins, Mapping)
        or set(pins) != required_pins
        or any(not isinstance(value, str) for value in pins.values())
    ):
        errors.append("frozen specification input pins are invalid")
    return errors
