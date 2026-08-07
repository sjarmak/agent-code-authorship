"""CLI for compiling blinded event reviews into packet adjudication bundles."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_compiler import (
    DEFAULT_BUNDLE_SHARD_COUNT,
    AdjudicationCompilerError,
    atomic_write_json,
    compile_adjudication_bundles,
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
    parser.add_argument("--resolution-response", type=Path)
    parser.add_argument("--audit-response", type=Path)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "sourcegraph-adjudication-v3/packet-bundles"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("study/sourcegraph-adjudication-bundle-execution.v3.json"),
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=DEFAULT_BUNDLE_SHARD_COUNT,
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


def _optional_load(path: Path | None, label: str) -> Mapping[str, Any] | None:
    return _load(path, label) if path is not None else None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manifest = compile_adjudication_bundles(
        _load(arguments.specification, "specification"),
        _load(arguments.queue_manifest, "queue manifest"),
        arguments.queue_root,
        [
            _load(path, f"primary response {index}")
            for index, path in enumerate(arguments.primary_response, start=1)
        ],
        arguments.output_root,
        resolution_response=_optional_load(
            arguments.resolution_response, "resolution response"
        ),
        audit_response=_optional_load(arguments.audit_response, "audit response"),
        shard_count=arguments.shard_count,
    )
    atomic_write_json(arguments.manifest, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
