import numpy as np

from authorship.logreg import auc


def test_weighted_auc_is_bounded_and_matches_pairwise_definition():
    labels = np.array([0, 1, 0, 1], dtype=float)
    scores = np.array([0, 1, 2, 3], dtype=float)
    weights = np.array([100, 1, 1, 100], dtype=float)

    result = auc(labels, scores, weights)

    assert result == 10200 / 10201
    assert 0 <= result <= 1


def test_weighted_auc_assigns_half_credit_to_ties():
    labels = np.array([0, 1], dtype=float)
    scores = np.array([1, 1], dtype=float)
    weights = np.array([7, 3], dtype=float)

    assert auc(labels, scores, weights) == 0.5
