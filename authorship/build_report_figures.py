"""Build publication-ready Tufte figures from frozen study results."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HORIZONS = ("30", "90", "180", "365")
AGENT_LABELS = {
    "Claude_Code": "Claude Code",
    "OpenAI_Codex": "OpenAI Codex",
}
AGENT_COLORS = {
    "Claude_Code": "#0072B2",
    "Copilot": "#D55E00",
    "Cursor": "#CC79A7",
    "Devin": "#009E73",
    "OpenAI_Codex": "#222222",
}
ACTIVE_SVG = re.compile(
    r"<script|<foreignObject|<animate|<set\b|\son[a-z]+\s*=|javascript:",
    re.IGNORECASE,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def survival_points(estimates: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract censoring-correct repository-balanced Kaplan-Meier survival."""
    rows = []
    for stratum in estimates["strata"]:
        dimensions = stratum.get("dimensions")
        if not isinstance(dimensions, dict) or set(dimensions) != {
            "agent_family",
            "language",
        }:
            continue
        estimate = stratum.get("estimate")
        points = (
            {
                int(point["horizon_days"]): point
                for point in estimate["primary"]["kaplan_meier"]
            }
            if stratum.get("status") == "identified"
            else {}
        )
        for horizon_text in HORIZONS:
            horizon = int(horizon_text)
            point = points.get(horizon)
            rows.append(
                {
                    "language": dimensions["language"],
                    "agent": dimensions["agent_family"],
                    "repository_count": stratum["repository_count"],
                    "horizon_days": horizon,
                    "status": stratum["status"],
                    "gate_reasons": (
                        [] if stratum["status"] == "identified" else [stratum["status"]]
                    ),
                    "survival": point["survival"] if point else None,
                    "risk_set_lines": point["risk_set_lines"] if point else None,
                    "risk_set_weight": point["risk_set_weight"] if point else None,
                    "estimand": "repository_balanced_kaplan_meier",
                }
            )
    return rows


