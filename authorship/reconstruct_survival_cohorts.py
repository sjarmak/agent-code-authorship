"""Reconstruct merged, attributable line cohorts from pinned repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from authorship.survival_git import git
from authorship.survival_lines import (
    commit_parents_many,
    extract_added_lines,
    extract_first_parent_many,
)


def deduplicate_physical_lines(
    records: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Keep one attribution for an identical landed physical source line."""
    retained: dict[tuple, dict] = {}
    duplicates = []
    for record in sorted(
        records,
        key=lambda item: (
            item["repository_id"],
            item["merge_commit"],
            item["path"],
            item["line_number"],
            item["content_sha256"],
            item["pr_number"],
            item["pr_url"],
        ),
    ):
        key = (
            record["repository_id"],
            record["merge_commit"],
            record["path"],
            record["line_number"],
            record["content_sha256"],
        )
        if key in retained:
            duplicates.append(
                {
                    "merge_commit": record["merge_commit"],
                    "path": record["path"],
                    "line_number": record["line_number"],
                    "content_sha256": record["content_sha256"],
                    "retained_pr_number": retained[key]["pr_number"],
                    "excluded_pr_number": record["pr_number"],
                    "reason": "duplicate_physical_line_attribution",
                }
            )
        else:
            retained[key] = record
    return list(retained.values()), duplicates


def write_shard(path: Path, records: list[dict]) -> str:
    payload = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return hashlib.sha256(payload).hexdigest()


def reconstruct(candidate: dict, inventory: dict, output_root: Path) -> dict:
    repository = Path(inventory["cache_path"])
    cutoff = inventory["cutoff_commit"]
    reachable = set(inventory["reachable_attributed_commits"])
    first_parent = git(repository, "rev-list", "--first-parent", "--reverse", cutoff)
    positions = {
        commit: index for index, commit in enumerate(first_parent.splitlines())
    }
    records = []
    excluded_prs = []
    admitted = [
        pr for pr in candidate["pull_requests"] if pr["merge_commit"] in reachable
    ]
    parents = commit_parents_many(
        repository, [pr["merge_commit"] for pr in admitted]
    )
    bulk_prs = []
    individual_prs = []
    for pr in admitted:
        merge_commit = pr["merge_commit"]
        direct_parent = parents[merge_commit][0]
        rebased = sorted(
            (
                commit
                for commit in pr["commit_shas"]
                if commit in positions
            ),
            key=positions.__getitem__,
        )
        if len(parents[merge_commit]) == 1 and rebased:
            base = git(repository, "rev-parse", f"{rebased[0]}^")
        else:
            base = direct_parent
        pr["_diff_base"] = base
        (bulk_prs if base == direct_parent else individual_prs).append(pr)
    bulk_lines = extract_first_parent_many(
        repository,
        [pr["merge_commit"] for pr in bulk_prs],
        candidate["language"],
    )
    bulk_heads = {pr["merge_commit"] for pr in bulk_prs}
    for pr in candidate["pull_requests"]:
        merge_commit = pr["merge_commit"]
        if merge_commit not in reachable:
            excluded_prs.append(
                {
                    "number": pr["number"],
                    "merge_commit": merge_commit,
                    "reason": "merge_commit_not_reachable_at_cutoff",
                }
            )
            continue
        base = pr["_diff_base"]
        if merge_commit in bulk_heads:
            added_lines = bulk_lines.get(merge_commit, [])
        else:
            added_lines = extract_added_lines(
                repository, base, merge_commit, candidate["language"]
            )
        for line in added_lines:
            records.append(
                {
                    "repository_id": candidate["repository_id"],
                    "language": candidate["language"],
                    "agent_family": candidate["agent_family"],
                    "provenance_tier": candidate["provenance_tier"],
                    "pr_number": pr["number"],
                    "pr_url": pr["url"],
                    "merged_at": pr["merged_at"],
                    "merge_commit": merge_commit,
                    "diff_base": base,
                    **line,
                }
            )
    records, duplicate_lines = deduplicate_physical_lines(records)
    shard = output_root / f"{candidate['repository_id'].replace('/', '__')}.jsonl"
    digest = write_shard(shard, records)
    return {
        "repository_id": candidate["repository_id"],
        "status": "reconstructed",
        "cutoff_commit": cutoff,
        "shard_path": str(shard),
        "shard_sha256": digest,
        "line_count": len(records),
        "eligible_200_lines": len(records) >= 200,
        "excluded_prs": excluded_prs,
        "excluded_duplicate_lines": duplicate_lines,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--git-inventory",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/git-inventory.v1.json"
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("/mnt/agent-code-authorship/survival-study/cohort-shards-v1"),
    )
    parser.add_argument("--workers", type=int, default=8)
    arguments = parser.parse_args()
    frame = json.loads(arguments.frame.read_text())
    inventory = json.loads(arguments.git_inventory.read_text())
    by_repository = {
        item["repository_id"]: item for item in inventory["repositories"]
    }
    records = []
    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {
            executor.submit(
                reconstruct,
                candidate,
                by_repository[candidate["repository_id"]],
                arguments.output_root,
            ): candidate["repository_id"]
            for candidate in frame["candidates"]
        }
        for index, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            print(
                f"{index}/{len(futures)} {record['repository_id']} "
                f"{record['line_count']} lines",
                flush=True,
            )
    records.sort(key=lambda item: item["repository_id"])
    manifest = {
        "cohort_version": 1,
        "candidate_frame_sha256": frame["frame_sha256"],
        "git_inventory_sha256": hashlib.sha256(
            arguments.git_inventory.read_bytes()
        ).hexdigest(),
        "outcomes_consulted": False,
        "repositories": records,
    }
    output = arguments.output_root.parent / "cohort-inventory.v1.json"
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
