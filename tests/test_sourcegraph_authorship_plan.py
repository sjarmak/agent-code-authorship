from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from authorship.sourcegraph_authorship_plan import (
    AuthorshipPlanError,
    authorship_unit_plan_sha256,
    build_authorship_unit_plan,
    validate_authorship_unit_plan,
)

CUTOFF = "2026-07-24T23:59:59Z"


def _protocol() -> dict:
    return {
        "protocol_sha256": "a" * 64,
        "snapshot": {"cutoff": CUTOFF},
        "adoption": {"event_windows_days": {"primary": [-180, 180]}},
        "selection": {
            "outcome_blind": True,
            "classifier_and_survival_outputs_forbidden_during_selection": True,
        },
    }


def _freeze() -> dict:
    return {
        "cohort_freeze_sha256": "b" * 64,
        "outcomes_consulted": False,
        "cohorts": {
            "human_evidence": {
                "H2_policy_human": {
                    "repositories": [
                        {
                            "repository_id": "policy/python",
                            "language": "Python",
                            "policy_commit_oid": "8" * 40,
                            "policy_effective_at": "2026-07-01T00:00:00Z",
                        },
                        {
                            "repository_id": "target/repo",
                            "language": "Go",
                            "policy_commit_oid": "9" * 40,
                            "policy_effective_at": "2023-08-09T00:00:00Z",
                        },
                        {
                            "repository_id": "policy/rust",
                            "language": "Rust",
                            "policy_commit_oid": "7" * 40,
                            "policy_effective_at": "2026-01-01T00:00:00Z",
                        },
                    ]
                },
                "H3_pre_adoption_proxy": {
                    "contemporary_pre_adoption": {
                        "repository_ids": ["paired/repo", "target/repo"]
                    }
                },
            }
        },
    }


def _agent_catalog() -> dict:
    return {
        "catalog_sha256": "c" * 64,
        "outcomes_consulted": False,
        "commits": [
            {
                "repository_id": "paired/repo",
                "sourcegraph_name": "github.com/sg-evals/paired-repo",
                "commit_oid": "1" * 40,
                "observed_at": "2025-07-13T15:40:17Z",
                "decision": "accept_confirmed",
                "evidence_tier": "confirmed",
                "primary_scope_eligible": True,
            },
            {
                "repository_id": "agent/only",
                "sourcegraph_name": "github.com/sg-evals/agent-only",
                "commit_oid": "2" * 40,
                "observed_at": "2026-02-01T00:00:00Z",
                "decision": "accept_confirmed",
                "evidence_tier": "confirmed",
                "primary_scope_eligible": True,
            },
            {
                "repository_id": "target/repo",
                "sourcegraph_name": "github.com/sg-evals/target-repo",
                "commit_oid": "3" * 40,
                "observed_at": "2026-03-01T00:00:00Z",
                "decision": "accept_confirmed",
                "evidence_tier": "confirmed",
                "primary_scope_eligible": True,
            },
            {
                "repository_id": "observed/repo",
                "sourcegraph_name": "github.com/sg-evals/observed-repo",
                "commit_oid": "4" * 40,
                "observed_at": "2026-04-01T00:00:00Z",
                "decision": "accept_observed",
                "evidence_tier": "observed",
                "primary_scope_eligible": False,
            },
        ],
    }


def _targets() -> dict:
    return {
        "manifest_version": 1,
        "repositories": [{"id": "TARGET/REPO"}],
    }


def _index() -> dict:
    names = {
        "paired/repo": "github.com/sg-evals/paired-repo",
        "agent/only": "github.com/sg-evals/agent-only",
        "target/repo": "github.com/sg-evals/target-repo",
        "policy/python": "github.com/sg-evals/policy-python",
        "policy/rust": "github.com/sg-evals/policy-rust",
    }
    return {
        "manifest_version": 3,
        "repositories": [
            {
                "canonical_repository_id": repository,
                "cutoff_commit": str(index + 1) * 40,
                "sourcegraph": {
                    "selected_name": sourcegraph_name,
                    "transport_status": "ready_mirror",
                },
            }
            for index, (repository, sourcegraph_name) in enumerate(names.items())
        ],
    }


