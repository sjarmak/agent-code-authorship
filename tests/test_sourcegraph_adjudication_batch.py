import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_adjudication_batch import (
    AdjudicationBatchError,
    batch_manifest_sha256,
    export_openai_review_batch,
    import_openai_review_batch,
)
from authorship.sourcegraph_adjudication_batch_cli import main
from authorship.sourcegraph_adjudication_compiler import review_response_sha256
from authorship.sourcegraph_adjudication_queue import (
    build_adjudication_work_queue,
)
from authorship.sourcegraph_discovery import evidence_packet_sha256
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

CUTOFF = "a" * 40


def _packet(packet_id: str, commit_oid: str) -> dict:
    query = (
        f"repo:^github\\.com/sg-evals/org-repo$@{CUTOFF} "
        "type:commit Co-authored-by:.*Claude count:all patternType:regexp"
    )
    document = {
        "packet_version": 3,
        "packet_id": packet_id,
        "packet_type": "adoption_event",
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": CUTOFF,
        "indexed_revision_oid": CUTOFF,
        "query_family_id": "agent_trailer_commits",
        "rendered_query": query,
        "rendered_query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "sourcegraph_result_ids": [f"sha256:{packet_id}"],
        "candidate_event": {
            "commit_oid": commit_oid,
            "observed_at": "2025-01-02T00:00:00Z",
        },
        "raw_evidence": [
            {
                "kind": "commit_message",
                "commit_oid": commit_oid,
                "path": None,
                "line": None,
                "value": (
                    "Co-authored-by: Claude <noreply@anthropic.com>"
                    "\u2028Evidence detail"
                ),
                "source_url": f"https://github.com/org/repo/commit/{commit_oid}",
            }
        ],
        "outcomes_consulted": False,
    }
    return {**document, "packet_sha256": evidence_packet_sha256(document)}


def _packet_index(specification: dict) -> dict:
    packets = [_packet(f"{index:064x}", str(index) * 40) for index in (1, 2)]
    document = {
        "packet_index_version": 3,
        "specification_sha256": specification["specification_sha256"],
        "source_result_manifest_sha256s": ["c" * 64],
        "packet_count": len(packets),
        "pending_file_enrichment_count": 0,
        "sourcegraph_line_number_basis": "zero_based_as_returned_by_graphql",
        "source_url_line_anchor_basis": "one_based",
        "packets": packets,
        "pending_file_enrichments": [],
        "outcomes_consulted": False,
    }
    return {**document, "packet_index_sha256": packet_index_sha256(document)}


def _tasks(root: Path, manifest: dict) -> list[dict]:
    return sorted(
        (
            json.loads(line)
            for shard in manifest["shards"]
            for line in (root / shard["shard_file"]).read_bytes().split(b"\n")
            if line
        ),
        key=lambda task: task["task_id"],
    )


@pytest.fixture
def queue_fixture(tmp_path: Path) -> dict:
    project = Path(__file__).resolve().parents[1]
    specification = json.loads(
        (project / "study/sourcegraph-discovery.v3.json").read_text()
    )
    queue_root = tmp_path / "queue"
    queue = build_adjudication_work_queue(
        specification,
        _packet_index(specification),
        queue_root,
        shard_count=2,
    )
    return {
        "specification": specification,
        "queue": queue,
        "queue_root": queue_root,
        "tasks": _tasks(queue_root, queue),
    }


