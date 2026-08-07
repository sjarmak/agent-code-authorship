"""CLI for freezing the repository-level Sourcegraph adoption catalog."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.sourcegraph_adoption_catalog import (
    AdoptionCatalogError,
    build_adoption_catalog,
)
from authorship.sourcegraph_adoption_review_cli import write_adoption_review_json
from authorship.sourcegraph_adoption_review_compile_cli import _load_mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Freeze repository adoption dates.")
    parser.add_argument(
        "--sequential-ledger",
        type=Path,
        default=Path("results/sourcegraph-adoption-review-v3/decision-ledger-007.json"),
    )
    parser.add_argument("--expected-sequential-ledger-sha256", required=True)
    parser.add_argument("--tranche", type=Path, action="append", required=True)
    parser.add_argument("--expected-tranche-sha256", action="append", required=True)
    parser.add_argument(
        "--anchor-manifest",
        type=Path,
        default=Path(
            "results/sourcegraph-adoption-review-v3/anchor-completion-008.json"
        ),
    )
    parser.add_argument("--expected-anchor-manifest-sha256", required=True)
    parser.add_argument(
        "--anchor-ledger",
        type=Path,
        default=Path(
            "results/sourcegraph-adoption-review-v3/anchor-decision-ledger-008.json"
        ),
    )
    parser.add_argument("--expected-anchor-ledger-sha256", required=True)
    parser.add_argument(
        "--language-inventory",
        type=Path,
        default=Path("study/sourcegraph-repository-languages.v3.json"),
    )
    parser.add_argument("--expected-language-inventory-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _require_hash(document: dict, field: str, expected: str) -> None:
    if document.get(field) != expected:
        raise AdoptionCatalogError(f"{field} differs from independently frozen hash")


def _load_inputs(args: argparse.Namespace) -> tuple[dict, list, dict, dict, dict]:
    sequential = _load_mapping(args.sequential_ledger)
    tranches = [_load_mapping(path) for path in args.tranche]
    manifest = _load_mapping(args.anchor_manifest)
    anchor = _load_mapping(args.anchor_ledger)
    languages = _load_mapping(args.language_inventory)
    if len(tranches) != len(args.expected_tranche_sha256):
        raise AdoptionCatalogError(
            "tranche paths and independently frozen hashes differ"
        )
    _require_hash(
        sequential,
        "decision_ledger_sha256",
        args.expected_sequential_ledger_sha256,
    )
    _require_hash(manifest, "manifest_sha256", args.expected_anchor_manifest_sha256)
    _require_hash(
        anchor,
        "anchor_completion_ledger_sha256",
        args.expected_anchor_ledger_sha256,
    )
    _require_hash(
        languages,
        "inventory_sha256",
        args.expected_language_inventory_sha256,
    )
    for tranche, expected in zip(tranches, args.expected_tranche_sha256, strict=True):
        _require_hash(tranche, "tranche_sha256", expected)
    return sequential, tranches, manifest, anchor, languages


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    catalog = build_adoption_catalog(*_load_inputs(args))
    write_adoption_review_json(args.output, catalog)
    print(
        json.dumps(
            {
                "catalog_sha256": catalog["catalog_sha256"],
                "dated_repository_count": catalog["dated_repository_count"],
                "output": str(args.output),
                "repository_count": catalog["repository_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
