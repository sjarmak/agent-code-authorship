"""Censoring-correct survival estimators for immutable line event histories."""

from __future__ import annotations

import math
import random
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

STATES = ("unchanged", "modified", "deleted")
ALLOWED_TRANSITIONS = {
    "unchanged": frozenset(("modified", "deleted")),
    "modified": frozenset(("deleted",)),
    "deleted": frozenset(),
}


class SurvivalContractError(ValueError):
    """The line-event document cannot identify the preregistered estimand."""


@dataclass(frozen=True)
class Transition:
    time_days: float
    from_state: str
    to_state: str


@dataclass(frozen=True)
class LineHistory:
    repository_id: str
    line_id: str
    censor_time_days: float
    transitions: tuple[Transition, ...]


@dataclass(frozen=True)
class EventArrays:
    repository_ids: tuple[str, ...]
    repository_line_counts: np.ndarray
    times: np.ndarray
    risk_unchanged: np.ndarray
    risk_modified: np.ndarray
    unchanged_to_modified: np.ndarray
    unchanged_to_deleted: np.ndarray
    modified_to_deleted: np.ndarray


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SurvivalContractError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SurvivalContractError(f"{field} must be a finite number")
    return result


def _parse_transition(
    record: Mapping[str, Any],
    *,
    current_state: str,
    previous_time: float,
    censor_time: float,
) -> Transition:
    time = _number(record.get("time_days"), "transition time_days")
    from_state = record.get("from_state")
    to_state = record.get("to_state")
    if time < 0 or time > censor_time:
        raise SurvivalContractError("transition time must be within observation")
    if time <= previous_time:
        raise SurvivalContractError("transition times must be strictly increasing")
    if from_state != current_state:
        raise SurvivalContractError("transition from_state does not match prior state")
    if to_state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
        raise SurvivalContractError(
            f"transition {current_state!r} -> {to_state!r} is not allowed"
        )
    return Transition(time, from_state, to_state)


def _parse_line(record: Mapping[str, Any]) -> LineHistory:
    repository_id = record.get("repository_id")
    line_id = record.get("line_id")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise SurvivalContractError("repository_id must be a non-empty string")
    if not isinstance(line_id, str) or not line_id.strip():
        raise SurvivalContractError("line_id must be a non-empty string")
    censor_time = _number(record.get("censor_time_days"), "censor_time_days")
    if censor_time < 0:
        raise SurvivalContractError("censor_time_days must be non-negative")
    records = record.get("transitions")
    if not isinstance(records, list):
        raise SurvivalContractError("transitions must be a list")
    transitions: list[Transition] = []
    current_state = "unchanged"
    previous_time = -1.0
    for transition_record in records:
        if not isinstance(transition_record, Mapping):
            raise SurvivalContractError("each transition must be an object")
        transition = _parse_transition(
            transition_record,
            current_state=current_state,
            previous_time=previous_time,
            censor_time=censor_time,
        )
        transitions.append(transition)
        current_state = transition.to_state
        previous_time = transition.time_days
    return LineHistory(
        repository_id.strip(), line_id.strip(), censor_time, tuple(transitions)
    )


def _parse_document(document: Mapping[str, Any]) -> tuple[LineHistory, ...]:
    if document.get("contract_version") != 1:
        raise SurvivalContractError("contract_version must equal 1")
    records = document.get("lines")
    if not isinstance(records, list) or not records:
        raise SurvivalContractError("lines must be a non-empty list")
    lines = tuple(_parse_line(record) for record in records)
    identities = [(line.repository_id, line.line_id) for line in lines]
    if len(identities) != len(set(identities)):
        raise SurvivalContractError("line IDs must be unique within each repository")
    return lines


def _horizons(values: Sequence[int | float]) -> tuple[float, ...]:
    horizons = tuple(_number(value, "horizon") for value in values)
    if not horizons or any(value < 0 for value in horizons):
        raise SurvivalContractError("horizons must be non-empty and non-negative")
    if any(left >= right for left, right in zip(horizons, horizons[1:])):
        raise SurvivalContractError("horizons must be strictly increasing")
    return horizons


