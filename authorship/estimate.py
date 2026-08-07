#!/usr/bin/env python3
"""Estimate what share of modern cohort code is agent-written — with the
confounds priced in rather than waved away.

This is NOT the trailer lower bound. The trailer method counts only code whose
commit says an agent wrote it, and every match there is a true positive. This
estimates the part that method cannot see, so it necessarily has both a false
positive and a false negative rate, and its output is a range.

Method
  1. Train SIGNED (trailer-signed, 2024+) vs PRE (<2023, certainly human) with
     repo-grouped folds. Score every UNSIGNED hunk with a fold model that never
     saw its repo, so project style cannot leak in.
  2. Adjusted count: p = (observed_positive_rate - FPR) / (TPR - FPR), the
     standard correction for a noisy classifier used as a quantifier. Weighted
     by lines, because the question is about lines of code, not hunk counts.
  3. Drift control. FPR from PRE-era code understates the false positive rate on
     MODERN human code if code style simply modernized (formatters, type
     annotations). So the pre-agent years are used as a placebo: fit the
     year-by-year false positive rate across 2016..2022, extrapolate it to the
     modern era, and re-run the estimate with that larger FPR. The two FPRs
     bracket the answer.
  4. Ablation: repeat with formatting-era proxies removed, since those are the
     features most likely to be measuring "written recently" not "written by an
     agent".
  5. Bootstrap over repos (not hunks) for the sampling interval, because hunks
     within a repo are anything but independent.

Usage: python3 -m authorship.estimate [--boots 200]
"""
from __future__ import annotations

import argparse
import json
import numpy as np

from authorship import paths, data as fp_data, logreg as fp_logreg
from authorship.features import NAMES

OUT = paths.RESULTS / "estimate.json"
RIDGE, FOLDS = 3.0, 5

# features that plausibly track "written in 2025" rather than "written by an
# agent": whitespace hygiene and formatter defaults moved industry-wide.
FORMATTING_PROXIES = ("trailing_ws_rate", "tab_rate", "double_quote_pref",
                      "semicolon_rate", "brace_only_rate", "indent_mean",
                      "indent_sd", "line_len_p90", "blank_rate")


def keep_cols(drop: tuple[str, ...]) -> np.ndarray:
    return np.array([i for i, n in enumerate(NAMES) if n not in drop])


def fold_models(ls: dict, cols: np.ndarray) -> list[tuple[set, dict]]:
    """(repos_held_out, model) per fold, trained on the other folds only."""
    out = []
    for test in fp_logreg.group_folds(ls["g"], FOLDS):
        train = np.setdiff1d(np.arange(len(ls["y"])), test)
        if len(np.unique(ls["y"][train])) < 2 or not len(test):
            continue
        model = fp_logreg.fit(ls["x"][np.ix_(train, cols)], ls["y"][train],
                              weight=ls["w"][train], ridge=RIDGE)
        out.append((set(ls["g"][test].tolist()), model))
    return out


def oof_scores(ls: dict, cols: np.ndarray, models: list) -> np.ndarray:
    s = np.full(len(ls["y"]), np.nan)
    for repos, model in models:
        idx = np.array([i for i, g in enumerate(ls["g"]) if g in repos])
        if len(idx):
            s[idx] = fp_logreg.predict(model, ls["x"][np.ix_(idx, cols)])
    return s


def score_holdout(records: list[dict], cols: np.ndarray, models: list) -> np.ndarray:
    """Score records with a model that excluded their repo (falls back to the
    mean over fold models for repos that were in every training set)."""
    x, _, g = fp_data.matrix(records)
    scores = np.full(len(records), np.nan)
    for repos, model in models:
        idx = np.array([i for i, gi in enumerate(g) if gi in repos])
        if len(idx):
            scores[idx] = fp_logreg.predict(model, x[np.ix_(idx, cols)])
    missing = np.where(np.isnan(scores))[0]
    if len(missing):
        preds = np.array([fp_logreg.predict(m, x[np.ix_(missing, cols)])
                          for _, m in models])
        scores[missing] = preds.mean(axis=0)
    return scores


def adjusted_count(obs: float, tpr: float, fpr: float) -> float:
    if tpr - fpr < 0.05:
        return float("nan")
    return float(np.clip((obs - fpr) / (tpr - fpr), 0.0, 1.0))


def wrate(scores: np.ndarray, weights: np.ndarray, thr: float) -> float:
    return float(np.average(scores >= thr, weights=weights))


