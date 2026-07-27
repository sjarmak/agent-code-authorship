import unittest

import numpy as np

from authorship.quantify import IdentificationError
from authorship.replication import (
    bootstrap_pipeline,
    evaluate_dedicated_validation,
    evaluate_identification,
    leave_one_target_group_out,
    run_quantification,
)


def population(rng, prefix, groups, per_group, mean):
    names = np.repeat([f"{prefix}{i}" for i in range(groups)], per_group)
    x = rng.normal(mean, 0.7, (groups * per_group, 4))
    weights = rng.integers(5, 30, len(x)).astype(float)
    return x, weights, names


class ReplicationPipelineTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(91)
        a_x, a_w, a_g = population(rng, "a", 10, 20, 1.5)
        h_x, h_w, h_g = population(rng, "h", 10, 20, -1.5)
        t_a_x, t_a_w, t_a_g = population(rng, "ta", 6, 20, 1.5)
        t_h_x, t_h_w, t_h_g = population(rng, "th", 4, 20, -1.5)
        self.reference_x = np.vstack([a_x, h_x])
        self.reference_y = np.concatenate([np.ones(len(a_x)), np.zeros(len(h_x))])
        self.reference_w = np.concatenate([a_w, h_w])
        self.reference_g = np.concatenate([a_g, h_g])
        self.target_x = np.vstack([t_a_x, t_h_x])
        self.target_w = np.concatenate([t_a_w, t_h_w])
        self.target_g = np.concatenate([t_a_g, t_h_g])
        self.true_share = float(t_a_w.sum() / self.target_w.sum())

    def test_logistic_pipeline_quantifies_held_out_population(self):
        result = run_quantification(
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            self.target_x,
            self.target_w,
            self.target_g,
            model_kind="logistic",
        )

        self.assertGreater(result["auc"], 0.9)
        self.assertAlmostEqual(result["mixture_share"], self.true_share, delta=0.1)
        self.assertLess(result["estimator_disagreement"], 0.1)
        self.assertEqual(result["model_kind"], "logistic")

    def test_reference_and_target_repository_overlap_is_rejected(self):
        overlapping = self.target_g.copy()
        overlapping[0] = self.reference_g[0]

        with self.assertRaisesRegex(IdentificationError, "overlap"):
            run_quantification(
                self.reference_x,
                self.reference_y,
                self.reference_w,
                self.reference_g,
                self.target_x,
                self.target_w,
                overlapping,
            )

    def test_nonlinear_challenger_is_diagnostic_only(self):
        result = run_quantification(
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            self.target_x,
            self.target_w,
            self.target_g,
            model_kind="gradient_boosted_trees",
            seed=4,
        )

        self.assertGreater(result["auc"], 0.9)
        self.assertEqual(result["model_kind"], "gradient_boosted_trees")
        self.assertEqual(result["headline_eligible"], False)

    def test_end_to_end_bootstrap_refits_and_is_deterministic(self):
        args = (
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            self.target_x,
            self.target_w,
            self.target_g,
        )

        first = bootstrap_pipeline(*args, replicates=12, seed=1729)
        second = bootstrap_pipeline(*args, replicates=12, seed=1729)

        self.assertEqual(first, second)
        self.assertEqual(first["successful_replicates"], 12)
        self.assertEqual(first["model_refits"], 12 * 6)
        self.assertLess(first["interval"][0], first["interval"][1])

    def test_leave_one_target_repository_out_reports_maximum_shift(self):
        point = run_quantification(
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            self.target_x,
            self.target_w,
            self.target_g,
        )
        stability = leave_one_target_group_out(
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            self.target_x,
            self.target_w,
            self.target_g,
            baseline_share=point["mixture_share"],
        )

        self.assertEqual(stability["groups_evaluated"], 10)
        self.assertLess(stability["maximum_shift"], 0.1)

    def test_final_identification_requires_every_locked_gate(self):
        point = {
            "auc": 0.9,
            "mixture_share": 0.4,
            "threshold_adjusted_share": 0.43,
            "gates": {
                "minimum_auc": True,
                "plausible_mixture": True,
                "maximum_estimator_disagreement": True,
            },
        }
        bootstrap = {"interval": [0.30, 0.48]}
        stability = {"maximum_shift": 0.04}
        synthetic = {"mean_absolute_error": 0.06, "interval_coverage": 0.92}

        result = evaluate_identification(point, bootstrap, stability, synthetic)
        self.assertTrue(result["identified"])
        self.assertEqual(len(result["gates"]), 7)

        synthetic["interval_coverage"] = 0.89
        failed = evaluate_identification(point, bootstrap, stability, synthetic)
        self.assertFalse(failed["identified"])
        self.assertFalse(failed["gates"]["minimum_synthetic_interval_coverage"])

    def test_dedicated_validation_is_scored_without_entering_model_fit(self):
        rng = np.random.default_rng(301)
        va_x, va_w, va_g = population(rng, "validation-agent", 1, 40, 1.5)
        vh_x, vh_w, vh_g = population(rng, "validation-human", 1, 40, -1.5)
        validation_x = np.vstack([va_x, vh_x])
        validation_y = np.concatenate([np.ones(len(va_x)), np.zeros(len(vh_x))])
        validation_w = np.concatenate([va_w, vh_w])
        validation_g = np.concatenate([va_g, vh_g])

        result = evaluate_dedicated_validation(
            self.reference_x,
            self.reference_y,
            self.reference_w,
            self.reference_g,
            validation_x,
            validation_y,
            validation_w,
            validation_g,
            shares=[0.0, 0.5, 1.0],
            groups_per_mixture=10,
            trials=2,
            interval_replicates=20,
            seed=19,
        )

        self.assertEqual(result["design"], "explicit_dedicated_validation")
        self.assertEqual(result["validation_groups"], {"agent": 1, "human": 1})
        self.assertEqual(result["model_refits"], 6)
        self.assertLess(result["mean_absolute_error"], 0.15)

    def test_dedicated_validation_rejects_repository_overlap(self):
        with self.assertRaisesRegex(IdentificationError, "overlap"):
            evaluate_dedicated_validation(
                self.reference_x,
                self.reference_y,
                self.reference_w,
                self.reference_g,
                self.reference_x[:2],
                self.reference_y[:2],
                self.reference_w[:2],
                self.reference_g[:2],
                shares=[0.5],
                groups_per_mixture=2,
                trials=1,
                interval_replicates=2,
                seed=19,
            )


if __name__ == "__main__":
    unittest.main()
