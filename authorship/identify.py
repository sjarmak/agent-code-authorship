#!/usr/bin/env python3
"""The identified estimate: agent-written share measured against MODERN human
code, per language.

estimate.py compares agent code to PRE-2023 code, so its separation is part
authorship and part "written recently". This module removes the era confound by
using the control corpus's negatives — post-2024 code from projects that ban AI
contributions — and removes the language confound by fitting and estimating
WITHIN each language, never across them.

Per language L (needing enough lines on both sides, in several repos each):
    fit    positives vs HUMAN_MODERN_L, repo-grouped folds
    measure TPR and FPR out-of-fold — FPR now comes from real modern human code
    apply  the fold models to UNSIGNED_L, each hunk scored by a model that never
           trained on its repo
    p_L    = (observed - FPR) / (TPR - FPR), clipped to [0,1]

Two positive definitions run independently, because each has a different flaw:
    SIGNED  trailer-signed cohort code — real-world agent code, but only the
            fraction whose committer left the trailer on
    OWN     the maintainer's own repos — confirmed agent-written, but one
            author's projects

Aggregation is line-weighted across languages. Bootstrap resamples repos.

Usage: python3 -m authorship.identify [--boots 200] [--min-lines-per-lang 4000]
"""
from __future__ import annotations

import argparse
import json
import numpy as np

from authorship import paths, data as fp_data, logreg as fp_logreg
from authorship.estimate import (
    adjusted_count,
    fold_models,
    oof_scores,
    score_holdout,
    wrate,
)
from authorship.features import NAMES

OUT = paths.RESULTS / "identified.json"
# language indicators are meaningless inside a single-language fit, and
# log_lines stays: hunk size is a genuine style difference, not a confound
LANG_COLS = ("is_python", "is_ts_js", "is_go", "is_curly_other")


def cols_without(drop: tuple[str, ...]) -> np.ndarray:
    return np.array([i for i, n in enumerate(NAMES) if n not in drop])


# TypeScript and JavaScript are pooled: same syntax family, same formatters, and
# the control side has TS in one project and JS in two, too thin apart. Keeping
# them separate would drop 38% of the cohort's unsigned lines from coverage.
LANG_MERGE = {"TypeScript": "TS/JS", "JavaScript": "TS/JS"}


def with_merged_langs(records: list[dict]) -> list[dict]:
    return [{**r, "lang": LANG_MERGE.get(r["lang"], r["lang"])} for r in records]


def of_lang(records: list[dict], lang: str) -> list[dict]:
    return [r for r in records if r["lang"] == lang]


def lines_of(records: list[dict]) -> float:
    return float(sum(r["lines"] for r in records))


def substantial_repos(records: list[dict], floor: float = 1000.0) -> int:
    """Repos contributing more than `floor` lines. Three repos where one holds
    97% of the lines is one repo with decoration, and grouped CV on it produces
    folds with no usable negatives."""
    per: dict[str, float] = {}
    for r in records:
        per[r["repo"]] = per.get(r["repo"], 0.0) + r["lines"]
    return sum(1 for v in per.values() if v >= floor)


# A fit has to clear these to be allowed to contribute a number. Failing them is
# reported as "not identifiable", which is a result, not an error to paper over.
MIN_AUC = 0.60
MIN_SEPARATION = 0.15   # TPR - FPR at the operating threshold
MAX_FPR = 0.80          # above this the threshold has collapsed to "all positive"
MIN_TPR = 0.20


def gate(fit: dict) -> str | None:
    if not np.isfinite(fit["auc"]) or fit["auc"] < MIN_AUC:
        return f"AUC {fit['auc']:.3f} < {MIN_AUC}"
    if fit["fpr"] > MAX_FPR or fit["tpr"] < MIN_TPR:
        return f"degenerate threshold (TPR {fit['tpr']:.2f}, FPR {fit['fpr']:.2f})"
    if fit["tpr"] - fit["fpr"] < MIN_SEPARATION:
        return f"separation {fit['tpr'] - fit['fpr']:.2f} < {MIN_SEPARATION}"
    if not np.isfinite(fit["p_unsigned"]):
        return "estimator undefined"
    return None


