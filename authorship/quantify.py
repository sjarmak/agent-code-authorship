"""Population quantification from cross-fitted classifier score distributions."""
from __future__ import annotations

from typing import Iterable

import numpy as np


class IdentificationError(RuntimeError):
    """Raised when labeled score distributions cannot identify a mixture."""


def _as_arrays(scores, weights) -> tuple[np.ndarray, np.ndarray]:
    score_array = np.asarray(scores, dtype=float)
    weight_array = np.asarray(weights, dtype=float)
    if (
        score_array.ndim != 1
        or weight_array.ndim != 1
        or len(score_array) != len(weight_array)
        or not len(score_array)
        or np.any(~np.isfinite(score_array))
        or np.any(~np.isfinite(weight_array))
        or np.any(weight_array <= 0)
    ):
        raise IdentificationError("scores and positive weights must be finite vectors")
    return score_array, weight_array


def _weighted_quantiles(
    values: np.ndarray, weights: np.ndarray, probabilities: np.ndarray
) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    sorted_values, sorted_weights = values[order], weights[order]
    positions = (np.cumsum(sorted_weights) - 0.5 * sorted_weights) / sorted_weights.sum()
    return np.interp(probabilities, positions, sorted_values)


def _edges(
    agent_scores: np.ndarray,
    agent_weights: np.ndarray,
    human_scores: np.ndarray,
    human_weights: np.ndarray,
    bins: int,
) -> np.ndarray:
    if bins < 2:
        raise IdentificationError("at least two bins are required")
    pooled = np.concatenate([agent_scores, human_scores])
    weights = np.concatenate([agent_weights, human_weights])
    inner = _weighted_quantiles(
        pooled, weights, np.linspace(0, 1, bins + 1)[1:-1]
    )
    unique = np.unique(inner)
    if len(unique) < 1:
        raise IdentificationError("reference scores are indistinguishable")
    return np.concatenate(([-np.inf], unique, [np.inf]))


def _histogram(
    scores: np.ndarray, weights: np.ndarray, edges: np.ndarray, smoothing: float
) -> np.ndarray:
    counts = np.histogram(scores, bins=edges, weights=weights)[0].astype(float)
    counts += smoothing
    return counts / counts.sum()


def _maximize_share(
    target_counts: np.ndarray, agent_probability: np.ndarray, human_probability: np.ndarray
) -> tuple[float, float]:
    def objective(share: float) -> float:
        mixture = share * agent_probability + (1 - share) * human_probability
        return float(np.sum(target_counts * np.log(np.clip(mixture, 1e-15, None))))

    left, right = 0.0, 1.0
    ratio = (np.sqrt(5) - 1) / 2
    x1, x2 = right - ratio * (right - left), left + ratio * (right - left)
    f1, f2 = objective(x1), objective(x2)
    for _ in range(80):
        if f1 < f2:
            left, x1, f1 = x1, x2, f2
            x2 = left + ratio * (right - left)
            f2 = objective(x2)
        else:
            right, x2, f2 = x2, x1, f1
            x1 = right - ratio * (right - left)
            f1 = objective(x1)
    candidates = [(0.0, objective(0.0)), (1.0, objective(1.0))]
    midpoint = (left + right) / 2
    candidates.append((midpoint, objective(midpoint)))
    return max(candidates, key=lambda pair: pair[1])


def fit_score_mixture(
    agent_scores,
    agent_weights,
    human_scores,
    human_weights,
    target_scores,
    target_weights,
    *,
    bins: int = 20,
    smoothing: float = 0.5,
    minimum_reference_variation: float = 0.05,
    maximum_total_variation: float = 0.10,
) -> dict:
    agent_scores, agent_weights = _as_arrays(agent_scores, agent_weights)
    human_scores, human_weights = _as_arrays(human_scores, human_weights)
    target_scores, target_weights = _as_arrays(target_scores, target_weights)
    edges = _edges(agent_scores, agent_weights, human_scores, human_weights, bins)
    agent_probability = _histogram(agent_scores, agent_weights, edges, smoothing)
    human_probability = _histogram(human_scores, human_weights, edges, smoothing)
    separation = 0.5 * np.abs(agent_probability - human_probability).sum()
    if separation < minimum_reference_variation:
        raise IdentificationError("reference score distributions are indistinguishable")
    target_counts = np.histogram(target_scores, bins=edges, weights=target_weights)[0]
    share, log_likelihood = _maximize_share(
        target_counts, agent_probability, human_probability
    )
    observed = target_counts / target_counts.sum()
    expected = share * agent_probability + (1 - share) * human_probability
    total_variation = float(0.5 * np.abs(observed - expected).sum())
    return {
        "share": float(share),
        "log_likelihood": log_likelihood,
        "total_variation": total_variation,
        "reference_total_variation": float(separation),
        "plausible": total_variation <= maximum_total_variation,
        "bins": len(edges) - 1,
        "edges": edges,
        "agent_probability": agent_probability,
        "human_probability": human_probability,
        "observed_probability": observed,
        "expected_probability": expected,
    }


