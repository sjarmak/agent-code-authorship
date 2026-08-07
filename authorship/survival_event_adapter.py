"""Convert pinned lineage snapshots into exact censoring-aware event histories."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

HORIZONS = (30, 90, 180, 365)


class SurvivalEventAdapterError(ValueError):
    """Raised when frozen lineage cannot support an exact event history."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def survival_event_artifact_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "survival_event_artifact_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SurvivalEventAdapterError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SurvivalEventAdapterError(f"{field} must be a timestamp") from error
    if parsed.utcoffset() is None:
        raise SurvivalEventAdapterError(f"{field} must include a timezone")
    return parsed


def _days(start: datetime, end: datetime, field: str) -> float:
    value = (end - start).total_seconds() / 86_400
    if value < 0:
        raise SurvivalEventAdapterError(f"{field} precedes line introduction")
    return value


def _group_records(
    repository_id: str, transitions: Sequence[Mapping[str, Any]]
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in transitions:
        if not isinstance(record, Mapping):
            raise SurvivalEventAdapterError("transition must be an object")
        if record.get("repository_id") != repository_id:
            raise SurvivalEventAdapterError("transition repository does not match")
        line_id = record.get("line_id")
        if not isinstance(line_id, str) or not line_id:
            raise SurvivalEventAdapterError("transition line_id is required")
        horizon = record.get("horizon_days")
        if (
            isinstance(horizon, bool)
            or not isinstance(horizon, int)
            or horizon not in HORIZONS
        ):
            raise SurvivalEventAdapterError("transition horizon is invalid")
        grouped[line_id].append(record)
    if not grouped:
        raise SurvivalEventAdapterError("transitions must not be empty")
    for records in grouped.values():
        records.sort(key=lambda record: record.get("horizon_days", -1))
        horizons = [record.get("horizon_days") for record in records]
        if horizons != list(HORIZONS):
            raise SurvivalEventAdapterError(
                "every line must contain all frozen horizons"
            )
    return grouped


def _structural_commits(
    repository_id: str,
    events: Sequence[Mapping[str, Any]],
    line_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    commits: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if not isinstance(event, Mapping):
            raise SurvivalEventAdapterError("structural event must be an object")
        line_id = event.get("line_id")
        if (
            not isinstance(line_id, str)
            or event.get("repository_id") != repository_id
            or line_id not in line_ids
        ):
            raise SurvivalEventAdapterError(
                "structural event population does not match"
            )
        if event.get("decision") != "modified_candidate":
            continue
        commit = event.get("commit")
        if not isinstance(commit, str) or len(commit) != 40:
            raise SurvivalEventAdapterError("structural modification is invalid")
        commits[line_id].add(commit)
    return {line_id: tuple(sorted(values)) for line_id, values in commits.items()}


def _consistent_metadata(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    fields = (
        "repository_id",
        "line_id",
        "merge_commit",
        "merged_at",
        "language",
        "agent_family",
        "provenance_tier",
    )
    first = records[0]
    if any(
        record.get(field) != first.get(field) for record in records for field in fields
    ):
        raise SurvivalEventAdapterError("line transition metadata is inconsistent")
    return first


def _commit_time(
    commit: Any, timestamps: Mapping[str, str], introduction: datetime, field: str
) -> float:
    if not isinstance(commit, str) or commit not in timestamps:
        raise SurvivalEventAdapterError(f"{field} commit timestamp is missing")
    return _days(introduction, _timestamp(timestamps[commit], field), field)


def _deletion_commit(records: Sequence[Mapping[str, Any]]) -> str | None:
    deletion_observed = any(record.get("state") == "deleted" for record in records)
    deletion_terminal = any(
        record.get("terminal_reason") in {"file_deleted", "line_removed"}
        for record in records
    )
    if not deletion_observed and not deletion_terminal:
        return None
    commits = {
        record.get("terminal_commit")
        for record in records
        if record.get("terminal_commit") is not None
    }
    if len(commits) != 1:
        raise SurvivalEventAdapterError("deleted line must have one terminal commit")
    return next(iter(commits))


def _censor_time(
    records: Sequence[Mapping[str, Any]],
    introduction: datetime,
    cutoff: datetime,
    timestamps: Mapping[str, str],
) -> tuple[float, str]:
    cutoff_days = _days(introduction, cutoff, "study cutoff")
    unobservable = [
        record["horizon_days"]
        for record in records
        if record.get("state") == "unobservable"
    ]
    if not unobservable:
        return cutoff_days, "study_cutoff"
    loss_commits = {
        record.get("terminal_commit")
        for record in records
        if record.get("state") == "unobservable"
        and record.get("terminal_commit") is not None
    }
    if len(loss_commits) > 1:
        raise SurvivalEventAdapterError(
            "lineage loss must have at most one terminal commit"
        )
    if loss_commits:
        loss_commit = next(iter(loss_commits))
        loss_time = _commit_time(loss_commit, timestamps, introduction, "lineage loss")
        if loss_time > cutoff_days:
            raise SurvivalEventAdapterError("lineage loss occurs after study cutoff")
        return loss_time, "lineage_loss"
    first_loss = min(unobservable)
    observed = [
        record["horizon_days"]
        for record in records
        if record["horizon_days"] < first_loss
        and record.get("horizon_status") == "observed"
        and record.get("state") != "unobservable"
    ]
    return float(max(observed, default=0)), "lineage_loss"


def _event_times(
    records: Sequence[Mapping[str, Any]],
    modification_commits: Sequence[str],
    timestamps: Mapping[str, str],
    introduction: datetime,
) -> tuple[float | None, float | None]:
    modification_time = min(
        (
            _commit_time(commit, timestamps, introduction, "modification")
            for commit in modification_commits
        ),
        default=None,
    )
    deletion_commit = _deletion_commit(records)
    deletion_time = (
        _commit_time(deletion_commit, timestamps, introduction, "deletion")
        if deletion_commit is not None
        else None
    )
    if modification_time is not None and deletion_time is not None:
        if modification_time > deletion_time:
            raise SurvivalEventAdapterError(
                "modification/deletion event order is invalid"
            )
        if modification_time == deletion_time:
            modification_time = None
    return modification_time, deletion_time


def _event_transitions(
    modification_time: float | None, deletion_time: float | None
) -> list[dict[str, Any]]:
    transitions = []
    state = "unchanged"
    if modification_time is not None:
        transitions.append(
            {
                "time_days": modification_time,
                "from_state": state,
                "to_state": "modified",
            }
        )
        state = "modified"
    if deletion_time is not None:
        transitions.append(
            {
                "time_days": deletion_time,
                "from_state": state,
                "to_state": "deleted",
            }
        )
    return transitions


def build_line_history(
    line_id: str,
    records: Sequence[Mapping[str, Any]],
    modification_commits: Sequence[str],
    timestamps: Mapping[str, str],
    cutoff: datetime,
) -> tuple[dict[str, Any], str]:
    """Build one exact event history from the four frozen snapshots."""
    horizons = [record.get("horizon_days") for record in records]
    if sorted(horizons) != list(HORIZONS):
        raise SurvivalEventAdapterError("line must contain all frozen horizons")
    records = sorted(records, key=lambda record: record["horizon_days"])
    first = _consistent_metadata(records)
    introduction = _timestamp(first.get("merged_at"), "merged_at")
    censor_time, censor_reason = _censor_time(records, introduction, cutoff, timestamps)
    modification_time, deletion_time = _event_times(
        records,
        modification_commits,
        timestamps,
        introduction,
    )
    event_times = [
        value for value in (modification_time, deletion_time) if value is not None
    ]
    if any(value > censor_time for value in event_times):
        raise SurvivalEventAdapterError("event occurs after censoring")
    transitions = _event_transitions(modification_time, deletion_time)
    censor_reason = "terminal_deletion" if deletion_time is not None else censor_reason
    tier = first.get("provenance_tier")
    if tier not in {1, 2}:
        raise SurvivalEventAdapterError("provenance tier is invalid")
    return {
        "repository_id": first["repository_id"],
        "line_id": line_id,
        "censor_time_days": censor_time,
        "transitions": transitions,
        "language": first["language"],
        "agent_family": first["agent_family"],
        "evidence_tier": f"tier_{tier}",
    }, censor_reason


def _input_sha256s(values: Mapping[str, Any]) -> dict[str, str]:
    required = ("transitions", "structural_events", "git_inventory")
    result = {}
    for field in required:
        value = values.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise SurvivalEventAdapterError(f"{field} checksum is invalid")
        result[field] = value
    return result


def build_event_artifact(
    repository_id: str,
    transitions: Sequence[Mapping[str, Any]],
    structural_events: Sequence[Mapping[str, Any]],
    *,
    commit_timestamps: Mapping[str, str],
    cutoff_timestamp: str,
    input_sha256s: Mapping[str, Any],
) -> dict[str, Any]:
    """Build exact event histories for one verified pinned repository."""
    grouped = _group_records(repository_id, transitions)
    modifications = _structural_commits(repository_id, structural_events, set(grouped))
    cutoff = _timestamp(cutoff_timestamp, "cutoff_timestamp")
    histories = []
    reasons: Counter[str] = Counter()
    for line_id, records in sorted(grouped.items()):
        history, reason = build_line_history(
            line_id,
            records,
            modifications.get(line_id, ()),
            commit_timestamps,
            cutoff,
        )
        histories.append(history)
        reasons[reason] += 1
    document = {
        "contract_version": 1,
        "repository_id": repository_id,
        "input_sha256s": _input_sha256s(input_sha256s),
        "line_count": len(histories),
        "censoring_audit": {
            reason: reasons[reason]
            for reason in ("study_cutoff", "lineage_loss", "terminal_deletion")
        },
        "lines": histories,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "survival_event_artifact_sha256": survival_event_artifact_sha256(document),
    }
