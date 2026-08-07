"""Render the exploratory commit-message proxy section for the v3 article."""

from __future__ import annotations

from html import escape
from typing import Any


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _metric_rows(analysis: dict[str, Any]) -> str:
    y_positions = {3: 105, 4: 205, 5: 305}
    rows = []
    for threshold, y in y_positions.items():
        metrics = analysis["validation"][str(threshold)]
        precision = float(metrics["precision"])
        recall = float(metrics["recall"])
        precision_x = 220 + 760 * precision
        recall_x = 220 + 760 * recall
        rows.append(
            f"""
        <text x="55" y="{y + 6}" font-size="16" font-weight="700"
          fill="oklch(22% 0.026 264)">score ≥ {threshold}</text>
        <line x1="220" y1="{y}" x2="980" y2="{y}"
          stroke="oklch(82% 0.025 272)" stroke-width="2"/>
        <circle cx="{precision_x:.1f}" cy="{y - 13}" r="10"
          fill="oklch(48% 0.2 292)"/>
        <circle cx="{recall_x:.1f}" cy="{y + 13}" r="10"
          fill="oklch(65% 0.18 46)"/>
        <text x="{min(precision_x + 16, 1040):.1f}" y="{y - 8}" font-size="14"
          fill="oklch(34% 0.16 292)">{_percent(precision)} precision</text>
        <text x="{min(recall_x + 16, 1040):.1f}" y="{y + 19}" font-size="14"
          fill="oklch(48% 0.025 260)">{_percent(recall)} recall</text>"""
        )
    return "".join(rows)


def _within_repository_rows(analysis: dict[str, Any]) -> tuple[str, int, int]:
    repositories = [
        repository
        for repository in analysis["repositories"]
        if repository["within_repository"]["status"] == "available"
    ]
    baseline_count = sum(
        repository["baseline"]["status"] == "available"
        for repository in repositories
    )
    rows = []
    for index, repository in enumerate(repositories):
        y = 88 + index * 48
        contrast = repository["within_repository"]["thresholds"]["4"]
        pre = float(contrast["pre"]["detected_commit_share"] or 0.0)
        post = float(contrast["post"]["detected_commit_share"] or 0.0)
        pre_x = 455 + 1250 * pre
        post_x = 455 + 1250 * post
        repository_name = escape(repository["repository_id"])
        rows.append(
            f"""
        <text x="35" y="{y + 5}" font-size="13"
          fill="oklch(22% 0.026 264)">{repository_name}</text>
        <line x1="{pre_x:.1f}" y1="{y}" x2="{post_x:.1f}" y2="{y}"
          stroke="oklch(78% 0.035 272)" stroke-width="5"/>
        <circle cx="{pre_x:.1f}" cy="{y}" r="7"
          fill="oklch(53% 0.15 248)"/>
        <circle cx="{post_x:.1f}" cy="{y}" r="8"
          fill="oklch(48% 0.2 292)"/>"""
        )
    return "".join(rows), len(repositories), baseline_count


def _validation_figure(analysis: dict[str, Any]) -> str:
    primary = analysis["validation"]["4"]
    return f"""
  <figure>
    <div class="figure-head">
      <h3>Precision stays high; recall is low</h3>
      <span>Post hoc validation · thresholds frozen</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 390" role="img"
        aria-labelledby="message-validation-title message-validation-desc">
        <title id="message-validation-title">Commit-message heuristic precision and recall</title>
        <desc id="message-validation-desc">Precision and recall at score thresholds three, four, and five.</desc>
        <rect width="1120" height="390" fill="oklch(98% 0.006 83)"/>
        <g font-size="12" fill="oklch(48% 0.025 260)" text-anchor="middle">
          <text x="220" y="365">0%</text><text x="410" y="365">25%</text>
          <text x="600" y="365">50%</text><text x="790" y="365">75%</text>
          <text x="980" y="365">100%</text>
        </g>
        {_metric_rows(analysis)}
      </svg>
    </div>
    <figcaption>At the primary threshold, precision is
    {_percent(primary["precision"])} but recall is only
    {_percent(primary["recall"])}. The proxy therefore identifies a selective
    subset and cannot estimate all agent-authored code. Threshold three improves
    recall to {_percent(analysis["validation"]["3"]["recall"])} with
    {_percent(analysis["validation"]["3"]["precision"])} precision.</figcaption>
  </figure>"""


