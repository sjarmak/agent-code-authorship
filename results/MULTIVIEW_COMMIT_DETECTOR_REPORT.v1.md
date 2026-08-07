# Multi-view agent-driven commit detector

Status: exploratory. Protocol version 1 was frozen before multi-view scoring.

## Result

Timing and process evidence contains a modest cross-repository ranking signal,
but the frozen detector is not accurate enough to turn its scores into
authorship labels.

The analysis covers all 15 pinned reference repositories and 26,620 labeled
commits. Nine repository windows are agent references supported by Tier-1
maintainer attestations. Six are policy-human controls backed by dated,
repository-local AI contribution bans.

| Evidence view | Precision at 0.5 | Recall at 0.5 | Average precision | ROC AUC | Brier score |
|---|---:|---:|---:|---:|---:|
| Message | 52.6% | 53.3% | 62.5% | 49.8% | 0.285 |
| Timing/process | 67.5% | 81.7% | 63.3% | 64.2% | 0.265 |
| Combined | 64.2% | 82.8% | 58.7% | 57.9% | 0.297 |

Timing/process improves ROC AUC by 14.4 percentage points over the fitted
message view. Adding message features to timing/process reduces ROC AUC by 6.3
points. These are leave-one-repository-out results; each repository is scored
only by a model trained on the other 14.

The message view here is a balanced logistic-regression model over the frozen
message features. It is not the separate unfitted seven-signal heuristic, whose
primary score-four operating point remains the selective commit-to-code proxy.

## AI-ban control errors

At the frozen probability threshold of 0.5, timing/process incorrectly flags
6,090 of 11,126 policy-human commits, or 54.7%. The combined model incorrectly
flags 7,155, or 64.3%. Error rates vary sharply by held-out repository.

| Policy-human repository | Labeled commits | Message FP rate | Timing/process FP rate | Combined FP rate |
|---|---:|---:|---:|---:|
| `asahilinux/m1n1` | 6 | 100.0% | 0.0% | 0.0% |
| `borgmatic-collective/borgmatic` | 175 | 99.4% | 88.6% | 88.6% |
| `poezio/poezio` | 200 | 76.5% | 46.5% | 44.5% |
| `qemu-project/qemu` | 8,999 | 62.9% | 50.2% | 62.3% |
| `superseriousbusiness/gotosocial` | 1,390 | 80.1% | 90.5% | 89.1% |
| `twpayne/chezmoi` | 356 | 98.6% | 18.3% | 18.5% |

This dispersion is the central finding. Commit cadence and diff shape capture
repository workflow as well as possible agent use. Holding out repositories
prevents direct fitting to the test repository, but it cannot eliminate owner,
era, project-type, or reference-construction confounding.

## Timing/process evidence

The deterministic Git extractor records:

- author and committer timestamps and their absolute delta;
- previous repository and same-author gaps;
- same-author commits in the prior 15 minutes;
- author-local hour, weekday, and UTC offset;
- parent count; and
- rename-aware files changed, lines added and deleted, binary files, and
  rename entries.

Raw names and email addresses are never published. Normalized author and
committer identities are hashed with a repository-scoped SHA-256 digest and are
retained only for mechanical within-repository joins. Identity hashes,
repository IDs, commit IDs, labels, and explicit provenance are all excluded
from model inputs.

Git clocks are process metadata, not an authorship oracle. Rebases, squashes,
imports, patches, and deliberate date editing can alter them.

## Outcome policy

The frozen taxonomy distinguishes:

1. verified agent;
2. likely agent-driven;
3. hybrid or assisted; and
4. unknown or human control.

This execution assigns 15,495 commits to `verified_agent` because they have
direct commit provenance or fall inside a Tier-1 maintainer-attested agent
reference window. It leaves 36,616 as `unknown_or_human_control`.

It assigns zero commits to `likely_agent_driven` and zero to
`hybrid_or_assisted`. The held-repository false-positive burden does not support
promoting model scores into either category. Hybrid labels also require
evidence that the current Git-only layer does not contain.

## Era adjustment

The preregistered 1 January 2025 era split is explicitly unavailable. It yields
688 training commits and 25,932 test commits, but both sides do not contain both
reference classes. The cutoff was not moved after observing the labels.

This does not replace the study's within-repository adoption design. The
preferred era adjustment still requires repositories with a datable first
agent PR, trailer, or default-branch commit and sufficient contemporary
2023-2024 pre-adoption history. Each adopter repository then supplies its own
human comparison, with not-yet adopters and separately labeled AI-ban
repositories supplying calendar-time controls.

## Commit-to-code assumption

For the authorized exploratory line of investigation, a commit independently
detected as agent-driven is treated as the author of all source-code lines that
commit adds. This is a disclosed attribution assumption. It does not establish
that the message writer, committer, and code author are always the same actor.

Because the multi-view model fails the false-positive diagnostic, its
probability threshold is not used for that attribution. The separately frozen
message heuristic remains the current selective detector.

## Sourcegraph role

The analysis retains the frozen Sourcegraph index-manifest reference and pinned
Git cutoff for every repository. Sourcegraph supplies the canonical-to-
`sg-evals` mapping, indexed repository frame, revision-level discovery,
commit/diff search, blame, structural search, and auditable evidence packets.
Pinned local Git histories supply complete clock and numstat fields.

All 15 references were already available in the indexed `sg-evals` frame.
Neither re-indexing nor SCIP/precise code intelligence is required.

## Reproducible artifacts

- Frozen protocol: `study/multiview-commit-detector.v1.json`
- Protocol schema: `study/multiview-commit-detector.schema.json`
- Machine-readable evidence and results:
  `results/multiview-commit-detector.v1.json`
- Timing extractor: `authorship/commit_timing_features.py`
- Leakage-safe evaluation: `authorship/multiview_evaluation.py`
- Execution layer: `authorship/multiview_commit_analysis.py`
- Rendered article: `results/agent-code-authorship-sourcegraph.html`

Protocol SHA-256:
`4bdd1e74aa118ceadce493012277d19b92396378a11eccd638198ffde7ad64e4`.

Result SHA-256 at report generation:
`bd6fe3041eb43124c5e2c0a7827fdce777639add69c7aeb1d8777cc7b03ca827`.
