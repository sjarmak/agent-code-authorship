"""Leakage-safe evaluation of timing, message, and combined commit views."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

TIMING_FEATURES = (
    "log_abs_author_committer_delta_seconds",
    "repository_gap_missing",
    "log_repository_previous_gap_seconds",
    "author_gap_missing",
    "log_same_author_previous_gap_seconds",
    "same_author_prior_15m_count",
    "author_hour_sine",
    "author_hour_cosine",
    "author_weekday_sine",
    "author_weekday_cosine",
    "author_utc_offset_hours",
    "parent_count",
    "log_files_changed",
    "log_lines_added",
    "log_lines_deleted",
    "binary_file_count",
    "rename_entry_count",
)
MESSAGE_FEATURES = (
    "message_score",
    "message_body_word_count",
    "message_bullet_count",
    "message_paragraph_count",
    "message_sentence_count",
    "message_sentence_line_rate",
    "message_structure_count",
    "message_explicit_validation",
)
VIEWS = {
    "timing_process": TIMING_FEATURES,
    "message": MESSAGE_FEATURES,
    "combined": TIMING_FEATURES + MESSAGE_FEATURES,
}


def _log(value: float | int | None, *, absolute: bool = False) -> float:
    if value is None:
        return 0.0
    number = abs(float(value)) if absolute else max(float(value), 0.0)
    return math.log1p(number)


def _cyclic(value: float, period: float) -> tuple[float, float]:
    angle = 2 * math.pi * value / period
    return math.sin(angle), math.cos(angle)


def _timing_values(record: Mapping[str, Any]) -> tuple[float, ...]:
    timing = record["timing_process_features"]
    repository_gap = timing.get("repository_previous_gap_seconds")
    author_gap = timing.get("same_author_previous_gap_seconds")
    hour_sine, hour_cosine = _cyclic(float(timing["author_local_hour"]), 24)
    weekday_sine, weekday_cosine = _cyclic(
        float(timing["author_weekday"]), 7
    )
    return (
        _log(timing.get("author_committer_delta_seconds"), absolute=True),
        float(repository_gap is None),
        _log(repository_gap),
        float(author_gap is None),
        _log(author_gap),
        float(timing["same_author_prior_15m_count"]),
        hour_sine,
        hour_cosine,
        weekday_sine,
        weekday_cosine,
        float(timing["author_utc_offset_minutes"]) / 60,
        float(timing["parent_count"]),
        _log(timing["files_changed"]),
        _log(timing["lines_added"]),
        _log(timing["lines_deleted"]),
        float(timing["binary_file_count"]),
        float(timing["rename_entry_count"]),
    )


def _message_values(record: Mapping[str, Any]) -> tuple[float, ...]:
    features = record["message_features"]
    return (
        float(record["message_score"]),
        float(features["body_word_count"]),
        float(features["bullet_count"]),
        float(features["paragraph_count"]),
        float(features["sentence_count"]),
        float(features["sentence_line_rate"]),
        float(features["structure_count"]),
        float(bool(features["explicit_validation"])),
    )


def feature_names(view: str) -> tuple[str, ...]:
    """Return the frozen non-label fields for an evidence view."""
    try:
        return VIEWS[view]
    except KeyError as error:
        raise ValueError(f"unknown feature view: {view}") from error


def feature_vector(
    record: Mapping[str, Any],
    view: str,
) -> tuple[float, ...]:
    """Mechanically transform one commit without consulting its label."""
    feature_names(view)
    if view == "timing_process":
        return _timing_values(record)
    if view == "message":
        return _message_values(record)
    return _timing_values(record) + _message_values(record)


def _label(record: Mapping[str, Any]) -> int:
    label = record.get("reference_label")
    if label not in {"agent", "human"}:
        raise ValueError("evaluation records require agent or human labels")
    return int(label == "agent")


def _matrix(
    records: Sequence[Mapping[str, Any]], view: str
) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.asarray([feature_vector(record, view) for record in records]),
        np.asarray([_label(record) for record in records]),
    )


def _model() -> Any:
    return make_pipeline(
        StandardScaler(),
        LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            random_state=0,
        ),
    )


def _metrics(
    labels: np.ndarray,
    probabilities: np.ndarray,
) -> dict[str, Any]:
    detected = probabilities >= 0.5
    tn, fp, fn, tp = confusion_matrix(
        labels, detected, labels=[0, 1]
    ).ravel()
    return {
        "labeled_commit_count": int(labels.size),
        "tp": int(tp),
        "fp": int(fp),
        "tn": int(tn),
        "fn": int(fn),
        "precision_at_0_5": float(
            precision_score(labels, detected, zero_division=0)
        ),
        "recall_at_0_5": float(
            recall_score(labels, detected, zero_division=0)
        ),
        "average_precision": float(average_precision_score(labels, probabilities)),
        "roc_auc": float(roc_auc_score(labels, probabilities)),
        "brier_score": float(brier_score_loss(labels, probabilities)),
    }


def _has_both_labels(records: Sequence[Mapping[str, Any]]) -> bool:
    return {_label(record) for record in records} == {0, 1}


def _repository_metrics(
    repository_id: str,
    labels: np.ndarray,
    probabilities: np.ndarray,
) -> dict[str, Any]:
    if set(labels.tolist()) not in ({0}, {1}):
        raise ValueError("held repository must have a single reference label")
    detected = probabilities >= 0.5
    negatives = labels == 0
    positives = labels == 1
    false_positives = int(np.sum(detected & negatives))
    false_negatives = int(np.sum(~detected & positives))
    reference_label = "agent" if bool(np.all(positives)) else "human"
    return {
        "repository_id": repository_id,
        "reference_label": reference_label,
        "commit_count": int(labels.size),
        "detected_positive_count": int(np.sum(detected)),
        "false_positive_count": false_positives,
        "false_positive_rate": (
            false_positives / int(np.sum(negatives))
            if bool(np.any(negatives))
            else None
        ),
        "false_negative_count": false_negatives,
        "false_negative_rate": (
            false_negatives / int(np.sum(positives))
            if bool(np.any(positives))
            else None
        ),
    }


def evaluate_leave_one_repository_out(
    records: Sequence[Mapping[str, Any]],
    *,
    view: str,
) -> dict[str, Any]:
    """Predict each repository only from models trained on other repositories."""
    feature_names(view)
    repositories = sorted({str(record["repository_id"]) for record in records})
    probabilities = []
    labels = []
    repository_metrics = []
    for repository_id in repositories:
        train = [
            record
            for record in records
            if record["repository_id"] != repository_id
        ]
        test = [
            record
            for record in records
            if record["repository_id"] == repository_id
        ]
        if not train or not test or not _has_both_labels(train):
            return {
                "status": "unavailable",
                "reason": "a held-repository fold lacks both training labels",
                "view": view,
            }
        train_x, train_y = _matrix(train, view)
        test_x, test_y = _matrix(test, view)
        fitted = _model().fit(train_x, train_y)
        fold_probabilities = fitted.predict_proba(test_x)[:, 1]
        probabilities.extend(fold_probabilities)
        labels.extend(test_y)
        repository_metrics.append(
            _repository_metrics(repository_id, test_y, fold_probabilities)
        )
    result = _metrics(np.asarray(labels), np.asarray(probabilities))
    return {
        "status": "available",
        "view": view,
        "held_out_repository_count": len(repositories),
        "repository_metrics": repository_metrics,
        **result,
    }


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


def evaluate_era_holdout(
    records: Sequence[Mapping[str, Any]],
    *,
    view: str,
    cutoff: str,
) -> dict[str, Any]:
    """Train before a frozen cutoff and score commits at or after it."""
    feature_names(view)
    boundary = _utc(cutoff)
    train = [
        record for record in records if _utc(record["committed_at"]) < boundary
    ]
    test = [
        record for record in records if _utc(record["committed_at"]) >= boundary
    ]
    if not train or not test or not _has_both_labels(train) or not _has_both_labels(test):
        return {
            "status": "unavailable",
            "reason": "era train and test windows must each contain both labels",
            "view": view,
            "cutoff": cutoff,
            "training_commit_count": len(train),
            "test_commit_count": len(test),
        }
    train_x, train_y = _matrix(train, view)
    test_x, test_y = _matrix(test, view)
    probabilities = _model().fit(train_x, train_y).predict_proba(test_x)[:, 1]
    return {
        "status": "available",
        "view": view,
        "cutoff": cutoff,
        "training_commit_count": len(train),
        "test_commit_count": len(test),
        **_metrics(test_y, probabilities),
    }
