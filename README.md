# Agent code authorship

An open research project measuring what repository history can—and cannot—tell
us about code written with coding agents.

The study combines public agent-provenance signals with Sourcegraph's indexed
Git history. It follows known agent-attributed lines after they land in
established open-source repositories, while keeping unsupported prevalence and
quality claims explicitly unavailable.

## Read the investigation

The self-contained article and its script-free figures are available at
[`results/agent-code-authorship-sourcegraph.html`](results/agent-code-authorship-sourcegraph.html).
It can be downloaded and opened directly in any modern browser.

## Current result

**The current population share is not identified.** Public provenance signals
find known positives, but they do not supply the contemporary human denominator
needed to estimate how much of all open-source code is agent-written.

Durability is identifiable in a separate frozen cohort:

- 656,070 agent-attributed Go and Python lines across 96 repositories
- 90.2% estimated to survive at 365 days
- 84.9% estimated to remain unchanged at 365 days
- separate estimates for Claude Code, Codex, GitHub Copilot, and Cursor

These numbers describe known agent-attributed code after it lands. They are not
an estimate of agent-written code's share of open source, and deletion is not a
revert or a quality judgment.

## Why prevalence remains unavailable

Commit trailers and other preserved provenance establish high-confidence agent
examples, but most assisted work is unlabeled. Treating unlabeled modern code as
human would build the answer into the control group.

The repository includes experiments with stylistic classifiers and
era-adjusted controls. Their identification gates reject a population estimate:
a model can distinguish old from new code without learning authorship, and the
available modern controls do not resolve that ambiguity. The published article
reports the boundary instead of substituting a style proxy.

## Repository map

| Path | Contents |
|---|---|
| `authorship/` | Collection, validation, estimation, and rendering code |
| `study/` | Frozen protocols, schemas, manifests, and study inputs |
| `results/` | Compact result artifacts, figures, and the rendered article |
| `corpora/` | Compressed feature records used by the earlier classifier work |
| `data/` | Cohort definitions and control-policy evidence |
| `tests/` | Unit, integration, artifact-contract, and browser tests |

Large raw Sourcegraph responses, local clones, caches, and agent-workspace
metadata are intentionally excluded. The checked-in manifests and checksums
document the inputs used by the published artifacts.

## Reproduce the published article

Python 3.12 is the reference runtime.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m authorship.build_v3_blog_post
```

The renderer uses the frozen JSON artifacts in `study/` and `results/`. If a
neighboring Sourcegraph checkout contains the blog fonts, they are embedded;
otherwise the output remains self-contained and uses system font fallbacks.

Rebuild the standalone study figures with:

```sh
python3 -m authorship.build_report_figures
```

## Validate

Install the development dependencies and run the Python suite:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 -m ruff check authorship tests
```

The browser checks cover responsive layout, navigation, runtime errors, and
WCAG A/AA violations:

```sh
npm install
npx playwright install chromium
npm run test:e2e
```

## Method at a glance

- Agent attribution requires explicit, auditable provenance; ambiguous cases
  remain unlabeled.
- Survival follows lines from their introducing commit through later repository
  states using indexed blame and Git history.
- Estimates are repository-balanced so a handful of very large repositories do
  not define the answer.
- Bootstrap intervals resample repositories, preserving codebase-level
  clustering.
- Underpowered or mixed-evidence strata are reported as not identified.
- Cohorts, revisions, schemas, and analysis gates are frozen before reporting.

For artifact-level detail, see
[`results/SOURCEGRAPH_STUDY_ASSETS.md`](results/SOURCEGRAPH_STUDY_ASSETS.md) and
the protocols under [`study/`](study/).

## Scope and interpretation

The repository studies established public repositories and known
agent-attributed code. It does not measure private code, unlabeled assistance,
developer productivity, defect rates, or the causal effect of any coding agent.

Prevalence, survival, reverts, and quality are different estimands. Do not add
them, average them, or treat one as a proxy for another.

## License

Released under the [MIT License](LICENSE).
