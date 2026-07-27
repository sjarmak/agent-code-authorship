"""Pin candidate Git histories and verify cutoff ancestry in parallel."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from authorship.survival_git import (
    SurvivalGitError,
    clone_or_fetch,
    inspect_repository,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pin(candidate: dict, cache: Path, bundles: Path) -> dict:
    repository_id = candidate["repository_id"]
    stem = repository_id.replace("/", "__")
    destination = cache / stem
    bundle = bundles / f"{stem}.bundle"
    try:
        clone_or_fetch(candidate["repository_url"], destination)
        inspection = inspect_repository(
            destination,
            default_branch=candidate["default_branch"],
            cutoff="2026-07-24T23:59:59Z",
            attributed_commits=[
                pr["merge_commit"] for pr in candidate["pull_requests"]
            ],
        )
        bundles.mkdir(parents=True, exist_ok=True)
        verify = subprocess.run(
            ["git", "bundle", "verify", str(bundle)],
            capture_output=True,
            text=True,
            check=False,
            cwd=destination,
        )
        if verify.returncode:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={destination.resolve()}",
                    "-C",
                    str(destination),
                    "bundle",
                    "create",
                    str(bundle),
                    "--all",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise SurvivalGitError(result.stderr.strip() or "git bundle failed")
        return {
            "repository_id": repository_id,
            "status": "pinned",
            "cache_path": str(destination),
            "bundle_path": str(bundle),
            "bundle_sha256": sha256(bundle),
            **inspection,
        }
    except (OSError, SurvivalGitError) as error:
        return {
            "repository_id": repository_id,
            "status": "retrieval_failed",
            "error": str(error)[:1000],
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/mnt/agent-code-authorship/survival-study"),
    )
    parser.add_argument("--workers", type=int, default=6)
    arguments = parser.parse_args()
    frame = json.loads(arguments.frame.read_text())
    cache = arguments.root / "repositories"
    bundles = arguments.root / "git-bundles"
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {
            executor.submit(pin, candidate, cache, bundles): candidate["repository_id"]
            for candidate in frame["candidates"]
        }
        records = []
        for index, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            print(
                f"{index}/{len(futures)} {record['repository_id']} {record['status']}",
                flush=True,
            )
    records.sort(key=lambda item: item["repository_id"])
    output = arguments.root / "git-inventory.v1.json"
    output.write_text(
        json.dumps(
            {
                "inventory_version": 1,
                "candidate_frame_sha256": frame["frame_sha256"],
                "cutoff": "2026-07-24T23:59:59Z",
                "outcomes_consulted": False,
                "repositories": records,
            },
            indent=2,
        )
        + "\n"
    )
    print(output)


if __name__ == "__main__":
    main()