def threshold_adjusted_share(
    agent_scores,
    agent_weights,
    human_scores,
    human_weights,
    target_scores,
    target_weights,
    *,
    threshold: float,
) -> float:
    agent_scores, agent_weights = _as_arrays(agent_scores, agent_weights)
    human_scores, human_weights = _as_arrays(human_scores, human_weights)
    target_scores, target_weights = _as_arrays(target_scores, target_weights)
    tpr = float(np.average(agent_scores >= threshold, weights=agent_weights))
    fpr = float(np.average(human_scores >= threshold, weights=human_weights))
    observed = float(np.average(target_scores >= threshold, weights=target_weights))
    if tpr - fpr < 0.05:
        raise IdentificationError("threshold has insufficient separation")
    return float(np.clip((observed - fpr) / (tpr - fpr), 0, 1))


def _resample_groups(
    scores: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    names = np.array(sorted(set(groups.tolist())), dtype=object)
    if not len(names):
        raise IdentificationError("group bootstrap requires repositories")
    selected = rng.choice(names, size=len(names), replace=True)
    indices = np.concatenate([np.where(groups == name)[0] for name in selected])
    return scores[indices], weights[indices]


def bootstrap_score_mixture(
    agent_scores,
    agent_weights,
    agent_groups,
    human_scores,
    human_weights,
    human_groups,
    target_scores,
    target_weights,
    target_groups,
    *,
    replicates: int,
    seed: int,
    bins: int = 20,
) -> list[float]:
    agent_scores, agent_weights = _as_arrays(agent_scores, agent_weights)
    human_scores, human_weights = _as_arrays(human_scores, human_weights)
    target_scores, target_weights = _as_arrays(target_scores, target_weights)
    group_arrays = [
        np.asarray(agent_groups, dtype=object),
        np.asarray(human_groups, dtype=object),
        np.asarray(target_groups, dtype=object),
    ]
    if any(
        len(groups) != len(scores)
        for groups, scores in zip(
            group_arrays, (agent_scores, human_scores, target_scores)
        )
    ):
        raise IdentificationError("group vectors must align with scores")
    rng = np.random.default_rng(seed)
    shares = []
    for _ in range(replicates):
        a_s, a_w = _resample_groups(
            agent_scores, agent_weights, group_arrays[0], rng
        )
        h_s, h_w = _resample_groups(
            human_scores, human_weights, group_arrays[1], rng
        )
        t_s, t_w = _resample_groups(
            target_scores, target_weights, group_arrays[2], rng
        )
        shares.append(
            fit_score_mixture(a_s, a_w, h_s, h_w, t_s, t_w, bins=bins)["share"]
        )
    return shares


def _join_groups(
    groups: dict[str, tuple[np.ndarray, np.ndarray]], selected: Iterable[str]
) -> tuple[np.ndarray, np.ndarray]:
    parts = [groups[name] for name in selected]
    return (
        np.concatenate([part[0] for part in parts]),
        np.concatenate([part[1] for part in parts]),
    )


def synthetic_mixture_evaluation(
    agent_groups: dict[str, tuple[np.ndarray, np.ndarray]],
    human_groups: dict[str, tuple[np.ndarray, np.ndarray]],
    *,
    shares: Iterable[float],
    groups_per_mixture: int,
    trials: int,
    seed: int,
    bins: int = 20,
    interval_replicates: int = 200,
) -> dict:
    agent_names = np.array(sorted(agent_groups), dtype=object)
    human_names = np.array(sorted(human_groups), dtype=object)
    if len(agent_names) < 2 or len(human_names) < 2:
        raise IdentificationError("synthetic evaluation needs held-out groups")
    split_a, split_h = len(agent_names) // 2, len(human_names) // 2
    reference_a, pool_a = agent_names[:split_a], agent_names[split_a:]
    reference_h, pool_h = human_names[:split_h], human_names[split_h:]
    a_scores, a_weights = _join_groups(agent_groups, reference_a)
    h_scores, h_weights = _join_groups(human_groups, reference_h)
    a_reference_groups = np.concatenate(
        [
            np.full(len(agent_groups[name][0]), name, dtype=object)
            for name in reference_a
        ]
    )
    h_reference_groups = np.concatenate(
        [
            np.full(len(human_groups[name][0]), name, dtype=object)
            for name in reference_h
        ]
    )
    result = synthetic_mixture_evaluation_from_holdout(
        (a_scores, a_weights, a_reference_groups),
        (h_scores, h_weights, h_reference_groups),
        {name: agent_groups[name] for name in pool_a},
        {name: human_groups[name] for name in pool_h},
        shares=shares,
        groups_per_mixture=groups_per_mixture,
        trials=trials,
        seed=seed,
        bins=bins,
        interval_replicates=interval_replicates,
    )
    result["design"] = "deterministic_internal_group_split"
    return result


def synthetic_mixture_evaluation_from_holdout(
    agent_reference: tuple[np.ndarray, np.ndarray, np.ndarray],
    human_reference: tuple[np.ndarray, np.ndarray, np.ndarray],
    agent_validation_groups: dict[str, tuple[np.ndarray, np.ndarray]],
    human_validation_groups: dict[str, tuple[np.ndarray, np.ndarray]],
    *,
    shares: Iterable[float],
    groups_per_mixture: int,
    trials: int,
    seed: int,
    bins: int = 20,
    interval_replicates: int = 200,
) -> dict:
    """Evaluate mixtures made only from explicit, untouched validation groups."""
    a_scores, a_weights, a_reference_groups = agent_reference
    h_scores, h_weights, h_reference_groups = human_reference
    arrays = [
        np.asarray(a_scores, dtype=float),
        np.asarray(a_weights, dtype=float),
        np.asarray(a_reference_groups, dtype=object),
        np.asarray(h_scores, dtype=float),
        np.asarray(h_weights, dtype=float),
        np.asarray(h_reference_groups, dtype=object),
    ]
    (
        a_scores,
        a_weights,
        a_reference_groups,
        h_scores,
        h_weights,
        h_reference_groups,
    ) = arrays
    if not agent_validation_groups or not human_validation_groups:
        raise IdentificationError("synthetic evaluation needs held-out groups")
    if (
        len(a_scores) != len(a_weights)
        or len(a_scores) != len(a_reference_groups)
        or len(h_scores) != len(h_weights)
        or len(h_scores) != len(h_reference_groups)
    ):
        raise IdentificationError("reference score, weight, and group vectors must align")
    if groups_per_mixture < 1 or trials < 1 or interval_replicates < 1:
        raise IdentificationError("synthetic evaluation counts must be positive")
    reference_names = set(a_reference_groups) | set(h_reference_groups)
    validation_names = set(agent_validation_groups) | set(human_validation_groups)
    if reference_names & validation_names:
        raise IdentificationError("reference and validation group overlap")

    rng = np.random.default_rng(seed)
    pool_a = np.array(sorted(agent_validation_groups), dtype=object)
    pool_h = np.array(sorted(human_validation_groups), dtype=object)
    validation_groups = {**agent_validation_groups, **human_validation_groups}
    rows = []
    for requested in shares:
        n_agent = round(groups_per_mixture * requested)
        n_human = groups_per_mixture - n_agent
        for trial in range(trials):
            selected_a = (
                rng.choice(pool_a, size=n_agent, replace=True) if n_agent else []
            )
            selected_h = (
                rng.choice(pool_h, size=n_human, replace=True) if n_human else []
            )
            target_scores, target_weights = _join_groups(
                validation_groups, [*selected_a, *selected_h]
            )
            selected = [*selected_a, *selected_h]
            target_groups = np.concatenate(
                [
                    np.full(
                        len(validation_groups[name][0]),
                        name,
                        dtype=object,
                    )
                    for name in selected
                ]
            )
            agent_weight = sum(
                float(agent_validation_groups[name][1].sum()) for name in selected_a
            )
            true_share = agent_weight / float(target_weights.sum())
            fitted = fit_score_mixture(
                a_scores,
                a_weights,
                h_scores,
                h_weights,
                target_scores,
                target_weights,
                bins=bins,
            )
            bootstrapped = bootstrap_score_mixture(
                a_scores,
                a_weights,
                a_reference_groups,
                h_scores,
                h_weights,
                h_reference_groups,
                target_scores,
                target_weights,
                target_groups,
                replicates=interval_replicates,
                seed=seed + len(rows) + trial,
                bins=bins,
            )
            interval = [
                float(np.percentile(bootstrapped, 2.5)),
                float(np.percentile(bootstrapped, 97.5)),
            ]
            rows.append(
                {
                    "requested_share": float(requested),
                    "true_share": true_share,
                    "estimated_share": fitted["share"],
                    "absolute_error": abs(fitted["share"] - true_share),
                    "plausible": fitted["plausible"],
                    "interval": interval,
                    "covered": interval[0] <= true_share <= interval[1],
                }
            )
    errors = np.array([row["absolute_error"] for row in rows])
    return {
        "design": "explicit_dedicated_validation",
        "reference_groups": {
            "agent": len(set(a_reference_groups)),
            "human": len(set(h_reference_groups)),
        },
        "validation_groups": {
            "agent": len(agent_validation_groups),
            "human": len(human_validation_groups),
        },
        "mixture_count": len(rows),
        "mean_absolute_error": float(errors.mean()),
        "maximum_absolute_error": float(errors.max()),
        "interval_coverage": float(np.mean([row["covered"] for row in rows])),
        "nominal_interval": 0.95,
        "rows": rows,
    }