def contextual_effects(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract paired repository-weighted effects and bootstrap intervals."""
    rows = []
    for language in comparison["languages"]:
        for horizon in HORIZONS:
            result = language["horizons"][horizon]
            estimate = result["paired_estimate"]
            primary = estimate["repository_weighted_primary"] if estimate else None
            rows.append(
                {
                    "language": language["language"],
                    "paired_repository_count": language["paired_repository_count"],
                    "horizon_days": int(horizon),
                    "status": result["status"],
                    "point": primary["point"] if primary else None,
                    "lower": primary["bootstrap_95_ci"][0] if primary else None,
                    "upper": primary["bootstrap_95_ci"][1] if primary else None,
                }
            )
    return rows


def inert_svg(svg: str) -> bool:
    return svg.lstrip().startswith("<svg") and ACTIVE_SVG.search(svg) is None


def render_contextual_effects(rows: list[dict[str, Any]]) -> str:
    identified = [row for row in rows if row["status"] == "identified"]
    if not identified:
        raise ValueError("no identified contextual effects")
    width = 1040
    left, right, top, bottom = 270, 250, 72, 48
    row_height = 38
    height = top + bottom + row_height * len(identified)
    xmin = min(0.0, *(row["lower"] for row in identified))
    xmax = max(0.0, *(row["upper"] for row in identified))
    span = xmax - xmin

    def sx(value: float) -> float:
        return left + (value - xmin) / span * (width - left - right)

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" '
            'font-family="et-book, ET-Bembo, Palatino, Georgia, serif" '
            'font-size="13">'
        ),
        f'<rect width="{width}" height="{height}" fill="white"/>',
        (
            '<text x="24" y="28" font-size="19">'
            "Agent minus non-agent-attributed survival"
            "</text>"
        ),
        (
            '<text x="24" y="49" font-size="12" fill="#666">'
            "Repository-weighted point estimate and 95% paired-repository "
            "bootstrap interval"
            "</text>"
        ),
        (
            f'<line x1="{sx(0):.1f}" y1="{top - 12}" '
            f'x2="{sx(0):.1f}" y2="{height - bottom + 4}" '
            'stroke="#aaa" stroke-width="0.8"/>'
        ),
        (
            f'<text x="{sx(0):.1f}" y="{top - 18}" text-anchor="middle" '
            'font-size="11" fill="#666">no difference</text>'
        ),
    ]
    for index, row in enumerate(identified):
        y = top + index * row_height
        label = f"{row['language']} · {row['horizon_days']}d"
        interval = (
            f"{100 * row['point']:.1f} pp "
            f"[{100 * row['lower']:.1f}, {100 * row['upper']:.1f}]"
        )
        parts.extend(
            [
                (
                    f'<text x="{left - 18}" y="{y + 4}" text-anchor="end">'
                    f"{html.escape(label)}</text>"
                ),
                (
                    f'<line x1="{sx(row["lower"]):.1f}" y1="{y}" '
                    f'x2="{sx(row["upper"]):.1f}" y2="{y}" '
                    'stroke="#333" stroke-width="1.2"/>'
                ),
                (
                    f'<line x1="{sx(row["lower"]):.1f}" y1="{y - 4}" '
                    f'x2="{sx(row["lower"]):.1f}" y2="{y + 4}" '
                    'stroke="#333" stroke-width="1"/>'
                ),
                (
                    f'<line x1="{sx(row["upper"]):.1f}" y1="{y - 4}" '
                    f'x2="{sx(row["upper"]):.1f}" y2="{y + 4}" '
                    'stroke="#333" stroke-width="1"/>'
                ),
                (
                    f'<circle cx="{sx(row["point"]):.1f}" cy="{y}" r="3.2" '
                    'fill="#111"/>'
                ),
                (
                    f'<text x="{width - right + 18}" y="{y + 4}" '
                    f'font-size="12">{html.escape(interval)}</text>'
                ),
            ]
        )
    axis_y = height - bottom + 4
    parts.extend(
        [
            (
                f'<line x1="{sx(xmin):.1f}" y1="{axis_y}" '
                f'x2="{sx(xmax):.1f}" y2="{axis_y}" '
                'stroke="#444" stroke-width="0.8"/>'
            ),
            (
                f'<text x="{sx(xmin):.1f}" y="{axis_y + 17}" '
                f'text-anchor="middle" font-size="11">{100*xmin:.1f} pp</text>'
            ),
            (
                f'<text x="{sx(xmax):.1f}" y="{axis_y + 17}" '
                f'text-anchor="middle" font-size="11">{100*xmax:.1f} pp</text>'
            ),
            "</svg>",
        ]
    )
    svg = "\n".join(parts)
    if not inert_svg(svg):
        raise ValueError("rendered active SVG")
    return svg


def render_survival_overplot(rows: list[dict[str, Any]]) -> str:
    identified = [row for row in rows if row["survival"] is not None]
    if not identified:
        raise ValueError("no identified survival estimates")
    width, height = 1200, 430
    top, bottom = 72, 54
    panel_width, gap = 480, 100
    lefts = {"Go": 62, "Python": 62 + panel_width + gap}
    plot_width = panel_width - 150
    ymin = min(row["survival"] for row in identified)
    ymax = max(row["survival"] for row in identified)

    def sx(left: float, horizon: int) -> float:
        return left + (horizon - 30) / (365 - 30) * plot_width

    def sy(value: float) -> float:
        return top + (ymax - value) / (ymax - ymin) * (height - top - bottom)

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" '
            'font-family="et-book, ET-Bembo, Palatino, Georgia, serif" '
            'font-size="12">'
        ),
        f'<rect width="{width}" height="{height}" fill="white"/>',
        (
            '<text x="24" y="28" font-size="19">'
            "Tier 2 agent-attributed code survival — overplotted alternative"
            "</text>"
        ),
        (
            '<text x="24" y="49" font-size="12" fill="#666">'
            "Repository-balanced Kaplan–Meier survival with right censoring"
            "</text>"
        ),
    ]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault((row["language"], row["agent"]), []).append(row)
    for language, left in lefts.items():
        parts.append(
            f'<text x="{left}" y="{top - 14}" font-size="14">{language}</text>'
        )
        parts.extend(
            [
                (
                    f'<line x1="{left}" y1="{sy(ymin):.1f}" '
                    f'x2="{left}" y2="{sy(ymax):.1f}" '
                    'stroke="#555" stroke-width="0.8"/>'
                ),
                (
                    f'<line x1="{sx(left, 30):.1f}" y1="{height-bottom}" '
                    f'x2="{sx(left, 365):.1f}" y2="{height-bottom}" '
                    'stroke="#555" stroke-width="0.8"/>'
                ),
            ]
        )
        for value in (ymax, ymin):
            parts.append(
                f'<text x="{left - 7}" y="{sy(value) + 4:.1f}" '
                f'text-anchor="end" font-size="10">{100*value:.1f}%</text>'
            )
        for horizon in (30, 365):
            parts.append(
                f'<text x="{sx(left, horizon):.1f}" y="{height-bottom+17}" '
                f'text-anchor="middle" font-size="10">{horizon}d</text>'
            )
        for agent in (
            "Claude_Code",
            "Copilot",
            "Cursor",
            "Devin",
            "OpenAI_Codex",
        ):
            points = [
                row
                for row in by_key.get((language, agent), [])
                if row["survival"] is not None
            ]
            label = AGENT_LABELS.get(agent, agent)
            if not points:
                parts.append(
                    f'<text x="{left + plot_width + 16}" '
                    f'y="{height-bottom+34}" font-size="10" '
                    f'fill="{AGENT_COLORS[agent]}">'
                    f"{html.escape(label)} — not identified</text>"
                )
                continue
            polyline = " ".join(
                f"{sx(left, row['horizon_days']):.1f}," f"{sy(row['survival']):.1f}"
                for row in points
            )
            color = AGENT_COLORS[agent]
            parts.append(
                f'<polyline points="{polyline}" fill="none" '
                f'stroke="{color}" stroke-width="1.5"/>'
            )
            for row in points:
                parts.append(
                    f'<circle cx="{sx(left, row["horizon_days"]):.1f}" '
                    f'cy="{sy(row["survival"]):.1f}" r="2.3" '
                    f'fill="{color}"/>'
                )
            endpoint = points[-1]
            parts.append(
                f'<text x="{sx(left, endpoint["horizon_days"]) + 7:.1f}" '
                f'y="{sy(endpoint["survival"]) + 4:.1f}" '
                f'font-size="10" fill="{color}">{html.escape(label)}</text>'
            )
    parts.append("</svg>")
    svg = "\n".join(parts)
    if not inert_svg(svg):
        raise ValueError("rendered active SVG")
    return svg


def _comparison_html() -> str:
    return """<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>Tier 2 survival — Tufte alternatives</title>
<style>
body{margin:2rem;font-family:Georgia,serif;color:#222;background:#fff}
h1{font-weight:400} p{max-width:70rem;line-height:1.45}
.figures{display:grid;grid-template-columns:1fr;gap:2rem}
object{width:100%;min-height:34rem;border:0}
</style>
<h1>Tier 2 survival: two valid Tufte forms</h1>
<p>The small multiples preserve invariant scales and make each trajectory
legible. The overplot makes between-agent separation within each language
more immediate, at the cost of greater line competition.</p>
<div class="figures">
<object data="tier2-survival-small-multiples.svg" type="image/svg+xml"></object>
<object data="tier2-survival-overplot.svg" type="image/svg+xml"></object>
</div></html>
"""


def _small_multiple_series(
    rows: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    keys = list(
        dict.fromkeys(
            (row["language"], row["agent"])
            for row in rows
            if row["survival"] is not None
        )
    )
    return [
        (
            f"{language} · {AGENT_LABELS.get(agent, agent)}",
            agent,
            [
                row
                for row in rows
                if row["language"] == language
                and row["agent"] == agent
                and row["survival"] is not None
            ],
        )
        for language, agent in keys
    ]


def _small_multiple_panel(
    index: int, label: str, agent: str, points: list[dict[str, Any]]
) -> str:
    left = 44 + (index % 3) * 342
    top = 108 + (index // 3) * 145
    width, height = 255, 88

    def sx(value: float) -> float:
        return left + (value - 30) / 335 * width

    def sy(value: float) -> float:
        return top + (1 - value) / 0.75 * height

    path = " ".join(
        f"{sx(point['horizon_days']):.1f},{sy(point['survival']):.1f}"
        for point in points
    )
    color = AGENT_COLORS[agent]
    circles = "".join(
        f'<circle cx="{sx(point["horizon_days"]):.1f}" '
        f'cy="{sy(point["survival"]):.1f}" r="2.8" fill="{color}"/>'
        for point in points
    )
    return (
        f'<text x="{left}" y="{top - 20}" font-size="13" font-weight="600">'
        f"{html.escape(label)}</text>"
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + height}" '
        'stroke="#bbb"/><line '
        f'x1="{left}" y1="{top + height}" x2="{left + width}" '
        f'y2="{top + height}" stroke="#bbb"/>'
        f'<polyline points="{path}" fill="none" stroke="{color}" '
        f'stroke-width="1.8"/>{circles}'
    )


def render_survival_small_multiples(rows: list[dict[str, Any]]) -> str:
    """Render invariant-scale small multiples without an external dependency."""
    series = _small_multiple_series(rows)
    if not series:
        raise ValueError("no identified survival series")
    height = 145 * ((len(series) + 2) // 3) + 90
    panels = "".join(
        _small_multiple_panel(index, label, agent, points)
        for index, (label, agent, points) in enumerate(series)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 1050 {height}" font-family="Avenir, Segoe UI, sans-serif">'
        '<rect width="1050" height="100%" fill="#fff"/>'
        '<text x="44" y="34" font-size="20" font-weight="650">'
        "Tier 2 agent-attributed code survival</text>"
        '<text x="44" y="58" font-size="12" fill="#666">'
        "Repository-balanced Kaplan-Meier survival; common 25% to 100% scale"
        f"</text>{panels}</svg>"
    )
    if not inert_svg(svg):
        raise ValueError("rendered active small-multiples SVG")
    return svg


def _render_survival_with_tufte(
    rows: list[dict[str, Any]], output: Path, skill_root: Path
) -> str:
    renderer = skill_root / "scripts/small_multiples.py"
    if not renderer.is_file():
        output.write_text(render_survival_small_multiples(rows))
        return "internal_inert_svg"
    identified = [row for row in rows if row["survival"] is not None]
    data = [
        {
            "facet": (
                f"{row['language']} · "
                f"{AGENT_LABELS.get(row['agent'], row['agent'])} "
                f"(n={row['repository_count']})"
            ),
            "horizon": row["horizon_days"],
            "survival_percent": 100 * row["survival"],
        }
        for row in identified
    ]
    order = []
    for row in rows:
        if row["survival"] is None:
            continue
        label = (
            f"{row['language']} · "
            f"{AGENT_LABELS.get(row['agent'], row['agent'])} "
            f"(n={row['repository_count']})"
        )
        if label not in order:
            order.append(label)
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(data, handle)
        handle.flush()
        subprocess.run(
            [
                "python3",
                str(renderer),
                "--data-file",
                handle.name,
                "--facet-key",
                "facet",
                "--x-key",
                "horizon",
                "--y-key",
                "survival_percent",
                "--title",
                "Tier 2 agent-attributed code survival",
                "--subtitle",
                (
                    "Repository-balanced Kaplan–Meier survival with right "
                    "censoring; gated cells omitted"
                ),
                "--cols",
                "3",
                "--order",
                ",".join(order),
                "--width",
                "1050",
                "--out",
                str(output),
            ],
            check=True,
        )
    if not inert_svg(output.read_text()):
        raise ValueError("Tufte renderer produced active SVG")
    return "tufte-chart"


def build(
    estimates_path: Path,
    comparison_path: Path,
    output: Path,
    skill_root: Path,
) -> None:
    estimates = json.loads(estimates_path.read_text())
    comparison = json.loads(comparison_path.read_text())
    survival = survival_points(estimates)
    contextual = contextual_effects(comparison)
    output.mkdir(parents=True, exist_ok=True)
    (output / "tier2-survival-points.v1.json").write_text(
        json.dumps(survival, indent=2, sort_keys=True) + "\n"
    )
    (output / "contextual-effects.v1.json").write_text(
        json.dumps(contextual, indent=2, sort_keys=True) + "\n"
    )
    survival_renderer = _render_survival_with_tufte(
        survival, output / "tier2-survival-small-multiples.svg", skill_root
    )
    (output / "contextual-effects.svg").write_text(
        render_contextual_effects(contextual)
    )
    (output / "tier2-survival-overplot.svg").write_text(
        render_survival_overplot(survival)
    )
    (output / "tier2-survival-alternatives.html").write_text(_comparison_html())
    manifest = {
        "artifact": "report-figure-manifest",
        "version": 1,
        "inputs": {
            str(estimates_path): _sha(estimates_path),
            str(comparison_path): _sha(comparison_path),
        },
        "method": {
            "renderer": survival_renderer,
            "survival_genre": "C5_small_multiples",
            "contextual_genre": "C2_range_frame_interval_plot",
            **(
                {
                    "skill": "tufte-chart",
                    "source": "https://github.com/gnurio/tufte-vdqi-plugin",
                }
                if survival_renderer == "tufte-chart"
                else {}
            ),
        },
        "figures": {
            name: _sha(output / name)
            for name in (
                "tier2-survival-small-multiples.svg",
                "tier2-survival-overplot.svg",
                "tier2-survival-alternatives.html",
                "contextual-effects.svg",
            )
        },
    }
    (output / "manifest.v1.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--estimates",
        type=Path,
        default=Path("results/survival-estimates.v2.json"),
    )
    parser.add_argument(
        "--comparison",
        type=Path,
        default=Path("results/contextual-comparison.v1.json"),
    )
    parser.add_argument("--output", type=Path, default=Path("results/figures"))
    parser.add_argument(
        "--skill-root",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        / "skills/tufte-chart",
    )
    arguments = parser.parse_args()
    build(
        arguments.estimates,
        arguments.comparison,
        arguments.output,
        arguments.skill_root,
    )
    print(arguments.output / "tier2-survival-small-multiples.svg")
    print(arguments.output / "contextual-effects.svg")


if __name__ == "__main__":
    main()
