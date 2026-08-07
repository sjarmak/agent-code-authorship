import copy
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.censoring_estimators import estimate_weighting
from authorship.survival_event_adapter import (
    SurvivalEventAdapterError,
    build_event_artifact,
    survival_event_artifact_sha256,
)

MERGE = "a" * 40
MODIFY = "b" * 40
DELETE = "c" * 40
MODIFY_LATE = "d" * 40
LOSS = "e" * 40


def transition(horizon: int, state: str, **overrides) -> dict:
    record = {
        "repository_id": "org/repo",
        "line_id": "line-1",
        "merge_commit": MERGE,
        "merged_at": "2025-01-01T00:00:00Z",
        "horizon_days": horizon,
        "horizon_status": (
            "right_censored" if state == "right_censored" else "observed"
        ),
        "state": state,
        "terminal_commit": DELETE if state == "deleted" else None,
        "language": "Python",
        "agent_family": "Cursor",
        "provenance_tier": 2,
    }
    return {**record, **overrides}


def transitions(*states: str) -> list[dict]:
    return [
        transition(horizon, state) for horizon, state in zip((30, 90, 180, 365), states)
    ]


def build(records: list[dict], structural_events: list[dict] | None = None) -> dict:
    return build_event_artifact(
        "org/repo",
        records,
        structural_events or [],
        commit_timestamps={
            MERGE: "2025-01-01T00:00:00Z",
            MODIFY: "2025-03-02T00:00:00Z",
            MODIFY_LATE: "2025-03-22T00:00:00Z",
            DELETE: "2025-05-01T00:00:00Z",
            LOSS: "2025-03-12T00:00:00Z",
        },
        cutoff_timestamp="2025-07-20T00:00:00Z",
        input_sha256s={
            "transitions": "1" * 64,
            "structural_events": "2" * 64,
            "git_inventory": "3" * 64,
        },
    )


def test_exact_modification_and_deletion_commits_become_event_history():
    records = transitions(
        "unchanged", "modified_candidate", "deleted", "right_censored"
    )
    for record in records:
        if record["state"] != "deleted":
            record["terminal_commit"] = DELETE
    structural = [
        {
            "repository_id": "org/repo",
            "line_id": "line-1",
            "decision": "modified_candidate",
            "commit": MODIFY,
        },
        {
            "repository_id": "org/repo",
            "line_id": "line-1",
            "decision": "modified_candidate",
            "commit": MODIFY_LATE,
        },
    ]

    artifact = build(records, structural)

    line = artifact["lines"][0]
    assert line["censor_time_days"] == 200.0
    assert line["transitions"] == [
        {
            "time_days": 60.0,
            "from_state": "unchanged",
            "to_state": "modified",
        },
        {
            "time_days": 120.0,
            "from_state": "modified",
            "to_state": "deleted",
        },
    ]
    assert line["language"] == "Python"
    assert line["agent_family"] == "Cursor"
    assert line["evidence_tier"] == "tier_2"
    assert artifact["survival_event_artifact_sha256"] == (
        survival_event_artifact_sha256(artifact)
    )


def test_lineage_loss_censors_at_last_observable_horizon():
    artifact = build(
        transitions("unchanged", "unobservable", "unobservable", "right_censored")
    )

    line = artifact["lines"][0]
    assert line["censor_time_days"] == 30.0
    assert line["transitions"] == []
    assert artifact["censoring_audit"] == {
        "study_cutoff": 0,
        "lineage_loss": 1,
        "terminal_deletion": 0,
    }


def test_lineage_loss_uses_exact_terminal_commit_after_modification():
    records = transitions("unchanged", "unobservable", "unobservable", "unobservable")
    for record in records[1:]:
        record["terminal_commit"] = LOSS
        record["terminal_reason"] = "replacement_below_threshold"
    structural = [
        {
            "repository_id": "org/repo",
            "line_id": "line-1",
            "decision": "modified_candidate",
            "commit": MODIFY,
        }
    ]

    artifact = build(records, structural)

    assert artifact["lines"][0]["censor_time_days"] == 70.0
    assert artifact["lines"][0]["transitions"] == [
        {
            "time_days": 60.0,
            "from_state": "unchanged",
            "to_state": "modified",
        }
    ]
    assert artifact["censoring_audit"]["lineage_loss"] == 1


