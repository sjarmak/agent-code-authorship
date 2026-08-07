"""CLI for resumable exact survival-event materialization."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from authorship.survival_event_batch import execute_event_materialization


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-inventory", type=Path, required=True)
    parser.add_argument("--lineage-inventory", type=Path, required=True)
    parser.add_argument("--lineage-validation", type=Path, required=True)
    parser.add_argument("--transition-root", type=Path, required=True)
    parser.add_argument("--structural-event-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--commit-batch-size", type=int, default=100_000)
    return parser


def _progress(entry: dict[str, Any], index: int, total: int) -> None:
    print(
        f"[{index}/{total}] {entry['repository_id']}: {entry['status']}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    inventory = execute_event_materialization(
        git_inventory_path=arguments.git_inventory,
        lineage_inventory_path=arguments.lineage_inventory,
        lineage_validation_path=arguments.lineage_validation,
        transition_root=arguments.transition_root,
        structural_event_root=arguments.structural_event_root,
        output_root=arguments.output_root,
        work_root=arguments.work_root,
        commit_batch_size=arguments.commit_batch_size,
        progress=_progress,
    )
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0 if inventory["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
