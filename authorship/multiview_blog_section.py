"""Render the exploratory multi-view commit-detector section."""

from __future__ import annotations

from html import escape
from typing import Any


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _held_out(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evaluations = analysis["evaluations"]["leave_one_repository_out"]
    if set(evaluations) != {"message", "timing_process", "combined"}:
        raise ValueError("all three frozen multi-view evaluations are required")
    if any(result.get("status") != "available" for result in evaluations.values()):
        raise ValueError("all held-repository evaluations must be available")
    return evaluations


def _performance_figure(evaluations: dict[str, dict[str, Any]]) -> str:
    labels = {
        "message": "Message",
        "timing_process": "Timing/process",
        "combined": "Combined",
    }
    rows = []
    for index, view in enumerate(("message", "timing_process", "combined")):
        result = evaluations[view]
        y = 105 + index * 100
        auc = float(result["roc_auc"])
        average_precision = float(result["average_precision"])
        rows.append(
            f"""
        <text x="45" y="{y + 5}" font-size="16" font-weight="700"
          fill="oklch(22% 0.026 264)">{labels[view]}</text>
        <line x1="245" y1="{y - 13}" x2="{245 + 730 * auc:.1f}"
          y2="{y - 13}" stroke="oklch(48% 0.2 292)" stroke-width="15"/>
        <line x1="245" y1="{y + 17}"
          x2="{245 + 730 * average_precision:.1f}" y2="{y + 17}"
          stroke="oklch(65% 0.18 46)" stroke-width="15"/>
        <text x="{min(265 + 730 * auc, 1035):.1f}" y="{y - 7}"
          font-size="13" fill="oklch(34% 0.16 292)">{_percent(auc)} AUC</text>
        <text x="{min(265 + 730 * average_precision, 1035):.1f}" y="{y + 23}"
          font-size="13" fill="oklch(48% 0.025 260)">{_percent(average_precision)} AP</text>"""
        )
    return f"""
  <figure>
    <div class="figure-head">
      <h3>Timing helps ranking, but remains weak</h3>
      <span>Leave-one-repository-out · frozen model</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 400" role="img"
        aria-labelledby="multiview-performance-title multiview-performance-desc">
        <title id="multiview-performance-title">Held-repository performance by evidence view</title>
        <desc id="multiview-performance-desc">ROC AUC and average precision for message, timing and process, and combined commit evidence.</desc>
        <rect width="1120" height="400" fill="oklch(98% 0.006 83)"/>
        <line x1="610" y1="55" x2="610" y2="335"
          stroke="oklch(78% 0.035 272)" stroke-dasharray="5 5"/>
        <text x="610" y="365" text-anchor="middle" font-size="13"
          fill="oklch(48% 0.025 260)">50% reference</text>
        {''.join(rows)}
      </svg>
    </div>
    <figcaption>Timing/process reaches
    {_percent(evaluations["timing_process"]["roc_auc"])} ROC AUC, compared with
    {_percent(evaluations["message"]["roc_auc"])} for the fitted message view.
    Combining the views falls to
    {_percent(evaluations["combined"]["roc_auc"])}. These are ranking metrics
    across repositories, not evidence that a threshold proves authorship.</figcaption>
  </figure>"""


def _human_metrics(
    evaluations: dict[str, dict[str, Any]],
    view: str,
) -> list[dict[str, Any]]:
    return [
        row
        for row in evaluations[view]["repository_metrics"]
        if row["reference_label"] == "human"
    ]


def _false_positive_rate(rows: list[dict[str, Any]]) -> float:
    commits = sum(int(row["commit_count"]) for row in rows)
    false_positives = sum(int(row["false_positive_count"]) for row in rows)
    return false_positives / commits


def _false_positive_figure(
    evaluations: dict[str, dict[str, Any]],
) -> tuple[str, float, float]:
    timing = _human_metrics(evaluations, "timing_process")
    combined = _human_metrics(evaluations, "combined")
    combined_by_repository = {row["repository_id"]: row for row in combined}
    rows = []
    for index, timing_row in enumerate(timing):
        repository_id = str(timing_row["repository_id"])
        combined_row = combined_by_repository[repository_id]
        y = 82 + index * 58
        timing_rate = float(timing_row["false_positive_rate"])
        combined_rate = float(combined_row["false_positive_rate"])
        rows.append(
            f"""
        <text x="30" y="{y + 5}" font-size="12"
          fill="oklch(22% 0.026 264)">{escape(repository_id)}</text>
        <line x1="405" y1="{y - 9}" x2="{405 + 600 * timing_rate:.1f}"
          y2="{y - 9}" stroke="oklch(53% 0.15 248)" stroke-width="12"/>
        <line x1="405" y1="{y + 10}" x2="{405 + 600 * combined_rate:.1f}"
          y2="{y + 10}" stroke="oklch(48% 0.2 292)" stroke-width="12"/>"""
        )
    timing_rate = _false_positive_rate(timing)
    combined_rate = _false_positive_rate(combined)
    figure = f"""
  <figure>
    <div class="figure-head">
      <h3>AI-ban controls expose repository shift</h3>
      <span>False-positive rate at probability ≥ 0.5</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 470" role="img"
        aria-labelledby="multiview-fp-title multiview-fp-desc">
        <title id="multiview-fp-title">False positives in six policy-human repositories</title>
        <desc id="multiview-fp-desc">Timing and combined false-positive rates vary substantially by held-out repository.</desc>
        <rect width="1120" height="470" fill="oklch(98% 0.006 83)"/>
        {''.join(rows)}
        <g font-size="12" fill="oklch(48% 0.025 260)" text-anchor="middle">
          <text x="405" y="445">0%</text><text x="705" y="445">50%</text>
          <text x="1005" y="445">100%</text>
        </g>
        <text x="405" y="35" font-size="12" fill="oklch(53% 0.15 248)">timing/process</text>
        <text x="535" y="35" font-size="12" fill="oklch(48% 0.2 292)">combined</text>
      </svg>
    </div>
    <figcaption>Across 11,126 dated policy-human commits, timing/process produces
    {_percent(timing_rate)} false positives and the combined model produces
    {_percent(combined_rate)}. The spread from 0% to roughly 90% shows that
    repository-specific workflow dominates a universal threshold.</figcaption>
  </figure>"""
    return figure, timing_rate, combined_rate


def render_multiview_section(analysis: dict[str, Any]) -> str:
    """Render validated, self-contained HTML for multi-view evidence."""
    if analysis.get("study_role") != "exploratory":
        raise ValueError("multi-view analysis must be exploratory")
    evaluations = _held_out(analysis)
    false_positive_figure, timing_fp, combined_fp = _false_positive_figure(
        evaluations
    )
    timing = evaluations["timing_process"]
    era = analysis["evaluations"]["era_holdout"]["timing_process"]
    if era.get("status") != "unavailable":
        raise ValueError("the frozen era holdout is expected to be unavailable")
    execution = analysis["execution"]
    return f"""
<section class="section" id="commit-detector">
  <div class="section-head">
    <div class="section-number">06</div>
    <div>
      <h2>Timing is a ranking signal, not a verdict</h2>
      <p>A second frozen analysis compares message, timing/process, and combined
      evidence. It uses author and committer clocks, burst cadence, topology,
      and rename-aware diff shape without feeding labels, repository identity,
      hashed author identity, or explicit provenance into the model.</p>
    </div>
  </div>

  <div class="fact-run" aria-label="Multi-view commit detector validation">
    <div class="fact"><b>{execution["analyzed_repository_count"]}</b><span>held-reference repositories</span></div>
    <div class="fact"><b>{execution["labeled_commit_count"]:,}</b><span>labeled commits</span></div>
    <div class="fact"><b>{_percent(timing["roc_auc"])}</b><span>timing/process ROC AUC</span></div>
    <div class="fact"><b>{_percent(timing_fp)}</b><span>timing false positives in AI-ban controls</span></div>
  </div>

  {_performance_figure(evaluations)}
  {false_positive_figure}

  <div class="prose">
    <p>The frozen era holdout is unavailable: its 688 training commits and
    25,932 test commits do not each contain both reference classes. The cutoff
    was not moved after seeing this result.</p>
    <p>The nine agent references and six policy-human references assign one
    label to an entire dated repository window. Leave-one-repository-out
    evaluation reduces direct repository memorization, but it cannot remove
    owner, era, workflow, or reference-construction confounding.</p>
    <p><strong>No “likely agent-driven” or “hybrid” outcomes are assigned.</strong>
    The artifact emits verified-agent only where direct provenance or a Tier-1
    maintainer attestation exists; everything else remains
    unknown-or-human-control. At the frozen threshold, the false-positive burden
    is too high to promote model scores into authorship labels.</p>
  </div>

  <div class="method-band">
    <h3>The commit-to-code proxy remains disclosed</h3>
    <p>When a commit is independently detected as agent-driven, this
    investigation treats all source lines added by that commit as agent code.
    That is the authorized study assumption, not proof that the message writer,
    committer, and code author are always the same actor. Git timestamps can
    also be rewritten by rebases, squashes, imports, or deliberate editing.</p>
    <p>Both the frozen protocol and the 76 MB evidence ledger retain pinned Git
    cutoffs and the Sourcegraph asset reference. This layer uses already indexed
    <code>sg-evals</code> repositories and requires neither re-indexing nor SCIP.</p>
  </div>
</section>
"""
