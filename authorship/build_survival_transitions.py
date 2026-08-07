"""Build line-level horizon transitions from pinned first-parent histories."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from authorship.survival_git import git
from authorship.survival_lineage import (
    advance_states,
    parse_name_status,
    parse_patch,
    select_horizon,
)


HORIZONS = (30, 90, 180, 365)
CUTOFF = "2026-07-24T23:59:59Z"


def first_parent_landing(
    repository: Path,
    history: list[tuple[str, str]],
    commit: str,
) -> str | None:
    """Return the earliest first-parent commit whose tree contains commit."""
    positions = {sha: index for index, (sha, _date) in enumerate(history)}
    if commit in positions:
        return commit
    low, high = 0, len(history) - 1
    if git(
        repository,
        "merge-base",
        commit,
        history[high][0],
        check=False,
    ) != commit:
        return None
    while low < high:
        middle = (low + high) // 2
        contained = (
            git(
                repository,
                "merge-base",
                commit,
                history[middle][0],
                check=False,
            )
            == commit
        )
        if contained:
            high = middle
        else:
            low = middle + 1
    return history[low][0]


class JsonlWriter:
    def __init__(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        self.destination = destination
        self.temporary = Path(temporary)
        self.stream = os.fdopen(descriptor, "wb")
        self.digest = hashlib.sha256()
        self.count = 0

    def write(self, record: dict[str, Any]) -> None:
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        self.stream.write(payload)
        self.digest.update(payload)
        self.count += 1

    def close(self) -> str:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        os.replace(self.temporary, self.destination)
        return self.digest.hexdigest()


def line_id(record: dict[str, Any]) -> str:
    payload = "\0".join(
        [
            record["repository_id"],
            record["merge_commit"],
            record["path"],
            str(record["line_number"]),
            record["content_sha256"],
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def timeline(repository: Path, cutoff_commit: str) -> list[tuple[str, str]]:
    output = git(
        repository,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x09%cI",
        cutoff_commit,
    )
    return [
        tuple(raw.split("\t", 1))  # type: ignore[misc]
        for raw in output.splitlines()
        if "\t" in raw
    ]


def changed_paths(
    repository: Path, earliest_commit: str, cutoff_commit: str
) -> dict[str, set[str]]:
    output = git(
        repository,
        "log",
        "--first-parent",
        "--reverse",
        "--format=__COMMIT__%H",
        "--name-status",
        "-M",
        f"{earliest_commit}..{cutoff_commit}",
    )
    return parse_name_status(output)


def _state_record(
    state: dict[str, Any],
    *,
    horizon: dict[str, Any],
) -> dict[str, Any]:
    return {
        "line_id": state["line_id"],
        "repository_id": state["repository_id"],
        "language": state["language"],
        "agent_family": state["agent_family"],
        "provenance_tier": state["provenance_tier"],
        "pr_number": state["pr_number"],
        "merge_commit": state["merge_commit"],
        "merged_at": state["merged_at"],
        "original_path": state["original_path"],
        "original_line_number": state["original_line_number"],
        "content_sha256": state["content_sha256"],
        "horizon_days": horizon["horizon_days"],
        "horizon_status": horizon["status"],
        "horizon_commit": horizon.get("commit"),
        "horizon_target_at": horizon.get("target_at"),
        "state": (
            "right_censored"
            if horizon["status"] == "right_censored"
            else (
                "unobservable"
                if horizon["status"] != "observed"
                else state["state"]
            )
        ),
        "current_path": (
            state.get("path")
            if state["state"] not in {"deleted", "unobservable"}
            else None
        ),
        "current_line_number": (
            state.get("line_number")
            if state["state"] not in {"deleted", "unobservable"}
            else None
        ),
        "terminal_commit": state.get("terminal_commit"),
        "terminal_reason": state.get("terminal_reason"),
    }


def process_repository(
    candidate: dict[str, Any],
    cohort: dict[str, Any],
    git_inventory: dict[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    repository_id = candidate["repository_id"]
    repository = Path(git_inventory["cache_path"])
    introductions = [
        json.loads(raw) for raw in Path(cohort["shard_path"]).read_text().splitlines()
    ]
    history = timeline(repository, git_inventory["cutoff_commit"])
    positions = {commit: index for index, (commit, _date) in enumerate(history)}
    by_commit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_pr: dict[tuple[str, int], list[str]] = defaultdict(list)
    landing_by_pr: dict[tuple[str, int], str] = {}
    for record in introductions:
        record["line_id"] = line_id(record)
        record["original_path"] = record["path"]
        record["original_line_number"] = record["line_number"]
        pr_key = (record["merge_commit"], record["pr_number"])
        landing = landing_by_pr.get(pr_key)
        if landing is None:
            landing = first_parent_landing(
                repository, history, record["merge_commit"]
            )
            if landing is None:
                raise RuntimeError(
                    f"{repository_id}: reachable commit {record['merge_commit']} "
                    "has no first-parent landing"
                )
            landing_by_pr[pr_key] = landing
        by_commit[landing].append(record)
        by_pr[pr_key].append(record["line_id"])

    earliest_position = min(positions[commit] for commit in by_commit)
    earliest_commit = history[earliest_position][0]
    path_changes = changed_paths(
        repository, earliest_commit, git_inventory["cutoff_commit"]
    )

    snapshots: dict[str, list[tuple[tuple[str, int], dict[str, Any]]]] = defaultdict(
        list
    )
    censored: list[tuple[tuple[str, int], dict[str, Any]]] = []
    for pr_key in sorted(by_pr):
        landing = landing_by_pr[pr_key]
        landing_date = history[positions[landing]][1]
        for days in HORIZONS:
            horizon = select_horizon(
                history,
                merge_commit=landing,
                merged_at=landing_date,
                days=days,
                cutoff=CUTOFF,
            )
            if horizon["status"] == "observed":
                snapshots[horizon["commit"]].append((pr_key, horizon))
            else:
                censored.append((pr_key, horizon))

    stem = repository_id.replace("/", "__")
    transition_writer = JsonlWriter(output_root / "transitions" / f"{stem}.jsonl")
    event_writer = JsonlWriter(output_root / "structural-events" / f"{stem}.jsonl")
    states: dict[str, dict[str, Any]] = {}
    active_index: dict[str, set[str]] = {}
    counts: Counter[tuple[int, str]] = Counter()
    previous_commit: str | None = None
    structural_counts: Counter[str] = Counter()

    for commit, _committed_at in history[earliest_position:]:
        paths = path_changes.get(commit, set())
        active_paths = {path for path, ids in active_index.items() if ids}
        if previous_commit is not None and paths & active_paths:
            patch = git(
                repository,
                "diff",
                "--no-color",
                "--unified=0",
                "--find-renames",
                previous_commit,
                commit,
            )
            parsed_changes = parse_patch(patch)
            represented = {
                change["old_path"]
                for change in parsed_changes
                if change["old_path"] is not None
            }
            for missing_path in sorted((paths & active_paths) - represented):
                parsed_changes.append(
                    {
                        "old_path": missing_path,
                        "new_path": missing_path,
                        "binary": True,
                        "hunks": [],
                    }
                )
            events = advance_states(
                states,
                parsed_changes,
                commit=commit,
                active_index=active_index,
            )
            for event in events:
                if event["decision"] in {"modified_candidate", "unobservable"}:
                    state = states[event["line_id"]]
                    event_writer.write(
                        {
                            "case_key": hashlib.sha256(
                                (
                                    event["line_id"]
                                    + "\0"
                                    + commit
                                    + "\0"
                                    + event["decision"]
                                ).encode()
                            ).hexdigest(),
                            "line_id": event["line_id"],
                            "repository_id": repository_id,
                            "language": candidate["language"],
                            "agent_family": candidate["agent_family"],
                            "provenance_tier": candidate["provenance_tier"],
                            "commit": commit,
                            "decision": event["decision"],
                            "reason": event.get("reason"),
                            "prior_text": event["prior_text"],
                            "candidate_text": event.get("horizon_text"),
                            "structural_score": event.get(
                                "structural_score",
                                event.get("best_structural_score"),
                            ),
                            "original_path": state["original_path"],
                            "candidate_path": event.get("horizon_path"),
                        }
                    )
                    structural_counts[event["decision"]] += 1

        for introduction in by_commit.get(commit, []):
            state = {
                **introduction,
                "state": "unchanged",
                "path": introduction["original_path"],
                "line_number": introduction["original_line_number"],
            }
            states[introduction["line_id"]] = state
            active_index.setdefault(state["path"], set()).add(state["line_id"])

        for pr_key, horizon in sorted(
            snapshots.get(commit, []),
            key=lambda item: (item[1]["horizon_days"], item[0]),
        ):
            for current_line_id in sorted(by_pr[pr_key]):
                row = _state_record(states[current_line_id], horizon=horizon)
                transition_writer.write(row)
                counts[(horizon["horizon_days"], row["state"])] += 1
        previous_commit = commit

    for pr_key, horizon in sorted(
        censored, key=lambda item: (item[1]["horizon_days"], item[0])
    ):
        for current_line_id in sorted(by_pr[pr_key]):
            row = _state_record(states[current_line_id], horizon=horizon)
            transition_writer.write(row)
            counts[(horizon["horizon_days"], row["state"])] += 1

    transition_sha256 = transition_writer.close()
    event_sha256 = event_writer.close()
    observed = sum(
        count for (horizon, state), count in counts.items() if state != "right_censored"
    )
    unobservable = sum(
        count for (_horizon, state), count in counts.items() if state == "unobservable"
    )
    return {
        "repository_id": repository_id,
        "line_count": len(introductions),
        "transition_count": transition_writer.count,
        "transition_sha256": transition_sha256,
        "structural_event_count": event_writer.count,
        "structural_event_sha256": event_sha256,
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
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--cohort-inventory",
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
        default=Path("/mnt/agent-code-authorship/survival-study/lineage-v1"),
    )
    parser.add_argument("--workers", type=int, default=2)
    arguments = parser.parse_args()
    frame = json.loads(arguments.frame.read_text())
    cohorts = json.loads(arguments.cohort_inventory.read_text())
    git_document = json.loads(arguments.git_inventory.read_text())
    candidates = {item["repository_id"]: item for item in frame["candidates"]}
    git_records = {
        item["repository_id"]: item for item in git_document["repositories"]
    }
    eligible = [
        item for item in cohorts["repositories"] if item["eligible_200_lines"]
    ]
    records = []
    with ThreadPoolExecutor(max_workers=arguments.workers) as executor:
        futures = {
            executor.submit(
                process_repository,
                candidates[item["repository_id"]],
                item,
                git_records[item["repository_id"]],
                arguments.output_root,
            ): item["repository_id"]
            for item in eligible
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            records.append(result)
            print(
                f"{index}/{len(futures)} {result['repository_id']} "
                f"{result['transition_count']} transitions",
                flush=True,
            )
    records.sort(key=lambda item: item["repository_id"])
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
    destination = arguments.output_root / "lineage-inventory.v1.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