def _weighted_lines(
    lines: Sequence[LineHistory],
    weighting: str,
    repository_multiplicities: Mapping[str, int] | None,
) -> list[tuple[LineHistory, float, int]]:
    if weighting not in {"repository", "line"}:
        raise SurvivalContractError("weighting must be 'repository' or 'line'")
    counts = Counter(line.repository_id for line in lines)
    multiplicities = repository_multiplicities or {
        repository_id: 1 for repository_id in counts
    }
    weighted = []
    for line in lines:
        copies = multiplicities.get(line.repository_id, 0)
        if copies < 0:
            raise SurvivalContractError("repository multiplicities cannot be negative")
        if not copies:
            continue
        weight = float(copies)
        if weighting == "repository":
            weight /= counts[line.repository_id]
        weighted.append((line, weight, copies))
    return weighted


def _deletion_time(line: LineHistory) -> float | None:
    return next(
        (
            transition.time_days
            for transition in line.transitions
            if transition.to_state == "deleted"
        ),
        None,
    )


def _risk_suffix(
    weighted: Sequence[tuple[LineHistory, float, int]],
) -> tuple[list[float], list[float], list[int]]:
    endings = sorted(
        (
            min(
                line.censor_time_days,
                deletion if deletion is not None else math.inf,
            ),
            weight,
            copies,
        )
        for line, weight, copies in weighted
        for deletion in (_deletion_time(line),)
    )
    times = [ending[0] for ending in endings]
    weights = [0.0] * (len(endings) + 1)
    copies = [0] * (len(endings) + 1)
    for index in range(len(endings) - 1, -1, -1):
        weights[index] = weights[index + 1] + endings[index][1]
        copies[index] = copies[index + 1] + endings[index][2]
    return times, weights, copies


def _risk_at(
    suffix: tuple[list[float], list[float], list[int]], time: float
) -> tuple[float, int]:
    times, weights, copies = suffix
    index = bisect_left(times, time)
    return weights[index], copies[index]


def _kaplan_meier(
    weighted: Sequence[tuple[LineHistory, float, int]],
    horizons: Sequence[float],
) -> list[dict[str, Any]]:
    deleted_weights = Counter()
    for line, weight, _ in weighted:
        deletion = _deletion_time(line)
        if deletion is not None and deletion <= horizons[-1]:
            deleted_weights[deletion] += weight
    deletion_times = sorted(deleted_weights)
    risk_suffix = _risk_suffix(weighted)
    survival = 1.0
    event_index = 0
    points = []
    for horizon in horizons:
        while event_index < len(deletion_times):
            event_time = deletion_times[event_index]
            if event_time > horizon:
                break
            risk_weight, _ = _risk_at(risk_suffix, event_time)
            deleted_weight = deleted_weights[event_time]
            if risk_weight > 0:
                survival *= 1.0 - (deleted_weight / risk_weight)
            event_index += 1
        risk_weight, risk_lines = _risk_at(risk_suffix, horizon)
        points.append(
            {
                "horizon_days": horizon,
                "survival": float(survival),
                "risk_set_weight": float(risk_weight),
                "risk_set_lines": risk_lines,
            }
        )
    return points


def _state_before(line: LineHistory, time: float) -> str:
    state = "unchanged"
    for transition in line.transitions:
        if transition.time_days >= time:
            break
        state = transition.to_state
    return state


def _aj_risk_set(
    weighted: Sequence[tuple[LineHistory, float, int]], horizon: float
) -> dict[str, dict[str, float | int]]:
    result = {state: {"weight": 0.0, "lines": 0} for state in ("unchanged", "modified")}
    for line, weight, copies in weighted:
        state = _state_before(line, horizon)
        if line.censor_time_days >= horizon and state != "deleted":
            result[state]["weight"] += weight
            result[state]["lines"] += copies
    return result


def _transition_batches(
    weighted: Sequence[tuple[LineHistory, float, int]], maximum: float
) -> dict[float, list[tuple[int, Transition, float]]]:
    batches: dict[float, list[tuple[int, Transition, float]]] = defaultdict(list)
    for index, (line, weight, _) in enumerate(weighted):
        for transition in line.transitions:
            if transition.time_days <= maximum:
                batches[transition.time_days].append((index, transition, weight))
    return dict(batches)


