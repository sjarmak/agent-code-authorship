#!/usr/bin/env python3
"""Page copy and HTML assembly for the report. Split from page.py so the chart
code and the writing stay separately readable."""
from __future__ import annotations

CSS = """
:root{
  --bkg:#000; --cream:#F5EDDD; --body:#C8C8C8; --muted:#8A8A8A;
  --line:#2a2a2a; --vm:#FF5543; --panel:#0a0a0a;
}
/* This page commits to one visual world — the Sourcegraph GTM ground is black
   by design, so the light theme is pinned rather than inverted. */
:root[data-theme="light"], :root[data-theme="dark"]{ color-scheme: dark; }
*{box-sizing:border-box}
body{
  margin:0; background:var(--bkg); color:var(--body);
  font-family:'PolyMono',var(--body-font,ui-monospace),SFMono-Regular,Menlo,monospace;
  font-size:15px; line-height:1.65; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:860px; margin:0 auto; padding:72px 28px 120px}
h1,h2,h3,.display{font-family:'PolySans',var(--display-font,system-ui),sans-serif; color:var(--cream);
  font-weight:500; text-wrap:balance; letter-spacing:-0.01em}
h1{font-size:clamp(34px,6vw,54px); line-height:1.04; margin:0 0 22px}
h2{font-size:23px; margin:0 0 14px; line-height:1.2}
h3{font-size:15px; margin:0 0 8px; letter-spacing:0.01em}
.eyebrow{font-size:11.5px; letter-spacing:0.14em; text-transform:uppercase;
  color:var(--vm); margin:0 0 26px}
.deck{font-size:16.5px; color:var(--body); max-width:66ch; margin:0 0 8px}
p{max-width:68ch}
section{margin-top:64px}
.rule{height:1px; background:var(--line); border:0; margin:0}
.panel{border:1px solid var(--line); padding:22px 24px; margin:26px 0}
.panel.warn{border-color:#5a2a24}
.panel.warn h3{color:var(--vm)}
.kicker{color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:0.1em; margin:0 0 18px}
.stats{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:1px; background:var(--line); border:1px solid var(--line); margin:28px 0}
.stat{background:var(--bkg); padding:18px 20px}
.stat .n{font-family:'PolySans',var(--display-font,sans-serif); font-size:27px; color:var(--cream);
  line-height:1.1; font-variant-numeric:tabular-nums}
.stat .n.vm{color:var(--vm)}
.stat .l{font-size:11.5px; color:var(--muted); margin-top:6px; line-height:1.45}
figure{margin:30px 0 8px}
figcaption{font-size:12.5px; color:var(--muted); margin-top:12px; max-width:70ch}
.legend{display:flex; flex-wrap:wrap; gap:16px; margin:14px 0 0; font-size:11.5px;
  color:var(--muted)}
.key{display:inline-flex; align-items:center; gap:7px}
.key i{width:9px; height:9px; display:inline-block}
.scroll{overflow-x:auto}
table{border-collapse:collapse; width:100%; font-size:12.5px; min-width:520px}
th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--muted); font-weight:400; text-transform:uppercase;
  letter-spacing:0.07em; font-size:10.5px}
td.num{text-align:right; font-variant-numeric:tabular-nums; color:var(--cream)}
td.q{color:var(--muted); font-size:11.5px; line-height:1.5}
.tag{font-size:10.5px; letter-spacing:0.06em; text-transform:uppercase;
  border:1px solid var(--line); padding:2px 7px; color:var(--muted); white-space:nowrap}
.tag.out{border-color:#5a2a24; color:var(--vm)}
code,.mono{font-family:'PolyMono',var(--body-font,ui-monospace),monospace}
code{color:var(--cream); font-size:12.5px}
pre{background:var(--panel); border:1px solid var(--line); padding:16px 18px;
  overflow-x:auto; font-size:12px; color:var(--body); line-height:1.7}
strong{color:var(--cream); font-weight:400}
.vm{color:var(--vm)}
ul{max-width:68ch; padding-left:19px}
li{margin:8px 0}
footer{margin-top:76px; padding-top:24px; border-top:1px solid var(--line);
  color:var(--muted); font-size:11.5px}
a{color:var(--cream); text-decoration:underline; text-decoration-color:var(--line)}
a:focus-visible{outline:2px solid var(--vm); outline-offset:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


def stat(n: str, label: str, vm: bool = False) -> str:
    cls = "n vm" if vm else "n"
    return f'<div class="stat"><div class="{cls}">{n}</div><div class="l">{label}</div></div>'


def page(*, fonts: str, ladder: str, dist: str, strip: str, legend: str,
         stats: str, control_table: str, dropped_table: str, numbers: dict) -> str:
    n = numbers
    return f"""<style>
{fonts}
{CSS}
</style>
<title>The invisible majority: a learned fingerprint for agent-written code</title>
<div class="wrap">

