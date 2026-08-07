#!/usr/bin/env python3
"""Minimal, dependency-light logistic regression + metrics for the fingerprint.

numpy only (no sklearn on this box). Newton-Raphson with L2 ridge converges in a
handful of iterations at this feature count, and keeping the fit in-repo means
the reported coefficients are reproducible from these files alone.

Everything is functional: fit() returns a new model dict, nothing mutates.
"""

from __future__ import annotations

import numpy as np


def standardizer(x: np.ndarray) -> dict:
    mu = x.mean(axis=0)
    sd = x.std(axis=0)
    return {"mu": mu, "sd": np.where(sd < 1e-9, 1.0, sd)}


def apply_std(std: dict, x: np.ndarray) -> np.ndarray:
    return (x - std["mu"]) / std["sd"]


def fit(
    x: np.ndarray,
    y: np.ndarray,
    weight: np.ndarray | None = None,
    ridge: float = 1.0,
    iters: int = 40,
) -> dict:
    """Fit y ~ sigmoid(b0 + x.beta). Features are standardized internally."""
    std = standardizer(x)
    z = np.hstack([np.ones((len(x), 1)), apply_std(std, x)])
    w = np.ones(len(x)) if weight is None else weight.astype(float)
    w = w / w.mean()
    beta = np.zeros(z.shape[1])
    penalty = np.eye(z.shape[1]) * ridge
    penalty[0, 0] = 0.0  # never penalize the intercept
    for _ in range(iters):
        p = _sigmoid(z @ beta)
        grad = z.T @ (w * (y - p)) - penalty @ beta
        s = np.clip(w * p * (1 - p), 1e-8, None)
        hess = z.T @ (z * s[:, None]) + penalty
        step = np.linalg.solve(hess, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"beta": beta, "std": std}


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    z = np.hstack([np.ones((len(x), 1)), apply_std(model["std"], x)])
    return _sigmoid(z @ model["beta"])


def _sigmoid(t: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(t, -35, 35)))


def auc(y: np.ndarray, score: np.ndarray, weight: np.ndarray | None = None) -> float:
    """Weighted ROC AUC via the Mann-Whitney rank form (ties get mid-ranks)."""
    w = np.ones(len(y)) if weight is None else weight.astype(float)
    order = np.argsort(score, kind="mergesort")
    s, y_s, w_s = score[order], y[order], w[order]
    pos, neg = w_s[y_s == 1].sum(), w_s[y_s == 0].sum()
    if pos == 0 or neg == 0:
        return float("nan")
    numerator = 0.0
    negative_below = 0.0
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[i]:
            j += 1
        block_labels = y_s[i : j + 1]
        block_weights = w_s[i : j + 1]
        positive_block = block_weights[block_labels == 1].sum()
        negative_block = block_weights[block_labels == 0].sum()
        numerator += positive_block * (negative_below + 0.5 * negative_block)
        negative_below += negative_block
        i = j + 1
    return float(numerator / (pos * neg))


def rates_at(
    y: np.ndarray, score: np.ndarray, thr: float, weight: np.ndarray | None = None
) -> tuple[float, float]:
    """(tpr, fpr) at a score threshold, line-weighted if weights given."""
    w = np.ones(len(y)) if weight is None else weight.astype(float)
    pred = score >= thr
    pos, neg = w[y == 1].sum(), w[y == 0].sum()
    tpr = w[(y == 1) & pred].sum() / pos if pos else float("nan")
    fpr = w[(y == 0) & pred].sum() / neg if neg else float("nan")
    return float(tpr), float(fpr)


def best_threshold(
    y: np.ndarray, score: np.ndarray, weight: np.ndarray | None = None
) -> float:
    """Threshold maximizing Youden's J over a coarse grid (stable, not overfit)."""
    grid = np.quantile(score, np.linspace(0.02, 0.98, 49))
    js = [
        (rates_at(y, score, t, weight)[0] - rates_at(y, score, t, weight)[1], t)
        for t in grid
    ]
    return float(max(js)[1])


def group_folds(groups: np.ndarray, k: int = 5) -> list[np.ndarray]:
    """Deterministic group-disjoint folds: no repo appears in train and test."""
    uniq = sorted(set(groups.tolist()))
    assign = {g: i % k for i, g in enumerate(uniq)}
    fold_of = np.array([assign[g] for g in groups])
    return [np.where(fold_of == i)[0] for i in range(k)]