def _within_repository_figure(
    rows: str, contrast_count: int, baseline_count: int
) -> str:
    return f"""
  <figure>
    <div class="figure-head">
      <h3>Within-repository message-style shift</h3>
      <span>Primary threshold · first explicit provenance date</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 470" role="img"
        aria-labelledby="message-within-title message-within-desc">
        <title id="message-within-title">Pre- and post-adoption detected commit shares</title>
        <desc id="message-within-desc">{contrast_count} repositories with commits on both sides of a dated first agent-provenance event.</desc>
        <rect width="1120" height="470" fill="oklch(98% 0.006 83)"/>
        <text x="455" y="35" font-size="13" fill="oklch(53% 0.15 248)">pre</text>
        <text x="515" y="35" font-size="13" fill="oklch(48% 0.2 292)">post</text>
        {rows}
        <g font-size="12" fill="oklch(48% 0.025 260)" text-anchor="middle">
          <text x="455" y="445">0%</text><text x="580" y="445">10%</text>
          <text x="705" y="445">20%</text><text x="830" y="445">30%</text>
          <text x="955" y="445">40%</text>
        </g>
      </svg>
    </div>
    <figcaption>{contrast_count} repositories have commits on both sides of a
    dated event, but only {baseline_count} has the preregistered minimum of 30
    contemporary pre-adoption messages. The other contrasts use absolute style
    signals only and are descriptive, not fully era-adjusted.</figcaption>
  </figure>"""


def _prevalence_warning(total: dict[str, Any]) -> str:
    share = _percent(total["agent_attributed_added_line_share"])
    return f"""
  <div class="method-band">
    <h3>Do not read {share} as prevalence</h3>
    <p>The primary rule attributes {share} of eligible added lines in this
    deliberately enriched reference sample to detected agents. The sample mixes
    agent-attested and policy-human repositories by design, so that percentage
    validates attribution mechanics; it is not a population estimate.</p>
  </div>"""


def _expansion_figure(expansion: dict[str, Any]) -> str:
    rows = []
    repositories = expansion["repositories"]
    for index, repository in enumerate(repositories):
        y = 82 + index * 58
        contrast = repository["within_repository"]["thresholds"]["4"]
        pre = float(contrast["pre"]["detected_commit_share"])
        post = float(contrast["post"]["detected_commit_share"])
        pre_x = 455 + 850 * pre
        post_x = 455 + 850 * post
        tier = (
            "confirmed"
            if repository["adoption_adjudication_status"] == "confirmed"
            else "observed"
        )
        rows.append(
            f"""
        <text x="30" y="{y + 5}" font-size="12"
          fill="oklch(22% 0.026 264)">{escape(repository["repository_id"])}</text>
        <line x1="{pre_x:.1f}" y1="{y}" x2="{post_x:.1f}" y2="{y}"
          stroke="oklch(78% 0.035 272)" stroke-width="5"/>
        <circle cx="{pre_x:.1f}" cy="{y}" r="7"
          fill="oklch(53% 0.15 248)"/>
        <circle cx="{post_x:.1f}" cy="{y}" r="8"
          fill="oklch(48% 0.2 292)"/>
        <text x="1040" y="{y + 5}" text-anchor="end" font-size="11"
          fill="oklch(48% 0.025 260)">{tier}</text>"""
        )
    confirmed = sum(
        repository["adoption_adjudication_status"] == "confirmed"
        for repository in repositories
    )
    observed = len(repositories) - confirmed
    return f"""
  <figure>
    <div class="figure-head">
      <h3>Six repositories clear the contemporary baseline gate</h3>
      <span>Frozen score four · adjudicated expansion</span>
    </div>
    <div class="graphic">
      <svg viewBox="0 0 1120 470" role="img"
        aria-labelledby="message-expansion-title message-expansion-desc">
        <title id="message-expansion-title">Pre- and post-event message-proxy detection shares in the expansion cohort</title>
        <desc id="message-expansion-desc">All six repositories have at least 30 contemporary pre-event messages and show a higher detected share after the event.</desc>
        <rect width="1120" height="470" fill="oklch(98% 0.006 83)"/>
        <text x="455" y="32" font-size="12" fill="oklch(53% 0.15 248)">pre</text>
        <text x="505" y="32" font-size="12" fill="oklch(48% 0.2 292)">post</text>
        {''.join(rows)}
        <g font-size="12" fill="oklch(48% 0.025 260)" text-anchor="middle">
          <text x="455" y="445">0%</text><text x="625" y="445">20%</text>
          <text x="795" y="445">40%</text><text x="965" y="445">60%</text>
        </g>
      </svg>
    </div>
    <figcaption>The 12-candidate Sourcegraph shortlist yields {confirmed}
    confirmed direct-agent events and {observed} observed trailer/autofix
    events; six search-polysemy hits are rejected. All six accepted events are
    ancestors of their pinned cutoffs and have 1,877-27,337 pre-event messages.
    Each share rises after the event, but this remains a descriptive workflow
    contrast rather than direct code authorship.</figcaption>
  </figure>"""


