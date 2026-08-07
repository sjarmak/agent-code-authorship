import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_ai_ban_catalog_cli as catalog_cli
from authorship.sourcegraph_ai_ban_catalog import (
    AiBanCatalogError,
    catalog_input_paths,
    catalog_sha256,
    materialize_ai_ban_catalog,
)
from authorship.sourcegraph_ai_ban_review_contracts import (
    ai_ban_target_manifest_sha256,
)
from authorship.sourcegraph_repository_languages import (
    repository_language_inventory_sha256,
)

ROOT = Path(__file__).parents[1]


def _write_json(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _language_inventory(tmp_path: Path) -> Path:
    target = json.loads(
        (ROOT / "study/sourcegraph-ai-ban-target-manifest.v3.json").read_text()
    )
    languages = ("Python", "Rust", "Go", "JavaScript")
    repositories = [
        {
            "canonical_repository_id": record["canonical_repository_id"],
            "review_sourcegraph_name": record["sourcegraph_name"],
            "language_sourcegraph_name": record["sourcegraph_name"],
            "language": languages[index % len(languages)],
        }
        for index, record in enumerate(target["repositories"])
    ]
    document = {
        "inventory_version": 3,
        "repository_count": len(repositories),
        "repositories": repositories,
        "outcomes_consulted": False,
        "scip_required": False,
    }
    document["inventory_sha256"] = repository_language_inventory_sha256(document)
    path = tmp_path / "ai-ban-languages.json"
    _write_json(path, document)
    return path


def _paths_and_pins(tmp_path: Path):
    language_path = _language_inventory(tmp_path)
    paths = catalog_input_paths(ROOT, language_inventory_path=language_path)
    pins = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }
    return paths, pins


def test_catalog_rematerializes_all_lineage_and_exact_blind_audit(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)

    catalog = materialize_ai_ban_catalog(paths, pins)

    assert catalog["repository_count"] == 17
    assert catalog["status_counts"] == {
        "dated_policy": 12,
        "no_admissible_policy": 2,
        "no_frozen_candidate": 3,
    }
    assert catalog["review"]["reviewed_event_count"] == 25
    assert catalog["audit"]["audited_event_count"] == 25
    assert catalog["audit"]["exact_agreement_count"] == 25
    assert (
        sum(stratum["reviewed_event_count"] for stratum in catalog["audit"]["strata"])
        == 25
    )
    assert all(
        stratum["exact_agreement_count"] == stratum["audited_event_count"]
        for stratum in catalog["audit"]["strata"]
    )
    assert len(catalog["predecessors"]["files"]) == len(paths) == 39
    assert catalog["terminal"]["task_count"] == 0
    assert catalog["outcomes_consulted"] is False
    assert catalog["scip_required"] is False
    assert catalog["paid_api_used"] is False
    assert catalog["openai_api_key_used"] is False
    assert catalog["catalog_sha256"] == catalog_sha256(catalog)

    schema = json.loads(
        (ROOT / "study/sourcegraph-ai-ban-catalog.schema.json").read_text()
    )
    jsonschema.validate(catalog, schema)


def test_catalog_records_preserve_status_specific_evidence(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)

    catalog = materialize_ai_ban_catalog(paths, pins)
    by_status = {
        status: [
            record for record in catalog["repositories"] if record["status"] == status
        ]
        for status in catalog["status_counts"]
    }

    assert all(record["scope_eligible"] for record in by_status["dated_policy"])
    assert all(
        record["accepted_policy"]["response_sha256"]
        and record["accepted_policy"]["evidence_citations"]
        and record["accepted_policy"]["packet_ids"]
        for record in by_status["dated_policy"]
    )
    assert all(
        record["target_evidence"]["cutoff_commit"]
        and record["target_evidence"]["case_id"]
        and record["target_evidence"]["repository_case_sha256"]
        for records in by_status.values()
        for record in records
    )
    assert all(
        record["accepted_policy"] is None
        and record["scope_exclusion_reason"] == "no_admissible_policy"
        and record["review_evidence"]
        and all(
            evidence["decision"] == "reject"
            and evidence["rationale"]
            and evidence["evidence_citations"]
            and evidence["response_sha256"]
            for evidence in record["review_evidence"]
        )
        for record in by_status["no_admissible_policy"]
    )
    assert all(
        record["accepted_policy"] is None
        and record["reviewed_event_count"] == 0
        and record["review_evidence"] == []
        and record["target_evidence"]["exclusion_reason"]
        == "no_frozen_ai_ban_candidate"
        and record["scope_exclusion_reason"] == "no_frozen_candidate"
        for record in by_status["no_frozen_candidate"]
    )


