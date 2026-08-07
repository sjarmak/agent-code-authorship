import copy
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_agent_commit_catalog_cli as catalog_cli
from authorship.sourcegraph_adoption_anchor_completion import (
    anchor_completion_ledger_sha256,
)
from authorship.sourcegraph_adoption_audit import (
    audit_response_sha256,
    compiled_audit_sha256,
)
from authorship.sourcegraph_adoption_review import adoption_review_response_sha256
from authorship.sourcegraph_agent_commit_catalog import (
    AgentCommitCatalogError,
    _selected_records,
    agent_commit_catalog_sha256,
    build_agent_commit_catalog,
    validate_agent_commit_catalog,
)

ROOT = Path(__file__).parents[1]
REVIEW_ROOT = ROOT / "results/sourcegraph-adoption-review-v3"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _artifacts() -> tuple[dict, list[dict], dict, dict, dict, dict]:
    sequential = _load(REVIEW_ROOT / "decision-ledger-007.json")
    tranches = [
        _load(REVIEW_ROOT / f"tranche-{number:03d}.json") for number in range(1, 8)
    ]
    manifest = _load(REVIEW_ROOT / "anchor-completion-008.json")
    anchor = _load(REVIEW_ROOT / "anchor-decision-ledger-008.json")
    languages = _load(ROOT / "study/sourcegraph-repository-languages.v3.json")
    audit = _load(REVIEW_ROOT / "audit-compiled-008.json")
    return sequential, tranches, manifest, anchor, languages, audit


def test_catalog_freezes_only_confirmed_explicit_commit_events():
    catalog = build_agent_commit_catalog(*_artifacts())

    assert catalog["reviewed_event_count"] == 1224
    assert catalog["commit_count"] == 155
    assert catalog["repository_count"] == 155
    assert catalog["primary_scope_commit_count"] == 155
    assert catalog["source_counts"] == {
        "anchor_completion": 63,
        "sequential": 92,
    }
    assert catalog["exclusion_counts"] == {
        "decision_not_accept_confirmed": 1069,
        "confirmed_missing_commit_oid": 0,
        "confirmed_non_explicit_provenance": 0,
    }
    assert catalog["excluded_event_count"] == 1069
    assert all(row["decision"] == "accept_confirmed" for row in catalog["commits"])
    assert all(
        row["candidate_kind"] == "explicit_provenance_anchor"
        for row in catalog["commits"]
    )
    assert all(len(row["commit_oid"]) == 40 for row in catalog["commits"])
    assert (
        len({(row["repository_id"], row["commit_oid"]) for row in catalog["commits"]})
        == 155
    )


def test_catalog_binds_review_evidence_adoption_context_and_audit():
    catalog = build_agent_commit_catalog(*_artifacts())
    sampled = [row for row in catalog["commits"] if row["reliability_audit"]["sampled"]]

    assert catalog["audit_sampled_commit_count"] == 46
    assert catalog["audit_exact_agreement_count"] == 44
    assert catalog["audit_disagreement_count"] == 2
    assert len(sampled) == 46
    assert all(
        row["task_sha256"] and row["response_sha256"] for row in catalog["commits"]
    )
    assert all(row["cited_evidence"] for row in catalog["commits"])
    assert all(
        evidence["source_url"] in row["evidence_citations"]
        for row in catalog["commits"]
        for evidence in row["cited_evidence"]
    )
    assert all(
        row["adoption_context"]["adoption_decision"]
        in {"accept_confirmed", "accept_observed"}
        for row in catalog["commits"]
    )
    assert all(row["primary_scope_eligible"] for row in catalog["commits"])
    assert {row["reliability_audit"]["exact_agreement"] for row in sampled} == {
        False,
        True,
    }


def test_catalog_is_schema_valid_and_self_checking():
    catalog = build_agent_commit_catalog(*_artifacts())

    validate_agent_commit_catalog(catalog)
    assert catalog == _load(ROOT / "study/sourcegraph-agent-commit-catalog.v3.json")
    assert catalog["catalog_sha256"] == agent_commit_catalog_sha256(catalog)
    jsonschema.validate(
        catalog,
        _load(ROOT / "study/sourcegraph-agent-commit-catalog.schema.json"),
    )
    assert catalog["outcomes_consulted"] is False
    assert (
        catalog["selection_rule"]["label_interpretation"]
        == "reviewed_positive_commit_only_not_post_adoption_inference"
    )


def test_catalog_validator_rejects_duplicate_repository_commit():
    catalog = build_agent_commit_catalog(*_artifacts())
    duplicated = copy.deepcopy(catalog)
    duplicated["commits"].append(copy.deepcopy(duplicated["commits"][0]))

    with pytest.raises(AgentCommitCatalogError, match="duplicate repository commit"):
        validate_agent_commit_catalog(duplicated)


