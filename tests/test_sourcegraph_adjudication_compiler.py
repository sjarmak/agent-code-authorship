import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_adjudication_compiler import (
    AdjudicationCompilerError,
    build_followup_assignments,
    bundle_manifest_sha256,
    compile_adjudication_bundles,
    followup_assignment_sha256,
    load_compiled_bundles,
    review_response_sha256,
)
from authorship.sourcegraph_adjudication_compiler_cli import main
from authorship.sourcegraph_adjudication_followup_cli import (
    main as followup_main,
)
from authorship.sourcegraph_adjudication_queue import (
    build_adjudication_work_queue,
)
from authorship.sourcegraph_adjudication_freeze import (
    build_adjudication_freeze,
)
from authorship.sourcegraph_discovery import (
    agreement_audit_selected,
    evidence_packet_sha256,
    validate_adjudication_bundle,
)
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

CUTOFF = "a" * 40


def _packet(packet_id: str, commit_oid: str) -> dict:
    query = (
        f"repo:^github\\.com/sg-evals/org-repo$@{CUTOFF} "
        "type:commit Co-authored-by:.*Claude count:all patternType:regexp"
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
                "source_url": (f"https://github.com/org/repo/commit/{commit_oid}"),
            }
        ],
        "outcomes_consulted": False,
    }
    return {**packet, "packet_sha256": evidence_packet_sha256(packet)}


def _selected_packet(specification: dict, selected: bool, commit: str) -> dict:
    audit = specification["adjudication"]["agreement_audit"]
    offset = int(commit[:2], 16) * 10_000
    for candidate in range(10_000):
        packet = _packet(f"{offset + candidate:064x}", commit)
        if agreement_audit_selected(packet["packet_sha256"], audit) is selected:
            return packet
    raise AssertionError("could not find packet with requested audit selection")


def _packet_index(specification: dict, packets: list[dict]) -> dict:
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
    records = []
    for shard in manifest["shards"]:
        path = root / shard["shard_file"]
        records.extend(
            json.loads(line) for line in path.read_bytes().split(b"\n") if line
        )
    return sorted(records, key=lambda task: task["task_id"])


def _response(
    queue_manifest: dict,
    *,
    stage: str,
    reviewer_id: str,
    tasks: list[dict],
    decisions: dict[str, str],
) -> dict:
    records = [
        {
            "task_id": task["task_id"],
            "task_sha256": task["task_sha256"],
            "decision": decisions[task["task_id"]],
            "rationale": f"{reviewer_id} applied the frozen rubric.",
        }
        for task in sorted(tasks, key=lambda task: task["task_id"])
    ]
    document = {
        "review_response_version": 3,
        "queue_manifest_sha256": queue_manifest["queue_manifest_sha256"],
        "stage": stage,
        "reviewer_id": reviewer_id,
        "reviewer_kind": "human",
        "reviewer_version": "reviewer-protocol-v3",
        "decision_count": len(records),
        "decisions": records,
        "outcome_blind": True,
        "peer_review_blind": True,
    }
    return {
        **document,
        "review_response_sha256": review_response_sha256(document),
    }


