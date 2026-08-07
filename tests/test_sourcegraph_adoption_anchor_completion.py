import copy
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_adoption_anchor_completion as anchor_completion
import authorship.sourcegraph_adoption_anchor_compile_cli as anchor_compile_cli
import authorship.sourcegraph_adoption_anchor_completion_cli as anchor_cli
from authorship.sourcegraph_adoption_anchor_completion import (
    AnchorCompletionError,
    anchor_amendment_sha256,
    anchor_completion_ledger_sha256,
    anchor_completion_manifest_sha256,
    build_anchor_completion_manifest,
    compile_anchor_completion_responses,
)
from authorship.sourcegraph_adoption_review import (
    adoption_review_response_sha256,
    adoption_review_task_sha256,
)


def _event(identifier: str, observed_at: str, packet_id: str, family: str) -> dict:
    return {
        "event_id": identifier * 64,
        "candidate_event": {
            "commit_oid": identifier * 40,
            "observed_at": observed_at,
        },
        "query_family_ids": [family],
        "packet_ids": [packet_id * 64],
    }


def _case() -> dict:
    return {
        "case_id": "9" * 64,
        "repository_case_sha256": "8" * 64,
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": "f" * 40,
        "queues": {
            "adoption_challenge_candidates": [
                _event("1", "2024-01-01T00:00:00Z", "a", "adoption_announcement_files"),
                _event("2", "2024-02-01T00:00:00Z", "b", "adoption_announcement_files"),
                _event("4", "2024-03-01T00:00:00Z", "d", "adoption_announcement_files"),
            ],
            "adoption_anchor_candidates": [
                _event("3", "2024-03-01T00:00:00Z", "c", "agent_trailer_commits")
            ],
        },
    }


def _packets() -> dict:
    return {
        packet_id
        * 64: {
            "packet_id": packet_id * 64,
            "packet_sha256": packet_id * 64,
            "query_family_id": family,
            "raw_evidence": [
                {
                    "kind": "commit_message",
                    "commit_oid": packet_id * 40,
                    "path": None,
                    "line": None,
                    "value": value,
                    "source_url": f"https://example.test/{packet_id}",
                }
            ],
            "outcomes_consulted": False,
        }
        for packet_id, family, value in [
            ("a", "adoption_announcement_files", "challenge one"),
            ("b", "adoption_announcement_files", "challenge two"),
            ("c", "agent_trailer_commits", "explicit anchor"),
            ("d", "adoption_announcement_files", "same-time challenge"),
        ]
    }


def _decision(event_id: str, decision: str, tranche_number: int = 1) -> dict:
    return {
        "canonical_repository_id": "org/repo",
        "event_id": event_id,
        "reviewer_id": f"reviewer-{tranche_number}",
        "review_role": "primary",
        "tranche_number": tranche_number,
        "decision": decision,
        "outcomes_consulted": False,
    }


def _build(decisions: list[dict]) -> dict:
    return build_anchor_completion_manifest(
        {
            "case_index_sha256": "7" * 64,
            "workflow_sha256": "6" * 64,
            "outcomes_consulted": False,
        },
        [_case()],
        _packets(),
        decisions,
        amendment_sha256="d" * 64,
        source_decision_ledger_sha256="e" * 64,
        tranche_number=8,
    )


def _amendment(source_decision_ledger_sha256: str) -> dict:
    amendment = json.loads(
        (
            Path(__file__).parents[1]
            / "study"
            / "sourcegraph-adoption-anchor-amendment.v3.json"
        ).read_text()
    )
    amendment["source_decision_ledger_sha256"] = source_decision_ledger_sha256
    amendment["amendment_sha256"] = anchor_amendment_sha256(amendment)
    return amendment


