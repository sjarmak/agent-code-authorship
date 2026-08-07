import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_adoption_audit import (
    AdoptionAuditError,
    audit_key_sha256,
    audit_response_sha256,
    audit_worksheet_sha256,
    build_adoption_reliability_audit,
    compile_adoption_reliability_responses,
)
from authorship.sourcegraph_adoption_review import (
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
)
from authorship.sourcegraph_adoption_review_contracts import (
    adoption_review_ledger_sha256,
    adoption_review_response_sha256,
)
from authorship.sourcegraph_repository_languages import (
    repository_language_inventory_sha256,
)

SHA = "a" * 64


def _raw_evidence(index: int, repository: str) -> dict:
    return {
        "kind": "commit",
        "commit_oid": f"{index + 1:040x}",
        "path": "README.md",
        "line": index + 1,
        "value": f"real repository event {index}",
        "source_url": f"https://sourcegraph.example/{repository}/{index}",
    }


def _task(index: int, *, repository: str, channel: str) -> dict:
    raw_evidence = _raw_evidence(index, repository)
    document = {
        "review_version": 3,
        "task_id": f"{index + 1000:064x}",
        "case_index_sha256": "b" * 64,
        "workflow_sha256": "c" * 64,
        "case_id": f"{index + 2000:064x}",
        "repository_case_sha256": f"{index + 3000:064x}",
        "canonical_repository_id": repository,
        "canonical_source_url": f"https://github.com/{repository}",
        "sourcegraph_name": f"github.com/sg-evals/{repository.replace('/', '-')}",
        "cutoff_commit": "d" * 40,
        "event_id": f"{index + 4000:064x}",
        "candidate_event": {
            "commit_oid": raw_evidence["commit_oid"],
            "observed_at": "2025-01-01T00:00:00Z",
        },
        "candidate_kind": "adoption_challenge",
        "candidate_position": 1,
        "candidate_count": 2,
        "query_family_ids": channel.split("+"),
        "packet_ids": [f"{index + 5000:064x}"],
        "evidence": [
            {
                "packet_id": f"{index + 5000:064x}",
                "packet_sha256": f"{index + 6000:064x}",
                "query_family_id": channel.split("+")[0],
                "raw_evidence": [raw_evidence],
            }
        ],
        "review_role": "primary",
        "review_question": "Does this event establish adoption?",
        "allowed_decisions": [
            "accept_confirmed",
            "accept_observed",
            "ambiguous",
            "insufficient",
            "reject",
        ],
        "outcomes_consulted": False,
    }
    return {**document, "task_sha256": adoption_review_task_sha256(document)}