def drift_fpr(pre: list[dict], scores: np.ndarray, thr: float,
              target_year: float) -> tuple[float, list[tuple[int, float, int]]]:
    """Year-by-year FPR across the pre-agent era, linearly extrapolated to
    target_year. Returns (extrapolated FPR, per-year table)."""
    years = np.array([int(r["date"][:4]) for r in pre])
    w = np.array([r["lines"] for r in pre], dtype=float)
    table = []
    for y in sorted(set(years.tolist())):
        m = years == y
        if w[m].sum() < 2000:  # too little code that year to trust a rate
            continue
        table.append((int(y), wrate(scores[m], w[m], thr), int(m.sum())))
    if len(table) < 3:
        return float("nan"), table
    xs = np.array([t[0] for t in table], dtype=float)
    ys = np.array([t[1] for t in table])
    slope, intercept = np.polyfit(xs, ys, 1)
    linear = slope * target_year + intercept
    # The drift is not linear — it steps up around 2020 and then plateaus — so a
    # least-squares line through 2013..2022 lands BELOW the most recent observed
    # years. Taking the larger of the line and the recent plateau keeps this
    # honestly conservative: a bigger modern FPR yields a smaller agent estimate.
    plateau = float(np.mean([t[1] for t in table[-3:]]))
    return float(np.clip(max(linear, plateau), 0.0, 0.95)), table


def estimate(pops: dict, cols: np.ndarray, label: str) -> dict:
    ls = fp_data.label_set(pops["SIGNED"], pops["PRE"])
    models = fold_models(ls, cols)
    oof = oof_scores(ls, cols, models)
    ok = ~np.isnan(oof)
    y, s, w = ls["y"][ok], oof[ok], ls["w"][ok]
    thr = fp_logreg.best_threshold(y, s, w)
    tpr, fpr = fp_logreg.rates_at(y, s, thr, w)

    pre_scores = oof[ls["n_pos"]:]
    fpr_modern, year_table = drift_fpr(pops["PRE"], pre_scores, thr, 2025.0)

    uns = pops["UNSIGNED"]
    u_scores = score_holdout(uns, cols, models)
    u_w = np.array([r["lines"] for r in uns], dtype=float)
    obs = wrate(u_scores, u_w, thr)

    return {
        "label": label, "threshold": thr, "tpr": tpr,
        "fpr_pre_era": fpr, "fpr_modern_extrapolated": fpr_modern,
        "auc_lines": fp_logreg.auc(y, s, w),
        "observed_positive_rate_unsigned": obs,
        "p_unsigned_high": adjusted_count(obs, tpr, fpr),
        "p_unsigned_low": adjusted_count(obs, tpr, fpr_modern),
        "fpr_by_year": year_table,
        "unsigned_scores": u_scores, "unsigned_lines": u_w,
        "unsigned_repos": np.array([r["repo"] for r in uns], dtype=object),
    }


def total_share(p_unsigned: float, signed_lines: float, unsigned_lines: float) -> float:
    modern = signed_lines + unsigned_lines
    return (signed_lines + p_unsigned * unsigned_lines) / modern if modern else float("nan")


def repo_created() -> dict[str, str]:
    ages = json.loads(paths.COHORT_AGES.read_text())
    return {r["full_name"]: r["created"][:10] for r in ages}


def by_group(est: dict, pops: dict) -> list[dict]:
    """Split the estimate by cohort group: repos born in the agent era vs
    established codebases agents arrived into."""
    created = repo_created()
    signed_lines: dict[str, float] = {}
    for r in pops["SIGNED"]:
        signed_lines[r["repo"]] = signed_lines.get(r["repo"], 0.0) + r["lines"]
    groups = {"agent-era repos (created 2024+)": [], "established repos (pre-2024)": []}
    for repo in sorted(set(est["unsigned_repos"].tolist())):
        born = created.get(repo, "2024-01-01")
        key = ("agent-era repos (created 2024+)" if born >= "2024-01-01"
               else "established repos (pre-2024)")
        groups[key].append(repo)
    out = []
    for name, repos in groups.items():
        idx = np.concatenate([np.where(est["unsigned_repos"] == r)[0] for r in repos]) \
            if repos else np.array([], dtype=int)
        if not len(idx):
            continue
        s, w = est["unsigned_scores"][idx], est["unsigned_lines"][idx]
        obs = wrate(s, w, est["threshold"])
        sl = float(sum(signed_lines.get(r, 0.0) for r in repos))
        lo = total_share(adjusted_count(obs, est["tpr"],
                                       est["fpr_modern_extrapolated"]), sl, float(w.sum()))
        hi = total_share(adjusted_count(obs, est["tpr"], est["fpr_pre_era"]),
                         sl, float(w.sum()))
        out.append({"group": name, "repos": len(repos),
                    "lines_signed": sl, "lines_unsigned": float(w.sum()),
                    "trailer_visible": sl / (sl + float(w.sum())),
                    "share_range": [lo, hi]})
    return out


