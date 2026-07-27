"""Pre-2023 diagnostics kept structurally separate from authorship labels."""
from __future__ import annotations

import numpy as np

from authorship import logreg
from authorship.quantify import IdentificationError, fit_score_mixture


class TemporalDiagnosticError(ValueError):
    """Raised when era diagnostics or temporal roles are not auditable."""


def _arrays(x, y, weights, groups):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    weights = np.asarray(weights, dtype=float)
    groups = np.asarray(groups, dtype=object)
    if (
        x.ndim != 2
        or y.ndim != 1
        or weights.ndim != 1
        or groups.ndim != 1
        or len(x) != len(y)
        or len(x) != len(weights)
        or len(x) != len(groups)
        or not len(x)
        or np.any(~np.isfinite(x))
        or np.any(~np.isfinite(weights))
        or np.any(weights <= 0)
    ):
        raise TemporalDiagnosticError("diagnostic arrays must align and be finite")
    return x, y, weights, groups


def _weighted_mean_variance(
    values: np.ndarray, weights: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    mean = np.average(values, axis=0, weights=weights)
    variance = np.average((values - mean) ** 2, axis=0, weights=weights)
    return mean, variance


def era_diagnostics(
    x,
    era,
    weights,
    groups,
    feature_names,
    *,
    folds: int = 5,
    ridge: float = 3.0,
) -> dict:
    """Measure how readily frozen features distinguish historical from modern."""
    x, era, weights, groups = _arrays(x, era, weights, groups)
    if set(np.unique(era)) != {0.0, 1.0}:
        raise TemporalDiagnosticError("era labels must contain historical and modern")
    if len(feature_names) != x.shape[1]:
        raise TemporalDiagnosticError("feature names must align with columns")
    for label in (0.0, 1.0):
        if len(set(groups[era == label].tolist())) < 5:
            raise TemporalDiagnosticError(
                "era diagnostic requires five repository groups per period"
            )
    scores = np.full(len(era), np.nan)
    fold_rows = []
    all_indices = np.arange(len(era))
    for fold, test in enumerate(logreg.group_folds(groups, folds)):
        if not len(test):
            continue
        train = np.setdiff1d(all_indices, test)
        if set(np.unique(era[train])) != {0.0, 1.0}:
            raise TemporalDiagnosticError("era fold lacks both periods")
        model = logreg.fit(
            x[train], era[train], weight=weights[train], ridge=ridge
        )
        scores[test] = logreg.predict(model, x[test])
        fold_rows.append(
            {
                "fold": fold,
                "train_groups": sorted(set(groups[train].tolist())),
                "test_groups": sorted(set(groups[test].tolist())),
            }
        )
    if np.any(~np.isfinite(scores)):
        raise TemporalDiagnosticError("era cross-fitting did not cover all records")
    historical = era == 0
    contemporary = era == 1
    old_mean, old_variance = _weighted_mean_variance(
        x[historical], weights[historical]
    )
    new_mean, new_variance = _weighted_mean_variance(
        x[contemporary], weights[contemporary]
    )
    pooled_sd = np.sqrt((old_variance + new_variance) / 2)
    standardized = (new_mean - old_mean) / np.maximum(pooled_sd, 1e-12)
    shifts = [
        {
            "feature": str(name),
            "historical_mean": float(old_mean[index]),
            "contemporary_mean": float(new_mean[index]),
            "standardized_mean_shift": float(standardized[index]),
        }
        for index, name in enumerate(feature_names)
    ]
    return {
        "role": "diagnostic_only",
        "repository_held_out_auc": logreg.auc(era, scores, weights),
        "records": len(era),
        "repository_groups": len(set(groups.tolist())),
        "folds": fold_rows,
        "feature_shifts": shifts,
    }


def historical_anchor_sensitivity(
    agent_scores,
    agent_weights,
    contemporary_human_scores,
    contemporary_human_weights,
    historical_human_scores,
    historical_human_weights,
    target_scores,
    target_weights,
    *,
    bins: int = 20,
) -> dict:
    """Compare the primary human anchor with a historical diagnostic anchor."""
    try:
        contemporary = fit_score_mixture(
            agent_scores,
            agent_weights,
            contemporary_human_scores,
            contemporary_human_weights,
            target_scores,
            target_weights,
            bins=bins,
        )
        historical = fit_score_mixture(
            agent_scores,
            agent_weights,
            historical_human_scores,
            historical_human_weights,
            target_scores,
            target_weights,
            bins=bins,
        )
    except IdentificationError as error:
        raise TemporalDiagnosticError(str(error)) from error
    return {
        "role": "diagnostic_only",
        "contemporary_human_anchor_share": contemporary["share"],
        "historical_human_anchor_share": historical["share"],
        "absolute_share_shift": abs(contemporary["share"] - historical["share"]),
        "contemporary_mixture_total_variation": contemporary["total_variation"],
        "historical_mixture_total_variation": historical["total_variation"],
    }


def build_primary_reference_arrays(records) -> dict:
    """Build authorship arrays while making historical exclusion observable."""
    rows = []
    excluded = 0
    labels_by_group = {}
    for record in records:
        role = record.get("temporal_role")
        if role == "historical_diagnostic":
            excluded += 1
            continue
        if role != "contemporary_reference":
            raise TemporalDiagnosticError(f"unsupported temporal role: {role!r}")
        label = record.get("label")
        if label not in ("human", "agent"):
            raise TemporalDiagnosticError("primary references need human/agent labels")
        group = record["repo"]
        numeric = 0.0 if label == "human" else 1.0
        if group in labels_by_group and labels_by_group[group] != numeric:
            raise TemporalDiagnosticError("repository group has conflicting labels")
        labels_by_group[group] = numeric
        rows.append((record, numeric))
    if not rows:
        raise TemporalDiagnosticError("no contemporary primary references")
    width = len(rows[0][0]["v"])
    if any(len(record["v"]) != width for record, _ in rows):
        raise TemporalDiagnosticError("feature vectors do not align")
    return {
        "x": np.asarray([record["v"] for record, _ in rows], dtype=float),
        "labels": np.asarray([label for _, label in rows], dtype=float),
        "weights": np.asarray(
            [record["line_count"] for record, _ in rows], dtype=float
        ),
        "groups": np.asarray([record["repo"] for record, _ in rows], dtype=object),
        "excluded_historical_records": excluded,
    }