def _build() -> dict:
    return build_authorship_unit_plan(
        _protocol(),
        _freeze(),
        _agent_catalog(),
        _targets(),
        _index(),
    )


def test_plan_excludes_target_overlap_and_non_confirmed_agent_rows() -> None:
    plan = _build()

    assert plan["status"] == "frozen_before_outcome_extraction"
    assert [row["repository_id"] for row in plan["agent_commits"]] == [
        "agent/only",
        "paired/repo",
    ]
    assert [row["repository_id"] for row in plan["h3_pairs"]] == ["paired/repo"]
    assert [row["repository_id"] for row in plan["h2_windows"]] == ["policy/python"]
    assert plan["counts"] == {
        "agent_commits": 2,
        "h3_pairs": 1,
        "h2_repositories": 1,
        "excluded_target_overlap_repositories": 1,
    }
    assert plan["exclusions"]["target_overlap_repository_ids"] == ["target/repo"]
    assert plan["target_repository_ids"] == ["target/repo"]
    assert validate_authorship_unit_plan(plan, _targets()) == []
    assert "authorship unit plan pinned target manifest is required" in (
        validate_authorship_unit_plan(plan)
    )


def test_rehashed_plan_cannot_admit_a_target_repository() -> None:
    plan = _build()
    forged = deepcopy(plan)
    forged["agent_commits"][0]["repository_id"] = "target/repo"
    forged["exclusions"]["target_overlap_repository_ids"] = []
    forged["counts"]["excluded_target_overlap_repositories"] = 0
    forged["authorship_unit_plan_sha256"] = authorship_unit_plan_sha256(forged)

    errors = validate_authorship_unit_plan(forged, _targets())

    assert "authorship unit plan contains target-overlap model populations" in errors
    assert "authorship unit plan target exclusions do not match overlap" in errors


def test_plan_uses_temporally_disjoint_frozen_windows() -> None:
    plan = _build()
    pair = plan["h3_pairs"][0]
    policy = plan["h2_windows"][0]

    assert pair["end_exclusive"] == "2025-07-13T15:40:17Z"
    assert pair["start_inclusive"] == "2025-01-14T15:40:17Z"
    assert pair["agent_commit_oid"] == "1" * 40
    assert pair["languages"] == ["Go", "Python"]
    assert policy["start_inclusive"] == "2026-07-01T00:00:00Z"
    assert policy["end_exclusive"] == CUTOFF
    assert policy["languages"] == ["Python"]

    start = datetime.fromisoformat(pair["start_inclusive"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(pair["end_exclusive"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == pytest.approx(180 * 24 * 60 * 60)
    assert start.tzinfo == timezone.utc


def test_plan_is_deterministic_and_content_bound() -> None:
    first = _build()
    second = _build()

    assert first == second
    tampered = deepcopy(first)
    tampered["agent_commits"][0]["commit_oid"] = "f" * 40
    assert (
        "authorship unit plan SHA-256 does not match"
        in validate_authorship_unit_plan(tampered)
    )


def test_plan_fails_closed_when_required_sourcegraph_mapping_is_missing() -> None:
    index = _index()
    index["repositories"] = [
        row
        for row in index["repositories"]
        if row["canonical_repository_id"] != "policy/python"
    ]

    with pytest.raises(AuthorshipPlanError, match="policy/python.*Sourcegraph mapping"):
        build_authorship_unit_plan(
            _protocol(),
            _freeze(),
            _agent_catalog(),
            _targets(),
            index,
        )


def test_plan_rejects_outcome_consultation() -> None:
    catalog = _agent_catalog()
    catalog["outcomes_consulted"] = True

    with pytest.raises(AuthorshipPlanError, match="outcome blind"):
        build_authorship_unit_plan(
            _protocol(),
            _freeze(),
            catalog,
            _targets(),
            _index(),
        )
