from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from authorship.sourcegraph_authorship_candidates import (
    AuthorshipCandidateError,
    build_candidate_manifest,
    candidate_manifest_sha256,
    validate_candidate_manifest,
)


def _plan() -> dict:
    return {
        "authorship_unit_plan_sha256": "a" * 64,
        "outcomes_consulted": False,
        "h3_pairs": [
            {
                "repository_id": "paired/repo",
                "sourcegraph_name": "github.com/sg-evals/paired-repo",
                "agent_commit_oid": "1" * 40,
                "start_inclusive": "2025-01-01T00:00:00Z",
                "end_exclusive": "2025-07-01T00:00:00Z",
                "languages": ["Go", "Python"],
                "authorship_role": "human",
                "evidence_tier": "H3_contemporary_pre_adoption",
            }
        ],
        "h2_windows": [
            {
                "repository_id": "policy/repo",
                "sourcegraph_name": "github.com/sg-evals/policy-repo",
                "head_oid": "2" * 40,
                "policy_commit_oid": "3" * 40,
                "start_inclusive": "2026-01-01T00:00:00Z",
                "end_exclusive": "2026-07-01T00:00:00Z",
                "languages": ["Python"],
                "authorship_role": "human",
                "evidence_tier": "H2_policy_human",
            }
        ],
    }


def _commit(oid: str, parent: str, date: str) -> dict:
    return {
        "commit_oid": oid,
        "first_parent_oid": parent,
        "parent_count": 1,
        "is_root_commit": False,
        "committed_at": date,
    }


def test_candidate_manifest_freezes_every_window_commit_without_sampling() -> None:
    calls = []

    def fetcher(sourcegraph_name: str, head_oid: str, **window):
        calls.append((sourcegraph_name, head_oid, window))
        if "paired-repo" in sourcegraph_name:
            return [
                _commit("4" * 40, "5" * 40, "2025-02-01T00:00:00Z"),
                _commit("6" * 40, "7" * 40, "2025-06-01T00:00:00Z"),
            ]
        return [_commit("8" * 40, "9" * 40, "2026-03-01T00:00:00Z")]

    manifest = build_candidate_manifest(_plan(), commit_fetcher=fetcher)

    assert manifest["status"] == "frozen_before_diff_extraction"
    assert manifest["counts"] == {
        "H2_policy_human": 1,
        "H3_contemporary_pre_adoption": 2,
        "total_commits": 3,
    }
    assert [row["commit_oid"] for row in manifest["commits"]] == [
        "4" * 40,
        "6" * 40,
        "8" * 40,
    ]
    assert manifest["commits"][0]["languages"] == ["Go", "Python"]
    assert manifest["commits"][-1]["languages"] == ["Python"]
    assert len(calls) == 2
    assert len(manifest["window_frames"]) == 2
    assert validate_candidate_manifest(manifest, _plan(), commit_fetcher=fetcher) == []


def test_candidate_manifest_is_deterministic_and_content_bound() -> None:
    def fetcher(sourcegraph_name: str, *_args, **_kwargs) -> list[dict]:
        date = (
            "2025-02-01T00:00:00Z"
            if "paired-repo" in sourcegraph_name
            else "2026-02-01T00:00:00Z"
        )
        return [_commit("4" * 40, "5" * 40, date)]

    first = build_candidate_manifest(_plan(), commit_fetcher=fetcher)
    second = build_candidate_manifest(_plan(), commit_fetcher=fetcher)

    assert first == second
    tampered = deepcopy(first)
    tampered["commits"][0]["commit_oid"] = "f" * 40
    assert "candidate manifest SHA-256 does not match" in validate_candidate_manifest(
        tampered, _plan(), commit_fetcher=fetcher
    )


def test_candidate_manifest_rejects_duplicate_commit_roles() -> None:
    plan = _plan()
    plan["h2_windows"][0] = {
        **plan["h2_windows"][0],
        "repository_id": "paired/repo",
        "sourcegraph_name": "github.com/sg-evals/paired-repo",
        "head_oid": "1" * 40,
    }

    def fetcher(*_args, **_kwargs) -> list[dict]:
        return [_commit("4" * 40, "5" * 40, "2025-02-01T00:00:00Z")]

    with pytest.raises(AuthorshipCandidateError, match="multiple human tiers"):
        build_candidate_manifest(plan, commit_fetcher=fetcher)


def test_candidate_manifest_rejects_outcome_consulted_plan() -> None:
    plan = _plan()
    plan["outcomes_consulted"] = True

    with pytest.raises(AuthorshipCandidateError, match="outcome blind"):
        build_candidate_manifest(plan, commit_fetcher=lambda *_a, **_k: [])


def test_rehashed_candidate_manifest_cannot_omit_a_frozen_window_commit() -> None:
    def fetcher(sourcegraph_name: str, *_args, **_kwargs) -> list[dict]:
        year = "2025" if "paired-repo" in sourcegraph_name else "2026"
        return [
            _commit("4" * 40, "5" * 40, f"{year}-02-01T00:00:00Z"),
            _commit("6" * 40, "7" * 40, f"{year}-03-01T00:00:00Z"),
        ]

    manifest = build_candidate_manifest(_plan(), commit_fetcher=fetcher)
    forged = deepcopy(manifest)
    removed = forged["commits"].pop()
    frame = next(
        row
        for row in forged["window_frames"]
        if row["repository_id"] == removed["repository_id"]
    )
    frame["commit_oids"].remove(removed["commit_oid"])
    tier_counts = Counter(row["evidence_tier"] for row in forged["commits"])
    forged["counts"] = {
        "H2_policy_human": tier_counts["H2_policy_human"],
        "H3_contemporary_pre_adoption": tier_counts["H3_contemporary_pre_adoption"],
        "total_commits": len(forged["commits"]),
    }
    forged["candidate_manifest_sha256"] = candidate_manifest_sha256(forged)

    assert "candidate manifest does not match independent Sourcegraph frame" in (
        validate_candidate_manifest(forged, _plan(), commit_fetcher=fetcher)
    )


def test_rehashed_candidate_manifest_rejects_out_of_window_metadata() -> None:
    def fetcher(sourcegraph_name: str, *_args, **_kwargs) -> list[dict]:
        date = (
            "2025-02-01T00:00:00Z"
            if "paired-repo" in sourcegraph_name
            else "2026-02-01T00:00:00Z"
        )
        return [_commit("4" * 40, "5" * 40, date)]

    manifest = build_candidate_manifest(_plan(), commit_fetcher=fetcher)
    forged = deepcopy(manifest)
    forged["commits"][0]["committed_at"] = "2030-01-01T00:00:00Z"
    forged["candidate_manifest_sha256"] = candidate_manifest_sha256(forged)

    assert "candidate manifest commit falls outside its frozen window" in (
        validate_candidate_manifest(forged, _plan(), commit_fetcher=fetcher)
    )


def test_candidate_validation_requires_an_independent_sourcegraph_frame() -> None:
    def fetcher(sourcegraph_name: str, *_args, **_kwargs) -> list[dict]:
        date = (
            "2025-02-01T00:00:00Z"
            if "paired-repo" in sourcegraph_name
            else "2026-02-01T00:00:00Z"
        )
        return [_commit("4" * 40, "5" * 40, date)]

    manifest = build_candidate_manifest(_plan(), commit_fetcher=fetcher)

    assert "candidate manifest independent Sourcegraph frame is required" in (
        validate_candidate_manifest(manifest, _plan())
    )
