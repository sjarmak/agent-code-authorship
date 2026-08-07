"""Execute all frozen era feature panels with repository-level inference."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from authorship.era_adjustment import estimate_era_adjustment
from authorship.era_study_diagnostics import build_study_diagnostics

FROZEN_MINIMUM_ADOPTERS = 20
FROZEN_MINIMUM_CONTROLS = 10
FROZEN_CROSS_FIT_FOLDS = 5
MATERIALIZATION_VERSION = 1


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "era_study_execution_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _feature_seed(seed: int, feature_id: str) -> int:
    digest = hashlib.sha256(f"{seed}|{feature_id}".encode()).hexdigest()
    return int(digest, 16) % (2**31)


def _validated_panels(
    materialization: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    if materialization.get("status") != "complete":
        raise RuntimeError("era panel materialization is incomplete")
    if (
        materialization.get("era_panel_materialization_version")
        != MATERIALIZATION_VERSION
    ):
        raise RuntimeError("era panel materialization version is invalid")
    if materialization.get("era_panel_materialization_sha256") != _materialization_sha(
        materialization
    ):
        raise RuntimeError("era panel materialization hash is invalid")
    panels = materialization.get("panels")
    if not isinstance(panels, Mapping) or not panels:
        raise RuntimeError("era panel materialization contains no feature panels")
    if any(
        not isinstance(panel, Mapping) or panel.get("feature_id") != feature_id
        for feature_id, panel in panels.items()
    ):
        raise RuntimeError("era feature panel identities are invalid")
    period_results = materialization.get("period_results")
    if (
        not isinstance(period_results, list)
        or materialization.get("feature_count") != len(panels)
        or materialization.get("period_result_count") != len(period_results)
    ):
        raise RuntimeError("era panel materialization declared counts are invalid")
    return panels


def _materialization_sha(materialization: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in materialization.items()
        if key != "era_panel_materialization_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _validate_analysis_parameters(
    analysis_mode: str,
    minimum_adopters: int,
    minimum_controls: int,
    cross_fit_folds: int,
) -> None:
    frozen = (
        minimum_adopters == FROZEN_MINIMUM_ADOPTERS
        and minimum_controls == FROZEN_MINIMUM_CONTROLS
        and cross_fit_folds == FROZEN_CROSS_FIT_FOLDS
    )
    if analysis_mode not in {"headline", "sensitivity"}:
        raise ValueError("analysis_mode must be headline or sensitivity")
    if analysis_mode == "headline" and not frozen:
        raise ValueError("frozen threshold overrides require sensitivity mode")


def _language_summary(features: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    languages = sorted(
        {language for result in features.values() for language in result["languages"]}
    )
    summary = {}
    for language in languages:
        statuses = Counter(
            result["languages"][language]["status"]
            for result in features.values()
            if language in result["languages"]
        )
        summary[language] = {
            "feature_count": sum(statuses.values()),
            "status_counts": dict(sorted(statuses.items())),
            "all_features_identified": statuses.get("identified", 0) == len(features),
        }
    return summary


def estimate_feature_panels(
    materialization: Mapping[str, Any],
    *,
    bootstrap_replicates: int,
    seed: int,
    minimum_adopters: int = 20,
    minimum_controls: int = 10,
    cross_fit_folds: int = 5,
    analysis_mode: str = "headline",
) -> dict[str, Any]:
    """Estimate every frozen feature panel and aggregate headline readiness."""
    _validate_analysis_parameters(
        analysis_mode, minimum_adopters, minimum_controls, cross_fit_folds
    )
    panels = _validated_panels(materialization)
    features = {
        feature_id: estimate_era_adjustment(
            panel,
            minimum_adopters=minimum_adopters,
            minimum_controls=minimum_controls,
            bootstrap_replicates=bootstrap_replicates,
            seed=_feature_seed(seed, feature_id),
            cross_fit_folds=cross_fit_folds,
        )
        for feature_id, panel in sorted(panels.items())
    }
    languages = _language_summary(features)
    diagnostics = build_study_diagnostics(panels)
    document = {
        "era_study_execution_version": 1,
        "analysis_mode": analysis_mode,
        "frozen_parameters_applied": analysis_mode == "headline",
        "era_panel_materialization_sha256": materialization.get(
            "era_panel_materialization_sha256"
        ),
        "bootstrap_replicates": bootstrap_replicates,
        "seed": seed,
        "minimum_adopters": minimum_adopters,
        "minimum_controls": minimum_controls,
        "cross_fit_folds": cross_fit_folds,
        "feature_count": len(features),
        "languages": languages,
        "diagnostics": diagnostics,
        "headline_inference_allowed": analysis_mode == "headline"
        and all(row["all_features_identified"] for row in languages.values())
        and all(result["headline_inference_allowed"] for result in features.values()),
        "features": features,
    }
    return {**document, "era_study_execution_sha256": _sha256(document)}
