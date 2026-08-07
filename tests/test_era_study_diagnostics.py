from authorship.era_study_diagnostics import build_study_diagnostics


def _repository(repository_id, feature_shift):
    return {
        "repository_id": repository_id,
        "language": "Python",
        "role": "never_adopter_control",
        "adoption_period": None,
        "policy_effective_period": None,
        "observations": [
            {
                "period": 0,
                "calendar_period": 0,
                "feature_value": float(index),
                "change_size": 0,
            }
            for index in [0]
        ]
        + [
            {
                "period": 1,
                "calendar_period": 1,
                "feature_value": float(feature_shift),
                "change_size": 0,
            }
        ],
    }


def _panels(repository_count=6):
    repositories = [
        _repository(f"control-{index}", index / 10) for index in range(repository_count)
    ]
    return {
        feature: {
            "panel_version": 1,
            "feature_id": feature,
            "estimand_id": "whole_repository_adoption_effect",
            "unit": "repository_calendar_period_introduced_code",
            "repositories": repositories,
        }
        for feature in ("feature_a", "feature_b")
    }


def test_reports_repository_heldout_auc_and_feature_shifts():
    diagnostics = build_study_diagnostics(_panels())
    python = diagnostics["languages"]["Python"]

    assert python["status"] == "reported"
    assert python["passed"] is True
    assert 0 <= python["repository_held_out_era_prediction"]["auc"] <= 1
    assert len(python["featurewise_standardized_mean_shift"]["features"]) == 2
    assert (
        diagnostics["historical_anchor_sensitivity"]["status"]
        == "deferred_to_exact_authorship_prevalence"
    )


def test_too_few_control_repositories_fails_closed():
    diagnostics = build_study_diagnostics(_panels(repository_count=4))

    assert diagnostics["languages"]["Python"]["status"] == "failed"
    assert diagnostics["languages"]["Python"]["passed"] is False
