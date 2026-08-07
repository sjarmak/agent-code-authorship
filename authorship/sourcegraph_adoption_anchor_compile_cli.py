"""Compile explicit-anchor responses after independent rematerialization."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.sourcegraph_adoption_anchor_completion import (
    AnchorCompletionError,
    compile_anchor_completion_responses,
    materialize_anchor_completion_manifest,
)
from authorship.sourcegraph_adoption_review_cli import write_adoption_review_json
from authorship.sourcegraph_adoption_review_compile_cli import (
    _load_mapping,
    _responses,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile the explicit-anchor completion tranche."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--response-bundle", type=Path, action="append", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--expected-decision-ledger-sha256", required=True)
    parser.add_argument("--expected-amendment-sha256", required=True)
    parser.add_argument(
        "--case-index",
        type=Path,
        default=Path("study/sourcegraph-repository-case-index.v3.json"),
    )
    parser.add_argument(
        "--case-root",
        type=Path,
        default=Path("results/sourcegraph-repository-cases-v3"),
    )
    parser.add_argument(
        "--review-protocol",
        type=Path,
        default=Path("study/sourcegraph-adoption-review-protocol.v3.json"),
    )
    parser.add_argument(
        "--amendment",
        type=Path,
        default=Path("study/sourcegraph-adoption-anchor-amendment.v3.json"),
    )
    parser.add_argument(
        "--discovery-specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--workflow-specification",
        type=Path,
        default=Path("study/sourcegraph-adjudication-workflow.v3.json"),
    )
    parser.add_argument(
        "--packet-index",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.v3.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = _load_mapping(args.manifest)
    if manifest.get("manifest_sha256") != args.expected_manifest_sha256:
        raise AnchorCompletionError("manifest differs from frozen expected checksum")
    decision_ledger = _load_mapping(args.decisions)
    rematerialized = materialize_anchor_completion_manifest(
        _load_mapping(args.case_index),
        _load_mapping(args.review_protocol),
        _load_mapping(args.amendment),
        args.case_root,
        _load_mapping(args.discovery_specification),
        _load_mapping(args.workflow_specification),
        args.packet_index,
        decision_ledger,
        expected_decision_ledger_sha256=args.expected_decision_ledger_sha256,
        expected_amendment_sha256=args.expected_amendment_sha256,
        tranche_number=manifest["tranche_number"],
    )
    if rematerialized != manifest:
        raise AnchorCompletionError("manifest does not match frozen rematerialization")
    ledger = compile_anchor_completion_responses(
        manifest,
        _responses(args.response_bundle, manifest),
    )
    write_adoption_review_json(args.output, ledger)
    print(
        json.dumps(
            {
                "decision_count": ledger["decision_count"],
                "ledger_sha256": ledger["anchor_completion_ledger_sha256"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
