"""Stable machine-result assembly for era-adjustment language estimates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def language_result(
    contrasts: Sequence[Mapping[str, Any]],
    excluded: Sequence[Mapping[str, Any]],
    multiplicities: Mapping[str, int],
    counts: tuple[int, int, int, int, int],
    reasons: Sequence[str],
    diagnostics: Mapping[str, Mapping[str, Any]],
    effect: float | None,
    placebo_offset: int,
) -> dict[str, Any]:
    """Assemble one content-stable language result."""
    adopter_count, control_count, h2_count, never_count, not_yet_count = counts
    pretrend = diagnostics["parallel_pre_trends"]["mean"]
    placebo = diagnostics["placebo_adoption_dates"]["mean"]
    return {
        "status": "not_identified" if reasons else "identified",
        "failure_reasons": list(reasons),
        "adopter_count": adopter_count,
        "control_count": control_count,
        "h2_ai_ban_control_count": h2_count,
        "never_adopter_control_count": never_count,
        "not_yet_adopter_control_count": not_yet_count,
        "excluded_pretrend_repository_count": sum(
            multiplicities[contrast["repository_id"]] for contrast in excluded
        ),
        "pre_adoption_evidence_tier": "H3",
        "ai_ban_control_evidence_tier": "H2",
        "era_adjusted_effect": effect,
        "pretrend_difference": pretrend,
        "placebo_effect": placebo,
        "placebo_date_offset": placebo_offset,
        "pretrend_test": dict(diagnostics["parallel_pre_trends"]),
        "placebo_test": dict(diagnostics["placebo_adoption_dates"]),
        "diagnostics": {
            name: dict(value) for name, value in sorted(diagnostics.items())
        },
        "authorship_headline_eligible": False,
        "adoption_periods": sorted(
            {contrast["adoption_period"] for contrast in contrasts}
        ),
        "adopter_contrasts": list(contrasts),
    }
