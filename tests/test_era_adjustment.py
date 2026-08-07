import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from authorship.era_adjustment import (
    EraAdjustmentError,
    estimate_era_adjustment,
)


def repository(
    repository_id,
    *,
    role,
    observations,
    language="Python",
    adoption_period=None,
    policy_effective_period=None,
    covariates=None,
):
    if role == "h2_ai_ban_control" and policy_effective_period is None:
        policy_effective_period = -100
    observations = dict(observations)
    first_period = min(observations)
    observations.setdefault(first_period - 1, observations[first_period])
    return {
        "repository_id": repository_id,
        "language": language,
        "role": role,
        "adoption_period": adoption_period,
        "policy_effective_period": policy_effective_period,
        "observations": [
            {
                "period": period,
                "feature_value": value,
                **((covariates or {}).get(period, {})),
            }
            for period, value in observations.items()
        ],
    }


def panel(*repositories):
    return {
        "panel_version": 1,
        "feature_id": "synthetic_feature",
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": list(repositories),
    }


class EraAdjustmentTests(unittest.TestCase):
    def test_within_repository_did_removes_fixed_effect_and_calendar_era(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=2,
                observations={0: 100, 1: 102, 3: 107},
            ),
            repository(
                "ban-control",
                role="h2_ai_ban_control",
                observations={0: 10, 1: 12, 3: 14},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=30,
            seed=17,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["status"], "identified")
        self.assertEqual(python["era_adjusted_effect"], 3.0)
        self.assertEqual(python["pretrend_difference"], 0.0)
        self.assertEqual(python["h2_ai_ban_control_count"], 1)
        self.assertEqual(python["pre_adoption_evidence_tier"], "H3")
        self.assertEqual(result["estimand"]["id"], "whole_repository_adoption_effect")
        self.assertEqual(result["evidence_strata"]["H1"]["status"], "not_available")

    def test_not_yet_adopters_are_controls_only_before_their_own_adoption(self):
        data = panel(
            repository(
                "treated",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            ),
            repository(
                "future",
                role="adopter",
                adoption_period=5,
                observations={0: 10, 1: 11, 3: 13, 4: 14, 6: 20},
            ),
            repository(
                "already-treated",
                role="adopter",
                adoption_period=3,
                observations={0: 20, 1: 21, 3: 30},
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={0: 30, 1: 31, 3: 33},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=20,
            seed=5,
        )
        contrast = result["languages"]["Python"]["adopter_contrasts"][0]

        self.assertEqual(contrast["control_repository_ids"], ["ban", "future"])
        self.assertNotIn("already-treated", contrast["control_repository_ids"])
        self.assertEqual(
            contrast["control_roles"],
            {
                "h2_ai_ban_control": ["ban"],
                "never_adopter_control": [],
                "not_yet_adopter": ["future"],
            },
        )

    def test_h2_controls_require_effective_policy_and_never_controls_remain(self):
        data = panel(
            repository(
                "treated",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            ),
            repository(
                "late-policy",
                role="h2_ai_ban_control",
                policy_effective_period=2,
                observations={0: 10, 1: 11, 3: 13},
            ),
            repository(
                "never",
                role="never_adopter_control",
                observations={0: 20, 1: 21, 3: 23},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=20,
            seed=9,
        )
        contrast = result["languages"]["Python"]["adopter_contrasts"][0]

        self.assertEqual(contrast["control_repository_ids"], ["never"])
        self.assertEqual(contrast["control_roles"]["never_adopter_control"], ["never"])
        self.assertEqual(contrast["control_roles"]["h2_ai_ban_control"], [])

    def test_repository_cross_fitting_excludes_same_fold_controls(self):
        adopter_id = "treated"
        fold = int(hashlib.sha256(adopter_id.encode()).hexdigest(), 16) % 2
        different = next(
            f"different-{index}"
            for index in range(20)
            if int(
                hashlib.sha256(f"different-{index}".encode()).hexdigest(),
                16,
            )
            % 2
            != fold
        )
        same = next(
            f"same-{index}"
            for index in range(20)
            if int(hashlib.sha256(f"same-{index}".encode()).hexdigest(), 16) % 2 == fold
        )
        data = panel(
            repository(
                adopter_id,
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            ),
            repository(
                different,
                role="never_adopter_control",
                observations={0: 10, 1: 11, 3: 13},
            ),
            repository(
                same,
                role="never_adopter_control",
                observations={0: 20, 1: 21, 3: 23},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=20,
            seed=2,
            cross_fit_folds=2,
        )
        contrast = result["languages"]["Python"]["adopter_contrasts"][0]

        self.assertEqual(contrast["cross_fit_fold"], fold)
        self.assertEqual(contrast["control_repository_ids"], [different])
        self.assertEqual(result["cross_fitting"]["fold_count"], 2)

    def test_controls_are_nearest_matched_on_frozen_period_covariates(self):
        periods = {
            period: {
                "change_size": 100,
                "code_age_days": 0,
                "calendar_period": period,
                "path_type_counts": {"source": 5, "test": 5},
            }
            for period in (0, 1, 3)
        }
        controls = [
            repository(
                f"close-{index}",
                role="never_adopter_control",
                observations={0: 0, 1: 1, 3: 3},
                covariates={
                    period: {
                        **periods[period],
                        "change_size": 90 + index,
                    }
                    for period in periods
                },
            )
            for index in range(5)
        ]
        controls.append(
            repository(
                "far",
                role="never_adopter_control",
                observations={0: 0, 1: 1, 3: 3},
                covariates={
                    period: {
                        "change_size": 1,
                        "code_age_days": 0,
                        "calendar_period": period,
                        "path_type_counts": {"source": 10, "test": 0},
                    }
                    for period in periods
                },
            )
        )
        data = panel(
            repository(
                "treated",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
                covariates=periods,
            ),
            *controls,
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=20,
            seed=2,
        )
        contrast = result["languages"]["Python"]["adopter_contrasts"][0]

        self.assertEqual(
            set(contrast["control_repository_ids"]),
            {f"close-{index}" for index in range(5)},
        )
        self.assertNotIn("far", contrast["control_repository_ids"])
        self.assertEqual(
            contrast["matching"]["match_on"],
            [
                "language",
                "path_type",
                "change_size",
                "code_age",
                "calendar_time",
            ],
        )

    def test_failed_placebo_test_blocks_headline_inference(self):
        data = panel(
            repository(
                "a1",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 10, 3: 14},
            ),
            repository(
                "a2",
                role="adopter",
                adoption_period=2,
                observations={0: 1, 1: 11, 3: 15},
            ),
            repository(
                "never",
                role="never_adopter_control",
                observations={0: 0, 1: 0, 3: 0},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=2,
            minimum_controls=1,
            bootstrap_replicates=20,
            seed=4,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["placebo_test"]["status"], "failed")
        self.assertEqual(python["status"], "not_identified")
        self.assertIn("placebo diagnostic failed", python["failure_reasons"])
        self.assertFalse(result["headline_inference_allowed"])

    def test_language_gate_fails_explicitly_instead_of_extrapolating(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 4},
                covariates={
                    period: {
                        "calendar_period": period,
                        "change_size": 12,
                        "code_age_days": 0,
                        "path_type_counts": {"source": 2, "test": 1},
                    }
                    for period in (0, 1, 3)
                },
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 1, 3: 2},
            ),
        )

        result = estimate_era_adjustment(
            data,
            bootstrap_replicates=10,
            seed=1,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["status"], "not_identified")
        self.assertIsNone(python["era_adjusted_effect"])
        self.assertIn("fewer than 20 adopters", python["failure_reasons"])
        self.assertIn("fewer than 10 controls", python["failure_reasons"])

    def test_missing_pretrend_period_fails_the_diagnostic_gate(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=2,
                observations={1: 1, 3: 5},
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={1: 1, 3: 3},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=10,
            seed=1,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["status"], "not_identified")
        self.assertIn("pretrend diagnostic unavailable", python["failure_reasons"])

    def test_parallel_pretrend_and_placebo_are_independent_gates(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=3,
                observations={0: 0, 1: 0, 2: 4, 4: 8},
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 0, 2: 0, 4: 0},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=10,
            seed=1,
        )
        python = result["languages"]["Python"]

        self.assertTrue(python["diagnostics"]["parallel_pre_trends"]["passed"])
        self.assertFalse(python["diagnostics"]["placebo_adoption_dates"]["passed"])
        self.assertEqual(python["status"], "not_identified")
        self.assertIn("placebo diagnostic failed", python["failure_reasons"])

    def test_adoption_estimator_rejects_exact_authorship_relabeling(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            )
        )
        data["estimand_id"] = "exact_agent_authorship_effect"
        data["unit"] = "confirmed_agent_introduced_hunk"

        with self.assertRaisesRegex(
            EraAdjustmentError, "whole_repository_adoption_effect"
        ):
            estimate_era_adjustment(
                data,
                bootstrap_replicates=10,
                seed=1,
            )

    def test_authorship_only_diagnostics_are_explicitly_deferred(self):
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=3,
                observations={0: 0, 1: 0, 2: 0, 4: 4},
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 0, 2: 0, 4: 0},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=10,
            seed=1,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["status"], "identified")
        self.assertFalse(python["authorship_headline_eligible"])
        self.assertEqual(
            python["diagnostics"]["historical_anchor_sensitivity"]["status"],
            "deferred_to_exact_authorship_prevalence",
        )

    def test_missing_pretrend_repository_is_excluded_before_precision_gate(self):
        data = panel(
            repository(
                "complete",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            ),
            repository(
                "incomplete",
                role="adopter",
                adoption_period=2,
                observations={1: 1, 3: 5},
            ),
            repository(
                "never",
                role="never_adopter_control",
                observations={0: 0, 1: 1, 3: 3},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=10,
            seed=1,
        )
        python = result["languages"]["Python"]

        self.assertEqual(python["status"], "identified")
        self.assertEqual(python["adopter_count"], 1)
        self.assertEqual(python["excluded_pretrend_repository_count"], 1)

    def test_repository_bootstrap_intervals_are_deterministic(self):
        data = panel(
            repository(
                "a1",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 5},
            ),
            repository(
                "a2",
                role="adopter",
                adoption_period=2,
                observations={0: 10, 1: 11, 3: 16},
            ),
            repository(
                "b1",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 1, 3: 3},
            ),
            repository(
                "b2",
                role="h2_ai_ban_control",
                observations={0: 20, 1: 21, 3: 24},
            ),
        )

        first = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=50,
            seed=20260727,
        )
        second = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=50,
            seed=20260727,
        )

        self.assertEqual(first["bootstrap"], second["bootstrap"])
        self.assertEqual(first["bootstrap"]["unit"], "repository")
        self.assertEqual(first["bootstrap"]["replicates"], 50)

    def test_repository_bootstrap_does_not_condition_on_diagnostic_success(self):
        data = panel(
            repository(
                "a1",
                role="adopter",
                adoption_period=3,
                observations={0: 0, 1: 0, 2: 1, 4: 11},
            ),
            repository(
                "a2",
                role="adopter",
                adoption_period=3,
                observations={0: 0, 1: 0, 2: -1, 4: -1},
            ),
            repository(
                "control",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 0, 2: 0, 4: 0},
            ),
        )

        result = estimate_era_adjustment(
            data,
            minimum_adopters=1,
            minimum_controls=1,
            bootstrap_replicates=1000,
            seed=1,
        )
        bootstrap = result["bootstrap"]["languages"]["Python"]

        self.assertEqual(bootstrap["identified_replicates"], 1000)
        self.assertEqual(bootstrap["effect_interval"], {"lower": 0.0, "upper": 10.0})

    def test_duplicate_periods_are_rejected(self):
        record = repository(
            "adopter",
            role="adopter",
            adoption_period=2,
            observations={0: 0, 1: 1, 3: 4},
        )
        record["observations"].append({"period": 1, "feature_value": 2})

        with self.assertRaisesRegex(EraAdjustmentError, "periods must be unique"):
            estimate_era_adjustment(
                panel(record),
                bootstrap_replicates=10,
                seed=1,
            )

    def test_panel_schema_accepts_h2_and_adopter_roles(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "study" / "era-adjustment-panel.schema.json").read_text()
        )
        data = panel(
            repository(
                "adopter",
                role="adopter",
                adoption_period=2,
                observations={0: 0, 1: 1, 3: 4},
                covariates={
                    period: {
                        "calendar_period": period,
                        "change_size": 12,
                        "code_age_days": 0,
                        "path_type_counts": {"source": 2, "test": 1},
                    }
                    for period in (0, 1, 3)
                },
            ),
            repository(
                "ban",
                role="h2_ai_ban_control",
                observations={0: 0, 1: 1, 3: 2},
            ),
        )

        jsonschema.validate(data, schema)


if __name__ == "__main__":
    unittest.main()
