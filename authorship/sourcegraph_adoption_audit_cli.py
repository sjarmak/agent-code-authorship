"""Materialize the combined, blind adoption reliability-audit artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_audit import build_adoption_reliability_audit
from authorship.sourcegraph_adoption_audit_adapter import (
    AdoptionAuditAdapterError,
    build_combined_adoption_audit_inputs,
)
from authorship.sourcegraph_adoption_review_cli import write_adoption_review_json
from authorship.sourcegraph_adoption_review_compile_cli import _load_mapping


def add_adoption_audit_source_arguments(
    parser: argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    parser.add_argument("--sequential-ledger", required=True, type=Path)
    parser.add_argument("--expected-sequential-ledger-sha256", required=True)
    parser.add_argument(
        "--sequential-tranche", action="append", required=True, type=Path
    )
    parser.add_argument(
        "--expected-sequential-tranche-sha256", action="append", required=True
    )
    parser.add_argument("--anchor-manifest", required=True, type=Path)
    parser.add_argument("--expected-anchor-manifest-sha256", required=True)
    parser.add_argument("--anchor-ledger", required=True, type=Path)
    parser.add_argument("--expected-anchor-ledger-sha256", required=True)
    parser.add_argument("--language-inventory", required=True, type=Path)
    parser.add_argument("--expected-language-inventory-sha256", required=True)
    return parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze one reliability audit across sequential and anchor review."
    )
    add_adoption_audit_source_arguments(parser)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def _require_hash(document: Mapping[str, Any], field: str, expected: str) -> None:
    if document.get(field) != expected:
        raise AdoptionAuditAdapterError(
            f"{field} differs from independently frozen hash"
        )


def _load_pinned_inputs(
    args: argparse.Namespace,
) -> tuple[dict, list, dict, dict, dict]:
    sequential = _load_mapping(args.sequential_ledger)
    manifest = _load_mapping(args.anchor_manifest)
    anchor = _load_mapping(args.anchor_ledger)
    inventory = _load_mapping(args.language_inventory)
    tranches = [_load_mapping(path) for path in args.sequential_tranche]
    if len(tranches) != len(args.expected_sequential_tranche_sha256):
        raise AdoptionAuditAdapterError("tranche paths and expected hashes differ")
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
        inventory,
        "inventory_sha256",
        args.expected_language_inventory_sha256,
    )
    for tranche, expected in zip(
        tranches, args.expected_sequential_tranche_sha256, strict=True
    ):
        _require_hash(tranche, "tranche_sha256", expected)
    return sequential, tranches, manifest, anchor, inventory


def _write_outputs(
    output_dir: Path,
    adapter: Mapping[str, Any],
    ledger: Mapping[str, Any],
    anchor_tranche: Mapping[str, Any],
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
) -> None:
    outputs = {
        "audit-source-adapter-008.json": adapter,
        "audit-combined-ledger-008.json": ledger,
        "audit-combined-tranche-008.json": anchor_tranche,
        "audit-worksheet-008.json": worksheet,
        "audit-key-008.json": key,
    }
    for name, document in outputs.items():
        write_adoption_review_json(output_dir / name, dict(document))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sequential, tranches, manifest, anchor, inventory = _load_pinned_inputs(args)
    combined, all_tranches, adapter = build_combined_adoption_audit_inputs(
        sequential,
        tranches,
        manifest,
        anchor,
    )
    worksheet, key = build_adoption_reliability_audit(
        combined,
        all_tranches,
        inventory,
        seed=args.seed,
    )
    _write_outputs(
        args.output_dir,
        adapter,
        combined,
        all_tranches[-1],
        worksheet,
        key,
    )
    print(
        json.dumps(
            {
                "adapter_sha256": adapter["adapter_sha256"],
                "audit_task_count": worksheet["sample_count"],
                "key_sha256": key["key_sha256"],
                "worksheet_sha256": worksheet["worksheet_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
