import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

import pytest
import jsonschema

from authorship.sourcegraph_blame_execution import (
    _blame_shard_sha256,
    BlameExecutionError,
    blame_execution_sha256,
    blame_plan_sha256,
    build_blame_plan,
    execute_blame_plan,
    load_execution_enrichments,
    validate_blame_execution,
)
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
RESULT_A = "sha256:" + "d" * 64
RESULT_B = "sha256:" + "e" * 64
EXECUTED_AT = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)


def pending(result_id: str, lines: list[int]) -> dict:
    return {
        "result_manifest_sha256": "1" * 64,
        "sourcegraph_result_id": result_id,
        "canonical_repository_id": "org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": SHA_A,
        "path": "POLICY.md",
        "sourcegraph_line_numbers": lines,
        "required_enrichment": "cutoff_pinned_blame",
    }


def packet_index(records: list[dict]) -> dict:
    document = {
        "pending_file_enrichment_count": len(records),
        "pending_file_enrichments": records,
    }
    return {**document, "packet_index_sha256": packet_index_sha256(document)}


def blame_response() -> dict:
    return {
        "b0": {
            "name": "github.com/sg-evals/org-repo",
            "commit": {
                "oid": SHA_A,
                "blob": {
                    "path": "POLICY.md",
                    "blame": [
                        {
                            "startLine": 5,
                            "endLine": 6,
                            "commit": {
                                "oid": SHA_B,
                                "author": {"date": "2025-04-01T00:00:00Z"},
                                "committer": {"date": "2025-04-02T00:00:00Z"},
                            },
                        },
                        {
                            "startLine": 9,
                            "endLine": 10,
                            "commit": {
                                "oid": SHA_C,
                                "author": {"date": "2025-05-01T00:00:00Z"},
                                "committer": None,
                            },
                        },
                    ],
                },
            },
        }
    }


def test_plan_groups_same_cutoff_file_and_preserves_path_only_matches():
    index = packet_index(
        [
            pending(RESULT_A, [4, 8]),
            pending(RESULT_B, []),
        ]
    )

    plan = build_blame_plan(index)

    assert plan["packet_index_sha256"] == index["packet_index_sha256"]
    assert plan["group_count"] == 1
    assert plan["query_group_count"] == 1
    assert plan["path_only_enrichment_count"] == 1
    assert plan["groups"][0]["requested_lines"] == [4, 8]
    assert plan["groups"][0]["blame_api_lines"] == [5, 9]
    assert plan["blame_plan_sha256"] == blame_plan_sha256(plan)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-blame-plan.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(plan)


def test_plan_rejects_packet_index_checksum_mismatch():
    index = packet_index([pending(RESULT_A, [4])])
    index["pending_file_enrichments"][0]["path"] = "OTHER.md"

    with pytest.raises(BlameExecutionError, match="SHA-256"):
        build_blame_plan(index)


def test_execution_batches_blame_maps_zero_based_ranges_and_resumes(tmp_path: Path):
    index = packet_index(
        [
            pending(RESULT_A, [4, 8]),
            pending(RESULT_B, []),
        ]
    )
    plan = build_blame_plan(index)
    calls = []

    def runner(query: str) -> dict:
        calls.append(query)
        return blame_response()

    first = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    second = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    enrichments = load_execution_enrichments(second, plan, tmp_path)

    assert first["status"] == "complete"
    assert second["status"] == "complete"
    assert len(calls) == 1
    assert first["blame_execution_sha256"] == blame_execution_sha256(first)
    assert validate_blame_execution(first, plan, index, tmp_path) == []
    assert validate_blame_execution(second, plan, index, tmp_path) == []
    execution_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-blame-execution.schema.json"
        ).read_text()
    )
    shard_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-blame-shard.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(execution_schema).validate(first)
    for summary in first["groups"]:
        shard = json.loads((tmp_path / summary["shard_path"]).read_text())
        jsonschema.Draft202012Validator(shard_schema).validate(shard)
    by_result = {
        enrichment["sourcegraph_result_id"]: enrichment for enrichment in enrichments
    }
    assert by_result[RESULT_A]["lines"] == [
        {
            "line": 4,
            "commit_oid": SHA_B,
            "observed_at": "2025-04-02T00:00:00Z",
        },
        {
            "line": 8,
            "commit_oid": SHA_C,
            "observed_at": "2025-05-01T00:00:00Z",
        },
    ]
    assert by_result[RESULT_B]["lines"] == []
    query_group = next(
        summary
        for summary in first["groups"]
        if summary["group_id"] == plan["groups"][0]["group_id"]
    )
    query_shard = json.loads((tmp_path / query_group["shard_path"]).read_text())
    assert query_shard["raw_blame_response"] == blame_response()["b0"]
    assert "startLine:5,endLine:9" in calls[0]
    assert "precise" not in calls[0].lower()
    assert "scip" not in calls[0].lower()