def language_fit(pos: list[dict], neg: list[dict], uns: list[dict],
                 cols: np.ndarray, pre: list[dict] | None = None) -> dict | None:
    ls = fp_data.label_set(pos, neg)
    models = fold_models(ls, cols)
    if len(models) < 2:
        return None
    oof = oof_scores(ls, cols, models)
    ok = ~np.isnan(oof)
    if len(np.unique(ls["y"][ok])) < 2:
        return None
    y, s, w = ls["y"][ok], oof[ok], ls["w"][ok]
    thr = fp_logreg.best_threshold(y, s, w)
    tpr, fpr = fp_logreg.rates_at(y, s, thr, w)
    u_scores = score_holdout(uns, cols, models)
    u_w = np.array([r["lines"] for r in uns], dtype=float)
    obs = wrate(u_scores, u_w, thr)

    # Second false positive rate, from the cohort's OWN pre-2023 code. Those are
    # the same repos as the unsigned population, so this reference absorbs the
    # cohort's house style — but it is old code, so it also absorbs era drift and
    # therefore overstates the FPR. Using the larger of the two FPRs gives the
    # conservative estimate: whichever way the bias runs, the answer is bounded.
    fpr_cohort_pre = float("nan")
    if pre:
        p_scores = score_holdout(pre, cols, models)
        p_w = np.array([r["lines"] for r in pre], dtype=float)
        fpr_cohort_pre = wrate(p_scores, p_w, thr)
    fpr_conservative = float(np.nanmax([fpr, fpr_cohort_pre]))
    # Placebo: hand the estimator pre-2023 cohort code as if its authorship were
    # unknown. The true answer is 0 — agents did not exist — so whatever comes
    # back is this specification's error floor on cohort code.
    placebo = adjusted_count(fpr_cohort_pre, tpr, fpr) if pre else float("nan")
    return {
        "placebo_pre2023_as_unknown": placebo,
        "auc": fp_logreg.auc(y, s, w), "threshold": thr, "tpr": tpr, "fpr": fpr,
        "fpr_cohort_pre": fpr_cohort_pre, "fpr_conservative": fpr_conservative,
        "observed": obs, "p_unsigned": adjusted_count(obs, tpr, fpr),
        "p_unsigned_conservative": adjusted_count(obs, tpr, fpr_conservative),
        "models": models, "unsigned_scores": u_scores, "unsigned_lines": u_w,
        "unsigned_repos": np.array([r["repo"] for r in uns], dtype=object),
        "n_pos_lines": lines_of(pos), "n_neg_lines": lines_of(neg),
        "pos_repos": len({r["repo"] for r in pos}),
        "neg_repos": len({r["repo"] for r in neg}),
    }


def run(pops: dict, pos_key: str, min_lines: float, min_repos: int) -> dict:
    cols = cols_without(LANG_COLS)
    langs = sorted({r["lang"] for r in pops["HUMAN_MODERN"]}
                   & {r["lang"] for r in pops[pos_key]}
                   & {r["lang"] for r in pops["UNSIGNED"]})
    per_lang, skipped = {}, []
    for lang in langs:
        pos, neg = of_lang(pops[pos_key], lang), of_lang(pops["HUMAN_MODERN"], lang)
        uns = of_lang(pops["UNSIGNED"], lang)
        if min(lines_of(pos), lines_of(neg)) < min_lines or not uns:
            skipped.append(f"{lang}: too little labeled code "
                           f"(agent {lines_of(pos):,.0f} / human {lines_of(neg):,.0f} lines)")
            continue
        n_pos_repos, n_neg_repos = substantial_repos(pos), substantial_repos(neg)
        if min(n_pos_repos, n_neg_repos) < min_repos:
            skipped.append(f"{lang}: needs {min_repos} substantial repos per side, "
                           f"has {n_pos_repos} agent / {n_neg_repos} human")
            continue
        fit = language_fit(pos, neg, uns, cols, of_lang(pops["PRE"], lang))
        if not fit:
            skipped.append(f"{lang}: fit failed")
            continue
        why = gate(fit)
        if why:
            skipped.append(f"{lang}: not identifiable — {why}")
            continue
        fit["signed_lines"] = lines_of(of_lang(pops["SIGNED"], lang))
        fit["unsigned_line_total"] = lines_of(uns)
        per_lang[lang] = fit
    return {"positives": pos_key, "per_lang": per_lang, "skipped": skipped}


