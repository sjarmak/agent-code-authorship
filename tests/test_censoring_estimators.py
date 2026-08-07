import json
import unittest
from pathlib import Path

import jsonschema
from numpy.testing import assert_allclose

import authorship.censoring_estimators as estimators
from authorship.censoring_estimators import (
    SurvivalContractError,
    estimate_survival,
    estimate_weighting,
    repository_bootstrap_draws,
)


def line(repository_id, line_id, censor_time_days, *transitions):
    return {
        "repository_id": repository_id,
        "line_id": line_id,
        "censor_time_days": censor_time_days,
        "transitions": [
            {
                "time_days": time,
                "from_state": from_state,
                "to_state": to_state,
            }
            for time, from_state, to_state in transitions
        ],
    }


def document(*lines):
    return {"contract_version": 1, "lines": list(lines)}


class CensoringEstimatorTests(unittest.TestCase):
    def test_kaplan_meier_is_monotone_and_does_not_complete_case_rise(self):
        events = document(
            line("low-survival", "deleted", 180, (100, "unchanged", "deleted")),
            line("high-survival", "retained", 365),
        )

        result = estimate_weighting(
            events, horizons_days=[90, 180, 365], weighting="repository"
        )

        survival = [point["survival"] for point in result["kaplan_meier"]]
        self.assertEqual(survival, [1.0, 0.5, 0.5])
        self.assertEqual(
            [point["risk_set_lines"] for point in result["kaplan_meier"]],
            [2, 1, 1],
        )
        self.assertTrue(
            all(left >= right for left, right in zip(survival, survival[1:]))
        )

    def test_right_censoring_removes_a_line_from_later_risk_sets(self):
        events = document(
            line("early-censor", "censored", 5),
            line("observed", "deleted", 20, (10, "unchanged", "deleted")),
        )

        point = estimate_weighting(events, horizons_days=[10], weighting="repository")[
            "kaplan_meier"
        ][0]

        self.assertEqual(point["risk_set_lines"], 1)
        self.assertEqual(point["risk_set_weight"], 1.0)
        self.assertEqual(point["survival"], 0.0)

    def test_repository_weighting_gives_each_repository_equal_total_weight(self):
        events = document(
            line("small", "only-line", 100, (10, "unchanged", "deleted")),
            *[line("large", f"line-{index}", 100) for index in range(9)],
        )

        repository = estimate_weighting(
            events, horizons_days=[20], weighting="repository"
        )
        line_weighted = estimate_weighting(events, horizons_days=[20], weighting="line")

        self.assertAlmostEqual(repository["kaplan_meier"][0]["survival"], 0.5)
        self.assertAlmostEqual(line_weighted["kaplan_meier"][0]["survival"], 0.9)
        self.assertAlmostEqual(
            repository["aalen_johansen"][0]["state_occupancy"]["deleted"], 0.5
        )
        self.assertAlmostEqual(
            line_weighted["aalen_johansen"][0]["state_occupancy"]["deleted"], 0.1
        )

    def test_aalen_johansen_conserves_mass_across_competing_transitions(self):
        events = document(
            line(
                "repo-a",
                "modified",
                30,
                (10, "unchanged", "modified"),
                (20, "modified", "deleted"),
            ),
            line("repo-b", "deleted", 30, (10, "unchanged", "deleted")),
        )

        points = estimate_weighting(
            events, horizons_days=[10, 20], weighting="repository"
        )["aalen_johansen"]

        self.assertEqual(
            points[0]["state_occupancy"],
            {"unchanged": 0.0, "modified": 0.5, "deleted": 0.5},
        )
        self.assertEqual(
            points[1]["state_occupancy"],
            {"unchanged": 0.0, "modified": 0.0, "deleted": 1.0},
        )
        for point in points:
            self.assertAlmostEqual(sum(point["state_occupancy"].values()), 1.0)

    def test_repository_bootstrap_is_clustered_and_deterministic(self):
        repositories = ["a", "b", "c"]

        first = repository_bootstrap_draws(repositories, replicates=20, seed=918273)
        second = repository_bootstrap_draws(repositories, replicates=20, seed=918273)

        self.assertEqual(first, second)
        self.assertTrue(all(sum(draw.values()) == 3 for draw in first))
        self.assertTrue(all(set(draw).issubset(set(repositories)) for draw in first))

    def test_repository_bootstrap_rejects_duplicate_repository_ids(self):
        with self.assertRaisesRegex(
            SurvivalContractError, "repository IDs must be unique"
        ):
            repository_bootstrap_draws(["a", "a"], replicates=10, seed=1)

    def test_repository_bootstrap_intervals_are_deterministic(self):
        events = document(
            line("repo-a", "a", 40, (10, "unchanged", "deleted")),
            line("repo-b", "b", 40, (20, "unchanged", "modified")),
            line("repo-c", "c", 40),
        )

        first = estimate_survival(
            events,
            horizons_days=[10, 30],
            bootstrap_replicates=50,
            seed=20260727,
        )
        second = estimate_survival(
            events,
            horizons_days=[10, 30],
            bootstrap_replicates=50,
            seed=20260727,
        )

        self.assertEqual(first["bootstrap"], second["bootstrap"])
        self.assertEqual(first["bootstrap"]["unit"], "repository")
        self.assertEqual(first["bootstrap"]["replicates"], 50)

    def test_vectorized_bootstrap_matches_explicit_repository_resampling(self):
        events = document(
            line("repo-a", "a1", 40, (10, "unchanged", "deleted")),
            line(
                "repo-a",
                "a2",
                40,
                (5, "unchanged", "modified"),
                (30, "modified", "deleted"),
            ),
            line("repo-b", "b", 15),
            line("repo-c", "c", 40, (20, "unchanged", "modified")),
        )
        horizons = (10.0, 20.0, 30.0)
        draws = repository_bootstrap_draws(
            ["repo-a", "repo-b", "repo-c"], replicates=25, seed=77
        )
        lines = estimators._parse_document(events)
        result = estimate_survival(
            events,
            horizons_days=horizons,
            bootstrap_replicates=25,
            seed=77,
        )

        for weighting in ("primary", "secondary"):
            mode = "repository" if weighting == "primary" else "line"
            explicit = [
                estimators._estimate_lines(lines, horizons, mode, draw)
                for draw in draws
            ]
            expected = estimators._bootstrap_summary(explicit, horizons, 0.95)
            actual = result["bootstrap"][weighting]
            assert_allclose(
                [(point["lower"], point["upper"]) for point in actual["kaplan_meier"]],
                [
                    (point["lower"], point["upper"])
                    for point in expected["kaplan_meier"]
                ],
            )
            for state in estimators.STATES:
                assert_allclose(
                    [
                        (
                            point["states"][state]["lower"],
                            point["states"][state]["upper"],
                        )
                        for point in actual["aalen_johansen"]
                    ],
                    [
                        (
                            point["states"][state]["lower"],
                            point["states"][state]["upper"],
                        )
                        for point in expected["aalen_johansen"]
                    ],
                )

    def test_semantically_invalid_transition_history_is_rejected(self):
        events = document(
            line(
                "repo",
                "line",
                20,
                (10, "unchanged", "modified"),
                (10, "modified", "deleted"),
            )
        )

        with self.assertRaisesRegex(SurvivalContractError, "strictly increasing"):
            estimate_weighting(events, horizons_days=[20], weighting="repository")

    def test_line_event_schema_accepts_the_contract(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "study" / "survival-line-events.schema.json").read_text()
        )
        events = document(line("repo", "line", 30, (10, "unchanged", "modified")))

        jsonschema.validate(events, schema)


if __name__ == "__main__":
    unittest.main()
