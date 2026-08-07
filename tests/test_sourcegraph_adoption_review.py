import copy
import hashlib
import json
from pathlib import Path

import pytest

import authorship.sourcegraph_adoption_review as adoption_review
import authorship.sourcegraph_adoption_review_cli as adoption_review_cli
import authorship.sourcegraph_adoption_review_compile_cli as adoption_compile_cli
from authorship.sourcegraph_adoption_review import (
    AdoptionReviewError,
    adoption_review_ledger_sha256,
    adoption_review_protocol_sha256,
    adoption_review_response_sha256,
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
    build_adoption_review_tranche,
    build_adoption_decision_ledger,
    compile_adoption_review_responses,
    materialize_adoption_review_tranche,
    required_evidence_packet_ids,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_repository_cases import (
    case_index_sha256,
    repository_case_sha256,
)


def _event(
    identifier: str,
    observed_at: str,
    *,
    packet_id: str,
    family: str,
) -> dict:
    return {
        "event_id": identifier,
        "packet_type": "adoption_event",
        "candidate_event": {
            "commit_oid": identifier[0] * 40,
            "observed_at": observed_at,
        },
        "query_family_ids": [family],
        "packet_ids": [packet_id],
        "packet_count": 1,
    }


def _case() -> dict:
    challenge_1 = _event(
        "1" * 64,
        "2024-01-01T00:00:00Z",
        packet_id="a" * 64,
        family="adoption_announcement_files",
    )
    challenge_2 = _event(
        "2" * 64,
        "2024-02-01T00:00:00Z",
        packet_id="b" * 64,
        family="adoption_announcement_files",
    )
    anchor = _event(
        "3" * 64,
        "2024-03-01T00:00:00Z",
        packet_id="c" * 64,
        family="agent_trailer_commits",
    )
    late_challenge = _event(
        "4" * 64,
        "2024-04-01T00:00:00Z",
        packet_id="d" * 64,
        family="adoption_announcement_files",
    )
    return {
        "case_id": "9" * 64,
        "canonical_repository_id": "org/repo",
        "canonical_source_url": "https://github.com/org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": "f" * 40,
        "repository_case_sha256": "8" * 64,
        "queues": {
            "adoption_anchor_candidates": [anchor],
            "adoption_challenge_candidates": [
                challenge_1,
                challenge_2,
                late_challenge,
            ],
        },
    }


def _packet(identifier: str, value: str) -> dict:
    return {
        "packet_id": identifier,
        "query_family_id": (
            "agent_trailer_commits"
            if identifier == "c" * 64
            else "adoption_announcement_files"
        ),
        "raw_evidence": [
            {
                "kind": "commit_message",
                "commit_oid": "1" * 40,
                "path": None,
                "line": None,
                "value": value,
                "source_url": "https://example.test/evidence",
            }
        ],
        "outcomes_consulted": False,
        "packet_sha256": identifier,
    }


def _packets() -> dict[str, dict]:
    return {
        "a" * 64: _packet("a" * 64, "early challenge"),
        "b" * 64: _packet("b" * 64, "later challenge"),
        "c" * 64: _packet("c" * 64, "explicit anchor"),
    }


def _case_index() -> dict:
    return {
        "case_index_sha256": "7" * 64,
        "workflow_sha256": "6" * 64,
        "outcomes_consulted": False,
    }


def _decision(
    event_id: str,
    decision: str,
    *,
    role: str = "primary",
    reviewer_id: str | None = None,
    tranche_number: int = 1,
) -> dict:
    reviewer = (
        reviewer_id
        or {
            "primary": "reviewer-primary",
            "secondary": "reviewer-secondary",
            "resolver": "reviewer-resolver",
        }[role]
    )
    return {
        "canonical_repository_id": "org/repo",
        "event_id": event_id,
        "reviewer_id": reviewer,
        "review_role": role,
        "tranche_number": tranche_number,
        "decision": decision,
        "outcomes_consulted": False,
    }


def test_initial_tranche_exposes_only_earliest_unresolved_event():
    manifest = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )

    assert manifest["task_count"] == 1
    task = manifest["tasks"][0]
    assert task["canonical_repository_id"] == "org/repo"
    assert task["event_id"] == "1" * 64
    assert task["candidate_kind"] == "adoption_challenge"
    assert task["candidate_position"] == 1
    assert task["candidate_count"] == 3
    assert task["review_role"] == "primary"
    assert task["outcomes_consulted"] is False
    assert task["evidence"][0]["raw_evidence"][0]["value"] == "early challenge"
    assert task["task_sha256"] == adoption_review_task_sha256(task)
    assert manifest["tranche_sha256"] == adoption_review_tranche_sha256(manifest)
    assert required_evidence_packet_ids([_case()], []) == {"a" * 64}


