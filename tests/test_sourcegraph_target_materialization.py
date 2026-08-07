from copy import deepcopy

from authorship.sourcegraph_target_execution import execute_target_unit_plan
from authorship.sourcegraph_target_materialization import (
    materialize_target_units,
    target_materialization_sha256,
    validate_target_materialization,
)
from authorship.sourcegraph_target_plan import target_unit_plan_sha256


def _task(path, suffix):
    return {
        "task_sha256": suffix * 64,
        "repository_id": "acme/alpha" if suffix == "1" else "acme/beta",
        "sourcegraph_name": f"github.com/sg-evals/acme-{suffix}",
        "cutoff_commit": suffix * 40,
        "cutoff_tree": "f" * 40,
        "effective_date_range": [
            "2024-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ],
        "path": path,
        "language": "Python",
        "file_inclusion_probability": 0.5,
        "file_sampling_weight": 2.0,
    }


def _plan():
    document = {
        "target_unit_plan_version": 1,
        "status": "frozen_before_target_outcome_extraction",
        "repository_count": 2,
        "ready_repository_count": 2,
        "file_count": 2,
        "pending_repositories": [],
        "tasks": [_task("a.py", "1"), _task("b.py", "2")],
    }
    return {**document, "target_unit_plan_sha256": target_unit_plan_sha256(document)}


def _response(task):
    return {
        "repository_name": task["sourcegraph_name"],
        "commit_oid": task["cutoff_commit"],
        "path": task["path"],
        "content": "x = 1\n",
        "blame": [
            {
                "startLine": 1,
                "endLine": 2,
                "commit": {
                    "oid": "c" * 40,
                    "author": {"date": "2025-01-01T00:00:00Z"},
                    "committer": {"date": "2025-01-01T00:00:00Z"},
                },
            }
        ],
    }


def test_materialization_preserves_target_units_and_weight_totals(tmp_path):
    plan = _plan()
    execution = execute_target_unit_plan(plan, tmp_path, _response)

    result = materialize_target_units(plan, execution, tmp_path)

    assert result["status"] == "complete"
    assert result["repository_count"] == 2
    assert result["processed_repository_count"] == 2
    assert result["unit_count"] == 2
    assert result["line_count"] == 2
    assert result["weighted_line_count"] == 4.0
    assert result["repository_unit_counts"] == {
        "acme/alpha": 1,
        "acme/beta": 1,
    }
    assert validate_target_materialization(result, plan, execution, tmp_path) == []


def test_materialization_validator_rejects_rehashed_unit_change(tmp_path):
    plan = _plan()
    execution = execute_target_unit_plan(plan, tmp_path, _response)
    result = materialize_target_units(plan, execution, tmp_path)
    forged = deepcopy(result)
    forged["units"][0]["feature_values"]["log_lines"] = 100
    forged["target_materialization_sha256"] = target_materialization_sha256(forged)

    errors = validate_target_materialization(forged, plan, execution, tmp_path)

    assert any("independent shard materialization" in error for error in errors)
