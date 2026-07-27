"""Resolve the frozen target cohort to immutable GitHub snapshots."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


CUTOFF = "2026-07-24T23:59:59Z"


class TargetManifestError(RuntimeError):
    """Raised when a target snapshot cannot be resolved safely."""


def resolve_github_snapshot(full_name: str, cutoff: str = CUTOFF) -> dict:
    if (
        full_name.count("/") != 1
        or any(part in ("", ".", "..") for part in full_name.split("/"))
        or not all(c.isalnum() or c in "._-/" for c in full_name)
    ):
        raise TargetManifestError(f"unsafe GitHub repository name: {full_name}")
    endpoint = f"repos/{full_name}/commits?until={cutoff}&per_page=1"
    completed = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise TargetManifestError(completed.stderr.strip() or "GitHub API failed")
    rows = json.loads(completed.stdout)
    if not rows:
        raise TargetManifestError("no commit exists at or before cutoff")
    row = rows[0]
    return {
        "commit": row["sha"],
        "committed_at": row["commit"]["committer"]["date"],
        "tree": row["commit"]["tree"]["sha"],
    }


def build_target_manifest(cohort: list[dict], resolver=resolve_github_snapshot) -> dict:
    repositories, unresolved = [], []
    for source in cohort:
        full_name = source["full_name"]
        try:
            snapshot = resolver(full_name, CUTOFF)
        except (TargetManifestError, KeyError, json.JSONDecodeError) as error:
            unresolved.append({"id": full_name, "reason": str(error)})
            continue
        repositories.append(
            {
                "id": full_name,
                "url": f"https://github.com/{full_name}",
                "role": "target",
                "label": "unlabeled",
                "languages": ["Python", "Go"],
                "snapshot": snapshot,
                "effective_date_range": ["2024-01-01T00:00:00Z", CUTOFF],
                "excluded_paths": [
                    "vendored",
                    "generated",
                    "minified",
                    "fixture_snapshots",
                    "lockfiles",
                    "machine_translated",
                ],
                "content_checksum": f"git-tree-sha1:{snapshot['tree']}",
            }
        )
    return {
        "manifest_version": 1,
        "cutoff": CUTOFF,
        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "data/cohort_ages.json",
        "repositories": repositories,
        "unresolved": unresolved,
    }


def write_manifest(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    cohort = json.loads((root / "data" / "cohort_ages.json").read_text())
    destination = root / "study" / "targets.v1.json"
    write_manifest(destination, build_target_manifest(cohort))
    print(destination)


if __name__ == "__main__":
    main()
