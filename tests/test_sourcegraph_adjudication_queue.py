import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_adjudication_queue import (
    AdjudicationQueueError,
    build_adjudication_work_queue,
    queue_manifest_sha256,
    task_sha256,
    validate_adjudication_work_queue,
)
from authorship.sourcegraph_adjudication_queue_cli import main
from authorship.sourcegraph_discovery import evidence_packet_sha256
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

CUTOFF = "a" * 40
COMMIT = "b" * 40


def _packet(
    *,
    packet_id: str,
    commit_oid: str = COMMIT,
    value: str = "Co-authored-by: Claude <noreply@anthropic.com>",
) -> dict:
    rendered_query = (
        f"repo:^github\\.com/sg-evals/org-repo$@{CUTOFF} "
        "type:commit Co-authored-by:.*Claude "
        "count:all patternType:regexp"
    )
    packet = {
        "packet_version": 3,
        "packet_id": packet_id,
        "packet_type": "adoption_event",
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": CUTOFF,
        "indexed_revision_oid": CUTOFF,
        "query_family_id": "agent_trailer_commits",
        "rendered_query": rendered_query,
        "rendered_query_sha256": hashlib.sha256(rendered_query.encode()).hexdigest(),
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
                "value": value,
                "source_url": (f"https://github.com/org/repo/commit/{commit_oid}"),
            }
        ],
        "outcomes_consulted": False,
    }
    return {**packet, "packet_sha256": evidence_packet_sha256(packet)}


def _packet_index(specification: dict, *packets: dict) -> dict:
    document = {
        "packet_index_version": 3,
        "specification_sha256": specification["specification_sha256"],
        "source_result_manifest_sha256s": ["c" * 64],
        "packet_count": len(packets),
        "pending_file_enrichment_count": 0,
        "sourcegraph_line_number_basis": "zero_based_as_returned_by_graphql",
        "source_url_line_anchor_basis": "one_based",
        "packets": list(packets),
        "pending_file_enrichments": [],
        "outcomes_consulted": False,
    }
    return {**document, "packet_index_sha256": packet_index_sha256(document)}


@pytest.fixture
def specification() -> dict:
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "study/sourcegraph-discovery.v3.json").read_text())


def _tasks(output_root: Path, manifest: dict) -> list[dict]:
    records = []
    for shard in manifest["shards"]:
        path = output_root / shard["shard_file"]
        records.extend(json.loads(line) for line in path.read_text().splitlines())
    return sorted(records, key=lambda record: record["task_id"])