def test_tranche_does_not_share_mutable_evidence_with_packets():
    packets = _packets()
    repository_case = _case()
    manifest = build_adoption_review_tranche(
        _case_index(),
        [repository_case],
        packets,
        [],
        tranche_number=1,
    )

    manifest["tasks"][0]["evidence"][0]["raw_evidence"][0]["value"] = "changed"
    manifest["tasks"][0]["candidate_event"]["observed_at"] = "changed"

    assert packets["a" * 64]["raw_evidence"][0]["value"] == "early challenge"
    assert (
        repository_case["queues"]["adoption_challenge_candidates"][0][
            "candidate_event"
        ]["observed_at"]
        == "2024-01-01T00:00:00Z"
    )


def test_rejection_advances_and_acceptance_stops_repository():
    rejected = [_decision("1" * 64, "reject")]
    second = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        rejected,
        tranche_number=2,
    )
    assert second["tasks"][0]["event_id"] == "2" * 64
    assert second["tasks"][0]["candidate_position"] == 2
    assert required_evidence_packet_ids([_case()], rejected) == {"b" * 64}

    accepted = [
        *rejected,
        _decision("2" * 64, "accept_observed", tranche_number=2),
    ]
    completed = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        accepted,
        tranche_number=3,
    )
    assert completed["tasks"] == []
    assert completed["completed_repository_count"] == 1


def test_ambiguous_event_routes_to_secondary_then_distinct_resolver():
    ambiguous = [_decision("1" * 64, "ambiguous")]
    secondary = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        ambiguous,
        tranche_number=2,
    )
    assert secondary["tasks"][0]["review_role"] == "secondary"

    two_reviews = [
        *ambiguous,
        _decision("1" * 64, "reject", role="secondary", tranche_number=2),
    ]
    resolution = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        two_reviews,
        tranche_number=3,
    )
    assert resolution["tasks"][0]["review_role"] == "resolver"

    resolved = [
        *two_reviews,
        _decision("1" * 64, "reject", role="resolver", tranche_number=3),
    ]
    advanced = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        resolved,
        tranche_number=4,
    )
    assert advanced["tasks"][0]["event_id"] == "2" * 64
    assert advanced["tasks"][0]["review_role"] == "primary"


def test_insufficient_evidence_advances_but_late_challenges_are_never_reviewed():
    decisions = [
        _decision("1" * 64, "insufficient"),
        _decision("2" * 64, "reject", tranche_number=2),
    ]
    anchor = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        decisions,
        tranche_number=3,
    )

    assert anchor["tasks"][0]["event_id"] == "3" * 64
    assert anchor["tasks"][0]["candidate_kind"] == "explicit_provenance_anchor"
    assert anchor["tasks"][0]["candidate_count"] == 3


def test_tranche_rejects_missing_or_outcome_exposed_provenance():
    with pytest.raises(AdoptionReviewError, match="missing evidence packet"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            {},
            [],
            tranche_number=1,
        )

    exposed = copy.deepcopy(_packets())
    exposed["a" * 64]["outcomes_consulted"] = True
    with pytest.raises(AdoptionReviewError, match="outcome exposed"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            exposed,
            [],
            tranche_number=1,
        )


