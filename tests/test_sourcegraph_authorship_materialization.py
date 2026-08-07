from __future__ import annotations

from copy import deepcopy

import pytest

from authorship.sourcegraph_authorship_execution import extraction_shard_sha256
from authorship.sourcegraph_authorship_exact import exact_hunk_units
from authorship.sourcegraph_authorship_materialization import (
    AuthorshipMaterializationError,
    match_h3_units,
    materialize_authorship_units,
    validate_authorship_materialization,
)


def _unit(
    *,
    repository: str,
    commit: str,
    hunk: str,
    content: str,
    role: str,
    tier: str,
    language: str = "Go",
    path_type: str = "source",
    lines: int = 3,
    time: str = "2025-06-01T00:00:00Z",
) -> dict:
    return {
        "repository_id": repository,
        "sourcegraph_name": f"github.com/sg-evals/{repository.replace('/', '-')}",
        "commit_oid": commit,
        "first_parent_oid": "9" * 40,
        "committed_at": time,
        "calendar_time": time,
        "path": f"{path_type}/{hunk}.go",
        "hunk_index": 0,
        "new_range": {"start_line": 1, "lines": lines},
        "line_numbers": list(range(1, lines + 1)),
        "hunk_sha256": hunk * 64,
        "content_sha256": content * 64,
        "sourcegraph_record_sha256": "d" * 64,
        "language": language,
        "path_type": path_type,
        "code_age_days": 0,
        "line_count": lines,
        "authorship_role": role,
        "evidence_tier": tier,
        "feature_values": {"log_lines": 1.0},
    }


def _task(unit: dict, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "repository_id": unit["repository_id"],
        "sourcegraph_name": unit["sourcegraph_name"],
        "commit_oid": unit["commit_oid"],
        "committed_at": unit["committed_at"],
        "language": unit["language"],
        "authorship_role": unit["authorship_role"],
        "evidence_tier": unit["evidence_tier"],
        "resolve_commit_metadata": False,
        "first_parent_oid": unit["first_parent_oid"],
        "parent_count": 1,
        "is_root_commit": False,
    }


def _shard(unit: dict, task_id: str) -> dict:
    task = _task(unit, task_id)
    marker = unit["content_sha256"][0]
    records = [
        {
            "path": unit["path"],
            "hunks": [
                {
                    "body": f"+// marker {marker}\n" * unit["line_count"],
                    "new_range": unit["new_range"],
                }
            ],
        }
    ]
    exact_units = exact_hunk_units(
        repository_id=unit["repository_id"],
        sourcegraph_name=unit["sourcegraph_name"],
        commit_oid=unit["commit_oid"],
        first_parent_oid=unit["first_parent_oid"],
        committed_at=unit["committed_at"],
        language=unit["language"],
        records=records,
        authorship_role=unit["authorship_role"],
        evidence_tier=unit["evidence_tier"],
    )
    document = {
        "extraction_shard_version": 1,
        "task": task,
        "commit_metadata": {
            "commit_oid": unit["commit_oid"],
            "first_parent_oid": unit["first_parent_oid"],
            "parent_count": 1,
            "is_root_commit": False,
            "committed_at": unit["committed_at"],
        },
        "raw_records": records,
        "units": exact_units,
        "counts": {"raw_records": len(records), "units": len(exact_units)},
        "status": "complete",
    }
    return {**document, "extraction_shard_sha256": extraction_shard_sha256(document)}


def _plan() -> dict:
    return {
        "authorship_unit_plan_sha256": "a" * 64,
        "exclusions": {"target_overlap_repository_ids": ["target/repo"]},
        "h3_pairs": [
            {
                "repository_id": "paired/repo",
                "agent_commit_oid": "1" * 40,
                "end_exclusive": "2025-07-01T00:00:00Z",
            }
        ],
    }


def _execution(task_ids: list[str]) -> dict:
    return {
        "status": "complete",
        "authorship_execution_sha256": "b" * 64,
        "fixed_shards": [{"task_id": task_ids[0]}, {"task_id": task_ids[1]}],
        "h3_repositories": [
            {"repository_id": "paired/repo", "shards": [{"task_id": task_ids[2]}]}
        ],
    }


def test_h3_matching_is_exact_stratum_calendar_first_and_without_replacement() -> None:
    agents = [
        _unit(
            repository="paired/repo",
            commit="1" * 40,
            hunk="a",
            content="1",
            role="agent",
            tier="confirmed_agent_commit",
            lines=10,
            time="2025-07-01T00:00:00Z",
        ),
        _unit(
            repository="paired/repo",
            commit="1" * 40,
            hunk="b",
            content="2",
            role="agent",
            tier="confirmed_agent_commit",
            lines=2,
            time="2025-07-01T00:00:00Z",
        ),
    ]
    humans = [
        _unit(
            repository="paired/repo",
            commit="2" * 40,
            hunk="c",
            content="3",
            role="human",
            tier="H3_contemporary_pre_adoption",
            lines=2,
            time="2025-06-30T00:00:00Z",
        ),
        _unit(
            repository="paired/repo",
            commit="3" * 40,
            hunk="d",
            content="4",
            role="human",
            tier="H3_contemporary_pre_adoption",
            lines=10,
            time="2025-06-01T00:00:00Z",
        ),
    ]

    result = match_h3_units(agents, humans, _plan()["h3_pairs"])

    assert len(result["matches"]) == 2
    assert len({row["human_hunk_sha256"] for row in result["matches"]}) == 2
    assert result["matches"][0]["human_hunk_sha256"] == "c" * 64
    assert result["matches"][0]["calendar_distance_days"] == 1.0
    assert result["unmatched_agent_hunk_sha256s"] == []