def _decision(task: dict, tranche: dict, value: str, reviewer: str) -> dict:
    tier = {
        "accept_confirmed": "confirmed",
        "accept_observed": "observed",
    }.get(value, "not_applicable")
    document = {
        "response_version": 3,
        "tranche_id": tranche["tranche_id"],
        "tranche_number": tranche["tranche_number"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "canonical_repository_id": task["canonical_repository_id"],
        "event_id": task["event_id"],
        "reviewer_id": reviewer,
        "review_role": task["review_role"],
        "decision": value,
        "default_branch_supported": value.startswith("accept_"),
        "evidence_tier": tier,
        "rationale": f"primary rationale for {task['event_id']}",
        "evidence_citations": [task["evidence"][0]["raw_evidence"][0]["source_url"]],
        "outcomes_consulted": False,
    }
    return {
        **document,
        "response_sha256": adoption_review_response_sha256(document),
    }


def _tranche(tasks: list[dict], rows: list[tuple[str, str, str, str]]) -> dict:
    tranche_document = {
        "review_version": 3,
        "tranche_id": "e" * 64,
        "tranche_number": 1,
        "case_index_sha256": "b" * 64,
        "workflow_sha256": "c" * 64,
        "prior_decision_ledger_sha256": None,
        "eligible_repository_count": len({row[0] for row in rows}),
        "completed_repository_count": 0,
        "exhausted_repository_count": 0,
        "active_repository_count": len({row[0] for row in rows}),
        "repository_count": len({row[0] for row in rows}),
        "task_count": len(tasks),
        "tasks": tasks,
        "outcomes_consulted": False,
    }
    return {
        **tranche_document,
        "tranche_sha256": adoption_review_tranche_sha256(tranche_document),
    }


def _ledger(tasks: list[dict], tranche: dict, rows: list) -> dict:
    decisions = [
        _decision(task, tranche, row[3], f"primary-{index}")
        for index, (task, row) in enumerate(zip(tasks, rows, strict=True))
    ]
    ledger_document = {
        "decision_ledger_version": 3,
        "tranche_id": tranche["tranche_id"],
        "tranche_sha256": tranche["tranche_sha256"],
        "case_index_sha256": "b" * 64,
        "workflow_sha256": "c" * 64,
        "prior_decision_ledger_sha256": None,
        "prior_decision_count": 0,
        "response_count": len(decisions),
        "decision_count": len(decisions),
        "decisions": decisions,
        "outcomes_consulted": False,
    }
    return {
        **ledger_document,
        "decision_ledger_sha256": adoption_review_ledger_sha256(ledger_document),
    }


def _inventory(tasks: list[dict], rows: list, tranche: dict) -> dict:
    repositories = [
        {
            "canonical_repository_id": repository,
            "review_sourcegraph_name": task["sourcegraph_name"],
            "language_sourcegraph_name": (
                f"github.com/sg-evals/{repository.replace('/', '-')}"
            ),
            "language": language,
        }
        for task, (repository, _channel, language, _value) in zip(
            tasks, rows, strict=True
        )
    ]
    repositories = list(
        {row["canonical_repository_id"]: row for row in repositories}.values()
    )
    counts = Counter(row["language"] for row in repositories)
    language_name_map = {
        row["canonical_repository_id"]: row["language_sourcegraph_name"]
        for row in repositories
    }
    name_map_sha256 = hashlib.sha256(
        json.dumps(language_name_map, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inventory_document = {
        "$schema": "sourcegraph-repository-language-inventory.schema.json",
        "inventory_version": 3,
        "case_index_sha256": "b" * 64,
        "source_tranche_sha256": tranche["tranche_sha256"],
        "observed_at": "2026-07-29T00:00:00Z",
        "sourcegraph_capability": "Repository.language",
        "sourcegraph_scope": "github.com/sg-evals/*",
        "scip_required": False,
        "index_manifest_sha256": "d" * 64,
        "repository_name_map_sha256": name_map_sha256,
        "repository_count": len(repositories),
        "review_scope_exception_count": 0,
        "language_counts": dict(sorted(counts.items())),
        "repositories": repositories,
        "outcomes_consulted": False,
    }
    return {
        **inventory_document,
        "inventory_sha256": repository_language_inventory_sha256(inventory_document),
    }


def _inputs(rows: list[tuple[str, str, str, str]]) -> tuple[dict, list, dict]:
    tasks = [
        _task(index, repository=repository, channel=channel)
        for index, (repository, channel, _language, _decision_value) in enumerate(rows)
    ]
    tranche = _tranche(tasks, rows)
    ledger = _ledger(tasks, tranche, rows)
    inventory = _inventory(tasks, rows, tranche)
    return ledger, [tranche], inventory


def _audit_response(task: dict, worksheet: dict, *, reviewer: str, decision: str):
    tier = {
        "accept_confirmed": "confirmed",
        "accept_observed": "observed",
    }.get(decision, "not_applicable")
    document = {
        "response_version": 1,
        "worksheet_sha256": worksheet["worksheet_sha256"],
        "audit_task_id": task["audit_task_id"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "canonical_repository_id": task["canonical_repository_id"],
        "event_id": task["event_id"],
        "reviewer_id": reviewer,
        "decision": decision,
        "default_branch_supported": decision.startswith("accept_"),
        "evidence_tier": tier,
        "rationale": "independent audit rationale",
        "evidence_citations": [task["evidence"][0]["raw_evidence"][0]["source_url"]],
        "outcomes_consulted": False,
    }
    return {**document, "response_sha256": audit_response_sha256(document)}


def _schema(name: str) -> dict:
    return json.loads((Path(__file__).parents[1] / "study" / name).read_text())


def _compile(
    worksheet: dict,
    key: dict,
    responses: list,
    ledger: dict,
    tranches: list,
    inventory: dict,
):
    return compile_adoption_reliability_responses(
        worksheet,
        key,
        responses,
        ledger,
        tranches,
        inventory,
        seed="seed",
        expected_worksheet_sha256=worksheet["worksheet_sha256"],
        expected_key_sha256=key["key_sha256"],
        expected_auditor_ids={
            response["audit_task_id"]: response["reviewer_id"] for response in responses
        },
    )


def _tamper_task(_ledger: dict, tranches: list, _inventory: dict) -> None:
    tranche = tranches[0]
    tranche["tasks"][0]["event_id"] = SHA
    tranche["tranche_sha256"] = adoption_review_tranche_sha256(tranche)


def _remove_task(_ledger: dict, tranches: list, _inventory: dict) -> None:
    tranche = tranches[0]
    tranche["tasks"].pop()
    tranche["task_count"] = 0
    tranche["tranche_sha256"] = adoption_review_tranche_sha256(tranche)


def test_builds_deterministic_blind_stratified_artifacts():
    rows = [(f"org/repo-{index}", "zeta+alpha", "Go", "reject") for index in range(31)]
    rows += [
        (f"org/python-{index}", "commit-message", "Python", "accept_confirmed")
        for index in range(4)
    ]
    ledger, tranches, inventory = _inputs(rows)

    first = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="frozen-audit-seed"
    )
    second = build_adoption_reliability_audit(
        ledger, list(reversed(tranches)), inventory, seed="frozen-audit-seed"
    )

    assert first == second
    worksheet, key = first
    assert worksheet["outcomes_consulted"] is False
    assert worksheet["scip_required"] is False
    assert worksheet["model_or_api_calls"] is False
    assert worksheet["sampling_policy"] == {
        "fraction": "0.10",
        "rounding": "ceiling",
        "minimum_per_joint_stratum": 1,
        "maximum_per_joint_stratum": 3,
        "repository_event_limit": 1,
        "repository_repeat_exception": "joint_stratum_target_would_be_unmet",
        "evidence_channel_collapse": (
            "sorted unique query_family_ids joined with '+'; no semantic grouping"
        ),
    }
    assert worksheet["sample_count"] == 4
    assert worksheet["repository_repeat_exception_count"] == 0
    assert sorted(item["target_sample_count"] for item in key["strata"]) == [1, 3]
    assert {item["evidence_channel"] for item in key["strata"]} == {
        "alpha+zeta",
        "commit-message",
    }
    assert all("decision" not in task for task in worksheet["tasks"])
    assert all("reviewer_id" not in task for task in worksheet["tasks"])
    assert all("rationale" not in task for task in worksheet["tasks"])
    assert all(task["evidence"] for task in worksheet["tasks"])
    assert all("primary_decision" in item for item in key["items"])
    assert key["worksheet_sha256"] == worksheet["worksheet_sha256"]
    assert key["predecessors"] == worksheet["predecessors"]
    jsonschema.validate(
        worksheet, _schema("sourcegraph-adoption-audit-worksheet.schema.json")
    )
    jsonschema.validate(key, _schema("sourcegraph-adoption-audit-key.schema.json"))


def test_builder_does_not_share_mutable_evidence_with_inputs():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, _key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )

    worksheet["tasks"][0]["evidence"][0]["raw_evidence"][0]["value"] = "changed"

    original = tranches[0]["tasks"][0]["evidence"][0]["raw_evidence"][0]
    assert original["value"].startswith("real")


def test_worksheet_and_key_do_not_share_mutable_predecessors():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )

    worksheet["predecessors"]["tranche_sha256s"][0] = "changed"

    assert key["predecessors"]["tranche_sha256s"][0] != "changed"


def test_repository_diversity_exception_is_explicit():
    rows = [
        ("org/shared", "channel-a", "Go", "reject"),
        ("org/shared", "channel-b", "Go", "reject"),
    ]
    ledger, tranches, inventory = _inputs(rows)

    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )

    assert worksheet["sample_count"] == 2
    assert worksheet["repository_repeat_exceptions"] == [
        {
            "canonical_repository_id": "org/shared",
            "stratum_id": key["items"][1]["stratum_id"],
            "reason": "joint_stratum_target_would_be_unmet",
        }
    ]


