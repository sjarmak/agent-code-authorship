import json
from copy import deepcopy

from authorship.sourcegraph_target_execution import (
    execute_target_unit_plan,
    fetch_target_file,
    fetch_tree_files,
    target_execution_sha256,
    validate_target_execution,
)
from authorship.sourcegraph_target_plan import target_unit_plan_sha256


def _task(path="src/a.py", suffix="1"):
    return {
        "task_sha256": suffix * 64,
        "repository_id": "acme/alpha",
        "sourcegraph_name": "github.com/sg-evals/acme-alpha",
        "cutoff_commit": "a" * 40,
        "cutoff_tree": "b" * 40,
        "effective_date_range": [
            "2024-01-01T00:00:00Z",
            "2026-01-02T23:59:59Z",
        ],
        "path": path,
        "language": "Python",
    }


def _plan():
    document = {
        "target_unit_plan_version": 1,
        "status": "frozen_before_target_outcome_extraction",
        "repository_count": 1,
        "ready_repository_count": 1,
        "file_count": 2,
        "pending_repositories": [],
        "tasks": [_task(), _task("src/b.py", "2")],
    }
    return {**document, "target_unit_plan_sha256": target_unit_plan_sha256(document)}


def _response(task):
    return {
        "repository_name": task["sourcegraph_name"],
        "commit_oid": task["cutoff_commit"],
        "path": task["path"],
        "content": "value = 1\n",
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


def test_execution_checkpoints_every_file_and_validates_raw_evidence(tmp_path):
    calls = []

    def runner(task):
        calls.append(task["path"])
        return _response(task)

    execution = execute_target_unit_plan(_plan(), tmp_path, runner, workers=2)

    assert execution["status"] == "complete"
    assert execution["success_count"] == 2
    assert execution["unit_count"] == 2
    assert sorted(calls) == ["src/a.py", "src/b.py"]
    assert validate_target_execution(_plan(), execution, tmp_path) == []


def test_execution_reuses_only_valid_content_addressed_shards(tmp_path):
    plan = _plan()
    first = execute_target_unit_plan(plan, tmp_path, _response)

    def forbidden(_task):
        raise AssertionError("valid shard should have been reused")

    second = execute_target_unit_plan(plan, tmp_path, forbidden)

    assert second["status"] == "complete"
    assert second["references"] == first["references"]


def test_validator_rejects_rehashed_reference_omission(tmp_path):
    plan = _plan()
    execution = execute_target_unit_plan(plan, tmp_path, _response)
    forged = deepcopy(execution)
    forged["references"] = forged["references"][:-1]
    forged["success_count"] = 1
    forged["task_count"] = 1
    forged["unit_count"] = 1
    forged["target_execution_sha256"] = target_execution_sha256(forged)

    errors = validate_target_execution(plan, forged, tmp_path)

    assert any("exact task set" in error for error in errors)


def test_fetch_tree_files_pins_commit_and_tree():
    def api_runner(query, **variables):
        assert "TargetTree" in query
        assert variables["rev"] == "a" * 40
        return {
            "repository": {
                "commit": {
                    "oid": "a" * 40,
                    "tree": {
                        "files": [{"path": "b.py"}, {"path": "a.py"}],
                    },
                }
            }
        }

    result = fetch_tree_files(
        "github.com/sg-evals/acme-alpha", "a" * 40, api_runner=api_runner
    )

    assert result == {
        "oid": "a" * 40,
        "paths": ["a.py", "b.py"],
    }


def test_fetch_target_file_pages_and_merges_blame_boundaries():
    content = "\n".join(f"line {number}" for number in range(1, 5002))

    def api_runner(query, **_variables):
        if "TargetBlobInitial" in query:
            return {
                "repository": {
                    "name": "github.com/sg-evals/acme-alpha",
                    "commit": {
                        "oid": "a" * 40,
                        "blob": {
                            "path": "src/a.py",
                            "content": content,
                            "blame": [
                                {
                                    "startLine": 1,
                                    "endLine": 5001,
                                    "commit": {
                                        "oid": "c" * 40,
                                        "author": {"date": "2025-01-01T00:00:00Z"},
                                        "committer": {"date": "2025-01-01T00:00:00Z"},
                                    },
                                }
                            ],
                        },
                    },
                }
            }
        assert "TargetBlamePage" in query
        return {
            "repository": {
                "commit": {
                    "oid": "a" * 40,
                    "blob": {
                        "path": "src/a.py",
                        "blame": [
                            {
                                "startLine": 5001,
                                "endLine": 5002,
                                "commit": {
                                    "oid": "c" * 40,
                                    "author": {"date": "2025-01-01T00:00:00Z"},
                                    "committer": {"date": "2025-01-01T00:00:00Z"},
                                },
                            }
                        ],
                    },
                }
            }
        }

    response = fetch_target_file(_task(), api_runner=api_runner)

    assert len(response["blame"]) == 1
    assert response["blame"][0]["startLine"] == 1
    assert response["blame"][0]["endLine"] == 5002


def test_validator_detects_on_disk_shard_tampering(tmp_path):
    plan = _plan()
    execution = execute_target_unit_plan(plan, tmp_path, _response)
    shard_path = tmp_path / execution["references"][0]["shard_path"]
    shard = json.loads(shard_path.read_text())
    shard["units"][0]["line_count"] = 99
    shard_path.write_text(json.dumps(shard))

    errors = validate_target_execution(plan, execution, tmp_path)

    assert any("file SHA-256" in error for error in errors)