def test_anchor_completion_selects_explicit_anchor_and_preserves_uncertainty():
    manifest = _build([_decision("1" * 64, "reject")])

    assert manifest["task_count"] == 1
    task = manifest["tasks"][0]
    assert task["candidate_kind"] == "explicit_provenance_anchor"
    assert task["candidate_position"] == 3
    assert task["candidate_count"] == 4
    assert manifest["selection_mode"] == "explicit_anchor_completion"
    assert len(manifest["tranche_id"]) == 64
    assert manifest["prehistory"] == [
        {
            "task_id": task["task_id"],
            "earliest_unresolved_position": 2,
            "unresolved_earlier_candidate_count": 1,
            "clean_prehistory": False,
        }
    ]
    assert task["evidence"][0]["raw_evidence"][0]["value"] == "explicit anchor"
    assert task["task_sha256"] == adoption_review_task_sha256(task)
    assert manifest["manifest_sha256"] == anchor_completion_manifest_sha256(manifest)
    assert manifest["outcomes_consulted"] is False


def test_same_timestamp_challenge_identity_does_not_change_anchor_prehistory():
    decisions = [
        _decision("1" * 64, "reject", 1),
        _decision("2" * 64, "reject", 2),
    ]
    high_id = _build(decisions)
    low_id_case = _case()
    low_id_case["queues"]["adoption_challenge_candidates"][2]["event_id"] = "0" * 64
    low_id = build_anchor_completion_manifest(
        {
            "case_index_sha256": "7" * 64,
            "workflow_sha256": "6" * 64,
            "outcomes_consulted": False,
        },
        [low_id_case],
        _packets(),
        decisions,
        amendment_sha256="d" * 64,
        source_decision_ledger_sha256="e" * 64,
        tranche_number=8,
    )

    assert high_id["tasks"] == low_id["tasks"]
    assert high_id["prehistory"] == low_id["prehistory"]
    assert high_id["prehistory"][0]["clean_prehistory"] is True
    assert high_id["prehistory"][0]["unresolved_earlier_candidate_count"] == 0


def test_anchor_completion_omits_repositories_already_completed_or_exhausted():
    completed = _build([_decision("1" * 64, "accept_observed")])
    exhausted = _build(
        [
            _decision("1" * 64, "reject", 1),
            _decision("2" * 64, "reject", 2),
            _decision("3" * 64, "reject", 3),
        ]
    )

    assert completed["task_count"] == 0
    assert completed["completed_repository_count"] == 1
    assert exhausted["task_count"] == 0
    assert exhausted["exhausted_repository_count"] == 1


def test_anchor_completion_rejects_open_peer_review_and_missing_anchor_packet():
    with pytest.raises(AnchorCompletionError, match="peer review"):
        _build([_decision("1" * 64, "ambiguous")])

    packets = _packets()
    del packets["c" * 64]
    with pytest.raises(AnchorCompletionError, match="missing evidence packet"):
        build_anchor_completion_manifest(
            {
                "case_index_sha256": "7" * 64,
                "workflow_sha256": "6" * 64,
                "outcomes_consulted": False,
            },
            [_case()],
            packets,
            [],
            amendment_sha256="d" * 64,
            source_decision_ledger_sha256="e" * 64,
            tranche_number=8,
        )


def test_anchor_completion_is_checksummed_against_amendment_and_source_ledger():
    manifest = _build([])
    tampered = copy.deepcopy(manifest)
    tampered["source_decision_ledger_sha256"] = "0" * 64

    assert anchor_completion_manifest_sha256(tampered) != manifest["manifest_sha256"]


