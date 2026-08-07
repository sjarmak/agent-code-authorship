"""Cross-feature modernization diagnostics for the frozen era study."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from authorship.temporal_diagnostics import TemporalDiagnosticError, era_diagnostics

CONTROL_ROLES = frozenset(("h2_ai_ban_control", "never_adopter_control"))


def _repositories(panel: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        repository["repository_id"]: repository
        for repository in panel.get("repositories", [])
        if isinstance(repository, Mapping)
    }


def _observations(repository: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    return {
        observation["period"]: observation
        for observation in repository.get("observations", [])
        if isinstance(observation, Mapping)
        and isinstance(observation.get("period"), int)
    }


def _feature_vector(
    feature_repositories: Mapping[str, Mapping[str, Mapping[str, Any]]],
    feature_ids: Sequence[str],
    repository_id: str,
    period: int,
) -> list[float] | None:
    vector = []
    for feature_id in feature_ids:
        repository = feature_repositories[feature_id].get(repository_id)
        observation = _observations(repository).get(period) if repository else None
        value = observation.get("feature_value") if observation else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        vector.append(float(value))
    return vector


def _language_rows(
    panels: Mapping[str, Mapping[str, Any]], language: str
) -> tuple[list[list[float]], list[int], list[float], list[str]]:
    feature_ids = sorted(panels)
    feature_repositories = {
        feature_id: _repositories(panels[feature_id]) for feature_id in feature_ids
    }
    base = feature_repositories[feature_ids[0]]
    rows: list[list[float]] = []
    eras: list[int] = []
    weights: list[float] = []
    groups: list[str] = []
    for repository_id, repository in sorted(base.items()):
        if repository.get("language") != language:
            continue
        if repository.get("role") not in CONTROL_ROLES:
            continue
        observations = _observations(repository)
        if len(observations) < 2:
            continue
        for era, period in enumerate((min(observations), max(observations))):
            vector = _feature_vector(
                feature_repositories, feature_ids, repository_id, period
            )
            if vector is None:
                break
            rows.append(vector)
            eras.append(era)
            weights.append(max(float(observations[period].get("change_size", 1)), 1.0))
            groups.append(repository_id)
    return rows, eras, weights, groups


def _language_diagnostics(
    panels: Mapping[str, Mapping[str, Any]], language: str
) -> dict[str, Any]:
    feature_ids = sorted(panels)
    rows, eras, weights, groups = _language_rows(panels, language)
    try:
        result = era_diagnostics(
            np.asarray(rows, dtype=float),
            np.asarray(eras, dtype=float),
            np.asarray(weights, dtype=float),
            np.asarray(groups, dtype=object),
            feature_ids,
        )
    except (TemporalDiagnosticError, ValueError, IndexError) as error:
        return {"status": "failed", "passed": False, "error": str(error)}
    return {
        "status": "reported",
        "passed": True,
        "gate_role": "report_only_no_preregistered_numeric_threshold",
        "repository_held_out_era_prediction": {
            "status": "reported",
            "auc": result["repository_held_out_auc"],
            "repository_groups": result["repository_groups"],
            "folds": result["folds"],
        },
        "featurewise_standardized_mean_shift": {
            "status": "reported",
            "features": result["feature_shifts"],
        },
    }


def build_study_diagnostics(
    panels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Report cross-feature era diagnostics without inventing outcome thresholds."""
    first = next(iter(panels.values()))
    languages = sorted(
        {
            repository["language"]
            for repository in first.get("repositories", [])
            if isinstance(repository, Mapping)
            and isinstance(repository.get("language"), str)
        }
    )
    return {
        "languages": {
            language: _language_diagnostics(panels, language) for language in languages
        },
        "historical_anchor_sensitivity": {
            "status": "deferred_to_exact_authorship_prevalence",
            "passed": None,
            "required_for": "authorship_headline",
        },
    }