def per_repo(est: dict, pops: dict, min_lines: float = 3000.0) -> list[dict]:
    signed_lines: dict[str, float] = {}
    for r in pops["SIGNED"]:
        signed_lines[r["repo"]] = signed_lines.get(r["repo"], 0.0) + r["lines"]
    rows = []
    for repo in sorted(set(est["unsigned_repos"].tolist())):
        idx = np.where(est["unsigned_repos"] == repo)[0]
        w = est["unsigned_lines"][idx]
        if w.sum() < min_lines:
            continue
        obs = wrate(est["unsigned_scores"][idx], w, est["threshold"])
        sl = signed_lines.get(repo, 0.0)
        lo = total_share(adjusted_count(obs, est["tpr"],
                                       est["fpr_modern_extrapolated"]), sl, float(w.sum()))
        hi = total_share(adjusted_count(obs, est["tpr"], est["fpr_pre_era"]),
                         sl, float(w.sum()))
        rows.append({"repo": repo, "lines": float(sl + w.sum()),
                     "trailer_visible": sl / (sl + float(w.sum())),
                     "observed_positive_rate": obs,
                     # a single repo's adjusted share saturates whenever its
                     # observed rate exceeds the TPR, so these are ordering
                     # information, not per-repo percentages to quote
                     "saturated": bool(obs >= est["tpr"]),
                     "share_range": [lo, hi]})
    return sorted(rows, key=lambda r: -r["observed_positive_rate"])


def bootstrap(est: dict, signed: list[dict], boots: int, seed: int = 7) -> dict:
    """Resample repos with replacement; recompute both ends of the range."""
    rng = np.random.default_rng(seed)
    repos = sorted(set(est["unsigned_repos"].tolist()))
    by_repo = {r: np.where(est["unsigned_repos"] == r)[0] for r in repos}
    signed_by_repo: dict[str, float] = {}
    for r in signed:
        signed_by_repo[r["repo"]] = signed_by_repo.get(r["repo"], 0.0) + r["lines"]
    lows, highs = [], []
    for _ in range(boots):
        pick = rng.choice(len(repos), size=len(repos), replace=True)
        idx = np.concatenate([by_repo[repos[i]] for i in pick])
        s, w = est["unsigned_scores"][idx], est["unsigned_lines"][idx]
        obs = wrate(s, w, est["threshold"])
        sl = float(sum(signed_by_repo.get(repos[i], 0.0) for i in pick))
        ul = float(w.sum())
        for target, out in ((est["fpr_modern_extrapolated"], lows),
                            (est["fpr_pre_era"], highs)):
            p = adjusted_count(obs, est["tpr"], target)
            out.append(total_share(p, sl, ul))
    return {"low_ci": [float(np.nanpercentile(lows, 2.5)),
                       float(np.nanpercentile(lows, 97.5))],
            "high_ci": [float(np.nanpercentile(highs, 2.5)),
                        float(np.nanpercentile(highs, 97.5))]}


def zero_trailer_control(est: dict, pops: dict) -> dict:
    """Diagnostic: modern code in repos where the sample found NO agent-signed
    commit at all. Not a clean human set — a project can use agents without
    trailers — but if the classifier fired at pre-agent rates here while firing
    hard elsewhere, the drift correction would be doing its job, and if it fires
    everywhere equally the signal is suspect. Reported, never used in the fit."""
    signed_repos = {r["repo"] for r in pops["SIGNED"]}
    repos = [r for r in sorted(set(est["unsigned_repos"].tolist()))
             if r not in signed_repos]
    if not repos:
        return {}
    idx = np.concatenate([np.where(est["unsigned_repos"] == r)[0] for r in repos])
    w = est["unsigned_lines"][idx]
    return {"repos": len(repos), "lines": float(w.sum()),
            "observed_positive_rate": wrate(est["unsigned_scores"][idx], w,
                                            est["threshold"])}