def test_anchor_completion_compiler_binds_exact_responses_and_prehistory():
    manifest = _build([_decision("1" * 64, "reject")])
    task = manifest["tasks"][0]
    response = {
        "response_version": 3,
        "tranche_id": manifest["tranche_id"],
        "tranche_number": manifest["tranche_number"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "canonical_repository_id": task["canonical_repository_id"],
        "event_id": task["event_id"],
        "reviewer_id": "anchor-reviewer",
        "review_role": "primary",
        "decision": "accept_confirmed",
        "default_branch_supported": True,
        "evidence_tier": "confirmed",
        "rationale": "The explicit trailer establishes agent-authored code.",
        "evidence_citations": ["https://example.test/c"],
        "outcomes_consulted": False,
    }
    response["response_sha256"] = adoption_review_response_sha256(response)

    ledger = compile_anchor_completion_responses(manifest, [response])

    assert ledger["decision_count"] == 1
    assert ledger["prehistory"] == manifest["prehistory"]
    assert ledger["anchor_completion_ledger_sha256"] == (
        anchor_completion_ledger_sha256(ledger)
    )
    with pytest.raises(AnchorCompletionError, match="exactly cover"):
        compile_anchor_completion_responses(manifest, [])

    tampered = copy.deepcopy(manifest)
    tampered["prehistory"][0]["clean_prehistory"] = True
    with pytest.raises(AnchorCompletionError, match="manifest checksum"):
        compile_anchor_completion_responses(tampered, [response])

    invalid = {**response, "evidence_citations": ["https://example.test/not-local"]}
    invalid["response_sha256"] = adoption_review_response_sha256(invalid)
    with pytest.raises(AnchorCompletionError, match="citations"):
        compile_anchor_completion_responses(manifest, [invalid])


def test_anchor_completion_rejects_invalid_frame_and_tranche():
    case_index = {
        "case_index_sha256": "7" * 64,
        "workflow_sha256": "6" * 64,
        "outcomes_consulted": True,
    }
    with pytest.raises(AnchorCompletionError, match="outcome exposed"):
        build_anchor_completion_manifest(
            case_index,
            [_case()],
            _packets(),
            [],
            amendment_sha256="d" * 64,
            source_decision_ledger_sha256="e" * 64,
            tranche_number=8,
        )
    with pytest.raises(AnchorCompletionError, match="positive integer"):
        build_anchor_completion_manifest(
            {**case_index, "outcomes_consulted": False},
            [_case()],
            _packets(),
            [],
            amendment_sha256="d" * 64,
            source_decision_ledger_sha256="e" * 64,
            tranche_number=0,
        )
    with pytest.raises(AnchorCompletionError, match="follow all prior"):
        _build([_decision("1" * 64, "reject", tranche_number=8)])


def test_anchor_materializer_validates_frozen_bindings(monkeypatch, tmp_path):
    ledger = {"decision_ledger_sha256": "e" * 64}
    amendment = _amendment(ledger["decision_ledger_sha256"])
    case_index = {
        "case_index_sha256": "7" * 64,
        "workflow_sha256": "6" * 64,
        "packet_index_sha256": "5" * 64,
        "outcomes_consulted": False,
    }
    monkeypatch.setattr(anchor_completion, "_validate_review_protocol", lambda *_: None)
    monkeypatch.setattr(
        anchor_completion, "_load_repository_cases", lambda *_: [_case()]
    )
    monkeypatch.setattr(
        anchor_completion,
        "validate_adoption_decision_ledger",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        anchor_completion,
        "validated_packet_stream_from_file",
        lambda *_: ({"packet_index_sha256": "5" * 64}, iter(_packets().values())),
    )

    manifest = anchor_completion.materialize_anchor_completion_manifest(
        case_index,
        {},
        amendment,
        tmp_path,
        {},
        {},
        tmp_path / "packets.json",
        ledger,
        expected_decision_ledger_sha256="e" * 64,
        expected_amendment_sha256=amendment["amendment_sha256"],
        tranche_number=8,
    )

    assert manifest["task_count"] == 1
    with pytest.raises(AnchorCompletionError, match="frozen checksum"):
        anchor_completion.materialize_anchor_completion_manifest(
            case_index,
            {},
            amendment,
            tmp_path,
            {},
            {},
            tmp_path / "packets.json",
            ledger,
            expected_decision_ledger_sha256="0" * 64,
            expected_amendment_sha256=amendment["amendment_sha256"],
            tranche_number=8,
        )
    drifted_amendment = copy.deepcopy(amendment)
    drifted_amendment["selection_rule"] = {}
    drifted_amendment["amendment_sha256"] = anchor_amendment_sha256(drifted_amendment)
    with pytest.raises(AnchorCompletionError, match="policy"):
        anchor_completion.materialize_anchor_completion_manifest(
            case_index,
            {},
            drifted_amendment,
            tmp_path,
            {},
            {},
            tmp_path / "packets.json",
            ledger,
            expected_decision_ledger_sha256="e" * 64,
            expected_amendment_sha256=drifted_amendment["amendment_sha256"],
            tranche_number=8,
        )
    with pytest.raises(AnchorCompletionError, match="amendment.*frozen checksum"):
        anchor_completion.materialize_anchor_completion_manifest(
            case_index,
            {},
            amendment,
            tmp_path,
            {},
            {},
            tmp_path / "packets.json",
            ledger,
            expected_decision_ledger_sha256="e" * 64,
            expected_amendment_sha256="0" * 64,
            tranche_number=8,
        )


def test_anchor_amendment_schema_rejects_policy_rewrites():
    amendment = _amendment("e" * 64)
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "study"
            / "sourcegraph-adoption-anchor-amendment.schema.json"
        ).read_text()
    )
    jsonschema.validate(amendment, schema)
    amendment["review_execution"]["paid_batch_api"] = True

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(amendment, schema)


