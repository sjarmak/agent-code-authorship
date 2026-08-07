# Commit-message style as a code-authorship proxy

Status: exploratory. The heuristic and thresholds were frozen before scoring.
This analysis does not modify the confirmatory v3 code-feature classifier or its
estimand.

## Assumption

For this analysis, a commit detected as agent-written from the normalized
writing style and content of its commit message is treated as agent-authored
code. Every eligible source-code line introduced by that commit is attributed
to an agent. Message authorship and code authorship are not independently
verified.

This is a construct assumption authorized for the exploratory analysis, not a
claim that commit-message style proves who wrote the code.

## Method

The deterministic heuristic strips provenance trailers, agent and model names,
emails, URLs, handles, issue numbers, and revisions before extracting message
features. It then awards one point for each of seven frozen signals:

1. Body length of at least 80 words.
2. At least four nonblank body lines and two paragraphs.
3. At least three bullet or numbered-list lines.
4. At least four complete sentences with a sentence-line rate of at least 50%.
5. An explicit test, check, lint, type-check, or build result.
6. Body length strictly above the repository's pre-adoption 90th percentile.
7. Structure count strictly above the repository's pre-adoption 90th percentile.

The primary threshold is four. Thresholds three and five are frozen sensitivity
analyses. Repository-relative signals require at least 30 commits from
2023 onward before a dated adoption point. Missing baselines contribute no
outlier points and remain explicitly unavailable.

Two mechanical execution corrections were made before the final scoring pass,
after pilot outputs existed: the history walk was expanded from first-parent
commits to all reachable non-merge commits, and Git rename detection was enabled
so pure renames do not count as introduced code. Neither correction changed a
signal, weight, or threshold, and neither was selected based on its effect on a
validation metric.

The Sourcegraph index manifest supplies the frozen canonical repository
identity, `sg-evals` mirror mapping, study role, and cutoff commit. Full commit
messages and added-line counts are read from pinned Git histories because
Sourcegraph search previews are not guaranteed to contain the full message.
No SCIP or precise code intelligence is required.

## Validation result

The validation covers 15 pinned reference repositories and 26,620 labeled
commits: nine maintainer-attested agent repositories and six human-reference
repositories with repository-local AI contribution bans.
Reference labels are used only after the rule is frozen.

| Score threshold | Precision | Recall | TP | FP | TN | FN |
|---:|---:|---:|---:|---:|---:|---:|
| 3 | 87.6% | 23.2% | 3,593 | 509 | 10,617 | 11,901 |
| 4, primary | 77.9% | 3.6% | 551 | 156 | 10,970 | 14,943 |
| 5 | 73.1% | 0.1% | 19 | 7 | 11,119 | 15,475 |

The primary rule is selective, not comprehensive. It misses 96.4% of commits in
the agent-attested reference histories. Its detected commits may be treated as a
high-confidence exploratory subset under the proxy assumption, but non-detected
commits cannot be treated as human.

Errors are also repository-clustered. At the primary threshold, four of the six
human-reference repositories have zero false positives in their labeled
windows. Of 156 false positives, 142 occur in
`superseriousbusiness/gotosocial` and 14 in `qemu-project/qemu`. Long,
structured human commit styles overlap the frozen agent-style signals. The
aggregate precision therefore does not establish transportability to an unseen
repository.

## Within-repository result

Seven adopter repositories have eligible commits on both sides of a dated first
explicit agent-provenance event. All seven show a primary-threshold
detected-commit increase from zero before the event to 1.6%-6.2% afterward.
None has the required 30-message contemporary pre-adoption baseline before its
earliest reachable explicit provenance event.

The original seven repositories therefore use absolute signals only. The
Sourcegraph expansion resolves that eligibility gap for six additional
repositories. Two events are confirmed direct Devin commits; four are observed
Copilot trailer or Autofix events. Each is an ancestor of its frozen cutoff and
has an available repository-local baseline built from 1,877-27,337
contemporary pre-event messages.

