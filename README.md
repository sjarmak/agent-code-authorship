# agent-code-authorship

Estimating how much of a codebase was written by coding agents, when almost none
of it says so.

A commit trailer (`Co-Authored-By: Claude`, `copilot-swe-agent`, and friends)
can tie a change to an agent. It only sees work whose provenance was preserved,
so it provides visible positives rather than a prevalence denominator. The
current Sourcegraph-backed catalog contains 155 provenance-positive commits in
155 repositories from a frozen frame of 302 repositories, 296 of them indexed
and accessible at their pinned revisions.

**The current population share is not identified.** The contemporary control
and era-adjustment gates do not support a defensible estimate of what share of
all open-source commits or code at HEAD is agent-written. The repository keeps
that result unavailable instead of substituting an exploratory style proxy.

What the data does identify is durability. In a separate 96-repository cohort
covering 656,070 agent-attributed Go and Python lines, the
repository-balanced estimate finds **90.2% surviving at 365 days**, including
**84.9% unchanged**. Those figures describe known agent-attributed code after it
lands, not how much of open source agents wrote.

The current rendered investigation, including the Sourcegraph study and charts,
is in [`results/agent-code-authorship-sourcegraph.html`](results/agent-code-authorship-sourcegraph.html).

## Why a naive fingerprint does not work

Train a classifier to separate trailer-signed code from pre-2023 code and it
scores AUC 0.75. That looks like a fingerprint until you read the coefficients.
The heaviest weight, standardized −4.56, is trailing whitespace. Pre-2023 code has
it and modern code does not, because formatters took over. What the model learned
is when a line was written.

Run the harder test instead. Trailer-signed against unsigned code, same
repositories, same era, and it collapses to AUC 0.52. Within the modern era the
two are stylistically indistinguishable. Two very different worlds produce that
result: style says nothing about authorship, or unsigned code is largely
agent-written too. No amount of modelling separates them, because the cohort
contains no modern code that is *known* to be human.

## The control group

There is a population of projects whose contribution policy rejects
AI-generated code. Their post-2024 commits are modern code the project asserts is
human-written, which is the missing class exactly. `corpus/control.py` gathers it
from 17 such projects and will not take a project's status on trust. It greps
each repository for its own policy language and records the quote in
[`data/control_evidence.json`](data/control_evidence.json). A project whose
policy cannot be located in its own tree is dropped.

Six candidates were dropped that way. The instructive one is Telegraf, whose
policy *permits* AI-generated contributions subject to disclosure. Treating its
code as human would have quietly poisoned the control group.

Read the control group as a proxy rather than a guarantee. A policy is not
enforcement, and any agent code that slipped past one raises the measured false
positive rate, which lowers the estimate. The bias runs toward understatement.
It is still a bias.

## What the fingerprint reads

Every surviving signal concerns comments rather than code structure. Agents write
comments as sentences (longer, capitalized, ending in a period) and attach
docstrings that modern human Python has largely stopped writing, appearing in 69%
of agent hunks against 10–34% across the four Python control projects
individually. Comment *density* runs the other way and is pure era drift, having
collapsed industry-wide after 2023.

`signals.py` scores every feature on whether modern humans already moved to where
agent code sits, so a feature that merely dates code cannot be mistaken for one
that attributes it.

## Method

- **Unit**: a blame hunk, meaning a contiguous run of lines at HEAD from one
  introducing commit, minimum 6 lines. Every statistic is line-weighted.
- **Features**: 46 style measures, all rates rather than counts, in
  `features.py`. Language indicators are carried as controls and excluded from
  reporting.
- **Model**: L2 logistic regression fit by Newton-Raphson in numpy
  (`logreg.py`). Deliberately weak, because the argument rests on identification
  rather than capacity, and inspectable coefficients are how the
  trailing-whitespace problem was caught.
