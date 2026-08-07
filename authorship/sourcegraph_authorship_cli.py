"""CLI for the exact Sourcegraph authorship-unit workflow."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_authorship_candidates import (
    build_candidate_manifest,
    validate_candidate_manifest,
)
from authorship.sourcegraph_authorship_execution import (
    AuthorshipExecutionError,
    execute_authorship_units,
    load_execution_shards,
    validate_execution_manifest,
)
from authorship.sourcegraph_authorship_exact import fetch_window_commits
from authorship.sourcegraph_authorship_materialization import (
    materialize_authorship_units,
    validate_authorship_materialization,
)
from authorship.sourcegraph_authorship_plan import (
    build_authorship_unit_plan,
    validate_authorship_unit_plan,
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
            f"exact authorship artifacts require CPython {required}; "
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


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _plan(args: argparse.Namespace) -> Mapping[str, Any]:
    targets = _load(args.targets, "target manifest")
    document = build_authorship_unit_plan(
        _load(args.protocol, "protocol"),
        _load(args.freeze, "cohort freeze"),
        _load(args.agent_catalog, "agent catalog"),
        targets,
        _load(args.index_manifest, "Sourcegraph index manifest"),
    )
    errors = validate_authorship_unit_plan(document, targets)
    if errors:
        raise SystemExit("invalid generated plan: " + "; ".join(errors))
    _atomic_json(args.output, document)
    return document


def _discover(args: argparse.Namespace) -> Mapping[str, Any]:
    plan = _load(args.plan, "authorship unit plan")
    targets = _load(args.targets, "target manifest")
    errors = validate_authorship_unit_plan(plan, targets)
    if errors:
        raise SystemExit("invalid authorship unit plan: " + "; ".join(errors))
    document = build_candidate_manifest(plan)
    errors = validate_candidate_manifest(
        document, plan, commit_fetcher=fetch_window_commits
    )
    if errors:
        raise SystemExit("invalid generated candidate manifest: " + "; ".join(errors))
    _atomic_json(args.output, document)
    return document


def _execute(args: argparse.Namespace) -> Mapping[str, Any]:
    plan = _load(args.plan, "authorship unit plan")
    candidates = _load(args.candidates, "candidate manifest")
    targets = _load(args.targets, "target manifest")
    plan_errors = validate_authorship_unit_plan(plan, targets)
    candidate_errors = validate_candidate_manifest(
        candidates, plan, commit_fetcher=fetch_window_commits
    )
    if plan_errors or candidate_errors:
        raise SystemExit(
            "invalid execution inputs: " + "; ".join([*plan_errors, *candidate_errors])
        )
    document = execute_authorship_units(
        plan,
        candidates,
        args.output_root,
        max_workers=args.workers,
    )
    errors = validate_execution_manifest(document, plan, candidates, args.output_root)
    if errors:
        raise SystemExit("invalid generated execution: " + "; ".join(errors))
    _atomic_json(args.manifest, document)
    return document


def _materialize(args: argparse.Namespace) -> Mapping[str, Any]:
    plan = _load(args.plan, "authorship unit plan")
    candidates = _load(args.candidates, "candidate manifest")
    execution = _load(args.execution, "authorship execution")
    targets = _load(args.targets, "target manifest")
    input_errors = [
        *validate_authorship_unit_plan(plan, targets),
        *validate_candidate_manifest(
            candidates, plan, commit_fetcher=fetch_window_commits
        ),
    ]
    if input_errors:
        raise SystemExit("invalid materialization inputs: " + "; ".join(input_errors))
    try:
        shards = load_execution_shards(execution, plan, candidates, args.output_root)
    except AuthorshipExecutionError as error:
        raise SystemExit(str(error)) from error
    document = materialize_authorship_units(plan, execution, shards)
    errors = validate_authorship_materialization(document, plan, execution)
    if errors:
        raise SystemExit("invalid generated materialization: " + "; ".join(errors))
    _atomic_json(args.output, document)
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--protocol", type=Path, required=True)
    plan.add_argument("--freeze", type=Path, required=True)
    plan.add_argument("--agent-catalog", type=Path, required=True)
    plan.add_argument("--targets", type=Path, required=True)
    plan.add_argument("--index-manifest", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--plan", type=Path, required=True)
    discover.add_argument("--targets", type=Path, required=True)
    discover.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--targets", type=Path, required=True)
    execute.add_argument("--candidates", type=Path, required=True)
    execute.add_argument("--output-root", type=Path, required=True)
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--workers", type=int, default=8)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--plan", type=Path, required=True)
    materialize.add_argument("--targets", type=Path, required=True)
    materialize.add_argument("--candidates", type=Path, required=True)
    materialize.add_argument("--execution", type=Path, required=True)
    materialize.add_argument("--output-root", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _require_runtime()
    args = _parser().parse_args(argv)
    actions = {
        "plan": _plan,
        "discover": _discover,
        "execute": _execute,
        "materialize": _materialize,
    }
    document = actions[args.command](args)
    output = getattr(args, "output", None) or getattr(args, "manifest", None)
    print(
        json.dumps(
            {
                "command": args.command,
                "output": str(output),
                "counts": document.get("counts"),
            },
            sort_keys=True,
        )
    )
    return 0 if document.get("status") != "incomplete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
