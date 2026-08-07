"""Within-repository era adjustment with contemporaneous untreated controls."""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from authorship.era_diagnostic_gates import adoption_diagnostics
from authorship.era_matching import (
    MATCH_ON,
    MATCHED_CONTROL_COUNT,
    control_roles,
    select_matched_controls,
)
from authorship.era_reporting import language_result

LANGUAGES = frozenset(("Python", "Go"))
ROLES = frozenset(("adopter", "h2_ai_ban_control", "never_adopter_control"))
ESTIMANDS = {
    "whole_repository_adoption_effect": "repository_calendar_period_introduced_code",
}


class EraAdjustmentError(ValueError):
    """The panel cannot identify the preregistered era contrast."""


@dataclass(frozen=True)
class Observation:
    period: int
    feature_value: float
    change_size: float
    code_age_days: float
    path_type_counts: tuple[tuple[str, int], ...]

    @property
    def test_share(self) -> float:
        counts = dict(self.path_type_counts)
        total = sum(counts.values())
        return counts.get("test", 0) / total if total else 0.0


@dataclass(frozen=True)
class RepositoryPanel:
    repository_id: str
    language: str
    role: str
    adoption_period: int | None
    policy_effective_period: int | None
    observations: tuple[Observation, ...]

    def value(self, period: int) -> float | None:
        observation = self.observation(period)
        return observation.feature_value if observation is not None else None

    def observation(self, period: int) -> Observation | None:
        return next(
            (
                observation
                for observation in self.observations
                if observation.period == period
            ),
            None,
        )


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EraAdjustmentError(f"{field} must be a non-empty string")
    return value.strip()


