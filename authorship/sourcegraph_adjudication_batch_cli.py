"""Prepare or import local OpenAI Batch files for blinded adjudication."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_batch import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REQUESTS,
    AdjudicationBatchError,
    export_openai_review_batch,
    import_openai_review_batch,
)
from authorship.sourcegraph_adjudication_compiler import atomic_write_json

DEFAULT_QUEUE_ROOT = Path(
    "/mnt/agent-code-authorship/survival-study/"
    "sourcegraph-adjudication-v3/work-queue"
)
DEFAULT_BATCH_ROOT = Path(
    "/mnt/agent-code-authorship/survival-study/"
    "sourcegraph-adjudication-v3/model-review"
)


def _common(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument("--queue-root", type=Path, default=DEFAULT_QUEUE_ROOT)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    _common(export)
    export.add_argument("--reviewer-id", required=True)
    export.add_argument("--model", required=True)
    export.add_argument("--reasoning-effort", default="low")
    export.add_argument("--output-root", type=Path, default=DEFAULT_BATCH_ROOT)
    export.add_argument(
        "--manifest",
        type=Path,
        default=Path("study/sourcegraph-adjudication-batch-export.v3.json"),
    )
    export.add_argument(
        "--max-requests-per-file", type=int, default=DEFAULT_MAX_REQUESTS
    )
    export.add_argument("--max-bytes-per-file", type=int, default=DEFAULT_MAX_BYTES)
    import_command = commands.add_parser("import")
    _common(import_command)
    import_command.add_argument("--batch-manifest", type=Path, required=True)
    import_command.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    import_command.add_argument(
        "--output-file", type=Path, action="append", required=True
    )
    import_command.add_argument("--review-response", type=Path, required=True)
    return parser


def _load(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdjudicationBatchError(f"cannot read {label}: {error}") from error
    if not isinstance(document, Mapping):
        raise AdjudicationBatchError(f"{label} must be an object")
    return document


def _export(arguments: argparse.Namespace) -> int:
    manifest = export_openai_review_batch(
        _load(arguments.specification, "specification"),
        _load(arguments.queue_manifest, "queue manifest"),
        arguments.queue_root,
        arguments.output_root,
        reviewer_id=arguments.reviewer_id,
        model=arguments.model,
        reasoning_effort=arguments.reasoning_effort,
        max_requests_per_file=arguments.max_requests_per_file,
        max_bytes_per_file=arguments.max_bytes_per_file,
    )
    atomic_write_json(arguments.manifest, manifest)
    return 0


def _import(arguments: argparse.Namespace) -> int:
    response = import_openai_review_batch(
        _load(arguments.specification, "specification"),
        _load(arguments.queue_manifest, "queue manifest"),
        arguments.queue_root,
        _load(arguments.batch_manifest, "batch manifest"),
        arguments.batch_root,
        arguments.output_file,
    )
    atomic_write_json(arguments.review_response, response)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return _export(arguments) if arguments.command == "export" else _import(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
