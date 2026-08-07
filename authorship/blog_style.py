"""Sourcegraph blog visual tokens for the self-contained study report."""

from __future__ import annotations

import base64
from pathlib import Path


BLOG_CSS = r"""
:root {
  color-scheme: dark;
  --sg-theme-surface: oklch(18% 0.00284 27deg);
  --sg-theme-background: oklch(16% 0.00284 27deg);
  --sg-theme-border-color: oklch(25% 0.00284 27deg);
  --sg-theme-foreground: oklch(94.5% 0.00284 27deg);
  --sg-theme-foreground-muted: oklch(74% 0.00284 27deg);
  --sg-theme-brand-color: oklch(71% 0.19 27deg);
  --sg-theme-accent: oklch(57% 0.2 265deg);
  --sg-theme-accent-muted: oklch(41% 0.13 265deg);
  --sg-theme-success: oklch(51% 0.08 160deg);
  --sg-theme-warning: oklch(68.5% 0.175 52deg);
  --sg-theme-danger: oklch(68% 0.18 27deg);
  --sg-theme-shadow: oklch(8% 0.00284 27deg / 0.85);
  --viz-bg: oklch(19.5% 0.008 265deg);
  --viz-ink: var(--sg-theme-foreground);
  --viz-muted: var(--sg-theme-foreground-muted);
  --viz-rule: oklch(32% 0.018 265deg);
  --viz-accent: var(--sg-theme-accent);
  --viz-brand: var(--sg-theme-brand-color);
  --viz-green: var(--sg-theme-success);
  --viz-warning: var(--sg-theme-warning);
  --viz-blue: oklch(70% 0.15 248deg);
  --viz-pink: oklch(72% 0.14 330deg);
  --viz-blog-purple: #8552f2;
  --viz-blog-coral: #ff7867;
  --viz-blog-black: #020202;
  --viz-blog-grid: #343434;
  --viz-blog-text: #ededed;
  --viz-blog-muted: #a9a9a9;
  --font-heading: 'Perfectly Nineties', Georgia, 'Times New Roman', serif;
  --font-sans: 'Poly Sans', 'Avenir Next', Avenir, 'Segoe UI', sans-serif;
  --font-mono: 'Poly Sans Mono', 'SFMono-Regular', Consolas, monospace;
  --content: 74rem;
  --measure: 68ch;
}

html { background: oklch(13% 0.003 27deg); }
body {
  background: var(--sg-theme-background);
  color: var(--sg-theme-foreground);
  font-family: var(--font-sans);
  font-size: 1rem;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}
body::before {
  position: fixed;
  inset: 0;
  z-index: -1;
  background-image:
    linear-gradient(to right, oklch(94.5% 0.00284 27deg / 0.035) 1px, transparent 1px),
    linear-gradient(to bottom, oklch(94.5% 0.00284 27deg / 0.035) 1px, transparent 1px);
  background-size: 32px 32px;
  content: '';
}

a { color: inherit; text-decoration-color: var(--sg-theme-foreground-muted); }
a:hover { text-decoration-color: var(--sg-theme-foreground); }
a:focus-visible { outline-color: var(--sg-theme-brand-color); }
.skip { background: var(--sg-theme-foreground); color: var(--sg-theme-background); }

.blog-rail {
  position: fixed;
  inset: 0 auto 0 max(1rem, calc((100vw - 90rem) / 2));
  z-index: 12;
  width: 15.5rem;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 2rem;
}
.rail-brand {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  text-decoration: none;
}
.mark-box {
  width: 1.8rem;
  height: 1.8rem;
  padding: 0.35rem;
  display: grid;
  place-items: center;
  border: 1px solid var(--sg-theme-border-color);
  border-radius: 6px;
  background: var(--sg-theme-surface);
}
.sourcegraph-mark { width: 100%; height: 100%; }
.rail-index { margin: auto 0; padding: 0; list-style: none; }
.rail-index li { margin: 0.55rem 0; }
.rail-index a {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  color: var(--sg-theme-foreground-muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  line-height: 1.3;
  text-decoration: none;
}
.rail-index a::before {
  width: 0.35rem;
  height: 0.35rem;
  border: 1px solid currentColor;
  border-radius: 50%;
  content: '';
}
.rail-index a:hover { color: var(--sg-theme-foreground); }
.rail-note {
  color: var(--sg-theme-foreground-muted);
  font-family: var(--font-mono);
  font-size: 0.67rem;
  line-height: 1.5;
}

.post-surface {
  max-width: 72rem;
  min-height: calc(100vh - 1rem);
  margin: 0.5rem max(0.5rem, calc((100vw - 90rem) / 2)) 0.5rem
    max(17rem, calc((100vw - 58rem) / 2));
  overflow: hidden;
  border: 1px solid var(--sg-theme-border-color);
  border-radius: 12px;
  background: var(--sg-theme-background);
  box-shadow: 0 12px 36px var(--sg-theme-shadow);
}
.mast {
  border-color: var(--sg-theme-border-color);
  padding: 0.85rem clamp(1rem, 4vw, 2rem);
  background: oklch(16% 0.00284 27deg / 0.94);
}
.mast-inner { max-width: 50rem; }
.mast strong, .mast span { font-family: var(--font-mono); font-size: 0.72rem; }
.mast span { color: var(--sg-theme-foreground-muted); }

.hero {
  max-width: 50rem;
  min-height: auto;
  padding: clamp(4rem, 9vw, 7rem) 2rem clamp(3rem, 7vw, 5rem);
  display: block;
}
.kicker {
  color: var(--sg-theme-brand-color);
  font-family: var(--font-mono);
  font-size: 0.72rem;
  letter-spacing: 0.08em;
}
h1, h2, h3 { font-family: var(--font-heading); font-weight: 400; letter-spacing: -0.025em; }
h1 { max-width: 13ch; font-size: clamp(3rem, 8vw, 6.5rem); line-height: 0.96; }
h2 { max-width: 18ch; font-size: clamp(2.25rem, 5vw, 4.2rem); }
h3 { font-size: clamp(1.4rem, 2.2vw, 2rem); }
.deck { max-width: 54ch; font-size: clamp(1.08rem, 2vw, 1.35rem); color: var(--sg-theme-foreground-muted); }
.hero-note {
  max-width: 29rem;
  margin-top: 3rem;
  border: 1px solid var(--sg-theme-border-color);
  border-radius: 8px;
  padding: 1rem;
  background: var(--sg-theme-surface);
  color: var(--sg-theme-foreground-muted);
}
.hero-note b { color: var(--sg-theme-foreground); font-family: var(--font-heading); }

.toc { display: none; }
.section { max-width: 58rem; padding: clamp(4rem, 8vw, 7rem) 2rem; }
.section + .section { border-top: 1px solid var(--sg-theme-border-color); }
.section-head { grid-template-columns: 5rem minmax(0, 1fr); gap: 1.5rem; margin-bottom: 3.5rem; }
.section-number {
  color: var(--sg-theme-brand-color);
  font-family: var(--font-mono);
  font-size: 1rem;
  font-weight: 400;
  letter-spacing: 0;
}
.section-head p { max-width: 56ch; color: var(--sg-theme-foreground-muted); }
.prose { max-width: var(--measure); margin: 0; }
.prose strong, .method-band strong { color: var(--sg-theme-foreground); }

.claim { background: var(--sg-theme-brand-color); color: oklch(13% 0.02 27deg); }
.claim p { max-width: 20ch; font-family: var(--font-heading); font-weight: 400; }
.claim em { color: inherit; text-decoration: underline; text-underline-offset: 0.12em; }

figure { border-color: var(--sg-theme-border-color); }
.figure-head { align-items: end; }
.figure-head span { color: var(--sg-theme-foreground-muted); font-family: var(--font-mono); }
.graphic {
  border: 1px solid var(--sg-theme-border-color);
  border-radius: 0;
  background: var(--viz-blog-black);
}
.graphic svg { min-width: 42rem; }
.graphic svg text { font-family: var(--font-sans); }
figcaption { color: var(--sg-theme-foreground-muted); }
.viz-key {
  display: inline-flex;
  gap: 1rem;
  margin-top: 0.5rem;
  color: var(--sg-theme-foreground-muted);
  font-family: var(--font-mono);
  font-size: 0.7rem;
}
.current-mobile-data { display: none; }
.finding-strip {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  margin: 0;
  border-block: 1px solid var(--sg-theme-border-color);
}
.finding-strip p {
  margin: 0;
  padding: 1.25rem;
  border-right: 1px solid var(--sg-theme-border-color);
  color: var(--sg-theme-foreground-muted);
}
.finding-strip p:last-child { border-right: 0; }
.finding-strip strong {
  display: block;
  color: var(--sg-theme-foreground);
  font-family: var(--font-heading);
  font-size: clamp(2rem, 4vw, 3.5rem);
  font-weight: 400;
  line-height: 1;
}

.fact-run { border-color: var(--sg-theme-border-color); }
.fact { border-color: var(--sg-theme-border-color); }
.fact b { color: var(--sg-theme-foreground); font-family: var(--font-heading); font-weight: 400; }
.fact span { color: var(--sg-theme-foreground-muted); }
.method-band, .two-col, .artifact-list li { border-color: var(--sg-theme-border-color); }
.method-band { background: var(--sg-theme-surface); }
.status {
  color: color-mix(in oklch, var(--sg-theme-success) 55%, var(--sg-theme-foreground));
}
.warning { color: var(--sg-theme-warning); }
code { font-family: var(--font-mono); }
footer { border-color: var(--sg-theme-border-color); background: var(--sg-theme-surface); }
.footer-inner { max-width: 58rem; }

@media (max-width: 64rem) {
  .blog-rail {
    position: relative;
    inset: auto;
    width: 100%;
    padding: 0.75rem 1rem;
    flex-direction: row;
    align-items: center;
    gap: 1.25rem;
    overflow-x: auto;
  }
  .rail-index {
    display: flex;
    flex: 0 0 auto;
    gap: 1rem;
    margin: 0;
    white-space: nowrap;
  }
  .rail-index li { margin: 0; }
  .rail-index a::before { display: none; }
  .rail-note { display: none; }
  .post-surface { margin: 0; min-height: 100vh; border-inline: 0; border-radius: 0; }
}
@media (max-width: 48rem) {
  .hero, .section { padding-inline: 1rem; }
  .section-head { display: block; }
  .section-number { margin-bottom: 0.8rem; }
  .figure-head { display: block; }
  .graphic { margin-inline: -1rem; border-inline: 0; border-radius: 0; padding: 1rem; }
  .graphic svg { min-width: 38rem; }
  .current-mobile-data {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0;
    margin: 0.75rem 0 0;
    border-block: 1px solid var(--sg-theme-border-color);
  }
  .current-mobile-data div {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.55rem 0;
    border-bottom: 1px solid var(--sg-theme-border-color);
  }
  .current-mobile-data div:last-child { border-bottom: 0; }
  .current-mobile-data dt { color: var(--sg-theme-foreground-muted); }
  .current-mobile-data dd { margin: 0; font-family: var(--font-mono); text-align: right; }
  .fact-run { grid-template-columns: repeat(2, 1fr); }
  .finding-strip { grid-template-columns: 1fr; }
  .finding-strip p { border-right: 0; border-bottom: 1px solid var(--sg-theme-border-color); }
  .finding-strip p:last-child { border-bottom: 0; }
  .fact { min-height: 8rem; border-bottom: 1px solid var(--sg-theme-border-color); }
}
"""


