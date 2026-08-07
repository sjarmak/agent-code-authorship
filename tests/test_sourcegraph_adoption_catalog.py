import copy
import json
from pathlib import Path

import jsonschema
import pytest

import authorship.sourcegraph_adoption_catalog_cli as catalog_cli
from authorship.sourcegraph_adoption_catalog import (
    AdoptionCatalogError,
    adoption_catalog_sha256,
    build_adoption_catalog,
)

ROOT = Path(__file__).parents[1]


def _load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def _artifacts() -> tuple[dict, list[dict], dict, dict, dict]:
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
    languages = _load("study/sourcegraph-repository-languages.v3.json")
    return sequential, tranches, anchor_manifest, anchor_ledger, languages


def test_catalog_freezes_dated_exhausted_and_scope_statuses():
    catalog = build_adoption_catalog(*_artifacts())

    assert catalog["repository_count"] == 248
    assert catalog["status_counts"] == {
        "dated_explicit_anchor": 66,
        "dated_sequential": 102,
        "no_credible_event": 80,
    }
    assert catalog["dated_repository_count"] == 168
    assert catalog["primary_scope_eligible_count"] == 247
    react = next(
        row for row in catalog["repositories"] if row["repository_id"] == "react/react"
    )
    assert react["status"] == "dated_explicit_anchor"
    assert react["primary_scope_eligible"] is False
    assert react["scope_exclusion_reason"] == "review_evidence_outside_sg_evals_org"
    assert react["adoption_interpretation"] == "first_observed_explicit_use"
    assert catalog["catalog_sha256"] == adoption_catalog_sha256(catalog)
    jsonschema.validate(
        catalog,
        _load("study/sourcegraph-adoption-catalog.schema.json"),
    )


def test_catalog_retains_clean_prehistory_and_unresolved_counts():
    catalog = build_adoption_catalog(*_artifacts())
    anchor_rows = [
        row
        for row in catalog["repositories"]
        if row["status"] == "dated_explicit_anchor"
    ]

    assert any(row["clean_prehistory"] for row in anchor_rows)
    assert any(not row["clean_prehistory"] for row in anchor_rows)
    assert all(
        row["unresolved_earlier_candidate_count"] > 0
        for row in anchor_rows
        if not row["clean_prehistory"]
    )


def test_catalog_rejects_self_consistent_predecessor_drift():
    sequential, tranches, anchor_manifest, anchor_ledger, languages = _artifacts()
    tampered = copy.deepcopy(anchor_ledger)
    tampered["source_decision_ledger_sha256"] = "0" * 64
    from authorship.sourcegraph_adoption_anchor_completion import (
        anchor_completion_ledger_sha256,
    )

    tampered["anchor_completion_ledger_sha256"] = anchor_completion_ledger_sha256(
        tampered
    )

    with pytest.raises(AdoptionCatalogError, match="source ledger binding"):
        build_adoption_catalog(
            sequential,
            tranches,
            anchor_manifest,
            tampered,
            languages,
        )


def _catalog_cli_arguments(tmp_path: Path) -> list[str]:
    sequential, tranches, manifest, ledger, languages = _artifacts()
    arguments = [
        "--sequential-ledger",
        str(ROOT / "results/sourcegraph-adoption-review-v3/decision-ledger-007.json"),
        "--expected-sequential-ledger-sha256",
        sequential["decision_ledger_sha256"],
    ]
    for number, tranche in enumerate(tranches, start=1):
        arguments.extend(
            [
                "--tranche",
                str(
                    ROOT
                    / f"results/sourcegraph-adoption-review-v3/tranche-{number:03d}.json"
                ),
                "--expected-tranche-sha256",
                tranche["tranche_sha256"],
            ]
        )
    return [
        *arguments,
        "--anchor-manifest",
        str(ROOT / "results/sourcegraph-adoption-review-v3/anchor-completion-008.json"),
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
        languages["inventory_sha256"],
        "--output",
        str(tmp_path / "catalog.json"),
    ]


def test_catalog_cli_requires_independently_frozen_predecessor_hashes(tmp_path):
    arguments = _catalog_cli_arguments(tmp_path)

    assert catalog_cli.main(arguments) == 0
    assert (
        json.loads((tmp_path / "catalog.json").read_text())["repository_count"] == 248
    )
    arguments[arguments.index("--expected-anchor-ledger-sha256") + 1] = "0" * 64

    with pytest.raises(AdoptionCatalogError, match="independently frozen"):
        catalog_cli.main(arguments)
