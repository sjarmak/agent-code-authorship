import unittest

import numpy as np

from authorship.quantify import (
    IdentificationError,
    bootstrap_score_mixture,
    fit_score_mixture,
    synthetic_mixture_evaluation,
    synthetic_mixture_evaluation_from_holdout,
    threshold_adjusted_share,
)


class ScoreMixtureTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(12)
        self.agent = np.clip(rng.normal(0.82, 0.08, 2000), 0, 1)
        self.human = np.clip(rng.normal(0.18, 0.08, 2000), 0, 1)
        self.unit = np.ones(2000)

    def target(self, share: float) -> tuple[np.ndarray, np.ndarray]:
        n_agent = round(2000 * share)
        scores = np.concatenate([self.agent[:n_agent], self.human[: 2000 - n_agent]])
        return scores, np.ones(len(scores))

    def test_full_score_mixture_recovers_known_share(self):
        target, weights = self.target(0.3)

        result = fit_score_mixture(
            self.agent,
            self.unit,
            self.human,
            self.unit,
            target,
            weights,
            bins=20,
        )

        self.assertAlmostEqual(result["share"], 0.3, delta=0.03)
        self.assertTrue(result["plausible"])

    def test_boundary_mixtures_recover_zero_and_one(self):
        zero, zero_weights = self.target(0.0)
        one, one_weights = self.target(1.0)

        fit_zero = fit_score_mixture(
            self.agent, self.unit, self.human, self.unit, zero, zero_weights
        )
        fit_one = fit_score_mixture(
            self.agent, self.unit, self.human, self.unit, one, one_weights
        )

        self.assertAlmostEqual(fit_zero["share"], 0.0, delta=0.02)
        self.assertAlmostEqual(fit_one["share"], 1.0, delta=0.02)

    def test_degenerate_reference_distributions_are_rejected(self):
        same = np.full(100, 0.5)
        weights = np.ones(100)

        with self.assertRaisesRegex(IdentificationError, "indistinguishable"):
            fit_score_mixture(same, weights, same, weights, same, weights)

    def test_distribution_outside_reference_mixture_is_implausible(self):
        target = np.full(1000, 0.5)

        result = fit_score_mixture(
            self.agent,
            self.unit,
            self.human,
            self.unit,
            target,
            np.ones(1000),
            maximum_total_variation=0.10,
        )

        self.assertFalse(result["plausible"])
        self.assertGreater(result["total_variation"], 0.10)

    def test_threshold_adjustment_corrects_classifier_error(self):
        target, weights = self.target(0.4)

        share = threshold_adjusted_share(
            self.agent,
            self.unit,
            self.human,
            self.unit,
            target,
            weights,
            threshold=0.5,
        )

        self.assertAlmostEqual(share, 0.4, delta=0.03)

    def test_group_bootstrap_is_deterministic_and_resamples_all_sides(self):
        target, weights = self.target(0.4)
        agent_groups = np.array([f"a{i // 200}" for i in range(2000)])
        human_groups = np.array([f"h{i // 200}" for i in range(2000)])
        target_groups = np.array([f"t{i // 200}" for i in range(2000)])

        first = bootstrap_score_mixture(
            self.agent,
            self.unit,
            agent_groups,
            self.human,
            self.unit,
            human_groups,
            target,
            weights,
            target_groups,
            replicates=40,
            seed=1729,
        )
        second = bootstrap_score_mixture(
            self.agent,
            self.unit,
            agent_groups,
            self.human,
            self.unit,
            human_groups,
            target,
            weights,
            target_groups,
            replicates=40,
            seed=1729,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 40)
        self.assertGreater(np.std(first), 0)

    def test_whole_group_synthetic_evaluation_reports_recovery_error(self):
        agent_groups = {
            f"a{i}": (self.agent[i * 200 : (i + 1) * 200], np.ones(200))
            for i in range(10)
        }
        human_groups = {
            f"h{i}": (self.human[i * 200 : (i + 1) * 200], np.ones(200))
            for i in range(10)
        }

        result = synthetic_mixture_evaluation(
            agent_groups,
            human_groups,
            shares=[0.0, 0.5, 1.0],
            groups_per_mixture=10,
            trials=4,
            seed=7,
            interval_replicates=40,
        )

        self.assertLess(result["mean_absolute_error"], 0.05)
        self.assertEqual(result["mixture_count"], 12)
        self.assertGreaterEqual(result["interval_coverage"], 0.9)
        self.assertTrue(all("interval" in row for row in result["rows"]))

    def test_explicit_holdout_evaluation_never_splits_validation_into_reference(self):
        agent_reference = (
            self.agent[:1000],
            np.ones(1000),
            np.repeat(["reference-agent"], 1000),
        )
        human_reference = (
            self.human[:1000],
            np.ones(1000),
            np.repeat(["reference-human"], 1000),
        )
        agent_validation = {"validation-agent": (self.agent[1000:], np.ones(1000))}
        human_validation = {"validation-human": (self.human[1000:], np.ones(1000))}

        result = synthetic_mixture_evaluation_from_holdout(
            agent_reference,
            human_reference,
            agent_validation,
            human_validation,
            shares=[0.0, 0.5, 1.0],
            groups_per_mixture=10,
            trials=2,
            seed=13,
            interval_replicates=20,
        )

        self.assertEqual(result["design"], "explicit_dedicated_validation")
        self.assertEqual(result["reference_groups"], {"agent": 1, "human": 1})
        self.assertEqual(result["validation_groups"], {"agent": 1, "human": 1})
        self.assertLess(result["mean_absolute_error"], 0.05)


if __name__ == "__main__":
    unittest.main()
