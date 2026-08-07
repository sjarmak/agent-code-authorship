# Sourcegraph study assets

This study uses the indexed `github.com/sg-evals/` namespace as its controlled
longitudinal discovery layer. Sourcegraph supplies cutoff-pinned repository,
commit, diff, file-search, and blame evidence. SCIP and precise code
intelligence are not used.

## Indexed population

- Canonical population: 302 repositories.
- Indexed and cutoff-accessible: 296 admissible repositories.
- Frozen exclusions: six repositories (five license/redistribution holds and
  one private-source hold).
- Indexing decision: no blanket re-index is needed. Future work is an audit and
  gap-repair operation, not a second copy of the corpus.

The frozen audit is in
`study/sourcegraph-index-audit.v3.json`. The checksummed execution inventory is
in `study/sourcegraph-effective-discovery-inventory.v3.json`.

## Discovery inventory

- Logical repository/query units: 1,776 (296 repositories × six families).
- Valid base manifests: 1,759.
- Timeout parents deterministically partitioned: 17.
- Final partition leaves: 3,311.
- Effective valid manifests: 4,634.
- Terminal timeout leaves: 10.
- Terminal Sourcegraph infrastructure-error leaves: 426.
- Inventory status: `complete_with_terminal_failures`.
- Inventory SHA-256:
  `e6624e1030db04e298717e400f57031ee46be44ec174557a3304c56a78c7aa5c`.

Terminal failures are missing evidence. They are never interpreted as a
negative search result. The final-branch executor can deliberately retry
infrastructure failures with:

```bash
python3 -m authorship.sourcegraph_discovery_partition_cli \
  --retry-terminal-execution-errors
```

The CLI reads `SRC_ACCESS_TOKEN` and `SRC_ENDPOINT` from its environment. It
does not read a shell startup file or dotenv file itself.

## Blame enrichment and evidence packets

- Cutoff/path blame groups: 24,728.
- Valid groups: 24,728.
- File-result enrichments: 25,757.
- Pending enrichments: zero.
- SCIP used: false.
- Precise code intelligence used: false.
- Final evidence packets: 105,933.
  - Adoption-event candidates: 102,181 packets across 293 repositories.
  - AI-ban-policy candidates: 3,752 packets across 157 repositories.
- Final packet-index SHA-256:
  `ab92e8b06192f655553d4e58f03c97a4dd4f7fb32e354d254eb10fe0c75006ff`.

The final index is
`results/sourcegraph-evidence-packets.v3.json`. Search line numbers are
zero-based; Sourcegraph's blame API is one-based. Both coordinate systems are
bound into every blame group so an off-by-one shard cannot be reused.

The final packet index and its intermediate discovery, blame, repository-case,
and authorship-unit shards are multi-gigabyte, rebuildable execution outputs.
They remain in the local results workspace and are excluded from ordinary Git
history. The repository tracks their schemas, frozen plans, checksums, compact
reports, final estimates, and the code needed to validate or rebuild them.

Overlapping partition searches can observe the same Sourcegraph result. Packet
deduplication preserves every source-manifest hash, verifies that the material
evidence is identical, and chooses a deterministic canonical query
provenance. A packet-ID collision with different evidence still fails closed.

## Longitudinal verification

The frozen survival frame contains 96 repositories. Sourcegraph verification
and pinned-Git lineage have deliberately separate denominators:

- Valid Sourcegraph probe manifests: 93.
- Checksummed Sourcegraph-verified longitudinal shards: 93.
- Explicit Sourcegraph absences: 3.
  - `axinc-ai/ailia-models`
  - `disler/single-file-agents`
  - `reflex-dev/reflex-web`
- Pinned-Git event histories: 96 of 96 repositories and 656,070 lines.
- Aggregate materialization SHA-256:
  `acfcb852f0ad4e6853d98c20f0e6c06737aa02f6430a3c0a1aea785f1183260a`.

