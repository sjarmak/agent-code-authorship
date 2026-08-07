"""Validate and render the newest frozen study state for the HTML report."""

from __future__ import annotations

from typing import Any


def _require_counts(source: dict[str, Any], key: str) -> dict[str, int]:
    counts = source.get(key)
    if not isinstance(counts, dict) or not all(
        isinstance(name, str) and isinstance(value, int)
        for name, value in counts.items()
    ):
        raise ValueError(f"{key} must contain integer counts")
    return counts


def current_study_summary(
    *,
    adoption_catalog: dict[str, Any],
    ai_ban_catalog: dict[str, Any],
    agent_commit_catalog: dict[str, Any],
    authorship_execution: dict[str, Any],
    era_estimates: dict[str, Any],
    semantic_finalization: dict[str, Any],
    semantic_record_summary: dict[str, Any],
    repository_frame: dict[str, Any],
    packet_index_summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the structurally validated fields used by the current-results section."""
    adoption = _require_counts(adoption_catalog, "status_counts")
    ai_ban = _require_counts(ai_ban_catalog, "status_counts")
    units = _require_counts(authorship_execution, "counts")
    languages = era_estimates.get("languages")
    if not isinstance(languages, dict):
        raise ValueError("era estimates must contain language results")
    era_counts = {
        language: _require_counts(result, "status_counts")
        for language, result in languages.items()
    }
    if authorship_execution.get("status") != "complete":
        raise ValueError("authorship unit execution must be complete")
    if era_estimates.get("headline_inference_allowed") is not False:
        raise ValueError("current report expects blocked headline inference")
    semantic_record_count = semantic_finalization["semantic_record_count"]
    if semantic_record_summary.get("record_count") != semantic_record_count:
        raise ValueError("semantic record summaries must describe the same records")
    return {
        "adoption_status_counts": adoption,
        "dated_adoption_repositories": adoption_catalog["dated_repository_count"],
        "ai_ban_status_counts": ai_ban,
        "dated_ai_ban_policies": ai_ban["dated_policy"],
        "agent_commit_count": agent_commit_catalog["commit_count"],
        "agent_units": units["agent_units"],
        "H2_units": units["H2_units"],
        "H3_candidate_units": units["H3_candidate_units"],
        "successful_tasks": units["successful_tasks"],
        "failed_tasks": units["failed_tasks"],
        "semantic_record_count": semantic_record_count,
        "semantic_followed_up_count": semantic_record_summary[
            "subsequent_change_present_count"
        ],
        "semantic_different_identity_followup_count": semantic_record_summary[
            "first_followup_author_differs_count"
        ],
        "semantic_repository_count": semantic_record_summary["repository_count"],
        "canonical_repository_count": repository_frame["candidate_repository_count"],
        "indexed_repository_count": repository_frame["indexed_and_cutoff_ready"],
        "evidence_packet_count": packet_index_summary["packet_count"],
        "headline_inference_allowed": False,
        "era_status_counts": era_counts,
    }


def _bar_width(value: int, maximum: int, width: int = 620) -> int:
    if value < 0 or maximum <= 0 or value > maximum:
        raise ValueError("bar values must fit the declared range")
    return round(width * value / maximum)


def _adjudication_figure(summary: dict[str, Any]) -> str:
    adoption = summary["adoption_status_counts"]
    ai_ban = summary["ai_ban_status_counts"]
    adoption_total = sum(adoption.values())
    ai_ban_total = sum(ai_ban.values())
    explicit = _bar_width(adoption["dated_explicit_anchor"], adoption_total)
    sequential = _bar_width(adoption["dated_sequential"], adoption_total)
    policies = _bar_width(ai_ban["dated_policy"], ai_ban_total)
    rejected_policies = ai_ban_total - ai_ban["dated_policy"]
    return f"""
  <figure>
    <div class="figure-head"><h3>Only {adoption["dated_explicit_anchor"]} adoption dates have a direct anchor</h3><span>{adoption_total} candidate repositories · Direct labels</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 390" role="img" aria-labelledby="adjudication-title adjudication-desc">
        <title id="adjudication-title">Adoption and AI-ban adjudication outcomes</title>
        <desc id="adjudication-desc">Of {adoption_total} adoption candidates, {summary["dated_adoption_repositories"]} received dates. Of {ai_ban_total} AI-ban repositories, {summary["dated_ai_ban_policies"]} have dated admissible policies.</desc>
        <rect width="920" height="390" fill="var(--viz-bg)"/>
        <text x="42" y="48" font-size="17" font-weight="650" fill="var(--viz-ink)">Adoption candidates · {adoption_total} repositories</text>
        <rect x="42" y="82" width="620" height="32" rx="4" fill="var(--viz-rule)"/>
        <rect x="42" y="82" width="{explicit}" height="32" fill="var(--viz-blog-purple)"/>
        <rect x="{42 + explicit}" y="82" width="{sequential}" height="32" fill="var(--viz-accent)"/>
        <text x="42" y="143" font-size="15" fill="var(--viz-ink)">{adoption["dated_explicit_anchor"]} explicit anchors</text>
        <text x="265" y="143" font-size="15" fill="var(--viz-ink)">{adoption["dated_sequential"]} sequential dates</text>
        <text x="545" y="143" font-size="15" fill="var(--viz-muted)">{adoption["no_credible_event"]} no credible event</text>
        <text x="42" y="220" font-size="17" font-weight="650" fill="var(--viz-ink)">AI-ban controls · {ai_ban_total} repositories</text>
        <rect x="42" y="254" width="620" height="32" rx="4" fill="var(--viz-rule)"/>
        <rect x="42" y="254" width="{policies}" height="32" fill="var(--viz-green)"/>
        <text x="42" y="315" font-size="15" fill="var(--viz-ink)">{summary["dated_ai_ban_policies"]} dated admissible policies</text>
        <text x="405" y="315" font-size="15" fill="var(--viz-muted)">{rejected_policies} rejected or without a frozen candidate</text>
        <text x="42" y="360" font-size="13" fill="var(--viz-muted)">Outcomes were adjudicated without consulting classifier or survival results.</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Adjudication outcome values">
      <div><dt>Explicit adoption anchors</dt><dd>{adoption["dated_explicit_anchor"]}</dd></div>
      <div><dt>Sequential adoption dates</dt><dd>{adoption["dated_sequential"]}</dd></div>
      <div><dt>No credible adoption event</dt><dd>{adoption["no_credible_event"]}</dd></div>
      <div><dt>Dated AI-ban policies</dt><dd>{summary["dated_ai_ban_policies"]}</dd></div>
      <div><dt>Rejected or unavailable controls</dt><dd>{rejected_policies}</dd></div>
    </dl>
    <figcaption>A direct anchor is a verifiable agent event. A sequential date is reconstructed from an ordered evidence trail. The remaining {adoption["no_credible_event"]} repositories keep an explicit no-event outcome. The AI-ban frame is shown separately because it has a different denominator.</figcaption>
  </figure>"""


def _unit_figure(summary: dict[str, Any]) -> str:
    rows = (
        ("H2 provenance units", summary["H2_units"], "var(--viz-blog-purple)"),
        ("H3 candidate units", summary["H3_candidate_units"], "var(--viz-accent)"),
        ("Explicit agent units", summary["agent_units"], "var(--viz-blog-coral)"),
    )
    maximum = max(value for _, value, _ in rows)
    marks = []
    for index, (label, value, color) in enumerate(rows):
        y = 100 + index * 90
        width = _bar_width(value, maximum, 590)
        marks.append(
            f'<text x="42" y="{y - 16}" font-size="15" fill="var(--viz-ink)">{label}</text>'
            f'<line x1="42" y1="{y}" x2="632" y2="{y}" stroke="var(--viz-rule)" stroke-width="10" stroke-linecap="round"/>'
            f'<line x1="42" y1="{y}" x2="{42 + width}" y2="{y}" stroke="{color}" stroke-width="10" stroke-linecap="round"/>'
            f'<circle cx="{42 + width}" cy="{y}" r="8" fill="{color}"/>'
            f'<text x="680" y="{y + 6}" font-size="24" font-weight="650" fill="var(--viz-ink)">{value:,}</text>'
        )
    return f"""
  <figure>
    <div class="figure-head"><h3>Explicit provenance is the narrowest evidence tier</h3><span>{summary["successful_tasks"]:,} successful tasks · shared zero scale</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 390" role="img" aria-labelledby="units-title units-desc">
        <title id="units-title">Materialized Sourcegraph authorship units</title>
        <desc id="units-desc">The execution contains {summary["H2_units"]:,} H2 units, {summary["H3_candidate_units"]:,} H3 candidate units, and {summary["agent_units"]:,} explicit agent units.</desc>
        <rect width="920" height="390" fill="var(--viz-bg)"/>
        {"".join(marks)}
        <text x="42" y="352" font-size="13" fill="var(--viz-muted)">Shared linear scale. Workflow categories are not a prevalence denominator.</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Materialized authorship-unit values">
      <div><dt>H2 provenance units</dt><dd>{summary["H2_units"]:,}</dd></div>
      <div><dt>H3 candidate units</dt><dd>{summary["H3_candidate_units"]:,}</dd></div>
      <div><dt>Explicit agent units</dt><dd>{summary["agent_units"]:,}</dd></div>
      <div><dt>Successful / failed tasks</dt><dd>{summary["successful_tasks"]:,} / {summary["failed_tasks"]}</dd></div>
    </dl>
    <figcaption>The index-backed workflow materialized {summary["agent_units"]:,} explicit agent units from {summary["agent_commit_count"]} provenance-positive commits. H2 and H3 are separate evidence categories, not a prevalence denominator, so the three values must not be added or converted into an authorship share.</figcaption>
  </figure>"""


def _semantic_followup_figure(summary: dict[str, Any]) -> str:
    total = summary["semantic_record_count"]
    followed_up = summary["semantic_followed_up_count"]
    different_identity = summary["semantic_different_identity_followup_count"]
    width = 690
    followed_width = _bar_width(followed_up, total, width)
    identity_width = _bar_width(different_identity, total, width)
    followed_percent = 100 * followed_up / total
    identity_percent = 100 * different_identity / total
    return f"""
  <figure>
    <div class="figure-head"><h3>Most changes did not sit still</h3><span>{total} semantic records · {summary["semantic_repository_count"]} pilot repositories</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 390" role="img" aria-labelledby="followup-title followup-desc">
        <title id="followup-title">Follow-up history for semantic change records</title>
        <desc id="followup-desc">Of {total} records, {followed_up} changed again on the same path and {different_identity} different first follow-up identity records were observed.</desc>
        <rect width="920" height="390" fill="var(--viz-blog-black)"/>
        <text x="42" y="62" font-size="16" font-weight="650" fill="var(--viz-ink)">All materialized records</text>
        <rect x="42" y="82" width="{width}" height="34" fill="var(--viz-blog-grid)"/>
        <text x="760" y="108" font-size="22" font-weight="650" fill="var(--viz-ink)">{total}</text>
        <text x="42" y="174" font-size="16" font-weight="650" fill="var(--viz-ink)">Changed again on the same path</text>
        <rect x="42" y="194" width="{followed_width}" height="34" fill="var(--viz-blog-purple)"/>
        <text x="760" y="220" font-size="22" font-weight="650" fill="var(--viz-ink)">{followed_up} · {followed_percent:.1f}%</text>
        <text x="42" y="286" font-size="16" font-weight="650" fill="var(--viz-ink)">First follow-up had a different commit identity</text>
        <rect x="42" y="306" width="{identity_width}" height="34" fill="var(--viz-blog-coral)"/>
        <text x="760" y="332" font-size="22" font-weight="650" fill="var(--viz-ink)">{different_identity} · {identity_percent:.1f}%</text>
        <text x="42" y="374" font-size="13" fill="var(--viz-muted)">Nested against all {total} records. Identity means normalized Git name and email, not a person.</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Semantic change follow-up values">
      <div><dt>Semantic records</dt><dd>{total}</dd></div>
      <div><dt>Changed again</dt><dd>{followed_up} · {followed_percent:.1f}%</dd></div>
      <div><dt>Different first follow-up identity</dt><dd>{different_identity} · {identity_percent:.1f}%</dd></div>
    </dl>
    <figcaption>The reliable topology fields describe what happened after a change. They do not identify who wrote the original code or whether a different commit identity represents a different person. This is evidence of continued repository activity, not a claim about human authorship.</figcaption>
  </figure>"""


def render_current_study_section(summary: dict[str, Any]) -> str:
    """Render the lead findings from the completed indexed study."""
    return f"""
<section class="section" id="findings">
  <div class="section-head">
    <div class="section-number">01</div>
    <div>
      <h2>What indexed history reveals</h2>
      <p>Repository-scale search turns AI adoption into an observable sequence: candidate traces, dated events, provenance-positive commits, and later changes. The useful result is the shape of that trail, not one universal authorship percentage.</p>
    </div>
  </div>
  <div class="finding-strip" aria-label="Current frozen study state">
    <p><strong>{summary["dated_adoption_repositories"]}</strong> dated adoption histories</p>
    <p><strong>{summary["agent_commit_count"]}</strong> provenance-positive commits</p>
    <p><strong>{summary["semantic_record_count"]}</strong> semantic changes followed through history</p>
  </div>
</section>
<section class="section" id="adoption">
  <div class="section-head"><div class="section-number">02</div><div><h2>Adoption is usually a trail, not a timestamp</h2><p>Sourcegraph search found candidate evidence in {sum(summary["adoption_status_counts"].values())} repositories. Review produced {summary["dated_adoption_repositories"]} dated adoption events, but fewer than half of those dates came from one direct anchor.</p></div></div>
  {_adjudication_figure(summary)}
</section>
<section class="section" id="follow-up">
  <div class="section-head"><div class="section-number">03</div><div><h2>Code keeps moving after the first change</h2><p>The semantic pilot followed materialized changes forward on the same path. Continued edits were common, and the first follow-up often carried a different normalized commit identity.</p></div></div>
  {_semantic_followup_figure(summary)}
</section>
<section class="section" id="provenance">
  <div class="section-head"><div class="section-number">04</div><div><h2>Attribution gets narrower as the claim gets stronger</h2><p>Search can surface a broad evidence frame. Direct agent attribution requires provenance tied to the code-introducing commit, which leaves a smaller but more defensible set.</p></div></div>
  {_unit_figure(summary)}
</section>"""
