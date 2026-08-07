"""Construct and freeze an outcome-blind semantic-topology control cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from authorship.semantic_change_materialization import _parse_hunks, _path_class

FORBIDDEN_OUTCOME_KEYS = {
    "subsequent_change_present",
    "time_to_first_followup_days",
    "first_followup_author_differs",
    "distinct_followup_author_count",
}
NUMERIC_COVARIATES = (
    "log1p_added_lines",
    "log1p_deleted_lines",
    "file_age_days",
    "prior_path_commit_count_180d",
    "repository_commit_count_30d",
)


def canonical_sha256(document: dict[str, Any]) -> str:
    excluded = {
        "candidate_pool_sha256",
        "match_sha256",
        "inventory_sha256",
    }
    content = {key: value for key, value in document.items() if key not in excluded}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def classify_task(subject: str, path: str) -> str:
    """Classify an introducing change without consulting its future."""
    value = f"{subject} {path}".casefold()
    if re.search(r"(^|\W)(fix|bug|defect|crash|regression|hotfix)(\W|$)", value):
        return "bug_fix"
    if path.casefold().endswith((".md", ".rst")) or "/docs/" in f"/{path.casefold()}":
        return "documentation"
    if re.search(r"(^|[/_.-])(test|tests|spec)([/_.-]|$)", path.casefold()):
        return "test"
    if re.search(r"(^|\W)(refactor|restructure|cleanup|simplif)(\W|$)", value):
        return "refactor"
    if re.search(
        r"(^|\W)(add|implement|introduce|feature|support)(\W|$)", subject.casefold()
    ):
        return "feature"
    if re.search(r"(^|\W)(bump|deps?|dependency|chore|build|ci|release)(\W|$)", value):
        return "maintenance"
    return "other"


def _walk_keys(value: Any, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{prefix}.{key}" if prefix else key
            if key in FORBIDDEN_OUTCOME_KEYS:
                hits.append(location)
            hits.extend(_walk_keys(child, location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_walk_keys(child, f"{prefix}[{index}]"))
    return hits


def validate_outcome_blind(rows: list[dict[str, Any]]) -> list[str]:
    return [f"forbidden outcome key at {hit}" for hit in _walk_keys(rows)]


def _scales(rows: list[dict[str, Any]]) -> dict[str, float]:
    result = {}
    for field in NUMERIC_COVARIATES:
        values = sorted(float(row["covariates"][field]) for row in rows)
        if len(values) < 2:
            result[field] = 1.0
            continue
        lower = values[len(values) // 4]
        upper = values[(3 * len(values)) // 4]
        result[field] = max(upper - lower, statistics.pstdev(values), 1e-9)
    return result


def _distance(
    treated: dict[str, Any], candidate: dict[str, Any], scales: dict[str, float]
) -> tuple[float, dict[str, float]]:
    components = {}
    for field in NUMERIC_COVARIATES:
        delta = (
            float(treated["covariates"][field]) - float(candidate["covariates"][field])
        ) / scales[field]
        components[field] = delta * delta
    language_penalty = float(
        treated["covariates"]["language"] != candidate["covariates"]["language"]
    )
    components["language_mismatch"] = language_penalty
    components["calendar_distance_days"] = (
        (
            float(treated["covariates"]["introduced_at_epoch"])
            - float(candidate["covariates"]["introduced_at_epoch"])
        )
        / 86400.0
        / 180.0
    ) ** 2
    return math.sqrt(sum(components.values())), components


def _candidate_id_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(row["unit_id"].encode()).hexdigest()


def freeze_matches(
    treated: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    caliper: float,
) -> dict[str, Any]:
    """Greedy deterministic exact/fallback matching without replacement."""
    errors = validate_outcome_blind(treated + candidates)
    if errors:
        raise ValueError("; ".join(errors))
    scales = _scales(treated + candidates)
    available = {item["unit_id"]: item for item in candidates}
    matches = []
    unmatched = []
    for target in sorted(treated, key=lambda item: item["unit_id"]):
        exact = [
            item
            for item in available.values()
            if (
                item["repository"],
                item["covariates"]["path_class"],
                item["covariates"]["task_class"],
            )
            == (
                target["repository"],
                target["covariates"]["path_class"],
                target["covariates"]["task_class"],
            )
        ]
        tier = "exact_task"
        pool = exact
        if not pool:
            tier = "fallback_path"
            pool = [
                item
                for item in available.values()
                if (
                    item["repository"],
                    item["covariates"]["path_class"],
                )
                == (
                    target["repository"],
                    target["covariates"]["path_class"],
                )
            ]
        if not pool:
            unmatched.append(
                {"treated_unit_id": target["unit_id"], "reason": "no_overlap"}
            )
            continue
        ranked = []
        for candidate in pool:
            distance, components = _distance(target, candidate, scales)
            ranked.append(
                (distance, _candidate_id_key(candidate), candidate, components)
            )
        distance, _, selected, components = min(
            ranked, key=lambda item: (item[0], item[1])
        )
        if distance > caliper:
            unmatched.append(
                {
                    "treated_unit_id": target["unit_id"],
                    "reason": "outside_caliper",
                    "nearest_distance": distance,
                }
            )
            continue
        matches.append(
            {
                "pair_id": "sha256:"
                + hashlib.sha256(
                    f"{target['unit_id']}\\0{selected['unit_id']}".encode()
                ).hexdigest(),
                "treated_unit_id": target["unit_id"],
                "control_unit_id": selected["unit_id"],
                "match_tier": tier,
                "distance": distance,
                "distance_components": components,
            }
        )
        del available[selected["unit_id"]]
    document = {
        "match_version": 1,
        "method": "exact_plus_robust_scaled_euclidean_nearest_neighbor",
        "caliper": caliper,
        "replacement": False,
        "scales": scales,
        "treated_count": len(treated),
        "candidate_count": len(candidates),
        "matched_count": len(matches),
        "matches": matches,
        "unmatched": unmatched,
        "outcomes_consulted": False,
    }
    document["match_sha256"] = canonical_sha256(document)
    return document


def group_by_repository(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["repository"]].append(row)
    return dict(grouped)


def _git(repo: Path, *args: str, timeout: int = 300) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError(f"git {' '.join(args[:3])} failed: {result.stderr[:300]}")
    return result.stdout


def _language(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    return {
        ".py": "Python",
        ".go": "Go",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".rs": "Rust",
        ".java": "Java",
        ".c": "C",
        ".h": "C/C++",
        ".cc": "C/C++",
        ".cpp": "C/C++",
        ".md": "Markdown",
        ".rst": "reStructuredText",
    }.get(suffix, suffix.lstrip(".") or "unknown")


def _commit_metadata(repo: Path, commit: str) -> tuple[int, str]:
    raw = _git(repo, "show", "-s", "--format=%ct%x00%s", commit).strip()
    timestamp, subject = raw.split("\0", 1)
    return int(timestamp), subject


def _history_index(repo: Path, cutoff: str) -> dict[str, Any]:
    raw = _git(
        repo,
        "log",
        "--first-parent",
        "--format=@@@%H%x00%ct%x00%P%x00%s",
        "--name-only",
        cutoff,
        timeout=900,
    )
    commits: dict[str, dict[str, Any]] = {}
    path_times: dict[str, list[int]] = defaultdict(list)
    commit_times: list[int] = []
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        if line.startswith("@@@"):
            commit, timestamp, parents, subject = line[3:].split("\0", 3)
            current = {
                "commit": commit,
                "timestamp": int(timestamp),
                "parents": parents.split(),
                "subject": subject,
                "paths": [],
            }
            commits[commit] = current
            commit_times.append(int(timestamp))
        elif current is not None and line:
            current["paths"].append(line)
            path_times[line].append(current["timestamp"])
    commit_times.sort()
    for values in path_times.values():
        values.sort()
    return {
        "commits": commits,
        "commit_times": commit_times,
        "path_times": dict(path_times),
    }


def _unit_covariates(
    repo: Path,
    *,
    commit: str,
    timestamp: int,
    subject: str,
    path: str,
    added: int,
    deleted: int,
    history_index: dict[str, Any],
) -> dict[str, Any]:
    path_times = history_index["path_times"].get(path, [])
    prior_index = bisect_left(path_times, timestamp)
    prior_times = path_times[:prior_index]
    first_timestamp = prior_times[0] if prior_times else timestamp
    since_180 = timestamp - 180 * 86400
    since_30 = timestamp - 30 * 86400
    commit_times = history_index["commit_times"]
    return {
        "repository": repo.name.replace("__", "/"),
        "path_class": _path_class(path),
        "task_class": classify_task(subject, path),
        "language": _language(path),
        "log1p_added_lines": math.log1p(added),
        "log1p_deleted_lines": math.log1p(deleted),
        "file_age_days": max(0.0, (timestamp - first_timestamp) / 86400.0),
        "prior_path_commit_count_180d": float(
            prior_index - bisect_left(path_times, since_180)
        ),
        "repository_commit_count_30d": float(
            bisect_left(commit_times, timestamp) - bisect_left(commit_times, since_30)
        ),
        "introduced_at_epoch": timestamp,
    }


def _commit_units(
    repo: Path,
    repository_id: str,
    commit: str,
    exposure: str,
    history_index: dict[str, Any],
) -> list[dict[str, Any]]:
    parent = _git(repo, "rev-parse", f"{commit}^").strip()
    timestamp, subject = _commit_metadata(repo, commit)
    diff = _git(
        repo,
        "diff",
        "--unified=0",
        "--no-ext-diff",
        "--no-renames",
        parent,
        commit,
    )
    return _hunks_to_units(
        repo=repo,
        repository_id=repository_id,
        commit=commit,
        parent=parent,
        exposure=exposure,
        timestamp=timestamp,
        subject=subject,
        diff=diff,
        history_index=history_index,
    )


def _hunks_to_units(
    *,
    repo: Path,
    repository_id: str,
    commit: str,
    parent: str,
    exposure: str,
    timestamp: int,
    subject: str,
    diff: str,
    history_index: dict[str, Any],
) -> list[dict[str, Any]]:
    units = []
    for hunk in _parse_hunks(diff):
        path = hunk["path"]
        if _path_class(path) not in {"source", "test", "documentation"}:
            continue
        identity = {
            "repository": repository_id,
            "commit": commit,
            "diff_base": parent,
            "path": path,
            "new_start": hunk["new_start"],
            "new_count": hunk["new_count"],
            "added_lines": hunk["added_lines"],
        }
        unit_id = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        units.append(
            {
                "unit_id": unit_id,
                "exposure": exposure,
                "repository": repository_id,
                "commit": commit,
                "diff_base": parent,
                "path": path,
                "hunk": {
                    "new_start": hunk["new_start"],
                    "new_count": hunk["new_count"],
                    "added_line_count": len(hunk["added_lines"]),
                    "deleted_line_count": hunk["old_count"],
                },
                "covariates": _unit_covariates(
                    repo,
                    commit=commit,
                    timestamp=timestamp,
                    subject=subject,
                    path=path,
                    added=len(hunk["added_lines"]),
                    deleted=hunk["old_count"],
                    history_index=history_index,
                ),
                "provenance": {
                    "route": "pinned_git",
                    "source_revision": commit,
                    "outcomes_consulted": False,
                },
            }
        )
    return units


def _streamed_candidate_units(
    repo: Path,
    repository_id: str,
    cutoff: str,
    earliest: datetime,
    latest: datetime,
    eligible_commits: set[str],
    history_index: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = _git(
        repo,
        "log",
        "--first-parent",
        "--no-merges",
        "--format=@@@%H",
        "--unified=0",
        "--no-renames",
        f"--since={earliest.isoformat()}",
        f"--until={latest.isoformat()}",
        cutoff,
        timeout=1800,
    )
    units: list[dict[str, Any]] = []
    current: str | None = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal lines
        if current is None or current not in eligible_commits:
            lines = []
            return
        metadata = history_index["commits"][current]
        units.extend(
            _hunks_to_units(
                repo=repo,
                repository_id=repository_id,
                commit=current,
                parent=metadata["parents"][0],
                exposure="control_candidate",
                timestamp=metadata["timestamp"],
                subject=metadata["subject"],
                diff="\n".join(lines),
                history_index=history_index,
            )
        )
        lines = []

    for line in raw.splitlines():
        if line.startswith("@@@") and re.fullmatch(r"@@@[0-9a-f]{40}", line):
            flush()
            current = line[3:]
        else:
            lines.append(line)
    flush()
    return units


def _agent_commits(catalog_entry: dict[str, Any]) -> set[str]:
    commits = set(catalog_entry.get("commit_shas", []))
    commits.update(
        pull["merge_commit"]
        for pull in catalog_entry.get("pull_requests", [])
        if pull.get("merge_commit")
    )
    return commits


def build_candidate_pool(
    protocol: dict[str, Any],
    plan: dict[str, Any],
    event_catalog: dict[str, Any],
    repository_root: Path,
) -> dict[str, Any]:
    """Build treated and eligible control hunks without reading follow-up history."""
    by_repository = {
        item["repository_id"].casefold(): item
        for item in event_catalog.get("candidates", [])
    }
    treated: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    revisions = {}
    window = protocol["eligibility"]["commit_window_days"]
    for entry in plan["repositories"]:
        if entry["stratum"] != "attributable_agent":
            continue
        repository_id = entry["canonical_repository_id"]
        repo = repository_root / repository_id.replace("/", "__")
        history_index = _history_index(repo, entry["cutoff_commit"])
        catalog = by_repository.get(repository_id.casefold(), {})
        excluded_agent = _agent_commits(catalog)
        introducing = [
            pull["merge_commit"]
            for pull in catalog.get("pull_requests", [])
            if pull.get("merge_commit")
        ] or entry["introduction_event"]["commit_shas"]
        introducing = list(dict.fromkeys(introducing))
        timestamps = []
        for commit in introducing:
            if commit not in excluded_agent:
                excluded_agent.add(commit)
            timestamp, _ = _commit_metadata(repo, commit)
            timestamps.append(timestamp)
            treated.extend(
                _commit_units(repo, repository_id, commit, "treated", history_index)
            )
        earliest = datetime.fromtimestamp(min(timestamps), UTC) - timedelta(days=window)
        latest = min(
            datetime.fromtimestamp(max(timestamps), UTC) + timedelta(days=window),
            datetime.fromtimestamp(
                _commit_metadata(repo, entry["cutoff_commit"])[0], UTC
            ),
        )
        revisions[repository_id] = {
            "cutoff_commit": entry["cutoff_commit"],
            "candidate_since": earliest.isoformat(),
            "candidate_until": latest.isoformat(),
        }
        commits = [
            commit
            for commit, metadata in history_index["commits"].items()
            if int(earliest.timestamp())
            <= metadata["timestamp"]
            <= int(latest.timestamp())
            and len(metadata["parents"]) == 1
        ]
        eligible_commits = set()
        for commit in sorted(set(commits)):
            if commit in excluded_agent:
                exclusions.append(
                    {
                        "repository": repository_id,
                        "commit": commit,
                        "reason": "known_agent_attributed_commit",
                    }
                )
                continue
            eligible_commits.add(commit)
        candidates.extend(
            _streamed_candidate_units(
                repo,
                repository_id,
                entry["cutoff_commit"],
                earliest,
                latest,
                eligible_commits,
                history_index,
            )
        )
    treated.sort(key=lambda item: item["unit_id"])
    candidates.sort(key=lambda item: item["unit_id"])
    errors = validate_outcome_blind(treated + candidates)
    if errors:
        raise ValueError("; ".join(errors))
    document = {
        "candidate_pool_version": 1,
        "protocol_sha256": protocol["protocol_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "outcomes_consulted": False,
        "repository_revisions": revisions,
        "treated_count": len(treated),
        "candidate_count": len(candidates),
        "exclusion_count": len(exclusions),
        "treated": treated,
        "candidates": candidates,
        "exclusions": exclusions,
    }
    document["candidate_pool_sha256"] = canonical_sha256(document)
    return document


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("study/semantic-control-protocol.v1.json"),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/semantic-topology-pilot-plan.v1.json"),
    )
    parser.add_argument(
        "--events", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path("/mnt/agent-code-authorship/survival-study/repositories"),
    )
    parser.add_argument(
        "--candidate-output",
        type=Path,
        default=Path("results/semantic-control-candidates.v1.json"),
    )
    parser.add_argument(
        "--match-output",
        type=Path,
        default=Path("results/semantic-control-matches.v1.json"),
    )
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    pool = build_candidate_pool(
        protocol,
        json.loads(args.plan.read_text()),
        json.loads(args.events.read_text()),
        args.repository_root,
    )
    write_json(args.candidate_output, pool)
    matches = freeze_matches(
        pool["treated"],
        pool["candidates"],
        caliper=float(protocol["matching"]["caliper"]),
    )
    matches["protocol_sha256"] = protocol["protocol_sha256"]
    matches["candidate_pool_sha256"] = pool["candidate_pool_sha256"]
    matches["match_sha256"] = canonical_sha256(matches)
    write_json(args.match_output, matches)
    print(
        json.dumps(
            {
                "candidate_pool_sha256": pool["candidate_pool_sha256"],
                "treated_count": pool["treated_count"],
                "candidate_count": pool["candidate_count"],
                "match_sha256": matches["match_sha256"],
                "matched_count": matches["matched_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
