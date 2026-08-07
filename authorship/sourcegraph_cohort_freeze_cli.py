"""CLI for the outcome-blind Sourcegraph longitudinal cohort freeze."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from authorship.sourcegraph_cohort_freeze import CohortFreezeError
from authorship.sourcegraph_cohort_io import materialize_cohort_freeze
from authorship.sourcegraph_cohort_validation import EXPECTED_INPUTS


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for input_id in sorted(EXPECTED_INPUTS):
        parser.add_argument(f"--{input_id.replace('_', '-')}", type=Path, required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--discovery-start", default="2023-01-01T00:00:00Z")
    parser.add_argument("--simulation-replicates", type=int, default=20_000)
    parser.add_argument("--simulation-seed", type=int, default=20260729)
    return parser.parse_args()


def _pins(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortFreezeError(f"cannot read independent pins: {error}") from error
    if not isinstance(document, dict):
        raise CohortFreezeError("independent pins must be a JSON object")
    return document


def main() -> None:
    arguments = _arguments()
    paths = {
        input_id: getattr(arguments, input_id) for input_id in sorted(EXPECTED_INPUTS)
    }
    artifact = materialize_cohort_freeze(
        paths,
        _pins(arguments.pins),
        output_path=arguments.output,
        discovery_start=arguments.discovery_start,
        simulation_replicates=arguments.simulation_replicates,
        simulation_seed=arguments.simulation_seed,
    )
    primary = artifact["cohorts"]["adoption_event_study"]["primary"]["languages"]
    print(
        json.dumps(
            {
                "cohort_freeze_sha256": artifact["cohort_freeze_sha256"],
                "primary": {
                    language: {
                        "status": row["status"],
                        "adopter_count": row["adopter_count"],
                    }
                    for language, row in primary.items()
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
