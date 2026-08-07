"""CLI for materializing the bounded explicit-anchor completion tranche."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.sourcegraph_adoption_anchor_completion import (
    materialize_anchor_completion_manifest,
)
from authorship.sourcegraph_adoption_review_cli import (
    _load_mapping,
    write_adoption_review_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize first explicit anchors for active repositories."
    )
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
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--expected-decision-ledger-sha256", required=True)
    parser.add_argument("--expected-amendment-sha256", required=True)
    parser.add_argument("--tranche-number", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = materialize_anchor_completion_manifest(
        _load_mapping(args.case_index),
        _load_mapping(args.review_protocol),
        _load_mapping(args.amendment),
        args.case_root,
        _load_mapping(args.discovery_specification),
        _load_mapping(args.workflow_specification),
        args.packet_index,
        _load_mapping(args.decisions),
        expected_decision_ledger_sha256=args.expected_decision_ledger_sha256,
        expected_amendment_sha256=args.expected_amendment_sha256,
        tranche_number=args.tranche_number,
    )
    write_adoption_review_json(args.output, manifest)
    print(
        json.dumps(
            {
                "manifest_sha256": manifest["manifest_sha256"],
                "output": str(args.output),
                "task_count": manifest["task_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
