# Agent-authored code survival study

Status: **final**.

The blinded structural-lineage precision gate passed; structural candidates are mapped to `modified`.

## Scope and provenance

- Frozen cutoff: 2026-07-24T23:59:59Z.
- Languages: Python and Go.
- Time zero: first default-branch commit containing the change.
- Sourcegraph role: discovery and snapshot audit. The connected instance indexed 0 frozen candidates, so checksummed pinned Git histories are authoritative for lineage.
- Physical-line deduplication excluded 228 duplicate attributions from Mochi PR overlaps; 656,070 unique eligible lines remain.

## Tier 1 — explicit generation provenance

**not_identified**: zero repositories remained eligible after the preregistered role-overlap exclusion. Tier 1 is not pooled with Tier 2.

## Tier 2 — AIDev agent-attributed pull requests

Primary estimates weight repositories equally. Secondary estimates pool lines. Cells failing either five-repository or 80% lineage coverage gates are reported as `not_identified`.

| Language | Agent | Repositories | 30d | 90d | 180d | 365d |
|---|---:|---:|---:|---:|---:|---:|
| Go | Claude_Code | 5 | 99.5% | 99.5% | 98.4% | not_identified |
| Go | Copilot | 15 | 99.0% | 97.1% | 96.8% | 90.5% |
| Go | Cursor | 4 | not_identified | not_identified | not_identified | not_identified |
| Go | Devin | 5 | 100.0% | 100.0% | 99.0% | 98.8% |
| Go | OpenAI_Codex | 8 | 99.4% | 99.4% | 99.4% | 98.3% |
| Python | Claude_Code | 8 | 95.5% | 93.2% | 90.3% | 83.3% |
| Python | Copilot | 14 | 98.1% | 97.5% | 97.2% | 95.0% |
| Python | Cursor | 8 | 82.7% | 81.4% | 73.6% | 77.3% |
| Python | Devin | 13 | 97.7% | 97.7% | 91.7% | 89.5% |
| Python | OpenAI_Codex | 16 | 96.5% | 95.5% | 95.5% | 92.4% |

Values are repository-weighted headline survival among observable unchanged/modified/deleted lines, with survival defined as `unchanged + modified`.

## Matched contemporaneous context

The secondary comparison is labeled `non_agent_attributed`, uses the same repositories, language/file type, ±90-day code-age caliper, and fourfold change-size caliper, without replacement. It is descriptive and has no causal interpretation.

| Language | Paired repositories | Horizon | Status | Repository-weighted agent-minus-context difference (95% CI) |
|---|---:|---:|---|---:|
| Go | 30 | 30d | identified | -0.5 pp (-1.5, -0.0) |
| Go | 30 | 90d | identified | -1.4 pp (-3.7, -0.0) |
| Go | 30 | 180d | identified | -0.5 pp (-3.6, 2.5) |
| Go | 30 | 365d | identified | 2.4 pp (-2.4, 7.6) |
| Python | 47 | 30d | identified | 1.5 pp (-4.8, 8.7) |
| Python | 47 | 90d | identified | 0.5 pp (-6.2, 7.7) |
| Python | 47 | 180d | identified | 1.6 pp (-6.2, 9.9) |
| Python | 47 | 365d | identified | 1.9 pp (-8.0, 12.4) |

## Identification and limitations

- Tier 1 is `not_identified` because no role-separated eligible repository remained.
- Tier 2 cells below five repositories or 80% lineage coverage are `not_identified`; descriptive point values are retained only in the machine artifact.
- Validated structural matches enter headline results as `modified`.
- Mochi dominates line-weighted totals, so repository weighting is the primary estimand.
- The contextual comparison is secondary and does not identify causal authorship effects.

## Reproducibility

Raw AIDev inputs, Git bundles, transition shards, contextual cohorts, and private blind mappings are checksummed on NAS. Repository artifacts contain the frozen protocol, candidate manifest, public blind-review file, estimates, and this report.
