"""Resumable terminal review for the blinded structural-lineage sample."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def progress(document: dict[str, Any]) -> tuple[int, int]:
    completed = sum(
        case["is_same_lineage"] is not None for case in document["cases"]
    )
    return completed, len(document["cases"])


def apply_decision(
    case: dict[str, Any], command: str
) -> bool:
    normalized = command.strip().lower()
    if normalized in {"y", "yes"}:
        case["is_same_lineage"] = True
        return True
    if normalized in {"n", "no"}:
        case["is_same_lineage"] = False
        return True
    return False


def save(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("study/lineage-adjudication.v1.json"),
    )
    parser.add_argument("--status", action="store_true")
    arguments = parser.parse_args()
    document = json.loads(arguments.path.read_text())
    completed, total = progress(document)
    if arguments.status:
        print(f"{completed}/{total} completed")
        return
    index = next(
        (
            position
            for position, case in enumerate(document["cases"])
            if case["is_same_lineage"] is None
        ),
        total,
    )
    while index < total:
        case = document["cases"][index]
        completed, _ = progress(document)
        print(f"\nCase {index + 1}/{total} ({completed} completed)")
        print(f"Language: {case['language']}  Relation: {case['path_relation']}")
        print(f"BEFORE: {case['prior_text']}")
        print(f"AFTER:  {case['candidate_text']}")
        command = input(
            "Same logical line? [y]es/[n]o/[s]kip/[b]ack/[q]uit: "
        ).strip().lower()
        if command in {"q", "quit"}:
            break
        if command in {"b", "back"}:
            index = max(0, index - 1)
            continue
        if command in {"s", "skip"}:
            index += 1
            continue
        if apply_decision(case, command):
            save(arguments.path, document)
            index += 1
            continue
        print("Unrecognized command.")
    completed, total = progress(document)
    print(f"\nSaved progress: {completed}/{total} completed")


if __name__ == "__main__":
    main()
