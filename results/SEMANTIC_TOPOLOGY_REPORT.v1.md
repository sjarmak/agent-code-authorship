# Sourcegraph semantic change-topology pilot

## Result

Pilot result: **validated_utility**.

Deep Search hypotheses are not statistical labels. All reported analytic fields below enter through pinned Git, deterministic Sourcegraph evidence, or frozen blinded adjudication.

## Frozen execution

- Repositories: 20
- Planned investigations: 100
- Materialized investigations: 100 / 100
- Terminal statuses: `{'completed': 96, 'error': 4}`
- Conversation identities preserved: 100
- Raw answers preserved: 97
- Search-trace statuses: `{'unavailable_not_exposed_by_api': 100}`
- Model-metadata statuses: `{'unavailable': 100}`
- Cited files extracted: 1743
- Proposed deterministic queries extracted: 15
- Semantic hunk records: 492
- Record materialization failures: 0
- Paths by class: `{'documentation': 25, 'source': 418, 'test': 49}`
- Records with subsequent same-path changes: 455
- Records whose first follow-up uses a different commit identity: 371
- Distinct follow-up-author count distribution: `{'0': 37, '1': 322, '2': 19, '3': 2, '4': 8, '5': 26, '6': 27, '9': 20, '10': 1, '11': 28, '22': 1, '24': 1}`

## Evidence boundary

- Deep Search candidates: 1676
- Pinned-Git verified citations: 456
- Unavailable citations: 1205
- Query candidates awaiting separate execution: 15
- Repository-held-out control candidates: 16
- Control-source statuses: `{'candidate_generated': 4, 'unavailable': 16}`
- Field `blast_radius`: unavailable=492
- Field `diffusion`: unavailable=492
- Field `human_assimilation`: observed=492
- Field `observability`: observed=492
- Field `ownership`: unavailable=492
- Field `semantic_hard_negatives`: unavailable=492
- Field `subsequent_changes`: observed=492
- Field `task_taxonomy`: unavailable=492
- Field `tests_and_docs`: observed=492
- Field `uncertainty`: observed=492
- Terminal investigation failures: 4
- Failure: `gastownhall/gascity` / `semantic_controls` / `ERROR_TOKEN_LIMIT_EXCEEDED`
- Failure: `ant-design/ant-design` / `change_topology` / `ERROR_TOKEN_LIMIT_EXCEEDED`
- Failure: `qemu-project/qemu` / `delegated_task_intent` / `ERROR_INTERNAL`
- Failure: `superseriousbusiness/gotosocial` / `semantic_controls` / `ERROR_TOKEN_LIMIT_EXCEEDED`

Unavailable SCIP, ownership, history, or citation evidence remains typed missingness; it is never silently imputed.

## Preregistered utility gates

| Gate | Result |
|---|---|
| New discovery family | NOT IDENTIFIED |
| Task-matched controls | NOT IDENTIFIED |
| Reliable topology fields | PASS |

Reliable fields: `distinct_followup_author_count`, `first_followup_author_differs`, `subsequent_change_present`

## Limitations

- Deep Search may omit its internal search trace and model metadata; those API omissions are preserved explicitly.
- Different commit identities are not interpreted as human authorship.
- Candidate semantic controls remain unlabeled until deterministic eligibility checks and blinded review succeed.
- Reliability reviewers were independent blinded agents, not humans; their agreement is reported as reproducibility evidence over pinned Git.
- Distinct author counts describe normalized Git name/email pairs, not people; mailmap or identity-resolution claims are intentionally excluded.
- A passed topology-field gate establishes reliable enrichment fields, not a general agent-code detector.

## Artifact hashes

| Artifact | SHA-256 |
|---|---|
| Protocol | `1da1f2e260917f1f48efe3cfb8101e315f24608bbd640f56c684e624d1af1cf1` |
| Pilot plan | `0a0285d0d371aee1b8e3bad94b0936c7b288acd10045fe4b9906cfa54794699d` |
| Deep Search inventory | `c464a88b1cf2f0347ec709819dfe4c8d87b0399530119115d3441d79c9ef6e2e` |
| Semantic record inventory | `3926e2148821ca0d5868c06ca25918c0204bde58a08bcce86a4e30a80d39f692` |
| Candidate inventory | `b4b267db15d87c82cdc2e758e9889f72ff87d346ff665ee9135b4619355063f3` |
| Candidate verification | `691a7ab770a4105fe086b04dce34f3acf9c8a8bcae150d1eea0c35bb7e840c0a` |
| Held-out control candidates | `851e5a4bca390f9f3d1c59ab0d772c51d1b791e82287a7973bd7d7783a431c0d` |
| Utility gates | `5614a0ca5d567aac38ebcf56e769204ea5e8662c67bcc19a2239a53d4bf9a8af` |

## Exact reproduction

```bash
PYTHONPATH=. python3 -m authorship.semantic_topology_protocol
export SRC_ENDPOINT=https://demo.sourcegraph.com
export SRC_ACCESS_TOKEN='<Sourcegraph access token>'
for shard in 0 1 2 3; do
  PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search \
    --shard-index "$shard" --shard-count 4 &
done
wait
PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search --retry-error-code transport_error
PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search --limit 0
PYTHONPATH=. python3 -m authorship.semantic_topology_deep_search --validate-only
PYTHONPATH=. python3 -m authorship.semantic_change_materialization
PYTHONPATH=. python3 -m authorship.semantic_topology_finalize
PYTHONPATH=. python3 -m authorship.semantic_topology_audit
PYTHONPATH=. pytest -q tests/test_semantic_topology_*.py tests/test_semantic_change_materialization.py
python3 -m ruff check authorship/semantic_topology_*.py authorship/semantic_change_materialization.py
```