def test_deletion_before_cutoff_survives_later_right_censoring():
    records = transitions("deleted", "deleted", "deleted", "right_censored")
    records[-1]["terminal_commit"] = DELETE

    artifact = build(records)

    assert artifact["lines"][0]["transitions"] == [
        {
            "time_days": 120.0,
            "from_state": "unchanged",
            "to_state": "deleted",
        }
    ]
    assert artifact["censoring_audit"]["terminal_deletion"] == 1


def test_terminal_deletion_between_last_horizon_and_cutoff_is_retained():
    records = transitions("unchanged", "unchanged", "unchanged", "right_censored")
    records[-1]["terminal_commit"] = DELETE
    records[-1]["terminal_reason"] = "file_deleted"

    artifact = build(records)

    assert artifact["lines"][0]["transitions"] == [
        {
            "time_days": 120.0,
            "from_state": "unchanged",
            "to_state": "deleted",
        }
    ]


def test_cotimestamped_modification_and_deletion_collapse_to_deletion():
    records = transitions("deleted", "deleted", "deleted", "right_censored")
    records[-1]["terminal_commit"] = DELETE
    structural = [
        {
            "repository_id": "org/repo",
            "line_id": "line-1",
            "decision": "modified_candidate",
            "commit": MODIFY,
        }
    ]

    artifact = build_event_artifact(
        "org/repo",
        records,
        structural,
        commit_timestamps={
            MODIFY: "2025-05-01T00:00:00Z",
            DELETE: "2025-05-01T00:00:00Z",
        },
        cutoff_timestamp="2025-07-20T00:00:00Z",
        input_sha256s={
            "transitions": "1" * 64,
            "structural_events": "2" * 64,
            "git_inventory": "3" * 64,
        },
    )

    assert artifact["lines"][0]["transitions"] == [
        {
            "time_days": 120.0,
            "from_state": "unchanged",
            "to_state": "deleted",
        }
    ]


def test_inconsistent_horizons_and_event_order_fail_closed():
    with pytest.raises(SurvivalEventAdapterError, match="frozen horizons"):
        build(transitions("unchanged", "unchanged", "unchanged", "unchanged")[:-1])
    malformed_horizon = transitions("unchanged", "unchanged", "unchanged", "unchanged")
    malformed_horizon[0]["horizon_days"] = {}
    with pytest.raises(SurvivalEventAdapterError, match="horizon"):
        build(malformed_horizon)

    records = transitions("modified_candidate", "deleted", "deleted", "right_censored")
    structural = [
        {
            "repository_id": "org/repo",
            "line_id": "line-1",
            "decision": "modified_candidate",
            "commit": MODIFY,
        }
    ]
    changed_dates = {
        MERGE: "2025-01-01T00:00:00Z",
        MODIFY: "2025-06-01T00:00:00Z",
        DELETE: "2025-05-01T00:00:00Z",
    }
    with pytest.raises(SurvivalEventAdapterError, match="event order"):
        build_event_artifact(
            "org/repo",
            records,
            structural,
            commit_timestamps=changed_dates,
            cutoff_timestamp="2025-07-20T00:00:00Z",
            input_sha256s={
                "transitions": "1" * 64,
                "structural_events": "2" * 64,
                "git_inventory": "3" * 64,
            },
        )


def test_artifact_checksum_detects_tampering():
    artifact = build(
        transitions("unchanged", "unchanged", "unchanged", "right_censored")
    )
    changed = copy.deepcopy(artifact)
    changed["lines"][0]["censor_time_days"] = 999

    assert survival_event_artifact_sha256(changed) != (
        changed["survival_event_artifact_sha256"]
    )


def test_artifact_is_schema_valid_and_estimator_compatible():
    artifact = build(transitions("deleted", "deleted", "deleted", "right_censored"))
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "study/survival-line-events.schema.json").read_text())

    jsonschema.Draft202012Validator(schema).validate(artifact)
    estimate = estimate_weighting(
        artifact, horizons_days=[30, 180, 365], weighting="repository"
    )
    survival = [point["survival"] for point in estimate["kaplan_meier"]]
    assert survival == sorted(survival, reverse=True)
