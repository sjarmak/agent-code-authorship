import json
import tempfile
import unittest
from pathlib import Path

from authorship.build_report_figures import (
    build,
    contextual_effects,
    inert_svg,
    render_contextual_effects,
    render_survival_overplot,
    survival_points,
)


class BuildReportFiguresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.estimates = json.loads(
            Path("results/survival-estimates.v1.json").read_text()
        )
        cls.comparison = json.loads(
            Path("results/contextual-comparison.v1.json").read_text()
        )

    def test_survival_is_unchanged_plus_modified(self):
        rows = survival_points(self.estimates)
        first_source = self.estimates["strata"][0]["horizons"]["30"]
        values = first_source["point_estimates"]["repository_weighted"][
            "conditional_on_observable_lineage"
        ]

        self.assertAlmostEqual(
            rows[0]["survival"], values["unchanged"] + values["modified"]
        )

    def test_survival_preserves_gate_failures(self):
        rows = survival_points(self.estimates)
        cursor = [
            row
            for row in rows
            if row["language"] == "Go" and row["agent"] == "Cursor"
        ]

        self.assertEqual(len(cursor), 4)
        self.assertTrue(all(row["status"] == "not_identified" for row in cursor))
        self.assertTrue(all(row["survival"] is None for row in cursor))

    def test_contextual_effects_preserve_bootstrap_interval(self):
        rows = contextual_effects(self.comparison)
        source = self.comparison["languages"][0]["horizons"]["30"][
            "paired_estimate"
        ]["repository_weighted_primary"]

        self.assertEqual(rows[0]["point"], source["point"])
        self.assertEqual(rows[0]["lower"], source["bootstrap_95_ci"][0])
        self.assertEqual(rows[0]["upper"], source["bootstrap_95_ci"][1])

    def test_contextual_svg_is_inert_and_directly_labeled(self):
        svg = render_contextual_effects(contextual_effects(self.comparison))

        self.assertTrue(inert_svg(svg))
        self.assertIn(
            "agent minus non-agent-attributed survival", svg.lower()
        )
        self.assertIn("Go · 30d", svg)
        self.assertNotIn("<script", svg.lower())
        self.assertNotIn("<foreignObject", svg)

    def test_survival_overplot_is_inert_and_directly_labeled(self):
        svg = render_survival_overplot(survival_points(self.estimates))

        self.assertTrue(inert_svg(svg))
        self.assertIn("Claude Code", svg)
        self.assertIn("OpenAI Codex", svg)
        self.assertNotIn("legend", svg.lower())

    def test_empty_or_active_graphics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "contextual"):
            render_contextual_effects([])
        with self.assertRaisesRegex(ValueError, "survival"):
            render_survival_overplot([])
        self.assertFalse(inert_svg("<svg><script>alert(1)</script></svg>"))
        self.assertFalse(inert_svg("<html></html>"))

    def test_build_writes_reproducible_figure_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_root = root / "skill"
            scripts = skill_root / "scripts"
            scripts.mkdir(parents=True)
            renderer = scripts / "small_multiples.py"
            renderer.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "out = sys.argv[sys.argv.index('--out') + 1]\n"
                "Path(out).write_text('<svg xmlns=\"http://www.w3.org/2000/svg\">"
                "<text>small multiples</text></svg>')\n"
            )
            output = root / "figures"

            build(
                Path("results/survival-estimates.v1.json"),
                Path("results/contextual-comparison.v1.json"),
                output,
                skill_root,
            )

            expected = {
                "contextual-effects.svg",
                "contextual-effects.v1.json",
                "manifest.v1.json",
                "tier2-survival-alternatives.html",
                "tier2-survival-overplot.svg",
                "tier2-survival-points.v1.json",
                "tier2-survival-small-multiples.svg",
            }
            self.assertEqual(
                {path.name for path in output.iterdir()}, expected
            )
            manifest = json.loads((output / "manifest.v1.json").read_text())
            self.assertEqual(
                set(manifest["figures"]),
                {
                    "contextual-effects.svg",
                    "tier2-survival-alternatives.html",
                    "tier2-survival-overplot.svg",
                    "tier2-survival-small-multiples.svg",
                },
            )


if __name__ == "__main__":
    unittest.main()