The three absences are missing Sourcegraph evidence, not negative search
results and not missing survival observations. Each valid longitudinal shard
binds the canonical repository, `sg-evals` mirror, cutoff commit and tree,
probe-manifest checksum, diff result IDs, blame commit OIDs, pinned-Git file
age, evidence tier, observability, and 30/90/180/365-day lineage. Pinned Git is
authoritative when Sourcegraph and Git disagree.

The aggregate audit is
`study/sourcegraph-longitudinal-materialization.v3.json`; shards are under
`/mnt/agent-code-authorship/survival-study/sourcegraph-longitudinal-v3/materialized/shards/`.
The offline, resumable reproduction command is:

```bash
python3 -m authorship.sourcegraph_longitudinal_materialization_cli
```

The command performs no Sourcegraph calls and requires neither a token nor
SCIP. It reuses only schema-valid, identity-bound shards.

## Era-adjusted study role

The primary modernization adjustment is a within-repository
difference-in-differences design:

- A repository's own contemporary pre-adoption code is its human baseline.
- Primary event windows are ±180 days; extended windows are ±365 days.
- Not-yet-adopting repositories provide calendar-time controls.
- Admissible AI-ban repositories provide H2 never-adopting controls.
- Matching includes language, path type, change size, code age, and calendar
  time.
- Pre-2023 code remains a historical human proxy and sensitivity anchor, not
  the sole contemporary human control.
- All post-adoption code is not assumed to be agent-authored. Direct authorship
  requires explicit hunk-level provenance; whole-repository estimates measure
  workflow, review, and formatting spillovers.

High-precision Sourcegraph discovery families currently produce candidate
adoption events in 248 repositories; 62 repositories have earliest observed
candidates in 2023–2024. AI-ban candidates span 157 repositories, including 42
with earliest observed evidence in 2023–2024. These are candidate frames, not
labels.

Two independent, outcome-blind and peer-blind primary reviews are required per
candidate under `study/sourcegraph-discovery.v3.json`. Disagreements require a
distinct resolution review. No adoption date, AI-ban control assignment, or
era-adjusted estimate is admissible before that freeze.

The checked-in era materialization predates the repair that adds the required
adoption-minus-three pretrend quarter. It is retained only as a failed-gate
artifact: all 46 features in both Go and Python remain `not_identified`, and it
must be rematerialized with that additional quarter before any era-adjusted
estimate can be reported.

### Exploratory message-proxy expansion

A separately labeled, single-reviewer exploratory pass adjudicated 12
late-2024 Sourcegraph candidates without consulting classifier or survival
outcomes. It accepted two confirmed direct Devin commits and four observed
Copilot trailer/Autofix events, and rejected six search-polysemy hits. The
rejections include `codext.de` matching an unbounded `codex` identity pattern
and three generated-code queries that described project-domain concepts rather
than authorship.

All six accepted events are ancestors of their `sg-evals` cutoff and have
1,877-27,337 contemporary pre-event messages. The unchanged message heuristic
was rerun over 119,650 commits and 17,277,638 added source lines. These
single-reviewer events support exploratory within-repository contrasts only;
they do not satisfy the independent-review gate above.

- Adjudication:
  `study/commit-message-era-candidate-adjudication.v1.json`
- Full expansion result:
  `results/commit-message-era-expansion.v1.json`
- Re-indexing required: false.
- SCIP required: false.

## Survival correction

The apparent Cursor/Python increase at 365 days in the earlier estimator was a
changing-denominator composition artifact: only lines still observable at
each horizon remained in the conditional estimate. At 365 days, repositories
and lines with shorter follow-up had dropped out, so the estimate could rise
even though individual code cannot become "more surviving."

The repository-balanced Kaplan–Meier estimates are monotone:

| Horizon | Survival |
| ---: | ---: |
| 30 days | 0.9699383472 |
| 90 days | 0.9640078705 |
| 180 days | 0.9485582809 |
| 365 days | 0.9019705250 |

The estimate artifact is `results/survival-estimates.v2.json`. It is bound to
the rebuilt 96-repository, 656,070-line event inventory. The Cursor/Go crossed
stratum contains four repositories and is therefore `not_identified` under the
preregistered five-repository minimum; the 12-repository Cursor harness
stratum used in the article remains identified.
