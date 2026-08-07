"""CLI for building repository/event Sourcegraph adjudication cases."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_repository_cases import (
    RepositoryCaseError,
    write_case_index,
)
from authorship.sourcegraph_repository_case_stream import (
    build_repository_case_index_from_file,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build compact repository/event Sourcegraph review cases."
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
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("results/sourcegraph-repository-cases-v3"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("study/sourcegraph-repository-case-index.v3.json"),
    )
    return parser


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RepositoryCaseError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RepositoryCaseError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    discovery = _load_mapping(args.discovery_specification)
    workflow = _load_mapping(args.workflow_specification)
    manifest = build_repository_case_index_from_file(
        discovery,
        workflow,
        args.packet_index,
        args.output_root,
    )
    args.manifest_output.parent.mkdir(parents=True, exist_ok=True)
    write_case_index(args.manifest_output, manifest)
    print(
        json.dumps(
            {
                "manifest": str(args.manifest_output),
                "repository_count": manifest["repository_count"],
                "event_count": manifest["event_count"],
                "packet_count": manifest["packet_count"],
                "case_index_sha256": manifest["case_index_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
