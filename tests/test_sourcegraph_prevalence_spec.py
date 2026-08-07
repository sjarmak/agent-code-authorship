from copy import deepcopy

from authorship.sourcegraph_prevalence_spec import (
    build_prevalence_analysis_spec,
    prevalence_analysis_spec_sha256,
    validate_prevalence_analysis_spec,
)


def _protocol():
    return {
        "protocol_version": 3,
        "status": "preregistered",
        "protocol_sha256": "a" * 64,
        "languages": ["Python", "Go"],
        "authorship": {
            "primary_model": "l2_logistic_regression",
            "primary_weighting": "line",
            "secondary_weighting": "repository",
            "cross_validation_unit": "repository",
            "bootstrap_unit": "repository",
            "partial_identification": {
                "enabled": True,
                "method": "union_over_evidence_tiers_and_contamination_grid",
                "contamination_grid_step": 0.05,
                "maximum_headline_width": 0.3,
                "failed_width_result": "not_identified",
            },
        },
        "human_evidence": {
            "tiers": {
                "H1": {"contamination_range": [0.0, 0.0]},
                "H2": {"contamination_range": [0.0, 0.2]},
                "H3": {"contamination_range": [0.0, 0.25]},
            }
        },
        "identification_gates": {
            "minimum_adopters_per_language": 20,
            "minimum_controls_per_language": 10,
            "minimum_agent_family_repositories": 8,
            "failed_gate_result": "not_identified",
        },
    }


def _features():
    return {
        "schema_version": 1,
        "status": "frozen",
        "names": ["log_lines", "is_python"],
    }


def _target_plan():
    return {
        "target_unit_plan_version": 1,
        "status": "blocked_missing_indexed_revision",
        "target_unit_plan_sha256": "b" * 64,
        "outcomes_consulted": False,
        "primary_weight": "line_count_times_file_sampling_weight",
        "secondary_weight": "equal_repository",
    }


def _lineage_inputs():
    return (
        {"status": "complete", "authorship_materialization_sha256": "c" * 64},
        {"headline_inference_allowed": False, "era_study_execution_sha256": "d" * 64},
        {"cohort_freeze_sha256": "e" * 64, "agent_family_gates": []},
    )


def test_spec_freezes_every_analysis_choice_before_target_outcomes():
    result = build_prevalence_analysis_spec(
        _protocol(), _features(), _target_plan(), *_lineage_inputs()
    )

    assert result["status"] == "frozen_before_target_outcome_extraction"
    assert result["outcomes_consulted"] is False
    assert result["features"] == ["log_lines", "is_python"]
    assert result["evidence_tiers"]["H3"]["agent_reference"] == (
        "matched_agent_hunk_sha256s_only"
    )
    assert result["estimands"]["primary"]["unit_weight"] == (
        "line_count_times_file_sampling_weight"
    )
    assert result["estimands"]["secondary"]["repository_total_weight"] == 1.0
    assert result["model"]["family"] == "l2_logistic_regression"
    assert result["model"]["ridge"] == 3.0
    assert result["uncertainty"]["bootstrap_replicates"] == 2000
    assert result["diagnostics"]["synthetic_mixture"]["shares"] == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
    ]
    assert result["era_adjustment"]["required_for_headline"] is True
    assert result["partial_identification"] == {
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
    }
    assert result["historical_anchors"]["primary_role"] == "excluded"
    assert result["input_pins"]["authorship_materialization_sha256"] == "c" * 64
    assert result["input_pins"]["era_study_execution_sha256"] == "d" * 64
    assert (
        validate_prevalence_analysis_spec(
            result, _protocol(), _features(), _target_plan(), *_lineage_inputs()
        )
        == []
    )


def test_spec_validation_detects_rehashed_drift_and_wrong_inputs():
    result = build_prevalence_analysis_spec(
        _protocol(), _features(), _target_plan(), *_lineage_inputs()
    )
    forged = deepcopy(result)
    forged["model"]["ridge"] = 0.0
    forged["prevalence_analysis_spec_sha256"] = prevalence_analysis_spec_sha256(forged)

    errors = validate_prevalence_analysis_spec(
        forged, _protocol(), _features(), _target_plan(), *_lineage_inputs()
    )
    assert any("frozen specification" in error for error in errors)

    changed_features = deepcopy(_features())
    changed_features["names"].append("is_go")
    errors = validate_prevalence_analysis_spec(
        result, _protocol(), changed_features, _target_plan(), *_lineage_inputs()
    )
    assert any("feature manifest" in error for error in errors)
