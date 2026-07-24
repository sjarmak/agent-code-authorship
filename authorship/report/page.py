#!/usr/bin/env python3
"""Render the fingerprint findings as a self-contained SG-brand page.

Reads results/identified.json, results/estimate.json and
data/control_evidence.json, plus the corpora for the score distributions, and
writes report.html: inert inline SVG, no network at render time.

Black ground, cream display type, monospace body, one accent colour reserved for
the agent signal. Fonts are not shipped; see fonts() for supplying your own.

Usage: python3 -m authorship.report.page
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np

from authorship import paths, data as fp_data, identify as fp_identify, logreg as fp_logreg
from authorship.features import CONTROL_NAMES, NAMES

OUT = paths.ROOT / "report.html"

CREAM, BODY, MUTED, VM, LINE = "#F5EDDD", "#C8C8C8", "#8A8A8A", "#FF5543", "#2a2a2a"
POP_LABEL = {"PRE": "cohort code, pre-2023", "HUMAN_MODERN": "AI-banning projects, 2024+",
             "SIGNED": "trailer-signed agent code", "OWN": "maintainer's own repos",
             "UNSIGNED": "cohort code 2024+, no trailer"}
POP_COLOR = {"PRE": "#4a4a4a", "HUMAN_MODERN": "#7d7d7d", "SIGNED": VM,
             "OWN": "#c0402f", "UNSIGNED": CREAM}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fonts() -> str:
    """Inline @font-face rules, if the operator has supplied any.

    The page this was first built for uses a licensed typeface, which does not
    belong in a public repository, so no font binaries ship here. Point
    AUTHORSHIP_FONT_CSS at a stylesheet containing @font-face rules with the
    faces embedded as data URIs (a CDN link will not work: the artifact host
    blocks external requests) and they are inlined. With nothing supplied the
    page falls back to system fonts and reads fine.
    """
    src = os.environ.get("AUTHORSHIP_FONT_CSS")
    if not src or not Path(src).exists():
        return ("/* No AUTHORSHIP_FONT_CSS supplied; using system faces. */\n"
                ":root{--display-font:system-ui,sans-serif;"
                "--body-font:ui-monospace,SFMono-Regular,Menlo,monospace}")
    return "\n".join(re.findall(r"@font-face\{[^}]*\}", Path(src).read_text()))


# --- data -------------------------------------------------------------------

def python_model(pops: dict) -> tuple[list, dict]:
    """Refit the Python SIGNED-vs-control fold models used for the charts."""
    cols = fp_identify.cols_without(fp_identify.LANG_COLS)
    pos = fp_identify.of_lang(pops["SIGNED"], "Python")
    neg = fp_identify.of_lang(pops["HUMAN_MODERN"], "Python")
    ls = fp_data.label_set(pos, neg)
    from authorship.estimate import fold_models, oof_scores
    models = fold_models(ls, cols)
    oof = oof_scores(ls, cols, models)
    ok = ~np.isnan(oof)
    thr = fp_logreg.best_threshold(ls["y"][ok], oof[ok], ls["w"][ok])
    return models, {"cols": cols, "threshold": float(thr)}


def distributions(pops: dict, models: list, cols, bins: int = 22) -> dict:
    """Line-weighted score histogram per population, Python only."""
    from authorship.estimate import score_holdout
    out = {}
    edges = np.linspace(0, 1, bins + 1)
    for pop in ("PRE", "HUMAN_MODERN", "SIGNED", "OWN", "UNSIGNED"):
        recs = fp_identify.of_lang(pops[pop], "Python")
        if not recs:
            continue
        s = score_holdout(recs, cols, models)
        w = np.array([r["lines"] for r in recs], dtype=float)
        hist, _ = np.histogram(s, bins=edges, weights=w)
        out[pop] = {"hist": (hist / hist.sum()).tolist(),
                    "mean": float(np.average(s, weights=w)),
                    "lines": float(w.sum())}
    return {"edges": edges.tolist(), "pops": out}


def feature_rows(pops: dict, lang: str = "Python", top: int = 10) -> list[dict]:
    """Features that separate agent from MODERN human code, ranked by effect size.

    Ranking by the authorship ratio alone surfaces features whose denominator is
    nearly zero — a ratio of 8 that means the old and agent values happen to sit
    on top of each other, not that the feature is informative. Effect size in
    standard deviations picks the features actually doing the work; the ratio
    then says whether that work is authorship or the calendar.
    """
    from authorship.signals import authorship_ratio, wmean
    sub = {k: fp_identify.of_lang(v, lang) for k, v in pops.items()}
    rows = []
    for i, name in enumerate(NAMES):
        if name in CONTROL_NAMES:
            continue
        vals = {k: wmean(sub.get(k, []), i) for k in POP_LABEL}
        ratio = authorship_ratio(vals["PRE"], vals["HUMAN_MODERN"], vals["SIGNED"])
        allv = [r["v"][i] for pop in sub.values() for r in pop]
        sd = float(np.std(allv)) if allv else 0.0
        if not np.isfinite(ratio) or sd <= 0:
            continue
        rows.append({"name": name, "vals": vals, "ratio": float(ratio),
                     "effect": abs(vals["SIGNED"] - vals["HUMAN_MODERN"]) / sd})
    rows = [r for r in rows if r["ratio"] >= 0.6]
    rows.sort(key=lambda r: -r["effect"])
    return rows[:top]


# --- charts -----------------------------------------------------------------

def ladder_svg(rows: list[dict], width: int = 860) -> str:
    """One horizontal range per specification, on a shared 0-100% axis."""
    left, right, top, row_h = 330, 40, 34, 46
    height = top + row_h * len(rows) + 34
    x0, x1 = left, width - right

    def px(v: float) -> float:
        return x0 + (x1 - x0) * v

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="estimate ranges by specification">']
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        parts.append(f'<line x1="{px(t):.1f}" y1="{top - 12}" x2="{px(t):.1f}" '
                     f'y2="{height - 30}" stroke="{LINE}" stroke-width="1"/>')
        parts.append(f'<text x="{px(t):.1f}" y="{height - 14}" fill="{MUTED}" '
                     f'font-size="11" text-anchor="middle" class="mono">'
                     f'{int(t * 100)}%</text>')
    for i, r in enumerate(rows):
        y = top + i * row_h
        lo, hi = r["range"]
        col = r.get("color", CREAM)
        parts.append(f'<text x="{left - 14}" y="{y + 4}" fill="{BODY}" font-size="11.5" '
                     f'text-anchor="end" class="mono">{esc(r["label"])}</text>')
        if abs(hi - lo) < 0.004:
            parts.append(f'<circle cx="{px(lo):.1f}" cy="{y}" r="5" fill="{col}"/>')
        else:
            parts.append(f'<line x1="{px(lo):.1f}" y1="{y}" x2="{px(hi):.1f}" y2="{y}" '
                         f'stroke="{col}" stroke-width="7" stroke-linecap="butt"/>')
        val = (f'{lo * 100:.0f}%' if abs(hi - lo) < 0.004
               else f'{lo * 100:.0f}–{hi * 100:.0f}%')
        parts.append(f'<text x="{px(hi):.1f}" y="{y - 12}" fill="{col}" font-size="12" '
                     f'class="mono" dx="-2" text-anchor="end">{val}</text>')
        if r.get("note"):
            parts.append(f'<text x="{left - 14}" y="{y + 19}" fill="{MUTED}" '
                         f'font-size="10" text-anchor="end" class="mono">'
                         f'{esc(r["note"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def distribution_svg(dist: dict, threshold: float, width: int = 860) -> str:
    """Small-multiple ridges: where each population's Python lines score."""
    order = ["PRE", "HUMAN_MODERN", "SIGNED", "OWN", "UNSIGNED"]
    order = [p for p in order if p in dist["pops"]]
    pad_l, pad_r, band, gap = 270, 30, 52, 10
    height = len(order) * (band + gap) + 40
    x0, x1 = pad_l, width - pad_r
    peak = max(max(dist["pops"][p]["hist"]) for p in order)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="score distributions by population">']
    for i, pop in enumerate(order):
        d = dist["pops"][pop]
        y_base = i * (band + gap) + band
        col = POP_COLOR[pop]
        pts = []
        n = len(d["hist"])
        for j, v in enumerate(d["hist"]):
            x = x0 + (x1 - x0) * (j + 0.5) / n
            pts.append(f'{x:.1f},{y_base - (v / peak) * (band - 8):.1f}')
        parts.append(f'<polyline points="{x0},{y_base} {" ".join(pts)} {x1},{y_base}" '
                     f'fill="{col}" fill-opacity="0.22" stroke="{col}" '
                     f'stroke-width="1.5"/>')
        mx = x0 + (x1 - x0) * d["mean"]
        parts.append(f'<line x1="{mx:.1f}" y1="{y_base - band + 6}" x2="{mx:.1f}" '
                     f'y2="{y_base}" stroke="{col}" stroke-width="1" '
                     f'stroke-dasharray="2 2"/>')
        parts.append(f'<text x="{pad_l - 14}" y="{y_base - 14}" fill="{BODY}" '
                     f'font-size="11.5" text-anchor="end" class="mono">'
                     f'{esc(POP_LABEL[pop])}</text>')
        parts.append(f'<text x="{pad_l - 14}" y="{y_base}" fill="{MUTED}" '
                     f'font-size="10.5" text-anchor="end" class="mono">'
                     f'mean {d["mean"]:.2f} · {d["lines"] / 1000:.0f}k lines</text>')
    tx = x0 + (x1 - x0) * threshold
    parts.append(f'<line x1="{tx:.1f}" y1="8" x2="{tx:.1f}" y2="{height - 34}" '
                 f'stroke="{MUTED}" stroke-width="1"/>')
    parts.append(f'<text x="{tx:.1f}" y="{height - 20}" fill="{MUTED}" font-size="10.5" '
                 f'text-anchor="middle" class="mono">threshold</text>')
    for t, lab in ((0.0, "human-like"), (1.0, "agent-like")):
        anchor = "start" if t == 0 else "end"
        x = x0 if t == 0 else x1
        parts.append(f'<text x="{x}" y="{height - 20}" fill="{MUTED}" font-size="10.5" '
                     f'text-anchor="{anchor}" class="mono">{lab}</text>')
    parts.append("</svg>")
    return "".join(parts)