<p class="eyebrow">Learned fingerprint &middot; July 2026</p>
<h1>The invisible majority</h1>
<p class="deck">A commit trailer proves an agent wrote a line. It only proves it for
lines whose committer left the trailer on. In this cohort that is
{n['trailer_pct']}% of the code written since 2024. This estimates the rest from
code style alone, against a control group of projects that ban AI contributions
outright.</p>

{stats}

<div class="panel warn">
<h3>This is not a lower bound, and must never be shown as one</h3>
<p>The trailer-based adoption work rests on a property this page does not have:
every match there is a true positive, so its numbers are floors. Everything here
is an estimate carrying a measured false positive rate and a stated error floor.
The two are different kinds of claim. Do not add them, average them, or put them
on one axis.</p>
</div>

<section>
<h2>What the trailer method cannot see</h2>
<p>Blame every sampled file across the {n['repos']} cohort repositories and split
the surviving lines by era and signature. {n['signed_k']}k lines written since 2024
carry an agent trailer. <strong>{n['unsigned_m']}M do not.</strong> That unsigned
mass is code of unknown authorship, seven times the size of the part anyone can
prove. Calling it human code assumes the answer.</p>
</section>

<section>
<h2>The problem with the obvious approach</h2>
<p>Train a classifier on trailer-signed code against pre-2023 code and it
separates them at AUC {n['auc_era']}. That sounds like a fingerprint until you
ask what it learned. The heaviest weight in the model is <em>trailing
whitespace</em>: pre-2023 code has it, modern code does not, because formatters
took over. What the model has learned is when a line was written.</p>
<p>Run the harder version of the test instead. Trailer-signed against unsigned
code, same repositories, same era, and it collapses to AUC {n['auc_within']}.
Within the modern era, signed and unsigned code are stylistically
indistinguishable. Two things could produce that: style says nothing about
authorship, or unsigned code is largely agent-written too. Nothing in the cohort
can tell those apart, because the cohort contains no modern code that is
<em>known</em> to be human.</p>
</section>

<section>
<h2>Borrowing a control group from projects that ban AI</h2>
<p>There is a population of projects whose contribution policy rejects
AI-generated code. Their post-2024 commits are modern code the project asserts is
human-written, which is exactly the missing class. {n['control_repos']} of them cleared an
evidence gate: the pipeline greps each repository for its own policy language and
records the quote, so nothing enters the control set on a third party's say-so.</p>
{control_table}
<p>Six candidates were dropped for failing that gate, which is the gate doing its
job rather than a shortfall. The instructive one is Telegraf: its policy
<em>permits</em> AI-generated contributions subject to disclosure, so treating its
code as human would have poisoned the control group.</p>
{dropped_table}
</section>

<section>
<h2>Within one language, within one era, the signal is real</h2>
<p>Everything is fit and estimated inside a single language, never across them,
because a control group of C projects would teach the model that C means human.
Python is where both sides are thickest: {n['py_pos_k']}k trailer-signed agent lines and
{n['py_neg_k']}k lines from AI-banning projects, across {n['py_neg_repos']}
projects. Repo-grouped folds keep every scored line out of its own training set.</p>
<figure>
{dist}
{legend}
<figcaption>Python lines scored by a model trained only on trailer-signed code
versus AI-banning projects, out-of-fold. The maintainer's own repositories hold
code confirmed agent-written, from projects the model never saw; they land at
{n['own_mean']}, above the trailer-signed code the model was trained on. Unsigned cohort
code sits at {n['uns_mean']}, between the two human distributions and the agent
ones.</figcaption>
</figure>
<p>Line-weighted AUC is {n['py_auc']} against the control group and {n['py_auc_own']}
when the maintainer's confirmed-agent repositories are the positive class. That
transfer is the load-bearing evidence: the model was never shown those
repositories, and it ranks them as agent-written anyway.</p>
</section>

