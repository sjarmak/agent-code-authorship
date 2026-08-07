from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pytest

from authorship.sourcegraph_authorship_execution import (
    AuthorshipExecutionError,
    authorship_execution_sha256,
    build_extraction_task,
    build_fixed_tasks,
    execute_authorship_units,
    extraction_shard_sha256,
    extract_task,
    load_execution_shards,
    run_task,
    scan_h3_repository,
    validate_execution_manifest,
    validate_extraction_shard,
)


def _plan() -> dict:
    return {
        "authorship_unit_plan_sha256": "a" * 64,
        "agent_commits": [
            {
                "repository_id": "agent/repo",
                "sourcegraph_name": "github.com/sg-evals/agent-repo",
                "commit_oid": "1" * 40,
                "observed_at": "2025-07-01T00:00:00Z",
                "languages": ["Go", "Python"],
                "authorship_role": "agent",
                "evidence_tier": "confirmed_agent_commit",
            }
        ],
        "h3_pairs": [
            {
                "repository_id": "paired/repo",
                "sourcegraph_name": "github.com/sg-evals/paired-repo",
                "agent_commit_oid": "2" * 40,
                "start_inclusive": "2025-01-01T00:00:00Z",
                "end_exclusive": "2025-07-01T00:00:00Z",
                "languages": ["Go", "Python"],
                "authorship_role": "human",
                "evidence_tier": "H3_contemporary_pre_adoption",
            }
        ],
    }


def _candidate(
    oid: str = "3" * 40,
    parent: str = "4" * 40,
    *,
    repository: str = "policy/repo",
    tier: str = "H2_policy_human",
    languages: tuple[str, ...] = ("Python",),
    committed_at: str = "2026-03-01T00:00:00Z",
) -> dict:
    return {
        "repository_id": repository,
        "sourcegraph_name": f"github.com/sg-evals/{repository.replace('/', '-')}",
        "commit_oid": oid,
        "first_parent_oid": parent,
        "parent_count": 1,
        "is_root_commit": False,
        "committed_at": committed_at,
        "languages": list(languages),
        "authorship_role": "human",
        "evidence_tier": tier,
        "window": {
            "start_inclusive": "2025-01-01T00:00:00Z",
            "end_exclusive": "2026-07-01T00:00:00Z",
        },
    }


def _unit(
    *,
    repository: str,
    role: str,
    tier: str,
    language: str,
    path_type: str,
    hunk: str,
) -> dict:
    return {
        "repository_id": repository,
        "commit_oid": "5" * 40,
        "path": f"{path_type}/{hunk}.{language.lower()}",
        "hunk_sha256": hunk * 64,
        "content_sha256": ("f" if role == "agent" else "e") * 64,
        "language": language,
        "path_type": path_type,
        "code_age_days": 0,
        "line_count": 3,
        "calendar_time": "2025-06-01T00:00:00Z",
        "authorship_role": role,
        "evidence_tier": tier,
    }


def _shard(task: dict, units: list[dict]) -> dict:
    from authorship.sourcegraph_authorship_exact import exact_hunk_units

    body = "+package main\n" if task["language"] == "Go" else "+value = 1\n"
    records = [
        {
            "path": unit["path"],
            "hunks": [
                {
                    "body": body * unit["line_count"],
                    "new_range": {
                        "start_line": 1,
                        "lines": unit["line_count"],
                    },
                }
            ],
        }
        for unit in units
    ]
    metadata = {
        "commit_oid": task["commit_oid"],
        "first_parent_oid": task.get("first_parent_oid", "9" * 40),
        "parent_count": task.get("parent_count", 1),
        "is_root_commit": task.get("is_root_commit", False),
        "committed_at": task["committed_at"],
    }
    exact_units = exact_hunk_units(
        repository_id=task["repository_id"],
        sourcegraph_name=task["sourcegraph_name"],
        commit_oid=task["commit_oid"],
        first_parent_oid=metadata["first_parent_oid"],
        committed_at=task["committed_at"],
        language=task["language"],
        records=records,
        authorship_role=task["authorship_role"],
        evidence_tier=task["evidence_tier"],
    )
    document = {
        "extraction_shard_version": 1,
        "task": task,
        "commit_metadata": metadata,
        "raw_records": records,
        "units": exact_units,
        "counts": {"raw_records": len(records), "units": len(exact_units)},
        "status": "complete",
    }
    return {**document, "extraction_shard_sha256": extraction_shard_sha256(document)}


def test_fixed_tasks_cover_all_agent_languages_and_every_h2_candidate() -> None:
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [
            _candidate(),
            _candidate(
                "6" * 40,
                "7" * 40,
                repository="paired/repo",
                tier="H3_contemporary_pre_adoption",
                languages=("Go", "Python"),
            ),
        ],
    }

    tasks = build_fixed_tasks(_plan(), candidates)

    assert [(task["authorship_role"], task["language"]) for task in tasks] == [
        ("agent", "Go"),
        ("agent", "Python"),
        ("human", "Python"),
    ]
    assert all(len(task["task_id"]) == 64 for task in tasks)


