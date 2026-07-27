"""Filter agent lineage artifacts to targets with contextual matches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from authorship.reconstruct_survival_cohorts import write_shard


def matched_target_keys(audit: dict[str, Any]) -> set[tuple[str, int]]:
    return {
        (target["source_commit"], target["pr_number"])
        for target in audit["targets"]
        if target["status"] == "matched"
    }


def _filter_jsonl(
    source: Path,
    destination: Path,
    *,
    include: callable,
) -> tuple[int, str, set[str]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    digest = hashlib.sha256()
    count = 0
    line_ids: set[str] = set()
    try:
        with source.open() as input_stream, os.fdopen(descriptor, "wb") as output:
            for raw in input_stream:
                record = json.loads(raw)
                if not include(record):
                    continue
                payload = (
                    json.dumps(record, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode()
                output.write(payload)
                digest.update(payload)
                line_ids.add(record["line_id"])
                count += 1
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return count, digest.hexdigest(), line_ids


def filter_repository(
    repository_id: str,
    audit: dict[str, Any],
    source_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    keys = matched_target_keys(audit)
    stem = repository_id.replace("/", "__")
    transition_count, transition_sha256, line_ids = _filter_jsonl(
        source_root / "transitions" / f"{stem}.jsonl",
        output_root / "transitions" / f"{stem}.jsonl",
        include=lambda row: (row["merge_commit"], row["pr_number"]) in keys,
    )
    event_count, event_sha256, _event_lines = _filter_jsonl(
        source_root / "structural-events" / f"{stem}.jsonl",
        output_root / "structural-events" / f"{stem}.jsonl",
        include=lambda event: event["line_id"] in line_ids,
    )
    if transition_count != len(line_ids) * 4:
        raise ValueError(
            f"{repository_id}: {transition_count} transitions for "
            f"{len(line_ids)} matched lines"
        )
    return {
        "repository_id": repository_id,
        "line_count": len(line_ids),
        "transition_count": transition_count,
        "transition_sha256": transition_sha256,
        "structural_event_count": event_count,
        "structural_event_sha256": event_sha256,
        "matched_target_count": len(keys),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--context-inventory",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/contextual-v1/"
            "cohort-inventory.v1.json"
        ),
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/lineage-v1"
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/contextual-v1/"
            "matched-agent-lineage-v1"
        ),
    )
    arguments = parser.parse_args()
    context = json.loads(arguments.context_inventory.read_text())
    records = []
    target_manifest = []
    for repository in context["repositories"]:
        if not repository["eligible_200_lines"]:
            continue
        audit = json.loads(Path(repository["match_audit_path"]).read_text())
        keys = matched_target_keys(audit)
        record = filter_repository(
            repository["repository_id"],
            audit,
            arguments.source_root,
            arguments.output_root,
        )
        records.append(record)
        target_manifest.append(
            {
                "repository_id": repository["repository_id"],
                "targets": [
                    {"source_commit": commit, "pr_number": number}
                    for commit, number in sorted(keys)
                ],
            }
        )
        print(
            f"{len(records)} {record['repository_id']} "
            f"{record['line_count']} lines",
            flush=True,
        )
    records.sort(key=lambda item: item["repository_id"])
    target_manifest.sort(key=lambda item: item["repository_id"])
    target_path = arguments.output_root / "matched-targets.v1.json"
    target_sha256 = write_shard(
        target_path,
        [
            {
                "artifact": "matched-agent-targets",
                "version": 1,
                "repositories": target_manifest,
            }
        ],
    )
    cohort_inventory = {
        "artifact": "matched-agent-cohort-inventory",
        "version": 1,
        "context_inventory_sha256": hashlib.sha256(
            arguments.context_inventory.read_bytes()
        ).hexdigest(),
        "matched_targets_sha256": target_sha256,
        "repositories": [
            {
                **record,
                "eligible_200_lines": True,
            }
            for record in records
        ],
    }
    output = arguments.output_root / "cohort-inventory.v1.json"
    output.write_text(json.dumps(cohort_inventory, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
