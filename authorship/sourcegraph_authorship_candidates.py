"""Freeze human commit candidates before extracting any code diffs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from authorship.sourcegraph_authorship_exact import fetch_window_commits

CANDIDATE_VERSION = 1
CommitFetcher = Callable[..., list[dict[str, Any]]]


class AuthorshipCandidateError(ValueError):
    """Raised when a human candidate frame is incomplete or ambiguous."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def candidate_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "candidate_manifest_sha256"
    }
    return _sha256(content)


def _windows(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    h3, h2 = plan.get("h3_pairs"), plan.get("h2_windows")
    if not isinstance(h3, list) or not isinstance(h2, list):
        raise AuthorshipCandidateError("authorship plan human windows are invalid")
    windows = [*h3, *h2]
    repositories: dict[str, set[str]] = {}
    for row in windows:
        repository, tier = row.get("repository_id"), row.get("evidence_tier")
        if not isinstance(repository, str) or not isinstance(tier, str):
            raise AuthorshipCandidateError("human window identity is invalid")
        repositories.setdefault(repository, set()).add(tier)
    if any(len(tiers) > 1 for tiers in repositories.values()):
        raise AuthorshipCandidateError("repository belongs to multiple human tiers")
    return sorted(windows, key=lambda row: (row["repository_id"], row["evidence_tier"]))


def _head_oid(window: Mapping[str, Any]) -> str:
    value = window.get("agent_commit_oid") or window.get("head_oid")
    if not isinstance(value, str):
        raise AuthorshipCandidateError("human window head commit is missing")
    return value


def _candidate(window: Mapping[str, Any], commit: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        key: commit.get(key)
        for key in (
            "commit_oid",
            "first_parent_oid",
            "parent_count",
            "is_root_commit",
            "committed_at",
        )
    }
    if any(value is None for value in fields.values()):
        raise AuthorshipCandidateError("window commit metadata is incomplete")
    return {
        "repository_id": window["repository_id"],
        "sourcegraph_name": window["sourcegraph_name"],
        **fields,
        "languages": list(window["languages"]),
        "authorship_role": "human",
        "evidence_tier": window["evidence_tier"],
        "window": {
            "start_inclusive": window["start_inclusive"],
            "end_exclusive": window["end_exclusive"],
        },
    }


def _commits(plan: Mapping[str, Any], fetcher: CommitFetcher) -> list[dict[str, Any]]:
    records = []
    for window in _windows(plan):
        commits = fetcher(
            window["sourcegraph_name"],
            _head_oid(window),
            start_inclusive=window["start_inclusive"],
            end_exclusive=window["end_exclusive"],
        )
        records.extend(_candidate(window, commit) for commit in commits)
    keys = [
        (row["repository_id"], row["commit_oid"], row["evidence_tier"])
        for row in records
    ]
    if len(keys) != len(set(keys)):
        raise AuthorshipCandidateError("candidate commits are duplicated")
    return sorted(
        records,
        key=lambda row: (
            row["commit_oid"],
            row["repository_id"],
            row["evidence_tier"],
        ),
    )


def _window_frames(
    plan: Mapping[str, Any], commits: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    frames = []
    for window in _windows(plan):
        repository, tier = window["repository_id"], window["evidence_tier"]
        frames.append(
            {
                "repository_id": repository,
                "sourcegraph_name": window["sourcegraph_name"],
                "head_oid": _head_oid(window),
                "start_inclusive": window["start_inclusive"],
                "end_exclusive": window["end_exclusive"],
                "languages": list(window["languages"]),
                "evidence_tier": tier,
                "commit_oids": sorted(
                    row["commit_oid"]
                    for row in commits
                    if row.get("repository_id") == repository
                    and row.get("evidence_tier") == tier
                ),
            }
        )
    return frames


def _commit_window_errors(
    commits: Sequence[Mapping[str, Any]], plan: Mapping[str, Any]
) -> list[str]:
    windows = {
        (row["repository_id"], row["evidence_tier"]): row for row in _windows(plan)
    }
    errors = []
    for commit in commits:
        window = windows.get((commit.get("repository_id"), commit.get("evidence_tier")))
        if window is None:
            errors.append("candidate manifest commit is outside the frozen frame")
            continue
        expected_window = {
            "start_inclusive": window["start_inclusive"],
            "end_exclusive": window["end_exclusive"],
        }
        if (
            commit.get("sourcegraph_name") != window["sourcegraph_name"]
            or commit.get("languages") != list(window["languages"])
            or commit.get("authorship_role") != "human"
            or commit.get("window") != expected_window
        ):
            errors.append(
                "candidate manifest commit metadata differs from frozen window"
            )
        try:
            committed = datetime.fromisoformat(
                str(commit.get("committed_at")).replace("Z", "+00:00")
            )
            start = datetime.fromisoformat(
                window["start_inclusive"].replace("Z", "+00:00")
            )
            end = datetime.fromisoformat(window["end_exclusive"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            errors.append("candidate manifest commit timestamp is invalid")
            continue
        if not start <= committed < end:
            errors.append("candidate manifest commit falls outside its frozen window")
    return errors


def build_candidate_manifest(
    plan: Mapping[str, Any],
    *,
    commit_fetcher: CommitFetcher = fetch_window_commits,
) -> dict[str, Any]:
    """Enumerate the full frozen H2/H3 commit windows without sampling."""
    if plan.get("outcomes_consulted") is not False:
        raise AuthorshipCandidateError("authorship plan must remain outcome blind")
    commits = _commits(plan, commit_fetcher)
    window_errors = _commit_window_errors(commits, plan)
    if window_errors:
        raise AuthorshipCandidateError("; ".join(sorted(set(window_errors))))
    tier_counts = Counter(row["evidence_tier"] for row in commits)
    document = {
        "candidate_manifest_version": CANDIDATE_VERSION,
        "status": "frozen_before_diff_extraction",
        "authorship_unit_plan_sha256": plan.get("authorship_unit_plan_sha256"),
        "selection": "all_reachable_commits_in_half_open_frozen_windows",
        "window_frames": _window_frames(plan, commits),
        "commits": commits,
        "counts": {
            "H2_policy_human": tier_counts["H2_policy_human"],
            "H3_contemporary_pre_adoption": tier_counts["H3_contemporary_pre_adoption"],
            "total_commits": len(commits),
        },
        "outcomes_consulted": False,
    }
    return {
        **document,
        "candidate_manifest_sha256": candidate_manifest_sha256(document),
    }


def _valid_commits(rows: Any) -> bool:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return False
    required = {
        "repository_id",
        "sourcegraph_name",
        "commit_oid",
        "first_parent_oid",
        "committed_at",
        "languages",
        "authorship_role",
        "evidence_tier",
        "window",
    }
    return all(isinstance(row, Mapping) and required <= set(row) for row in rows)


def _independent_frame_errors(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    commit_fetcher: CommitFetcher | None,
) -> list[str]:
    if commit_fetcher is None:
        return ["candidate manifest independent Sourcegraph frame is required"]
    try:
        expected_commits = _commits(plan, commit_fetcher)
    except (AuthorshipCandidateError, OSError, RuntimeError, ValueError) as error:
        return [f"candidate manifest independent Sourcegraph frame failed: {error}"]
    if document.get("commits") != expected_commits or document.get(
        "window_frames"
    ) != _window_frames(plan, expected_commits):
        return ["candidate manifest does not match independent Sourcegraph frame"]
    return []


def validate_candidate_manifest(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    commit_fetcher: CommitFetcher | None = None,
) -> list[str]:
    """Validate a candidate manifest against its frozen parent plan."""
    errors = []
    if document.get("candidate_manifest_version") != CANDIDATE_VERSION:
        errors.append("candidate manifest version does not match")
    if document.get("authorship_unit_plan_sha256") != plan.get(
        "authorship_unit_plan_sha256"
    ):
        errors.append("candidate manifest does not match authorship plan")
    if document.get("candidate_manifest_sha256") != candidate_manifest_sha256(document):
        errors.append("candidate manifest SHA-256 does not match")
    commits = document.get("commits")
    if not _valid_commits(commits):
        return [*errors, "candidate manifest commits are invalid"]
    errors.extend(_commit_window_errors(commits, plan))
    if document.get("window_frames") != _window_frames(plan, commits):
        errors.append("candidate manifest window frame does not match commits")
    errors.extend(_independent_frame_errors(document, plan, commit_fetcher))
    counts = Counter(row["evidence_tier"] for row in commits)
    expected = {
        "H2_policy_human": counts["H2_policy_human"],
        "H3_contemporary_pre_adoption": counts["H3_contemporary_pre_adoption"],
        "total_commits": len(commits),
    }
    if document.get("counts") != expected:
        errors.append("candidate manifest counts do not match")
    if document.get("outcomes_consulted") is not False:
        errors.append("candidate manifest is not outcome blind")
    return errors