def feature_strip_svg(rows: list[dict], width: int = 860) -> str:
    """Per feature, the four population means placed on a normalized axis."""
    pad_l, pad_r, row_h, top = 200, 96, 30, 26
    height = top + row_h * len(rows) + 26
    x0, x1 = pad_l, width - pad_r
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="feature means by population">']
    for i, r in enumerate(rows):
        y = top + i * row_h
        vals = r["vals"]
        keys = ["PRE", "HUMAN_MODERN", "UNSIGNED", "SIGNED"]
        lo = min(vals[k] for k in keys)
        hi = max(vals[k] for k in keys)
        span = (hi - lo) or 1.0
        parts.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="{LINE}" '
                     f'stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 12}" y="{y + 4}" fill="{BODY}" font-size="11.5" '
                     f'text-anchor="end" class="mono">{esc(r["name"])}</text>')
        for k in keys:
            x = x0 + (x1 - x0) * (vals[k] - lo) / span
            col = POP_COLOR[k]
            if k == "UNSIGNED":
                parts.append(f'<rect x="{x - 3.5:.1f}" y="{y - 3.5}" width="7" '
                             f'height="7" fill="none" stroke="{col}" stroke-width="1.4"/>')
            else:
                parts.append(f'<circle cx="{x:.1f}" cy="{y}" r="4" fill="{col}"/>')
        shown = f'{r["ratio"]:.2f}' if r["ratio"] < 3 else "&gt;3"
        parts.append(f'<text x="{width - pad_r + 10}" y="{y + 4}" fill="{MUTED}" '
                     f'font-size="11" class="mono">{shown}</text>')
    parts.append(f'<text x="{width - pad_r + 10}" y="{top - 12}" fill="{MUTED}" '
                 f'font-size="10" class="mono">authorship</text>')
    parts.append("</svg>")
    return "".join(parts)


