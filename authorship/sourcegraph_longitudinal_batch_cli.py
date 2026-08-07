"""CLI for freezing the longitudinal Sourcegraph pre-probe batch plan."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_batch import build_batch_plan

DEFAULT_ROOT = Path("/mnt/agent-code-authorship/survival-study")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--git-inventory", type=Path, default=DEFAULT_ROOT / "git-inventory.v1.json"
    )
    parser.add_argument(
        "--cohort-inventory",
        type=Path,
        default=DEFAULT_ROOT / "cohort-inventory.v1.json",
    )
    parser.add_argument(
        "--lineage-inventory",
        type=Path,
        default=DEFAULT_ROOT / "lineage-v1/lineage-inventory.v1.json",
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
    )
    parser.add_argument(
        "--transition-root", type=Path, default=DEFAULT_ROOT / "lineage-v1/transitions"
    )
    parser.add_argument(
        "--probe-root",
        type=Path,
        default=DEFAULT_ROOT / "sourcegraph-longitudinal-v3/probes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-batch-plan.v3.json"),
    )
    return parser


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LongitudinalExtractionError(f"cannot read {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise LongitudinalExtractionError(f"{path} must contain an object")
    return document


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    plan = build_batch_plan(
        _load(arguments.git_inventory),
        _load(arguments.cohort_inventory),
        _load(arguments.lineage_inventory),
        _load(arguments.index_manifest),
        transition_root=arguments.transition_root,
        probe_root=arguments.probe_root,
    )
    _atomic_write(arguments.output, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
