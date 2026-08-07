import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_ai_ban_review_compile_cli as compile_cli
import authorship.sourcegraph_ai_ban_review_cli as review_cli
import authorship.sourcegraph_ai_ban_review_io as review_io
from authorship.sourcegraph_ai_ban_review import (
    AiBanReviewError,
    ai_ban_ledger_sha256,
    ai_ban_response_sha256,
    ai_ban_target_manifest_sha256,
    ai_ban_worksheet_sha256,
    build_ai_ban_review_worksheet,
    build_ai_ban_target_manifest,
    compile_ai_ban_review_responses,
    required_ai_ban_packet_ids,
)
from authorship.sourcegraph_discovery import evidence_packet_sha256
from authorship.sourcegraph_repository_cases import (
    case_index_sha256,
    repository_case_sha256,
)


def _event(index: int) -> dict:
    return {
        "candidate_event": {
            "commit_oid": f"{index + 1:040x}",
            "observed_at": f"2025-01-{index + 1:02d}T00:00:00Z",
        },
        "event_id": f"{index + 100:064x}",
        "packet_count": 1,
        "packet_ids": [f"{index + 200:064x}"],
        "packet_type": "ai_ban_policy",
        "query_family_ids": ["ai_ban_policy_files"],
    }


def _case(repository: str, events: list[dict], index: int) -> dict:
    document = {
        "case_version": 3,
        "case_id": f"{index + 300:064x}",
        "canonical_repository_id": repository,
        "canonical_source_url": f"https://github.com/{repository}",
        "sourcegraph_name": f"github.com/sg-evals/{repository.replace('/', '-')}",
        "cutoff_commit": "c" * 40,
        "workflow": {
            "workflow_sha256": "d" * 64,
            "specification_sha256": "e" * 64,
            "packet_index_sha256": "f" * 64,
        },
        "packet_count": sum(event["packet_count"] for event in events),
        "event_count": len(events),
        "events": events,
        "queues": {
            "adoption_anchor_candidates": [],
            "adoption_challenge_candidates": [],
            "ai_ban_candidates": events,
            "explicit_provenance_candidates": [],
        },
        "outcomes_consulted": False,
    }
    return {
        **document,
        "repository_case_sha256": repository_case_sha256(document),
    }


def _index_record(repository: str, *, mirror: bool = True) -> dict:
    mirror_name = f"github.com/sg-evals/{repository.replace('/', '-')}"
    sourcegraph = {
        "direct": {
            "name": f"github.com/{repository}",
            "state": "not_indexed" if mirror else "indexed",
            "cutoff_state": "not_accessible" if mirror else "accessible",
            "cutoff_oid": None if mirror else "c" * 40,
        },
        "mirror": {
            "name": mirror_name,
            "state": "indexed" if mirror else "not_indexed",
            "cutoff_state": "accessible" if mirror else "not_accessible",
            "cutoff_oid": "c" * 40 if mirror else None,
        },
        "selected_name": mirror_name if mirror else f"github.com/{repository}",
    }
    return {
        "canonical_repository_id": repository,
        "canonical_source_url": f"https://github.com/{repository}",
        "roles": ["adoption_ai_ban_control_seed"],
        "cutoff_commit": "c" * 40,
        "sourcegraph": sourcegraph,
    }


def _inputs():
    repositories = [f"org/repo-{index}" for index in range(17)]
    control = {"since": "2024-01-01", "repos": repositories, "dropped": []}
    index_records = [
        _index_record(repository, mirror=index != 2)
        for index, repository in enumerate(repositories)
        if index != 4
    ]
    cases = {}
    for index, repository in enumerate(repositories):
        if index in {2, 3, 4}:
            continue
        events = [] if index == 1 else [_event(index + 20)]
        if index == 0:
            events = [_event(1), _event(0)]
        cases[repository] = _case(repository, events, index)
    case_records = [
        {
            "canonical_repository_id": repository,
            "case_id": case["case_id"],
            "case_file": f"cases/{case['case_id']}.json",
            "sha256": f"{index + 500:064x}",
            "byte_count": 1,
        }
        for index, (repository, case) in enumerate(cases.items())
    ]
    case_index_document = {
        "case_index_version": 3,
        "specification_sha256": "e" * 64,
        "workflow_sha256": "d" * 64,
        "packet_index_sha256": "f" * 64,
        "review_unit": "repository_event",
        "packet_level_exhaustive_review_required": False,
        "repository_count": len(case_records),
        "event_count": sum(case["event_count"] for case in cases.values()),
        "packet_count": sum(case["packet_count"] for case in cases.values()),
        "repositories": case_records,
        "outcomes_consulted": False,
    }
    case_index = {
        **case_index_document,
        "case_index_sha256": case_index_sha256(case_index_document),
    }
    index_manifest = {
        "outcomes_consulted": False,
        "repositories": index_records,
    }
    predecessors = {
        "control_evidence_file_sha256": "1" * 64,
        "index_manifest_file_sha256": "2" * 64,
        "case_index_file_sha256": "3" * 64,
        "case_index_sha256": case_index["case_index_sha256"],
        "packet_index_sha256": "f" * 64,
        "specification_sha256": "e" * 64,
        "workflow_sha256": "d" * 64,
    }
    return control, index_manifest, case_index, cases, predecessors