def render_commit_message_section(
    analysis: dict[str, Any],
    expansion: dict[str, Any],
) -> str:
    """Render validated, self-contained HTML for the exploratory proxy."""
    if analysis.get("study_role") != "exploratory":
        raise ValueError("commit-message analysis must be exploratory")
    if expansion.get("study_role") != "exploratory":
        raise ValueError("commit-message expansion must be exploratory")
    execution = analysis["execution"]
    primary = analysis["validation"]["4"]
    total = analysis["threshold_totals"]["4"]
    rows, contrast_count, baseline_count = _within_repository_rows(analysis)
    labeled_commits = sum(primary[key] for key in ("tp", "fp", "tn", "fn"))
    return f"""
<section class="section" id="message-proxy">
  <div class="section-head">
    <div class="section-number">05</div>
    <div>
      <h2>Commit-message style as a code-authorship proxy</h2>
      <p>This separate exploratory analysis asks whether the writing style and
      content of a commit message can identify a high-confidence subset of
      agent-written commits after direct provenance markers are stripped.</p>
    </div>
  </div>

  <div class="claim">
    <p>Detected message author equals code author is an <em>assumption, not an
    observed fact.</em></p>
  </div>

  <div class="prose">
    <p>{escape(analysis["proxy_assumption"])}</p>
    <p>The rule was frozen before scoring. It assigns one point for each of
    seven mechanical signals, uses no fitted weights, and calls a commit
    agent-detected at a primary score of four. Agent names, trailers, email
    addresses, URLs, handles, issue numbers, and revisions are removed before
    feature extraction.</p>
  </div>

  <div class="fact-run" aria-label="Commit-message proxy validation">
    <div class="fact"><b>{execution["analyzed_repository_count"]}</b><span>pinned reference repositories</span></div>
    <div class="fact"><b>{labeled_commits:,}</b><span>labeled commits in post hoc validation</span></div>
    <div class="fact"><b>{_percent(primary["precision"])}</b><span>primary precision</span></div>
    <div class="fact"><b>{_percent(primary["recall"])}</b><span>primary recall</span></div>
  </div>

  {_validation_figure(analysis)}
  {_within_repository_figure(rows, contrast_count, baseline_count)}
  {_expansion_figure(expansion)}
  {_prevalence_warning(total)}

  <div class="prose">
    <p><strong>Expansion result:</strong>
    {expansion["execution"]["analyzed_repository_count"]} of 12 discovery
    candidates are adjudicated and analyzed at their pinned cutoffs:
    2 confirmed direct-agent commits and 4 observed assisted/trailer events.
    The other 6 remain explicit rejections, including a <code>codext.de</code>
    identity falsely matched by the discovery regex.</p>
    <p>The expansion covers
    {expansion["threshold_totals"]["4"]["eligible_commit_count"]:,} commits.
    Every repository has an available repository-local pre-event baseline.
    Confirmed and observed event tiers stay separate, and no re-indexing or SCIP
    enablement is required.</p>
  </div>
</section>
"""
