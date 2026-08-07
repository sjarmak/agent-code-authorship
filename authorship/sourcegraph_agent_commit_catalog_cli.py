"""CLI for freezing reviewed explicit-provenance agent commits."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_review_cli import write_adoption_review_json
from authorship.sourcegraph_agent_commit_catalog import (
    AgentCommitCatalogError,
    build_agent_commit_catalog,
)

REVIEW_ROOT = Path("results/sourcegraph-adoption-review-v3")


def _add_sequential_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sequential-ledger",
        type=Path,
        default=REVIEW_ROOT / "decision-ledger-007.json",
    )
    parser.add_argument("--expected-sequential-ledger-sha256", required=True)
    parser.add_argument("--tranche", type=Path, action="append", required=True)
    parser.add_argument("--expected-tranche-sha256", action="append", required=True)


def _add_anchor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--anchor-manifest",
        type=Path,
        default=REVIEW_ROOT / "anchor-completion-008.json",
    )
    parser.add_argument("--expected-anchor-manifest-sha256", required=True)
    parser.add_argument(
        "--anchor-ledger",
        type=Path,
        default=REVIEW_ROOT / "anchor-decision-ledger-008.json",
    )
    parser.add_argument("--expected-anchor-ledger-sha256", required=True)


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--language-inventory",
        type=Path,
        default=Path("study/sourcegraph-repository-languages.v3.json"),
    )
    parser.add_argument("--expected-language-inventory-sha256", required=True)
    parser.add_argument(
        "--compiled-audit",
        type=Path,
        default=REVIEW_ROOT / "audit-compiled-008.json",
    )
    parser.add_argument("--expected-compiled-audit-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze reviewed explicit-provenance agent commits."
    )
    _add_sequential_arguments(parser)
    _add_anchor_arguments(parser)
    _add_context_arguments(parser)
    return parser


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AgentCommitCatalogError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AgentCommitCatalogError(f"{path} must contain a JSON object")
    return value


def _require_hash(document: Mapping[str, Any], field: str, expected: str) -> None:
    if document.get(field) != expected:
        raise AgentCommitCatalogError(f"{field} differs from independently frozen hash")


def _load_inputs(args: argparse.Namespace) -> tuple[dict, list, dict, dict, dict, dict]:
    sequential = _load(args.sequential_ledger)
    tranches = [_load(path) for path in args.tranche]
    manifest = _load(args.anchor_manifest)
    anchor = _load(args.anchor_ledger)
    languages = _load(args.language_inventory)
    audit = _load(args.compiled_audit)
    if len(tranches) != len(args.expected_tranche_sha256):
        raise AgentCommitCatalogError(
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
    _require_hash(
        audit,
        "compiled_audit_sha256",
        args.expected_compiled_audit_sha256,
    )
    for tranche, expected in zip(tranches, args.expected_tranche_sha256, strict=True):
        _require_hash(tranche, "tranche_sha256", expected)
    return sequential, tranches, manifest, anchor, languages, audit


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    catalog = build_agent_commit_catalog(*_load_inputs(args))
    write_adoption_review_json(args.output, catalog)
    print(
        json.dumps(
            {
                "audit_sampled_commit_count": catalog["audit_sampled_commit_count"],
                "catalog_sha256": catalog["catalog_sha256"],
                "commit_count": catalog["commit_count"],
                "excluded_event_count": catalog["excluded_event_count"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