def test_same_stratum_repository_repeats_are_reported():
    rows = [("org/shared", "channel", "Go", "reject") for _index in range(11)]
    ledger, tranches, inventory = _inputs(rows)

    worksheet, _key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )

    assert worksheet["sample_count"] == 2
    assert worksheet["repository_repeat_exception_count"] == 1
    assert (
        worksheet["repository_repeat_exceptions"][0]["canonical_repository_id"]
        == "org/shared"
    )


def test_prior_stratum_repeats_preserve_within_stratum_diversity():
    rows = [
        ("org/beta", "small-beta", "Go", "reject"),
        ("org/gamma", "small-gamma", "Go", "reject"),
        *[("org/alpha", "large", "Go", "reject") for _index in range(19)],
        ("org/beta", "large", "Go", "reject"),
        ("org/gamma", "large", "Go", "reject"),
    ]
    ledger, tranches, inventory = _inputs(rows)

    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )

    large_ids = {
        item["audit_task_id"]
        for item in key["items"]
        if item["evidence_channel"] == "large"
    }
    repositories = {
        task["canonical_repository_id"]
        for task in worksheet["tasks"]
        if task["audit_task_id"] in large_ids
    }
    assert repositories == {"org/alpha", "org/beta", "org/gamma"}
    assert worksheet["repository_repeat_exception_count"] == 2


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda ledger, _tranches, _inventory: ledger.update(
                decision_ledger_sha256=SHA
            ),
            "ledger checksum",
        ),
        (
            _tamper_task,
            "task checksum",
        ),
        (
            _remove_task,
            "exactly cover",
        ),
        (
            lambda _ledger, _tranches, inventory: inventory.update(
                inventory_sha256=SHA
            ),
            "inventory checksum",
        ),
        (
            lambda _ledger, _tranches, inventory: inventory["repositories"][0].update(
                review_sourcegraph_name="github.com/sg-evals/wrong"
            ),
            "inventory checksum",
        ),
    ],
)
def test_builder_fails_closed_on_predecessor_drift(mutator, message):
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    mutator(ledger, tranches, inventory)

    with pytest.raises(AdoptionAuditError, match=message):
        build_adoption_reliability_audit(
            ledger, tranches, inventory, seed="frozen-audit-seed"
        )


