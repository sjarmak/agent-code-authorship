import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_batch import (
    longitudinal_batch_plan_sha256,
)
from authorship.sourcegraph_longitudinal_probe_execution import (
    execute_probe_plan,
    probe_execution_sha256,
)
from authorship.sourcegraph_longitudinal_probe_execution_cli import main

REPOSITORY_ID = "org/repo"
SOURCEGRAPH_NAME = "github.com/sg-evals/org-repo"
MERGE = "b" * 40
BASE = "c" * 40
CUTOFF = "a" * 40
FIXED_TIME = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)


def introduction() -> dict:
    return {
        "repository_id": REPOSITORY_ID,
        "merge_commit": MERGE,
        "diff_base": BASE,
        "merged_at": "2025-01-10T00:00:00Z",
        "path": "src/main.py",
        "line_number": 10,
        "content_sha256": "1" * 64,
        "text": "value = True",
        "language": "Python",
        "agent_family": "OpenAI_Codex",
        "provenance_tier": 2,
        "pr_number": 7,
    }


def plan(tmp_path: Path) -> dict:
    cohort = tmp_path / "cohort.jsonl"
    payload = json.dumps(introduction(), sort_keys=True) + "\n"
    cohort.write_text(payload)
    unit = {
        "probe_unit_id": "6" * 64,
        "repository": {
            "canonical_repository_id": REPOSITORY_ID,
            "sourcegraph_name": SOURCEGRAPH_NAME,
            "cutoff_commit": CUTOFF,
            "cutoff_tree": "d" * 40,
            "bundle_sha256": "2" * 64,
            "cache_path": "/cache/repo",
        },
        "introduction_shard": {
            "path": str(cohort),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        },
        "transition_shard": {"path": "/transition", "sha256": "3" * 64},
        "probe_status": "pending_sourcegraph",
        "probe_manifest_path": str(tmp_path / "probes/repo.json"),
    }
    document = {
        "longitudinal_batch_plan_version": 3,
        "candidate_frame_sha256": "4" * 64,
        "input_inventory_sha256s": {
            "git": "5" * 64,
            "cohort": "7" * 64,
            "lineage": "8" * 64,
            "sourcegraph_index": "9" * 64,
        },
        "horizons_days": [30, 90, 180, 365],
        "repository_count": 1,
        "units": [unit],
        "outcomes_consulted": False,
    }
    return {
        **document,
        "longitudinal_batch_plan_sha256": longitudinal_batch_plan_sha256(document),
    }


def commit_match() -> dict:
    return {
        "__typename": "CommitSearchResult",
        "messagePreview": None,
        "diffPreview": {
            "value": "+value = True",
            "highlights": [{"line": 0, "character": 1, "length": 5}],
        },
        "commit": {
            "repository": {"name": SOURCEGRAPH_NAME},
            "oid": MERGE,
            "url": f"/repo/-/commit/{MERGE}",
            "subject": "agent change",
            "author": {
                "date": "2025-01-10T00:00:00Z",
                "person": {"displayName": "Example"},
            },
        },
    }


def api_runner(graphql_query: str, **variables: str) -> dict:
    if "query" in variables:
        return {
            "search": {
                "results": {
                    "results": [commit_match()],
                    "limitHit": False,
                    "cloning": [],
                    "missing": [],
                    "timedout": [],
                    "resultCount": 1,
                    "elapsedMilliseconds": 1,
                }
            }
        }
    return {
        "repository": {
            "name": SOURCEGRAPH_NAME,
            "commit": {
                "oid": MERGE,
                "blob": {
                    "path": "src/main.py",
                    "blame": [
                        {
                            "startLine": 10,
                            "endLine": 10,
                            "commit": {"oid": MERGE},
                        }
                    ],
                },
            },
        }
    }