def _event_hazards(
    occupancy: Mapping[str, float],
    risk: Mapping[str, float],
    batch: Sequence[tuple[int, Transition, float]],
) -> dict[str, float]:
    event_weights = Counter()
    for _, transition, weight in batch:
        event_weights[(transition.from_state, transition.to_state)] += weight
    updated = dict(occupancy)
    for source in ("unchanged", "modified"):
        if not risk[source]:
            continue
        for target in ALLOWED_TRANSITIONS[source]:
            if not event_weights[(source, target)]:
                continue
            probability = event_weights[(source, target)] / risk[source]
            mass = occupancy[source] * probability
            updated[source] -= mass
            updated[target] += mass
    return updated


def _remove_prior_censoring(
    censoring: Sequence[tuple[float, int]],
    censor_index: int,
    event_time: float,
    current_states: Mapping[int, str],
    weighted: Sequence[tuple[LineHistory, float, int]],
    risk: Counter[str],
) -> int:
    while censor_index < len(censoring) and censoring[censor_index][0] < event_time:
        _, line_index = censoring[censor_index]
        state = current_states[line_index]
        if state != "deleted":
            risk[state] -= weighted[line_index][1]
        censor_index += 1
    return censor_index


def _apply_state_changes(
    batch: Sequence[tuple[int, Transition, float]],
    current_states: dict[int, str],
    risk: Counter[str],
) -> None:
    for line_index, transition, weight in batch:
        risk[transition.from_state] -= weight
        if transition.to_state != "deleted":
            risk[transition.to_state] += weight
        current_states[line_index] = transition.to_state


def _aalen_johansen(
    weighted: Sequence[tuple[LineHistory, float, int]],
    horizons: Sequence[float],
) -> list[dict[str, Any]]:
    batches = _transition_batches(weighted, horizons[-1])
    event_times = sorted(batches)
    current_states = {index: "unchanged" for index in range(len(weighted))}
    occupancy = {"unchanged": 1.0, "modified": 0.0, "deleted": 0.0}
    risk = Counter({"unchanged": sum(weight for _, weight, _ in weighted)})
    censoring = sorted(
        (line.censor_time_days, index) for index, (line, _, _) in enumerate(weighted)
    )
    censor_index = 0
    event_index = 0
    points = []
    for horizon in horizons:
        while event_index < len(event_times) and event_times[event_index] <= horizon:
            event_time = event_times[event_index]
            censor_index = _remove_prior_censoring(
                censoring,
                censor_index,
                event_time,
                current_states,
                weighted,
                risk,
            )
            occupancy = _event_hazards(occupancy, risk, batches[event_time])
            _apply_state_changes(batches[event_time], current_states, risk)
            event_index += 1
        points.append(
            {
                "horizon_days": horizon,
                "state_occupancy": {state: float(occupancy[state]) for state in STATES},
                "risk_set_by_state": _aj_risk_set(weighted, horizon),
            }
        )
    return points


