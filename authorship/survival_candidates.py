"""Outcome-blind repository selection for the agent-code survival study."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from authorship.languages import SKIP_PATH, lang_of


START = "2024-01-01T00:00:00Z"
CUTOFF = "2026-07-24T23:59:59Z"


def frame_sha256(frame: dict[str, Any]) -> str:
    content = {key: value for key, value in frame.items() if key != "frame_sha256"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate_candidate_frame(frame: dict[str, Any]) -> list[str]:
    errors = []
    if frame.get("outcomes_consulted") is not False:
        errors.append("candidate selection must be outcome blind")
    if frame.get("frame_sha256") != frame_sha256(frame):
        errors.append("frame_sha256 does not match canonical frame")
    candidates = frame.get("candidates")
    if not isinstance(candidates, list):
        return errors + ["candidates must be a list"]
    repositories = [item.get("repository_id") for item in candidates]
    if len(repositories) != len(set(repositories)):
        errors.append("repository groups must be unique")
    for candidate in candidates:
        if candidate.get("language") not in {"Python", "Go"}:
            errors.append("candidate language must be Python or Go")
        if candidate.get("attributable_added_lines", 0) < 200:
            errors.append("candidate must have at least 200 attributable lines")
        if candidate.get("default_branch_status") != "github_verified":
            errors.append("candidate default branch must be verified")
        if not candidate.get("commit_shas") or not candidate.get("pull_requests"):
            errors.append("candidate must retain exact PR and commit identifiers")
    return errors


def canonical_repository_id(value: str) -> str:
    value = value.strip().removesuffix("/").removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
            break
    return value.lower()


def selection_key(repository_id: str, language: str, agent: str, tier: int) -> str:
    payload = "\0".join(
        [str(tier), language, agent, canonical_repository_id(repository_id)]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def freeze_candidates(
    rows: Iterable[dict[str, Any]],
    *,
    target_ids: set[str],
    reference_ids: set[str],
    cap: int,
) -> dict[str, Any]:
    excluded = {
        canonical_repository_id(value) for value in target_ids | reference_ids
    }
    groups: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for row in rows:
        repo_id = canonical_repository_id(row["repository_id"])
        language = row["language"]
        filename = row.get("filename")
        merged_at = row.get("merged_at")
        if (
            repo_id in excluded
            or language not in {"Python", "Go"}
            or not isinstance(filename, str)
            or not isinstance(merged_at, str)
            or not (START <= merged_at <= CUTOFF)
            or lang_of(filename) != language
            or SKIP_PATH(filename)
        ):
            continue
        tier = int(row["provenance_tier"])
        agent = str(row["agent_family"])
        key = (repo_id, language, agent, tier)
        candidate = groups.setdefault(
            key,
            {
                "repository_id": repo_id,
                "repository_url": row["repository_url"],
                "language": language,
                "agent_family": agent,
                "provenance_tier": tier,
                "pr_ids": set(),
                "pr_numbers": set(),
                "pr_urls": set(),
                "commit_shas": set(),
                "_pull_requests": {},
                "attributable_added_lines": 0,
                "first_merged_at": merged_at,
                "last_merged_at": merged_at,
                "default_branch_status": "pending_git_verification",
            },
        )
        candidate["pr_ids"].add(int(row["pr_id"]))
        candidate["pr_numbers"].add(int(row["pr_number"]))
        candidate["pr_urls"].add(row["pr_url"])
        candidate["commit_shas"].add(row["commit_sha"])
        candidate["attributable_added_lines"] += max(0, int(row.get("additions") or 0))
        pr = candidate["_pull_requests"].setdefault(
            int(row["pr_number"]),
            {
                "id": int(row["pr_id"]),
                "number": int(row["pr_number"]),
                "url": row["pr_url"],
                "merged_at": merged_at,
                "commit_shas": set(),
                "attributable_added_lines": 0,
            },
        )
        pr["commit_shas"].add(row["commit_sha"])
        pr["attributable_added_lines"] += max(0, int(row.get("additions") or 0))
        candidate["first_merged_at"] = min(candidate["first_merged_at"], merged_at)
        candidate["last_merged_at"] = max(candidate["last_merged_at"], merged_at)

    agent_sets: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for repo_id, language, agent, tier in groups:
        agent_sets[(repo_id, language, tier)].add(agent)
    ambiguous_tier_two = {
        repo_id
        for (repo_id, _language, tier), agents in agent_sets.items()
        if tier == 2 and len(agents) > 1
    }

    eligible = []
    for candidate in groups.values():
        if (
            candidate["attributable_added_lines"] < 200
            or candidate["repository_id"] in ambiguous_tier_two
        ):
            continue
        candidate["pr_ids"] = sorted(candidate["pr_ids"])
        candidate["pr_numbers"] = sorted(candidate["pr_numbers"])
        candidate["pr_urls"] = sorted(candidate["pr_urls"])
        candidate["commit_shas"] = sorted(candidate["commit_shas"])
        candidate["pull_requests"] = []
        for pr in candidate.pop("_pull_requests").values():
            pr["commit_shas"] = sorted(pr["commit_shas"])
            candidate["pull_requests"].append(pr)
        candidate["pull_requests"].sort(key=lambda item: item["number"])
        candidate["selection_key"] = selection_key(
            candidate["repository_id"],
            candidate["language"],
            candidate["agent_family"],
            candidate["provenance_tier"],
        )
        eligible.append(candidate)

    strata: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for candidate in eligible:
        strata[
            (
                candidate["language"],
                candidate["agent_family"],
                candidate["provenance_tier"],
            )
        ].append(candidate)

    selected = []
    for candidates in strata.values():
        candidates.sort(key=lambda item: item["selection_key"])
        tier_one = [item for item in candidates if item["provenance_tier"] == 1]
        tier_two = [item for item in candidates if item["provenance_tier"] != 1]
        selected.extend(tier_one + tier_two[:cap])
    selected.sort(key=lambda item: item["selection_key"])
    return {
        "frame_version": 1,
        "status": "pending_default_branch_verification",
        "outcomes_consulted": False,
        "selection": {
            "strata": ["language", "agent_family", "provenance_tier"],
            "maximum_repositories_per_stratum": cap,
            "minimum_attributable_added_lines": 200,
            "ordering": "sha256(tier + NUL + language + NUL + agent_family + NUL + canonical_repository_id)",
        },
        "exclusions": {
            "multi_agent_tier_2_repositories": sorted(ambiguous_tier_two),
        },
        "candidates": selected,
    }
