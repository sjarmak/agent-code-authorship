"""Render the Sourcegraph-branded agent-footprint story shell."""

from __future__ import annotations

from string import Template


INDEX_STORY_PAGE = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="A Sourcegraph index study of how much agent-attributed code survives in open-source repositories, by harness, language, and age.">
<title>How much of open source is agent-written, and how much survives? | Sourcegraph research</title>
<style>
$font_css
:root {
  --paper: oklch(96% 0.012 83); --ink: oklch(22% 0.026 264);
  --muted: oklch(48% 0.025 260); --rule: oklch(78% 0.035 272);
  --violet-dark: oklch(34% 0.16 292); --orange: oklch(65% 0.18 46);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; text-rendering: optimizeLegibility; }
a { color: var(--violet-dark); text-underline-offset: 0.2em; }
a:focus-visible { outline: 3px solid var(--orange); outline-offset: 4px; }
.skip { position: absolute; left: 1rem; top: -6rem; z-index: 20; padding: 0.7rem 1rem; }
.skip:focus { top: 1rem; }
.mast { border-bottom: 1px solid var(--rule); }
.mast-inner { margin: auto; display: flex; gap: 1.5rem; align-items: baseline; justify-content: space-between; }
.hero { margin: auto; }
.kicker { margin: 0 0 1.2rem; font-weight: 750; text-transform: uppercase; }
h1, h2, h3 { margin: 0; line-height: 1.04; }
.deck { margin: 2rem 0 0; line-height: 1.42; }
.hero-observation { margin: 3rem 0 0; padding-top: 1.1rem; border-top: 1px solid var(--rule); }
.hero-observation strong { display: block; font-size: clamp(1.4rem, 3vw, 2.25rem); line-height: 1.1; }
.hero-observation span { display: block; max-width: 58ch; margin-top: 0.6rem; color: var(--muted); }
main { overflow: hidden; }
.section { margin: auto; }
.section-head { display: grid; align-items: start; }
.section-head p { max-width: 56ch; margin: 1.5rem 0 0; font-size: 1.12em; }
.prose p { margin: 0 0 1.5rem; }
figure { margin: clamp(3rem, 7vw, 6rem) 0; border-top: 1px solid var(--ink); padding-top: 1.2rem; }
.figure-head { display: flex; justify-content: space-between; gap: 2rem; align-items: baseline; margin-bottom: 1.5rem; }
.figure-head span { color: var(--muted); font-size: 0.76rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
.graphic { padding: clamp(1rem, 3vw, 2.5rem); overflow-x: auto; }
.graphic svg { display: block; width: 100%; height: auto; min-width: 42rem; }
figcaption { max-width: 75ch; margin: 1rem 0 0; color: var(--muted); font-size: 0.87rem; line-height: 1.52; }
.method-band { margin-top: 4rem; padding: clamp(2rem, 5vw, 4rem); }
.method-band h3 { max-width: 24ch; }
.method-band p { max-width: 65ch; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: clamp(2rem, 6vw, 6rem); margin-top: 4rem; }
.two-col > div { border-top: 1px solid var(--violet-dark); padding-top: 1.2rem; }
.artifact-list { margin: 3rem 0 0; padding: 0; list-style: none; }
.artifact-list li { display: grid; grid-template-columns: 1fr 1.5fr; gap: 2rem; padding: 1rem 0; border-top: 1px solid var(--rule); }
.artifact-list code { overflow-wrap: anywhere; }
footer { border-top: 1px solid var(--rule); }
.footer-inner { margin: auto; padding: 2rem; }
@media (max-width: 48rem) { .two-col, .artifact-list li { grid-template-columns: 1fr; } .graphic svg { min-width: 38rem; } }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
$blog_css
</style>
</head>
<body>
<a class="skip" href="#footprint">Skip to the evidence</a>
<nav class="blog-rail" aria-label="Study navigation">
  <a class="rail-brand" href="#top"><span class="mark-box"><svg class="sourcegraph-mark" viewBox="0 0 24 24" role="img" aria-label="Sourcegraph"><path d="M5 5.5 9.4 10M5 18.5 9.4 14M19 5.5 14.6 10M19 18.5 14.6 14M12 3v6M12 15v6" fill="none" stroke="var(--sg-theme-brand-color)" stroke-width="2.4" stroke-linecap="round"/></svg></span><span>Research brief</span></a>
  <ol class="rail-index">
    <li><a href="#footprint">Footprint</a></li><li><a href="#harnesses">Harnesses</a></li>
    <li><a href="#languages">Languages</a></li><li><a href="#age">Code age</a></li>
    <li><a href="#write-vs-head">Writing vs HEAD</a></li><li><a href="#reverts">Reverts</a></li>
    <li><a href="#methods">Methods</a></li><li><a href="#artifacts">Artifacts</a></li>
  </ol>
  <p class="rail-note">$indexed_count indexed repositories<br>Frozen revisions<br>Repository-balanced estimates</p>
</nav>
<div class="post-surface" id="top">
<header>
  <div class="mast"><div class="mast-inner"><strong>The agent footprint in open source</strong><span>Sourcegraph index study · August 2026</span></div></div>
  <div class="hero">
    <p class="kicker">A cross-repository study using Sourcegraph</p>
    <h1>How much of open source is agent-written, and how much survives?</h1>
    <p class="deck">The index can follow code tied to explicit agent provenance after it lands. The available controls still cannot support a defensible percentage of all open-source commits or all code at HEAD.</p>
    <p class="hero-observation"><strong>$survival_365 survives at one year; $unchanged_365 is unchanged.</strong><span>That result covers $survival_lines Go and Python lines across $survival_repos repositories. It is a durability estimate, not an open-source prevalence estimate.</span></p>
  </div>
</header>
<main>
$footprint_sections
<section class="section" id="methods">
  <div class="section-head"><div class="section-number">07</div><div><h2>How the index makes survival measurable</h2><p>Sourcegraph establishes the cross-repository evidence frame and binds candidate provenance to frozen revisions. Pinned Git then follows each attributable line forward through unchanged, modified, deleted, or censored states.</p></div></div>
  <div class="two-col"><div><h3>Population</h3><p>$canonical_count canonical repositories, $indexed_count accessible at their frozen revisions, and $provenance_commits reviewed provenance-positive commits across $provenance_repos repositories.</p></div><div><h3>Survival estimand</h3><p>Repository-balanced Kaplan-Meier survival at 30, 90, 180, and 365 days. Repository bootstrap intervals preserve codebase-level clustering.</p></div></div>
  <div class="prose"><p><strong>Known boundary:</strong> only Go and Python are in the survival cohort. Provenance-positive commits cover visible positives and carry sampling bias. Harness comparisons are observational and do not estimate causal tool quality.</p></div>
</section>
<section class="section" id="artifacts">
  <div class="section-head"><div class="section-number">08</div><div><h2>Inspect the evidence behind every claim</h2><p>The page is generated from frozen machine-readable artifacts. No chart value is typed into the HTML by hand.</p></div></div>
  <ul class="artifact-list">
    <li><strong>Corrected survival estimates</strong><code>results/survival-estimates.v2.json</code></li>
    <li><strong>Provenance-positive commits</strong><code>study/sourcegraph-agent-commit-catalog.v3.json</code></li>
    <li><strong>Prevalence identification boundary</strong><code>study/sourcegraph-era-study-estimates.v1.json</code></li>
    <li><strong>Indexed study frame</strong><code>results/SOURCEGRAPH_STUDY_ASSETS.md</code></li>
  </ul>
</section>
</main>
<footer><div class="footer-inner"><p>Rendered from frozen Sourcegraph and pinned-Git artifacts by <code>python3 -m authorship.build_v3_blog_post</code>.</p></div></footer>
</div>
</body>
</html>
""")


def render_index_story(
    *, footprint_sections: str, summary: dict[str, object], font_css: str, blog_css: str
) -> str:
    """Return the standalone Sourcegraph blog document."""
    overall = summary["overall"]
    return INDEX_STORY_PAGE.substitute(
        footprint_sections=footprint_sections,
        canonical_count=f'{summary["canonical_repository_count"]:,}',
        indexed_count=f'{summary["indexed_repository_count"]:,}',
        provenance_commits=f'{summary["provenance_commit_count"]:,}',
        provenance_repos=f'{summary["provenance_repository_count"]:,}',
        survival_repos=f'{summary["survival_repository_count"]:,}',
        survival_lines=f'{summary["survival_line_count"]:,}',
        survival_365=f'{100 * overall["survival_365"]:.1f}%',
        unchanged_365=f'{100 * overall["unchanged_365"]:.1f}%',
        font_css=font_css,
        blog_css=blog_css,
    )
