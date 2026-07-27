"""Build an outcome-blind matched non-agent-attributed contextual cohort."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from authorship.build_survival_transitions import first_parent_landing, timeline
from authorship.languages import SKIP_PATH, lang_of
from authorship.reconstruct_survival_cohorts import write_shard
from authorship.survival_git import git
from authorship.survival_lines import commit_parents_many, extract_first_parent_many


def _date(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_numstat_history(payload: str, language: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in payload.splitlines():
        if raw.startswith("__COMMIT__"):
            sha, committed_at = raw.removeprefix("__COMMIT__").split("\t", 1)
            current = {
                "commit": sha,
                "committed_at": committed_at,
                "added_lines": 0,
            }
            commits.append(current)
            continue
        if current is None or not raw:
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3 or parts[0] == "-":
            continue
        path = parts[2]
        if lang_of(path) == language and not SKIP_PATH(path.lower()):
            current["added_lines"] += int(parts[0])
    return [commit for commit in commits if commit["added_lines"] > 0]


def commit_candidates(
    repository: Path, cutoff_commit: str, language: str
) -> list[dict[str, Any]]:
    payload = git(
        repository,
        "log",
        "--first-parent",
        "--reverse",
        "--format=__COMMIT__%H%x09%cI",
        "--numstat",
        cutoff_commit,
    )
    return parse_numstat_history(payload, language)


def match_targets(
    targets: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    excluded_commits: set[str],
    maximum_days: float = 90,
    maximum_size_ratio: float = 4,
) -> list[dict[str, Any]]:
    available = {
        candidate["commit"]: candidate
        for candidate in candidates
        if candidate["commit"] not in excluded_commits
    }
    results = []
    ordered_targets = sorted(
        targets,
        key=lambda target: hashlib.sha256(
            (
                target["repository_id"]
                + "\0"
                + target["source_commit"]
                + "\0"
                + str(target["pr_number"])
            ).encode()
        ).hexdigest(),
    )
    for target in ordered_targets:
        target_date = _date(target["landing_at"])
        target_size = target["line_count"]
        ranked = []
        for candidate in available.values():
            days = abs((_date(candidate["committed_at"]) - target_date).total_seconds()) / 86400
            ratio = candidate["added_lines"] / target_size
            if (
                days > maximum_days
                or ratio > maximum_size_ratio
                or ratio < 1 / maximum_size_ratio
            ):
                continue
            size_distance = abs(math.log2(ratio))
            ranked.append(
                (
                    days / maximum_days + size_distance,
                    days,
                    size_distance,
                    candidate["commit"],
                    candidate,
                )
            )
        if not ranked:
            results.append(
                {
                    **target,
                    "status": "unmatched",
                    "reason": "no_candidate_within_date_and_size_calipers",
                }
            )
            continue
        _cost, days, size_distance, commit, candidate = min(ranked)
        available.pop(commit)
        results.append(
            {
                **target,
                "status": "matched",
                "context_commit": commit,
                "context_committed_at": candidate["committed_at"],
                "context_added_lines": candidate["added_lines"],
                "date_difference_days": round(days, 6),
                "absolute_log2_size_ratio": round(size_distance, 6),
                "selection_cost": round(days / maximum_days + size_distance, 6),
            }
        )
    return results


def _target_records(
    candidate: dict[str, Any],
    cohort: dict[str, Any],
    repository: Path,
    cutoff_commit: str,
) -> tuple[list[dict[str, Any]], set[str], list[tuple[str, str]]]:
    records = [
        json.loads(raw)
        for raw in Path(cohort["shard_path"]).read_text().splitlines()
        if raw
    ]
    sizes: dict[tuple[str, int], int] = defaultdict(int)
    for record in records:
        sizes[(record["merge_commit"], record["pr_number"])] += 1
    history = timeline(repository, cutoff_commit)
    positions = {commit: index for index, (commit, _date) in enumerate(history)}
    dates = dict(history)
    targets = []
    excluded: set[str] = set()
    landings: list[tuple[str, str]] = []
    for (source_commit, pr_number), line_count in sorted(sizes.items()):
        landing = first_parent_landing(repository, history, source_commit)
        if landing is None:
            continue
        excluded.add(landing)
        landings.append((source_commit, landing))
        targets.append(
            {
                "repository_id": candidate["repository_id"],
                "language": candidate["language"],
                "agent_family": candidate["agent_family"],
                "provenance_tier": candidate["provenance_tier"],
                "source_commit": source_commit,
                "pr_number": pr_number,
                "landing_commit": landing,
                "landing_at": dates[landing],
                "line_count": line_count,
                "landing_position": positions[landing],
            }
        )
    for pr in candidate["pull_requests"]:
        landing = first_parent_landing(repository, history, pr["merge_commit"])
        if landing is not None:
            excluded.add(landing)
    return targets, excluded, landings


def build_repository(
    candidate: dict[str, Any],
    cohort: dict[str, Any],
    git_record: dict[str, Any],
    output_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = Path(git_record["cache_path"])
    targets, excluded, _landings = _target_records(
        candidate, cohort, repository, git_record["cutoff_commit"]
    )
    candidates = commit_candidates(
        repository, git_record["cutoff_commit"], candidate["language"]
    )
    matches = match_targets(targets, candidates, excluded_commits=excluded)
    matched = [match for match in matches if match["status"] == "matched"]
    additions = extract_first_parent_many(
        repository,
        [match["context_commit"] for match in matched],
        candidate["language"],
    )
    context_parents = commit_parents_many(
        repository, [match["context_commit"] for match in matched]
    )
    records = []
    synthetic_prs = []
    for synthetic_number, match in enumerate(
        sorted(matched, key=lambda item: item["context_commit"]), start=1
    ):
        lines = additions.get(match["context_commit"], [])
        diff_base = context_parents[match["context_commit"]][0]
        if len(lines) > match["line_count"]:
            lines = sorted(
                lines,
                key=lambda line: hashlib.sha256(
                    (
                        match["context_commit"]
                        + "\0"
                        + line["path"]
                        + "\0"
                        + str(line["line_number"])
                        + "\0"
                        + line["text"]
                    ).encode()
                ).hexdigest(),
            )[: match["line_count"]]
        synthetic_prs.append(
            {
                "number": synthetic_number,
                "merge_commit": match["context_commit"],
                "merged_at": match["context_committed_at"],
                "target_source_commit": match["source_commit"],
                "target_pr_number": match["pr_number"],
            }
        )
        for line in lines:
            records.append(
                {
                    "repository_id": candidate["repository_id"],
                    "language": candidate["language"],
                    "agent_family": "non_agent_attributed",
                    "provenance_tier": 0,
                    "comparison_label": "non_agent_attributed",
                    "pr_number": synthetic_number,
                    "pr_url": None,
                    "merged_at": match["context_committed_at"],
                    "merge_commit": match["context_commit"],
                    "diff_base": diff_base,
                    "target_source_commit": match["source_commit"],
                    "target_pr_number": match["pr_number"],
                    **line,
                }
            )
    stem = candidate["repository_id"].replace("/", "__")
    shard_path = output_root / "cohort-shards" / f"{stem}.jsonl"
    shard_sha256 = write_shard(shard_path, records)
    audit_path = output_root / "match-audits" / f"{stem}.json"
    audit = {
        "repository_id": candidate["repository_id"],
        "label": "non_agent_attributed",
        "forbidden_label_used": False,
        "excluded_attributed_landing_commits": sorted(excluded),
        "targets": matches,
    }
    audit_sha256 = write_shard(audit_path, [audit])
    inventory = {
        "repository_id": candidate["repository_id"],
        "language": candidate["language"],
        "shard_path": str(shard_path),
        "shard_sha256": shard_sha256,
        "match_audit_path": str(audit_path),
        "match_audit_sha256": audit_sha256,
        "target_count": len(targets),
        "matched_target_count": len(matched),
        "unmatched_target_count": len(targets) - len(matched),
        "line_count": len(records),
        "eligible_200_lines": len(records) >= 200,
    }
    context_candidate = {
        "repository_id": candidate["repository_id"],
        "language": candidate["language"],
        "agent_family": "non_agent_attributed",
        "provenance_tier": 0,
        "pull_requests": synthetic_prs,
    }
    return inventory, context_candidate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--cohorts",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/cohort-inventory.v1.json"
        ),
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
        default=Path(
            "/mnt/agent-code-authorship/survival-study/contextual-v1"
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    arguments = parser.parse_args()
    frame = json.loads(arguments.frame.read_text())
    cohorts = json.loads(arguments.cohorts.read_text())
    git_inventory = json.loads(arguments.git_inventory.read_text())
    candidates = {
        candidate["repository_id"]: candidate for candidate in frame["candidates"]
    }
    git_records = {
        record["repository_id"]: record
        for record in git_inventory["repositories"]
    }
    records = []
    context_candidates = []
    eligible = [
        cohort
        for cohort in cohorts["repositories"]
        if cohort["eligible_200_lines"]
    ]
    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {
            executor.submit(
                build_repository,
                candidates[cohort["repository_id"]],
                cohort,
                git_records[cohort["repository_id"]],
                arguments.output_root,
            ): cohort["repository_id"]
            for cohort in eligible
        }
        for index, future in enumerate(as_completed(futures), start=1):
            inventory, context_candidate = future.result()
            records.append(inventory)
            context_candidates.append(context_candidate)
            print(
                f"{index}/{len(eligible)} {inventory['repository_id']} "
                f"{inventory['matched_target_count']}/{inventory['target_count']} "
                f"matches {inventory['line_count']} lines",
                flush=True,
            )
    context_candidates.sort(key=lambda item: item["repository_id"])
    records.sort(key=lambda item: item["repository_id"])
    inventory_document = {
        "artifact": "contextual-cohort-inventory",
        "version": 1,
        "label": "non_agent_attributed",
        "outcomes_consulted": False,
        "candidate_frame_sha256": hashlib.sha256(
            arguments.frame.read_bytes()
        ).hexdigest(),
        "agent_cohort_inventory_sha256": hashlib.sha256(
            arguments.cohorts.read_bytes()
        ).hexdigest(),
        "git_inventory_sha256": hashlib.sha256(
            arguments.git_inventory.read_bytes()
        ).hexdigest(),
        "repositories": records,
    }
    frame_document = {
        "artifact": "contextual-candidate-frame",
        "version": 1,
        "label": "non_agent_attributed",
        "candidates": context_candidates,
    }
    frame_document["frame_sha256"] = hashlib.sha256(
        json.dumps(
            context_candidates, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    arguments.output_root.mkdir(parents=True, exist_ok=True)
    (arguments.output_root / "cohort-inventory.v1.json").write_text(
        json.dumps(inventory_document, indent=2, sort_keys=True) + "\n"
    )
    (arguments.output_root / "candidate-frame.v1.json").write_text(
        json.dumps(frame_document, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
