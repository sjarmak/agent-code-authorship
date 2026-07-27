"""Build the outcome-blind survival candidate frame from pinned AIDev tables."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import duckdb

from authorship.survival_candidates import frame_sha256, freeze_candidates
from authorship.github_reachability import apply_reachability


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_ids(path: Path) -> set[str]:
    document = json.loads(path.read_text())
    return {entry["id"] for entry in document["repositories"]}


def aidev_rows(raw: Path) -> list[dict]:
    connection = duckdb.connect()
    query = """
        SELECT
          r.full_name AS repository_id,
          'https://github.com/' || r.full_name AS repository_url,
          r.language,
          p.agent AS agent_family,
          2 AS provenance_tier,
          p.id AS pr_id,
          p.number AS pr_number,
          p.html_url AS pr_url,
          d.sha AS commit_sha,
          p.merged_at,
          d.filename,
          d.additions
        FROM read_parquet(?) p
        JOIN read_parquet(?) r ON p.repo_id = r.id
        JOIN read_parquet(?) d ON p.id = d.pr_id
        WHERE p.merged_at IS NOT NULL
          AND r.language IN ('Python', 'Go')
    """
    table = connection.execute(
        query,
        [
            str(raw / "pull_request.parquet"),
            str(raw / "repository.parquet"),
            str(raw / "pr_commit_details.parquet"),
        ],
    ).to_arrow_table()
    return table.to_pylist()


def build(root: Path, raw: Path, reachability: Path | None = None) -> dict:
    reference_document = json.loads(
        (root / "study" / "repositories.v2.json").read_text()
    )
    frame = freeze_candidates(
        aidev_rows(raw),
        target_ids=manifest_ids(root / "study" / "targets.v1.json"),
        reference_ids={entry["id"] for entry in reference_document["repositories"]},
        cap=25,
    )
    tier_one_reference_overlap = sorted(
        entry["id"]
        for entry in reference_document["repositories"]
        if entry.get("label") == "agent"
        and (entry.get("evidence") or {}).get("tier") == 1
    )
    frame["tier_1_audit"] = {
        "eligible_nonoverlapping_repositories": 0,
        "excluded_existing_reference_overlap": tier_one_reference_overlap,
        "interpretation": "All exact Tier 1 sources available at freeze time already serve as references in the parent study and are excluded by the locked role-overlap rule.",
    }
    frame["$schema"] = "survival-candidates.schema.json"
    frame["protocol_sha256"] = sha256(root / "study" / "survival-protocol.v1.json")
    frame["input_inventory_sha256"] = sha256(
        root / "study" / "survival-inputs.v1.json"
    )
    if reachability is not None:
        frame = apply_reachability(frame, json.loads(reachability.read_text()))
        frame["github_reachability_sha256"] = sha256(reachability)
    frame["frame_sha256"] = frame_sha256(frame)
    return frame


def atomic_write(path: Path, document: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/raw/aidev/"
            "68ed5f4b80d27a9e057fc57567f38bd322ac73ec"
        ),
    )
    parser.add_argument(
        "--output", type=Path, default=root / "study" / "survival-candidates.v1.json"
    )
    parser.add_argument(
        "--reachability",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/raw/github/"
            "survival-default-branch-facts.v1.json"
        ),
    )
    arguments = parser.parse_args()
    atomic_write(arguments.output, build(root, arguments.raw, arguments.reachability))
    print(arguments.output)


if __name__ == "__main__":
    main()
