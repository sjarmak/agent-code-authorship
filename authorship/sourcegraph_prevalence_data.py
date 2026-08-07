"""Prepare role-disjoint exact units for the frozen prevalence classifier."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from authorship.role_separation import enforce_role_separation

TIER_KEYS = {
    "H1": "H1_attested_human",
    "H2": "H2_policy_human",
    "H3": "H3_contemporary_pre_adoption",
}


class PrevalenceDataError(ValueError):
    """Raised when classifier arrays cannot be built without design drift."""


def _matched_agent_ids(materialization: Mapping[str, Any]) -> set[str]:
    matching = materialization.get("h3_matching", {})
    matches = matching.get("matches", []) if isinstance(matching, Mapping) else []
    identifiers = {
        row.get("agent_hunk_sha256") for row in matches if isinstance(row, Mapping)
    }
    if any(not isinstance(value, str) for value in identifiers):
        raise PrevalenceDataError("H3 match identities are invalid")
    return identifiers


def _contemporary(
    units: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int]:
    retained = []
    excluded = 0
    for unit in units:
        if unit.get("temporal_role") == "historical_diagnostic":
            excluded += 1
        else:
            retained.append(unit)
    return retained, excluded


def _reference_units(
    materialization: Mapping[str, Any], tier: str, language: str
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], int]:
    if tier not in TIER_KEYS:
        raise PrevalenceDataError(f"unknown evidence tier: {tier}")
    if materialization.get("status") != "complete":
        raise PrevalenceDataError("authorship materialization is incomplete")
    agents = list(materialization.get("agent_units", []))
    humans = list(materialization.get("human_units", {}).get(TIER_KEYS[tier], []))
    if tier == "H3":
        matched = _matched_agent_ids(materialization)
        agents = [unit for unit in agents if unit.get("hunk_sha256") in matched]
    agents = [unit for unit in agents if unit.get("language") == language]
    humans = [unit for unit in humans if unit.get("language") == language]
    agents, historical_agents = _contemporary(agents)
    humans, historical_humans = _contemporary(humans)
    agents = [
        unit
        for unit in agents
        if unit.get("authorship_role", "agent") == "agent"
    ]
    humans = [
        unit
        for unit in humans
        if unit.get("authorship_role", "human") == "human"
    ]
    return agents, humans, historical_agents + historical_humans


def _record(
    unit: Mapping[str, Any], features: Sequence[str], *, label: float | None
) -> dict[str, Any]:
    repository = unit.get("repository_id")
    values = unit.get("feature_values")
    if not isinstance(repository, str) or not repository:
        raise PrevalenceDataError("unit repository identity is missing")
    if not isinstance(values, Mapping) or any(name not in values for name in features):
        raise PrevalenceDataError("unit does not contain every frozen feature")
    vector = [float(values[name]) for name in features]
    if not np.all(np.isfinite(vector)):
        raise PrevalenceDataError("unit features must be finite")
    return {
        **dict(unit),
        "repo": repository,
        "label_value": label,
        "v": vector,
    }


def _repository_weights(
    base_weights: Sequence[float], groups: Sequence[str]
) -> np.ndarray:
    totals: dict[str, float] = defaultdict(float)
    for weight, group in zip(base_weights, groups):
        totals[group] += float(weight)
    if any(total <= 0 for total in totals.values()):
        raise PrevalenceDataError("repository weights must be positive")
    return np.asarray(
        [float(weight) / totals[group] for weight, group in zip(base_weights, groups)],
        dtype=float,
    )


def _arrays(
    records: Sequence[Mapping[str, Any]], *, target: bool, weighting: str
) -> dict[str, np.ndarray]:
    groups = np.asarray([record["repo"] for record in records], dtype=object)
    x = np.asarray([record["v"] for record in records], dtype=float)
    base = np.asarray(
        [
            record["weighted_line_count"] if target else record["line_count"]
            for record in records
        ],
        dtype=float,
    )
    weights = base if weighting == "line" else _repository_weights(base, groups)
    result = {"x": x, "weights": weights, "groups": groups}
    if not target:
        result["labels"] = np.asarray(
            [record["label_value"] for record in records], dtype=float
        )
    return result


def _validate_weighting(weighting: str) -> None:
    if weighting not in {"line", "repository"}:
        raise PrevalenceDataError(f"unknown weighting: {weighting}")


def prepare_prevalence_arrays(
    materialization: Mapping[str, Any],
    target_materialization: Mapping[str, Any],
    features: Sequence[str],
    *,
    tier: str,
    language: str,
    weighting: str = "line",
) -> dict[str, Any]:
    """Select one tier/language and construct exact frozen model arrays."""
    _validate_weighting(weighting)
    if target_materialization.get("status") != "complete":
        raise PrevalenceDataError("target materialization is incomplete")
    agents, humans, historical = _reference_units(materialization, tier, language)
    targets = [
        unit
        for unit in target_materialization.get("units", [])
        if unit.get("language") == language
    ]
    reference = [
        *[_record(unit, features, label=1.0) for unit in agents],
        *[_record(unit, features, label=0.0) for unit in humans],
    ]
    target = [_record(unit, features, label=None) for unit in targets]
    filtered, report = enforce_role_separation(
        {"reference": reference, "target": target}
    )
    retained = filtered["reference"]
    agent_count = sum(record["label_value"] == 1.0 for record in retained)
    human_count = len(retained) - agent_count
    return {
        "tier": tier,
        "language": language,
        "weighting": weighting,
        "reference": _arrays(retained, target=False, weighting=weighting),
        "target": _arrays(filtered["target"], target=True, weighting=weighting),
        "counts": {
            "agent_units_before_role_separation": len(agents),
            "human_units_before_role_separation": len(humans),
            "agent_units": agent_count,
            "human_units": human_count,
            "target_units": len(filtered["target"]),
            "historical_reference_units_excluded": historical,
            "agent_repositories": len(
                {record["repo"] for record in retained if record["label_value"] == 1.0}
            ),
            "human_repositories": len(
                {record["repo"] for record in retained if record["label_value"] == 0.0}
            ),
            "target_repositories": len(
                {record["repo"] for record in filtered["target"]}
            ),
        },
        "role_separation": report,
    }