_FONT_SPECS = (
    ("Perfectly Nineties", "PerfectlyNineties-Regular.woff", "woff", 400),
    ("Perfectly Nineties", "PerfectlyNineties-Semibold.woff", "woff", 600),
    ("Perfectly Nineties", "PerfectlyNineties-Bold.woff", "woff", 700),
    ("Poly Sans", "PolySans-Neutral.woff2", "woff2", 400),
    ("Poly Sans Mono", "PolySans-SlimMono-300.woff2", "woff2", 300),
)


def embedded_sourcegraph_font_css(font_root: Path) -> str:
    """Embed the local Sourcegraph blog fonts in a standalone document."""
    faces = []
    for family, filename, font_format, weight in _FONT_SPECS:
        path = font_root / filename
        if not path.is_file():
            raise FileNotFoundError(f"required Sourcegraph font is missing: {path}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        faces.append(
            "@font-face {"
            f"font-family: '{family}';"
            f"src: url('data:font/{font_format};base64,{encoded}') format('{font_format}');"
            f"font-weight: {weight};font-style: normal;font-display: swap;"
            "}"
        )
    return "\n".join(faces)


_PALETTE_REPLACEMENTS = {
    "oklch(98% 0.006 83)": "var(--viz-bg)",
    "oklch(96% 0.012 83)": "var(--viz-ink)",
    "oklch(22% 0.026 264)": "var(--viz-ink)",
    "oklch(48% 0.025 260)": "var(--viz-muted)",
    "oklch(78% 0.035 272)": "var(--viz-rule)",
    "oklch(48% 0.2 292)": "var(--viz-accent)",
    "oklch(34% 0.16 292)": "var(--viz-accent)",
    "oklch(65% 0.18 46)": "var(--viz-brand)",
    "oklch(55% 0.14 155)": "var(--viz-green)",
    "oklch(53% 0.15 248)": "var(--viz-blue)",
    "#fff": "var(--viz-bg)",
    "#222222": "var(--viz-ink)",
    "#222": "var(--viz-ink)",
    "#666": "var(--viz-muted)",
    "#bbb": "var(--viz-rule)",
    "#0072B2": "var(--viz-blue)",
    "#D55E00": "var(--viz-brand)",
    "#CC79A7": "var(--viz-pink)",
    "#009E73": "var(--viz-green)",
}


def apply_blog_visual_style(document: str) -> str:
    """Map legacy inert SVG colors onto the Sourcegraph blog palette."""
    styled = document
    for source, target in _PALETTE_REPLACEMENTS.items():
        styled = styled.replace(source, target)
    return styled
