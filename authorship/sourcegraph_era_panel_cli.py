"""CLI for planning, executing, and materializing Sourcegraph era panels."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery_execution import atomic_write_json
from authorship.sourcegraph_era_execution import (
    execute_era_panel_plan,
    load_complete_period_results,
)
from authorship.sourcegraph_era_panel import (
    build_era_panel_plan,
    build_feature_panels,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sourcegraph-era-panel")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--freeze", type=Path, required=True)
    plan.add_argument("--adoption", type=Path, required=True)
    plan.add_argument("--index", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--output-directory", type=Path, required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--plan", type=Path, required=True)
    materialize.add_argument("--execution", type=Path, required=True)
    materialize.add_argument("--execution-root", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)
    return parser


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def _plan(arguments: argparse.Namespace) -> Mapping[str, Any]:
    document = build_era_panel_plan(
        _load(arguments.freeze),
        _load(arguments.adoption),
        _load(arguments.index),
    )
    atomic_write_json(arguments.output, document)
    return document


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _execute(arguments: argparse.Namespace) -> Mapping[str, Any]:
    return execute_era_panel_plan(
        _load(arguments.plan),
        arguments.output_directory,
        progress=_progress,
    )


def _materialize(arguments: argparse.Namespace) -> Mapping[str, Any]:
    plan = _load(arguments.plan)
    periods = load_complete_period_results(
        _load(arguments.execution),
        arguments.execution_root,
        plan["era_panel_plan_sha256"],
    )
    document = build_feature_panels(plan, periods)
    atomic_write_json(arguments.output, document)
    return document


def main() -> None:
    arguments = _parser().parse_args()
    commands = {
        "plan": _plan,
        "execute": _execute,
        "materialize": _materialize,
    }
    document = commands[arguments.command](arguments)
    digest_key = {
        "plan": "era_panel_plan_sha256",
        "execute": "era_panel_execution_sha256",
        "materialize": "era_panel_materialization_sha256",
    }[arguments.command]
    digest = document.get(digest_key)
    print(json.dumps({"command": arguments.command, "sha256": digest}))


if __name__ == "__main__":
    main()
