"""CLI for materializing the bounded AI-ban target and review worksheet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from authorship.sourcegraph_ai_ban_review_io import materialize_ai_ban_review


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    _add_input_arguments(parser)
    _add_pin_arguments(parser)
    _add_output_arguments(parser)
    return parser


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--control-evidence",
        type=Path,
        default=Path("data/control_evidence.json"),
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
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
        "--discovery",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=Path("study/sourcegraph-adjudication-workflow.v3.json"),
    )
    parser.add_argument(
        "--packet-index",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.v3.json"),
    )
    parser.add_argument("--decisions", type=Path)


def _add_pin_arguments(parser: argparse.ArgumentParser) -> None:
    for name in (
        "control-evidence",
        "index-manifest",
        "case-index-file",
        "case-index",
        "packet-index",
    ):
        parser.add_argument(f"--expected-{name}-sha256", required=True)
    parser.add_argument("--expected-decision-ledger-sha256")


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target-output",
        type=Path,
        default=Path("study/sourcegraph-ai-ban-target-manifest.v3.json"),
    )
    parser.add_argument(
        "--worksheet-output",
        type=Path,
        default=Path("study/sourcegraph-ai-ban-review-worksheet.v3.json"),
    )


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target, worksheet = materialize_ai_ban_review(
        control_evidence_path=args.control_evidence,
        index_manifest_path=args.index_manifest,
        case_index_path=args.case_index,
        case_root=args.case_root,
        discovery_path=args.discovery,
        workflow_path=args.workflow,
        packet_index_path=args.packet_index,
        decision_ledger_path=args.decisions,
        expected_control_evidence_sha256=args.expected_control_evidence_sha256,
        expected_index_manifest_sha256=args.expected_index_manifest_sha256,
        expected_case_index_file_sha256=args.expected_case_index_file_sha256,
        expected_case_index_sha256=args.expected_case_index_sha256,
        expected_packet_index_sha256=args.expected_packet_index_sha256,
        expected_decision_ledger_sha256=args.expected_decision_ledger_sha256,
    )
    _write(args.target_output, target)
    _write(args.worksheet_output, worksheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
