import unittest

import numpy as np

from authorship.temporal_diagnostics import (
    TemporalDiagnosticError,
    build_primary_reference_arrays,
    era_diagnostics,
    historical_anchor_sensitivity,
)


class TemporalDiagnosticsTests(unittest.TestCase):
    def test_repository_held_out_era_auc_and_feature_shifts(self):
        rng = np.random.default_rng(8)
        historical = rng.normal(-1.5, 0.4, (120, 3))
        contemporary = rng.normal(1.5, 0.4, (120, 3))
        x = np.vstack([historical, contemporary])
        era = np.concatenate([np.zeros(120), np.ones(120)])
        groups = np.concatenate(
            [
                np.repeat([f"h{i}" for i in range(6)], 20),
                np.repeat([f"c{i}" for i in range(6)], 20),
            ]
        )
        weights = np.ones(len(x))

        result = era_diagnostics(
            x,
            era,
            weights,
            groups,
            feature_names=["a", "b", "c"],
        )

        self.assertGreater(result["repository_held_out_auc"], 0.95)
        self.assertEqual(len(result["feature_shifts"]), 3)
        self.assertTrue(
            all(
                not set(fold["train_groups"]) & set(fold["test_groups"])
                for fold in result["folds"]
            )
        )

    def test_era_diagnostic_requires_repository_diversity_per_period(self):
        x = np.arange(24, dtype=float).reshape(12, 2)
        era = np.repeat([0.0, 1.0], 6)
        groups = np.repeat(["old", "new"], 6)

        with self.assertRaisesRegex(TemporalDiagnosticError, "five"):
            era_diagnostics(x, era, np.ones(12), groups, ["a", "b"])

    def test_historical_anchor_sensitivity_is_deterministic(self):
        rng = np.random.default_rng(3)
        agent = rng.normal(0.85, 0.05, 500)
        contemporary_human = rng.normal(0.15, 0.05, 500)
        historical_human = rng.normal(0.28, 0.05, 500)
        target = np.concatenate([agent[:200], contemporary_human[:300]])
        weights = np.ones(500)

        first = historical_anchor_sensitivity(
            agent,
            weights,
            contemporary_human,
            weights,
            historical_human,
            weights,
            target,
            weights,
        )
        second = historical_anchor_sensitivity(
            agent,
            weights,
            contemporary_human,
            weights,
            historical_human,
            weights,
            target,
            weights,
        )

        self.assertEqual(first, second)
        self.assertGreater(first["absolute_share_shift"], 0)
        self.assertEqual(first["role"], "diagnostic_only")

    def test_historical_records_never_enter_primary_reference_arrays(self):
        records = [
            {
                "repo": "human/new",
                "label": "human",
                "temporal_role": "contemporary_reference",
                "line_count": 10,
                "v": [1.0, 2.0],
            },
            {
                "repo": "human/old",
                "label": "human",
                "temporal_role": "historical_diagnostic",
                "line_count": 1000,
                "v": [99.0, 99.0],
            },
            {
                "repo": "agent/new",
                "label": "agent",
                "temporal_role": "contemporary_reference",
                "line_count": 20,
                "v": [3.0, 4.0],
            },
        ]

        arrays = build_primary_reference_arrays(records)

        self.assertEqual(arrays["excluded_historical_records"], 1)
        self.assertEqual(arrays["groups"].tolist(), ["human/new", "agent/new"])
        self.assertEqual(arrays["labels"].tolist(), [0.0, 1.0])
        self.assertNotIn(99.0, arrays["x"])

    def test_unknown_temporal_role_and_conflicting_group_labels_fail_closed(self):
        with self.assertRaisesRegex(TemporalDiagnosticError, "unsupported temporal"):
            build_primary_reference_arrays(
                [
                    {
                        "repo": "x/y",
                        "label": "human",
                        "temporal_role": "primary_human_reference",
                        "line_count": 5,
                        "v": [1.0],
                    }
                ]
            )
        records = [
            {
                "repo": "same/repo",
                "label": label,
                "temporal_role": "contemporary_reference",
                "line_count": 5,
                "v": [value],
            }
            for label, value in (("human", 1.0), ("agent", 2.0))
        ]
        with self.assertRaisesRegex(TemporalDiagnosticError, "conflicting"):
            build_primary_reference_arrays(records)

    def test_era_arrays_and_feature_names_must_align(self):
        with self.assertRaisesRegex(TemporalDiagnosticError, "align"):
            era_diagnostics(
                np.ones((10, 2)),
                np.zeros(9),
                np.ones(10),
                np.repeat(["a"], 10),
                ["x", "y"],
            )
        groups = np.concatenate(
            [np.repeat([f"h{i}" for i in range(5)], 2), np.repeat([f"c{i}" for i in range(5)], 2)]
        )
        with self.assertRaisesRegex(TemporalDiagnosticError, "feature names"):
            era_diagnostics(
                np.ones((20, 2)),
                np.repeat([0.0, 1.0], 10),
                np.ones(20),
                groups,
                ["only_one"],
            )


if __name__ == "__main__":
    unittest.main()