def _target() -> dict:
    return build_ai_ban_target_manifest(*_inputs())


def _packets(target: dict) -> list[dict]:
    packets = []
    for record in target["repositories"]:
        for candidate in record["candidates"]:
            for packet_id in candidate["packet_ids"]:
                document = {
                    "packet_version": 3,
                    "packet_id": packet_id,
                    "packet_type": "ai_ban_policy",
                    "canonical_repository_id": record["canonical_repository_id"],
                    "canonical_source_url": record["canonical_source_url"],
                    "sourcegraph_name": record["sourcegraph_name"],
                    "cutoff_commit": record["cutoff_commit"],
                    "indexed_revision_oid": record["cutoff_commit"],
                    "query_family_id": "ai_ban_policy_files",
                    "rendered_query": "frozen query",
                    "rendered_query_sha256": "9" * 64,
                    "sourcegraph_result_ids": ["sha256:" + "8" * 64],
                    "candidate_event": candidate["candidate_event"],
                    "raw_evidence": [
                        {
                            "kind": "file_content",
                            "commit_oid": candidate["candidate_event"]["commit_oid"],
                            "path": "CONTRIBUTING.md",
                            "line": 1,
                            "value": "review this policy text",
                            "source_url": (f"https://sourcegraph.example/{packet_id}"),
                        }
                    ],
                    "outcomes_consulted": False,
                }
                packets.append(
                    {
                        **document,
                        "packet_sha256": evidence_packet_sha256(document),
                    }
                )
    return packets


def _schema(name: str) -> dict:
    return json.loads((Path(__file__).parents[1] / "study" / name).read_text())