def aggregate(res: dict, pops: dict) -> dict:
    """Line-weighted share of 2024+ cohort lines, over IDENTIFIABLE languages
    only. Coverage says how much of the cohort that actually is — an estimate
    over a third of the code is not an estimate over the code."""
    signed = sum(f["signed_lines"] for f in res["per_lang"].values())
    unsigned = sum(f["unsigned_line_total"] for f in res["per_lang"].values())
    agent = signed + sum(f["p_unsigned"] * f["unsigned_line_total"]
                         for f in res["per_lang"].values())
    agent_cons = signed + sum((f["p_unsigned_conservative"]
                               if np.isfinite(f["p_unsigned_conservative"]) else 0.0)
                              * f["unsigned_line_total"]
                              for f in res["per_lang"].values())
    all_modern = lines_of(pops["SIGNED"]) + lines_of(pops["UNSIGNED"])
    return {"lines_signed": signed, "lines_unsigned": unsigned,
            "share_2024plus_conservative": agent_cons / (signed + unsigned)
            if signed + unsigned else float("nan"),
            "share_2024plus": agent / (signed + unsigned) if signed + unsigned else float("nan"),
            "trailer_visible": signed / (signed + unsigned) if signed + unsigned else float("nan"),
            "coverage_of_cohort_modern_lines": (signed + unsigned) / all_modern
            if all_modern else float("nan")}


def bootstrap(res: dict, boots: int, seed: int = 11) -> list[float]:
    """Resample repos inside each language, recompute the aggregate share."""
    rng = np.random.default_rng(seed)
    shares = []
    for _ in range(boots):
        signed = unsigned = agent = 0.0
        for lang, f in res["per_lang"].items():
            repos = sorted(set(f["unsigned_repos"].tolist()))
            by = {r: np.where(f["unsigned_repos"] == r)[0] for r in repos}
            pick = rng.choice(len(repos), size=len(repos), replace=True)
            idx = np.concatenate([by[repos[i]] for i in pick])
            s, w = f["unsigned_scores"][idx], f["unsigned_lines"][idx]
            obs = wrate(s, w, f["threshold"])
            p = adjusted_count(obs, f["tpr"], f["fpr"])
            u = float(w.sum())
            signed += f["signed_lines"]
            unsigned += u
            agent += f["signed_lines"] + (p if np.isfinite(p) else 0.0) * u
        if signed + unsigned:
            shares.append(agent / (signed + unsigned))
    return shares


def cross_check(pops: dict, res: dict) -> dict:
    """Score OWN (confirmed agent, modern) with the SIGNED-vs-HUMAN_MODERN
    models, and HUMAN_MODERN's own out-of-fold rate, for reference. A model that
    is really detecting authorship should rank OWN like SIGNED, not like the
    control."""
    out = {}
    cols = cols_without(LANG_COLS)
    for lang, f in res["per_lang"].items():
        for pop in ("OWN", "PRE"):
            recs = of_lang(pops[pop], lang)
            if lines_of(recs) < 2000:
                continue
            s = score_holdout(recs, cols, f["models"])
            w = np.array([r["lines"] for r in recs], dtype=float)
            out.setdefault(pop, {})[lang] = wrate(s, w, f["threshold"])
    return out