def test_anchor_completion_cli_writes_materialized_manifest(monkeypatch, tmp_path):
    paths = {
        name: tmp_path / f"{name}.json"
        for name in [
            "case-index",
            "review-protocol",
            "amendment",
            "discovery",
            "workflow",
            "ledger",
        ]
    }
    for path in paths.values():
        path.write_text("{}")
    output = tmp_path / "anchor-tranche.json"
    manifest = _build([])
    monkeypatch.setattr(
        anchor_cli,
        "materialize_anchor_completion_manifest",
        lambda *_args, **_kwargs: manifest,
    )

    result = anchor_cli.main(
        [
            "--case-index",
            str(paths["case-index"]),
            "--case-root",
            str(tmp_path),
            "--review-protocol",
            str(paths["review-protocol"]),
            "--amendment",
            str(paths["amendment"]),
            "--discovery-specification",
            str(paths["discovery"]),
            "--workflow-specification",
            str(paths["workflow"]),
            "--packet-index",
            str(tmp_path / "packets.json"),
            "--decisions",
            str(paths["ledger"]),
            "--expected-decision-ledger-sha256",
            "e" * 64,
            "--expected-amendment-sha256",
            "d" * 64,
            "--tranche-number",
            "8",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text()) == manifest


def test_anchor_compile_cli_rematerializes_before_writing(monkeypatch, tmp_path):
    manifest = _build([])
    decision_ledger = {"decision_ledger_sha256": "e" * 64}
    output = tmp_path / "anchor-ledger.json"
    written = {}

    def load_mapping(path):
        if path.name == "manifest.json":
            return manifest
        if path.name == "decisions.json":
            return decision_ledger
        return {}

    monkeypatch.setattr(anchor_compile_cli, "_load_mapping", load_mapping)
    monkeypatch.setattr(
        anchor_compile_cli,
        "materialize_anchor_completion_manifest",
        lambda *_args, **_kwargs: manifest,
    )
    monkeypatch.setattr(anchor_compile_cli, "_responses", lambda *_args: [])
    monkeypatch.setattr(
        anchor_compile_cli,
        "compile_anchor_completion_responses",
        lambda *_args: {
            "decision_count": 0,
            "anchor_completion_ledger_sha256": "f" * 64,
        },
    )
    monkeypatch.setattr(
        anchor_compile_cli,
        "write_adoption_review_json",
        lambda path, document: written.update(path=path, document=document),
    )

    result = anchor_compile_cli.main(
        [
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--response-bundle",
            str(tmp_path / "responses.json"),
            "--expected-manifest-sha256",
            manifest["manifest_sha256"],
            "--decisions",
            str(tmp_path / "decisions.json"),
            "--expected-decision-ledger-sha256",
            "e" * 64,
            "--expected-amendment-sha256",
            "d" * 64,
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert written["path"] == output
    assert written["document"]["anchor_completion_ledger_sha256"] == "f" * 64

    with pytest.raises(AnchorCompletionError, match="expected checksum"):
        anchor_compile_cli.main(
            [
                "--manifest",
                str(tmp_path / "manifest.json"),
                "--response-bundle",
                str(tmp_path / "responses.json"),
                "--expected-manifest-sha256",
                "0" * 64,
                "--decisions",
                str(tmp_path / "decisions.json"),
                "--expected-decision-ledger-sha256",
                "e" * 64,
                "--expected-amendment-sha256",
                "d" * 64,
                "--output",
                str(output),
            ]
        )
