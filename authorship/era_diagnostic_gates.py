"""Fail-closed diagnostic gates for repository-adoption event studies."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _t_critical(degrees_of_freedom: int) -> float:
    z = 1.959963984540054
    inverse = 1.0 / degrees_of_freedom
    first = (z**3 + z) * inverse / 4.0
    second = (5 * z**5 + 16 * z**3 + 3 * z) * inverse**2 / 96.0
    third = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) * inverse**3 / 384.0
    return z + first + second + third


def mean_zero_test(
    contrasts: Sequence[Mapping[str, Any]],
    multiplicities: Mapping[str, int],
    field: str,
) -> dict[str, Any]:
    """Test one repository-level diagnostic contrast against a zero mean."""
    values = [
        contrast[field]
        for contrast in contrasts
        if contrast[field] is not None
        for _ in range(multiplicities[contrast["repository_id"]])
    ]
    mean = sum(values) / len(values) if values else None
    if len(values) < 2:
        passed = mean is not None and abs(mean) <= 1e-12
        return {
            "status": "passed" if passed else "failed_or_unavailable",
            "passed": passed,
            "repository_count": len(values),
            "mean": mean,
            "test_statistic": None,
            "critical_value": None,
        }
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    statistic = math.inf if variance == 0 and mean != 0 else 0.0
    if variance > 0:
        statistic = abs(mean) / math.sqrt(variance / len(values))
    critical = _t_critical(len(values) - 1)
    passed = statistic <= critical
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "repository_count": len(values),
        "mean": mean,
        "test_statistic": statistic,
        "critical_value": critical,
    }


def adoption_diagnostics(
    contrasts: Sequence[Mapping[str, Any]],
    multiplicities: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    """Assemble independent event-study and explicitly deferred authorship gates."""
    parallel = mean_zero_test(contrasts, multiplicities, "parallel_pretrend_difference")
    placebo = mean_zero_test(contrasts, multiplicities, "placebo_difference")
    calendar = {
        "status": "passed" if contrasts else "failed_or_unavailable",
        "passed": bool(contrasts),
        "method": "same_calendar_period_exact_match",
        "repository_count": sum(
            multiplicities[contrast["repository_id"]] for contrast in contrasts
        ),
    }
    deferred = {
        "status": "deferred_to_exact_authorship_prevalence",
        "passed": None,
        "required_for": "authorship_headline",
    }
    return {
        "parallel_pre_trends": parallel,
        "placebo_adoption_dates": placebo,
        "calendar_time_balance": calendar,
        "repository_held_out_era_prediction": dict(deferred),
        "featurewise_standardized_mean_shift": dict(deferred),
        "historical_anchor_sensitivity": dict(deferred),
    }
