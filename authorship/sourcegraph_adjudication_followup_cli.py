"""CLI for deriving peer-blind follow-up assignments from primary reviews."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_compiler import (
    AdjudicationCompilerError,
    atomic_write_json,
    build_followup_assignments,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--queue-manifest",
        type=Path,
        default=Path("study/sourcegraph-adjudication-work-queue.v3.json"),
    )
    parser.add_argument(
        "--queue-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "sourcegraph-adjudication-v3/work-queue"
        ),
    )
    parser.add_argument(
        "--primary-response",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument(
        "--assignment",
        type=Path,
        default=Path("study/sourcegraph-adjudication-followup-assignment.v3.json"),
    )
    return parser


def _load(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdjudicationCompilerError(f"cannot read {label}: {error}") from error
    if not isinstance(document, Mapping):
        raise AdjudicationCompilerError(f"{label} must be an object")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    assignment = build_followup_assignments(
        _load(arguments.specification, "specification"),
        _load(arguments.queue_manifest, "queue manifest"),
        arguments.queue_root,
        [
            _load(path, f"primary response {index}")
            for index, path in enumerate(arguments.primary_response, start=1)
        ],
    )
    atomic_write_json(arguments.assignment, assignment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