def test_tranche_rejects_decision_order_and_contract_drift():
    duplicate = [
        _decision("1" * 64, "reject"),
        _decision("1" * 64, "reject"),
    ]
    with pytest.raises(AdoptionReviewError, match="duplicate review role"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            duplicate,
            tranche_number=2,
        )

    exposed = [_decision("1" * 64, "reject")]
    exposed[0]["outcomes_consulted"] = True
    with pytest.raises(AdoptionReviewError, match="outcome exposed"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            exposed,
            tranche_number=2,
        )

    repeated_reviewer = [
        _decision("1" * 64, "ambiguous"),
        _decision(
            "1" * 64,
            "reject",
            role="secondary",
            reviewer_id="reviewer-primary",
        ),
    ]
    with pytest.raises(AdoptionReviewError, match="reviewers must be distinct"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            repeated_reviewer,
            tranche_number=2,
        )

    later_only = [_decision("2" * 64, "accept_observed", tranche_number=2)]
    with pytest.raises(AdoptionReviewError, match="decision progression"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            later_only,
            tranche_number=3,
        )

    skipped_tranche = [
        _decision("1" * 64, "reject", tranche_number=1),
        _decision("2" * 64, "accept_observed", tranche_number=1),
    ]
    with pytest.raises(AdoptionReviewError, match="higher tranche"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            skipped_tranche,
            tranche_number=2,
        )

    prior_from_tranche_two = [
        _decision("1" * 64, "reject", tranche_number=2),
    ]
    with pytest.raises(AdoptionReviewError, match="newer than all prior decisions"):
        build_adoption_review_tranche(
            _case_index(),
            [_case()],
            _packets(),
            prior_from_tranche_two,
            tranche_number=1,
        )


