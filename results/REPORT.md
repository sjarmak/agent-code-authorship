# Agent Code Authorship Replication — Frozen Result

Cutoff: 2026-07-24T23:59:59Z
Result: **Not identified**

## Finding

The preregistered classifier/mixture estimate was not computed. The gathered
reference corpus fails the locked minimum of five substantial repository groups
per language and label:

| Language | Agent groups | Human groups | Required |
|---|---:|---:|---:|
| Python | 6 | 2 | 5 per side |
| Go | 3 | 2 | 5 per side |

A substantial group was fixed in advance as at least 2,000 eligible lines.
Fitting a model after this failure would substitute within-repository sample
size for cross-repository diversity and would not support the intended
population claim. The headline estimate is therefore `null`, as required by the
protocol.

## What was successfully replicated

All 150 repositories in the original target cohort were resolved to exact
cutoff commits and Git tree hashes. The source-only, language-aware gather
produced:

| Language | Repositories with eligible code | Hunks | Surviving lines |
|---|---:|---:|---:|
| Python | 86 | 337,949 | 8,738,920 |
| Go | 22 | 128,188 | 3,291,011 |

The reference gather covered 15 pinned repositories. It enforced repository
roles, effective policy dates, source exclusions, whole-repository grouping,
and separate dedicated-validation roles. `yt-dlp/yt-dlp` was removed from the
human reference set when the frozen target manifest exposed role overlap; it
remains in the original target cohort. This correction made the group-diversity
failure more visible but did not create it.

## Classifier and quantifier design

Had the pre-model gate passed, the primary classifier would have been
ridge-regularized logistic regression over 46 frozen source-style features,
with repository-group cross-fitting. A gradient-boosted tree was implemented
only as a nonlinear diagnostic. The population share would have been estimated
from the complete score distribution using a line-weighted, 20-bin
class-conditional mixture likelihood; threshold-adjusted counts were reserved
as a robustness check.

The implementation includes repository-level end-to-end bootstrap refitting,
whole-repository synthetic mixtures with interval coverage, leave-one-target
stability, estimator-disagreement checks, and the locked AUC and interval-width
gates. None were run to manufacture a result after the upstream diversity gate
failed.

## Reproducibility

- Machine result: `results/replication.v1.json`
- Result SHA-256:
  `109546709100f05b22982c1a07356ecd2c6a71f0839839bddaf50a84ee6e97b9`
- Protocol SHA-256:
  `4ccefb87a8aa30f962c3569412116112180843a205ba4b5ad0ab30c2cc30facd`
- Target shards: `/mnt/agent-code-authorship/target-shards`
- Reference shards: `/mnt/agent-code-authorship/reference-shards`
- Verified target Git clones:
  `/mnt/agent-code-authorship/target-repositories-local-git`

Every shard has an adjacent metadata file recording its snapshot commit,
feature/corpus schema, record count, and SHA-256. The committed machine result
records all 150 target shard checksums without committing third-party source.

## Highest-leverage next step

Acquire at least three additional substantial, temporally valid, repository-local
human controls in each language, plus two additional confirmed-agent Go
repositories. Selection must remain outcome-blind and must not reuse any target
repository. If that evidence cannot be obtained, the scientifically correct
conclusion remains that the population share is not identified from the
available labels.
