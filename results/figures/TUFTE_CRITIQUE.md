# Tufte critique of report figures

## Tier 2 survival alternatives

Context: publication figures comparing repository-weighted survival across
language, agent family, and four follow-up horizons.

### Scores

- Integrity — 10/10 — one-dimensional survival is encoded by position on a
  shared linear scale; gated cells are omitted and disclosed rather than
  imputed.
- Proportionality — 10/10 — identical numeric changes occupy identical
  distances in every panel.
- Data-ink ratio — 9/10 — no grid, box, shadow, gradient, or decoration.
- Redundant ink — 9/10 — position and a thin line carry the values; labels
  identify facets directly.
- Data density — 8/10 — 35 identified estimates occupy nine compact panels.
- Integration — 9/10 — agent, language, and repository counts appear on the
  panels; no remote legend is required.
- Context — 8/10 — horizon, estimand, weighting, and gated-cell behavior are
  stated; joint uncertainty is intentionally absent because the frozen artifact
  does not contain a valid interval for the summed estimand.
- Clarity — 9/10 — shared scales make panel comparisons legitimate.
- Typography — 9/10 — restrained serif type, horizontal labels, and modest
  hierarchy.

### Chartjunk species present

None: no moiré, dreaded grid, duck, or decorative non-data ink.

### Distortion check

Lie factor: 1.00 by construction. The linear shared position scale is directly
proportional to the plotted percentage; there is no resemblance to the
dimensionality violations in VDQI's named failures.

### Genres considered

1. C5 small multiples — strongest report default because ten trajectories need
   invariant shared scales and uncluttered comparison.
2. C10 overplotted time series — useful alternate because within-language
   separation is immediately visible, but labels and lines compete.
3. C6 supertable — best source for exact values and gate reasons, retained in
   the report beside the graphic.

Chosen: C5 small multiples. Default challenge: this is the second-line VDQI
move; a single default line chart loses legibility among closely clustered
agents.

Multi-render trigger: many series over one x-axis. Both C5 small multiples and
the directly labeled overplot are delivered in
`tier2-survival-alternatives.html`.

### Overall: 9.2/10

The figures are honest, dense, directly labeled, and explicit about
non-identification.

### Fixes

1. No integrity correction required.
2. If the estimator is extended, retain each bootstrap draw of
   `unchanged + modified` and add a thin uncertainty band; do not sum marginal
   interval endpoints.

## Matched contextual effects

Context: publication figure for eight paired repository-weighted
agent-minus-`non_agent_attributed` survival differences.

### Scores

- Integrity — 10/10 — points and intervals share one linear range frame, with
  zero shown as a quiet reference.
- Proportionality — 10/10 — interval length and point position are directly
  proportional to percentage-point differences.
- Data-ink ratio — 9/10 — only intervals, points, direct labels, and one
  meaningful zero reference remain.
- Redundant ink — 10/10 — no redundant fill, border, or legend encoding.
- Data density — 8/10 — eight points and sixteen interval endpoints are
  presented compactly.
- Integration — 10/10 — each row carries language, horizon, point, and interval.
- Context — 10/10 — the title names the contrast and subtitle names the
  repository weighting and bootstrap unit.
- Clarity — 9/10 — intervals crossing zero are immediately visible.
- Typography — 9/10 — labels read left-to-right in restrained serif type.

### Chartjunk species present

None.

### Distortion check

Lie factor: 1.00 by construction. This is a C2 range-frame position graphic,
not an area or volume encoding.

### Genres considered

1. C2 range-frame interval plot — strongest fit for signed effects with
   uncertainty.
2. C6 supertable — useful for exact lookup and retained in the report.
3. C9 zero-baseline bars — rejected because bars obscure uncertainty and imply
   magnitude-from-zero rather than estimation error.

Chosen: C2 range-frame interval plot. Default challenge: the interval plot
preserves uncertainty that a default bar chart loses.

Multi-render trigger: none; the report table already supplies the exact-value
companion.

### Overall: 9.6/10

The figure foregrounds effect size and uncertainty without implying a causal
interpretation.

### Fixes

No substantive change required.
