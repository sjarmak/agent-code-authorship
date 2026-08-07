"""Deterministic repository-period covariate matching for era contrasts."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

MATCHED_CONTROL_COUNT = 5
MATCH_ON = ("language", "path_type", "change_size", "code_age", "calendar_time")


def control_roles(controls: Sequence[Any]) -> dict[str, list[str]]:
    """Group matched controls by their frozen evidence role."""
    return {
        role: [
            control.repository_id
            for control in controls
            if (
                (role == "not_yet_adopter" and control.role == "adopter")
                or control.role == role
            )
        ]
        for role in (
            "h2_ai_ban_control",
            "never_adopter_control",
            "not_yet_adopter",
        )
    }


def _matching_vector(
    repository: Any, pre_period: int, post_period: int
) -> tuple[float, float, float]:
    observations = [
        repository.observation(pre_period),
        repository.observation(post_period),
    ]
    if any(observation is None for observation in observations):
        raise ValueError("matching requires complete period covariates")
    complete = [observation for observation in observations if observation is not None]
    return (
        sum(math.log1p(observation.change_size) for observation in complete) / 2,
        sum(observation.test_share for observation in complete) / 2,
        sum(observation.code_age_days for observation in complete) / 2,
    )


def select_matched_controls(
    adopter: Any,
    controls: Sequence[Any],
    pre_period: int,
    post_period: int,
) -> tuple[list[Any], dict[str, float]]:
    """Select five nearest eligible controls on frozen period covariates."""
    repositories = [adopter, *controls]
    vectors = np.asarray(
        [
            _matching_vector(repository, pre_period, post_period)
            for repository in repositories
        ],
        dtype=float,
    )
    scales = np.std(vectors, axis=0)
    scales = np.where(scales > 0, scales, 1.0)
    distances = np.sum(((vectors[1:] - vectors[0]) / scales) ** 2, axis=1)
    ranked = sorted(
        zip(controls, distances, strict=True),
        key=lambda item: (float(item[1]), item[0].repository_id),
    )
    selected = ranked[:MATCHED_CONTROL_COUNT]
    return (
        [control for control, _distance in selected],
        {control.repository_id: float(distance) for control, distance in selected},
    )