def _estimate_lines(
    lines: Sequence[LineHistory],
    horizons: Sequence[float],
    weighting: str,
    multiplicities: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    weighted = _weighted_lines(lines, weighting, multiplicities)
    if not weighted:
        raise SurvivalContractError("at least one repository must be sampled")
    return {
        "weighting": weighting,
        "kaplan_meier": _kaplan_meier(weighted, horizons),
        "aalen_johansen": _aalen_johansen(weighted, horizons),
    }


def estimate_weighting(
    document: Mapping[str, Any],
    *,
    horizons_days: Sequence[int | float],
    weighting: str,
) -> dict[str, Any]:
    """Estimate one weighting without consulting any study outcome artifact."""
    return _estimate_lines(
        _parse_document(document), _horizons(horizons_days), weighting
    )


def repository_bootstrap_draws(
    repository_ids: Sequence[str], *, replicates: int, seed: int
) -> list[dict[str, int]]:
    """Return deterministic whole-repository resamples with replacement."""
    if len(repository_ids) != len(set(repository_ids)):
        raise SurvivalContractError("bootstrap repository IDs must be unique")
    repositories = tuple(sorted(repository_ids))
    if not repositories:
        raise SurvivalContractError("bootstrap requires at least one repository")
    if (
        isinstance(replicates, bool)
        or not isinstance(replicates, int)
        or replicates < 1
    ):
        raise SurvivalContractError("bootstrap replicates must be a positive integer")
    generator = random.Random(seed)
    return [
        dict(Counter(generator.choice(repositories) for _ in repositories))
        for _ in range(replicates)
    ]


def _transition_groups(
    lines: Sequence[LineHistory], maximum: float
) -> dict[float, list[tuple[int, Transition]]]:
    groups: dict[float, list[tuple[int, Transition]]] = defaultdict(list)
    for line_index, line in enumerate(lines):
        for transition in line.transitions:
            if transition.time_days <= maximum:
                groups[transition.time_days].append((line_index, transition))
    return dict(groups)


def _empty_event_arrays(
    event_count: int, repository_count: int
) -> tuple[np.ndarray, ...]:
    return tuple(
        np.zeros((event_count, repository_count), dtype=np.float64) for _ in range(5)
    )


def _remove_count_censoring(
    censoring: Sequence[tuple[float, int]],
    censor_index: int,
    time: float,
    line_repositories: np.ndarray,
    current_states: Sequence[str],
    current_u: np.ndarray,
    current_m: np.ndarray,
) -> int:
    while censor_index < len(censoring) and censoring[censor_index][0] < time:
        line_index = censoring[censor_index][1]
        repository = line_repositories[line_index]
        if current_states[line_index] == "unchanged":
            current_u[repository] -= 1.0
        elif current_states[line_index] == "modified":
            current_m[repository] -= 1.0
        censor_index += 1
    return censor_index


def _record_count_events(
    batch: Sequence[tuple[int, Transition]],
    time_index: int,
    line_repositories: np.ndarray,
    current_states: list[str],
    current_u: np.ndarray,
    current_m: np.ndarray,
    event_arrays: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> None:
    event_um, event_ud, event_md = event_arrays
    for line_index, transition in batch:
        repository = line_repositories[line_index]
        target = transition.to_state
        if transition.from_state == "unchanged" and target == "modified":
            event_um[time_index, repository] += 1.0
            current_u[repository] -= 1.0
            current_m[repository] += 1.0
        elif transition.from_state == "unchanged":
            event_ud[time_index, repository] += 1.0
            current_u[repository] -= 1.0
        else:
            event_md[time_index, repository] += 1.0
            current_m[repository] -= 1.0
        current_states[line_index] = target


def _repository_layout(
    lines: Sequence[LineHistory],
) -> tuple[tuple[str, ...], np.ndarray, np.ndarray]:
    repository_ids = tuple(sorted({line.repository_id for line in lines}))
    repository_index = {
        repository_id: index for index, repository_id in enumerate(repository_ids)
    }
    line_repositories = np.array(
        [repository_index[line.repository_id] for line in lines], dtype=np.int64
    )
    repository_line_counts = np.bincount(
        line_repositories, minlength=len(repository_ids)
    ).astype(np.float64)
    return repository_ids, line_repositories, repository_line_counts


def _event_arrays(lines: Sequence[LineHistory], maximum: float) -> EventArrays:
    (
        repository_ids,
        line_repositories,
        repository_line_counts,
    ) = _repository_layout(lines)
    groups = _transition_groups(lines, maximum)
    times = np.array(sorted(groups), dtype=np.float64)
    risk_u, risk_m, event_um, event_ud, event_md = _empty_event_arrays(
        len(times), len(repository_ids)
    )
    current_states = ["unchanged"] * len(lines)
    current_u = repository_line_counts.copy()
    current_m = np.zeros(len(repository_ids), dtype=np.float64)
    censoring = sorted(
        (line.censor_time_days, index) for index, line in enumerate(lines)
    )
    censor_index = 0
    for time_index, time in enumerate(times):
        censor_index = _remove_count_censoring(
            censoring,
            censor_index,
            time,
            line_repositories,
            current_states,
            current_u,
            current_m,
        )
        risk_u[time_index] = current_u
        risk_m[time_index] = current_m
        _record_count_events(
            groups[float(time)],
            time_index,
            line_repositories,
            current_states,
            current_u,
            current_m,
            (event_um, event_ud, event_md),
        )
    return EventArrays(
        repository_ids,
        repository_line_counts,
        times,
        risk_u,
        risk_m,
        event_um,
        event_ud,
        event_md,
    )


def _draw_matrix(
    repository_ids: Sequence[str], draws: Sequence[Mapping[str, int]]
) -> np.ndarray:
    return np.array(
        [
            [draw.get(repository_id, 0) for repository_id in repository_ids]
            for draw in draws
        ],
        dtype=np.float64,
    )


def _weighted_event_array(
    values: np.ndarray, statistics: EventArrays, weighting: str
) -> np.ndarray:
    if weighting == "line":
        return values
    return values / statistics.repository_line_counts[np.newaxis, :]


def _bootstrap_km(
    statistics: EventArrays,
    draws: np.ndarray,
    horizons: Sequence[float],
    weighting: str,
) -> np.ndarray:
    deleted = statistics.unchanged_to_deleted + statistics.modified_to_deleted
    deletion_rows = np.flatnonzero(deleted.sum(axis=1))
    times = statistics.times[deletion_rows]
    risk = _weighted_event_array(
        statistics.risk_unchanged + statistics.risk_modified,
        statistics,
        weighting,
    )[deletion_rows]
    deleted = _weighted_event_array(deleted, statistics, weighting)[deletion_rows]
    survival = np.ones(draws.shape[0], dtype=np.float64)
    results = []
    start = 0
    for horizon in horizons:
        stop = int(np.searchsorted(times, horizon, side="right"))
        for chunk_start in range(start, stop, 1024):
            chunk_stop = min(chunk_start + 1024, stop)
            risk_draws = risk[chunk_start:chunk_stop] @ draws.T
            deleted_draws = deleted[chunk_start:chunk_stop] @ draws.T
            hazards = np.divide(
                deleted_draws,
                risk_draws,
                out=np.zeros_like(deleted_draws),
                where=risk_draws > 0,
            )
            survival *= np.prod(1.0 - hazards, axis=0)
        results.append(survival.copy())
        start = stop
    return np.stack(results, axis=1)


def _aj_update(
    occupancy: np.ndarray,
    risk_u: np.ndarray,
    risk_m: np.ndarray,
    event_um: np.ndarray,
    event_ud: np.ndarray,
    event_md: np.ndarray,
) -> None:
    for row in range(risk_u.shape[0]):
        hazard_um = np.divide(
            event_um[row],
            risk_u[row],
            out=np.zeros_like(event_um[row]),
            where=risk_u[row] > 0,
        )
        hazard_ud = np.divide(
            event_ud[row],
            risk_u[row],
            out=np.zeros_like(event_ud[row]),
            where=risk_u[row] > 0,
        )
        hazard_md = np.divide(
            event_md[row],
            risk_m[row],
            out=np.zeros_like(event_md[row]),
            where=risk_m[row] > 0,
        )
        mass_um = occupancy[:, 0] * hazard_um
        mass_ud = occupancy[:, 0] * hazard_ud
        mass_md = occupancy[:, 1] * hazard_md
        occupancy[:, 0] -= mass_um + mass_ud
        occupancy[:, 1] += mass_um - mass_md
        occupancy[:, 2] += mass_ud + mass_md


def _bootstrap_aj(
    statistics: EventArrays,
    draws: np.ndarray,
    horizons: Sequence[float],
    weighting: str,
) -> np.ndarray:
    arrays = [
        _weighted_event_array(values, statistics, weighting)
        for values in (
            statistics.risk_unchanged,
            statistics.risk_modified,
            statistics.unchanged_to_modified,
            statistics.unchanged_to_deleted,
            statistics.modified_to_deleted,
        )
    ]
    occupancy = np.zeros((draws.shape[0], 3), dtype=np.float64)
    occupancy[:, 0] = 1.0
    results = []
    start = 0
    for horizon in horizons:
        stop = int(np.searchsorted(statistics.times, horizon, side="right"))
        for chunk_start in range(start, stop, 512):
            chunk_stop = min(chunk_start + 512, stop)
            projected = [values[chunk_start:chunk_stop] @ draws.T for values in arrays]
            _aj_update(occupancy, *projected)
        results.append(occupancy.copy())
        start = stop
    return np.stack(results, axis=1)


def _interval(values: Sequence[float], confidence_level: float) -> dict[str, float]:
    alpha = (1.0 - confidence_level) / 2.0
    lower, upper = np.quantile(values, [alpha, 1.0 - alpha])
    return {"lower": float(lower), "upper": float(upper)}


def _bootstrap_array_summary(
    km: np.ndarray,
    aj: np.ndarray,
    horizons: Sequence[float],
    confidence_level: float,
) -> dict[str, Any]:
    return {
        "kaplan_meier": [
            {
                "horizon_days": horizon,
                **_interval(km[:, index], confidence_level),
            }
            for index, horizon in enumerate(horizons)
        ],
        "aalen_johansen": [
            {
                "horizon_days": horizon,
                "states": {
                    state: _interval(aj[:, index, state_index], confidence_level)
                    for state_index, state in enumerate(STATES)
                },
            }
            for index, horizon in enumerate(horizons)
        ],
    }


def _bootstrap_summary(
    estimates: Sequence[Mapping[str, Any]],
    horizons: Sequence[float],
    confidence_level: float,
) -> dict[str, Any]:
    km = []
    aj = []
    for index, horizon in enumerate(horizons):
        km.append(
            {
                "horizon_days": horizon,
                **_interval(
                    [result["kaplan_meier"][index]["survival"] for result in estimates],
                    confidence_level,
                ),
            }
        )
        aj.append(
            {
                "horizon_days": horizon,
                "states": {
                    state: _interval(
                        [
                            result["aalen_johansen"][index]["state_occupancy"][state]
                            for result in estimates
                        ],
                        confidence_level,
                    )
                    for state in STATES
                },
            }
        )
    return {"kaplan_meier": km, "aalen_johansen": aj}


def estimate_survival(
    document: Mapping[str, Any],
    *,
    horizons_days: Sequence[int | float],
    bootstrap_replicates: int,
    seed: int,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Return preregistered primary/secondary estimates and cluster intervals."""
    if not 0.0 < confidence_level < 1.0:
        raise SurvivalContractError("confidence_level must be between zero and one")
    lines = _parse_document(document)
    horizons = _horizons(horizons_days)
    repositories = sorted({line.repository_id for line in lines})
    draws = repository_bootstrap_draws(
        repositories, replicates=bootstrap_replicates, seed=seed
    )
    primary = _estimate_lines(lines, horizons, "repository")
    secondary = _estimate_lines(lines, horizons, "line")
    statistics = _event_arrays(lines, horizons[-1])
    draw_matrix = _draw_matrix(statistics.repository_ids, draws)
    primary_km = _bootstrap_km(statistics, draw_matrix, horizons, "repository")
    primary_aj = _bootstrap_aj(statistics, draw_matrix, horizons, "repository")
    secondary_km = _bootstrap_km(statistics, draw_matrix, horizons, "line")
    secondary_aj = _bootstrap_aj(statistics, draw_matrix, horizons, "line")
    return {
        "estimator_version": 2,
        "repository_count": len(repositories),
        "line_count": len(lines),
        "horizons_days": list(horizons),
        "primary": primary,
        "secondary": secondary,
        "bootstrap": {
            "unit": "repository",
            "replicates": bootstrap_replicates,
            "seed": seed,
            "confidence_level": confidence_level,
            "primary": _bootstrap_array_summary(
                primary_km, primary_aj, horizons, confidence_level
            ),
            "secondary": _bootstrap_array_summary(
                secondary_km, secondary_aj, horizons, confidence_level
            ),
        },
    }