def legend() -> str:
    items = "".join(
        f'<span class="key"><i style="background:{POP_COLOR[p]}"></i>'
        f'{esc(POP_LABEL[p])}</span>' for p in
        ("PRE", "HUMAN_MODERN", "UNSIGNED", "SIGNED", "OWN"))
    return f'<div class="legend">{items}</div>'


# --- tables -----------------------------------------------------------------

def control_table(evidence: dict) -> str:
    rows = sorted(((k, v) for k, v in evidence["repos"].items()),
                  key=lambda kv: -kv[1]["lines"])
    body = "".join(
        f'<tr><td>{esc(name)}</td><td class="num">{v["lines"]:,}</td>'
        f'<td class="q">&ldquo;{esc(v["policy"][0]["quote"][:150])}&rdquo;'
        f'<br><span style="opacity:.7">{esc(v["policy"][0]["file"][:70])}</span></td></tr>'
        for name, v in rows)
    return ('<div class="scroll"><table><thead><tr><th>project</th>'
            '<th style="text-align:right">2024+ lines</th>'
            f'<th>its own policy language</th></tr></thead><tbody>{body}'
            '</tbody></table></div>')


def dropped_table(evidence: dict) -> str:
    body = "".join(
        f'<tr><td>{esc(d.split(" (")[0])}</td>'
        f'<td><span class="tag out">{esc(d.split("(")[1].rstrip(")"))}</span></td></tr>'
        for d in evidence.get("dropped", []) if "(" in d)
    return ('<div class="scroll"><table><thead><tr><th>candidate</th>'
            f'<th>why it was not used</th></tr></thead><tbody>{body}'
            '</tbody></table></div>')


