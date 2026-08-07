"""CLI for materializing sequential Sourcegraph adoption-review tranches."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_review import (
    AdoptionReviewError,
    materialize_adoption_review_tranche,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize the next earliest-unresolved adoption-review tranche."
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
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--expected-decision-ledger-sha256")
    parser.add_argument("--tranche-number", type=int, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    return parser


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdoptionReviewError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AdoptionReviewError(f"{path} must contain a JSON object")
    return value


def _load_decision_ledger(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    return _load_mapping(path)


def write_adoption_review_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    case_index = _load_mapping(args.case_index)
    review_protocol = _load_mapping(args.review_protocol)
    discovery = _load_mapping(args.discovery_specification)
    workflow = _load_mapping(args.workflow_specification)
    decision_ledger = _load_decision_ledger(args.decisions)
    manifest = materialize_adoption_review_tranche(
        case_index,
        review_protocol,
        args.case_root,
        discovery,
        workflow,
        args.packet_index,
        decision_ledger,
        expected_decision_ledger_sha256=args.expected_decision_ledger_sha256,
        tranche_number=args.tranche_number,
    )
    write_adoption_review_json(args.output, manifest)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "tranche_number": manifest["tranche_number"],
                "task_count": manifest["task_count"],
                "tranche_sha256": manifest["tranche_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
