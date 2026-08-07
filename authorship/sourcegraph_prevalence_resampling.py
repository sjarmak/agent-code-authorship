"""Repository-cluster resampling for Sourcegraph prevalence estimation."""

from __future__ import annotations

from typing import Any

import numpy as np

from authorship.quantify import IdentificationError
from authorship.replication import run_quantification


def resample_repository_clusters(
    values: np.ndarray,
    labels: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    rng: np.random.Generator,
    *,
    prefix: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Resample repositories once while retaining every row and label."""
    names = np.asarray(sorted(set(groups.tolist())), dtype=object)
    if not len(names):
        raise IdentificationError("repository bootstrap requires repositories")
    selected = rng.choice(names, size=len(names), replace=True)
    indices, sampled_groups = [], []
    for original in selected:
        rows = np.where(groups == original)[0]
        indices.append(rows)
        sampled_groups.append(
            np.full(
                len(rows),
                f"{prefix}::{original}",
                dtype=object,
            )
        )
    joined = np.concatenate(indices)
    return (
        values[joined],
        labels[joined],
        weights[joined],
        np.concatenate(sampled_groups),
    )


def resample_target_clusters(
    values: np.ndarray,
    weights: np.ndarray,
    groups: np.ndarray,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    labels = np.zeros(len(values), dtype=float)
    sampled = resample_repository_clusters(
        values, labels, weights, groups, rng, prefix="target"
    )
    return sampled[0], sampled[2], sampled[3]


def _bootstrap_replicate(
    reference: tuple[np.ndarray, ...],
    target: tuple[np.ndarray, ...],
    *,
    seed: int,
    bins: int,
) -> dict[str, Any]:
    return run_quantification(*reference, *target, seed=seed, bins=bins)


def repository_cluster_bootstrap(
    reference_x: np.ndarray,
    reference_y: np.ndarray,
    reference_weights: np.ndarray,
    reference_groups: np.ndarray,
    target_x: np.ndarray,
    target_weights: np.ndarray,
    target_groups: np.ndarray,
    *,
    replicates: int,
    seed: int,
    bins: int,
) -> dict[str, Any]:
    """Bootstrap the end-to-end model with joint repository clusters."""
    rng = np.random.default_rng(seed)
    shares, failures, refits = [], [], 0
    for replicate in range(replicates):
        reference = resample_repository_clusters(
            reference_x,
            reference_y,
            reference_weights,
            reference_groups,
            rng,
            prefix="reference",
        )
        target = resample_target_clusters(target_x, target_weights, target_groups, rng)
        try:
            result = _bootstrap_replicate(
                reference, target, seed=seed + replicate, bins=bins
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
        "resampling_unit": "joint_repository_cluster",
    }
