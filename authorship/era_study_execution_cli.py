"""CLI for executing all materialized Sourcegraph era feature panels."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from authorship.era_study_execution import estimate_feature_panels
from authorship.sourcegraph_discovery_execution import atomic_write_json


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def execute_file(
    source: Path,
    output: Path,
    *,
    bootstrap_replicates: int,
    seed: int,
) -> dict[str, Any]:
    """Execute and atomically write one complete era study artifact."""
    document = estimate_feature_panels(
        _load(source),
        bootstrap_replicates=bootstrap_replicates,
        seed=seed,
    )
    atomic_write_json(output, document)
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="era-study-execution")
    parser.add_argument("--materialization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260729)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    document = execute_file(
        arguments.materialization,
        arguments.output,
        bootstrap_replicates=arguments.bootstrap_replicates,
        seed=arguments.seed,
    )
    print(json.dumps({"sha256": document["era_study_execution_sha256"]}))


if __name__ == "__main__":
    main()