| Repository | Event tier | Pre detected share | Post detected share | Change |
|---|---|---:|---:|---:|
| `getsentry/sentry` | observed trailer | 6.0% | 15.9% | +9.9 pp |
| `langflow-ai/langflow` | observed trailer | 4.6% | 33.1% | +28.5 pp |
| `marimo-team/marimo` | confirmed agent | 6.6% | 31.8% | +25.2 pp |
| `open-webui/open-webui` | observed trailer | 1.4% | 4.0% | +2.6 pp |
| `stirling-tools/stirling-pdf` | observed trailer | 6.4% | 54.6% | +48.2 pp |
| `supabase/supabase` | confirmed agent | 6.9% | 40.6% | +33.6 pp |

All six shifts are positive, but they remain descriptive workflow contrasts.
The first observed trailer is not proof that every later commit is agent code,
and the event review has not yet received the preregistered second independent
review. The pre-event rates also show why a repository-local baseline matters:
long, structured human messages can activate the same style signals.

## Attribution result

Across all 52,111 eligible commits in the deliberately enriched reference
sample, the primary rule detects 719 commits and attributes 422,432 of
5,761,158 eligible added source lines to agents, or 7.3%.

That 7.3% is not a prevalence estimate. The sample intentionally mixes
agent-attested and human-reference repositories for validation. It demonstrates
the requested commit-to-code attribution mechanism and its sensitivity to
threshold choice:

| Threshold | Detected commits | Commit share | Attributed lines | Line share |
|---:|---:|---:|---:|---:|
| 3 | 4,632 | 8.9% | 2,088,796 | 36.3% |
| 4, primary | 719 | 1.4% | 422,432 | 7.3% |
| 5 | 26 | 0.05% | 22,552 | 0.4% |

## Interpretation

Commit-message style contains a real but incomplete authorship signal. A simple
unfitted heuristic can identify a selective subset of agent-attested commits,
but it is not a substitute for explicit provenance or a trained, held-out
classifier. The low recall and repository-clustered false positives are central
findings, not defects to hide by tuning the threshold after observing labels.

The next admissible expansion is to apply the already frozen heuristic to more
`sg-evals` repositories with adjudicated adoption dates and at least 30
contemporary pre-adoption commits. Those repositories can support the intended
within-repository era adjustment. The 296 indexed mirrors do not need to be
re-indexed; expansion requires full-message extraction and event adjudication,
not SCIP.

## Expansion adjudication and execution

All 12 shortlisted repositories were already cutoff-accessible in `sg-evals`;
none required re-indexing. Outcome-blind Sourcegraph evidence review accepted
six and rejected six:

- `supabase/supabase` and `marimo-team/marimo` have confirmed direct Devin bot
  commits.
- `stirling-tools/stirling-pdf`, `open-webui/open-webui`,
  `getsentry/sentry`, and `langflow-ai/langflow` have observed Copilot
  trailer or Autofix events.
- `microsoft/lisa` and `microsoft/retina` have no agent trailer at the
  shortlisted 2024 commit; their first valid frozen hits are in 2025.
- `rustdesk/rustdesk` is a regex-polysemy error: `codext.de` matched the
  unbounded `codex` identity pattern.
- The Ghidra, Azure SDK, and Home Assistant disclosure hits describe project
  domain concepts such as generated packages, software agents, or backup
  agents—not code authorship.

The unchanged heuristic analyzed all six accepted repositories at their pinned
cutoffs: 119,650 commits and 17,277,638 added source lines. At score four it
detects 16,135 commits and attributes 4,604,064 lines, or 26.6%, in this
deliberately event-enriched expansion cohort. That percentage is not a
population prevalence estimate.

## Reproducible artifacts

- Frozen protocol:
  `study/commit-message-authorship-heuristic.v1.json`
- Protocol schema:
  `study/commit-message-authorship-heuristic.schema.json`
- Era-adjustment candidate shortlist:
  `study/commit-message-era-candidate-shortlist.v1.json`
- Outcome-blind expansion adjudication:
  `study/commit-message-era-candidate-adjudication.v1.json`
- Expansion result:
  `results/commit-message-era-expansion.v1.json`
- Machine-readable result:
  `results/commit-message-authorship-heuristic.v1.json`
- Implementation:
  `authorship/commit_message_heuristic.py`
  and `authorship/commit_message_analysis.py`
- Rendered article:
  `results/agent-code-authorship-sourcegraph.html`