def test_queue_groups_duplicate_events_and_preserves_every_packet(
    specification: dict, tmp_path: Path
):
    duplicate_a = _packet(packet_id="1" * 64, value="first observation")
    duplicate_b = _packet(packet_id="2" * 64, value="second observation")
    separate = _packet(packet_id="3" * 64, commit_oid="d" * 40)
    index = _packet_index(specification, duplicate_a, duplicate_b, separate)

    manifest = build_adjudication_work_queue(
        specification,
        index,
        tmp_path,
        shard_count=2,
    )
    tasks = _tasks(tmp_path, manifest)

    assert manifest["packet_count"] == 3
    assert manifest["task_count"] == 2
    assert sum(shard["packet_count"] for shard in manifest["shards"]) == 3
    grouped = next(task for task in tasks if task["packet_count"] == 2)
    assert [record["packet_id"] for record in grouped["observations"]] == [
        "1" * 64,
        "2" * 64,
    ]
    assert grouped["task_sha256"] == task_sha256(grouped)
    assert manifest["queue_manifest_sha256"] == queue_manifest_sha256(manifest)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-work-queue.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)
    task_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-task.schema.json"
        ).read_text()
    )
    task_validator = jsonschema.Draft202012Validator(task_schema)
    for task in tasks:
        task_validator.validate(task)
    for shard in manifest["shards"]:
        payload = (tmp_path / shard["shard_file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == shard["sha256"]
    assert validate_adjudication_work_queue(manifest, tmp_path) == []


def test_queue_is_deterministic_and_rejects_event_identity_drift(
    specification: dict, tmp_path: Path
):
    first = _packet(packet_id="1" * 64)
    second = _packet(packet_id="2" * 64, value="other evidence")
    index = _packet_index(specification, first, second)

    original = build_adjudication_work_queue(
        specification, index, tmp_path, shard_count=3
    )
    repeated = build_adjudication_work_queue(
        specification,
        index,
        tmp_path,
        shard_count=3,
    )

    assert repeated == original
    changed = copy.deepcopy(index)
    changed["packets"][1]["candidate_event"]["observed_at"] = "2025-01-03T00:00:00Z"
    changed["packets"][1]["packet_sha256"] = evidence_packet_sha256(
        changed["packets"][1]
    )
    changed["packet_index_sha256"] = packet_index_sha256(changed)
    with pytest.raises(AdjudicationQueueError, match="event metadata"):
        build_adjudication_work_queue(
            specification,
            changed,
            tmp_path,
            shard_count=3,
        )


def test_queue_rejects_tampered_or_outcome_exposed_inputs(
    specification: dict, tmp_path: Path
):
    index = _packet_index(specification, _packet(packet_id="1" * 64))
    tampered = copy.deepcopy(index)
    tampered["packet_count"] = 2
    with pytest.raises(AdjudicationQueueError, match="packet index"):
        build_adjudication_work_queue(specification, tampered, tmp_path, shard_count=2)
    outcome_exposed = copy.deepcopy(index)
    outcome_exposed["outcomes_consulted"] = True
    outcome_exposed["packet_index_sha256"] = packet_index_sha256(outcome_exposed)
    with pytest.raises(AdjudicationQueueError, match="outcome blind"):
        build_adjudication_work_queue(
            specification,
            outcome_exposed,
            tmp_path,
            shard_count=2,
        )
    with pytest.raises(AdjudicationQueueError, match="shard_count"):
        build_adjudication_work_queue(specification, index, tmp_path, shard_count=0)


def test_queue_validator_rejects_tampered_shard(specification: dict, tmp_path: Path):
    index = _packet_index(specification, _packet(packet_id="1" * 64))
    manifest = build_adjudication_work_queue(
        specification, index, tmp_path, shard_count=2
    )
    shard_path = tmp_path / manifest["shards"][0]["shard_file"]
    shard_path.write_text(f"{shard_path.read_text()}{{}}\n")

    errors = validate_adjudication_work_queue(manifest, tmp_path)

    assert "shard checksum does not match" in errors
    assert "task contract is invalid" in errors


def test_queue_validator_rejects_rehashed_outcome_field(
    specification: dict, tmp_path: Path
):
    index = _packet_index(specification, _packet(packet_id="1" * 64))
    manifest = build_adjudication_work_queue(
        specification, index, tmp_path, shard_count=2
    )
    shard = manifest["shards"][0]
    shard_path = tmp_path / shard["shard_file"]
    task = json.loads(shard_path.read_text())
    task["survival_outcome"] = 0.5
    task["task_sha256"] = task_sha256(task)
    payload = (
        json.dumps(task, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    shard_path.write_bytes(payload)
    shard["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["queue_manifest_sha256"] = queue_manifest_sha256(manifest)

    errors = validate_adjudication_work_queue(manifest, tmp_path)

    assert "task contract is invalid" in errors


def test_queue_cli_runs_end_to_end(specification: dict, tmp_path: Path):
    index = _packet_index(specification, _packet(packet_id="1" * 64))
    specification_path = tmp_path / "specification.json"
    index_path = tmp_path / "packets.json"
    output_root = tmp_path / "queue"
    manifest_path = tmp_path / "queue-manifest.json"
    specification_path.write_text(json.dumps(specification))
    index_path.write_text(json.dumps(index))

    assert (
        main(
            [
                "--specification",
                str(specification_path),
                "--packet-index",
                str(index_path),
                "--output-root",
                str(output_root),
                "--manifest",
                str(manifest_path),
                "--shard-count",
                "2",
            ]
        )
        == 0
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["packet_count"] == 1
    assert len(_tasks(output_root, manifest)) == 1


def test_queue_cli_rejects_non_object_input(tmp_path: Path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]")

    with pytest.raises(AdjudicationQueueError, match="must be an object"):
        main(
            [
                "--specification",
                str(invalid),
                "--packet-index",
                str(invalid),
                "--output-root",
                str(tmp_path / "queue"),
                "--manifest",
                str(tmp_path / "manifest.json"),
            ]
        )
