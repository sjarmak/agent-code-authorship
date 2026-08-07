"""Repository-held-out synthetic diagnostics for prevalence estimation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from authorship import logreg
from authorship.quantify import IdentificationError, fit_score_mixture
from authorship.sourcegraph_prevalence_resampling import (
    resample_repository_clusters,
    resample_target_clusters,
)


def _labels_by_group(labels: np.ndarray, groups: np.ndarray) -> dict[str, set[float]]:
    grouped: dict[str, set[float]] = defaultdict(set)
    for label, group in zip(labels, groups):
        grouped[str(group)].add(float(label))
    return dict(grouped)


def repository_heldout_split(
    labels: Sequence[float], groups: Sequence[str]
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministically hold out whole repositories, stratified by label set."""
    labels = np.asarray(labels, dtype=float)
    groups = np.asarray(groups, dtype=object)
    if len(labels) != len(groups):
        raise IdentificationError("labels and repository groups must align")
    strata: dict[tuple[float, ...], list[str]] = defaultdict(list)
    for group, values in _labels_by_group(labels, groups).items():
        strata[tuple(sorted(values))].append(group)
    development, validation = set(), set()
    for names in strata.values():
        ordered = sorted(names)
        if len(ordered) < 2:
            raise IdentificationError(
                "synthetic validation needs two repositories per label stratum"
            )
        development.update(ordered[::2])
        validation.update(ordered[1::2])
    development_index = np.where(np.isin(groups, sorted(development)))[0]
    validation_index = np.where(np.isin(groups, sorted(validation)))[0]
    if set(np.unique(labels[development_index])) != {0.0, 1.0}:
        raise IdentificationError("development repositories lack both classes")
    if set(np.unique(labels[validation_index])) != {0.0, 1.0}:
        raise IdentificationError("validation repositories lack both classes")
    return development_index, validation_index


def _crossfit_scores(
    x: np.ndarray,
    labels: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    *,
    folds: int,
    ridge: float,
) -> np.ndarray:
    scores = np.full(len(labels), np.nan)
    for test in logreg.group_folds(groups, folds):
        if not len(test):
            continue
        train = np.setdiff1d(np.arange(len(labels)), test)
        if set(np.unique(labels[train])) != {0.0, 1.0}:
            raise IdentificationError("repository fold lacks both classes")
        model = logreg.fit(x[train], labels[train], weights[train], ridge=ridge)
        scores[test] = logreg.predict(model, x[test])
    if np.any(~np.isfinite(scores)):
        raise IdentificationError("cross-fitting did not score every reference")
    return scores


def _scored_validation(
    arrays: Mapping[str, Any], specification: Mapping[str, Any]
) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    reference = arrays["reference"]
    model = specification["model"]
    development, validation = repository_heldout_split(
        reference["labels"], reference["groups"]
    )
    dev_scores = _crossfit_scores(
        reference["x"][development],
        reference["labels"][development],
        reference["weights"][development],
        reference["groups"][development],
        folds=model["cross_fit_folds"],
        ridge=model["ridge"],
    )
    fitted = logreg.fit(
        reference["x"][development],
        reference["labels"][development],
        reference["weights"][development],
        ridge=model["ridge"],
    )
    validation_scores = logreg.predict(fitted, reference["x"][validation])
    references = (
        dev_scores,
        reference["labels"][development],
        reference["weights"][development],
        reference["groups"][development],
    )
    heldout = (
        validation_scores,
        reference["labels"][validation],
        reference["weights"][validation],
        reference["groups"][validation],
    )
    return references, heldout


def _fit_scored_mixture(
    references: tuple[np.ndarray, ...],
    target: tuple[np.ndarray, ...],
    *,
    bins: int,
) -> dict[str, Any]:
    scores, labels, weights, _ = references
    target_scores, target_weights, _ = target
    agent, human = labels == 1.0, labels == 0.0
    return fit_score_mixture(
        scores[agent],
        weights[agent],
        scores[human],
        weights[human],
        target_scores,
        target_weights,
        bins=bins,
    )