def _write_case_fixture(tmp_path: Path) -> tuple[dict, Path]:
    case = _case()
    case["repository_case_sha256"] = repository_case_sha256(case)
    payload = (
        json.dumps(case, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    case_root = tmp_path / "cases"
    case_path = case_root / "cases/fixture.json"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_bytes(payload)
    index = {
        **_case_index(),
        "packet_index_sha256": "5" * 64,
        "repositories": [
            {
                "case_id": case["case_id"],
                "canonical_repository_id": case["canonical_repository_id"],
                "case_file": "cases/fixture.json",
                "byte_count": len(payload),
                "packet_count": 4,
                "event_count": 4,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    index["case_index_sha256"] = case_index_sha256(index)
    return index, case_root


def _review_protocol(index: dict) -> dict:
    document = {
        "protocol_version": 3,
        "case_index": {
            "case_index_sha256": index["case_index_sha256"],
        },
        "workflow": {
            "workflow_sha256": "6" * 64,
        },
        "packet_index": {
            "packet_index_sha256": "5" * 64,
        },
        "decision_chain": {
            "responses_bind_tranche_id_and_number": True,
            "later_events_require_a_higher_tranche_number": True,
            "peer_roles_require_distinct_reviewers": True,
            "prior_ledger_checksum_required": True,
            "naked_decision_arrays_forbidden": True,
        },
        "review_execution": {
            "local_or_subagent_only": True,
            "paid_batch_api": False,
            "user_openai_api_key": False,
            "semantic_auto_labeling": False,
        },
        "outcomes_consulted": False,
    }
    return {
        **document,
        "protocol_sha256": adoption_review_protocol_sha256(document),
    }


def test_materializer_loads_bound_cases_and_only_retains_active_packets(
    monkeypatch,
    tmp_path: Path,
):
    index, case_root = _write_case_fixture(tmp_path)
    packets = _packets()
    observed = {}

    def fake_stream(discovery, workflow, packet_path):
        observed["inputs"] = (discovery, workflow, packet_path)
        return (
            {"packet_index_sha256": "5" * 64},
            iter([packets["b" * 64], packets["a" * 64], packets["c" * 64]]),
        )

    monkeypatch.setattr(
        adoption_review,
        "validated_packet_stream_from_file",
        fake_stream,
    )
    packet_path = tmp_path / "packets.json"
    manifest = materialize_adoption_review_tranche(
        index,
        _review_protocol(index),
        case_root,
        {"specification_sha256": "4" * 64},
        {"workflow_sha256": "6" * 64},
        packet_path,
        None,
        tranche_number=1,
    )

    assert observed["inputs"][2] == packet_path
    assert manifest["task_count"] == 1
    assert manifest["tasks"][0]["packet_ids"] == ["a" * 64]
    assert manifest["tasks"][0]["evidence"][0]["packet_id"] == "a" * 64


def test_materializer_fails_closed_on_case_or_packet_binding_drift(
    monkeypatch,
    tmp_path: Path,
):
    index, case_root = _write_case_fixture(tmp_path)
    case_path = case_root / index["repositories"][0]["case_file"]
    case_path.write_text("{}")

    with pytest.raises(AdoptionReviewError, match="case byte count"):
        materialize_adoption_review_tranche(
            index,
            _review_protocol(index),
            case_root,
            {},
            {"workflow_sha256": "6" * 64},
            tmp_path / "packets.json",
            None,
            tranche_number=1,
        )

    index, case_root = _write_case_fixture(tmp_path)
    monkeypatch.setattr(
        adoption_review,
        "validated_packet_stream_from_file",
        lambda *_: ({"packet_index_sha256": "0" * 64}, iter(())),
    )
    with pytest.raises(AdoptionReviewError, match="packet index binding"):
        materialize_adoption_review_tranche(
            index,
            _review_protocol(index),
            case_root,
            {},
            {"workflow_sha256": "6" * 64},
            tmp_path / "packets.json",
            None,
            tranche_number=1,
        )


def test_materializer_rejects_self_consistent_case_frame_replacement(tmp_path: Path):
    index, case_root = _write_case_fixture(tmp_path)
    protocol = _review_protocol(index)
    record = index["repositories"][0]
    case_path = case_root / record["case_file"]
    case = json.loads(case_path.read_text())
    case["queues"]["adoption_challenge_candidates"][0]["candidate_event"][
        "observed_at"
    ] = "1999-01-01T00:00:00Z"
    case["repository_case_sha256"] = repository_case_sha256(case)
    payload = (
        json.dumps(case, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    case_path.write_bytes(payload)
    record["byte_count"] = len(payload)
    record["sha256"] = hashlib.sha256(payload).hexdigest()
    index["case_index_sha256"] = case_index_sha256(index)

    with pytest.raises(AdoptionReviewError, match="frozen case index binding"):
        materialize_adoption_review_tranche(
            index,
            protocol,
            case_root,
            {},
            {"workflow_sha256": "6" * 64},
            tmp_path / "packets.json",
            None,
            tranche_number=1,
        )


def test_materializer_rejects_rehashed_decision_ledger_replacement(tmp_path: Path):
    index, case_root = _write_case_fixture(tmp_path)
    tranche = build_adoption_review_tranche(
        index,
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)
    ledger = build_adoption_decision_ledger(tranche, [response], [])
    expected_sha256 = ledger["decision_ledger_sha256"]
    decision = ledger["decisions"][0]
    decision["decision"] = "accept_confirmed"
    decision["default_branch_supported"] = True
    decision["evidence_tier"] = "confirmed"
    decision["response_sha256"] = adoption_review_response_sha256(decision)
    ledger["decision_ledger_sha256"] = adoption_review_ledger_sha256(ledger)

    with pytest.raises(AdoptionReviewError, match="frozen expected checksum"):
        materialize_adoption_review_tranche(
            index,
            _review_protocol(index),
            case_root,
            {},
            {"workflow_sha256": "6" * 64},
            tmp_path / "packets.json",
            ledger,
            expected_decision_ledger_sha256=expected_sha256,
            tranche_number=2,
        )


@pytest.mark.parametrize(
    ("mutation", "message", "recompute"),
    [
        (
            lambda protocol: protocol.update(outcomes_consulted=True),
            "outcome exposed",
            True,
        ),
        (
            lambda protocol: protocol["workflow"].update(workflow_sha256="0" * 64),
            "workflow binding",
            True,
        ),
        (
            lambda protocol: protocol["packet_index"].update(
                packet_index_sha256="0" * 64
            ),
            "packet binding",
            True,
        ),
        (
            lambda protocol: protocol["decision_chain"].update(
                naked_decision_arrays_forbidden=False
            ),
            "decision-chain policy",
            True,
        ),
        (
            lambda protocol: protocol["review_execution"].update(paid_batch_api=True),
            "execution policy",
            True,
        ),
        (
            lambda protocol: protocol.update(protocol_sha256="0" * 64),
            "protocol checksum",
            False,
        ),
    ],
)
def test_materializer_rejects_adoption_protocol_drift(
    tmp_path: Path,
    mutation,
    message: str,
    recompute: bool,
):
    index, case_root = _write_case_fixture(tmp_path)
    protocol = _review_protocol(index)
    mutation(protocol)
    if recompute:
        protocol["protocol_sha256"] = adoption_review_protocol_sha256(protocol)

    with pytest.raises(AdoptionReviewError, match=message):
        materialize_adoption_review_tranche(
            index,
            protocol,
            case_root,
            {},
            {"workflow_sha256": "6" * 64},
            tmp_path / "packets.json",
            None,
            tranche_number=1,
        )


def test_adoption_review_cli_writes_checksumming_manifest(monkeypatch, tmp_path: Path):
    expected = {
        "review_version": 3,
        "tranche_number": 2,
        "task_count": 0,
        "tranche_sha256": "1" * 64,
    }
    observed = {}

    def fake_materialize(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(
        adoption_review_cli,
        "materialize_adoption_review_tranche",
        fake_materialize,
    )
    for name, value in (
        ("case-index.json", {}),
        ("review-protocol.json", {}),
        ("discovery.json", {}),
        ("workflow.json", {}),
        ("decisions.json", {"decisions": []}),
        ("packets.json", {}),
    ):
        (tmp_path / name).write_text(json.dumps(value))
    output = tmp_path / "tranche.json"

    exit_code = adoption_review_cli.main(
        [
            "--case-index",
            str(tmp_path / "case-index.json"),
            "--case-root",
            str(tmp_path),
            "--review-protocol",
            str(tmp_path / "review-protocol.json"),
            "--discovery-specification",
            str(tmp_path / "discovery.json"),
            "--workflow-specification",
            str(tmp_path / "workflow.json"),
            "--packet-index",
            str(tmp_path / "packets.json"),
            "--decisions",
            str(tmp_path / "decisions.json"),
            "--tranche-number",
            "2",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text()) == expected
    assert observed["kwargs"] == {
        "expected_decision_ledger_sha256": None,
        "tranche_number": 2,
    }


def _response(task: dict, tranche: dict, decision: str = "reject") -> dict:
    document = {
        "response_version": 3,
        "tranche_id": tranche["tranche_id"],
        "tranche_number": tranche["tranche_number"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "canonical_repository_id": task["canonical_repository_id"],
        "event_id": task["event_id"],
        "reviewer_id": "reviewer-primary",
        "review_role": task["review_role"],
        "decision": decision,
        "default_branch_supported": decision.startswith("accept_"),
        "evidence_tier": (
            "confirmed"
            if decision == "accept_confirmed"
            else "observed" if decision == "accept_observed" else "not_applicable"
        ),
        "rationale": "The evidence is unrelated to AI coding-agent adoption.",
        "evidence_citations": [task["evidence"][0]["raw_evidence"][0]["source_url"]],
        "outcomes_consulted": False,
    }
    return {
        **document,
        "response_sha256": adoption_review_response_sha256(document),
    }


def test_response_compiler_binds_complete_reviews_to_frozen_tasks():
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)

    decisions = compile_adoption_review_responses(tranche, [response])

    assert decisions == [response]


def test_response_compiler_rejects_missing_or_tampered_reviews():
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    with pytest.raises(AdoptionReviewError, match="exactly cover"):
        compile_adoption_review_responses(tranche, [])

    response = _response(tranche["tasks"][0], tranche)
    response["event_id"] = "0" * 64
    response["response_sha256"] = adoption_review_response_sha256(response)
    with pytest.raises(AdoptionReviewError, match="task binding"):
        compile_adoption_review_responses(tranche, [response])


def test_response_compiler_enforces_acceptance_evidence_tier():
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche, "accept_confirmed")
    response["default_branch_supported"] = False
    response["response_sha256"] = adoption_review_response_sha256(response)

    with pytest.raises(AdoptionReviewError, match="default-branch"):
        compile_adoption_review_responses(tranche, [response])


@pytest.mark.parametrize(
    ("mutation", "message", "recompute"),
    [
        (
            lambda response: response.update(response_sha256="0" * 64),
            "checksum",
            False,
        ),
        (
            lambda response: response.update(outcomes_consulted=True),
            "outcome exposed",
            True,
        ),
        (
            lambda response: response.update(reviewer_id=""),
            "reviewer",
            True,
        ),
        (
            lambda response: response.update(rationale=""),
            "rationale",
            True,
        ),
        (
            lambda response: response.update(evidence_citations=[]),
            "citations",
            True,
        ),
        (
            lambda response: response.update(evidence_tier="observed"),
            "evidence tier",
            True,
        ),
        (
            lambda response: response.update(default_branch_supported="yes"),
            "default-branch field",
            True,
        ),
        (
            lambda response: response.update(tranche_number=99),
            "tranche binding",
            True,
        ),
    ],
)
def test_response_compiler_rejects_contract_drift(
    mutation,
    message: str,
    recompute: bool,
):
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)
    mutation(response)
    if recompute:
        response["response_sha256"] = adoption_review_response_sha256(response)

    with pytest.raises(AdoptionReviewError, match=message):
        compile_adoption_review_responses(tranche, [response])


def test_decision_ledger_is_bound_to_tranche_and_responses():
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)

    ledger = build_adoption_decision_ledger(tranche, [response], [])

    assert ledger["response_count"] == 1
    assert ledger["decision_count"] == 1
    assert ledger["decisions"] == [response]
    assert ledger["decision_ledger_sha256"] == adoption_review_ledger_sha256(ledger)


def _compile_source_args(tmp_path: Path) -> list[str]:
    arguments = []
    for flag, filename in (
        ("--case-index", "compile-case-index.json"),
        ("--review-protocol", "compile-review-protocol.json"),
        ("--discovery-specification", "compile-discovery.json"),
        ("--workflow-specification", "compile-workflow.json"),
        ("--packet-index", "compile-packets.json"),
    ):
        path = tmp_path / filename
        path.write_text("{}")
        arguments.extend([flag, str(path)])
    arguments.extend(["--case-root", str(tmp_path)])
    return arguments


def test_compile_cli_merges_response_bundles_into_ledger(
    monkeypatch,
    tmp_path: Path,
):
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)
    tranche_path = tmp_path / "tranche.json"
    response_path = tmp_path / "responses.json"
    output_path = tmp_path / "ledger.json"
    tranche_path.write_text(json.dumps(tranche))
    response_path.write_text(
        json.dumps(
            {
                "reviewer_id": "reviewer-primary",
                "tranche_id": tranche["tranche_id"],
                "task_start": 0,
                "task_end": 0,
                "responses": [response],
            }
        )
    )
    monkeypatch.setattr(
        adoption_compile_cli,
        "materialize_adoption_review_tranche",
        lambda *_args, **_kwargs: tranche,
    )

    exit_code = adoption_compile_cli.main(
        [
            "--tranche",
            str(tranche_path),
            "--response-bundle",
            str(response_path),
            "--expected-tranche-sha256",
            tranche["tranche_sha256"],
            "--output",
            str(output_path),
            *_compile_source_args(tmp_path),
        ]
    )

    ledger = json.loads(output_path.read_text())
    assert exit_code == 0
    assert ledger["decision_count"] == 1
    assert ledger["decisions"] == [response]


def test_compile_cli_rejects_tampered_prior_ledger(monkeypatch, tmp_path: Path):
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)
    prior = copy.deepcopy(build_adoption_decision_ledger(tranche, [response], []))
    expected_prior_sha256 = prior["decision_ledger_sha256"]
    prior_decision = prior["decisions"][0]
    prior_decision["decision"] = "accept_confirmed"
    prior_decision["default_branch_supported"] = True
    prior_decision["evidence_tier"] = "confirmed"
    prior_decision["rationale"] = "Self-consistently replaced historical decision."
    prior_decision["response_sha256"] = adoption_review_response_sha256(prior_decision)
    prior["decision_ledger_sha256"] = adoption_review_ledger_sha256(prior)
    tranche_path = tmp_path / "tranche.json"
    response_path = tmp_path / "responses.json"
    prior_path = tmp_path / "prior.json"
    tranche_path.write_text(json.dumps(tranche))
    response_path.write_text(
        json.dumps(
            {
                "reviewer_id": "reviewer-primary",
                "tranche_id": tranche["tranche_id"],
                "task_start": 0,
                "task_end": 0,
                "responses": [response],
            }
        )
    )
    prior_path.write_text(json.dumps(prior))
    monkeypatch.setattr(
        adoption_compile_cli,
        "materialize_adoption_review_tranche",
        lambda *_args, **_kwargs: tranche,
    )

    with pytest.raises(AdoptionReviewError, match="frozen expected checksum"):
        adoption_compile_cli.main(
            [
                "--tranche",
                str(tranche_path),
                "--response-bundle",
                str(response_path),
                "--prior-ledger",
                str(prior_path),
                "--expected-prior-ledger-sha256",
                expected_prior_sha256,
                "--expected-tranche-sha256",
                tranche["tranche_sha256"],
                "--output",
                str(tmp_path / "ledger.json"),
                *_compile_source_args(tmp_path),
            ]
        )


def test_compile_cli_rejects_self_consistent_task_evidence_replacement(
    monkeypatch,
    tmp_path: Path,
):
    frozen = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    changed = copy.deepcopy(frozen)
    task = changed["tasks"][0]
    task["evidence"][0]["raw_evidence"][0][
        "source_url"
    ] = "https://example.invalid/replaced-evidence"
    task["task_sha256"] = adoption_review_task_sha256(task)
    changed["tranche_sha256"] = adoption_review_tranche_sha256(changed)
    tranche_path = tmp_path / "changed-tranche.json"
    response_path = tmp_path / "responses.json"
    tranche_path.write_text(json.dumps(changed))
    response_path.write_text(
        json.dumps(
            {
                "reviewer_id": "reviewer-primary",
                "tranche_id": changed["tranche_id"],
                "task_start": 0,
                "task_end": 0,
                "responses": [_response(changed["tasks"][0], changed)],
            }
        )
    )
    monkeypatch.setattr(
        adoption_compile_cli,
        "materialize_adoption_review_tranche",
        lambda *_args, **_kwargs: frozen,
    )

    with pytest.raises(AdoptionReviewError, match="rematerialized evidence"):
        adoption_compile_cli.main(
            [
                "--tranche",
                str(tranche_path),
                "--response-bundle",
                str(response_path),
                "--expected-tranche-sha256",
                frozen["tranche_sha256"],
                "--output",
                str(tmp_path / "ledger.json"),
                *_compile_source_args(tmp_path),
            ]
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda bundle: bundle.update(reviewer_id="wrong-reviewer"),
            "assignment",
        ),
        (
            lambda bundle: bundle.update(task_start=1, task_end=1),
            "task range",
        ),
    ],
)
def test_response_bundle_envelope_is_bound_to_reviewer_and_task_range(
    tmp_path: Path,
    mutation,
    message: str,
):
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    bundle = {
        "reviewer_id": "reviewer-primary",
        "tranche_id": tranche["tranche_id"],
        "task_start": 0,
        "task_end": 0,
        "responses": [_response(tranche["tasks"][0], tranche)],
    }
    mutation(bundle)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle))

    with pytest.raises(AdoptionReviewError, match=message):
        adoption_compile_cli._responses([path], tranche)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda ledger: ledger.update(outcomes_consulted=True),
            "outcome exposed",
        ),
        (
            lambda ledger: ledger.update(case_index_sha256="0" * 64),
            "study binding",
        ),
        (
            lambda ledger: ledger.update(decision_count=2),
            "count",
        ),
        (
            lambda ledger: ledger.update(
                prior_decision_count=999,
                response_count=0,
            ),
            "count relation",
        ),
        (
            lambda ledger: ledger["decisions"][0].update(response_sha256="0" * 64),
            "response checksum",
        ),
    ],
)
def test_prior_ledger_validator_rejects_self_consistent_contract_drift(
    mutation,
    message: str,
):
    tranche = build_adoption_review_tranche(
        _case_index(),
        [_case()],
        _packets(),
        [],
        tranche_number=1,
    )
    response = _response(tranche["tasks"][0], tranche)
    ledger = build_adoption_decision_ledger(tranche, [response], [])
    mutation(ledger)
    ledger["decision_ledger_sha256"] = adoption_review_ledger_sha256(ledger)

    with pytest.raises(AdoptionReviewError, match=message):
        validate_adoption_decision_ledger(
            ledger,
            case_index_sha256_value=tranche["case_index_sha256"],
            workflow_sha256_value=tranche["workflow_sha256"],
        )
