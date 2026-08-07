import copy
import json
from pathlib import Path

import pytest

from authorship.features import NAMES
from authorship.sourcegraph_era_execution import (
    _sha256,
    execute_era_panel_plan,
    fetch_comparison_raw_diff,
    fetch_period_boundaries,
    load_complete_period_results,
    period_result_sha256,
)


def _plan():
    return {
        "era_panel_plan_version": 1,
        "era_panel_plan_sha256": "1" * 64,
        "required_periods": [8100, 8102],
        "repositories": [
            {
                "repository_id": "org/repo",
                "language": "Python",
                "sourcegraph_name": "github.com/sg-evals/org-repo",
                "cutoff_commit": "a" * 40,
            }
        ],
    }


def test_sourcegraph_boundary_adapter_pins_quarter_dates():
    calls = []

    def fake_api(_graphql_query, **variables):
        calls.append((_graphql_query, variables))
        return {
            "repository": {
                "commit": {
                    "b0": {"nodes": [{"oid": "0" * 40}]},
                    "b1": {"nodes": [{"oid": "1" * 40}]},
                    "b2": {"nodes": [{"oid": "2" * 40}]},
                    "b3": {"nodes": [{"oid": "3" * 40}]},
                }
            }
        }

    boundaries = fetch_period_boundaries(
        "github.com/sg-evals/org-repo",
        "a" * 40,
        [8100, 8102],
        api_runner=fake_api,
    )

    assert boundaries == {
        8100: ("0" * 40, "1" * 40),
        8102: ("2" * 40, "3" * 40),
    }
    assert "2025-01-01T00:00:00Z" in calls[0][0]
    assert "2025-10-01T00:00:00Z" in calls[0][0]
    assert calls[0][1]["repo"] == "github.com/sg-evals/org-repo"


def test_sourcegraph_boundary_adapter_marks_precreation_period_unobserved():
    def fake_api(_graphql_query, **_variables):
        return {
            "repository": {
                "commit": {
                    "b0": {"nodes": []},
                    "b1": {"nodes": [{"oid": "1" * 40}]},
                }
            }
        }

    boundaries = fetch_period_boundaries(
        "github.com/sg-evals/org-repo",
        "a" * 40,
        [8100],
        api_runner=fake_api,
    )

    assert boundaries == {8100: None}


def test_sourcegraph_comparison_adapter_paginates_every_file_diff():
    calls = []

    def fake_api(_graphql_query, **variables):
        calls.append(variables)
        after = variables.get("after")
        page = {
            "rawDiff": "first\n" if after is None else "second\n",
            "pageInfo": {
                "hasNextPage": after is None,
                "endCursor": "cursor" if after is None else None,
            },
        }
        return {"repository": {"commit": {"diff": {"fileDiffs": page}}}}

    raw_diff = fetch_comparison_raw_diff(
        "github.com/sg-evals/org-repo",
        "0" * 40,
        "1" * 40,
        "Python",
        api_runner=fake_api,
    )

    assert raw_diff == "first\nsecond\n"
    assert calls == [
        {
            "repo": "github.com/sg-evals/org-repo",
            "rev": "1" * 40,
            "base": "0" * 40,
            "query": ".py",
        },
        {
            "repo": "github.com/sg-evals/org-repo",
            "rev": "1" * 40,
            "base": "0" * 40,
            "after": "cursor",
            "query": ".py",
        },
    ]


def test_execution_is_resumable_and_content_bound(tmp_path: Path):
    boundary_calls = []
    comparison_calls = []

    def boundaries(repository, cutoff, periods):
        boundary_calls.append((repository, cutoff, tuple(periods)))
        return {
            period: (str(index) * 40, str(index + 1) * 40)
            for index, period in enumerate(periods, start=2)
        }

    def comparison(repository, base, head, language, plan_sha, repository_id, period):
        comparison_calls.append((repository, base, head, language))
        assert plan_sha == "1" * 64
        assert repository_id == "org/repo"
        assert period in {8100, 8102}
        return {
            "raw_diff_sha256": "f" * 64,
            "introduced_code_line_count": 3,
            "feature_values": {name: 0.0 for name in NAMES},
            "eligible_path_count": 5,
            "path_type_counts": {"source": 4, "test": 1},
            "materialization_design": "exact_net_quarter_path_batches",
            "code_age_days": 0,
            "calendar_period": period,
        }

    first = execute_era_panel_plan(
        _plan(),
        tmp_path,
        boundary_runner=boundaries,
        comparison_runner=comparison,
    )
    second = execute_era_panel_plan(
        _plan(),
        tmp_path,
        boundary_runner=boundaries,
        comparison_runner=comparison,
    )

    assert first["status"] == "complete"
    assert first["complete_period_count"] == 2
    assert second["reused_period_count"] == 2
    assert len(boundary_calls) == 1
    assert len(comparison_calls) == 2
    loaded = load_complete_period_results(second, tmp_path, "1" * 64)
    assert len(loaded) == 2
    for unit in second["periods"]:
        shard = json.loads((tmp_path / unit["shard_path"]).read_text())
        assert shard["period_result_sha256"] == unit["period_result_sha256"]
        assert "raw_diff" not in shard
        assert "feature_values" in shard


