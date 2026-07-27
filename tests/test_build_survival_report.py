import json
import unittest
from pathlib import Path

from authorship.build_survival_report import build_report


class BuildSurvivalReportTests(unittest.TestCase):
    def test_report_separates_tiers_and_uses_context_label(self):
        estimates = json.loads(
            Path("results/survival-estimates.v1.json").read_text()
        )
        comparison = json.loads(
            Path("results/contextual-comparison.v1.json").read_text()
        )

        report = build_report(
            estimates, comparison, validation_pending=True
        )

        self.assertIn("Tier 1", report)
        self.assertIn("Tier 2", report)
        self.assertIn("not_identified", report)
        self.assertIn("non_agent_attributed", report)
        self.assertIn("pending blinded", report)
        self.assertIn("## Report figures", report)
        self.assertIn("figures/tier2-survival-small-multiples.svg", report)
        self.assertIn("figures/contextual-effects.svg", report)
        self.assertNotIn("causal authorship effects are identified", report)

    def test_completed_failed_gate_builds_exact_only_final_report(self):
        estimates = json.loads(
            Path("results/survival-estimates.v1.json").read_text()
        )
        comparison = json.loads(
            Path("results/contextual-comparison.v1.json").read_text()
        )

        report = build_report(
            estimates,
            comparison,
            validation_pending=False,
            structural_gate_passed=False,
        )

        self.assertIn("Status: **final**", report)
        self.assertIn("precision gate failed", report)
        self.assertIn(
            "Structural matches do not enter these headline results.", report
        )

    def test_completed_passing_gate_includes_structural_matches(self):
        estimates = json.loads(
            Path("results/survival-estimates.v1.json").read_text()
        )
        comparison = json.loads(
            Path("results/contextual-comparison.v1.json").read_text()
        )

        report = build_report(
            estimates,
            comparison,
            validation_pending=False,
            structural_gate_passed=True,
        )

        self.assertIn(
            "Validated structural matches enter headline results as `modified`.",
            report,
        )
        self.assertNotIn(
            "Structural matches do not enter these headline results.", report
        )
        first = estimates["strata"][0]["horizons"]["30"]
        observable = first["point_estimates"]["repository_weighted"][
            "conditional_on_observable_lineage"
        ]
        expected_survival = 100 * (
            observable["unchanged"] + observable["modified"]
        )
        self.assertIn(f"{expected_survival:.1f}%", report)


if __name__ == "__main__":
    unittest.main()
