# Sourcegraph v3 sequential adoption review

This review dates repository adoption without consulting classifier scores,
code-survival outcomes, or final study estimates. It uses local or subagent
review only. Do not use an OpenAI API key, the paid Batch API, or the superseded
72,960-task packet-level workflow.

## Frozen inputs

- Workflow: `study/sourcegraph-adjudication-workflow.v3.json`
- Adoption review protocol:
  `study/sourcegraph-adoption-review-protocol.v3.json`
- Adoption protocol canonical SHA-256:
  `b34685b4ff4aad32b68dec77d2015a96de59e5787f6109fb136e64b914a9d225`
- Repository case index:
  `study/sourcegraph-repository-case-index.v3.json`
- Case-index canonical SHA-256:
  `8729186a45ca2b46cbe163b1069c3a6998e4cf5abcdcc63b3075c07592d1bf15`
- Raw packet index:
  `results/sourcegraph-evidence-packets.v3.json`
- Packet-index canonical SHA-256:
  `ab92e8b06192f655553d4e58f03c97a4dd4f7fb32e354d254eb10fe0c75006ff`
- Tranche schema:
  `study/sourcegraph-adoption-review-tranche.schema.json`
- Response schema:
  `study/sourcegraph-adoption-review-response.schema.json`
- Decision-ledger schema:
  `study/sourcegraph-adoption-decision-ledger.schema.json`
- Independent append-chain registry:
  `study/sourcegraph-adoption-review-chain.v3.json`
- Chain-registry schema:
  `study/sourcegraph-adoption-review-chain.schema.json`

The first tranche is
`results/sourcegraph-adoption-review-v3/tranche-001.json`. It contains one
earliest unresolved event for each of 248 repositories with an explicit
provenance anchor. Of those events, 197 are earlier challenge candidates and
51 are immediate explicit-provenance anchors. The tranche references 373 raw
Sourcegraph evidence packets.

## Decision rule

Review only the evidence in the assigned task. The question is whether that
event establishes credible repository adoption of an AI coding agent at the
recorded date.

- `accept_confirmed`: explicit repository-linked provenance establishes actual
  coding-agent use. Set `evidence_tier` to `confirmed`.
- `accept_observed`: repository-linked announcement, configuration, or policy
  evidence credibly establishes adoption, but no explicit authored-code event
  is shown. Set `evidence_tier` to `observed`.
- `reject`: the hit is unrelated, generic use of words such as “generated,”
  describes non-AI generation, or otherwise does not support adoption.
- `insufficient`: the evidence may concern AI coding agents but cannot establish
  repository adoption or a defensible date.
- `ambiguous`: a real semantic uncertainty requires a peer-blind second review.
  Do not use this merely because evidence is sparse; use `insufficient` then.

Accepted events require `default_branch_supported: true`. Here that means the
event and its evidence are bound to Sourcegraph history scoped to the frozen
cutoff revision. Cite at least one `source_url` present in the task. Write a
specific rationale; do not infer adoption from commit-message style or from
post-2023 timing.

## Sequential stopping

Each repository has an ordered sequence consisting of every challenge event
strictly earlier than its first explicit-provenance anchor, followed by that
anchor. A challenge with the exact anchor timestamp is ordered after the anchor;
an opaque event ID never determines prehistory.

1. Review the earliest unresolved event.
2. Acceptance closes the repository at that date.
3. Rejection or insufficient evidence advances to the next event.
4. Ambiguity assigns the same event to a different secondary reviewer.
5. After a secondary review, a third distinct resolver makes the final decision.
6. If the sequence is exhausted, retain an explicit no-credible-event result.

The next tranche is generated only after the current tranche is compiled. This
is why 5,307 possible candidate events are not all mandatory reviews.

After tranche 7, the frozen
`study/sourcegraph-adoption-anchor-amendment.v3.json` bounds the remaining
review burden. Repositories still active with no open peer review move directly
to semantic review of their first explicit-provenance anchor. This does not
assert that earlier use was absent: the separate anchor manifest retains the
earliest unresolved position, unresolved earlier-candidate count, and
`clean_prehistory` flag for every task. An accepted anchor is interpreted as
first observed explicit use in the frozen frame, not earliest actual adoption.
The clean-prehistory restriction remains a required sensitivity analysis.
Anchor materialization requires the independently frozen amendment SHA-256
`d4f2daef1b9e15bcdf5bec1ffbe7450be2adbcfbb106d537c9ab990a296a1cc1`.
The amendment schema and runtime validator both fix the complete nested policy;
a self-rechecksummed rewrite is rejected.

