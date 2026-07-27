"""Cross-fitted classifier-to-quantifier replication pipeline."""
from __future__ import annotations

import numpy as np

from authorship import logreg
from authorship.quantify import (
    IdentificationError,
    fit_score_mixture,
    synthetic_mixture_evaluation_from_holdout,
    threshold_adjusted_share,
)


RIDGE = 3.0
FOLDS = 5


def _fit_model(
    kind: str,
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    seed: int,
):
    if kind == "logistic":
        return ("logistic", logreg.fit(x, y, weight=weights, ridge=RIDGE))
    if kind == "gradient_boosted_trees":
        try:
            from sklearn.ensemble import GradientBoostingClassifier
        except ImportError as error:  # pragma: no cover - environment dependent
            raise IdentificationError("scikit-learn is required for challenger") from error
        model = GradientBoostingClassifier(
            n_estimators=100,
            learning_rate=0.05,
            max_depth=2,
            random_state=seed,
        )
        model.fit(x, y, sample_weight=weights)
        return ("gradient_boosted_trees", model)
    raise IdentificationError(f"unknown model kind: {kind}")


def _predict(model, x: np.ndarray) -> np.ndarray:
    kind, fitted = model
    if kind == "logistic":
        return logreg.predict(fitted, x)
    return fitted.predict_proba(x)[:, 1]


def _crossfit(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    kind: str,
    seed: int,
) -> tuple[np.ndarray, int]:
    scores = np.full(len(y), np.nan)
    refits = 0
    for fold, test in enumerate(logreg.group_folds(groups, FOLDS)):
        train = np.setdiff1d(np.arange(len(y)), test)
        if not len(test):
            continue
        if len(np.unique(y[train])) != 2:
            raise IdentificationError("repository fold lacks both reference classes")
        model = _fit_model(kind, x[train], y[train], weights[train], seed + fold)
        scores[test] = _predict(model, x[test])
        refits += 1
    if np.any(~np.isfinite(scores)):
        raise IdentificationError("cross-fitting did not cover every reference record")
    return scores, refits


def run_quantification(
    reference_x,
    reference_y,
    reference_weights,
    reference_groups,
    target_x,
    target_weights,
    target_groups,
    *,
    model_kind: str = "logistic",
    seed: int = 1729,
    bins: int = 20,
) -> dict:
    x = np.asarray(reference_x, dtype=float)
    y = np.asarray(reference_y, dtype=float)
    weights = np.asarray(reference_weights, dtype=float)
    groups = np.asarray(reference_groups, dtype=object)
    target_x = np.asarray(target_x, dtype=float)
    target_weights = np.asarray(target_weights, dtype=float)
    target_groups = np.asarray(target_groups, dtype=object)
    if (
        len(x) != len(y)
        or len(x) != len(weights)
        or len(x) != len(groups)
        or len(target_x) != len(target_weights)
        or len(target_x) != len(target_groups)
    ):
        raise IdentificationError("feature, label, weight, and group vectors must align")
    overlap = set(groups.tolist()) & set(target_groups.tolist())
    if overlap:
        raise IdentificationError("reference and target repository overlap")
    if set(np.unique(y)) != {0.0, 1.0}:
        raise IdentificationError("references require agent and human labels")

    reference_scores, refits = _crossfit(
        x, y, weights, groups, model_kind, seed
    )
    full_model = _fit_model(model_kind, x, y, weights, seed + FOLDS)
    target_scores = _predict(full_model, target_x)
    refits += 1

    agent = y == 1
    human = y == 0
    mixture = fit_score_mixture(
        reference_scores[agent],
        weights[agent],
        reference_scores[human],
        weights[human],
        target_scores,
        target_weights,
        bins=bins,
    )
    threshold = logreg.best_threshold(y, reference_scores, weights)
    adjusted = threshold_adjusted_share(
        reference_scores[agent],
        weights[agent],
        reference_scores[human],
        weights[human],
        target_scores,
        target_weights,
        threshold=threshold,
    )
    auc = logreg.auc(y, reference_scores, weights)
    disagreement = abs(mixture["share"] - adjusted)
    gates = {
        "minimum_auc": auc >= 0.70,
        "plausible_mixture": mixture["plausible"],
        "maximum_estimator_disagreement": disagreement <= 0.10,
    }
    return {
        "model_kind": model_kind,
        "headline_eligible": model_kind == "logistic",
        "auc": auc,
        "mixture_share": mixture["share"],
        "threshold_adjusted_share": adjusted,
        "estimator_disagreement": disagreement,
        "mixture_total_variation": mixture["total_variation"],
        "threshold": threshold,
        "gates": gates,
        "identified": all(gates.values()),
        "model_refits": refits,
    }


