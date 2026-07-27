import unittest

from authorship.survival_estimates import (
    analysis_counts,
    coverage,
    estimate_stratum,
    exact_only_counts,
)


class SurvivalEstimateTests(unittest.TestCase):
    def test_exact_only_maps_unvalidated_structural_matches_to_unobservable(self):
        counts = exact_only_counts(
            {
                "unchanged": 5,
                "modified_candidate": 2,
                "deleted": 1,
                "unobservable": 3,
                "right_censored": 4,
            }
        )

        self.assertEqual(counts["modified"], 0)
        self.assertEqual(counts["unobservable"], 5)
        self.assertEqual(sum(counts.values()), 15)

    def test_validated_structural_matches_map_to_modified(self):
        counts = analysis_counts(
            {"unchanged": 5, "modified_candidate": 2, "unobservable": 3},
            include_structural=True,
        )

        self.assertEqual(counts["modified"], 2)
        self.assertEqual(counts["unobservable"], 3)

    def test_coverage_excludes_right_censoring_from_denominator(self):
        self.assertEqual(
            coverage(
                [
                    {
                        "unchanged": 7,
                        "modified": 0,
                        "deleted": 1,
                        "unobservable": 2,
                        "right_censored": 10,
                    }
                ]
            ),
            0.8,
        )

    def test_repository_and_line_weighting_are_distinct_and_reproducible(self):
        repositories = []
        for index in range(5):
            counts = {
                str(horizon): (
                    {"unchanged": 9, "deleted": 1}
                    if index < 4
                    else {"unchanged": 1000}
                )
                for horizon in (30, 90, 180, 365)
            }
            repositories.append({"counts": counts})

        first = estimate_stratum(repositories, replicates=20, seed=7)
        second = estimate_stratum(repositories, replicates=20, seed=7)
        horizon = first["horizons"]["30"]

        self.assertEqual(first, second)
        self.assertNotEqual(
            horizon["point_estimates"]["repository_weighted"],
            horizon["point_estimates"]["line_weighted"],
        )
        self.assertIsNotNone(horizon["bootstrap_95_ci"])

    def test_identification_gates_are_explicit(self):
        repositories = [
            {
                "counts": {
                    str(horizon): {
                        "unchanged": 7,
                        "modified_candidate": 2,
                        "unobservable": 1,
                    }
                    for horizon in (30, 90, 180, 365)
                }
            }
            for _ in range(4)
        ]

        result = estimate_stratum(repositories, replicates=5, seed=3)

        self.assertEqual(result["horizons"]["30"]["status"], "not_identified")
        self.assertCountEqual(
            result["horizons"]["30"]["gate_reasons"],
            [
                "fewer_than_5_repositories",
                "lineage_coverage_below_0.80",
            ],
        )
        self.assertIsNone(result["horizons"]["30"]["bootstrap_95_ci"])


if __name__ == "__main__":
    unittest.main()