@pytest.fixture
def setup_queue(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    specification = json.loads(
        (root / "study/sourcegraph-discovery.v3.json").read_text()
    )
    selected = _selected_packet(specification, True, "b" * 40)
    disagreement = _selected_packet(specification, False, "d" * 40)
    unselected = _selected_packet(specification, False, "e" * 40)
    index = _packet_index(specification, [selected, disagreement, unselected])
    queue_root = tmp_path / "queue"
    queue = build_adjudication_work_queue(
        specification, index, queue_root, shard_count=2
    )
    tasks = _tasks(queue_root, queue)
    task_by_commit = {task["candidate_event"]["commit_oid"]: task for task in tasks}
    decisions_a = {task["task_id"]: "confirmed" for task in tasks}
    decisions_b = dict(decisions_a)
    decisions_b[task_by_commit["d" * 40]["task_id"]] = "observed"
    primary_a = _response(
        queue,
        stage="primary",
        reviewer_id="reviewer-a",
        tasks=tasks,
        decisions=decisions_a,
    )
    primary_b = _response(
        queue,
        stage="primary",
        reviewer_id="reviewer-b",
        tasks=tasks,
        decisions=decisions_b,
    )
    resolution_task = task_by_commit["d" * 40]
    resolution = _response(
        queue,
        stage="resolution",
        reviewer_id="reviewer-c",
        tasks=[resolution_task],
        decisions={resolution_task["task_id"]: "observed"},
    )
    audit_task = task_by_commit["b" * 40]
    audit = _response(
        queue,
        stage="agreement_audit",
        reviewer_id="reviewer-c",
        tasks=[audit_task],
        decisions={audit_task["task_id"]: "confirmed"},
    )
    return {
        "specification": specification,
        "index": index,
        "queue": queue,
        "queue_root": queue_root,
        "tasks": tasks,
        "primary_a": primary_a,
        "primary_b": primary_b,
        "resolution": resolution,
        "audit": audit,
    }


def test_compiler_expands_group_reviews_into_valid_packet_bundles(
    setup_queue: dict, tmp_path: Path
):
    output_root = tmp_path / "bundles"
    result = compile_adjudication_bundles(
        setup_queue["specification"],
        setup_queue["queue"],
        setup_queue["queue_root"],
        [setup_queue["primary_a"], setup_queue["primary_b"]],
        output_root,
        resolution_response=setup_queue["resolution"],
        audit_response=setup_queue["audit"],
        shard_count=2,
    )
    bundles = load_compiled_bundles(result, output_root)

    assert result["packet_count"] == setup_queue["index"]["packet_count"]
    assert result["task_count"] == 3
    assert result["disagreement_task_count"] == 1
    assert result["agreement_audit_task_count"] == 1
    assert result["bundle_manifest_sha256"] == bundle_manifest_sha256(result)
    assert len(bundles) == 3
    assert all(
        not validate_adjudication_bundle(bundle, setup_queue["specification"])
        for bundle in bundles
    )
    by_commit = {
        next(
            packet["candidate_event"]["commit_oid"]
            for packet in setup_queue["index"]["packets"]
            if packet["packet_id"] == bundle["packet_id"]
        ): bundle
        for bundle in bundles
    }
    assert [review["stage"] for review in by_commit["b" * 40]["reviews"]] == [
        "primary",
        "primary",
        "agreement_audit",
    ]
    assert [review["stage"] for review in by_commit["d" * 40]["reviews"]] == [
        "primary",
        "primary",
        "resolution",
    ]
    assert [review["stage"] for review in by_commit["e" * 40]["reviews"]] == [
        "primary",
        "primary",
    ]
    frozen = build_adjudication_freeze(
        setup_queue["specification"],
        setup_queue["index"],
        bundles,
    )
    assert frozen["packet_count"] == 3
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-bundle-execution.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(result)
    response_schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-review-response.schema.json"
        ).read_text()
    )
    response_validator = jsonschema.Draft202012Validator(response_schema)
    for response in (
        setup_queue["primary_a"],
        setup_queue["primary_b"],
        setup_queue["resolution"],
        setup_queue["audit"],
    ):
        response_validator.validate(response)


def test_followup_assignments_expose_only_task_identity(setup_queue: dict):
    assignment = build_followup_assignments(
        setup_queue["specification"],
        setup_queue["queue"],
        setup_queue["queue_root"],
        [setup_queue["primary_a"], setup_queue["primary_b"]],
    )

    assert assignment["resolution_task_count"] == 1
    assert assignment["agreement_audit_task_count"] == 1
    assert assignment["primary_decisions_exposed"] is False
    assert assignment["outcomes_consulted"] is False
    assert assignment["followup_assignment_sha256"] == followup_assignment_sha256(
        assignment
    )
    assert not any(
        "decision" in task
        for stage in ("resolution_tasks", "agreement_audit_tasks")
        for task in assignment[stage]
    )
    expected = {task["task_id"]: task["task_sha256"] for task in setup_queue["tasks"]}
    assigned = {
        task["task_id"]: task["task_sha256"]
        for stage in ("resolution_tasks", "agreement_audit_tasks")
        for task in assignment[stage]
    }
    assert all(expected[task_id] == task_sha for task_id, task_sha in assigned.items())

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-adjudication-followup-assignment.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(assignment)


def test_compiler_requires_independent_exact_primary_coverage(
    setup_queue: dict, tmp_path: Path
):
    same_reviewer = copy.deepcopy(setup_queue["primary_b"])
    same_reviewer["reviewer_id"] = "reviewer-a"
    same_reviewer["review_response_sha256"] = review_response_sha256(same_reviewer)
    with pytest.raises(AdjudicationCompilerError, match="independent"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [setup_queue["primary_a"], same_reviewer],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=setup_queue["audit"],
        )
    incomplete = copy.deepcopy(setup_queue["primary_b"])
    incomplete["decisions"].pop()
    incomplete["decision_count"] -= 1
    incomplete["review_response_sha256"] = review_response_sha256(incomplete)
    with pytest.raises(AdjudicationCompilerError, match="coverage"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [setup_queue["primary_a"], incomplete],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=setup_queue["audit"],
        )