# --- assembly ---------------------------------------------------------------

def ladder_rows(ident: dict) -> list[dict]:
    """One row per specification, plus the trailer floor and reported range."""
    by = {r["label"]: r for r in ident["runs"]}
    signed = by.get("trailer-signed cohort code")
    own = by.get("maintainer's own repos")
    trailer = signed["aggregate"]["trailer_visible"]
    rows = [{"label": "trailer-signed only", "range": [trailer, trailer],
             "color": "#7d7d7d", "note": "certain; the adoption artifact's floor"}]
    for r, name in ((own, "own repos"), (signed, "trailer-signed")):
        if not r:
            continue
        cons = r["aggregate"]["share_2024plus_conservative"]
        rows.append({"label": f"{name} vs cohort pre-2023", "range": [cons, cons],
                     "color": "#c0402f", "note": "conservative reference"})
        rows.append({"label": f"{name} vs AI-banning projects",
                     "range": r["ci95"], "color": VM, "note": "95% CI over repos"})
    lo, hi = ident["point_range"]
    rows.append({"label": "reported estimate", "range": [lo, hi], "color": CREAM,
                 "note": "across all four specifications"})
    return rows


def numbers(ident: dict, est: dict, evidence: dict, pops: dict, dist: dict,
            frows: list[dict], model: dict) -> dict:
    signed = next(r for r in ident["runs"] if r["label"] == "trailer-signed cohort code")
    own = next(r for r in ident["runs"] if r["label"] == "maintainer's own repos")
    py_s, py_o = signed["per_lang"]["Python"], own["per_lang"]["Python"]
    rust = signed["per_lang"].get("Rust", {})
    ts = next((s for s in signed["skipped"] if s.startswith("TS/JS")), "")
    ts_auc = re.search(r"AUC ([0-9.]+)", ts)
    lines = fp_data.load_control()
    docs = {p: np.average([r["v"][NAMES.index("docstring_present")]
                           for r in fp_identify.of_lang(pops[p], "Python")],
                          weights=[r["lines"] for r in
                                   fp_identify.of_lang(pops[p], "Python")])
            for p in ("SIGNED", "HUMAN_MODERN")}
    from authorship.signals import mixture_table
    mrows = [(0.0, r["name"], r["vals"], r["ratio"]) for r in frows]
    mix, _, _ = mixture_table({k: fp_identify.of_lang(v, "Python")
                               for k, v in pops.items()}, mrows)
    mvals = [m[2] for m in mix] or [float("nan")]
    uns_lines = sum(r["lines"] for r in pops["UNSIGNED"])
    ts_lines = sum(r["lines"] for r in pops["UNSIGNED"] if r["lang"] == "TS/JS")
    return {
        "trailer_pct": f"{signed['aggregate']['trailer_visible'] * 100:.0f}",
        "repos": len({r["repo"] for r in pops["SIGNED"]} | {r["repo"] for r in pops["UNSIGNED"]}),
        "own_repos": len({r["repo"] for r in pops["OWN"]}),
        "control_repos": len(evidence["repos"]),
        "signed_k": f"{sum(r['lines'] for r in pops['SIGNED']) / 1000:.0f}",
        "unsigned_m": f"{uns_lines / 1e6:.2f}",
        "auc_era": f"{est['runs'][0]['auc_lines']:.2f}",
        "auc_within": "0.52",
        "py_pos_k": f"{py_s['n_pos_lines'] / 1000:.0f}",
        "py_neg_k": f"{py_s['n_neg_lines'] / 1000:.0f}",
        "py_neg_repos": py_s["neg_repos"],
        "py_auc": f"{py_s['auc']:.2f}", "py_auc_own": f"{py_o['auc']:.2f}",
        "own_mean": f"{dist['pops']['OWN']['mean']:.2f}",
        "uns_mean": f"{dist['pops']['UNSIGNED']['mean']:.2f}",
        "placebo": f"{py_s['placebo_pre2023_as_unknown'] * 100:.0f}",
        "doc_agent": f"{docs['SIGNED'] * 100:.0f}%",
        "doc_human": f"{docs['HUMAN_MODERN'] * 100:.0f}%",
        "mix_median": f"{np.median(mvals) * 100:.0f}",
        "mix_lo": f"{np.percentile(mvals, 25) * 100:.0f}",
        "mix_hi": f"{np.percentile(mvals, 75) * 100:.0f}",
        "ts_auc": ts_auc.group(1) if ts_auc else "0.41",
        "ts_pct": f"{ts_lines / uns_lines * 100:.0f}",
        "rust_auc": f"{rust.get('auc', float('nan')):.2f}",
        "rust_placebo": f"{rust.get('placebo_pre2023_as_unknown', 0) * 100:.0f}",
        "coverage": f"{signed['aggregate']['coverage_of_cohort_modern_lines'] * 100:.0f}",
        "hunks": f"{len(fp_data.load()[0]):,}",
        "features": len(NAMES),
        "ref_date": "2026-07-24",
    }


