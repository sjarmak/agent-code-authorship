"""CLI for freezing and executing the Sourcegraph prevalence analysis."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery_execution import atomic_write_json
from authorship.sourcegraph_prevalence_execution import run_prevalence_analysis
from authorship.sourcegraph_prevalence_spec import build_prevalence_analysis_spec


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def freeze_file(
    protocol_path: Path,
    feature_path: Path,
    target_plan_path: Path,
    authorship_path: Path,
    era_path: Path,
    cohort_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    specification = build_prevalence_analysis_spec(
        _load(protocol_path),
        _load(feature_path),
        _load(target_plan_path),
        _load(authorship_path),
        _load(era_path),
        _load(cohort_path),
    )
    atomic_write_json(output_path, specification)
    return specification


def execute_file(
    specification_path: Path,
    target_plan_path: Path,
    authorship_path: Path,
    target_path: Path,
    era_path: Path,
    cohort_path: Path,
    feature_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    execution = run_prevalence_analysis(
        _load(specification_path),
        _load(target_plan_path),
        _load(authorship_path),
        _load(target_path),
        _load(era_path),
        _load(cohort_path),
        _load(feature_path),
    )
    atomic_write_json(output_path, execution)
    return execution


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sourcegraph-prevalence")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--protocol", type=Path, required=True)
    freeze.add_argument("--features", type=Path, required=True)
    freeze.add_argument("--target-plan", type=Path, required=True)
    freeze.add_argument("--authorship", type=Path, required=True)
    freeze.add_argument("--era", type=Path, required=True)
    freeze.add_argument("--cohort", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    for name in (
        "specification",
        "target-plan",
        "authorship",
        "target",
        "era",
        "cohort",
        "features",
    ):
        execute.add_argument(f"--{name}", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "freeze":
        document = freeze_file(
            arguments.protocol,
            arguments.features,
            arguments.target_plan,
            arguments.authorship,
            arguments.era,
            arguments.cohort,
            arguments.output,
        )
        field = "prevalence_analysis_spec_sha256"
    else:
        document = execute_file(
            arguments.specification,
            arguments.target_plan,
            arguments.authorship,
            arguments.target,
            arguments.era,
            arguments.cohort,
            arguments.features,
            arguments.output,
        )
        field = "prevalence_execution_sha256"
    print(json.dumps({"sha256": document[field]}))


if __name__ == "__main__":
    main()
