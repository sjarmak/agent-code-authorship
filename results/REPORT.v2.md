# Agent-Code Authorship Study: Amendment-v2 Result

## Result

The study remains **not identified**. The headline population estimate is
`null`.

The frozen contemporary expansion was designed to require five independent
development repositories plus one untouched validation repository for each
language and label. External maintainer solicitation was declined, so the
study acquired zero admissible exact-commit human attestations. Non-solicitation
was not interpreted as evidence of either human or agent authorship.

## Agent provenance

The maintainer reconfirmed that the Gas City and Gastown projects are
agent-generated. `gastownhall/gascity` was already admitted under the pinned v1
Tier-1 entire-history attestation. The new statement about
`gastownhall/gastown` was recorded, but it was not promoted into the model
corpus because it did not specify an exact commit or explicitly attest the
entire history through the frozen cutoff. The statement was not generalized to
`gascity-packs`, `wasteland`, or `tmux-adapter`.

This conservative treatment does not determine the result: the human side has
no admissible amendment-v2 groups in either language.

## Corpus rebuild and leakage controls

The admissible pinned reference corpus was rebuilt from the verified v1 shards,
restricted to surviving code introduced from 2024-01-01 through the unchanged
2026-07-24 cutoff. Repository policies without exact commit scope were excluded
from primary human labels, as required by amendment v2.

The rebuilt reference corpus contains:

| Language | Admissible agent groups | Hunks | Surviving lines |
|---|---:|---:|---:|
| Python | 6 total (5 development, 1 validation) | 9,717 | 475,024 |
| Go | 3 total (2 development, 1 validation) | 53,294 | 1,733,488 |

Strict role separation compared exact content hashes across development,
validation, and all 150 target repositories. It removed 60 labeled records
covering 19 collision hashes while preserving every target record. The rebuilt
reference shards are stored at
`/mnt/agent-code-authorship/reference-shards-v2`.

## Locked gates

The upstream reference-diversity gate fails:

| Language | Label | Development | Validation | Required | Pass |
|---|---|---:|---:|---:|---|
| Python | Agent | 5 | 1 | 5 + 1 | Yes |
| Python | Human | 0 | 0 | 5 + 1 | No |
| Go | Agent | 2 | 1 | 5 + 1 | No |
| Go | Human | 0 | 0 | 5 + 1 | No |

Under the preregistered workflow, classifier AUC, mixture plausibility,
estimator agreement, leave-one-repository-out stability, end-to-end bootstrap
width, and held-out synthetic-mixture recovery are therefore marked `not_run`.
Running them on a one-class or historically substituted reference corpus would
not answer the estimand.

Protocol v1 also forbids a systematic synthetic-mixture bias trend but does not
define its statistic or threshold. That item is reported as `not_evaluable`;
no post-outcome rule was invented.

## Historical code

Pre-2023 code was not used as a human label or classifier negative class. The
era-confounding and historical-anchor diagnostics remain implemented, but are
marked `not_run` because there is no admissible contemporary primary model to
compare against.

## Reproducibility

- Machine result: `results/replication.v2.json`
- Machine-result SHA-256:
  `79a695de659ad92df35e1c32c6c4ae6502794b1685b0e5a0490826d130acff5f`
- Frozen response ledger: `study/attestations.v2.json`
- Execution decision: `study/execution-decision.v2.json`
- Rebuilt manifest: `study/repositories.v2.json`
- Role-separation report: `study/role-separation.v2.json`

The machine result pins the SHA-256 of the parent protocol, amendment,
candidate frame, response ledger, execution decision, both human audit batches,
the agent audit, both manifests, the role-separation report, the v1 result, and
the implementation modules used to rebuild and freeze the result.