def test_selection_reports_each_exclusion_reason():
    commit = "a" * 40
    records = [
        (
            {
                "candidate_kind": "explicit_provenance_anchor",
                "candidate_event": {"commit_oid": commit},
            },
            {"decision": "reject"},
            "sequential",
        ),
        (
            {
                "candidate_kind": "adoption_challenge",
                "candidate_event": {"commit_oid": commit},
            },
            {"decision": "accept_confirmed"},
            "sequential",
        ),
        (
            {
                "candidate_kind": "explicit_provenance_anchor",
                "candidate_event": {"commit_oid": None},
            },
            {"decision": "accept_confirmed"},
            "anchor_completion",
        ),
    ]

    selected, exclusions = _selected_records(records)

    assert selected == []
    assert exclusions == {
        "decision_not_accept_confirmed": 1,
        "confirmed_missing_commit_oid": 1,
        "confirmed_non_explicit_provenance": 1,
    }


def test_catalog_rejects_outcome_exposed_anchor_decision():
    artifacts = list(_artifacts())
    anchor = copy.deepcopy(artifacts[3])
    decision = next(
        item for item in anchor["decisions"] if item["decision"] == "accept_confirmed"
    )
    decision["outcomes_consulted"] = True
    decision["response_sha256"] = adoption_review_response_sha256(decision)
    anchor["anchor_completion_ledger_sha256"] = anchor_completion_ledger_sha256(anchor)
    artifacts[3] = anchor

    with pytest.raises(AgentCommitCatalogError, match="outcome exposed"):
        build_agent_commit_catalog(*artifacts)


def test_catalog_rejects_outcome_exposed_audit_response():
    artifacts = list(_artifacts())
    audit = copy.deepcopy(artifacts[5])
    response = audit["responses"][0]
    response["outcomes_consulted"] = True
    response["response_sha256"] = audit_response_sha256(response)
    audit["compiled_audit_sha256"] = compiled_audit_sha256(audit)
    artifacts[5] = audit

    with pytest.raises(AgentCommitCatalogError, match="outcome exposed"):
        build_agent_commit_catalog(*artifacts)


def _cli_arguments(tmp_path: Path) -> list[str]:
    sequential, tranches, manifest, anchor, languages, audit = _artifacts()
    arguments = [
        "--sequential-ledger",
        str(REVIEW_ROOT / "decision-ledger-007.json"),
        "--expected-sequential-ledger-sha256",
        sequential["decision_ledger_sha256"],
    ]
    for number, tranche in enumerate(tranches, start=1):
        arguments.extend(
            [
                "--tranche",
                str(REVIEW_ROOT / f"tranche-{number:03d}.json"),
                "--expected-tranche-sha256",
                tranche["tranche_sha256"],
            ]
        )
    return [
        *arguments,
        "--anchor-manifest",
        str(REVIEW_ROOT / "anchor-completion-008.json"),
        "--expected-anchor-manifest-sha256",
        manifest["manifest_sha256"],
        "--anchor-ledger",
        str(REVIEW_ROOT / "anchor-decision-ledger-008.json"),
        "--expected-anchor-ledger-sha256",
        anchor["anchor_completion_ledger_sha256"],
        "--language-inventory",
        str(ROOT / "study/sourcegraph-repository-languages.v3.json"),
        "--expected-language-inventory-sha256",
        languages["inventory_sha256"],
        "--compiled-audit",
        str(REVIEW_ROOT / "audit-compiled-008.json"),
        "--expected-compiled-audit-sha256",
        audit["compiled_audit_sha256"],
        "--output",
        str(tmp_path / "agent-commits.json"),
    ]


def test_catalog_cli_requires_independently_frozen_predecessor_hashes(tmp_path):
    arguments = _cli_arguments(tmp_path)

    assert catalog_cli.main(arguments) == 0
    assert _load(tmp_path / "agent-commits.json")["commit_count"] == 155

    drifted_path = tmp_path / "drifted-anchor-ledger.json"
    drifted = copy.deepcopy(_artifacts()[3])
    drifted["source_decision_ledger_sha256"] = "0" * 64
    drifted["anchor_completion_ledger_sha256"] = anchor_completion_ledger_sha256(
        drifted
    )
    drifted_path.write_text(json.dumps(drifted))
    arguments[arguments.index("--anchor-ledger") + 1] = str(drifted_path)

    with pytest.raises(AgentCommitCatalogError, match="independently frozen"):
        catalog_cli.main(arguments)
