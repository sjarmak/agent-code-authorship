from copy import deepcopy

import pytest

from authorship.sourcegraph_target_exact import (
    TargetExactError,
    materialize_target_file,
    target_file_shard_sha256,
    validate_target_file_shard,
)


def _task():
    return {
        "task_sha256": "f" * 64,
        "repository_id": "acme/alpha",
        "sourcegraph_name": "github.com/sg-evals/acme-alpha",
        "cutoff_commit": "a" * 40,
        "cutoff_tree": "b" * 40,
        "effective_date_range": [
            "2024-01-01T00:00:00Z",
            "2026-01-02T23:59:59Z",
        ],
        "path": "src/main.py",
        "language": "Python",
        "file_inclusion_probability": 0.5,
        "file_sampling_weight": 2.0,
    }


def _response():
    return {
        "repository_name": "github.com/sg-evals/acme-alpha",
        "commit_oid": "a" * 40,
        "path": "src/main.py",
        "content": "def old():\n    return 1\n\ndef new():\n    return 2\n",
        "blame": [
            {
                "startLine": 1,
                "endLine": 4,
                "commit": {
                    "oid": "1" * 40,
                    "author": {"date": "2023-12-01T00:00:00Z"},
                    "committer": {"date": "2023-12-01T00:00:00Z"},
                },
            },
            {
                "startLine": 4,
                "endLine": 6,
                "commit": {
                    "oid": "2" * 40,
                    "author": {"date": "2025-02-01T00:00:00Z"},
                    "committer": {"date": "2025-02-02T00:00:00Z"},
                },
            },
        ],
    }


def test_materialization_retains_surviving_in_range_blame_hunks():
    shard = materialize_target_file(_task(), _response())

    assert shard["status"] == "success"
    assert shard["raw_blame_hunk_count"] == 2
    assert shard["retained_unit_count"] == 1
    unit = shard["units"][0]
    assert unit["repository_id"] == "acme/alpha"
    assert unit["introducing_commit_oid"] == "2" * 40
    assert unit["introduced_at"] == "2025-02-01T00:00:00Z"
    assert unit["line_numbers"] == [4, 5]
    assert unit["line_count"] == 2
    assert unit["file_inclusion_probability"] == 0.5
    assert unit["file_sampling_weight"] == 2.0
    assert unit["weighted_line_count"] == 4.0
    assert unit["end_line_exclusive"] == 6
    assert unit["path_type"] == "source"
    assert set(unit["feature_values"]) >= {"log_lines", "is_python"}
    assert validate_target_file_shard(shard, _task()) == []


def test_materialization_keeps_one_line_units_for_line_weighting():
    response = _response()
    response["content"] = "value = 1"
    response["blame"] = [
        {
            "startLine": 1,
            "endLine": 2,
            "commit": {
                "oid": "3" * 40,
                "author": {"date": "2025-01-01T00:00:00Z"},
                "committer": {"date": "2025-01-01T00:00:00Z"},
            },
        }
    ]

    shard = materialize_target_file(_task(), response)

    assert shard["units"][0]["line_count"] == 1


def test_materialization_requires_exact_complete_blame_coverage():
    response = _response()
    response["blame"][1]["startLine"] = 5

    with pytest.raises(TargetExactError, match="cover every content line"):
        materialize_target_file(_task(), response)


def test_materialization_rejects_naive_blame_timestamp():
    response = _response()
    response["blame"][1]["commit"]["author"]["date"] = "2025-02-01T00:00:00"

    with pytest.raises(TargetExactError, match="timezone"):
        materialize_target_file(_task(), response)


def test_validator_recomputes_features_from_raw_sourcegraph_response():
    shard = materialize_target_file(_task(), _response())
    forged = deepcopy(shard)
    forged["units"][0]["feature_values"]["log_lines"] = 999.0
    forged["target_file_shard_sha256"] = target_file_shard_sha256(forged)

    errors = validate_target_file_shard(forged, _task())

    assert any("derived target units" in error for error in errors)


def test_materialization_rejects_revision_or_repository_drift():
    for field, value in (
        ("repository_name", "github.com/sg-evals/wrong"),
        ("commit_oid", "0" * 40),
        ("path", "wrong.py"),
    ):
        response = {**_response(), field: value}
        with pytest.raises(TargetExactError):
            materialize_target_file(_task(), response)


def test_materialization_rejects_non_hex_commit_oid():
    response = _response()
    response["blame"][1]["commit"]["oid"] = "z" * 40

    with pytest.raises(TargetExactError, match="OID"):
        materialize_target_file(_task(), response)