def report(res: dict, agg: dict, ci: list[float], label: str) -> dict:
    print(f"\n=== positives: {label} ===")
    print(f"{'lang':9} {'AUC':>5} {'TPR':>5} {'FPR':>5} {'FPRpre':>7} {'obs':>5} "
          f"{'p(uns)':>7} {'p cons':>7} {'agent/human lines':>19}  repos")
    for lang, f in sorted(res["per_lang"].items(),
                          key=lambda kv: -kv[1]["unsigned_line_total"]):
        print(f"{lang:9} {f['auc']:5.3f} {f['tpr']:5.3f} {f['fpr']:5.3f} "
              f"{f['fpr_cohort_pre']:7.3f} {f['observed']:5.3f} "
              f"{f['p_unsigned']:7.3f} {f['p_unsigned_conservative']:7.3f} "
              f"{f['n_pos_lines']:9,.0f}/{f['n_neg_lines']:8,.0f}  "
              f"{f['pos_repos']}/{f['neg_repos']}")
    for s in res["skipped"]:
        print(f"  excluded  {s}")
    lo, hi = (float(np.percentile(ci, 2.5)), float(np.percentile(ci, 97.5))) \
        if ci else (float("nan"), float("nan"))
    print(f"  agent share of 2024+ lines, identifiable languages only "
          f"({agg['coverage_of_cohort_modern_lines'] * 100:.0f}% of cohort modern "
          f"lines):\n     {agg['share_2024plus_conservative'] * 100:.1f}% using the "
          f"cohort's own pre-2023 code as the human reference (conservative)"
          f"\n     {agg['share_2024plus'] * 100:.1f}% using the AI-banning projects "
          f"(95% CI over repos {lo * 100:.1f}-{hi * 100:.1f}%)")
    print(f"  trailer-visible in the same lines: {agg['trailer_visible'] * 100:.1f}%")
    for lang, f in res["per_lang"].items():
        if np.isfinite(f["placebo_pre2023_as_unknown"]):
            print(f"  placebo — pre-2023 {lang} scored as if unknown: "
                  f"{f['placebo_pre2023_as_unknown'] * 100:.0f}% "
                  f"(true answer 0%; this is the error floor the conservative "
                  f"column removes)")
    return {"label": label, "aggregate": agg, "ci95": [lo, hi],
            "per_lang": {k: {kk: vv for kk, vv in v.items()
                             if kk not in ("models", "unsigned_scores",
                                           "unsigned_lines", "unsigned_repos")}
                         for k, v in res["per_lang"].items()},
            "skipped": res["skipped"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--boots", type=int, default=200)
    ap.add_argument("--min-lines-per-lang", type=float, default=4000)
    ap.add_argument("--min-repos", type=int, default=3,
                    help="repos required per side, so grouped CV is meaningful")
    args = ap.parse_args()

    cohort, own = fp_data.load()
    control = fp_data.load_control()
    if not control:
        raise SystemExit("no control corpus — run python3 -m authorship.corpus.control")
    pops = {k: with_merged_langs(v) for k, v in
            fp_data.populations(cohort, own, control).items()}
    print("corpus: " + ", ".join(f"{k} {len(v):,} hunks/{lines_of(v):,.0f} lines"
                                for k, v in pops.items()))

    saved = {"runs": []}
    for pos_key, label in (("SIGNED", "trailer-signed cohort code"),
                           ("OWN", "maintainer's own repos")):
        res = run(pops, pos_key, args.min_lines_per_lang, args.min_repos)
        if not res["per_lang"]:
            print(f"\n  no language met the coverage bar for {label}")
            continue
        agg = aggregate(res, pops)
        saved["runs"].append(report(res, agg, bootstrap(res, args.boots), label))
        if pos_key == "SIGNED":
            xc = cross_check(pops, res)
            saved["cross_check"] = xc
            print("  cross-check — rate scored agent-like by these same models:")
            for pop, per in xc.items():
                cells = "  ".join(
                    f"{language} {value:.3f}"
                    for language, value in sorted(per.items())
                )
                print(f"    {pop:4} {cells}")

    if len(saved["runs"]) == 2:
        points = [r["aggregate"][k] for r in saved["runs"]
                  for k in ("share_2024plus", "share_2024plus_conservative")]
        cis = [c for r in saved["runs"] for c in r["ci95"]]
        lo, hi = min(points + cis), max(points + cis)
        saved["envelope"] = [lo, hi]
        saved["point_range"] = [min(points), max(points)]
        print(f"\n  IDENTIFIED ESTIMATE: {min(points) * 100:.0f}%-"
              f"{max(points) * 100:.0f}% of 2024+ cohort lines are agent-written, "
              f"across four specifications\n  (two definitions of agent code x two "
              f"human references)")
        print(f"  widened by repo resampling: {lo * 100:.0f}%-{hi * 100:.0f}%")
        print(f"  the trailer method sees {saved['runs'][0]['aggregate']['trailer_visible'] * 100:.1f}% "
              f"of the same lines")

    OUT.write_text(json.dumps(saved, indent=2) + "\n")
    print(f"\n  -> {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