def test_execution_records_external_failure_and_remains_incomplete(tmp_path: Path):
    def boundaries(_repository, _cutoff, periods):
        return {period: ("2" * 40, "3" * 40) for period in periods}

    def comparison(
        _repository,
        _base,
        _head,
        _language,
        _plan_sha,
        _repository_id,
        _period,
    ):
        raise RuntimeError("Sourcegraph unavailable")

    manifest = execute_era_panel_plan(
        _plan(),
        tmp_path,
        boundary_runner=boundaries,
        comparison_runner=comparison,
    )

    assert manifest["status"] == "incomplete"
    assert manifest["complete_period_count"] == 0
    assert all(not unit["complete"] for unit in manifest["periods"])


def test_structural_precreation_absence_is_complete_but_unobserved(tmp_path: Path):
    def boundaries(_repository, _cutoff, periods):
        return {period: None for period in periods}

    manifest = execute_era_panel_plan(
        _plan(),
        tmp_path,
        boundary_runner=boundaries,
        comparison_runner=lambda *_args: pytest.fail("diff must not run"),
    )

    assert manifest["status"] == "complete"
    assert manifest["complete_period_count"] == 2
    assert manifest["observed_period_count"] == 0
    assert all(not unit["observed"] for unit in manifest["periods"])


def _complete_execution(tmp_path: Path):
    def boundaries(_repository, _cutoff, periods):
        return {period: ("2" * 40, "3" * 40) for period in periods}

    def comparison(
        _repository,
        _base,
        _head,
        _language,
        _plan_sha,
        _repository_id,
        period,
    ):
        return {
            "raw_diff_sha256": "f" * 64,
            "introduced_code_line_count": 3,
            "feature_values": {name: 0.0 for name in NAMES},
            "eligible_path_count": 1,
            "path_type_counts": {"source": 1},
            "materialization_design": "exact_net_quarter_path_batches",
            "code_age_days": 0,
            "calendar_period": period,
        }

    return execute_era_panel_plan(
        _plan(),
        tmp_path,
        boundary_runner=boundaries,
        comparison_runner=comparison,
    )


def _rehash_manifest(manifest):
    manifest["era_panel_execution_sha256"] = _sha256(
        manifest, "era_panel_execution_sha256"
    )


def test_loader_rejects_stale_manifest_hash_and_declared_count(tmp_path: Path):
    manifest = _complete_execution(tmp_path)
    stale = copy.deepcopy(manifest)
    stale["era_panel_execution_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="execution manifest hash"):
        load_complete_period_results(stale, tmp_path, "1" * 64)

    wrong_count = copy.deepcopy(manifest)
    wrong_count["observed_period_count"] = 0
    _rehash_manifest(wrong_count)
    with pytest.raises(RuntimeError, match="declared counts"):
        load_complete_period_results(wrong_count, tmp_path, "1" * 64)


def test_loader_rejects_rehashed_wrong_result_version_and_plan(tmp_path: Path):
    manifest = _complete_execution(tmp_path)
    summary = manifest["periods"][0]
    shard_path = tmp_path / summary["shard_path"]
    shard = json.loads(shard_path.read_text())
    shard["period_result_version"] = 999
    shard["period_result_sha256"] = period_result_sha256(shard)
    shard_path.write_text(json.dumps(shard))
    summary["period_result_sha256"] = shard["period_result_sha256"]
    _rehash_manifest(manifest)

    with pytest.raises(RuntimeError, match="period result version"):
        load_complete_period_results(manifest, tmp_path, "1" * 64)

    fresh_directory = tmp_path / "wrong-plan"
    wrong_plan = _complete_execution(fresh_directory)
    for summary in wrong_plan["periods"]:
        path = fresh_directory / summary["shard_path"]
        shard = json.loads(path.read_text())
        shard["era_panel_plan_sha256"] = "9" * 64
        shard["period_result_sha256"] = period_result_sha256(shard)
        path.write_text(json.dumps(shard))
        summary["period_result_sha256"] = shard["period_result_sha256"]
    wrong_plan["era_panel_plan_sha256"] = "9" * 64
    _rehash_manifest(wrong_plan)

    with pytest.raises(RuntimeError, match="frozen plan"):
        load_complete_period_results(wrong_plan, fresh_directory, "1" * 64)


def test_loader_rejects_rehashed_summary_to_shard_mismatch(tmp_path: Path):
    manifest = _complete_execution(tmp_path)
    manifest["periods"][0]["sourcegraph_name"] = "github.com/sg-evals/wrong"
    _rehash_manifest(manifest)

    with pytest.raises(RuntimeError, match="summary differs"):
        load_complete_period_results(manifest, tmp_path, "1" * 64)