def test_bulk_probe_execution_is_complete_checksummed_and_resumable(tmp_path: Path):
    frozen_plan = plan(tmp_path)

    first = execute_probe_plan(
        frozen_plan, api_runner=api_runner, clock=lambda: FIXED_TIME
    )
    second = execute_probe_plan(
        frozen_plan, api_runner=api_runner, clock=lambda: FIXED_TIME
    )

    assert first["status"] == "complete"
    assert first["valid_unit_count"] == 1
    assert first["executed_unit_count"] == 1
    assert second["reused_unit_count"] == 1
    assert second["probe_execution_sha256"] == probe_execution_sha256(second)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "study/sourcegraph-longitudinal-probe-execution.schema.json"
    )
    jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(
        second
    )


def test_bulk_probe_execution_records_failure_and_rejects_tampered_plan(
    tmp_path: Path,
):
    frozen_plan = plan(tmp_path)

    def failing_runner(_query: str, **_variables: str) -> dict:
        raise RuntimeError("Sourcegraph unavailable")

    result = execute_probe_plan(
        frozen_plan, api_runner=failing_runner, clock=lambda: FIXED_TIME
    )

    assert result["status"] == "incomplete"
    assert result["invalid_unit_count"] == 1
    assert "Sourcegraph unavailable" in result["units"][0]["error"]
    changed = dict(frozen_plan)
    changed["repository_count"] = 2
    with pytest.raises(LongitudinalExtractionError, match="checksum"):
        execute_probe_plan(changed, api_runner=api_runner, clock=lambda: FIXED_TIME)
    invalid_unit = json.loads(json.dumps(frozen_plan))
    invalid_unit["units"][0]["probe_unit_id"] = "invalid"
    invalid_unit["longitudinal_batch_plan_sha256"] = longitudinal_batch_plan_sha256(
        {
            key: value
            for key, value in invalid_unit.items()
            if key != "longitudinal_batch_plan_sha256"
        }
    )
    with pytest.raises(LongitudinalExtractionError, match="unit contract"):
        execute_probe_plan(
            invalid_unit, api_runner=api_runner, clock=lambda: FIXED_TIME
        )


def test_bulk_probe_execution_continues_after_one_unit_fails(tmp_path: Path):
    frozen_plan = plan(tmp_path)
    second = json.loads(json.dumps(frozen_plan["units"][0]))
    second["probe_unit_id"] = "7" * 64
    second["probe_manifest_path"] = str(tmp_path / "probes/second.json")
    document = {
        key: value
        for key, value in frozen_plan.items()
        if key != "longitudinal_batch_plan_sha256"
    }
    document["units"] = [frozen_plan["units"][0], second]
    document["repository_count"] = 2
    frozen_plan = {
        **document,
        "longitudinal_batch_plan_sha256": longitudinal_batch_plan_sha256(document),
    }
    calls = 0

    def fail_once_runner(graphql_query: str, **variables: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one repository failed")
        return api_runner(graphql_query, **variables)

    result = execute_probe_plan(
        frozen_plan, api_runner=fail_once_runner, clock=lambda: FIXED_TIME
    )

    assert result["status"] == "incomplete"
    assert result["valid_unit_count"] == 1
    assert result["invalid_unit_count"] == 1


def test_probe_execution_cli_writes_manifest_atomically(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "execution/result.json"
    plan_path.write_text(json.dumps(plan(tmp_path)))

    assert (
        main(
            ["--plan", str(plan_path), "--output", str(output)],
            api_runner=api_runner,
            clock=lambda: FIXED_TIME,
        )
        == 0
    )
    assert json.loads(output.read_text())["status"] == "complete"


def test_probe_execution_cli_returns_nonzero_for_incomplete_run(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "execution.json"
    plan_path.write_text(json.dumps(plan(tmp_path)))

    def failing_runner(_query: str, **_variables: str) -> dict:
        raise RuntimeError("unavailable")

    assert (
        main(
            ["--plan", str(plan_path), "--output", str(output)],
            api_runner=failing_runner,
            clock=lambda: FIXED_TIME,
        )
        == 1
    )
    assert json.loads(output.read_text())["status"] == "incomplete"


def test_probe_execution_cli_rejects_non_object_plan(tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("[]")

    with pytest.raises(LongitudinalExtractionError, match="must be an object"):
        main(
            ["--plan", str(plan_path)],
            api_runner=api_runner,
            clock=lambda: FIXED_TIME,
        )
