"""CLI for building the blinded Sourcegraph adjudication work queue."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_queue import (
    DEFAULT_SHARD_COUNT,
    AdjudicationQueueError,
    atomic_write_json,
    build_adjudication_work_queue,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--packet-index",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.v3.json"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "sourcegraph-adjudication-v3/work-queue"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("study/sourcegraph-adjudication-work-queue.v3.json"),
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=DEFAULT_SHARD_COUNT,
    )
    return parser


def _load(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdjudicationQueueError(f"cannot read {label}: {error}") from error
    if not isinstance(document, Mapping):
        raise AdjudicationQueueError(f"{label} must be an object")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manifest = build_adjudication_work_queue(
        _load(arguments.specification, "specification"),
        _load(arguments.packet_index, "packet index"),
        arguments.output_root,
        shard_count=arguments.shard_count,
    )
    atomic_write_json(arguments.manifest, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