def test_catalog_rejects_pin_drift_and_incomplete_language_inventory(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    drifted = {**pins, "target_manifest": "0" * 64}

    with pytest.raises(AiBanCatalogError, match="independent pin"):
        materialize_ai_ban_catalog(paths, drifted)

    language = json.loads(paths["language_inventory"].read_text())
    language["repositories"].pop()
    language["repository_count"] -= 1
    language["inventory_sha256"] = repository_language_inventory_sha256(language)
    _write_json(paths["language_inventory"], language)
    pins["language_inventory"] = hashlib.sha256(
        paths["language_inventory"].read_bytes()
    ).hexdigest()

    with pytest.raises(AiBanCatalogError, match="exactly cover"):
        materialize_ai_ban_catalog(paths, pins)


def test_catalog_rejects_invalid_predecessor_root_type(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    invalid_path = tmp_path / "target-array.json"
    _write_json(invalid_path, [])
    paths["target_manifest"] = invalid_path
    pins["target_manifest"] = hashlib.sha256(invalid_path.read_bytes()).hexdigest()

    with pytest.raises(AiBanCatalogError, match="root contract"):
        materialize_ai_ban_catalog(paths, pins)


def test_catalog_rejects_wrong_blind_auditor_identity(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    responses = json.loads(paths["audit_responses_001"].read_text())
    responses[0]["reviewer_id"] = "primary-reviewer"
    forged_path = tmp_path / "forged-audit.json"
    _write_json(forged_path, responses)
    paths["audit_responses_001"] = forged_path
    pins["audit_responses_001"] = hashlib.sha256(forged_path.read_bytes()).hexdigest()

    with pytest.raises(AiBanCatalogError, match="auditor|reviewer"):
        materialize_ai_ban_catalog(paths, pins)


def test_catalog_rejects_outcome_exposed_target_even_when_rechecksummed(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    target = json.loads(paths["target_manifest"].read_text())
    target["classifier_outcomes_consulted"] = True
    target["target_manifest_sha256"] = ai_ban_target_manifest_sha256(target)
    exposed_path = tmp_path / "outcome-exposed-target.json"
    _write_json(exposed_path, target)
    paths["target_manifest"] = exposed_path
    pins["target_manifest"] = hashlib.sha256(exposed_path.read_bytes()).hexdigest()

    with pytest.raises(AiBanCatalogError, match="execution policy"):
        materialize_ai_ban_catalog(paths, pins)


def test_catalog_rejects_outcome_exposed_language_inventory(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    inventory = json.loads(paths["language_inventory"].read_text())
    inventory["classifier_outcomes_consulted"] = True
    inventory["inventory_sha256"] = repository_language_inventory_sha256(inventory)
    exposed_path = tmp_path / "outcome-exposed-languages.json"
    _write_json(exposed_path, inventory)
    paths["language_inventory"] = exposed_path
    pins["language_inventory"] = hashlib.sha256(exposed_path.read_bytes()).hexdigest()

    with pytest.raises(AiBanCatalogError, match="execution policy"):
        materialize_ai_ban_catalog(paths, pins)


def test_cli_requires_all_pins_and_writes_catalog(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    output = tmp_path / "catalog.json"
    arguments = [
        "--repo-root",
        str(ROOT),
        "--language-inventory",
        str(paths["language_inventory"]),
        "--output",
        str(output),
    ]
    for name, digest in pins.items():
        arguments.extend(["--pin", f"{name}={digest}"])

    assert catalog_cli.main(arguments) == 0
    catalog = json.loads(output.read_text())
    assert catalog["repository_count"] == 17
    assert catalog["catalog_sha256"] == catalog_sha256(catalog)


def test_catalog_detects_audit_decision_disagreement(tmp_path):
    paths, pins = _paths_and_pins(tmp_path)
    responses = copy.deepcopy(json.loads(paths["audit_responses_001"].read_text()))
    responses[0]["decision"] = "accept_policy"
    forged_path = tmp_path / "disagreeing-audit.json"
    _write_json(forged_path, responses)
    paths["audit_responses_001"] = forged_path
    pins["audit_responses_001"] = hashlib.sha256(forged_path.read_bytes()).hexdigest()

    with pytest.raises(AiBanCatalogError):
        materialize_ai_ban_catalog(paths, pins)