def _batch_output(task: dict, decision: str = "observed") -> dict:
    output = {
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "decision": decision,
        "rationale": "The frozen evidence establishes a datable agent trailer.",
    }
    return {
        "id": f"batch_req_{task['task_id'][:8]}",
        "custom_id": task["task_id"],
        "response": {
            "status_code": 200,
            "request_id": f"req_{task['task_id'][:8]}",
            "body": {
                "id": f"resp_{task['task_id'][:8]}",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.6",
                "output": [
                    {
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        },
        "error": None,
    }


def test_export_and_import_openai_batch_is_blind_complete_and_order_independent(
    queue_fixture: dict, tmp_path: Path
):
    batch_root = tmp_path / "batch"
    manifest = export_openai_review_batch(
        queue_fixture["specification"],
        queue_fixture["queue"],
        queue_fixture["queue_root"],
        batch_root,
        reviewer_id="reviewer-model-a",
        model="gpt-5.6",
        reasoning_effort="low",
        max_requests_per_file=1,
    )

    assert manifest["request_count"] == 2
    assert manifest["emitted_file_count"] == 2
    assert manifest["batch_manifest_sha256"] == batch_manifest_sha256(manifest)
    requests = [
        json.loads(line)
        for record in manifest["files"]
        for line in (batch_root / record["file"]).read_bytes().split(b"\n")
        if line
    ]
    assert {request["custom_id"] for request in requests} == {
        task["task_id"] for task in queue_fixture["tasks"]
    }
    assert all(request["url"] == "/v1/responses" for request in requests)
    assert all(request["body"]["store"] is False for request in requests)
    assert all(
        request["body"]["text"]["format"]["strict"] is True for request in requests
    )
    assert all(
        "survival_outcome" not in json.dumps(request).lower()
        and "classifier_score" not in json.dumps(request).lower()
        for request in requests
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-batch-export.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)

    output_path = tmp_path / "output.jsonl"
    output_path.write_text(
        "".join(
            f"{json.dumps(_batch_output(task), sort_keys=True)}\n"
            for task in reversed(queue_fixture["tasks"])
        )
    )
    response = import_openai_review_batch(
        queue_fixture["specification"],
        queue_fixture["queue"],
        queue_fixture["queue_root"],
        manifest,
        batch_root,
        [output_path],
    )

    assert response["decision_count"] == 2
    assert response["stage"] == "primary"
    assert response["reviewer_kind"] == "model"
    assert response["outcome_blind"] is True
    assert response["peer_review_blind"] is True
    assert response["review_response_sha256"] == review_response_sha256(response)
    assert [item["task_id"] for item in response["decisions"]] == sorted(
        task["task_id"] for task in queue_fixture["tasks"]
    )


@pytest.mark.parametrize("failure", ["missing", "duplicate", "api_error", "refusal"])
def test_import_rejects_incomplete_or_failed_batch_results(
    queue_fixture: dict, tmp_path: Path, failure: str
):
    batch_root = tmp_path / "batch"
    manifest = export_openai_review_batch(
        queue_fixture["specification"],
        queue_fixture["queue"],
        queue_fixture["queue_root"],
        batch_root,
        reviewer_id="reviewer-model-a",
        model="gpt-5.6",
    )
    records = [_batch_output(task) for task in queue_fixture["tasks"]]
    if failure == "missing":
        records.pop()
    elif failure == "duplicate":
        records.append(records[0])
    elif failure == "api_error":
        records[0]["response"] = None
        records[0]["error"] = {"code": "batch_expired", "message": "expired"}
    else:
        records[0]["response"]["body"]["output"][0]["content"] = [
            {"type": "refusal", "refusal": "Cannot review."}
        ]
    output_path = tmp_path / "output.jsonl"
    output_path.write_text(
        "".join(f"{json.dumps(record, sort_keys=True)}\n" for record in records)
    )

    with pytest.raises(AdjudicationBatchError):
        import_openai_review_batch(
            queue_fixture["specification"],
            queue_fixture["queue"],
            queue_fixture["queue_root"],
            manifest,
            batch_root,
            [output_path],
        )


def test_import_rejects_rehashed_batch_file_metadata(
    queue_fixture: dict, tmp_path: Path
):
    batch_root = tmp_path / "batch"
    manifest = export_openai_review_batch(
        queue_fixture["specification"],
        queue_fixture["queue"],
        queue_fixture["queue_root"],
        batch_root,
        reviewer_id="reviewer-model-a",
        model="gpt-5.6",
    )
    changed = copy.deepcopy(manifest)
    changed["files"][0]["byte_count"] += 1
    changed["batch_manifest_sha256"] = batch_manifest_sha256(changed)

    with pytest.raises(AdjudicationBatchError, match="file metadata"):
        import_openai_review_batch(
            queue_fixture["specification"],
            queue_fixture["queue"],
            queue_fixture["queue_root"],
            changed,
            batch_root,
            [],
        )


def test_batch_cli_exports_and_imports_review_response(
    queue_fixture: dict, tmp_path: Path
):
    specification_path = tmp_path / "specification.json"
    queue_path = tmp_path / "queue.json"
    specification_path.write_text(json.dumps(queue_fixture["specification"]))
    queue_path.write_text(json.dumps(queue_fixture["queue"]))
    batch_root = tmp_path / "batch"
    manifest_path = tmp_path / "batch-manifest.json"

    assert (
        main(
            [
                "export",
                "--specification",
                str(specification_path),
                "--queue-manifest",
                str(queue_path),
                "--queue-root",
                str(queue_fixture["queue_root"]),
                "--reviewer-id",
                "reviewer-model-a",
                "--model",
                "gpt-5.6",
                "--output-root",
                str(batch_root),
                "--manifest",
                str(manifest_path),
            ]
        )
        == 0
    )
    output_path = tmp_path / "output.jsonl"
    output_path.write_text(
        "".join(
            f"{json.dumps(_batch_output(task), sort_keys=True)}\n"
            for task in queue_fixture["tasks"]
        )
    )
    response_path = tmp_path / "review-response.json"

    assert (
        main(
            [
                "import",
                "--specification",
                str(specification_path),
                "--queue-manifest",
                str(queue_path),
                "--queue-root",
                str(queue_fixture["queue_root"]),
                "--batch-manifest",
                str(manifest_path),
                "--batch-root",
                str(batch_root),
                "--output-file",
                str(output_path),
                "--review-response",
                str(response_path),
            ]
        )
        == 0
    )
    response = json.loads(response_path.read_text())
    assert response["decision_count"] == 2
    assert response["review_response_sha256"] == review_response_sha256(response)