def _response(task: dict, worksheet: dict, decision: str, reviewer: str) -> dict:
    accepted = decision == "accept_policy"
    document = {
        "response_version": 3,
        "worksheet_sha256": worksheet["worksheet_sha256"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "canonical_repository_id": task["canonical_repository_id"],
        "event_id": task["event_id"],
        "review_role": task["review_role"],
        "reviewer_id": reviewer,
        "decision": decision,
        "default_branch_supported": accepted,
        "policy_scope": (
            "repository_wide_ai_generated_contribution_prohibition"
            if accepted
            else "not_applicable"
        ),
        "evidence_tier": (
            "datable_default_branch_policy" if accepted else "not_applicable"
        ),
        "rationale": "fresh semantic review",
        "evidence_citations": [task["evidence"][0]["raw_evidence"][0]["source_url"]],
        "outcomes_consulted": False,
    }
    return {**document, "response_sha256": ai_ban_response_sha256(document)}


def test_target_manifest_accounts_for_bounded_frame_and_exclusions():
    target = _target()

    assert target["repository_count"] == 17
    assert target["eligible_repository_count"] == 13
    assert target["excluded_repository_count"] == 4
    assert target["exclusion_counts"] == {
        "direct_index_only": 1,
        "missing_frozen_case": 1,
        "missing_index_manifest_record": 1,
        "no_frozen_ai_ban_candidate": 1,
    }
    assert target["repositories"][0]["candidate_count"] == 2
    assert [
        candidate["candidate_event"]["observed_at"]
        for candidate in target["repositories"][0]["candidates"]
    ] == ["2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"]
    assert target["outcomes_consulted"] is False
    assert target["scip_required"] is False
    assert target["paid_api_used"] is False
    assert target["openai_api_key_used"] is False
    assert target["target_manifest_sha256"] == ai_ban_target_manifest_sha256(target)
    jsonschema.validate(
        target, _schema("sourcegraph-ai-ban-target-manifest.schema.json")
    )


def test_target_manifest_accepts_preregistered_repository_object():
    control, index_manifest, case_index, cases, predecessors = _inputs()
    control["repos"] = {repository: {"policy": []} for repository in control["repos"]}

    target = build_ai_ban_target_manifest(
        control, index_manifest, case_index, cases, predecessors
    )

    assert target["repository_count"] == 17


def test_worksheet_selects_one_earliest_event_and_preserves_raw_evidence():
    target = _target()
    worksheet = build_ai_ban_review_worksheet(
        target,
        _packets(target),
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )

    assert worksheet["task_count"] == target["eligible_repository_count"]
    first = next(
        task
        for task in worksheet["tasks"]
        if task["canonical_repository_id"] == "org/repo-0"
    )
    assert first["candidate_position"] == 1
    assert first["candidate_count"] == 2
    assert first["review_role"] == "primary"
    assert first["evidence"][0]["raw_evidence"][0]["value"] == (
        "review this policy text"
    )
    assert first["allowed_decisions"] == [
        "accept_policy",
        "ambiguous",
        "insufficient",
        "reject",
    ]
    assert worksheet["outcomes_consulted"] is False
    assert worksheet["classifier_outcomes_consulted"] is False
    assert worksheet["survival_outcomes_consulted"] is False
    assert worksheet["worksheet_sha256"] == ai_ban_worksheet_sha256(worksheet)
    jsonschema.validate(
        worksheet, _schema("sourcegraph-ai-ban-review-worksheet.schema.json")
    )


def test_reject_advances_to_next_event_and_accept_stops_repository():
    target = _target()
    packets = _packets(target)
    worksheet = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    task = next(
        item
        for item in worksheet["tasks"]
        if item["canonical_repository_id"] == "org/repo-0"
    )
    responses = [
        _response(item, worksheet, "accept_policy", f"reviewer-{index}")
        for index, item in enumerate(worksheet["tasks"])
    ]
    responses[worksheet["tasks"].index(task)] = _response(
        task, worksheet, "reject", "reviewer-reject"
    )
    ledger = compile_ai_ban_review_responses(
        worksheet,
        responses,
        prior_ledger=None,
        expected_worksheet_sha256=worksheet["worksheet_sha256"],
        expected_reviewer_ids={
            response["task_id"]: response["reviewer_id"] for response in responses
        },
    )

    next_worksheet = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=ledger,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )

    assert next_worksheet["task_count"] == 1
    assert next_worksheet["tasks"][0]["canonical_repository_id"] == "org/repo-0"
    assert next_worksheet["tasks"][0]["candidate_position"] == 2
    assert required_ai_ban_packet_ids(target, ledger) == {
        next_worksheet["tasks"][0]["packet_ids"][0]
    }
    assert ledger["decision_ledger_sha256"] == ai_ban_ledger_sha256(ledger)
    jsonschema.validate(
        responses[0], _schema("sourcegraph-ai-ban-review-response.schema.json")
    )