def test_extract_task_resolves_agent_parent_and_materializes_exact_units() -> None:
    task = build_extraction_task(
        {
            **_plan()["agent_commits"][0],
            "language": "Go",
            "resolve_commit_metadata": True,
        }
    )
    metadata_calls = 0

    def metadata_fetcher(_name: str, _oid: str) -> dict:
        nonlocal metadata_calls
        metadata_calls += 1
        return {
            "commit_oid": "1" * 40,
            "first_parent_oid": "9" * 40,
            "parent_count": 1,
            "is_root_commit": False,
            "committed_at": "2025-07-01T00:00:00Z",
        }

    records = [
        {
            "path": "main.go",
            "hunks": [
                {
                    "body": "+package main\n",
                    "new_range": {"start_line": 1, "lines": 1},
                }
            ],
        }
    ]
    shard = extract_task(
        task,
        metadata_fetcher=metadata_fetcher,
        records_fetcher=lambda *_args, **_kwargs: records,
    )

    assert metadata_calls == 1
    assert shard["status"] == "complete"
    assert shard["counts"] == {"raw_records": 1, "units": 1}
    assert shard["units"][0]["first_parent_oid"] == "9" * 40
    assert validate_extraction_shard(shard, task) == []


def test_extract_task_rejects_timestamp_drift() -> None:
    task = build_extraction_task(
        {
            **_plan()["agent_commits"][0],
            "language": "Go",
            "resolve_commit_metadata": True,
        }
    )

    with pytest.raises(AuthorshipExecutionError, match="timestamp"):
        extract_task(
            task,
            metadata_fetcher=lambda *_args: {
                "commit_oid": "1" * 40,
                "first_parent_oid": "9" * 40,
                "parent_count": 1,
                "is_root_commit": False,
                "committed_at": "2025-07-02T00:00:00Z",
            },
            records_fetcher=lambda *_args, **_kwargs: [],
        )


def test_rehashed_shard_cannot_forge_derived_unit_content() -> None:
    task = build_extraction_task(
        {
            **_plan()["agent_commits"][0],
            "language": "Go",
            "resolve_commit_metadata": True,
        }
    )
    shard = extract_task(
        task,
        metadata_fetcher=lambda *_args: {
            "commit_oid": "1" * 40,
            "first_parent_oid": "9" * 40,
            "parent_count": 1,
            "is_root_commit": False,
            "committed_at": "2025-07-01T00:00:00Z",
        },
        records_fetcher=lambda *_args, **_kwargs: [
            {
                "path": "main.go",
                "hunks": [
                    {
                        "body": "+package main\n",
                        "new_range": {"start_line": 1, "lines": 1},
                    }
                ],
            }
        ],
    )
    forged = deepcopy(shard)
    forged["units"][0]["content_sha256"] = "0" * 64
    forged["extraction_shard_sha256"] = extraction_shard_sha256(forged)

    assert "extraction shard units do not match raw records" in (
        validate_extraction_shard(forged, task)
    )
    invalid_metadata = deepcopy(shard)
    invalid_metadata["commit_metadata"]["committed_at"] = "2030-01-01T00:00:00Z"
    invalid_metadata["extraction_shard_sha256"] = extraction_shard_sha256(
        invalid_metadata
    )
    assert "extraction shard commit metadata is invalid" in (
        validate_extraction_shard(invalid_metadata, task)
    )
    incomplete = deepcopy(shard)
    incomplete["status"] = "incomplete"
    incomplete["extraction_shard_sha256"] = extraction_shard_sha256(incomplete)
    assert "extraction shard is incomplete" in (
        validate_extraction_shard(incomplete, task)
    )
    assert "extraction shard records are invalid" in (validate_extraction_shard({}, {}))


def test_extract_task_uses_pinned_blob_materialization_for_root_commit() -> None:
    task = build_extraction_task(
        {
            **_plan()["agent_commits"][0],
            "language": "Python",
            "resolve_commit_metadata": True,
        }
    )
    root_calls = 0

    def root_fetcher(*_args) -> list[dict]:
        nonlocal root_calls
        root_calls += 1
        return []

    shard = extract_task(
        task,
        metadata_fetcher=lambda *_args: {
            "commit_oid": "1" * 40,
            "first_parent_oid": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            "parent_count": 0,
            "is_root_commit": True,
            "committed_at": "2025-07-01T00:00:00Z",
        },
        records_fetcher=lambda *_args: pytest.fail("diff fetcher must not run"),
        root_records_fetcher=root_fetcher,
    )

    assert root_calls == 1
    assert shard["counts"] == {"raw_records": 0, "units": 0}


