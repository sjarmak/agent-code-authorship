#!/usr/bin/env python3
"""Feature-level audit: which stylistic differences are authorship, and which
are just the calendar?

For every feature, the line-weighted mean in each population:

  PRE           cohort code from before 2023            human, old
  HUMAN_MODERN  post-2024 code from AI-banning projects human, new
  SIGNED        trailer-signed cohort code              agent, new
  OWN           the maintainer's own repos              agent, new
  UNSIGNED      post-2024 cohort code, no trailer       unknown, new

Read the columns as a 2x2. A feature where HUMAN_MODERN has already moved to
where SIGNED sits is ERA DRIFT — the industry changed, not the author. A feature
where HUMAN_MODERN stays with PRE and only the agent columns move is a real
AUTHORSHIP signal. The gap ratio in the last column scores exactly that:

    (agent - human_modern) / (agent - pre)   near 1 -> authorship
                                             near 0 -> drift

Restricted to one language at a time, because a Go/Python mix would fake
differences that are only language.

Usage: python3 -m authorship.signals [--lang Python] [--top 18]
"""
from __future__ import annotations

import argparse
import numpy as np

from authorship import data as fp_data
from authorship.features import CONTROL_NAMES, NAMES

ORDER = ("PRE", "HUMAN_MODERN", "SIGNED", "OWN", "UNSIGNED")


def wmean(records: list[dict], col: int) -> float:
    if not records:
        return float("nan")
    v = np.array([r["v"][col] for r in records], dtype=float)
    w = np.array([r["lines"] for r in records], dtype=float)
    return float(np.average(v, weights=w))


def authorship_ratio(pre: float, human_new: float, agent: float) -> float:
    """How much of the old->agent movement is NOT explained by modern humans."""
    denom = agent - pre
    if abs(denom) < 1e-9:
        return float("nan")
    return (agent - human_new) / denom


def mixture_share(human_new: float, agent: float, unsigned: float) -> float:
    """Method of moments. If unsigned code is a mix of modern-human and agent
    code, its mean on a feature sits between theirs, and the position gives the
    mixing weight. Independent of the classifier — no fit, no threshold, no
    cross-validation — so it is a genuine second opinion rather than a restated
    one. Fragile per feature (any project-specific convention moves it), which is
    why it is only worth reading as a spread over several features."""
    denom = agent - human_new
    if abs(denom) < 1e-9:
        return float("nan")
    return (unsigned - human_new) / denom


def feature_sd(pops: dict, col: int) -> float:
    v = [r["v"][col] for pop in pops.values() for r in pop]
    return float(np.std(v)) if v else 0.0


def mixture_table(pops: dict, rows: list, min_authorship: float = 0.6,
                  min_gap_sd: float = 0.3) -> tuple[list, int, int]:
    """Only features whose agent/human gap is wide enough to divide by. A gap of
    a hundredth of a standard deviation puts noise in the denominator and returns
    a percentage that looks precise and means nothing."""
    out, thin, clipped = [], 0, 0
    for _, name, vals, ratio in rows:
        if not np.isfinite(ratio) or ratio < min_authorship:
            continue
        sd = feature_sd(pops, NAMES.index(name))
        for agent_pop in ("SIGNED", "OWN"):
            gap = abs(vals[agent_pop] - vals["HUMAN_MODERN"])
            if sd <= 0 or gap < min_gap_sd * sd:
                thin += 1
                continue
            p = mixture_share(vals["HUMAN_MODERN"], vals[agent_pop], vals["UNSIGNED"])
            if not np.isfinite(p):
                continue
            if p < 0 or p > 1:
                clipped += 1
            out.append((name, agent_pop, float(np.clip(p, 0.0, 1.0))))
    return out, thin, clipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lang", default="Python")
    ap.add_argument("--top", type=int, default=18)
    args = ap.parse_args()

    cohort, own = fp_data.load()
    pops = fp_data.populations(cohort, own, fp_data.load_control())
    pops = {k: [r for r in v if r["lang"] == args.lang] for k, v in pops.items()}
    counts = {k: sum(r["lines"] for r in v) for k, v in pops.items()}
    print(f"language {args.lang} — lines per population: " +
          ", ".join(f"{k} {counts[k]:,}" for k in ORDER))
    if min(counts.get(k, 0) for k in ("PRE", "HUMAN_MODERN", "SIGNED")) < 2000:
        print("  (a population is too thin here to read much into)")

    rows = []
    for i, name in enumerate(NAMES):
        if name in CONTROL_NAMES:
            continue
        vals = {k: wmean(pops.get(k, []), i) for k in ORDER}
        spread = abs(vals["SIGNED"] - vals["PRE"])
        rows.append((spread, name, vals,
                     authorship_ratio(vals["PRE"], vals["HUMAN_MODERN"],
                                      vals["SIGNED"])))
    rows.sort(reverse=True, key=lambda r: r[0])

    head = f"{'feature':22}" + "".join(f"{k[:9]:>11}" for k in ORDER) + "  authorship"
    print("\n" + head)
    print("-" * len(head))
    for _, name, vals, ratio in rows[:args.top]:
        cells = "".join(f"{vals[k]:11.3f}" for k in ORDER)
        flag = "drift" if ratio < 0.25 else ("mixed" if ratio < 0.6 else "AUTHOR")
        print(f"{name:22}{cells}   {ratio:5.2f} {flag}")
    print("\n  authorship = (SIGNED - HUMAN_MODERN) / (SIGNED - PRE);  <0.25 means "
          "modern humans\n  already moved there, so the feature dates code rather "
          "than attributing it.")

    mix, thin, clipped = mixture_table(pops, rows)
    if mix:
        print(f"\nclassifier-free cross-check — agent share of UNSIGNED {args.lang} "
              f"implied by\nwhere its mean sits between modern-human and agent code:")
        for name, pop, p in sorted(mix, key=lambda m: m[2]):
            print(f"  {p * 100:5.0f}%   {name} (vs {pop})")
        vals = [m[2] for m in mix]
        print(f"  median {np.median(vals) * 100:.0f}%, "
              f"interquartile {np.percentile(vals, 25) * 100:.0f}-"
              f"{np.percentile(vals, 75) * 100:.0f}% across "
              f"{len(mix)} feature/reference pairs")
        print(f"  ({thin} pairs dropped for too small a gap to divide by; "
              f"{clipped} of the {len(mix)} shown landed outside 0-100% before "
              f"clipping,\n   which is the mixture model telling you it is only "
              f"approximately right)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