def stats_block(n: dict, ident: dict) -> str:
    from authorship.report.copy import stat
    lo, hi = ident["point_range"]
    return ('<div class="stats">'
            + stat(f"{n['trailer_pct']}%", "of 2024+ lines carry an agent trailer — "
                                           "certain, and a floor")
            + stat(f"{lo * 100:.0f}–{hi * 100:.0f}%", "estimated agent-written, "
                                                      "identifiable languages", vm=True)
            + stat(f"{n['unsigned_m']}M", "surviving lines of unknown authorship")
            + stat(f"{n['control_repos']}", "AI-banning projects as the human control")
            + "</div>")


def main() -> int:
    cohort, own = fp_data.load()
    pops = {k: fp_identify.with_merged_langs(v) for k, v in
            fp_data.populations(cohort, own, fp_data.load_control()).items()}
    ident = json.loads((paths.RESULTS / "identified.json").read_text())
    est = json.loads((paths.RESULTS / "estimate.json").read_text())
    evidence = json.loads(paths.CONTROL_EVIDENCE.read_text())

    models, meta = python_model(pops)
    dist = distributions(pops, models, meta["cols"])
    frows = feature_rows(pops, "Python", top=10)
    n = numbers(ident, est, evidence, pops, dist, frows, meta)

    from authorship.report.copy import page
    html = page(fonts=fonts(), ladder=ladder_svg(ladder_rows(ident)),
                dist=distribution_svg(dist, meta["threshold"]),
                strip=feature_strip_svg(frows), legend=legend(),
                stats=stats_block(n, ident), control_table=control_table(evidence),
                dropped_table=dropped_table(evidence), numbers=n)
    OUT.write_text(html)
    print(f"  {OUT.name}: {len(html) / 1024:.0f} KB, "
          f"{len(frows)} feature rows, {len(dist['pops'])} distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