## Blinding and identity

Every response must preserve the task identity and checksum, record a stable
`reviewer_id`, bind the source `tranche_id` and `tranche_number`, and set
`outcomes_consulted: false`. Primary, secondary, and resolver identities for
the same repository-event must be distinct. A later event or peer-review role
must come from a strictly later tranche. Reviewers must not inspect prior
decisions, classifier outputs, survival results, or other reviewers’
rationales.

The compiler validates exact task coverage, response checksums, evidence
citations, evidence-tier consistency, accepted-event default-branch support,
task identity, bundle reviewer/range claims, independently rematerialized
tranche evidence, and the frozen predecessor-ledger checksum before a response
may advance the state machine.

Obtain each `--expected-tranche-sha256` and
`--expected-decision-ledger-sha256` value from the independently frozen
append-chain registry, never from the artifact being validated. Append the new
ledger hash and next tranche hash to that registry only after both artifacts
validate and reproduce.

Compile a completed tranche with its already-frozen SHA:

```bash
python3 -m authorship.sourcegraph_adoption_review_compile_cli \
  --tranche results/sourcegraph-adoption-review-v3/tranche-001.json \
  --response-bundle /path/to/primary-a.json \
  --response-bundle /path/to/primary-b.json \
  --expected-tranche-sha256 <frozen-tranche-sha256> \
  --output results/sourcegraph-adoption-review-v3/decision-ledger-001.json
```

For tranche 2 and later, also pass the predecessor ledger and the checksum
frozen before materialization:

```bash
python3 -m authorship.sourcegraph_adoption_review_cli \
  --decisions results/sourcegraph-adoption-review-v3/decision-ledger-001.json \
  --expected-decision-ledger-sha256 <frozen-ledger-sha256> \
  --tranche-number 2 \
  --output results/sourcegraph-adoption-review-v3/tranche-002.json
```

## Reliability audit

The repository-language inventory is bound to the independently frozen
Sourcegraph index-manifest file SHA-256
`eb39ec53d2cc73791f7ebef75a4226fdf67be0272017fed2d05ebbbe292b33b5`.
It uses `Repository.language` for the 248 `github.com/sg-evals/*` mirrors and
does not require SCIP. The frozen inventory SHA-256 is
`773d41498700dedebe9aee9a92046b7646c4136d992f1a5138d8ba4758117635`.

The combined audit adapter binds all 1,119 sequential decisions and 105 anchor
decisions into one 1,224-decision frame. Its SHA-256 is
`f182d6f88124aa7d50e796365224dc683810bc8e5e8574712ba8e8c8542f3547`.
The deterministic seed is
`sourcegraph-adoption-reliability-v3-2026-07-29`.

The 142-case worksheet is stratified jointly by decision, evidence channel,
and Sourcegraph language. It selects a repository at most once per stratum
until unique repositories are exhausted and records every necessary repeat.
The blind worksheet omits primary decisions and primary rationales; its key is
stored separately.

- Worksheet SHA-256:
  `368d528ddd9b66bec4b513a1275e657c86d6de178bf2e51e6174a106483397ca`
- Key SHA-256:
  `5ef81cd9645b4560819e6f094a1f737bd3596e34280d97e3ec4d500e2b2d8a6e`
- Compiled audit SHA-256:
  `51a37d9219a666c94edc36939dc24a6f182bebb64e10c5c89f625c1e2379821c`
- Exact agreement: 128/142 (90.14%)
- Unresolved: 0/142

Materialize with
`python3 -m authorship.sourcegraph_adoption_audit_cli --help` and compile with
`python3 -m authorship.sourcegraph_adoption_audit_compile_cli --help`. Both
commands require independently supplied predecessor hashes and rematerialize
the audit artifacts before accepting reviewer responses.

The repository-level catalog is
`study/sourcegraph-adoption-catalog.v3.json`, SHA-256
`46b359397c591f61d49bb93e007d5df04bbd4ba7559731bde605ff2ab40fe9da`.
It contains 248 repositories: 102 dated sequentially, 66 dated at an explicit
anchor, and 80 with no credible event. The primary sg-evals-only cohort has 247
repositories; `react/react` is excluded because its frozen review evidence came
from the direct index. The separate scope-remediation issue must be resolved
before that repository can enter the primary cohort.