<section>
<h2>The estimate</h2>
<p>A classifier with a known true positive and false positive rate can be used as
a quantifier: the share of a population it flags, corrected by those rates, gives
the share that is genuinely positive. Four specifications run independently: two
definitions of agent code, crossed with two reference populations for human
code.</p>
<figure>
{ladder}
<figcaption>Every row is its own specification rather than a variant of a single
estimate. Bars show the 95% interval from resampling repositories; dots mark
point estimates where no interval applies. The reported range spans all
four.</figcaption>
</figure>
<p>The conservative rows use the cohort's own pre-2023 code as the human
reference. That reference is contaminated by era drift, so it overstates the
false positive rate and understates the answer. That is deliberate. It also
passes the placebo test by construction, which the other reference fails: hand
the
control-referenced estimator pre-2023 Python as if its authorship were unknown
and it reports {n['placebo']}% agent-written, for code that predates the tools.
That is its error floor, and the reason the range starts where it does.</p>
</section>

<section>
<h2>What the fingerprint is actually reading</h2>
<p>Each feature can be scored on whether modern humans have already moved to
where agent code sits. If they have, the feature dates code rather than
attributing it.</p>
<figure>
{strip}
{legend}
<figcaption>Population means per feature, each row scaled to its own range. The
right-hand number is (agent &minus; modern human) / (agent &minus; old human):
near 1 means modern humans have not followed. Unsigned code (hollow square) sits
between the human and agent marks on nearly every row.</figcaption>
</figure>
<p>Every surviving signal concerns comments rather than code structure. Agents
write comments as sentences, longer, capitalized and ending in a period, and they
attach docstrings that
modern human Python has largely stopped writing: present in {n['doc_agent']} of
agent hunks against {n['doc_human']} of control hunks. Comment
<em>density</em> runs the other way, as pure drift: it collapsed industry-wide
after 2023, and agents sit near the modern level rather than the old one.</p>
<p>The top row deserves suspicion rather than a headline. Double-quote preference
is a formatter setting, and the two projects holding 85% of the control group's
Python both prefer single quotes, so that row describes this control group rather
than human beings in general. The comment-shape features survive that objection:
docstrings appear in 10–34% of hunks across the four control projects
individually and 69% of agent hunks, and comments average 0.9–2.6 words against
5.3. Every control project sits on the same side of the agent value.</p>
<p>Reading those features as a mixture, with unsigned code's mean sitting between
the modern-human and agent means, gives an estimate that uses no classifier at
all: median {n['mix_median']}%, interquartile {n['mix_lo']}–{n['mix_hi']}%. It is
the cruder method and it lands below the fitted models. A second method that
simply agreed would have told us less.</p>
</section>

<section>
<h2>Where it fails</h2>
<ul>
<li><strong>TypeScript and JavaScript: not identifiable.</strong> AUC
{n['ts_auc']}, no better than chance. Those languages are {n['ts_pct']}% of the
cohort's modern lines, and the largest hole in this work. The feature means do differ, but
with only three control projects the between-project variance in TS style swamps
the authorship signal and nothing generalizes across folds.</li>
<li><strong>Rust: a weak instrument.</strong> AUC {n['rust_auc']}, with a placebo
that comes back at {n['rust_placebo']}%. Half of its apparent signal is error
floor.
Its numbers are in the aggregate but should not be quoted alone.</li>
<li><strong>C, C++, Go, Java: not enough labeled agent code.</strong> Under 2k
trailer-signed lines each in this sample.</li>
<li><strong>Coverage.</strong> The estimate covers {n['coverage']}% of the
cohort's modern lines. It is an estimate about Python and Rust, extended to
nothing else.</li>
<li><strong>The control group is a proxy.</strong> A policy is not enforcement.
Any agent code that slipped into those projects raises the measured false
positive rate and lowers the estimate, so the bias runs toward understatement.
It is still a bias.</li>
</ul>
</section>

<section>
<h2>Reproducing it</h2>
<pre>python3 -m authorship.corpus.cohort --files 100   # cohort corpus, Sourcegraph blame
python3 -m authorship.corpus.own --owner NAME     # agent ground truth, own repos
python3 -m authorship.corpus.control --files 250  # human control, AI-banning projects
python3 -m authorship.model                       # label sets, grouped CV, coefficients
python3 -m authorship.identify --boots 400        # the identified per-language estimate
python3 -m authorship.signals --lang Python       # feature audit + classifier-free check</pre>
<p>{n['hunks']} hunk records over {n['repos']} cohort repositories, {n['own_repos']}
maintainer repositories and {n['control_repos']} control projects. Features are
{n['features']} style measures, all rates rather than counts. The model is
logistic regression with L2, fit by Newton-Raphson in numpy, kept simple so the
argument can be checked by hand.</p>
</section>

<footer>
This page reports an estimate. The trailer-based bound lives in the adoption
artifact and stays there. Reference date {n['ref_date']}.
</footer>
</div>
"""