def test_builder_rejects_self_consistent_identity_rewrites():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    inventory["repositories"][0][
        "review_sourcegraph_name"
    ] = "github.com/sg-evals/wrong"
    inventory["inventory_sha256"] = repository_language_inventory_sha256(inventory)

    with pytest.raises(AdoptionAuditError, match="identity binding"):
        build_adoption_reliability_audit(ledger, tranches, inventory, seed="seed")


def test_builder_rejects_self_consistent_task_decision_rewrites():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    decision = ledger["decisions"][0]
    decision["event_id"] = SHA
    decision["response_sha256"] = adoption_review_response_sha256(decision)
    ledger["decision_ledger_sha256"] = adoption_review_ledger_sha256(ledger)

    with pytest.raises(AdoptionAuditError, match="task and decision binding"):
        build_adoption_reliability_audit(ledger, tranches, inventory, seed="seed")


def test_compiler_emits_overall_and_by_stratum_metrics():
    rows = [
        ("org/one", "channel-a", "Go", "reject"),
        ("org/two", "channel-b", "Python", "accept_observed"),
    ]
    ledger, tranches, inventory = _inputs(rows)
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )
    responses = [
        _audit_response(
            worksheet["tasks"][0],
            worksheet,
            reviewer="auditor-a",
            decision=key["items"][0]["primary_decision"],
        ),
        _audit_response(
            worksheet["tasks"][1],
            worksheet,
            reviewer="auditor-b",
            decision="unresolved",
        ),
    ]

    compiled = _compile(worksheet, key, responses, ledger, tranches, inventory)

    assert compiled["overall"] == {
        "sample_count": 2,
        "exact_agreement_count": 1,
        "exact_agreement_rate": 0.5,
        "unresolved_count": 1,
        "unresolved_rate": 0.5,
    }
    assert len(compiled["by_stratum"]) == 2
    assert {item["sample_count"] for item in compiled["by_stratum"]} == {1}
    assert compiled["predecessors"]["worksheet_sha256"] == worksheet["worksheet_sha256"]
    assert compiled["predecessors"]["key_sha256"] == key["key_sha256"]
    assert compiled["outcomes_consulted"] is False
    responses[0]["rationale"] = "changed after compilation"
    assert compiled["responses"][0]["rationale"] == "independent audit rationale"
    jsonschema.validate(
        responses[0], _schema("sourcegraph-adoption-audit-response.schema.json")
    )
    jsonschema.validate(
        compiled, _schema("sourcegraph-adoption-audit-compiled.schema.json")
    )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda responses, _key: responses.pop(), "exactly cover"),
        (
            lambda responses, key: responses[0].update(
                reviewer_id=key["items"][0]["primary_reviewer_id"]
            ),
            "distinct from primary",
        ),
        (
            lambda responses, _key: responses[0].update(
                evidence_citations=["https://sourcegraph.example/not-this-task"]
            ),
            "task-local",
        ),
        (
            lambda responses, _key: responses[0].update(decision="invented"),
            "decision",
        ),
        (
            lambda responses, _key: responses[0].update(
                decision="accept_confirmed",
                evidence_tier="observed",
                default_branch_supported=True,
            ),
            "tier",
        ),
        (
            lambda responses, _key: responses[0].update(
                decision="accept_confirmed",
                evidence_tier="confirmed",
                default_branch_supported=False,
            ),
            "default-branch",
        ),
    ],
)
def test_compiler_rejects_invalid_audit_responses(mutator, message):
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )
    responses = [
        _audit_response(
            worksheet["tasks"][0],
            worksheet,
            reviewer="fresh-auditor",
            decision="reject",
        )
    ]
    mutator(responses, key)

    with pytest.raises(AdoptionAuditError, match=message):
        _compile(worksheet, key, responses, ledger, tranches, inventory)


