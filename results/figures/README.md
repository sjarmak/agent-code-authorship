# Report figures

These figures are generated from the frozen final study artifacts:

- `tier2-survival-small-multiples.svg` — the report-default comparison of
  repository-weighted survival trajectories.
- `tier2-survival-overplot.svg` — the required alternate view for the
  many-series data shape.
- `tier2-survival-alternatives.html` — both survival views with guidance on
  their trade-off.
- `contextual-effects.svg` — repository-weighted matched differences with 95%
  paired-repository bootstrap intervals.

The survival estimand is repository-balanced Kaplan–Meier time to deletion.
Lineage loss is right-censored at its last observation; the risk set is reported
at every horizon. The figure currently plots point estimates for legibility,
while the frozen v2 artifact retains whole-repository bootstrap intervals.
The contextual figure includes its paired-repository bootstrap interval.

Install the rendering skills:

```bash
python3 ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo gnurio/tufte-vdqi-plugin \
  --path skills/tufte-chart skills/tufte-critique
```

Reproduce:

```bash
python3 -m authorship.build_report_figures
python3 -m authorship.build_survival_report
python3 -m authorship.validate_survival_result
```

`manifest.v1.json` pins the input and output checksums.