def report(est: dict, pops: dict, boots: dict | None) -> dict:
    signed_lines = float(sum(r["lines"] for r in pops["SIGNED"]))
    unsigned_lines = float(est["unsigned_lines"].sum())
    pre_lines = float(sum(r["lines"] for r in pops["PRE"]))
    lo = total_share(est["p_unsigned_low"], signed_lines, unsigned_lines)
    hi = total_share(est["p_unsigned_high"], signed_lines, unsigned_lines)
    trailer_only = signed_lines / (signed_lines + unsigned_lines)

    print(f"\n{est['label']}")
    print(f"  line-weighted AUC (SIGNED vs PRE, repo-held-out) {est['auc_lines']:.3f}")
    print(f"  threshold {est['threshold']:.3f}   TPR {est['tpr']:.3f}")
    print(f"  FPR on pre-agent code            {est['fpr_pre_era']:.3f}")
    print(f"  FPR extrapolated to 2025 code    {est['fpr_modern_extrapolated']:.3f}"
          "   (drift placebo)")
    print(f"  UNSIGNED lines scored agent-like {est['observed_positive_rate_unsigned']:.3f}")
    print(f"  -> agent share OF UNSIGNED lines {est['p_unsigned_low']:.3f} .. "
          f"{est['p_unsigned_high']:.3f}")
    print(f"  -> agent share of ALL 2024+ lines {lo:.3f} .. {hi:.3f}   "
          f"(trailer-visible alone: {trailer_only:.3f})")
    if boots:
        print(f"     bootstrap 95% over repos: low end {boots['low_ci'][0]:.3f}-"
              f"{boots['low_ci'][1]:.3f}, high end {boots['high_ci'][0]:.3f}-"
              f"{boots['high_ci'][1]:.3f}")
    print("  false positive rate by pre-agent year (the drift being controlled):")
    for y, r, n in est["fpr_by_year"]:
        print(f"     {y}  {r:.3f}  (n={n})")
    ctrl = zero_trailer_control(est, pops)
    if ctrl:
        print(f"  control — modern code in the {ctrl['repos']} repos with no "
              f"agent-signed commit in the sample:\n"
              f"     {ctrl['observed_positive_rate']:.3f} scored agent-like "
              f"({ctrl['lines']:,.0f} lines)")
    return {
        "zero_trailer_control": ctrl,
        "label": est["label"], "threshold": est["threshold"], "tpr": est["tpr"],
        "fpr_pre_era": est["fpr_pre_era"],
        "fpr_modern_extrapolated": est["fpr_modern_extrapolated"],
        "auc_lines": est["auc_lines"],
        "observed_positive_rate_unsigned": est["observed_positive_rate_unsigned"],
        "p_unsigned_range": [est["p_unsigned_low"], est["p_unsigned_high"]],
        "share_2024plus_range": [lo, hi],
        "trailer_visible_share": trailer_only,
        "lines": {"signed": signed_lines, "unsigned": unsigned_lines, "pre": pre_lines},
        "fpr_by_year": est["fpr_by_year"],
        "bootstrap": boots,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boots", type=int, default=200)
    args = ap.parse_args()

    cohort, own = fp_data.load()
    pops = fp_data.populations(cohort, own)
    print("corpus: " + ", ".join(f"{k} {len(v):,} hunks" for k, v in pops.items()))

    runs = []
    full = estimate(pops, keep_cols(()), "all features")
    runs.append(report(full, pops, bootstrap(full, pops["SIGNED"], args.boots)))
    ablated = estimate(pops, keep_cols(FORMATTING_PROXIES),
                       "formatting-era proxies removed")
    runs.append(report(ablated, pops, bootstrap(ablated, pops["SIGNED"], args.boots)))

    groups = by_group(ablated, pops)
    print("\nby cohort group (formatting-neutral specification)")
    for g in groups:
        print(f"  {g['group']:34} {g['repos']:3} repos  "
              f"estimate {g['share_range'][0] * 100:4.1f}%-{g['share_range'][1] * 100:4.1f}%"
              f"   (trailer-visible {g['trailer_visible'] * 100:4.1f}%)")
    repos = per_repo(ablated, pops)
    sat = sum(1 for r in repos if r["saturated"])
    print(f"\nmost agent-like repos by observed rate ({len(repos)} repos with "
          f">=3k sampled lines; {sat} saturate the adjusted estimator, so only "
          f"the raw rate is shown)")
    for r in repos[:12]:
        print(f"  {r['observed_positive_rate'] * 100:5.1f}% of lines scored "
              f"agent-like  (trailer-visible {r['trailer_visible'] * 100:4.1f}%)  "
              f"{r['repo']}")
    runs[1]["by_group"] = groups
    runs[1]["per_repo"] = repos

    spans = [r["share_2024plus_range"] for r in runs]
    lo = min(s[0] for s in spans)
    hi = max(s[1] for s in spans)
    print(f"\n  ESTIMATE across specifications: {lo * 100:.0f}%-{hi * 100:.0f}% of "
          f"2024+ surviving cohort lines are agent-written")
    print(f"  (trailer-visible lower bound in the same sample: "
          f"{runs[0]['trailer_visible_share'] * 100:.1f}%)")

    OUT.write_text(json.dumps({"runs": runs, "envelope_2024plus": [lo, hi],
                               "dropped_in_ablation": list(FORMATTING_PROXIES)},
                              indent=2) + "\n")
    print(f"\n  -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
