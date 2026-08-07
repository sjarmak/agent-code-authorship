"""Build and render the evidence-backed agent-footprint findings."""

from __future__ import annotations

from typing import Any


_HARNESS_SPECS = (
    ("Claude Code", "agent_family=Claude_Code"),
    ("Codex", "agent_family=OpenAI_Codex"),
    ("Copilot", "agent_family=Copilot"),
    ("Cursor", "agent_family=Cursor"),
)
_LANGUAGE_SPECS = (("Go", "language=Go"), ("Python", "language=Python"))


def _stratum(source: dict[str, Any], stratum_id: str) -> dict[str, Any]:
    matches = [row for row in source.get("strata", []) if row.get("stratum_id") == stratum_id]
    if len(matches) != 1 or matches[0].get("status") != "identified":
        raise ValueError(f"identified survival stratum is required: {stratum_id}")
    return matches[0]


def _point(points: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    matches = [point for point in points if point.get("horizon_days") == horizon]
    if len(matches) != 1:
        raise ValueError(f"one survival point is required at {horizon} days")
    return matches[0]


def _survival_row(source: dict[str, Any], name: str, stratum_id: str) -> dict[str, Any]:
    row = _stratum(source, stratum_id)
    estimate = row["estimate"]
    curve = []
    for horizon in source["horizons_days"]:
        value = _point(estimate["primary"]["kaplan_meier"], horizon)
        interval = _point(estimate["bootstrap"]["primary"]["kaplan_meier"], horizon)
        curve.append(
            {
                "days": horizon,
                "survival": value["survival"],
                "lower": interval["lower"],
                "upper": interval["upper"],
            }
        )
    states = _point(estimate["primary"]["aalen_johansen"], 365)["state_occupancy"]
    return {
        "name": name,
        "repository_count": row["repository_count"],
        "line_count": row["line_count"],
        "curve": curve,
        "survival_365": curve[-1]["survival"],
        "lower_365": curve[-1]["lower"],
        "upper_365": curve[-1]["upper"],
        "unchanged_365": states["unchanged"],
        "modified_365": states["modified"],
        "deleted_365": states["deleted"],
        "line_weighted_survival_365": _point(
            estimate["secondary"]["kaplan_meier"], 365
        )["survival"],
    }


def _with_survivor_mass(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    masses = [row["line_count"] * row["line_weighted_survival_365"] for row in rows]
    total = sum(masses)
    if total <= 0:
        raise ValueError("language survivor mass must be positive")
    return [
        {**row, "surviving_line_mass_365": mass, "survivor_mass_share_365": mass / total}
        for row, mass in zip(rows, masses, strict=True)
    ]


def footprint_summary(
    *,
    survival_estimates: dict[str, Any],
    agent_commit_catalog: dict[str, Any],
    era_estimates: dict[str, Any],
    repository_frame: dict[str, Any],
) -> dict[str, Any]:
    """Extract only frozen fields that support the article's claims."""
    if not all(survival_estimates.get("invariants", {}).values()):
        raise ValueError("survival estimator invariants must pass")
    if era_estimates.get("headline_inference_allowed") is not False:
        raise ValueError("the footprint share must retain its not-identified status")
    overall = _survival_row(survival_estimates, "All measured agent code", "overall")
    harnesses = [
        _survival_row(survival_estimates, name, stratum_id)
        for name, stratum_id in _HARNESS_SPECS
    ]
    additional_harness = _survival_row(
        survival_estimates, "Devin", "agent_family=Devin"
    )
    languages = _with_survivor_mass(
        [
            _survival_row(survival_estimates, name, stratum_id)
            for name, stratum_id in _LANGUAGE_SPECS
        ]
    )
    return {
        "canonical_repository_count": repository_frame["candidate_repository_count"],
        "indexed_repository_count": repository_frame["indexed_and_cutoff_ready"],
        "provenance_commit_count": agent_commit_catalog["commit_count"],
        "provenance_repository_count": agent_commit_catalog["repository_count"],
        "survival_repository_count": survival_estimates["counts"]["repositories"],
        "survival_line_count": survival_estimates["counts"]["lines"],
        "commit_share_status": "not_identified",
        "revert_lift_status": "not_measured",
        "overall": overall,
        "harnesses": harnesses,
        "displayed_harness_repository_count": sum(
            row["repository_count"] for row in harnesses
        ),
        "additional_harness": additional_harness,
        "languages": languages,
    }


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _state_figure(summary: dict[str, Any]) -> str:
    overall = summary["overall"]
    states = (
        ("Unchanged", overall["unchanged_365"], "var(--viz-blog-purple)"),
        ("Modified", overall["modified_365"], "var(--viz-blog-coral)"),
        ("Deleted", overall["deleted_365"], "var(--viz-blog-grid)"),
    )
    x, width = 42, 796
    rectangles, labels, mobile = [], [], []
    offset = x
    for index, (label, value, color) in enumerate(states):
        segment = width * value
        rectangles.append(
            f'<rect x="{offset:.1f}" y="105" width="{segment:.1f}" height="58" fill="{color}"/>'
        )
        labels.append(
            f'<text x="{42 + index * 270}" y="238" font-size="18" fill="var(--viz-ink)">{label}  {_percent(value)}</text>'
        )
        mobile.append(f"<div><dt>{label}</dt><dd>{_percent(value)}</dd></div>")
        offset += segment
    return f"""
  <figure>
    <div class="figure-head"><h3>Most lines still exist one year later</h3><span>{summary['survival_repository_count']} repositories · Direct labels</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 310" role="img" aria-labelledby="state-title state-desc">
        <title id="state-title">State of agent-attributed code after 365 days</title>
        <desc id="state-desc">After one year, {_percent(overall['unchanged_365'])} is unchanged, {_percent(overall['modified_365'])} is modified, and {_percent(overall['deleted_365'])} is deleted using repository-balanced estimates.</desc>
        <rect width="920" height="310" fill="var(--viz-blog-black)"/>
        <text x="42" y="63" font-size="16" fill="var(--viz-muted)">Repository-balanced state after 365 days</text>
        {''.join(rectangles)}
        {''.join(labels)}
        <text x="42" y="282" font-size="14" fill="var(--viz-muted)">Survival = unchanged + modified = {_percent(overall['survival_365'])}</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Agent code state after one year">{''.join(mobile)}<div><dt>Surviving</dt><dd>{_percent(overall['survival_365'])}</dd></div></dl>
    <figcaption>Among {summary['survival_line_count']:,} agent-attributed Go and Python lines in {summary['survival_repository_count']} repositories, the repository-balanced Kaplan-Meier estimate is {_percent(overall['survival_365'])} surviving at 365 days. The competing-state estimate separates {_percent(overall['unchanged_365'])} unchanged from {_percent(overall['modified_365'])} modified. The {summary['provenance_commit_count']}-commit catalog and this survival cohort are separate frozen frames. This estimates durability after introduction; the share of all code at HEAD needs a different denominator.</figcaption>
  </figure>"""


def _harness_figure(summary: dict[str, Any]) -> str:
    def x(value: float) -> float:
        return 300 + 540 * (value - 0.5) / 0.5

    harnesses = summary["harnesses"]
    highest = max(harnesses, key=lambda row: row["survival_365"])
    lowest = min(harnesses, key=lambda row: row["survival_365"])
    minimum_repositories = min(row["repository_count"] for row in harnesses)
    maximum_repositories = max(row["repository_count"] for row in harnesses)
    displayed_repositories = sum(row["repository_count"] for row in harnesses)
    marks, mobile = [], []
    for index, row in enumerate(harnesses):
        y = 105 + index * 78
        marks.append(
            f'<text x="42" y="{y + 6}" font-size="17" fill="var(--viz-ink)">{row["name"]}</text>'
            f'<line x1="{x(row["lower_365"]):.1f}" y1="{y}" x2="{x(row["upper_365"]):.1f}" y2="{y}" stroke="var(--viz-muted)" stroke-width="3"/>'
            f'<line x1="{x(row["lower_365"]):.1f}" y1="{y - 8}" x2="{x(row["lower_365"]):.1f}" y2="{y + 8}" stroke="var(--viz-muted)"/>'
            f'<line x1="{x(row["upper_365"]):.1f}" y1="{y - 8}" x2="{x(row["upper_365"]):.1f}" y2="{y + 8}" stroke="var(--viz-muted)"/>'
            f'<circle cx="{x(row["survival_365"]):.1f}" cy="{y}" r="9" fill="var(--viz-blog-coral)"/>'
            f'<text x="858" y="{y + 6}" font-size="17" font-weight="650" fill="var(--viz-ink)">{_percent(row["survival_365"])}</text>'
        )
        mobile.append(
            f'<div><dt>{row["name"]} · {row["repository_count"]} repos</dt><dd>{_percent(row["survival_365"])}</dd></div>'
        )
    return f"""
  <figure>
    <div class="figure-head"><h3>Harness differences are visible, but uncertain</h3><span>365-day survival · 95% intervals</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 440" role="img" aria-labelledby="harness-title harness-desc">
        <title id="harness-title">One-year survival by coding-agent harness</title>
        <desc id="harness-desc">Repository-balanced 365-day estimates are shown for Claude Code, Codex, Copilot, and Cursor with repository-bootstrap 95 percent intervals.</desc>
        <rect width="920" height="440" fill="var(--viz-blog-black)"/>
        <line x1="300" y1="62" x2="840" y2="62" stroke="var(--viz-blog-grid)"/>
        <g font-size="13" fill="var(--viz-muted)" text-anchor="middle"><text x="300" y="44">50%</text><text x="570" y="44">75%</text><text x="840" y="44">100%</text></g>
        {''.join(marks)}
        <text x="42" y="414" font-size="13" fill="var(--viz-muted)">Dots are estimates. Whiskers are repository-bootstrap 95% intervals.</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="One-year survival by harness">{''.join(mobile)}</dl>
    <figcaption>{highest['name']} is highest at {_percent(highest['survival_365'])}; {lowest['name']} is lowest at {_percent(lowest['survival_365'])}. The intervals overlap, the repository counts range from {minimum_repositories} to {maximum_repositories}, and the cohort is observational. The overall estimate also includes {summary['additional_harness']['repository_count']} Devin repositories; this view shows the four requested harnesses across {displayed_repositories} repositories. Treat it as a durability comparison, not a harness leaderboard.</figcaption>
  </figure>"""


def _language_figure(summary: dict[str, Any]) -> str:
    marks, mobile = [], []
    for index, row in enumerate(summary["languages"]):
        y = 100 + index * 130
        width = 700 * row["survivor_mass_share_365"]
        marks.append(
            f'<text x="42" y="{y - 18}" font-size="18" fill="var(--viz-ink)">{row["name"]}</text>'
            f'<rect x="42" y="{y}" width="700" height="38" fill="var(--viz-blog-grid)"/>'
            f'<rect x="42" y="{y}" width="{width:.1f}" height="38" fill="var(--viz-blog-purple)"/>'
            f'<text x="770" y="{y + 28}" font-size="20" font-weight="650" fill="var(--viz-ink)">{_percent(row["survivor_mass_share_365"])}</text>'
            f'<text x="42" y="{y + 65}" font-size="13" fill="var(--viz-muted)">{row["line_count"]:,} starting lines · {_percent(row["survival_365"])} repository-balanced survival</text>'
        )
        mobile.append(
            f'<div><dt>{row["name"]} survivor mass</dt><dd>{_percent(row["survivor_mass_share_365"])}</dd></div>'
        )
    return f"""
  <figure>
    <div class="figure-head"><h3>Most measured survivor mass is Go</h3><span>Line-weighted cohort composition</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 390" role="img" aria-labelledby="language-title language-desc">
        <title id="language-title">Estimated 365-day survivor mass by language</title>
        <desc id="language-desc">The measured cohort contains Go and Python. Bars show each language's share of estimated line-weighted survivor mass after 365 days.</desc>
        <rect width="920" height="390" fill="var(--viz-blog-black)"/>
        {''.join(marks)}
        <text x="42" y="360" font-size="13" fill="var(--viz-muted)">Shares describe this two-language cohort, not open source as a whole.</text>
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Survivor mass by language">{''.join(mobile)}</dl>
    <figcaption>Go accounts for {_percent(summary['languages'][0]['survivor_mass_share_365'])} of estimated 365-day survivor line mass in this cohort; Python accounts for {_percent(summary['languages'][1]['survivor_mass_share_365'])}. Volume is line-weighted and dominated by large repositories, while the displayed survival rates are repository-balanced. No other languages were measured in this survival artifact.</figcaption>
  </figure>"""


def _age_figure(summary: dict[str, Any]) -> str:
    curve = summary["overall"]["curve"]
    xs = [125, 340, 555, 770]

    def y(value: float) -> float:
        return 350 - 1200 * (value - 0.75)

    path = " ".join(
        f"{'M' if index == 0 else 'L'} {x_value} {y(row['survival']):.1f}"
        for index, (x_value, row) in enumerate(zip(xs, curve, strict=True))
    )
    marks, mobile = [], []
    for x_value, row in zip(xs, curve, strict=True):
        marks.append(
            f'<line x1="{x_value}" y1="{y(row["lower"]):.1f}" x2="{x_value}" y2="{y(row["upper"]):.1f}" stroke="var(--viz-muted)" stroke-width="2"/>'
            f'<circle cx="{x_value}" cy="{y(row["survival"]):.1f}" r="8" fill="var(--viz-blog-coral)"/>'
            f'<text x="{x_value}" y="{y(row["survival"]) - 18:.1f}" font-size="16" font-weight="650" fill="var(--viz-ink)" text-anchor="middle">{_percent(row["survival"])}</text>'
            f'<text x="{x_value}" y="390" font-size="14" fill="var(--viz-muted)" text-anchor="middle">{row["days"]} days</text>'
        )
        mobile.append(f'<div><dt>{row["days"]} days</dt><dd>{_percent(row["survival"])}</dd></div>')
    return f"""
  <figure>
    <div class="figure-head"><h3>The largest measured drop arrives between six and twelve months</h3><span>Age since introduction · repository-balanced</span></div>
    <div class="graphic">
      <svg viewBox="0 0 920 430" role="img" aria-labelledby="age-title age-desc">
        <title id="age-title">Survival of agent-attributed lines by age</title>
        <desc id="age-desc">Survival is {_percent(curve[0]['survival'])} at 30 days, {_percent(curve[1]['survival'])} at 90 days, {_percent(curve[2]['survival'])} at 180 days, and {_percent(curve[3]['survival'])} at 365 days.</desc>
        <rect width="920" height="430" fill="var(--viz-blog-black)"/>
        <g stroke="var(--viz-blog-grid)"><line x1="70" y1="50" x2="840" y2="50"/><line x1="70" y1="170" x2="840" y2="170"/><line x1="70" y1="290" x2="840" y2="290"/></g>
        <g font-size="13" fill="var(--viz-muted)"><text x="20" y="55">100%</text><text x="27" y="175">90%</text><text x="27" y="295">80%</text></g>
        <path d="{path}" fill="none" stroke="var(--viz-blog-purple)" stroke-width="5" stroke-linejoin="round"/>
        {''.join(marks)}
      </svg>
    </div>
    <dl class="current-mobile-data" aria-label="Survival by code age">{''.join(mobile)}</dl>
    <figcaption>Age means days since an agent-attributed line first reached the default branch. This curve answers how long those lines persist. It does not describe the age distribution of every agent-attributed line currently standing at HEAD.</figcaption>
  </figure>"""


def render_footprint_sections(summary: dict[str, Any]) -> str:
    """Render the four supported findings and two explicit evidence gaps."""
    return f"""
<section class="section" id="footprint">
  <div class="section-head"><div class="section-number">01</div><div><h2>Known agent code has a measurable afterlife</h2><p>The index found {summary['provenance_commit_count']} provenance-positive commits across {summary['provenance_repository_count']} repositories. These visible positives form a defensible lower bound and leave the share of all open-source commits unknown.</p></div></div>
  <div class="finding-strip" aria-label="Current footprint findings">
    <p><strong>{_percent(summary['overall']['survival_365'])}</strong> survives at 365 days</p>
    <p><strong>{_percent(summary['overall']['unchanged_365'])}</strong> remains unchanged</p>
    <p><strong>Not identified</strong> share of all commits</p>
  </div>
{_state_figure(summary)}
</section>
<section class="section" id="harnesses">
  <div class="section-head"><div class="section-number">02</div><div><h2>Durability varies more than the headline number suggests</h2><p>Repository-balanced estimates keep one large codebase from deciding the result. The four requested harnesses show different one-year survival estimates and wide uncertainty.</p></div></div>
{_harness_figure(summary)}
</section>
<section class="section" id="languages">
  <div class="section-head"><div class="section-number">03</div><div><h2>Where the measured survivor mass lives</h2><p>The frozen survival cohort covers Go and Python only. That narrow language frame makes the comparison inspectable and prevents a two-language result from masquerading as all of open source.</p></div></div>
{_language_figure(summary)}
</section>
<section class="section" id="age">
  <div class="section-head"><div class="section-number">04</div><div><h2>Code age exposes when maintenance pressure arrives</h2><p>Most agent-attributed lines that reach the default branch remain through the first six months. The sharper decline appears between 180 and 365 days.</p></div></div>
{_age_figure(summary)}
</section>
<section class="section" id="write-vs-head">
  <div class="section-head"><div class="section-number">05</div><div><h2>Does a repository retain agent code at the rate it is written?</h2><p><strong class="warning">Not identified.</strong> The current artifacts measure survival of known agent-attributed lines, but they do not provide a comparable per-repository denominator for all lines written and all lines at HEAD.</p></div></div>
  <div class="method-band"><h3>The next useful index join</h3><p>For each repository and time window, join provenance-positive introduced lines to all introduced lines, then compare that writing share with the agent-attributed share of code at the frozen HEAD. Plot every repository against a 45-degree parity line and weight repositories equally.</p></div>
</section>
<section class="section" id="reverts">
  <div class="section-head"><div class="section-number">06</div><div><h2>Are agent commits overrepresented in reverts?</h2><p><strong class="warning">Not measured.</strong> A deletion is not a revert. Revert lift needs explicit revert relationships for both agent and comparison commits, aligned follow-up windows, and a repository-balanced baseline.</p></div></div>
  <div class="two-col"><div><h3>What we have</h3><p>Line histories distinguish unchanged, modified, and deleted states at fixed ages.</p></div><div><h3>What lift requires</h3><p>Agent revert rate divided by a matched non-agent revert rate, with confidence intervals and the same opportunity window.</p></div></div>
</section>"""