def test_compiler_rejects_tampered_artifacts_and_response_checksums():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )
    response = _audit_response(
        worksheet["tasks"][0],
        worksheet,
        reviewer="fresh-auditor",
        decision="reject",
    )

    for changed_worksheet, changed_key, changed_response, message in [
        ({**worksheet, "seed": "changed"}, key, response, "worksheet checksum"),
        (worksheet, {**key, "seed": "changed"}, response, "key checksum"),
        (worksheet, key, {**response, "rationale": "changed"}, "response checksum"),
    ]:
        with pytest.raises(AdoptionAuditError, match=message):
            _compile(
                changed_worksheet,
                changed_key,
                [changed_response],
                ledger,
                tranches,
                inventory,
            )


def test_compiler_rejects_self_consistent_contract_and_stratum_rewrites():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )
    response = _audit_response(
        worksheet["tasks"][0],
        worksheet,
        reviewer="fresh-auditor",
        decision="reject",
    )
    response["extra"] = "not in schema"
    response["response_sha256"] = audit_response_sha256(response)
    with pytest.raises(AdoptionAuditError, match="response contract"):
        _compile(worksheet, key, [response], ledger, tranches, inventory)

    del response["extra"]
    response["response_sha256"] = audit_response_sha256(response)
    key["items"][0]["language"] = "Wrong"
    key["key_sha256"] = audit_key_sha256(key)
    with pytest.raises(AdoptionAuditError, match="stratum binding"):
        _compile(worksheet, key, [response], ledger, tranches, inventory)