def evaluate_dedicated_validation(
    reference_x,
    reference_y,
    reference_weights,
    reference_groups,
    validation_x,
    validation_y,
    validation_weights,
    validation_groups,
    *,
    shares,
    groups_per_mixture: int,
    trials: int,
    interval_replicates: int,
    seed: int,
    bins: int = 20,
) -> dict:
    """Fit only on development references and evaluate untouched repositories."""
    x = np.asarray(reference_x, dtype=float)
    y = np.asarray(reference_y, dtype=float)
    weights = np.asarray(reference_weights, dtype=float)
    groups = np.asarray(reference_groups, dtype=object)
    validation_x = np.asarray(validation_x, dtype=float)
    validation_y = np.asarray(validation_y, dtype=float)
    validation_weights = np.asarray(validation_weights, dtype=float)
    validation_groups = np.asarray(validation_groups, dtype=object)
    if (
        len(x) != len(y)
        or len(x) != len(weights)
        or len(x) != len(groups)
        or len(validation_x) != len(validation_y)
        or len(validation_x) != len(validation_weights)
        or len(validation_x) != len(validation_groups)
    ):
        raise IdentificationError("feature, label, weight, and group vectors must align")
    if set(groups.tolist()) & set(validation_groups.tolist()):
        raise IdentificationError("reference and validation repository overlap")
    if set(np.unique(y)) != {0.0, 1.0} or set(np.unique(validation_y)) != {
        0.0,
        1.0,
    }:
        raise IdentificationError(
            "development and validation each require agent and human labels"
        )
    labels_by_validation_group = {}
    for group, label in zip(validation_groups, validation_y):
        prior = labels_by_validation_group.setdefault(group, label)
        if prior != label:
            raise IdentificationError("validation repository has conflicting labels")

    reference_scores, refits = _crossfit(
        x, y, weights, groups, "logistic", seed
    )
    model = _fit_model("logistic", x, y, weights, seed + FOLDS)
    validation_scores = _predict(model, validation_x)
    refits += 1

    agent = y == 1
    human = y == 0
    agent_validation = {}
    human_validation = {}
    for name in sorted(set(validation_groups.tolist())):
        selected = validation_groups == name
        destination = (
            agent_validation
            if labels_by_validation_group[name] == 1.0
            else human_validation
        )
        destination[name] = (
            validation_scores[selected],
            validation_weights[selected],
        )
    result = synthetic_mixture_evaluation_from_holdout(
        (
            reference_scores[agent],
            weights[agent],
            groups[agent],
        ),
        (
            reference_scores[human],
            weights[human],
            groups[human],
        ),
        agent_validation,
        human_validation,
        shares=shares,
        groups_per_mixture=groups_per_mixture,
        trials=trials,
        seed=seed,
        bins=bins,
        interval_replicates=interval_replicates,
    )
    result["model_refits"] = refits
    return result


