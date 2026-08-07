import copy

import pytest

from authorship.sourcegraph_era_panel import (
    EraPanelError,
    build_era_panel_plan,
    build_feature_panels,
    materialize_exact_commit_units,
    summarize_period_diff,
)


def _freeze():
    return {
        "cohort_freeze_sha256": "1" * 64,
        "outcomes_consulted": False,
        "cohorts": {
            "adoption_event_study": {
                "primary": {
                    "languages": {
                        "Python": {"repository_ids": ["org/adopter"]},
                        "Go": {"repository_ids": []},
                    }
                },
                "control_pool": {
                    "languages": {
                        "Python": {
                            "h2_policy_repository_ids": ["org/policy"],
                            "never_observed_repository_ids": ["org/never"],
                        },
                        "Go": {
                            "h2_policy_repository_ids": [],
                            "never_observed_repository_ids": [],
                        },
                    }
                },
            },
            "human_evidence": {
                "H2_policy_human": {
                    "repositories": [
                        {
                            "repository_id": "org/policy",
                            "policy_effective_at": "2024-01-15T00:00:00Z",
                        }
                    ]
                }
            },
        },
    }


def _adoption():
    return {
        "catalog_sha256": "2" * 64,
        "outcomes_consulted": False,
        "repositories": [
            {
                "repository_id": "org/adopter",
                "language": "Python",
                "adoption_observed_at": "2025-05-10T00:00:00Z",
            },
            {
                "repository_id": "org/never",
                "language": "Python",
                "adoption_observed_at": None,
            },
        ],
    }


def _index():
    return {
        "manifest_sha256": "3" * 64,
        "outcomes_consulted": False,
        "repositories": [
            {
                "canonical_repository_id": repository_id,
                "cutoff_commit": character * 40,
                "sourcegraph": {
                    "selected_name": f"github.com/sg-evals/{repository_id.replace('/', '-')}"
                },
            }
            for repository_id, character in (
                ("org/adopter", "a"),
                ("org/policy", "b"),
                ("org/never", "c"),
            )
        ],
    }


def test_plan_freezes_staggered_adopters_and_static_controls():
    plan = build_era_panel_plan(_freeze(), _adoption(), _index())
    by_id = {row["repository_id"]: row for row in plan["repositories"]}

    assert plan["required_periods"] == [8098, 8099, 8100, 8102]
    assert by_id["org/adopter"]["role"] == "adopter"
    assert by_id["org/adopter"]["adoption_period"] == 8101
    assert by_id["org/policy"]["role"] == "h2_ai_ban_control"
    assert by_id["org/policy"]["policy_effective_period"] == 8096
    assert by_id["org/never"]["role"] == "never_adopter_control"
    assert plan["outcomes_consulted"] is False
    assert plan["sourcegraph_materialization"] == {
        "design": "exact_net_quarter_path_batches",
        "period_result_version": 4,
        "changed_files_page_size": 5000,
        "maximum_changed_file_pages": 20,
        "path_batch_size": 64,
        "required_fields": [
            "RepositoryComparison.changedFiles",
            "RepositoryComparison.fileDiffs.paths",
        ],
        "cap_result": "repository_period_incomplete",
    }


def test_complete_sourcegraph_periods_materialize_feature_panels():
    plan = build_era_panel_plan(_freeze(), _adoption(), _index())
    patch = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def greet(name: str):\n"
        '+    return f"hello {name}"\n'
    )
    periods = [
        {
            "repository_id": repository["repository_id"],
            "period": period,
            "base_oid": "d" * 40,
            "head_oid": "e" * 40,
            **summarize_period_diff(patch, "Python"),
            "complete": True,
        }
        for repository in plan["repositories"]
        for period in plan["required_periods"]
    ]

    materialization = build_feature_panels(plan, periods)
    panel = materialization["panels"]["typehint_rate"]

    assert materialization["status"] == "complete"
    assert panel["estimand_id"] == "whole_repository_adoption_effect"
    assert len(panel["repositories"]) == 3
    assert all(
        len(repository["observations"]) == 4 for repository in panel["repositories"]
    )
    observation = panel["repositories"][0]["observations"][0]
    assert observation["change_size"] == 2
    assert observation["code_age_days"] == 0
    assert observation["calendar_period"] == observation["period"]
    assert observation["path_type_counts"] == {"source": 1}


def test_incomplete_or_duplicate_periods_fail_closed():
    plan = build_era_panel_plan(_freeze(), _adoption(), _index())
    periods = [
        {
            "repository_id": repository["repository_id"],
            "period": period,
            "base_oid": "d" * 40,
            "head_oid": "e" * 40,
            **summarize_period_diff("", "Python"),
            "complete": True,
        }
        for repository in plan["repositories"]
        for period in plan["required_periods"]
    ]
    periods[0]["complete"] = False

    with pytest.raises(EraPanelError, match="incomplete"):
        build_feature_panels(plan, periods)

    duplicate = copy.deepcopy(periods[1:])
    duplicate.append(copy.deepcopy(duplicate[0]))
    with pytest.raises(EraPanelError, match="exactly cover"):
        build_feature_panels(plan, duplicate)


def test_structural_precreation_periods_produce_sparse_panels_not_zeroes():
    plan = build_era_panel_plan(_freeze(), _adoption(), _index())
    periods = [
        {
            "repository_id": repository["repository_id"],
            "period": period,
            "base_oid": "d" * 40,
            "head_oid": "e" * 40,
            **summarize_period_diff("", "Python"),
            "observed": True,
            "observation_status": "observed",
            "complete": True,
        }
        for repository in plan["repositories"]
        for period in plan["required_periods"]
    ]
    periods[0] = {
        "repository_id": periods[0]["repository_id"],
        "period": periods[0]["period"],
        "base_oid": None,
        "head_oid": None,
        "feature_values": None,
        "introduced_code_line_count": None,
        "raw_diff_sha256": None,
        "eligible_path_count": None,
        "path_type_counts": None,
        "materialization_design": "exact_net_quarter_path_batches",
        "code_age_days": None,
        "observed": False,
        "observation_status": "structural_precreation",
        "complete": True,
    }

    materialization = build_feature_panels(plan, periods)
    panel = materialization["panels"]["typehint_rate"]
    first = next(
        repository
        for repository in panel["repositories"]
        if repository["repository_id"] == periods[0]["repository_id"]
    )

    assert len(first["observations"]) == 3
    assert all(
        observation["period"] != periods[0]["period"]
        for observation in first["observations"]
    )


def test_exact_commit_units_have_real_identity_and_normalized_content_hashes():
    patch = (
        "diff --git a/main.py b/main.py\n"
        "--- a/main.py\n"
        "+++ b/main.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+x = 1   \n"
        "+print(x)\n"
    )

    units = materialize_exact_commit_units(
        repository_id="org/repo",
        commit_oid="a" * 40,
        language="Python",
        raw_diff=patch,
        authorship_role="agent",
        evidence_tier="confirmed",
    )

    assert len(units) == 1
    assert units[0]["path"] == "main.py"
    assert len(units[0]["hunk_sha256"]) == 64
    assert len(units[0]["content_sha256"]) == 64
    assert units[0]["line_count"] == 2