def test_run_task_reuses_only_a_valid_content_bound_shard(tmp_path) -> None:
    task = build_extraction_task(
        {
            **_candidate(),
            "language": "Python",
            "resolve_commit_metadata": False,
        }
    )
    calls = 0

    def runner(value: dict) -> dict:
        nonlocal calls
        calls += 1
        return _shard(value, [])

    first = run_task(task, tmp_path, task_runner=runner)
    second = run_task(task, tmp_path, task_runner=runner)

    assert first["extraction_shard_sha256"] == second["extraction_shard_sha256"]
    assert first["units"] == second["units"]
    assert calls == 1
    assert first["reused"] is False
    assert second["reused"] is True


def test_h3_scan_stops_at_without_replacement_stratum_capacity() -> None:
    agent_units = [
        _unit(
            repository="paired/repo",
            role="agent",
            tier="confirmed_agent_commit",
            language="Go",
            path_type="source",
            hunk="a",
        ),
        _unit(
            repository="paired/repo",
            role="agent",
            tier="confirmed_agent_commit",
            language="Go",
            path_type="test",
            hunk="b",
        ),
    ]
    candidates = [
        _candidate(
            str(index) * 40,
            str(index + 1) * 40,
            repository="paired/repo",
            tier="H3_contemporary_pre_adoption",
            languages=("Go", "Python"),
            committed_at=f"2025-06-0{index}T00:00:00Z",
        )
        for index in (3, 2, 1)
    ]
    calls = []

    def runner(task: dict) -> dict:
        calls.append((task["commit_oid"], task["language"]))
        units = [
            _unit(
                repository="paired/repo",
                role="human",
                tier="H3_contemporary_pre_adoption",
                language="Go",
                path_type="source",
                hunk=str(len(calls)),
            )
        ]
        if len(calls) == 2:
            units.append(
                _unit(
                    repository="paired/repo",
                    role="human",
                    tier="H3_contemporary_pre_adoption",
                    language="Go",
                    path_type="test",
                    hunk="d",
                )
            )
        return _shard(task, units)

    result = scan_h3_repository(
        "paired/repo",
        candidates,
        agent_units,
        task_runner=runner,
    )

    assert calls == [("3" * 40, "Go"), ("2" * 40, "Go")]
    assert result["capacity_satisfied"] is True
    assert result["required_capacity"] == {
        "Go|source|0": 1,
        "Go|test|0": 1,
    }
    assert result["observed_capacity"] == {
        "Go|source|0": 2,
        "Go|test|0": 1,
    }
    assert Counter(unit["path_type"] for unit in result["units"]) == {
        "source": 2,
        "test": 1,
    }


def test_h3_scan_reports_exhausted_capacity_without_inventing_units() -> None:
    agent_units = [
        _unit(
            repository="paired/repo",
            role="agent",
            tier="confirmed_agent_commit",
            language="Go",
            path_type="test",
            hunk="a",
        )
    ]

    result = scan_h3_repository(
        "paired/repo",
        [_candidate(repository="paired/repo", tier="H3_contemporary_pre_adoption")],
        agent_units,
        task_runner=lambda task: _shard(task, []),
    )

    assert result["capacity_satisfied"] is False
    assert result["exhausted_window"] is True


def test_end_to_end_execution_checkpoints_fixed_and_adaptive_tasks(tmp_path) -> None:
    plan = _plan()
    paired_agent = {
        **plan["agent_commits"][0],
        "repository_id": "paired/repo",
        "sourcegraph_name": "github.com/sg-evals/paired-repo",
        "commit_oid": "2" * 40,
        "languages": ["Go"],
    }
    plan["agent_commits"] = [paired_agent]
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [
            _candidate(),
            _candidate(
                "6" * 40,
                "7" * 40,
                repository="paired/repo",
                tier="H3_contemporary_pre_adoption",
                languages=("Go", "Python"),
            ),
        ],
    }

    def runner(task: dict) -> dict:
        units = []
        if task["repository_id"] == "paired/repo" and task["language"] == "Go":
            unit = _unit(
                repository="paired/repo",
                role=task["authorship_role"],
                tier=task["evidence_tier"],
                language="Go",
                path_type="source",
                hunk="a" if task["authorship_role"] == "agent" else "b",
            )
            unit["commit_oid"] = task["commit_oid"]
            units = [unit]
        return _shard(task, units)

    manifest = execute_authorship_units(
        plan,
        candidates,
        tmp_path,
        max_workers=2,
        task_runner=runner,
    )

    assert manifest["status"] == "complete", manifest["failures"]
    assert manifest["counts"] == {
        "fixed_tasks": 2,
        "h3_scanned_tasks": 1,
        "successful_tasks": 3,
        "failed_tasks": 0,
        "agent_units": 1,
        "H2_units": 0,
        "H3_candidate_units": 1,
    }
    assert manifest["h3_repositories"][0]["capacity_satisfied"] is True
    assert validate_execution_manifest(manifest, plan, candidates, tmp_path) == []
    forged = deepcopy(manifest)
    forged["h3_repositories"][0]["capacity_satisfied"] = False
    forged["authorship_execution_sha256"] = authorship_execution_sha256(forged)
    assert "authorship execution H3 adaptive scan contract does not match" in (
        validate_execution_manifest(forged, plan, candidates, tmp_path)
    )
    wrong_counts = deepcopy(manifest)
    wrong_counts["counts"]["agent_units"] += 1
    wrong_counts["authorship_execution_sha256"] = authorship_execution_sha256(
        wrong_counts
    )
    assert "authorship execution counts do not match" in (
        validate_execution_manifest(wrong_counts, plan, candidates, tmp_path)
    )
    wrong_status = deepcopy(manifest)
    wrong_status["status"] = "incomplete"
    wrong_status["authorship_execution_sha256"] = authorship_execution_sha256(
        wrong_status
    )
    assert "authorship execution status does not match failures" in (
        validate_execution_manifest(wrong_status, plan, candidates, tmp_path)
    )