def test_materialization_drops_cross_role_content_before_matching() -> None:
    agent = _unit(
        repository="paired/repo",
        commit="1" * 40,
        hunk="a",
        content="f",
        role="agent",
        tier="confirmed_agent_commit",
        time="2025-07-01T00:00:00Z",
    )
    h2 = _unit(
        repository="policy/repo",
        commit="2" * 40,
        hunk="b",
        content="f",
        role="human",
        tier="H2_policy_human",
    )
    h3 = _unit(
        repository="paired/repo",
        commit="3" * 40,
        hunk="c",
        content="e",
        role="human",
        tier="H3_contemporary_pre_adoption",
    )
    shards = [_shard(agent, "1" * 64), _shard(h2, "2" * 64), _shard(h3, "3" * 64)]

    result = materialize_authorship_units(
        _plan(), _execution(["1" * 64, "2" * 64, "3" * 64]), shards
    )

    assert result["counts"]["agent_units"] == 0
    assert result["counts"]["H2_units"] == 0
    assert result["counts"]["H3_units"] == 0
    assert result["overlap_audit"]["cross_role_content_hash_count"] == 1
    assert result["overlap_audit"]["dropped_agent_units"] == 1
    assert result["overlap_audit"]["dropped_human_candidate_units"] == 1
    assert (
        validate_authorship_materialization(
            result, _plan(), _execution(["1" * 64, "2" * 64, "3" * 64])
        )
        == []
    )


def test_materialization_retains_valid_h2_and_matched_h3_units() -> None:
    agent = _unit(
        repository="paired/repo",
        commit="1" * 40,
        hunk="a",
        content="1",
        role="agent",
        tier="confirmed_agent_commit",
        time="2025-07-01T00:00:00Z",
    )
    h2 = _unit(
        repository="policy/repo",
        commit="2" * 40,
        hunk="b",
        content="2",
        role="human",
        tier="H2_policy_human",
    )
    h3 = _unit(
        repository="paired/repo",
        commit="3" * 40,
        hunk="c",
        content="3",
        role="human",
        tier="H3_contemporary_pre_adoption",
        time="2025-06-01T00:00:00Z",
    )
    task_ids = ["1" * 64, "2" * 64, "3" * 64]
    execution = _execution(task_ids)

    result = materialize_authorship_units(
        _plan(),
        execution,
        [_shard(agent, task_ids[0]), _shard(h2, task_ids[1]), _shard(h3, task_ids[2])],
    )

    assert result["status"] == "complete"
    assert result["counts"] == {
        "agent_units": 1,
        "H2_units": 1,
        "H3_units": 1,
        "H3_matches": 1,
        "H3_unmatched_agent_units": 0,
        "cross_role_collision_hashes": 0,
    }
    assert result["role_audit"]["temporal_violation_count"] == 0
    assert result["role_audit"]["postfilter_overlap_errors"] == []
    assert validate_authorship_materialization(result, _plan(), execution) == []


def test_materialization_rejects_h3_human_unit_at_or_after_adoption() -> None:
    agent = _unit(
        repository="paired/repo",
        commit="1" * 40,
        hunk="a",
        content="1",
        role="agent",
        tier="confirmed_agent_commit",
        time="2025-07-01T00:00:00Z",
    )
    h2 = _unit(
        repository="policy/repo",
        commit="2" * 40,
        hunk="b",
        content="2",
        role="human",
        tier="H2_policy_human",
    )
    h3 = _unit(
        repository="paired/repo",
        commit="3" * 40,
        hunk="c",
        content="3",
        role="human",
        tier="H3_contemporary_pre_adoption",
        time="2025-07-01T00:00:00Z",
    )
    task_ids = ["1" * 64, "2" * 64, "3" * 64]

    with pytest.raises(AuthorshipMaterializationError, match="temporal"):
        materialize_authorship_units(
            _plan(),
            _execution(task_ids),
            [
                _shard(agent, task_ids[0]),
                _shard(h2, task_ids[1]),
                _shard(h3, task_ids[2]),
            ],
        )


def test_materialization_hash_detects_tampering() -> None:
    empty_execution = {
        "status": "complete",
        "authorship_execution_sha256": "b" * 64,
        "fixed_shards": [],
        "h3_repositories": [],
    }
    result = materialize_authorship_units(
        {**_plan(), "h3_pairs": []}, empty_execution, []
    )
    tampered = deepcopy(result)
    tampered["counts"]["agent_units"] = 1

    assert (
        "authorship materialization SHA-256 does not match"
        in validate_authorship_materialization(
            tampered, {**_plan(), "h3_pairs": []}, empty_execution
        )
    )


def test_materialization_validator_fails_closed_on_invalid_shapes_and_status() -> None:
    plan = {**_plan(), "h3_pairs": []}
    execution = {
        "status": "complete",
        "authorship_execution_sha256": "b" * 64,
        "fixed_shards": [],
        "h3_repositories": [],
    }
    result = materialize_authorship_units(plan, execution, [])

    invalid_units = {**result, "agent_units": None}
    assert "authorship materialization units are invalid" in (
        validate_authorship_materialization(invalid_units, plan, execution)
    )
    invalid_tiers = {**result, "human_units": {}}
    assert "authorship materialization human tiers are invalid" in (
        validate_authorship_materialization(invalid_tiers, plan, execution)
    )
    incomplete = {**result, "status": "incomplete"}
    assert "authorship materialization is incomplete" in (
        validate_authorship_materialization(incomplete, plan, execution)
    )
