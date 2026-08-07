"""CLI for offline materialization of frozen longitudinal probe results."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_execution import (
    _atomic_write,
    execute_longitudinal_unit,
)
from authorship.sourcegraph_longitudinal_materialization import (
    UnitExecutor,
    execute_materialization_plan,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-batch-plan.v3.json"),
    )
    parser.add_argument(
        "--probe-execution",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-probe-execution.v3.json"),
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("study/protocol.v3.json"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "sourcegraph-longitudinal-v3/materialized"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-materialization.v3.json"),
    )
    return parser


def _load(path: Path, name: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LongitudinalExtractionError(f"cannot read {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise LongitudinalExtractionError(f"{name} must be an object")
    return document


def main(
    argv: Sequence[str] | None = None,
    *,
    unit_executor: UnitExecutor = execute_longitudinal_unit,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    arguments = _parser().parse_args(argv)
    result = execute_materialization_plan(
        _load(arguments.plan, "batch plan"),
        _load(arguments.probe_execution, "probe execution"),
        _load(arguments.protocol, "protocol"),
        arguments.output_directory,
        unit_executor=unit_executor,
        clock=clock,
    )
    _atomic_write(arguments.output, result)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
