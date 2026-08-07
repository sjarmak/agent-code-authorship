"""Prospective precision simulation for repository-paired event studies."""

from __future__ import annotations

import math
import random
from typing import Any

SIMULATION_EFFECT = 0.7
SIMULATION_CORRELATION = 0.5
SIMULATION_TARGET_POWER = 0.8
NORMAL_CRITICAL_VALUE = 1.959963984540054


class CohortPrecisionError(ValueError):
    """The prospective precision design is structurally invalid."""


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CohortPrecisionError(f"{label} must be a positive integer")
    return value


def _simulation_power(
    repository_count: int,
    standardized_effect: float,
    within_pair_correlation: float,
    replicates: int,
    seed: int,
) -> float:
    generator = random.Random(f"{seed}:{repository_count}")
    noise_scale = math.sqrt(2.0 * (1.0 - within_pair_correlation))
    critical_value = _student_t_critical(repository_count - 1)
    detected = 0
    for _ in range(replicates):
        values = [
            standardized_effect + noise_scale * generator.normalvariate(0.0, 1.0)
            for _ in range(repository_count)
        ]
        mean = sum(values) / repository_count
        variance = sum((value - mean) ** 2 for value in values) / (repository_count - 1)
        statistic = mean / math.sqrt(variance / repository_count)
        detected += abs(statistic) >= critical_value
    return detected / replicates


def _student_t_critical(degrees_of_freedom: int) -> float:
    """Approximate the two-sided 5% Student-t critical value."""
    inverse = 1.0 / degrees_of_freedom
    z = NORMAL_CRITICAL_VALUE
    first = (z**3 + z) * inverse / 4.0
    second = (5 * z**5 + 16 * z**3 + 3 * z) * inverse**2 / 96.0
    third = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) * inverse**3 / 384.0
    return z + first + second + third


def _validate_options(
    minimum_repositories: int,
    maximum_repositories: int,
    standardized_effect: float,
    within_pair_correlation: float,
    target_power: float,
    replicates: int,
) -> tuple[int, int, int]:
    minimum = _positive_integer(minimum_repositories, "minimum_repositories")
    maximum = _positive_integer(maximum_repositories, "maximum_repositories")
    repetitions = _positive_integer(replicates, "replicates")
    if minimum < 2:
        raise CohortPrecisionError("minimum_repositories must be at least two")
    if maximum < minimum:
        raise CohortPrecisionError("maximum_repositories precedes minimum_repositories")
    if not 0.0 < standardized_effect or not 0.0 <= within_pair_correlation < 1.0:
        raise CohortPrecisionError("simulation effect or correlation is invalid")
    if not 0.0 < target_power < 1.0:
        raise CohortPrecisionError("target_power must be between zero and one")
    return minimum, maximum, repetitions


def _power_row(
    count: int,
    standardized_effect: float,
    within_pair_correlation: float,
    repetitions: int,
    seed: int,
) -> dict[str, float | int]:
    return {
        "repository_count": count,
        "critical_value": _student_t_critical(count - 1),
        "power": _simulation_power(
            count,
            standardized_effect,
            within_pair_correlation,
            repetitions,
            seed,
        ),
    }


def simulate_paired_repository_power(
    *,
    minimum_repositories: int,
    maximum_repositories: int,
    standardized_effect: float,
    within_pair_correlation: float,
    target_power: float,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    """Run a prospective paired-repository Monte Carlo power simulation."""
    minimum, maximum, repetitions = _validate_options(
        minimum_repositories,
        maximum_repositories,
        standardized_effect,
        within_pair_correlation,
        target_power,
        replicates,
    )
    rows = [
        _power_row(
            count,
            standardized_effect,
            within_pair_correlation,
            repetitions,
            seed,
        )
        for count in range(minimum, maximum + 1)
    ]
    selected = next(
        (row["repository_count"] for row in rows if row["power"] >= target_power),
        None,
    )
    return {
        "method": "paired_repository_monte_carlo",
        "test_statistic": "paired_student_t_two_sided",
        "alpha": 0.05,
        "standardized_effect": standardized_effect,
        "within_pair_correlation": within_pair_correlation,
        "target_power": target_power,
        "replicates": repetitions,
        "seed": seed,
        "power_by_repository_count": rows,
        "selected_minimum_repositories": selected,
        "outcomes_consulted": False,
    }
