"""Freeze a deterministic, blinded structural-lineage adjudication sample."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def select_cases(
    events: Iterable[dict[str, Any]], *, per_class: int = 100
) -> list[dict[str, Any]]:
    eligible = [
        event
        for event in events
        if event["decision"] in {"modified_candidate", "unobservable"}
        and event.get("candidate_text") is not None
    ]
    chosen: list[dict[str, Any]] = []
    used_lines: set[str] = set()
    for selection_class, decision in (
        ("accepted", "modified_candidate"),
        ("rejected", "unobservable"),
    ):
        count = 0
        for event in sorted(
            (item for item in eligible if item["decision"] == decision),
            key=lambda item: item["case_key"],
        ):
            if event["line_id"] in used_lines:
                continue
            chosen.append({**event, "selection_class": selection_class})
            used_lines.add(event["line_id"])
            count += 1
            if count == per_class:
                break
        if count != per_class:
            raise ValueError(
                f"needed {per_class} {selection_class} cases, found {count}"
            )
    return sorted(chosen, key=lambda item: item["case_key"])


def blinded_record(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": event["case_key"],
        "language": event["language"],
        "prior_text": event["prior_text"],
        "candidate_text": event["candidate_text"],
        "path_relation": (
            "same_file"
            if event.get("candidate_path") == event.get("original_path")
            else "cross_file"
        ),
        "is_same_lineage": None,
        "reviewer_confidence": None,
        "notes": None,
    }


def _atomic_json(destination: Path, document: Any) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def freeze(
    event_root: Path,
    public_path: Path,
    private_path: Path,
    *,
    lineage_inventory_sha256: str,
) -> dict[str, str]:
    events = (
        json.loads(raw)
        for path in sorted(event_root.glob("*.jsonl"))
        for raw in path.read_text().splitlines()
        if raw
    )
    selected = select_cases(events)
    private_document = {
        "artifact": "lineage-adjudication-private",
        "version": 1,
        "lineage_inventory_sha256": lineage_inventory_sha256,
        "cases": selected,
    }
    private_sha256 = _atomic_json(private_path, private_document)
    public_document = {
        "artifact": "lineage-adjudication",
        "version": 1,
        "status": "awaiting_blinded_review",
        "lineage_inventory_sha256": lineage_inventory_sha256,
        "private_mapping_sha256": private_sha256,
        "selection": {
            "accepted": 100,
            "rejected": 100,
            "order": "case_key_ascending_with_unique_line_id",
        },
        "cases": [blinded_record(event) for event in selected],
    }
    public_sha256 = _atomic_json(public_path, public_document)
    return {"public_sha256": public_sha256, "private_sha256": private_sha256}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--event-root",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/lineage-v1/structural-events"
        ),
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/lineage-v1/"
            "lineage-inventory.v1.json"
        ),
    )
    parser.add_argument(
        "--public",
        type=Path,
        default=Path("study/lineage-adjudication.v1.json"),
    )
    parser.add_argument(
        "--private",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "lineage-adjudication-private.v1.json"
        ),
    )
    arguments = parser.parse_args()
    digest = hashlib.sha256(arguments.inventory.read_bytes()).hexdigest()
    print(
        json.dumps(
            freeze(
                arguments.event_root,
                arguments.public,
                arguments.private,
                lineage_inventory_sha256=digest,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