def test_execution_runs_blame_batches_with_bounded_parallelism(tmp_path: Path):
    first = pending(RESULT_A, [4])
    first["path"] = "FIRST.md"
    second = pending(RESULT_B, [4])
    second["path"] = "SECOND.md"
    index = packet_index([first, second])
    plan = build_blame_plan(index)
    release = Event()
    lock = Lock()
    active = 0
    peak = 0

    def runner(query: str) -> dict:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if peak == 2:
                release.set()
        assert release.wait(timeout=1)
        path = "FIRST.md" if "FIRST.md" in query else "SECOND.md"
        response = blame_response()
        response["b0"]["commit"]["blob"]["path"] = path
        with lock:
            active -= 1
        return response

    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=runner,
        clock=lambda: EXECUTED_AT,
        batch_size=1,
        max_workers=2,
    )

    assert execution["status"] == "complete"
    assert peak == 2


def test_execution_fails_closed_when_blame_does_not_cover_requested_line(
    tmp_path: Path,
):
    index = packet_index([pending(RESULT_A, [4, 8])])
    plan = build_blame_plan(index)
    response = blame_response()
    response["b0"]["commit"]["blob"]["blame"].pop()

    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: response,
        clock=lambda: EXECUTED_AT,
    )

    assert execution["status"] == "incomplete"
    assert execution["invalid_group_count"] == 1
    with pytest.raises(BlameExecutionError, match="not complete"):
        load_execution_enrichments(execution, plan, tmp_path)
    assert validate_blame_execution(execution, plan, index, tmp_path) == []


def test_validator_rejects_rechecksummed_external_shard(
    tmp_path: Path,
):
    index = packet_index([pending(RESULT_A, [4, 8])])
    plan = build_blame_plan(index)
    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: blame_response(),
        clock=lambda: EXECUTED_AT,
    )
    outside = tmp_path.parent / "outside-blame.json"
    source = tmp_path / execution["groups"][0]["shard_path"]
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    execution["blame_execution_sha256"] = blame_execution_sha256(execution)

    errors = validate_blame_execution(execution, plan, index, tmp_path)

    assert any("outside output directory" in error for error in errors)


def test_validator_recomputes_enrichments_from_raw_blame_response(tmp_path: Path):
    index = packet_index([pending(RESULT_A, [4, 8])])
    plan = build_blame_plan(index)
    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: blame_response(),
        clock=lambda: EXECUTED_AT,
    )
    summary = execution["groups"][0]
    shard_path = tmp_path / summary["shard_path"]
    shard = json.loads(shard_path.read_text())
    shard["enrichments"][0]["lines"][0]["commit_oid"] = SHA_C
    shard["blame_shard_sha256"] = _blame_shard_sha256(shard)
    shard_path.write_text(json.dumps(shard))
    summary["blame_shard_sha256"] = shard["blame_shard_sha256"]
    execution["blame_execution_sha256"] = blame_execution_sha256(execution)

    errors = validate_blame_execution(execution, plan, index, tmp_path)

    assert any("do not match group" in error for error in errors)


def test_validator_rejects_rechecksummed_unrecognized_execution_field(
    tmp_path: Path,
):
    index = packet_index([pending(RESULT_A, [4, 8])])
    plan = build_blame_plan(index)
    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: blame_response(),
        clock=lambda: EXECUTED_AT,
    )
    execution["unsupported"] = True
    execution["blame_execution_sha256"] = blame_execution_sha256(execution)

    errors = validate_blame_execution(execution, plan, index, tmp_path)

    assert any("unexpected fields" in error for error in errors)


def test_execution_requeries_rechecksummed_invalid_reusable_shard(tmp_path: Path):
    index = packet_index([pending(RESULT_A, [4, 8])])
    plan = build_blame_plan(index)
    first = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: blame_response(),
        clock=lambda: EXECUTED_AT,
    )
    shard_path = tmp_path / first["groups"][0]["shard_path"]
    shard = json.loads(shard_path.read_text())
    shard["enrichments"][0]["lines"][0]["commit_oid"] = SHA_C
    shard["blame_shard_sha256"] = _blame_shard_sha256(shard)
    shard_path.write_text(json.dumps(shard))
    calls = []

    second = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda query: calls.append(query) or blame_response(),
        clock=lambda: EXECUTED_AT,
    )

    assert len(calls) == 1
    assert validate_blame_execution(second, plan, index, tmp_path) == []


def test_plan_and_execution_are_json_serializable(tmp_path: Path):
    index = packet_index([pending(RESULT_B, [])])
    plan = build_blame_plan(index)
    execution = execute_blame_plan(
        plan,
        index,
        tmp_path,
        api_runner=lambda _query: {},
        clock=lambda: EXECUTED_AT,
    )

    json.dumps(plan)
    json.dumps(execution)