def _resample_reference(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    x_parts, y_parts, weight_parts, group_parts = [], [], [], []
    draw = 0
    for label in (0.0, 1.0):
        label_groups = np.array(sorted(set(groups[y == label].tolist())), dtype=object)
        selected = rng.choice(label_groups, size=len(label_groups), replace=True)
        for original in selected:
            indices = np.where((groups == original) & (y == label))[0]
            x_parts.append(x[indices])
            y_parts.append(y[indices])
            weight_parts.append(weights[indices])
            group_parts.append(
                np.full(len(indices), f"reference-{label}-{draw}", dtype=object)
            )
            draw += 1
    return (
        np.vstack(x_parts),
        np.concatenate(y_parts),
        np.concatenate(weight_parts),
        np.concatenate(group_parts),
    )


def _resample_target(
    x: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    names = np.array(sorted(set(groups.tolist())), dtype=object)
    selected = rng.choice(names, size=len(names), replace=True)
    x_parts, weight_parts, group_parts = [], [], []
    for draw, original in enumerate(selected):
        indices = np.where(groups == original)[0]
        x_parts.append(x[indices])
        weight_parts.append(weights[indices])
        group_parts.append(np.full(len(indices), f"target-{draw}", dtype=object))
    return np.vstack(x_parts), np.concatenate(weight_parts), np.concatenate(group_parts)


def bootstrap_pipeline(
    reference_x,
    reference_y,
    reference_weights,
    reference_groups,
    target_x,
    target_weights,
    target_groups,
    *,
    replicates: int,
    seed: int,
    bins: int = 20,
) -> dict:
    x = np.asarray(reference_x, dtype=float)
    y = np.asarray(reference_y, dtype=float)
    weights = np.asarray(reference_weights, dtype=float)
    groups = np.asarray(reference_groups, dtype=object)
    target_x = np.asarray(target_x, dtype=float)
    target_weights = np.asarray(target_weights, dtype=float)
    target_groups = np.asarray(target_groups, dtype=object)
    rng = np.random.default_rng(seed)
    shares = []
    refits = 0
    failures = []
    for replicate in range(replicates):
        sampled = _resample_reference(x, y, weights, groups, rng)
        sampled_target = _resample_target(
            target_x, target_weights, target_groups, rng
        )
        try:
            result = run_quantification(
                *sampled,
                *sampled_target,
                model_kind="logistic",
                seed=seed + replicate,
                bins=bins,
            )
        except IdentificationError as error:
            failures.append(str(error))
            continue
        shares.append(result["mixture_share"])
        refits += result["model_refits"]
    if not shares:
        raise IdentificationError("every end-to-end bootstrap replicate failed")
    return {
        "replicates": replicates,
        "successful_replicates": len(shares),
        "failed_replicates": len(failures),
        "model_refits": refits,
        "interval": [
            float(np.percentile(shares, 2.5)),
            float(np.percentile(shares, 97.5)),
        ],
        "shares": [float(share) for share in shares],
        "failure_reasons": sorted(set(failures)),
    }


def leave_one_target_group_out(
    reference_x,
    reference_y,
    reference_weights,
    reference_groups,
    target_x,
    target_weights,
    target_groups,
    *,
    baseline_share: float,
    seed: int = 1729,
    bins: int = 20,
) -> dict:
    """Measure headline sensitivity to removing each target repository."""
    target_groups = np.asarray(target_groups, dtype=object)
    target_x = np.asarray(target_x, dtype=float)
    target_weights = np.asarray(target_weights, dtype=float)
    rows = []
    for name in sorted(set(target_groups.tolist())):
        keep = target_groups != name
        if not np.any(keep):
            raise IdentificationError("leave-one-out requires multiple target groups")
        result = run_quantification(
            reference_x,
            reference_y,
            reference_weights,
            reference_groups,
            target_x[keep],
            target_weights[keep],
            target_groups[keep],
            seed=seed,
            bins=bins,
        )
        shift = abs(result["mixture_share"] - baseline_share)
        rows.append(
            {
                "omitted_group": str(name),
                "mixture_share": result["mixture_share"],
                "absolute_shift": shift,
            }
        )
    return {
        "groups_evaluated": len(rows),
        "maximum_shift": max(row["absolute_shift"] for row in rows),
        "rows": rows,
    }


def evaluate_identification(
    point: dict,
    bootstrap: dict,
    stability: dict,
    synthetic: dict,
) -> dict:
    """Apply every locked quantitative identification gate."""
    interval_width = bootstrap["interval"][1] - bootstrap["interval"][0]
    gates = {
        **point["gates"],
        "maximum_leave_one_group_out_shift": stability["maximum_shift"] <= 0.10,
        "maximum_bootstrap_interval_width": interval_width <= 0.25,
        "maximum_synthetic_mean_absolute_error": (
            synthetic["mean_absolute_error"] <= 0.10
        ),
        "minimum_synthetic_interval_coverage": (
            synthetic["interval_coverage"] >= 0.90
        ),
    }
    return {
        "identified": all(gates.values()),
        "gates": gates,
        "bootstrap_interval": bootstrap["interval"],
        "bootstrap_interval_width": interval_width,
        "maximum_leave_one_group_out_shift": stability["maximum_shift"],
        "synthetic_mean_absolute_error": synthetic["mean_absolute_error"],
        "synthetic_interval_coverage": synthetic["interval_coverage"],
    }