def _hardened_compile(
    worksheet: dict,
    key: dict,
    response: dict,
    ledger: dict,
    tranches: list,
    inventory: dict,
):
    return compile_adoption_reliability_responses(
        worksheet,
        key,
        [response],
        ledger,
        tranches,
        inventory,
        seed="seed",
        expected_worksheet_sha256=worksheet["worksheet_sha256"],
        expected_key_sha256=key["key_sha256"],
        expected_auditor_ids={response["audit_task_id"]: "fresh-auditor"},
    )


def _single_audit():
    ledger, tranches, inventory = _inputs([("org/repo", "channel", "Go", "reject")])
    worksheet, key = build_adoption_reliability_audit(
        ledger, tranches, inventory, seed="seed"
    )
    response = _audit_response(
        worksheet["tasks"][0],
        worksheet,
        reviewer="fresh-auditor",
        decision="reject",
    )
    return ledger, tranches, inventory, worksheet, key, response


@pytest.mark.parametrize("attack", ["evidence", "predecessor", "primary_key"])
def test_compiler_rematerializes_and_rejects_self_consistent_forgery(attack):
    ledger, tranches, inventory, worksheet, key, response = _single_audit()
    forged_worksheet, forged_key = deepcopy(worksheet), deepcopy(key)
    if attack == "evidence":
        forged_worksheet["tasks"][0]["evidence"][0]["raw_evidence"][0][
            "value"
        ] = "forged evidence"
    elif attack == "predecessor":
        forged_worksheet["predecessors"]["decision_ledger_sha256"] = SHA
    else:
        forged_key["items"][0].update(
            primary_reviewer_id="forged-primary",
            primary_rationale="forged rationale",
            primary_evidence_citations=["https://sourcegraph.example/forged"],
        )
    if attack != "primary_key":
        forged_worksheet["worksheet_sha256"] = audit_worksheet_sha256(forged_worksheet)
        forged_key["worksheet_sha256"] = forged_worksheet["worksheet_sha256"]
        forged_key["predecessors"] = forged_worksheet["predecessors"]
    forged_key["key_sha256"] = audit_key_sha256(forged_key)

    with pytest.raises(AdoptionAuditError, match="rematerialized"):
        _hardened_compile(
            forged_worksheet, forged_key, response, ledger, tranches, inventory
        )


def test_compiler_rejects_self_consistent_auditor_identity_forgery():
    ledger, tranches, inventory, worksheet, key, response = _single_audit()
    response["reviewer_id"] = "forged-auditor"
    response["response_sha256"] = audit_response_sha256(response)

    with pytest.raises(AdoptionAuditError, match="auditor identity"):
        _hardened_compile(worksheet, key, response, ledger, tranches, inventory)


def test_compiler_rejects_self_consistent_source_ledger_forgery():
    ledger, tranches, inventory, worksheet, key, response = _single_audit()
    forged_ledger = deepcopy(ledger)
    decision = forged_ledger["decisions"][0]
    decision["reviewer_id"] = "forged-primary"
    decision["rationale"] = "forged primary rationale"
    decision["evidence_citations"] = ["https://sourcegraph.example/forged"]
    decision["response_sha256"] = adoption_review_response_sha256(decision)
    forged_ledger["decision_ledger_sha256"] = adoption_review_ledger_sha256(
        forged_ledger
    )

    with pytest.raises(AdoptionAuditError, match="rematerialized"):
        _hardened_compile(worksheet, key, response, forged_ledger, tranches, inventory)


@pytest.mark.parametrize("pin", ["worksheet", "key"])
def test_compiler_rejects_incorrect_independent_artifact_pins(pin):
    ledger, tranches, inventory, worksheet, key, response = _single_audit()
    arguments = {
        "seed": "seed",
        "expected_worksheet_sha256": worksheet["worksheet_sha256"],
        "expected_key_sha256": key["key_sha256"],
        "expected_auditor_ids": {response["audit_task_id"]: "fresh-auditor"},
    }
    arguments[f"expected_{pin}_sha256"] = SHA

    with pytest.raises(AdoptionAuditError, match="independently expected"):
        compile_adoption_reliability_responses(
            worksheet,
            key,
            [response],
            ledger,
            tranches,
            inventory,
            **arguments,
        )
