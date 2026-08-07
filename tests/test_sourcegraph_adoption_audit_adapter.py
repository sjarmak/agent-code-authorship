import copy
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_adoption_audit_compile_cli as audit_compile_cli
import authorship.sourcegraph_adoption_audit_cli as audit_cli
from authorship.sourcegraph_adoption_audit_adapter import (
    AdoptionAuditAdapterError,
    audit_adapter_sha256,
    build_combined_adoption_audit_inputs,
)
from authorship.sourcegraph_adoption_anchor_completion import (
    anchor_completion_ledger_sha256,
    anchor_completion_manifest_sha256,
)
from authorship.sourcegraph_adoption_review import (
    adoption_review_tranche_sha256,
    validate_adoption_decision_ledger,
)

ROOT = Path(__file__).parents[1]


def _load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def _inputs() -> tuple[dict, list[dict], dict, dict]:
    sequential = _load(
        "results/sourcegraph-adoption-review-v3/decision-ledger-007.json"
    )
    tranches = [
        _load(f"results/sourcegraph-adoption-review-v3/tranche-{number:03d}.json")
        for number in range(1, 8)
    ]
    anchor_manifest = _load(
        "results/sourcegraph-adoption-review-v3/anchor-completion-008.json"
    )
    anchor_ledger = _load(
        "results/sourcegraph-adoption-review-v3/anchor-decision-ledger-008.json"
    )
    return sequential, tranches, anchor_manifest, anchor_ledger


def test_adapter_combines_sequential_and_anchor_decisions_for_one_audit_frame():
    combined, tranches, adapter = build_combined_adoption_audit_inputs(*_inputs())

    assert len(tranches) == 8
    assert tranches[-1]["task_count"] == 105
    assert tranches[-1]["tranche_sha256"] == adoption_review_tranche_sha256(
        tranches[-1]
    )
    assert combined["prior_decision_count"] == 1119
    assert combined["response_count"] == 105
    assert combined["decision_count"] == 1224
    assert (
        len(
            validate_adoption_decision_ledger(
                combined,
                case_index_sha256_value=combined["case_index_sha256"],
                workflow_sha256_value=combined["workflow_sha256"],
            )
        )
        == 1224
    )
    assert adapter["adapter_sha256"] == audit_adapter_sha256(adapter)
    jsonschema.validate(
        adapter,
        _load("study/sourcegraph-adoption-audit-adapter.schema.json"),
    )


def test_adapter_outputs_do_not_share_mutable_rows_with_sources():
    sequential, source_tranches, manifest, ledger = _inputs()
    combined, tranches, _adapter = build_combined_adoption_audit_inputs(
        sequential,
        source_tranches,
        manifest,
        ledger,
    )

    combined["decisions"][0]["rationale"] = "changed"
    tranches[0]["tasks"][0]["candidate_event"]["observed_at"] = "changed"
    tranches[-1]["tasks"][0]["candidate_event"]["observed_at"] = "changed"

    assert sequential["decisions"][0]["rationale"] != "changed"
    assert source_tranches[0]["tasks"][0]["candidate_event"]["observed_at"] != "changed"
    assert manifest["tasks"][0]["candidate_event"]["observed_at"] != "changed"


def test_adapter_rejects_self_consistent_anchor_ledger_drift():
    sequential, tranches, manifest, ledger = _inputs()
    forged = copy.deepcopy(ledger)
    forged["source_decision_ledger_sha256"] = "0" * 64
    forged["anchor_completion_ledger_sha256"] = anchor_completion_ledger_sha256(forged)

    with pytest.raises(AdoptionAuditAdapterError, match="source ledger"):
        build_combined_adoption_audit_inputs(
            sequential,
            tranches,
            manifest,
            forged,
        )


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("tranche_checksum", "tranche checksum"),
        ("anchor_manifest_checksum", "manifest checksum"),
        ("anchor_ledger_checksum", "ledger checksum"),
        ("anchor_adjacency", "sequentially adjacent"),
    ],
)
def test_adapter_fails_closed_on_source_contract_drift(target, message):
    sequential, tranches, manifest, ledger = _inputs()
    if target == "tranche_checksum":
        tranches[0]["tranche_sha256"] = "0" * 64
    elif target == "anchor_manifest_checksum":
        manifest["manifest_sha256"] = "0" * 64
    elif target == "anchor_ledger_checksum":
        ledger["anchor_completion_ledger_sha256"] = "0" * 64
    else:
        manifest["tranche_number"] = 9
        manifest["manifest_sha256"] = anchor_completion_manifest_sha256(manifest)
        ledger["manifest_sha256"] = manifest["manifest_sha256"]
        ledger["anchor_completion_ledger_sha256"] = anchor_completion_ledger_sha256(
            ledger
        )

    with pytest.raises(AdoptionAuditAdapterError, match=message):
        build_combined_adoption_audit_inputs(
            sequential,
            tranches,
            manifest,
            ledger,
        )