def test_ambiguous_event_routes_to_distinct_secondary_then_resolver():
    target = _target()
    packets = _packets(target)
    worksheet = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    responses = [
        _response(item, worksheet, "accept_policy", f"reviewer-{index}")
        for index, item in enumerate(worksheet["tasks"])
    ]
    task = worksheet["tasks"][0]
    responses[0] = _response(task, worksheet, "ambiguous", "primary-reviewer")
    ledger = compile_ai_ban_review_responses(
        worksheet,
        responses,
        prior_ledger=None,
        expected_worksheet_sha256=worksheet["worksheet_sha256"],
        expected_reviewer_ids={
            response["task_id"]: response["reviewer_id"] for response in responses
        },
    )
    secondary = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=ledger,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    assert secondary["tasks"][0]["review_role"] == "secondary"
    secondary_response = _response(
        secondary["tasks"][0], secondary, "accept_policy", "secondary-reviewer"
    )
    ledger = compile_ai_ban_review_responses(
        secondary,
        [secondary_response],
        prior_ledger=ledger,
        expected_worksheet_sha256=secondary["worksheet_sha256"],
        expected_reviewer_ids={secondary_response["task_id"]: "secondary-reviewer"},
    )
    resolver = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=ledger,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    assert resolver["tasks"][0]["review_role"] == "resolver"
    assert resolver["tasks"][0]["allowed_decisions"] == [
        "accept_policy",
        "insufficient",
        "reject",
    ]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda response, _task: response.update(
                evidence_citations=["https://sourcegraph.example/other"]
            ),
            "task-local",
        ),
        (
            lambda response, task: response.update(
                reviewer_id=task.get("primary_reviewer_id", "same")
            ),
            "reviewer",
        ),
        (
            lambda response, _task: response.update(
                policy_scope="training_data_use_only"
            ),
            "policy scope",
        ),
    ],
)
def test_response_compiler_fails_closed(mutator, message):
    target = _target()
    worksheet = build_ai_ban_review_worksheet(
        target,
        _packets(target),
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    responses = [
        _response(item, worksheet, "accept_policy", f"reviewer-{index}")
        for index, item in enumerate(worksheet["tasks"])
    ]
    mutator(responses[0], worksheet["tasks"][0])

    with pytest.raises(AiBanReviewError, match=message):
        compile_ai_ban_review_responses(
            worksheet,
            responses,
            prior_ledger=None,
            expected_worksheet_sha256=worksheet["worksheet_sha256"],
            expected_reviewer_ids={
                response["task_id"]: (
                    "reviewer-0"
                    if index == 0 and message == "reviewer"
                    else response["reviewer_id"]
                )
                for index, response in enumerate(responses)
            },
        )


def test_prior_ledger_cannot_forge_target_task_identity():
    target = _target()
    packets = _packets(target)
    worksheet = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    response = _response(worksheet["tasks"][0], worksheet, "reject", "reviewer")
    ledger = compile_ai_ban_review_responses(
        worksheet,
        [
            response,
            *[
                _response(task, worksheet, "accept_policy", f"reviewer-{index}")
                for index, task in enumerate(worksheet["tasks"][1:])
            ],
        ],
        prior_ledger=None,
        expected_worksheet_sha256=worksheet["worksheet_sha256"],
        expected_reviewer_ids={
            worksheet["tasks"][0]["task_id"]: "reviewer",
            **{
                task["task_id"]: f"reviewer-{index}"
                for index, task in enumerate(worksheet["tasks"][1:])
            },
        },
    )
    ledger["decisions"][0]["task_id"] = "0" * 64
    ledger["decisions"][0]["response_sha256"] = ai_ban_response_sha256(
        ledger["decisions"][0]
    )
    ledger["decision_ledger_sha256"] = ai_ban_ledger_sha256(ledger)

    with pytest.raises(AiBanReviewError, match="outside target scope"):
        build_ai_ban_review_worksheet(
            target,
            packets,
            decision_ledger=ledger,
            expected_target_manifest_sha256=target["target_manifest_sha256"],
        )


def test_cli_requires_frozen_pins_and_writes_both_artifacts(monkeypatch, tmp_path):
    target = _target()
    worksheet = build_ai_ban_review_worksheet(
        target,
        _packets(target),
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    monkeypatch.setattr(
        review_cli,
        "materialize_ai_ban_review",
        lambda **_kwargs: (target, worksheet),
    )
    target_path = tmp_path / "target.json"
    worksheet_path = tmp_path / "worksheet.json"

    assert (
        review_cli.main(
            [
                "--expected-control-evidence-sha256",
                "1" * 64,
                "--expected-index-manifest-sha256",
                "2" * 64,
                "--expected-case-index-file-sha256",
                "3" * 64,
                "--expected-case-index-sha256",
                target["predecessors"]["case_index_sha256"],
                "--expected-packet-index-sha256",
                "f" * 64,
                "--target-output",
                str(target_path),
                "--worksheet-output",
                str(worksheet_path),
            ]
        )
        == 0
    )
    assert json.loads(target_path.read_text()) == target
    assert json.loads(worksheet_path.read_text()) == worksheet


def _write_json(path: Path, document: dict) -> str:
    payload = json.dumps(document).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_readers_fail_closed_on_pin_drift_and_invalid_json(tmp_path):
    path = tmp_path / "document.json"
    digest = _write_json(path, {"value": 1})

    assert review_io._read_pinned(path, digest, "fixture") == {"value": 1}
    with pytest.raises(AiBanReviewError, match="independent pin"):
        review_io._read_pinned(path, "0" * 64, "fixture")

    path.write_text("{")
    with pytest.raises(AiBanReviewError, match="JSON is invalid"):
        review_io._read_json(path, "fixture")


def test_selected_packets_exhausts_stream_and_rejects_missing(monkeypatch, tmp_path):
    packets = [{"packet_id": "a"}, {"packet_id": "b"}]
    monkeypatch.setattr(
        review_io,
        "validated_packet_stream_from_file",
        lambda *_args: (
            {"packet_index_sha256": "f" * 64},
            iter(packets),
        ),
    )

    selected = review_io._selected_packets(
        {}, {}, tmp_path / "packets.json", {"b"}, "f" * 64
    )
    assert selected == [{"packet_id": "b"}]

    with pytest.raises(AiBanReviewError, match="missing evidence packet"):
        review_io._selected_packets({}, {}, tmp_path / "packets.json", {"c"}, "f" * 64)


def test_materialize_binds_independently_pinned_inputs(monkeypatch, tmp_path):
    control, index_manifest, case_index, cases, _predecessors = _inputs()
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("control", "manifest", "case_index", "discovery", "workflow")
    }
    control_sha = _write_json(paths["control"], control)
    manifest_sha = _write_json(paths["manifest"], index_manifest)
    case_index_sha = _write_json(paths["case_index"], case_index)
    _write_json(paths["discovery"], {})
    _write_json(paths["workflow"], {})
    monkeypatch.setattr(review_io, "_load_cases", lambda *_args: cases)
    monkeypatch.setattr(
        review_io,
        "_selected_packets",
        lambda *_args: _packets(
            build_ai_ban_target_manifest(
                control,
                index_manifest,
                case_index,
                cases,
                {
                    "control_evidence_file_sha256": control_sha,
                    "index_manifest_file_sha256": manifest_sha,
                    "case_index_file_sha256": case_index_sha,
                    "case_index_sha256": case_index["case_index_sha256"],
                    "packet_index_sha256": "f" * 64,
                    "specification_sha256": "e" * 64,
                    "workflow_sha256": "d" * 64,
                },
            )
        ),
    )

    target, worksheet = review_io.materialize_ai_ban_review(
        control_evidence_path=paths["control"],
        index_manifest_path=paths["manifest"],
        case_index_path=paths["case_index"],
        case_root=tmp_path,
        discovery_path=paths["discovery"],
        workflow_path=paths["workflow"],
        packet_index_path=tmp_path / "packets.json",
        expected_control_evidence_sha256=control_sha,
        expected_index_manifest_sha256=manifest_sha,
        expected_case_index_file_sha256=case_index_sha,
        expected_case_index_sha256=case_index["case_index_sha256"],
        expected_packet_index_sha256="f" * 64,
    )

    assert target["repository_count"] == 17
    assert worksheet["task_count"] == target["eligible_repository_count"]


def test_materialize_advances_from_independently_pinned_decision_ledger(
    monkeypatch,
    tmp_path,
):
    control, index_manifest, case_index, cases, _predecessors = _inputs()
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("control", "manifest", "case_index", "discovery", "workflow")
    }
    control_sha = _write_json(paths["control"], control)
    manifest_sha = _write_json(paths["manifest"], index_manifest)
    case_index_sha = _write_json(paths["case_index"], case_index)
    _write_json(paths["discovery"], {})
    _write_json(paths["workflow"], {})
    target = build_ai_ban_target_manifest(
        control,
        index_manifest,
        case_index,
        cases,
        {
            "control_evidence_file_sha256": control_sha,
            "index_manifest_file_sha256": manifest_sha,
            "case_index_file_sha256": case_index_sha,
            "case_index_sha256": case_index["case_index_sha256"],
            "packet_index_sha256": "f" * 64,
            "specification_sha256": "e" * 64,
            "workflow_sha256": "d" * 64,
        },
    )
    packets = _packets(target)
    first = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    responses = [
        _response(task, first, "accept_policy", f"reviewer-{index}")
        for index, task in enumerate(first["tasks"])
    ]
    responses[0] = _response(first["tasks"][0], first, "reject", "reviewer-reject")
    ledger = compile_ai_ban_review_responses(
        first,
        responses,
        prior_ledger=None,
        expected_worksheet_sha256=first["worksheet_sha256"],
        expected_reviewer_ids={
            response["task_id"]: response["reviewer_id"] for response in responses
        },
    )
    ledger_path = tmp_path / "ledger.json"
    _write_json(ledger_path, ledger)
    monkeypatch.setattr(review_io, "_load_cases", lambda *_args: cases)
    monkeypatch.setattr(review_io, "_selected_packets", lambda *_args: packets)

    _target_document, worksheet = review_io.materialize_ai_ban_review(
        control_evidence_path=paths["control"],
        index_manifest_path=paths["manifest"],
        case_index_path=paths["case_index"],
        case_root=tmp_path,
        discovery_path=paths["discovery"],
        workflow_path=paths["workflow"],
        packet_index_path=tmp_path / "packets.json",
        decision_ledger_path=ledger_path,
        expected_control_evidence_sha256=control_sha,
        expected_index_manifest_sha256=manifest_sha,
        expected_case_index_file_sha256=case_index_sha,
        expected_case_index_sha256=case_index["case_index_sha256"],
        expected_packet_index_sha256="f" * 64,
        expected_decision_ledger_sha256=ledger["decision_ledger_sha256"],
    )

    assert worksheet["task_count"] == 1
    assert worksheet["tasks"][0]["candidate_position"] == 2
    assert (
        worksheet["predecessors"]["prior_decision_ledger_sha256"]
        == ledger["decision_ledger_sha256"]
    )


