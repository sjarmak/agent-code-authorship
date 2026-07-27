"""Rebuild a lineage inventory from atomically completed transition shards."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from authorship.build_survival_transitions import HORIZONS


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_repository(
    cohort: dict[str, Any], output_root: Path
) -> dict[str, Any]:
    repository_id = cohort["repository_id"]
    stem = repository_id.replace("/", "__")
    transition_path = output_root / "transitions" / f"{stem}.jsonl"
    event_path = output_root / "structural-events" / f"{stem}.jsonl"
    counts: Counter[tuple[int, str]] = Counter()
    line_ids: set[str] = set()
    transition_count = 0
    with transition_path.open() as stream:
        for raw in stream:
            row = json.loads(raw)
            counts[(row["horizon_days"], row["state"])] += 1
            line_ids.add(row["line_id"])
            transition_count += 1
    expected = cohort["line_count"] * len(HORIZONS)
    if transition_count != expected or len(line_ids) != cohort["line_count"]:
        raise ValueError(
            f"{repository_id}: expected {expected} transitions for "
            f"{cohort['line_count']} lines, found {transition_count} "
            f"transitions for {len(line_ids)} lines"
        )
    structural_counts: Counter[str] = Counter()
    event_count = 0
    with event_path.open() as stream:
        for raw in stream:
            event = json.loads(raw)
            structural_counts[event["decision"]] += 1
            event_count += 1
    observed = sum(
        count
        for (_horizon, state), count in counts.items()
        if state != "right_censored"
    )
    unobservable = sum(
        count
        for (_horizon, state), count in counts.items()
        if state == "unobservable"
    )
    return {
        "repository_id": repository_id,
        "line_count": cohort["line_count"],
        "transition_count": transition_count,
        "transition_sha256": _sha256_file(transition_path),
        "structural_event_count": event_count,
        "structural_event_sha256": _sha256_file(event_path),
        "counts": {
            str(horizon): {
                state: counts[(horizon, state)]
                for state in (
                    "unchanged",
                    "modified_candidate",
                    "deleted",
                    "unobservable",
                    "right_censored",
                )
                if counts[(horizon, state)]
            }
            for horizon in HORIZONS
        },
        "lineage_coverage": (
            round((observed - unobservable) / observed, 6) if observed else None
        ),
        "structural_counts": dict(sorted(structural_counts.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frame", type=Path, required=True)
    parser.add_argument("--cohort-inventory", type=Path, required=True)
    parser.add_argument("--git-inventory", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    frame = json.loads(arguments.frame.read_text())
    cohorts = json.loads(arguments.cohort_inventory.read_text())
    eligible = [
        cohort
        for cohort in cohorts["repositories"]
        if cohort["eligible_200_lines"]
    ]
    records = [
        summarize_repository(cohort, arguments.output_root)
        for cohort in eligible
    ]
    records.sort(key=lambda record: record["repository_id"])
    manifest = {
        "lineage_version": 1,
        "candidate_frame_sha256": frame["frame_sha256"],
        "cohort_inventory_sha256": hashlib.sha256(
            arguments.cohort_inventory.read_bytes()
        ).hexdigest(),
        "git_inventory_sha256": hashlib.sha256(
            arguments.git_inventory.read_bytes()
        ).hexdigest(),
        "horizons_days": list(HORIZONS),
        "structural_threshold": 0.8,
        "structural_margin": 0.1,
        "structural_status": "pending_blinded_validation",
        "repositories": records,
    }
    output = arguments.output_root / "lineage-inventory.v1.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