def test_load_execution_shards_requires_a_fully_valid_execution(tmp_path) -> None:
    plan = _plan()
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [_candidate()],
    }
    manifest = execute_authorship_units(
        plan,
        candidates,
        tmp_path,
        max_workers=1,
        task_runner=lambda task: _shard(task, []),
    )

    shards = load_execution_shards(manifest, plan, candidates, tmp_path)

    assert [shard["task"]["task_id"] for shard in shards] == [
        reference["task_id"]
        for reference in [
            *manifest["fixed_shards"],
            *[
                shard
                for repository in manifest["h3_repositories"]
                for shard in repository["shards"]
            ],
        ]
    ]
    forged = deepcopy(manifest)
    forged["fixed_shards"][0]["task_id"] = "f" * 64
    forged["authorship_execution_sha256"] = authorship_execution_sha256(forged)
    assert any(
        "execution shard reference does not match content" in error
        for error in validate_execution_manifest(forged, plan, candidates, tmp_path)
    )

    shard_path = tmp_path / manifest["fixed_shards"][0]["shard"]
    shard_path.write_text("{}")
    with pytest.raises(AuthorshipExecutionError, match="invalid"):
        load_execution_shards(manifest, plan, candidates, tmp_path)


def test_rehashed_execution_cannot_omit_a_required_fixed_task(tmp_path) -> None:
    plan = _plan()
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [_candidate()],
    }
    manifest = execute_authorship_units(
        plan,
        candidates,
        tmp_path,
        max_workers=1,
        task_runner=lambda task: _shard(task, []),
    )
    forged = deepcopy(manifest)
    forged["fixed_shards"].pop()
    forged["counts"]["fixed_tasks"] -= 1
    forged["counts"]["successful_tasks"] -= 1
    forged["authorship_execution_sha256"] = authorship_execution_sha256(forged)

    assert "authorship execution fixed task frame does not match" in (
        validate_execution_manifest(forged, plan, candidates, tmp_path)
    )


def test_rehashed_execution_cannot_omit_an_h3_repository_scan(tmp_path) -> None:
    plan = _plan()
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [_candidate()],
    }
    manifest = execute_authorship_units(
        plan,
        candidates,
        tmp_path,
        max_workers=1,
        task_runner=lambda task: _shard(task, []),
    )
    forged = deepcopy(manifest)
    forged["h3_repositories"] = []
    forged["authorship_execution_sha256"] = authorship_execution_sha256(forged)

    assert "authorship execution H3 repository frame does not match" in (
        validate_execution_manifest(forged, plan, candidates, tmp_path)
    )


def test_end_to_end_execution_records_failures_and_stays_incomplete(tmp_path) -> None:
    candidates = {
        "candidate_manifest_sha256": "b" * 64,
        "commits": [_candidate()],
    }

    def runner(task: dict) -> dict:
        if task["authorship_role"] == "agent":
            raise RuntimeError("synthetic Sourcegraph failure")
        return _shard(task, [])

    manifest = execute_authorship_units(
        _plan(),
        candidates,
        tmp_path,
        max_workers=2,
        task_runner=runner,
    )

    assert manifest["status"] == "incomplete"
    assert manifest["counts"]["failed_tasks"] == 2
    assert all(
        "synthetic Sourcegraph failure" in row["error"] for row in manifest["failures"]
    )
    with pytest.raises(AuthorshipExecutionError, match="not complete"):
        load_execution_shards(manifest, _plan(), candidates, tmp_path)
