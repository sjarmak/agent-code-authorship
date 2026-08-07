import json
import tempfile
import unittest
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from authorship.agent_footprint_blog_section import (
    footprint_summary,
    render_footprint_sections,
)
from authorship.blog_style import BLOG_CSS, embedded_sourcegraph_font_css
from authorship.build_v3_blog_post import build, main, render_html
from authorship.sourcegraph_index_story import render_index_story


class _DocumentAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.figures = 0
        self.figcaptions = 0
        self.svgs = 0
        self.scripts = 0
        self.external_assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.add(element_id)
        if tag == "figure":
            self.figures += 1
        elif tag == "figcaption":
            self.figcaptions += 1
        elif tag == "svg":
            self.svgs += 1
        elif tag == "script":
            self.scripts += 1
        for name in ("src", "href"):
            value = values.get(name)
            if value and not value.startswith(("#", "data:")):
                self.external_assets.append(value)


class BuildV3BlogPostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.summary = footprint_summary(
            survival_estimates=json.loads(
                (cls.root / "results/survival-estimates.v2.json").read_text()
            ),
            agent_commit_catalog=json.loads(
                (cls.root / "study/sourcegraph-agent-commit-catalog.v3.json").read_text()
            ),
            era_estimates=json.loads(
                (cls.root / "study/sourcegraph-era-study-estimates.v1.json").read_text()
            ),
            repository_frame=json.loads(
                (cls.root / "study/sourcegraph-adjudication-workflow.v3.json").read_text()
            )["repository_frame"],
        )

    def test_summary_uses_frozen_footprint_and_survival_artifacts(self) -> None:
        self.assertEqual(self.summary["canonical_repository_count"], 302)
        self.assertEqual(self.summary["indexed_repository_count"], 296)
        self.assertEqual(self.summary["provenance_commit_count"], 155)
        self.assertEqual(self.summary["provenance_repository_count"], 155)
        self.assertEqual(self.summary["survival_repository_count"], 96)
        self.assertEqual(self.summary["survival_line_count"], 656070)
        self.assertEqual(self.summary["commit_share_status"], "not_identified")
        self.assertAlmostEqual(self.summary["overall"]["survival_365"], 0.9019705249700927)
        self.assertAlmostEqual(self.summary["overall"]["unchanged_365"], 0.8493933300902979)
        self.assertEqual(
            [row["name"] for row in self.summary["harnesses"]],
            ["Claude Code", "Codex", "Copilot", "Cursor"],
        )
        self.assertEqual(
            [row["repository_count"] for row in self.summary["harnesses"]],
            [13, 24, 29, 12],
        )
        self.assertEqual(self.summary["displayed_harness_repository_count"], 78)
        self.assertEqual(self.summary["additional_harness"]["name"], "Devin")
        self.assertEqual(self.summary["additional_harness"]["repository_count"], 18)
        self.assertEqual(
            [row["name"] for row in self.summary["languages"]], ["Go", "Python"]
        )

    def test_render_leads_with_agent_footprint_question_and_honest_answer(self) -> None:
        html = render_html(summary=self.summary)

        self.assertIn(
            "How much of open source is agent-written, and how much survives?", html
        )
        self.assertIn("90.2%", html)
        self.assertIn("84.9%", html)
        self.assertIn("155", html)
        self.assertIn("Claude Code", html)
        self.assertIn("Codex", html)
        self.assertIn("Copilot", html)
        self.assertIn("Cursor", html)
        self.assertIn("Not identified", html)
        self.assertIn("Not measured", html)
        self.assertIn("A deletion is not a revert", html)
        self.assertIn("separate frozen frames", html)
        self.assertIn("also includes 18 Devin repositories", html)
        self.assertNotIn("AI adoption rarely has a clean start date", html)
        self.assertNotIn("72–89%", html)
        self.assertNotIn("\N{EM DASH}", html)

    def test_figures_derive_every_displayed_metric_from_summary(self) -> None:
        changed = deepcopy(self.summary)
        changed["provenance_commit_count"] = 154
        changed["provenance_repository_count"] = 153
        changed["overall"]["survival_365"] = 0.812
        changed["overall"]["unchanged_365"] = 0.731
        changed["overall"]["modified_365"] = 0.081
        changed["overall"]["deleted_365"] = 0.188
        changed["overall"]["curve"][-1]["survival"] = 0.812
        changed["harnesses"][0]["survival_365"] = 0.777
        changed["languages"][0]["survival_365"] = 0.888

        sections = render_footprint_sections(changed)
        story = render_index_story(
            footprint_sections=sections,
            summary=changed,
            font_css="",
            blog_css=BLOG_CSS,
        )

        self.assertIn("81.2%", story)
        self.assertIn("73.1% unchanged", story)
        self.assertIn("77.7%", story)
        self.assertIn("88.8%", story)
        self.assertIn("154", story)
        self.assertIn("153 repositories", story)
        self.assertNotIn("90.2%", story)

    def test_harness_caption_derives_extrema_and_repository_range(self) -> None:
        changed = deepcopy(self.summary)
        changed["harnesses"][0]["survival_365"] = 0.999
        changed["harnesses"][0]["repository_count"] = 31
        changed["harnesses"][3]["survival_365"] = 0.501
        changed["harnesses"][3]["repository_count"] = 9

        sections = render_footprint_sections(changed)

        self.assertIn("Claude Code is highest at 99.9%", sections)
        self.assertIn("Cursor is lowest at 50.1%", sections)
        self.assertIn("repository counts range from 9 to 31", sections)

    def test_render_matches_sourcegraph_blog_visual_system(self) -> None:
        html = render_html(summary=self.summary)

        self.assertIn("--sg-theme-brand-color: oklch(71% 0.19 27deg)", html)
        self.assertIn("--sg-theme-accent: oklch(57% 0.2 265deg)", html)
        self.assertIn("--viz-blog-purple: #8552f2", html)
        self.assertIn("--viz-blog-coral: #ff7867", html)
        self.assertIn("--viz-blog-black: #020202", html)
        self.assertIn("'Perfectly Nineties'", html)
        self.assertIn("'Poly Sans'", html)
        self.assertIn('class="sourcegraph-mark"', html)
        self.assertIn('class="blog-rail"', html)
        self.assertIn('class="post-surface"', html)
        self.assertIn("Direct labels", html)
        self.assertIn('class="current-mobile-data"', html)

    def test_sourcegraph_fonts_are_embedded_as_data_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            font_root = Path(directory)
            for filename in (
                "PerfectlyNineties-Regular.woff",
                "PerfectlyNineties-Semibold.woff",
                "PerfectlyNineties-Bold.woff",
                "PolySans-Neutral.woff2",
                "PolySans-SlimMono-300.woff2",
            ):
                (font_root / filename).write_bytes(b"font-data")

            css = embedded_sourcegraph_font_css(font_root)

        self.assertEqual(css.count("@font-face"), 5)
        self.assertIn("data:font/woff2;base64,Zm9udC1kYXRh", css)
        self.assertIn("data:font/woff;base64,Zm9udC1kYXRh", css)

    def test_build_writes_a_self_contained_accessible_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "study.html"

            build(self.root, output)
            html = output.read_text()
            audit = _DocumentAudit()
            audit.feed(html)

            self.assertTrue(html.startswith("<!doctype html>"))
            self.assertIn('<meta name="viewport"', html)
            self.assertIn("@media (prefers-reduced-motion: reduce)", html)
            self.assertIn("@media (max-width: 48rem)", html)
            self.assertIn("Skip to the evidence", html)
            self.assertIn("@font-face", html)
            self.assertIn("data:font/woff2;base64,", html)
            self.assertEqual(audit.figures, 4)
            self.assertEqual(audit.figures, audit.figcaptions)
            self.assertEqual(audit.svgs, 5)
            self.assertEqual(audit.scripts, 0)
            self.assertEqual(audit.external_assets, [])
            self.assertTrue(
                {
                    "footprint",
                    "harnesses",
                    "languages",
                    "age",
                    "write-vs-head",
                    "reverts",
                    "methods",
                    "artifacts",
                }.issubset(audit.ids)
            )

    def test_build_uses_system_fonts_when_sibling_checkout_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "study.html"
            missing_font_root = Path(directory) / "missing-fonts"

            with patch(
                "authorship.build_v3_blog_post._default_font_root",
                return_value=missing_font_root,
            ):
                build(self.root, output)

            html = output.read_text()

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertNotIn("@font-face", html)
        self.assertIn("font-family: var(--font-sans)", html)

    def test_build_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.html"
            second = Path(directory) / "second.html"

            build(self.root, first)
            build(self.root, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_cli_builds_requested_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "article.html"
            argv = [
                "build_v3_blog_post",
                "--root",
                str(self.root),
                "--output",
                str(output),
            ]

            with patch("sys.argv", argv):
                self.assertEqual(main(), 0)

            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
