"""CLI for Sourcegraph-derived current-snapshot prevalence target units."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_target_execution import (
    atomic_write_json,
    execute_target_unit_plan,
    fetch_target_file,
    fetch_tree_files,
    validate_target_execution,
)
from authorship.sourcegraph_target_materialization import (
    materialize_target_units,
    validate_target_materialization,
)
from authorship.sourcegraph_target_plan import (
    build_target_unit_plan,
    target_unit_plan_sha256,
    validate_target_unit_plan,
)

CANONICAL_PYTHON = (3, 12, 3)


def _require_runtime(
    version_info: Sequence[int] | None = None,
    implementation: str | None = None,
) -> None:
    current = tuple((version_info or sys.version_info)[:3])
    runtime = implementation or platform.python_implementation()
    if runtime != "CPython" or current != CANONICAL_PYTHON:
        required = ".".join(str(value) for value in CANONICAL_PYTHON)
        actual = ".".join(str(value) for value in current)
        raise SystemExit(
            f"target-unit artifacts require CPython {required}; "
            f"current runtime is {runtime} {actual}"
        )


def _load(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {label} {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{label} must be a JSON object")
    return document


def _inputs(args: argparse.Namespace) -> tuple[Mapping[str, Any], ...]:
    return (
        _load(args.targets, "target manifest"),
        _load(args.index_manifest, "Sourcegraph index manifest"),
        _load(args.features, "feature manifest"),
    )


def _validate_plan(
    plan: Mapping[str, Any], inputs: tuple[Mapping[str, Any], ...]
) -> None:
    errors = validate_target_unit_plan(plan, *inputs, fetch_tree_files)
    if errors:
        raise SystemExit("invalid target unit plan: " + "; ".join(errors))


def _plan(args: argparse.Namespace) -> Mapping[str, Any]:
    inputs = _inputs(args)
    document = build_target_unit_plan(*inputs, fetch_tree_files)
    if document.get("target_unit_plan_sha256") != target_unit_plan_sha256(document):
        raise SystemExit("generated target unit plan SHA-256 does not match")
    atomic_write_json(args.output, document)
    return document


def _execute(args: argparse.Namespace) -> Mapping[str, Any]:
    inputs = _inputs(args)
    plan = _load(args.plan, "target unit plan")
    _validate_plan(plan, inputs)
    document = execute_target_unit_plan(
        plan,
        args.output_root,
        fetch_target_file,
        workers=args.workers,
    )
    errors = validate_target_execution(plan, document, args.output_root)
    if errors:
        raise SystemExit("invalid target execution: " + "; ".join(errors))
    atomic_write_json(args.manifest, document)
    return document


def _materialize(args: argparse.Namespace) -> Mapping[str, Any]:
    inputs = _inputs(args)
    plan = _load(args.plan, "target unit plan")
    execution = _load(args.execution, "target execution")
    _validate_plan(plan, inputs)
    document = materialize_target_units(plan, execution, args.output_root)
    errors = validate_target_materialization(
        document, plan, execution, args.output_root
    )
    if errors:
        raise SystemExit("invalid target materialization: " + "; ".join(errors))
    atomic_write_json(args.output, document)
    return document


def _input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--index-manifest", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    _input_arguments(plan)
    plan.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    _input_arguments(execute)
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--output-root", type=Path, required=True)
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--workers", type=int, default=8)
    materialize = commands.add_parser("materialize")
    _input_arguments(materialize)
    materialize.add_argument("--plan", type=Path, required=True)
    materialize.add_argument("--execution", type=Path, required=True)
    materialize.add_argument("--output-root", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _require_runtime()
    args = _parser().parse_args(argv)
    document = {
        "plan": _plan,
        "execute": _execute,
        "materialize": _materialize,
    }[
        args.command
    ](args)
    output = getattr(args, "output", None) or getattr(args, "manifest", None)
    print(
        json.dumps(
            {
                "command": args.command,
                "output": str(output),
                "status": document.get("status"),
                "repository_count": document.get("repository_count"),
                "unit_count": document.get("unit_count"),
            },
            sort_keys=True,
        )
    )
    successful = {
        "plan": "frozen_before_target_outcome_extraction",
        "execute": "complete",
        "materialize": "complete",
    }
    return 0 if document.get("status") == successful[args.command] else 2


if __name__ == "__main__":
    raise SystemExit(main())
