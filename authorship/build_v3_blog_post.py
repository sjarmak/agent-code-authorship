"""Render the Sourcegraph index study as a self-contained HTML blog post."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from authorship.agent_footprint_blog_section import (
    footprint_summary,
    render_footprint_sections,
)
from authorship.blog_style import (
    BLOG_CSS,
    apply_blog_visual_style,
    embedded_sourcegraph_font_css,
)
from authorship.sourcegraph_index_story import render_index_story

DEFAULT_OUTPUT = Path("results/agent-code-authorship-sourcegraph.html")


def render_html(
    *,
    summary: dict[str, Any],
    font_css: str = "",
) -> str:
    """Render the deterministic, self-contained article."""
    document = render_index_story(
        footprint_sections=render_footprint_sections(summary),
        summary=summary,
        font_css=font_css,
        blog_css=BLOG_CSS,
    )
    return apply_blog_visual_style(document)


def _read_json(root: Path, relative_path: str) -> dict[str, Any]:
    return json.loads((root / relative_path).read_text())


def _load_summary(root: Path) -> dict[str, Any]:
    return footprint_summary(
        survival_estimates=_read_json(root, "results/survival-estimates.v2.json"),
        agent_commit_catalog=_read_json(
            root, "study/sourcegraph-agent-commit-catalog.v3.json"
        ),
        era_estimates=_read_json(root, "study/sourcegraph-era-study-estimates.v1.json"),
        repository_frame=_read_json(
            root, "study/sourcegraph-adjudication-workflow.v3.json"
        )["repository_frame"],
    )


def _default_font_root(root: Path) -> Path:
    return root.resolve().parent / "sourcegraph/cmd/docs/static/_docssvc/fonts"


def _font_css(root: Path, font_root: Path | None) -> str:
    resolved_root = font_root or _default_font_root(root)
    if font_root is None and not resolved_root.is_dir():
        return ""
    return embedded_sourcegraph_font_css(resolved_root)


def build(root: Path, output: Path, font_root: Path | None = None) -> None:
    """Build the article from frozen study artifacts under ``root``."""
    html = render_html(
        summary=_load_summary(root),
        font_css=_font_css(root, font_root),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--font-root",
        type=Path,
        help="Sourcegraph cmd/docs font directory; defaults to the sibling checkout",
    )
    args = parser.parse_args()
    build(args.root, args.output, args.font_root)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
