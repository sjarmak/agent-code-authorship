import numpy as np

from authorship.sourcegraph_prevalence_data import prepare_prevalence_arrays

FEATURES = ["f1", "f2"]


def _unit(repo, digest, values, *, lines=1, language="Python", **extra):
    return {
        "repository_id": repo,
        "content_sha256": digest,
        "hunk_sha256": digest,
        "language": language,
        "line_count": lines,
        "feature_values": dict(zip(FEATURES, values)),
        **extra,
    }


def _materialization():
    agents = [
        _unit("agent/a", "a" * 64, [1, 0], lines=2),
        _unit("agent/b", "b" * 64, [2, 0], lines=4),
        _unit("agent/c", "c" * 64, [3, 0], lines=8),
    ]
    h3 = [
        _unit("agent/a", "d" * 64, [0, 1], lines=3),
        _unit("agent/b", "e" * 64, [0, 2], lines=5),
    ]
    return {
        "status": "complete",
        "agent_units": agents,
        "human_units": {
            "H1_attested_human": [],
            "H2_policy_human": [_unit("human/a", "f" * 64, [0, 3], lines=6)],
            "H3_contemporary_pre_adoption": h3,
        },
        "h3_matching": {
            "matches": [
                {
                    "agent_hunk_sha256": "a" * 64,
                    "human_hunk_sha256": "d" * 64,
                },
                {
                    "agent_hunk_sha256": "b" * 64,
                    "human_hunk_sha256": "e" * 64,
                },
            ]
        },
    }


def _targets():
    return {
        "status": "complete",
        "units": [
            _unit(
                "target/a",
                "a" * 64,
                [0.4, 0.6],
                lines=10,
                weighted_line_count=20,
            ),
            _unit(
                "target/a",
                "1" * 64,
                [0.5, 0.5],
                lines=10,
                weighted_line_count=20,
            ),
            _unit(
                "target/b",
                "2" * 64,
                [0.6, 0.4],
                lines=5,
                weighted_line_count=5,
            ),
        ],
    }


def test_h3_uses_only_matched_agent_units_and_removes_target_collisions():
    result = prepare_prevalence_arrays(
        _materialization(), _targets(), FEATURES, tier="H3", language="Python"
    )

    assert result["counts"]["agent_units_before_role_separation"] == 2
    assert result["counts"]["human_units_before_role_separation"] == 2
    assert result["counts"]["agent_units"] == 1
    assert result["counts"]["human_units"] == 2
    assert result["role_separation"]["collision_hashes"] == 1
    assert result["target"]["x"].shape == (3, 2)
    assert result["reference"]["labels"].tolist() == [1.0, 0.0, 0.0]


def test_primary_and_repository_weighting_follow_frozen_estimands():
    primary = prepare_prevalence_arrays(
        _materialization(),
        _targets(),
        FEATURES,
        tier="H2",
        language="Python",
        weighting="line",
    )
    secondary = prepare_prevalence_arrays(
        _materialization(),
        _targets(),
        FEATURES,
        tier="H2",
        language="Python",
        weighting="repository",
    )

    assert primary["reference"]["weights"].tolist() == [4.0, 8.0, 6.0]
    assert primary["target"]["weights"].tolist() == [20.0, 20.0, 5.0]
    for arrays in (secondary["reference"], secondary["target"]):
        totals = {
            repo: float(arrays["weights"][arrays["groups"] == repo].sum())
            for repo in set(arrays["groups"].tolist())
        }
        assert totals == {repo: 1.0 for repo in totals}
    assert np.isclose(secondary["target"]["weights"].sum(), 2.0)


def test_historical_anchors_are_never_primary_references():
    materialization = _materialization()
    materialization["agent_units"].append(
        _unit(
            "historical/a",
            "9" * 64,
            [9, 9],
            temporal_role="historical_diagnostic",
        )
    )

    result = prepare_prevalence_arrays(
        materialization, _targets(), FEATURES, tier="H2", language="Python"
    )

    assert result["counts"]["historical_reference_units_excluded"] == 1
    assert "historical/a" not in result["reference"]["groups"]


def test_historical_exclusion_count_is_language_specific():
    materialization = _materialization()
    materialization["agent_units"].append(
        _unit(
            "historical/go",
            "8" * 64,
            [9, 9],
            language="Go",
            temporal_role="historical_diagnostic",
        )
    )

    result = prepare_prevalence_arrays(
        materialization, _targets(), FEATURES, tier="H2", language="Python"
    )

    assert result["counts"]["historical_reference_units_excluded"] == 0
