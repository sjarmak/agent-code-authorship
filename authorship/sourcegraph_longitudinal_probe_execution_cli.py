"""CLI for executing the frozen longitudinal Sourcegraph probe plan."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.sg import api, check_auth
from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_execution import _atomic_write
from authorship.sourcegraph_longitudinal_probe_execution import execute_probe_plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-batch-plan.v3.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("study/sourcegraph-longitudinal-probe-execution.v3.json"),
    )
    return parser


def _load(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LongitudinalExtractionError(f"cannot read {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise LongitudinalExtractionError("batch plan must be an object")
    return document


def main(
    argv: Sequence[str] | None = None,
    *,
    api_runner: Callable[..., Mapping[str, Any]] = api,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    arguments = _parser().parse_args(argv)
    if api_runner is api:
        check_auth()
    result = execute_probe_plan(
        _load(arguments.plan), api_runner=api_runner, clock=clock
    )
    _atomic_write(arguments.output, result)
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
