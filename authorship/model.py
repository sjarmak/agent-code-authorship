#!/usr/bin/env python3
"""Fit and honestly validate the agent-written-code fingerprint.

For each label set (see data.py): repo-grouped 5-fold CV so no repo is ever in
both train and test — the failure mode that would otherwise let the model
memorize a project's house style and report it as authorship detection.

Reports per label set:
  AUC (hunk-level and line-weighted), out-of-fold
  TPR/FPR at the Youden-optimal out-of-fold threshold  (the inputs the
  prevalence estimator needs)
  the strongest standardized coefficients, excluding language/size controls

Plus one genuinely external check: the L2 model (trained only on cohort
trailer-signed vs pre-2023 code) is scored against the maintainer's own repos,
which it never saw. If the fingerprint is real, OWN should score high.

Writes results/model.json (coefficients + metrics) for estimate.py.
Usage: python3 -m authorship.model
"""
from __future__ import annotations

import json
import numpy as np

from authorship import paths, data as fp_data, logreg as fp_logreg
from authorship.features import CONTROL_NAMES, NAMES

OUT = paths.RESULTS / "model.json"
RIDGE = 3.0
FOLDS = 5


def cross_val(ls: dict) -> dict:
    """Out-of-fold scores under repo-grouped CV."""
    oof = np.full(len(ls["y"]), np.nan)
    for test in fp_logreg.group_folds(ls["g"], FOLDS):
        train = np.setdiff1d(np.arange(len(ls["y"])), test)
        if len(np.unique(ls["y"][train])) < 2 or not len(test):
            continue
        model = fp_logreg.fit(ls["x"][train], ls["y"][train],
                              weight=ls["w"][train], ridge=RIDGE)
        oof[test] = fp_logreg.predict(model, ls["x"][test])
    ok = ~np.isnan(oof)
    y, s, w = ls["y"][ok], oof[ok], ls["w"][ok]
    thr = fp_logreg.best_threshold(y, s, w)
    tpr, fpr = fp_logreg.rates_at(y, s, thr, w)
    return {
        "auc_hunk": fp_logreg.auc(y, s),
        "auc_lines": fp_logreg.auc(y, s, w),
        "threshold": thr, "tpr": tpr, "fpr": fpr,
        "covered": int(ok.sum()), "oof": oof,
    }


def top_weights(model: dict, k: int = 10) -> list[tuple[str, float]]:
    beta = model["beta"][1:]
    pairs = [(n, float(b)) for n, b in zip(NAMES, beta) if n not in CONTROL_NAMES]
    return sorted(pairs, key=lambda p: -abs(p[1]))[:k]


def describe(name: str, ls: dict, cv: dict, model: dict) -> None:
    print(f"\n{name}")
    print(f"  {ls['n_pos']:,} positive hunks ({ls['lines_pos']:,.0f} lines) vs "
          f"{ls['n_neg']:,} negative ({ls['lines_neg']:,.0f} lines), "
          f"{len(set(ls['g'].tolist()))} repos")
    print(f"  out-of-fold AUC  {cv['auc_hunk']:.3f} per hunk, "
          f"{cv['auc_lines']:.3f} line-weighted")
    print(f"  at threshold {cv['threshold']:.3f}:  TPR {cv['tpr']:.3f}   "
          f"FPR {cv['fpr']:.3f}")
    print("  strongest signals (standardized, + = agent-like):")
    for n, b in top_weights(model):
        print(f"    {b:+.3f}  {n}")


def external_check(model: dict, pops: dict) -> dict:
    """Score every population with one model. OWN is fully held out for L2."""
    out = {}
    for pop in ("SIGNED", "UNSIGNED", "PRE", "OWN"):
        if not pops[pop]:
            continue
        x, w, _ = fp_data.matrix(pops[pop])
        s = fp_logreg.predict(model, x)
        out[pop] = {"mean": float(np.average(s, weights=w)),
                    "median": float(np.median(s)), "n": len(s)}
    return out


def main() -> int:
    cohort, own = fp_data.load()
    if not cohort:
        raise SystemExit("no cohort corpus — run python3 -m authorship.corpus.cohort")
    pops = fp_data.populations(cohort, own)
    print("corpus: " + ", ".join(
        f"{k} {len(v):,} hunks/{sum(r['lines'] for r in v):,} lines"
        for k, v in pops.items()))

    sets = fp_data.build(pops)
    saved = {"ridge": RIDGE, "folds": FOLDS, "feature_names": list(NAMES),
             "populations": {k: {"hunks": len(v),
                                 "lines": sum(r["lines"] for r in v)}
                             for k, v in pops.items()},
             "sets": {}}
    for name, ls in sets.items():
        cv = cross_val(ls)
        full = fp_logreg.fit(ls["x"], ls["y"], weight=ls["w"], ridge=RIDGE)
        describe(name, ls, cv, full)
        saved["sets"][name] = {
            "n_pos": ls["n_pos"], "n_neg": ls["n_neg"],
            "lines_pos": ls["lines_pos"], "lines_neg": ls["lines_neg"],
            "auc_hunk": cv["auc_hunk"], "auc_lines": cv["auc_lines"],
            "threshold": cv["threshold"], "tpr": cv["tpr"], "fpr": cv["fpr"],
            "beta": [float(b) for b in full["beta"]],
            "std_mu": [float(m) for m in full["std"]["mu"]],
            "std_sd": [float(s) for s in full["std"]["sd"]],
            "top_weights": top_weights(full, 14),
        }

    if "L2_era" in sets and pops["OWN"]:
        ls = sets["L2_era"]
        l2 = fp_logreg.fit(ls["x"], ls["y"], weight=ls["w"], ridge=RIDGE)
        ext = external_check(l2, pops)
        saved["external_check_L2"] = ext
        print("\nexternal check — L2 model scored on populations it was not "
              "trained to separate\n  (line-weighted mean agent-likeness)")
        for pop, v in ext.items():
            print(f"    {pop:9} {v['mean']:.3f}  (n={v['n']:,})")

    OUT.write_text(json.dumps(saved, indent=2) + "\n")
    print(f"\n  -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