def _bootstrap_scored_mixture(
    references: tuple[np.ndarray, ...],
    target: tuple[np.ndarray, ...],
    *,
    replicates: int,
    seed: int,
    bins: int,
) -> list[float]:
    scores, labels, weights, groups = references
    target_scores, target_weights, target_groups = target
    rng, shares = np.random.default_rng(seed), []
    for _ in range(replicates):
        sampled_reference = resample_repository_clusters(
            scores, labels, weights, groups, rng, prefix="synthetic-reference"
        )
        sampled_target = resample_target_clusters(
            target_scores, target_weights, target_groups, rng
        )
        shares.append(
            _fit_scored_mixture(sampled_reference, sampled_target, bins=bins)["share"]
        )
    return shares


def _select_synthetic_groups(
    heldout: tuple[np.ndarray, ...],
    assignments: Sequence[float],
    rng: np.random.Generator,
    *,
    trial_id: str,
) -> tuple[tuple[np.ndarray, ...], float, list[dict[str, Any]]]:
    scores, labels, weights, groups = heldout
    eligible = {
        label: sorted(set(groups[labels == label].tolist())) for label in (0.0, 1.0)
    }
    assigned: dict[str, float] = {}
    parts, selections, total_agent_weight = [], [], 0.0
    for draw, label in enumerate(assignments):
        candidates = [
            group for group in eligible[label] if assigned.get(group, label) == label
        ]
        if not candidates:
            raise IdentificationError(
                "held-out repositories cannot satisfy synthetic class assignment"
            )
        group = str(rng.choice(np.asarray(candidates, dtype=object)))
        assigned[group] = label
        selections.append({"repository": group, "label": int(label)})
        selected = np.where((groups == group) & (labels == label))[0]
        synthetic_group = np.full(
            len(selected), f"{trial_id}::draw:{draw}", dtype=object
        )
        parts.append((scores[selected], weights[selected], synthetic_group))
        if label == 1.0:
            total_agent_weight += float(weights[selected].sum())
    target = tuple(
        np.concatenate([part[index] for part in parts]) for index in range(3)
    )
    return target, total_agent_weight / float(target[1].sum()), selections


def _synthetic_row(
    references: tuple[np.ndarray, ...],
    heldout: tuple[np.ndarray, ...],
    assignments: np.ndarray,
    rng: np.random.Generator,
    *,
    trial_id: str,
    interval_replicates: int,
    seed: int,
    bins: int,
) -> dict[str, Any]:
    target, true_share, selections = _select_synthetic_groups(
        heldout, assignments, rng, trial_id=trial_id
    )
    fitted = _fit_scored_mixture(references, target, bins=bins)
    shares = _bootstrap_scored_mixture(
        references,
        target,
        replicates=interval_replicates,
        seed=seed,
        bins=bins,
    )
    interval = [
        float(np.percentile(shares, 2.5)),
        float(np.percentile(shares, 97.5)),
    ]
    return {
        "true_share": true_share,
        "estimated_share": fitted["share"],
        "absolute_error": abs(fitted["share"] - true_share),
        "plausible": fitted["plausible"],
        "interval": interval,
        "covered": interval[0] <= true_share <= interval[1],
        "source_repository_assignments": selections,
    }


def synthetic_diagnostic(
    arrays: Mapping[str, Any], specification: Mapping[str, Any]
) -> dict[str, Any]:
    references, heldout = _scored_validation(arrays, specification)
    design = specification["diagnostics"]["synthetic_mixture"]
    seed = specification["model"]["seed"]
    bins = specification["model"]["score_mixture_bins"]
    rng, rows = np.random.default_rng(seed), []
    for requested in design["shares"]:
        n_agent = round(design["groups_per_mixture"] * requested)
        assignments = np.asarray(
            [1.0] * n_agent + [0.0] * (design["groups_per_mixture"] - n_agent)
        )
        for trial in range(design["trials_per_share"]):
            rng.shuffle(assignments)
            row = _synthetic_row(
                references,
                heldout,
                assignments,
                rng,
                trial_id=f"share:{requested}::trial:{trial}",
                interval_replicates=design["interval_replicates"],
                seed=seed + len(rows),
                bins=bins,
            )
            rows.append({"requested_share": float(requested), **row})
    errors = [row["absolute_error"] for row in rows]
    return {
        "design": "repository_held_out_joint_repository_resampling",
        "mixture_count": len(rows),
        "mean_absolute_error": float(np.mean(errors)),
        "maximum_absolute_error": float(np.max(errors)),
        "interval_coverage": float(np.mean([row["covered"] for row in rows])),
        "nominal_interval": 0.95,
        "rows": rows,
    }