- **Validation**: repo-grouped 5-fold CV throughout. No repository appears in
  both training and test, and unlabeled hunks are scored by a fold model that
  never saw their repository. External check: a model trained only on
  trailer-signed code versus the control group scores an independent
  confirmed-agent corpus at 0.97 mean.
- **Quantification**: the classifier answers what share of a population is
  positive rather than which items are, via
  `p = (observed − FPR) / (TPR − FPR)`, line-weighted, per language.
- **Two human references**: the control group, and the cohort's own pre-2023
  code. The second absorbs house style but also era drift, so it overstates the
  false positive rate and understates the answer. Both are reported.
- **Placebo**: hand the estimator pre-2023 Python as if its authorship were
  unknown. The control-referenced specification returns 36% agent-written for
  code that predates the tools. That is its error floor, and why the reported
  range starts where it does.
- **Uncertainty**: bootstrap resamples repositories. Resampling hunks would give
  a fake-narrow interval, since hunks within a repository are anything but
  independent.
- **Gates**: a language must clear AUC ≥ 0.60, TPR ≥ 0.20, FPR ≤ 0.80,
  TPR − FPR ≥ 0.15, and three substantial repositories per side, or it is
  reported as not identifiable.

## What does not work

| language | status |
|---|---|
| Python | identified: AUC 0.92 against the control group, 0.96 with vouched positives |
| Rust | weak instrument: AUC 0.69, placebo error floor 50% |
| TypeScript / JavaScript | **not identifiable**: AUC 0.41. 29% of the cohort's modern lines, and the largest hole here |
| C, C++, Go, Java | too little trailer-signed code to label anything |

One high-precision tell from earlier work, the edit-elision comment an agent
leaves behind (`// ...existing code...`), fired on none of the 141,112 hunks
gathered here. Real, and far too rare to sample.

## Layout

```
authorship/
  features.py      46 stylistic measures over a block of lines
  logreg.py        logistic regression, weighted AUC, grouped folds
  data.py          corpus loading; the four populations and three label sets
  model.py         label sets, grouped CV, coefficients        -> results/model.json
  estimate.py      era-referenced estimate + drift placebo     -> results/estimate.json
  identify.py      per-language estimate vs the control group  -> results/identified.json
  signals.py       feature audit + a classifier-free cross-check
  sg.py            Sourcegraph blame access (the only network dependency)
  trailers.py      the agent trailer patterns, listed openly
  languages.py     extension map and what counts as vendored
  corpus/
    cohort.py      the repositories under study
    own.py         vouched agent-positive ground truth
    control.py     AI-banning projects, with policy evidence
  report/          the write-up renderer
data/              cohort manifest, control-group policy evidence
corpora/           feature records, gzipped, load transparently
results/           fitted coefficients, metrics, estimates
```

## Running it

```sh
pip install -r requirements.txt
```

The committed corpora are enough to reproduce every number:

```sh
python3 -m authorship.model                       # label sets, grouped CV, coefficients
python3 -m authorship.identify --boots 400        # the identified per-language estimate
python3 -m authorship.signals --lang Python       # feature audit + classifier-free check
python3 -m authorship.report.page                 # render report.html
```

Re-gathering needs a Sourcegraph instance that indexes the cohort, which is what
buys you blame over 150 repositories, plus `gh` and disk for the two local
gatherers:

```sh
export SRC_ENDPOINT=https://sourcegraph.example.com SRC_ACCESS_TOKEN=...
python3 -m authorship.corpus.cohort --files 100
python3 -m authorship.corpus.own --owner <github-user>
python3 -m authorship.corpus.control --files 250   # ~2GB of shallow clones
```

`AUTHORSHIP_CACHE` relocates clones and shards. `AUTHORSHIP_FONT_CSS` supplies
`@font-face` rules for the report; without it the page uses system faces, because
no font binaries ship here.

## Scope

Numbers here are estimates carrying a measured false positive rate. They are a
different kind of claim from a trailer scan, where every match is a true positive
and the number is a floor. Do not add them, average them, or put them on one
axis.