def _period(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EraAdjustmentError(f"{field} must be an integer")
    return value


def _feature_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EraAdjustmentError("feature_value must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise EraAdjustmentError("feature_value must be a finite number")
    return result


def _nonnegative(value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    result = _feature_value(value)
    if result < 0:
        raise EraAdjustmentError(f"{field} must be nonnegative")
    return result


def _path_counts(value: Any) -> tuple[tuple[str, int], ...]:
    if value is None:
        return (("source", 1),)
    if not isinstance(value, Mapping) or not set(value).issubset({"source", "test"}):
        raise EraAdjustmentError("path_type_counts must contain source/test counts")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in value.values()
    ):
        raise EraAdjustmentError("path_type_counts must be nonnegative integers")
    return tuple(sorted(value.items()))


def _parse_observation(record: Mapping[str, Any]) -> Observation:
    period = _period(record.get("period"), "observation period")
    calendar_period = record.get("calendar_period", period)
    if _period(calendar_period, "calendar_period") != period:
        raise EraAdjustmentError("calendar_period must equal observation period")
    return Observation(
        period=period,
        feature_value=_feature_value(record.get("feature_value")),
        change_size=_nonnegative(record.get("change_size"), "change_size", 1.0),
        code_age_days=_nonnegative(record.get("code_age_days"), "code_age_days", 0.0),
        path_type_counts=_path_counts(record.get("path_type_counts")),
    )


def _parse_repository(record: Mapping[str, Any]) -> RepositoryPanel:
    repository_id = _identifier(record.get("repository_id"), "repository_id")
    language = record.get("language")
    role = record.get("role")
    if language not in LANGUAGES:
        raise EraAdjustmentError("language must be Python or Go")
    if role not in ROLES:
        raise EraAdjustmentError("repository role is invalid")
    adoption = record.get("adoption_period")
    policy_effective = record.get("policy_effective_period")
    if role == "adopter":
        adoption = _period(adoption, "adoption_period")
        if policy_effective is not None:
            raise EraAdjustmentError("adopters cannot have a policy effective period")
    elif adoption is not None:
        raise EraAdjustmentError("controls cannot have an adoption period")
    elif role == "h2_ai_ban_control":
        policy_effective = _period(policy_effective, "policy_effective_period")
    elif policy_effective is not None:
        raise EraAdjustmentError("never-adopter controls cannot have policy timing")
    raw_observations = record.get("observations")
    if not isinstance(raw_observations, list) or not raw_observations:
        raise EraAdjustmentError("observations must be a non-empty list")
    observations = tuple(
        _parse_observation(observation) for observation in raw_observations
    )
    periods = [observation.period for observation in observations]
    if len(periods) != len(set(periods)):
        raise EraAdjustmentError("observation periods must be unique")
    return RepositoryPanel(
        repository_id,
        language,
        role,
        adoption,
        policy_effective,
        tuple(sorted(observations, key=lambda observation: observation.period)),
    )


def _parse_panel(document: Mapping[str, Any]) -> tuple[RepositoryPanel, ...]:
    if document.get("panel_version") != 1:
        raise EraAdjustmentError("panel_version must equal 1")
    _identifier(document.get("feature_id"), "feature_id")
    estimand = _identifier(document.get("estimand_id"), "estimand_id")
    if estimand != "whole_repository_adoption_effect":
        raise EraAdjustmentError(
            "this estimator only accepts whole_repository_adoption_effect"
        )
    if ESTIMANDS.get(estimand) != document.get("unit"):
        raise EraAdjustmentError("estimand_id and unit are inconsistent")
    records = document.get("repositories")
    if not isinstance(records, list) or not records:
        raise EraAdjustmentError("repositories must be a non-empty list")
    repositories = tuple(_parse_repository(record) for record in records)
    identifiers = [repository.repository_id for repository in repositories]
    if len(identifiers) != len(set(identifiers)):
        raise EraAdjustmentError("repository IDs must be unique")
    return repositories


def _repository_fold(repository_id: str, fold_count: int) -> int:
    digest = hashlib.sha256(repository_id.encode()).hexdigest()
    return int(digest, 16) % fold_count


def _untreated_for_contrast(
    candidate: RepositoryPanel,
    *,
    pre_period: int,
    post_period: int,
) -> bool:
    if candidate.role == "never_adopter_control":
        return True
    if candidate.role == "h2_ai_ban_control":
        return (
            candidate.policy_effective_period is not None
            and candidate.policy_effective_period <= pre_period
        )
    return (
        candidate.adoption_period is not None
        and candidate.adoption_period > post_period
    )


def _eligible_controls(
    adopter: RepositoryPanel,
    repositories: Sequence[RepositoryPanel],
    *,
    pre_period: int,
    post_period: int,
    multiplicities: Mapping[str, int],
    cross_fit_folds: int,
) -> list[RepositoryPanel]:
    controls = []
    for candidate in repositories:
        if candidate.repository_id == adopter.repository_id:
            continue
        if candidate.language != adopter.language:
            continue
        if multiplicities.get(candidate.repository_id, 0) < 1:
            continue
        same_fold = _repository_fold(
            candidate.repository_id, cross_fit_folds
        ) == _repository_fold(adopter.repository_id, cross_fit_folds)
        if cross_fit_folds > 1 and same_fold:
            continue
        untreated = _untreated_for_contrast(
            candidate, pre_period=pre_period, post_period=post_period
        )
        if (
            untreated
            and candidate.value(pre_period) is not None
            and candidate.value(post_period) is not None
        ):
            controls.append(candidate)
    return sorted(controls, key=lambda repository: repository.repository_id)


def _weighted_mean(
    values: Sequence[tuple[float, int]],
) -> float:
    total_weight = sum(weight for _, weight in values)
    if not total_weight:
        raise EraAdjustmentError("weighted mean requires positive total weight")
    return sum(value * weight for value, weight in values) / total_weight


def _control_change(
    controls: Sequence[RepositoryPanel],
    pre_period: int,
    post_period: int,
    multiplicities: Mapping[str, int],
) -> float:
    return _weighted_mean(
        [
            (
                control.value(post_period) - control.value(pre_period),
                multiplicities[control.repository_id],
            )
            for control in controls
        ]
    )


def _preperiod_difference(
    adopter: RepositoryPanel,
    controls: Sequence[RepositoryPanel],
    *,
    start_period: int,
    end_period: int,
    multiplicities: Mapping[str, int],
) -> float | None:
    adopter_start = adopter.value(start_period)
    adopter_end = adopter.value(end_period)
    eligible = [
        control
        for control in controls
        if control.value(start_period) is not None
        and control.value(end_period) is not None
    ]
    if adopter_start is None or adopter_end is None or not eligible:
        return None
    control_change = _control_change(eligible, start_period, end_period, multiplicities)
    return (adopter_end - adopter_start) - control_change


def _contrast_record(
    adopter: RepositoryPanel,
    controls: Sequence[RepositoryPanel],
    periods: Mapping[str, int],
    changes: Mapping[str, float],
    matching_distances: Mapping[str, float],
    multiplicities: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "repository_id": adopter.repository_id,
        "cross_fit_fold": _repository_fold(
            adopter.repository_id, periods["cross_fit_folds"]
        ),
        "adoption_period": periods["adoption"],
        "pre_period": periods["pre"],
        "post_period": periods["post"],
        "treated_change": changes["treated"],
        "control_change": changes["control"],
        "contrast": changes["treated"] - changes["control"],
        "control_repository_ids": [control.repository_id for control in controls],
        "control_roles": control_roles(controls),
        "matching": {
            "method": "nearest_repository_period_covariates",
            "ratio": MATCHED_CONTROL_COUNT,
            "match_on": list(MATCH_ON),
            "distances": dict(matching_distances),
        },
        "parallel_pretrend_difference": _preperiod_difference(
            adopter,
            controls,
            start_period=periods["pretrend"],
            end_period=periods["placebo"],
            multiplicities=multiplicities,
        ),
        "placebo_difference": _preperiod_difference(
            adopter,
            controls,
            start_period=periods["placebo"],
            end_period=periods["pre"],
            multiplicities=multiplicities,
        ),
    }


def _adopter_contrast(
    adopter: RepositoryPanel,
    repositories: Sequence[RepositoryPanel],
    multiplicities: Mapping[str, int],
    offsets: Mapping[str, int],
) -> dict[str, Any] | None:
    adoption = adopter.adoption_period
    if adoption is None:
        return None
    cross_fit_folds = offsets["cross_fit_folds"]
    pre_period = adoption + offsets["pre_offset"]
    post_period = adoption + offsets["post_offset"]
    adopter_pre = adopter.value(pre_period)
    adopter_post = adopter.value(post_period)
    if adopter_pre is None or adopter_post is None:
        return None
    controls = _eligible_controls(
        adopter,
        repositories,
        pre_period=pre_period,
        post_period=post_period,
        multiplicities=multiplicities,
        cross_fit_folds=cross_fit_folds,
    )
    if not controls:
        return None
    controls, matching_distances = select_matched_controls(
        adopter, controls, pre_period, post_period
    )
    control_change = _control_change(controls, pre_period, post_period, multiplicities)
    treated_change = adopter_post - adopter_pre
    periods = {
        "adoption": adoption,
        "pre": pre_period,
        "post": post_period,
        "pretrend": adoption + offsets["pretrend_offset"],
        "placebo": adoption + offsets["placebo_offset"],
        "cross_fit_folds": cross_fit_folds,
    }
    changes = {"treated": treated_change, "control": control_change}
    return _contrast_record(
        adopter, controls, periods, changes, matching_distances, multiplicities
    )


def _failure_reasons(
    adopter_count: int,
    control_count: int,
    minimum_adopters: int,
    minimum_controls: int,
) -> list[str]:
    reasons = []
    if adopter_count < minimum_adopters:
        reasons.append(f"fewer than {minimum_adopters} adopters")
    if control_count < minimum_controls:
        reasons.append(f"fewer than {minimum_controls} controls")
    return reasons


def _language_contrasts(
    language: str,
    repositories: Sequence[RepositoryPanel],
    multiplicities: Mapping[str, int],
    **offsets: int,
) -> list[dict[str, Any]]:
    adopters = sorted(
        (
            repository
            for repository in repositories
            if repository.language == language
            and repository.role == "adopter"
            and multiplicities.get(repository.repository_id, 0) > 0
        ),
        key=lambda item: item.repository_id,
    )
    return [
        contrast
        for adopter in adopters
        if (
            contrast := _adopter_contrast(
                adopter, repositories, multiplicities, offsets
            )
        )
        is not None
    ]


def _contrast_counts(
    contrasts: Sequence[Mapping[str, Any]],
    multiplicities: Mapping[str, int],
) -> tuple[int, int, int, int, int]:
    adopter_count = sum(
        multiplicities[contrast["repository_id"]] for contrast in contrasts
    )
    control_ids = {
        identifier
        for contrast in contrasts
        for identifier in contrast["control_repository_ids"]
    }
    h2_ids = {
        identifier
        for contrast in contrasts
        for identifier in contrast["control_roles"]["h2_ai_ban_control"]
    }
    never_ids = {
        identifier
        for contrast in contrasts
        for identifier in contrast["control_roles"]["never_adopter_control"]
    }
    not_yet_ids = {
        identifier
        for contrast in contrasts
        for identifier in contrast["control_roles"]["not_yet_adopter"]
    }
    return (
        adopter_count,
        sum(multiplicities[identifier] for identifier in control_ids),
        sum(multiplicities[identifier] for identifier in h2_ids),
        sum(multiplicities[identifier] for identifier in never_ids),
        sum(multiplicities[identifier] for identifier in not_yet_ids),
    )


def _identified_effects(
    contrasts: Sequence[Mapping[str, Any]],
    multiplicities: Mapping[str, int],
) -> tuple[float, float | None]:
    effect = _weighted_mean(
        [
            (contrast["contrast"], multiplicities[contrast["repository_id"]])
            for contrast in contrasts
        ]
    )
    pretrends = [
        (
            contrast["parallel_pretrend_difference"],
            multiplicities[contrast["repository_id"]],
        )
        for contrast in contrasts
        if contrast["parallel_pretrend_difference"] is not None
    ]
    return effect, _weighted_mean(pretrends) if pretrends else None


def _split_diagnostic_contrasts(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    def is_complete(contrast: Mapping[str, Any]) -> bool:
        return (
            contrast["parallel_pretrend_difference"] is not None
            and contrast["placebo_difference"] is not None
        )

    complete = [contrast for contrast in candidates if is_complete(contrast)]
    excluded = [contrast for contrast in candidates if not is_complete(contrast)]
    return complete, excluded


def _language_estimate(
    language: str,
    repositories: Sequence[RepositoryPanel],
    multiplicities: Mapping[str, int],
    options: Mapping[str, int],
) -> dict[str, Any]:
    candidates = _language_contrasts(language, repositories, multiplicities, **options)
    contrasts, excluded = _split_diagnostic_contrasts(candidates)
    adopter_count, control_count, h2_count, never_count, not_yet_count = (
        _contrast_counts(contrasts, multiplicities)
    )
    reasons = _failure_reasons(
        adopter_count,
        control_count,
        options["minimum_adopters"],
        options["minimum_controls"],
    )
    if excluded and not contrasts:
        reasons.append("pretrend diagnostic unavailable")
    diagnostics = adoption_diagnostics(contrasts, multiplicities)
    if not diagnostics["parallel_pre_trends"]["passed"]:
        reasons.append("pretrend test rejected parallel trends")
    if not diagnostics["placebo_adoption_dates"]["passed"]:
        reasons.append("placebo diagnostic failed")
    if not diagnostics["calendar_time_balance"]["passed"]:
        reasons.append("calendar-time balance unavailable")
    effect = None
    if contrasts and not reasons:
        effect, _ = _identified_effects(contrasts, multiplicities)
    counts = adopter_count, control_count, h2_count, never_count, not_yet_count
    return language_result(
        contrasts,
        excluded,
        multiplicities,
        counts,
        reasons,
        diagnostics,
        effect,
        options["placebo_offset"],
    )


def _bootstrap_draws(
    repositories: Sequence[RepositoryPanel], *, replicates: int, seed: int
) -> list[dict[str, int]]:
    strata: dict[tuple[str, str], list[str]] = defaultdict(list)
    for repository in repositories:
        strata[(repository.language, repository.role)].append(repository.repository_id)
    generator = random.Random(seed)
    draws = []
    for _ in range(replicates):
        draw = Counter()
        for identifiers in strata.values():
            ordered = sorted(identifiers)
            draw.update(generator.choice(ordered) for _ in ordered)
        draws.append(dict(draw))
    return draws


def _interval(values: Sequence[float], confidence_level: float) -> dict[str, float]:
    alpha = (1.0 - confidence_level) / 2.0
    lower, upper = np.quantile(values, [alpha, 1.0 - alpha])
    return {"lower": float(lower), "upper": float(upper)}


def _validate_options(
    *,
    minimum_adopters: int,
    minimum_controls: int,
    bootstrap_replicates: int,
    cross_fit_folds: int,
    confidence_level: float,
) -> None:
    for value, name in (
        (minimum_adopters, "minimum_adopters"),
        (minimum_controls, "minimum_controls"),
        (bootstrap_replicates, "bootstrap_replicates"),
        (cross_fit_folds, "cross_fit_folds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EraAdjustmentError(f"{name} must be a positive integer")
    if not 0.0 < confidence_level < 1.0:
        raise EraAdjustmentError("confidence_level must be between zero and one")


def _estimate_languages(
    languages: Sequence[str],
    repositories: Sequence[RepositoryPanel],
    multiplicities: Mapping[str, int],
    options: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    return {
        language: _language_estimate(language, repositories, multiplicities, options)
        for language in languages
    }


def _bootstrap_intervals(
    languages: Sequence[str],
    estimates: Sequence[Mapping[str, float | None]],
    confidence_level: float,
) -> dict[str, dict[str, Any]]:
    intervals = {}
    for language in languages:
        values = [
            draw[language]
            for draw in estimates
            if draw[language] is not None
        ]
        intervals[language] = {
            "identified_replicates": len(values),
            "effect_interval": (
                _interval(values, confidence_level) if values else None
            ),
        }
    return intervals


def _bootstrap_effects(
    languages: Sequence[str],
    repositories: Sequence[RepositoryPanel],
    multiplicities: Mapping[str, int],
    options: Mapping[str, int],
) -> dict[str, float | None]:
    effects = {}
    for language in languages:
        candidates = _language_contrasts(
            language, repositories, multiplicities, **options
        )
        contrasts, _ = _split_diagnostic_contrasts(candidates)
        effects[language] = (
            _identified_effects(contrasts, multiplicities)[0] if contrasts else None
        )
    return effects


def _estimation_options(
    *,
    minimum_adopters: int,
    minimum_controls: int,
    pre_offset: int,
    post_offset: int,
    pretrend_offset: int,
    placebo_offset: int,
    bootstrap_replicates: int,
    cross_fit_folds: int,
    confidence_level: float,
) -> dict[str, int]:
    _validate_options(
        minimum_adopters=minimum_adopters,
        minimum_controls=minimum_controls,
        bootstrap_replicates=bootstrap_replicates,
        cross_fit_folds=cross_fit_folds,
        confidence_level=confidence_level,
    )
    return {
        "minimum_adopters": minimum_adopters,
        "minimum_controls": minimum_controls,
        "pre_offset": pre_offset,
        "post_offset": post_offset,
        "pretrend_offset": pretrend_offset,
        "placebo_offset": placebo_offset,
        "cross_fit_folds": cross_fit_folds,
    }


def _evidence_strata(
    repositories: Sequence[RepositoryPanel],
) -> dict[str, dict[str, Any]]:
    return {
        "H1": {"status": "not_available", "repository_count": 0},
        "H2": {
            "status": "available",
            "repository_count": sum(
                repository.role == "h2_ai_ban_control" for repository in repositories
            ),
        },
        "H3": {
            "status": "available",
            "repository_count": sum(
                repository.role == "adopter" for repository in repositories
            ),
        },
    }


def _bootstrap_summary(
    languages: Sequence[str],
    repositories: Sequence[RepositoryPanel],
    options: Mapping[str, int],
    *,
    replicates: int,
    seed: int,
    confidence_level: float,
) -> dict[str, Any]:
    draws = _bootstrap_draws(repositories, replicates=replicates, seed=seed)
    estimates = [
        _bootstrap_effects(languages, repositories, draw, options) for draw in draws
    ]
    return {
        "unit": "repository",
        "stratified_by": ["language", "role"],
        "replicates": replicates,
        "seed": seed,
        "confidence_level": confidence_level,
        "languages": _bootstrap_intervals(languages, estimates, confidence_level),
    }


def _result_document(
    document: Mapping[str, Any],
    repositories: Sequence[RepositoryPanel],
    estimates: Mapping[str, Mapping[str, Any]],
    bootstrap: Mapping[str, Any],
    cross_fit_folds: int,
) -> dict[str, Any]:
    identified = all(result["status"] == "identified" for result in estimates.values())
    authorship_ready = all(
        result["authorship_headline_eligible"] for result in estimates.values()
    )
    return {
        "estimator_version": 1,
        "feature_id": document["feature_id"],
        "estimand": {
            "id": document["estimand_id"],
            "unit": document["unit"],
            "authorship_claim": False,
        },
        "status": "identified" if identified else "not_identified",
        "languages": estimates,
        "headline_inference_allowed": identified and authorship_ready,
        "cross_fitting": {
            "unit": "repository",
            "fold_count": cross_fit_folds,
            "fold_assignment": "sha256_repository_id_modulo_fold_count",
            "same_fold_controls_excluded": cross_fit_folds > 1,
        },
        "evidence_strata": _evidence_strata(repositories),
        "bootstrap": bootstrap,
    }


def estimate_era_adjustment(
    document: Mapping[str, Any],
    *,
    minimum_adopters: int = 20,
    minimum_controls: int = 10,
    pre_offset: int = -1,
    post_offset: int = 1,
    pretrend_offset: int = -3,
    placebo_offset: int = -2,
    cross_fit_folds: int = 1,
    bootstrap_replicates: int,
    seed: int,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Estimate feature shift net of repository and contemporaneous era effects."""
    options = _estimation_options(
        minimum_adopters=minimum_adopters,
        minimum_controls=minimum_controls,
        pre_offset=pre_offset,
        post_offset=post_offset,
        pretrend_offset=pretrend_offset,
        placebo_offset=placebo_offset,
        bootstrap_replicates=bootstrap_replicates,
        cross_fit_folds=cross_fit_folds,
        confidence_level=confidence_level,
    )
    repositories = _parse_panel(document)
    languages = sorted({repository.language for repository in repositories})
    multiplicities = {repository.repository_id: 1 for repository in repositories}
    estimates = _estimate_languages(languages, repositories, multiplicities, options)
    bootstrap = _bootstrap_summary(
        languages,
        repositories,
        options,
        replicates=bootstrap_replicates,
        seed=seed,
        confidence_level=confidence_level,
    )
    return _result_document(
        document, repositories, estimates, bootstrap, cross_fit_folds
    )
