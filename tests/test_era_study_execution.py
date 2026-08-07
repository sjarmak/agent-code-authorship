import copy
import hashlib
import json

import pytest

from authorship.era_study_execution import estimate_feature_panels


def _repository(repository_id, role, values, adoption=None):
    values = dict(values)
    first_period = min(values)
    values.setdefault(first_period - 1, values[first_period])
    return {
        "repository_id": repository_id,
        "language": "Python",
        "role": role,
        "adoption_period": adoption,
        "policy_effective_period": None,
        "observations": [
            {
                "period": period,
                "calendar_period": period,
                "feature_value": value,
                "change_size": 10,
                "code_age_days": 0,
                "path_type_counts": {"source": 1},
            }
            for period, value in values.items()
        ],
    }


def _materialization(panel):
    document = {
        "era_panel_materialization_version": 1,
        "era_panel_plan_sha256": "b" * 64,
        "status": "complete",
        "period_result_count": 0,
        "period_results": [],
        "feature_count": 1,
        "panels": {"feature_a": panel},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return {
        **document,
        "era_panel_materialization_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def test_feature_panel_execution_is_deterministic_and_content_bound():
    panel = {
        "panel_version": 1,
        "feature_id": "feature_a",
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": [
            _repository("treated", "adopter", {0: 0, 1: 1, 3: 5}, adoption=2),
            _repository("never", "never_adopter_control", {0: 0, 1: 1, 3: 3}),
        ],
    }
    materialization = _materialization(panel)

    first = estimate_feature_panels(
        materialization,
        bootstrap_replicates=20,
        seed=20260729,
        minimum_adopters=1,
        minimum_controls=1,
        cross_fit_folds=1,
        analysis_mode="sensitivity",
    )
    second = estimate_feature_panels(
        materialization,
        bootstrap_replicates=20,
        seed=20260729,
        minimum_adopters=1,
        minimum_controls=1,
        cross_fit_folds=1,
        analysis_mode="sensitivity",
    )

    assert first == second
    assert (
        first["features"]["feature_a"]["languages"]["Python"]["status"] == "identified"
    )
    assert first["headline_inference_allowed"] is False
    assert first["analysis_mode"] == "sensitivity"
    assert first["diagnostics"]["languages"]["Python"]["status"] == "failed"
    assert first["era_study_execution_sha256"]


def test_incomplete_materialization_fails_closed():
    try:
        estimate_feature_panels(
            {"status": "incomplete", "panels": {}},
            bootstrap_replicates=10,
            seed=1,
        )
    except RuntimeError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("incomplete era materialization must fail closed")


def test_materialization_hash_version_and_counts_fail_closed():
    panel = {
        "panel_version": 1,
        "feature_id": "feature_a",
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": [
            _repository("treated", "adopter", {0: 0, 1: 1, 3: 5}, adoption=2),
            _repository("never", "never_adopter_control", {0: 0, 1: 1, 3: 3}),
        ],
    }
    materialization = _materialization(panel)
    stale = copy.deepcopy(materialization)
    stale["era_panel_materialization_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="materialization hash"):
        estimate_feature_panels(stale, bootstrap_replicates=10, seed=1)

    wrong_version = copy.deepcopy(materialization)
    wrong_version["era_panel_materialization_version"] = 999
    payload = {
        key: value
        for key, value in wrong_version.items()
        if key != "era_panel_materialization_sha256"
    }
    wrong_version["era_panel_materialization_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError, match="materialization version"):
        estimate_feature_panels(wrong_version, bootstrap_replicates=10, seed=1)

    wrong_count = copy.deepcopy(materialization)
    wrong_count["feature_count"] = 2
    payload = {
        key: value
        for key, value in wrong_count.items()
        if key != "era_panel_materialization_sha256"
    }
    wrong_count["era_panel_materialization_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(RuntimeError, match="declared counts"):
        estimate_feature_panels(wrong_count, bootstrap_replicates=10, seed=1)


def test_headline_mode_rejects_frozen_threshold_overrides():
    panel = {
        "panel_version": 1,
        "feature_id": "feature_a",
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": [
            _repository("treated", "adopter", {0: 0, 1: 1, 3: 5}, adoption=2),
            _repository("never", "never_adopter_control", {0: 0, 1: 1, 3: 3}),
        ],
    }

    with pytest.raises(ValueError, match="sensitivity"):
        estimate_feature_panels(
            _materialization(panel),
            bootstrap_replicates=10,
            seed=1,
            minimum_adopters=1,
            minimum_controls=1,
        )