def test_materialize_requires_both_decision_ledger_and_independent_pin(tmp_path):
    arguments = {
        "control_evidence_path": tmp_path / "control.json",
        "index_manifest_path": tmp_path / "manifest.json",
        "case_index_path": tmp_path / "case-index.json",
        "case_root": tmp_path,
        "discovery_path": tmp_path / "discovery.json",
        "workflow_path": tmp_path / "workflow.json",
        "packet_index_path": tmp_path / "packets.json",
        "expected_control_evidence_sha256": "1" * 64,
        "expected_index_manifest_sha256": "2" * 64,
        "expected_case_index_file_sha256": "3" * 64,
        "expected_case_index_sha256": "4" * 64,
        "expected_packet_index_sha256": "5" * 64,
    }

    with pytest.raises(AiBanReviewError, match="requires.*pin"):
        review_io.materialize_ai_ban_review(
            **arguments,
            decision_ledger_path=tmp_path / "ledger.json",
        )
    with pytest.raises(AiBanReviewError, match="has no input"):
        review_io.materialize_ai_ban_review(
            **arguments,
            expected_decision_ledger_sha256="6" * 64,
        )


def test_compile_cli_writes_independently_pinned_decision_ledger(tmp_path):
    target = _target()
    worksheet = build_ai_ban_review_worksheet(
        target,
        _packets(target),
        decision_ledger=None,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    responses = [
        _response(task, worksheet, "reject", "primary-reviewer")
        for task in worksheet["tasks"]
    ]
    worksheet_path = tmp_path / "worksheet.json"
    responses_path = tmp_path / "responses.json"
    output_path = tmp_path / "ledger.json"
    _write_json(worksheet_path, worksheet)
    _write_json(responses_path, responses)

    assert (
        compile_cli.main(
            [
                "--worksheet",
                str(worksheet_path),
                "--expected-worksheet-sha256",
                worksheet["worksheet_sha256"],
                "--response-bundle",
                str(responses_path),
                "--reviewer-id",
                "primary-reviewer",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    ledger = json.loads(output_path.read_text())
    assert ledger["decision_count"] == worksheet["task_count"]
    assert ledger["decision_ledger_sha256"] == ai_ban_ledger_sha256(ledger)


def test_compile_cli_requires_prior_ledger_and_pin_together(tmp_path):
    arguments = [
        "--worksheet",
        str(tmp_path / "worksheet.json"),
        "--expected-worksheet-sha256",
        "1" * 64,
        "--response-bundle",
        str(tmp_path / "responses.json"),
        "--reviewer-id",
        "reviewer",
        "--output",
        str(tmp_path / "ledger.json"),
    ]

    with pytest.raises(AiBanReviewError, match="requires.*pin"):
        compile_cli.main(
            [
                *arguments,
                "--prior-ledger",
                str(tmp_path / "prior.json"),
            ]
        )
    with pytest.raises(AiBanReviewError, match="has no input"):
        compile_cli.main(
            [
                *arguments,
                "--expected-prior-ledger-sha256",
                "2" * 64,
            ]
        )