def test_compiler_requires_exact_resolution_and_agreement_audit(
    setup_queue: dict, tmp_path: Path
):
    with pytest.raises(AdjudicationCompilerError, match="resolution"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [setup_queue["primary_a"], setup_queue["primary_b"]],
            tmp_path / "bundles",
            audit_response=setup_queue["audit"],
        )
    with pytest.raises(AdjudicationCompilerError, match="agreement_audit"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [setup_queue["primary_a"], setup_queue["primary_b"]],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
        )
    exposed = copy.deepcopy(setup_queue["audit"])
    exposed["outcome_blind"] = False
    exposed["review_response_sha256"] = review_response_sha256(exposed)
    with pytest.raises(AdjudicationCompilerError, match="blind"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [setup_queue["primary_a"], setup_queue["primary_b"]],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=exposed,
        )


def test_review_response_rejects_rehashed_extra_fields(
    setup_queue: dict, tmp_path: Path
):
    changed = copy.deepcopy(setup_queue["primary_a"])
    changed["survival_outcome"] = 0.5
    changed["review_response_sha256"] = review_response_sha256(changed)

    with pytest.raises(AdjudicationCompilerError, match="unapproved"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [changed, setup_queue["primary_b"]],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=setup_queue["audit"],
        )
    with pytest.raises(AdjudicationCompilerError, match="primary response"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [{}, setup_queue["primary_b"]],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=setup_queue["audit"],
        )
    invalid_model = copy.deepcopy(setup_queue["primary_a"])
    invalid_model.update(
        {
            "reviewer_kind": "model",
            "provider": "example",
            "model_id": "reviewer-model",
            "model_version": "v1",
            "prompt_sha256": "invalid",
        }
    )
    invalid_model["review_response_sha256"] = review_response_sha256(invalid_model)
    with pytest.raises(AdjudicationCompilerError, match="model metadata"):
        compile_adjudication_bundles(
            setup_queue["specification"],
            setup_queue["queue"],
            setup_queue["queue_root"],
            [invalid_model, setup_queue["primary_b"]],
            tmp_path / "bundles",
            resolution_response=setup_queue["resolution"],
            audit_response=setup_queue["audit"],
        )


def test_compiler_cli_runs_end_to_end(setup_queue: dict, tmp_path: Path):
    paths = {}
    for name in (
        "specification",
        "queue",
        "primary_a",
        "primary_b",
        "resolution",
        "audit",
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(setup_queue[name]))
        paths[name] = path
    output_root = tmp_path / "compiled"
    manifest_path = tmp_path / "bundle-execution.json"

    assert (
        main(
            [
                "--specification",
                str(paths["specification"]),
                "--queue-manifest",
                str(paths["queue"]),
                "--queue-root",
                str(setup_queue["queue_root"]),
                "--primary-response",
                str(paths["primary_a"]),
                "--primary-response",
                str(paths["primary_b"]),
                "--resolution-response",
                str(paths["resolution"]),
                "--audit-response",
                str(paths["audit"]),
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
    assert len(load_compiled_bundles(manifest, output_root)) == 3


def test_followup_cli_writes_blind_assignments(setup_queue: dict, tmp_path: Path):
    paths = {}
    for name in ("specification", "queue", "primary_a", "primary_b"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(setup_queue[name]))
        paths[name] = path
    assignment_path = tmp_path / "followup-assignment.json"

    assert (
        followup_main(
            [
                "--specification",
                str(paths["specification"]),
                "--queue-manifest",
                str(paths["queue"]),
                "--queue-root",
                str(setup_queue["queue_root"]),
                "--primary-response",
                str(paths["primary_a"]),
                "--primary-response",
                str(paths["primary_b"]),
                "--assignment",
                str(assignment_path),
            ]
        )
        == 0
    )
    assignment = json.loads(assignment_path.read_text())
    assert assignment["resolution_task_count"] == 1
    assert assignment["agreement_audit_task_count"] == 1
    assert assignment["primary_decisions_exposed"] is False
