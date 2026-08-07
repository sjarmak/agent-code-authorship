"""CLI for verified stratified censoring-correct survival estimates."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.survival_estimate_execution import execute_survival_estimates


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260724)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    artifact = execute_survival_estimates(
        event_root=arguments.event_root,
        output_path=arguments.output,
        bootstrap_replicates=arguments.bootstrap_replicates,
        seed=arguments.seed,
    )
    print(
        json.dumps(
            {
                "counts": artifact["counts"],
                "survival_estimate_artifact_sha256": artifact[
                    "survival_estimate_artifact_sha256"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
