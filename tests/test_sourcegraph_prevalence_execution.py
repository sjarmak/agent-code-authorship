from copy import deepcopy

import numpy as np

import authorship.sourcegraph_prevalence_execution as execution
from authorship.sourcegraph_prevalence_execution import (
    PrevalenceExecutionError,
    apply_identification_gates,
    partial_identification_envelopes,
    prevalence_execution_sha256,
    repository_heldout_split,
    resample_repository_clusters,
    run_prevalence_analysis,
    validate_prevalence_execution,
)
from authorship.sourcegraph_prevalence_spec import build_prevalence_analysis_spec
from authorship.sourcegraph_authorship_materialization import (
    authorship_materialization_sha256,
)
from authorship.sourcegraph_cohort_validation import cohort_freeze_sha256
from authorship.sourcegraph_target_materialization import (
    target_materialization_sha256,
)
from authorship.sourcegraph_target_plan import target_unit_plan_sha256


def _spec_inputs():
    protocol = {
        "protocol_version": 3,
        "status": "preregistered",
        "protocol_sha256": "a" * 64,
        "languages": ["Python", "Go"],
        "authorship": {
            "partial_identification": {
                "enabled": True,
                "method": "union_over_evidence_tiers_and_contamination_grid",
                "contamination_grid_step": 0.05,
                "maximum_headline_width": 0.3,
                "failed_width_result": "not_identified",
            }
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
    features = {"status": "frozen", "names": ["f1", "f2"]}
    plan_document = {
        "outcomes_consulted": False,
        "primary_weight": "line_count_times_file_sampling_weight",
        "repositories": [
            {"repository_id": "target/a"},
            *[{"repository_id": f"target/{index}"} for index in range(149)],
        ],
    }
    plan = {
        **plan_document,
        "target_unit_plan_sha256": target_unit_plan_sha256(plan_document),
    }
    return protocol, features, plan


def test_gate_failure_forces_not_identified_despite_good_model_diagnostics():
    result = apply_identification_gates(
        tier="H3",
        counts={"human_repositories": 19, "target_repositories": 150},
        point_gates={
            "minimum_auc": True,
            "plausible_mixture": True,
            "maximum_estimator_disagreement": True,
        },
        diagnostic_gates={
            "maximum_leave_one_repository_out_shift": True,
            "maximum_bootstrap_interval_width": True,
            "maximum_synthetic_mean_absolute_error": True,
            "minimum_synthetic_interval_coverage": True,
        },
        era_diagnostics_identified=False,
        era_adjustment_applied=False,
        agent_family_identified=True,
        target_complete=True,
        minimum_adopters=20,
        minimum_controls=10,
    )

    assert result["gates"]["minimum_reference_repositories"] is False
    assert result["gates"]["era_adjustment_diagnostics_identified"] is False
    assert result["gates"]["era_adjustment_applied_to_model_input"] is False
    assert result["identified"] is False
    assert result["status"] == "not_identified"


def test_repository_holdout_never_splits_a_repository_across_model_roles():
    labels = np.asarray([1, 0, 1, 0, 1, 0, 1, 0], dtype=float)
    groups = np.asarray(["a", "a", "b", "b", "c", "c", "d", "d"], dtype=object)

    development, validation = repository_heldout_split(labels, groups)

    assert set(groups[development]) & set(groups[validation]) == set()
    assert set(labels[development]) == {0.0, 1.0}
    assert set(labels[validation]) == {0.0, 1.0}


def test_repository_bootstrap_keeps_every_repositories_labels_together():
    x = np.asarray([[1.0], [-1.0], [2.0], [-2.0]])
    labels = np.asarray([1.0, 0.0, 1.0, 0.0])
    weights = np.ones(4)
    groups = np.asarray(["a", "a", "b", "b"], dtype=object)

    sampled = resample_repository_clusters(
        x,
        labels,
        weights,
        groups,
        np.random.default_rng(7),
        prefix="reference",
    )

    sampled_labels, sampled_groups = sampled[1], sampled[3]
    for group in set(sampled_groups):
        assert set(sampled_labels[sampled_groups == group]) == {0.0, 1.0}


def test_repository_bootstrap_keeps_duplicate_draws_in_one_holdout_group():
    values = np.arange(6, dtype=float).reshape(-1, 1)
    labels = np.asarray([1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    weights = np.ones(6)
    groups = np.asarray([f"r{index}" for index in range(6)], dtype=object)

    sampled_values, _, _, sampled_groups = resample_repository_clusters(
        values,
        labels,
        weights,
        groups,
        np.random.default_rng(0),
        prefix="reference",
    )

    assert len(sampled_values) == len(values)
    assert len(set(sampled_values[:, 0])) < len(sampled_values)
    for value in set(sampled_values[:, 0]):
        assert len(set(sampled_groups[sampled_values[:, 0] == value])) == 1


def test_synthetic_mixture_never_assigns_one_repository_to_both_classes():
    heldout = (
        np.asarray([0.9, 0.1, 0.8, 0.2]),
        np.asarray([1.0, 0.0, 1.0, 0.0]),
        np.ones(4),
        np.asarray(["a", "a", "b", "b"], dtype=object),
    )

    _, _, assignments = execution._select_synthetic_groups(
        heldout,
        [1.0, 0.0, 1.0, 0.0],
        np.random.default_rng(4),
        trial_id="trial",
    )

    labels_by_repository = {}
    for assignment in assignments:
        labels_by_repository.setdefault(assignment["repository"], set()).add(
            assignment["label"]
        )
    assert all(len(labels) == 1 for labels in labels_by_repository.values())


def _unit(repo, digest, label, value):
    return {
        "repository_id": repo,
        "repo": repo,
        "content_sha256": digest,
        "hunk_sha256": digest,
        "authorship_role": label,
        "language": "Python",
        "line_count": 2,
        "weighted_line_count": 2,
        "feature_values": {"f1": value, "f2": 1 - value},
    }


def _inputs():
    protocol, features, plan = _spec_inputs()
    authorship = {
        "status": "complete",
        "agent_units": [
            _unit("agent/a", "1" * 64, "agent", 0.9),
            _unit("agent/b", "2" * 64, "agent", 0.8),
        ],
        "human_units": {
            "H1_attested_human": [],
            "H2_policy_human": [
                _unit("human/a", "3" * 64, "human", 0.1),
            ],
            "H3_contemporary_pre_adoption": [
                _unit("agent/a", "4" * 64, "human", 0.2),
            ],
        },
        "h3_matching": {
            "matches": [
                {
                    "agent_hunk_sha256": "1" * 64,
                    "human_hunk_sha256": "4" * 64,
                }
            ]
        },
    }
    authorship["authorship_materialization_sha256"] = authorship_materialization_sha256(
        authorship
    )
    target = {
        "status": "complete",
        "repository_count": 150,
        "processed_repository_count": 150,
        "observed_repository_count": 1,
        "zero_unit_repositories": [f"target/{index}" for index in range(149)],
        "unit_count": 1,
        "line_count": 2,
        "weighted_line_count": 2,
        "repository_unit_counts": {
            "target/a": 1,
            **{f"target/{index}": 0 for index in range(149)},
        },
        "target_unit_plan_sha256": plan["target_unit_plan_sha256"],
        "target_execution_sha256": "d" * 64,
        "units": [
            _unit("target/a", "5" * 64, "unknown", 0.5),
        ],
    }
    target["target_materialization_sha256"] = target_materialization_sha256(target)
    era = {
        "headline_inference_allowed": False,
    }
    era["era_study_execution_sha256"] = execution._artifact_sha256(
        era, "era_study_execution_sha256"
    )
    cohort = {
        "agent_family_gates": [
            {"agent_family": "A", "status": "identified"},
        ],
    }
    cohort["cohort_freeze_sha256"] = cohort_freeze_sha256(cohort)
    spec = build_prevalence_analysis_spec(
        protocol, features, plan, authorship, era, cohort
    )
    return spec, plan, authorship, target, era, cohort, features


def _fake_cell(arrays, specification):
    return {
        "status": "numeric_diagnostic_complete",
        "diagnostic_estimate": {
            "mixture_share": 0.4,
            "threshold_adjusted_share": 0.42,
        },
        "point_gates": {
            "minimum_auc": True,
            "plausible_mixture": True,
            "maximum_estimator_disagreement": True,
        },
        "diagnostic_gates": {
            "maximum_leave_one_repository_out_shift": True,
            "maximum_bootstrap_interval_width": True,
            "maximum_synthetic_mean_absolute_error": True,
            "minimum_synthetic_interval_coverage": True,
        },
        "diagnostics": {},
    }


def test_execution_retains_numeric_diagnostics_but_never_promotes_failed_era(
    monkeypatch,
):
    monkeypatch.setattr(execution, "_estimate_cell", _fake_cell)
    inputs = _inputs()

    result = run_prevalence_analysis(*inputs)

    assert result["headline_inference_allowed"] is False
    h2_python = result["results"]["H2"]["Python"]["line"]
    assert h2_python["diagnostic_estimate"]["mixture_share"] == 0.4
    assert h2_python["identification"]["identified"] is False
    assert (
        h2_python["identification"]["gates"]["era_adjustment_diagnostics_identified"]
        is False
    )
    assert (
        h2_python["identification"]["gates"]["era_adjustment_applied_to_model_input"]
        is False
    )
    assert h2_python["headline_estimate"] is None
    assert result["results"]["H1"]["Python"]["line"]["status"] == (
        "not_identified_no_human_reference"
    )


def test_validator_detects_rehashed_result_drift(monkeypatch):
    monkeypatch.setattr(execution, "_estimate_cell", _fake_cell)
    inputs = _inputs()
    result = run_prevalence_analysis(*inputs)
    forged = deepcopy(result)
    forged["results"]["H2"]["Python"]["line"]["diagnostic_estimate"][
        "mixture_share"
    ] = 0.99
    forged["prevalence_execution_sha256"] = prevalence_execution_sha256(forged)

    errors = validate_prevalence_execution(forged, *inputs)

    assert any("independently recomputed" in error for error in errors)


def _numeric_arrays():
    reference_x = []
    labels = []
    groups = []
    for index in range(10):
        reference_x.extend(
            [
                [2.0 + index / 100, 0.1],
                [1.8 + index / 100, 0.2],
                [-2.0 + index / 100, -0.1],
                [-1.8 + index / 100, -0.2],
            ]
        )
        labels.extend([1.0, 1.0, 0.0, 0.0])
        groups.extend([f"reference/{index}"] * 4)
    return {
        "reference": {
            "x": np.asarray(reference_x),
            "labels": np.asarray(labels),
            "weights": np.ones(40),
            "groups": np.asarray(groups, dtype=object),
        },
        "target": {
            "x": np.asarray(
                [
                    [2.0, 0.1],
                    [-2.0, -0.1],
                    [1.9, 0.2],
                    [-1.9, -0.2],
                    [0.0, 0.0],
                    [0.2, 0.0],
                ]
            ),
            "weights": np.ones(6),
            "groups": np.asarray(
                [
                    "target/a",
                    "target/a",
                    "target/b",
                    "target/b",
                    "target/c",
                    "target/c",
                ],
                dtype=object,
            ),
        },
    }


def test_numeric_cell_runs_repository_bootstrap_stability_and_synthetic_holdout():
    protocol, features, plan = _spec_inputs()
    authorship = {
        "status": "complete",
        "authorship_materialization_sha256": "c" * 64,
    }
    era = {
        "headline_inference_allowed": False,
        "era_study_execution_sha256": "d" * 64,
    }
    cohort = {"cohort_freeze_sha256": "e" * 64, "agent_family_gates": []}
    specification = build_prevalence_analysis_spec(
        protocol, features, plan, authorship, era, cohort
    )
    specification["uncertainty"]["bootstrap_replicates"] = 3
    synthetic = specification["diagnostics"]["synthetic_mixture"]
    synthetic.update(
        {
            "shares": [0.0, 0.5, 1.0],
            "groups_per_mixture": 2,
            "trials_per_share": 1,
            "interval_replicates": 3,
        }
    )

    result = execution._estimate_cell(_numeric_arrays(), specification)

    assert result["status"] == "numeric_diagnostic_complete"
    assert 0 <= result["diagnostic_estimate"]["mixture_share"] <= 1
    assert result["diagnostics"]["bootstrap"]["successful_replicates"] == 3
    assert (
        result["diagnostics"]["leave_one_target_repository_out"]["groups_evaluated"]
        == 3
    )
    assert result["diagnostics"]["synthetic_mixture"]["design"] == (
        "repository_held_out_joint_repository_resampling"
    )


def test_partial_identification_uses_only_primary_identified_tier_intervals():
    protocol, features, plan = _spec_inputs()
    authorship = {
        "status": "complete",
        "authorship_materialization_sha256": "c" * 64,
    }
    era = {
        "headline_inference_allowed": False,
        "era_study_execution_sha256": "d" * 64,
    }
    cohort = {"cohort_freeze_sha256": "e" * 64, "agent_family_gates": []}
    specification = build_prevalence_analysis_spec(
        protocol, features, plan, authorship, era, cohort
    )
    identified = {
        "identification": {"identified": True},
        "diagnostics": {"bootstrap": {"interval": [0.1, 0.15]}},
    }
    h2 = {
        "identification": {"identified": True},
        "diagnostics": {"bootstrap": {"interval": [0.12, 0.18]}},
    }
    secondary_only = {
        "identification": {"identified": True},
        "diagnostics": {"bootstrap": {"interval": [0.8, 0.9]}},
    }
    results = {
        tier: {
            language: {
                "line": (
                    identified
                    if tier == "H1" and language == "Python"
                    else (
                        h2
                        if tier == "H2" and language == "Python"
                        else {"identification": {"identified": False}}
                    )
                ),
                "repository": secondary_only,
            }
            for language in ("Python", "Go")
        }
        for tier in ("H1", "H2", "H3")
    }

    envelopes = partial_identification_envelopes(results, specification)

    assert envelopes["Python"]["interval"] == [0.1, 0.344]
    assert envelopes["Python"]["identified"] is True
    assert envelopes["Go"]["identified"] is False
    assert envelopes["Go"]["headline_estimate"] is None


def test_partial_identification_accepts_exact_maximum_width_boundary():
    result = execution._partial_envelope(
        [[0.1, 0.4]],
        [],
        {
            "maximum_headline_width": 0.3,
            "failed_width_result": "not_identified",
        },
    )

    assert result["interval_width"] == 0.30000000000000004
    assert result["identified"] is True


def test_execution_input_validation_fails_closed():
    inputs = list(_inputs())
    stale = deepcopy(inputs[0])
    stale["prevalence_analysis_spec_sha256"] = "0" * 64
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(stale, *inputs[1:])

    wrong_features = deepcopy(inputs[-1])
    wrong_features["names"] = ["other"]
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(*inputs[:-1], wrong_features)

    incomplete = deepcopy(inputs[2])
    incomplete["status"] = "incomplete"
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(inputs[0], inputs[1], incomplete, *inputs[3:])

    forged = deepcopy(inputs[0])
    forged["model"]["ridge"] = 0.0
    forged["prevalence_analysis_spec_sha256"] = (
        execution.prevalence_analysis_spec_sha256(forged)
    )
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(forged, *inputs[1:])

    tampered_authorship = deepcopy(inputs[2])
    tampered_authorship["agent_units"][0]["feature_values"]["f1"] = 999
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(inputs[0], inputs[1], tampered_authorship, *inputs[3:])

    swapped_counts = deepcopy(inputs[3])
    swapped_counts["repository_unit_counts"]["target/a"] = 0
    swapped_counts["repository_unit_counts"]["target/0"] = 1
    swapped_counts["zero_unit_repositories"] = [
        repository
        for repository, count in swapped_counts["repository_unit_counts"].items()
        if count == 0
    ]
    swapped_counts["target_materialization_sha256"] = target_materialization_sha256(
        swapped_counts
    )
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(
            inputs[0], inputs[1], inputs[2], swapped_counts, *inputs[4:]
        )

    substituted_repository = deepcopy(inputs[3])
    substituted_repository["repository_unit_counts"]["attacker/not-in-frame"] = (
        substituted_repository["repository_unit_counts"].pop("target/0")
    )
    substituted_repository["zero_unit_repositories"] = sorted(
        repository
        for repository, count in substituted_repository[
            "repository_unit_counts"
        ].items()
        if count == 0
    )
    substituted_repository["target_materialization_sha256"] = (
        target_materialization_sha256(substituted_repository)
    )
    with np.testing.assert_raises(PrevalenceExecutionError):
        run_prevalence_analysis(
            inputs[0],
            inputs[1],
            inputs[2],
            substituted_repository,
            *inputs[4:],
        )