def test_audit_cli_writes_only_independently_pinned_outputs(tmp_path):
    sequential, tranches, manifest, ledger = _inputs()
    inventory = _load("study/sourcegraph-repository-languages.v3.json")
    arguments = [
        "--sequential-ledger",
        str(ROOT / "results/sourcegraph-adoption-review-v3/decision-ledger-007.json"),
        "--expected-sequential-ledger-sha256",
        sequential["decision_ledger_sha256"],
    ]
    for number, tranche in enumerate(tranches, start=1):
        arguments.extend(
            [
                "--sequential-tranche",
                str(
                    ROOT
                    / f"results/sourcegraph-adoption-review-v3/tranche-{number:03d}.json"
                ),
                "--expected-sequential-tranche-sha256",
                tranche["tranche_sha256"],
            ]
        )
    arguments.extend(
        [
            "--anchor-manifest",
            str(
                ROOT
                / "results/sourcegraph-adoption-review-v3/anchor-completion-008.json"
            ),
            "--expected-anchor-manifest-sha256",
            manifest["manifest_sha256"],
            "--anchor-ledger",
            str(
                ROOT
                / "results/sourcegraph-adoption-review-v3/anchor-decision-ledger-008.json"
            ),
            "--expected-anchor-ledger-sha256",
            ledger["anchor_completion_ledger_sha256"],
            "--language-inventory",
            str(ROOT / "study/sourcegraph-repository-languages.v3.json"),
            "--expected-language-inventory-sha256",
            inventory["inventory_sha256"],
            "--seed",
            "test-combined-audit",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert audit_cli.main(arguments) == 0
    assert len(json.loads((tmp_path / "audit-worksheet-008.json").read_text())["tasks"])
    arguments[arguments.index("--expected-anchor-ledger-sha256") + 1] = "0" * 64
    with pytest.raises(AdoptionAuditAdapterError, match="independently frozen"):
        audit_cli.main(arguments)


def test_audit_compile_cli_rematerializes_and_pins_auditor(
    monkeypatch,
    tmp_path,
):
    captured = {}
    monkeypatch.setattr(
        audit_compile_cli,
        "_load_pinned_inputs",
        lambda _args: ({}, [], {}, {}, {}),
    )
    monkeypatch.setattr(
        audit_compile_cli,
        "build_combined_adoption_audit_inputs",
        lambda *_args: ({}, [{}], {"adapter_sha256": "a" * 64}),
    )
    monkeypatch.setattr(
        audit_compile_cli,
        "_load_mapping",
        lambda path: (
            {"tasks": [{"audit_task_id": "audit-task"}]}
            if path.name == "worksheet.json"
            else {}
        ),
    )
    monkeypatch.setattr(audit_compile_cli, "_responses", lambda _path: [{}])

    def compile_responses(*_args, **kwargs):
        captured["auditors"] = kwargs["expected_auditor_ids"]
        return {"compiled_audit_sha256": "b" * 64, "response_count": 1}

    monkeypatch.setattr(
        audit_compile_cli,
        "compile_adoption_reliability_responses",
        compile_responses,
    )
    monkeypatch.setattr(
        audit_compile_cli,
        "write_adoption_review_json",
        lambda path, document: captured.update(path=path, document=document),
    )
    arguments = [
        "--sequential-ledger",
        str(tmp_path / "sequential.json"),
        "--expected-sequential-ledger-sha256",
        "1" * 64,
        "--sequential-tranche",
        str(tmp_path / "tranche.json"),
        "--expected-sequential-tranche-sha256",
        "2" * 64,
        "--anchor-manifest",
        str(tmp_path / "manifest.json"),
        "--expected-anchor-manifest-sha256",
        "3" * 64,
        "--anchor-ledger",
        str(tmp_path / "anchor.json"),
        "--expected-anchor-ledger-sha256",
        "4" * 64,
        "--language-inventory",
        str(tmp_path / "languages.json"),
        "--expected-language-inventory-sha256",
        "5" * 64,
        "--worksheet",
        str(tmp_path / "worksheet.json"),
        "--expected-worksheet-sha256",
        "6" * 64,
        "--key",
        str(tmp_path / "key.json"),
        "--expected-key-sha256",
        "7" * 64,
        "--expected-adapter-sha256",
        "a" * 64,
        "--response-bundle",
        str(tmp_path / "responses.json"),
        "--auditor-id",
        "fresh-auditor",
        "--seed",
        "seed",
        "--output",
        str(tmp_path / "compiled.json"),
    ]

    assert audit_compile_cli.main(arguments) == 0
    assert captured["auditors"] == {"audit-task": "fresh-auditor"}
    assert captured["path"] == tmp_path / "compiled.json"


def test_audit_compile_cli_validates_response_bundle_shape(tmp_path):
    path = tmp_path / "responses.json"
    path.write_text('[{"audit_task_id": "task"}]')
    assert audit_compile_cli._responses(path) == [{"audit_task_id": "task"}]
    path.write_text('{"audit_task_id": "task"}')

    with pytest.raises(AdoptionAuditAdapterError, match="must be an array"):
        audit_compile_cli._responses(path)
