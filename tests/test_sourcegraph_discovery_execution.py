import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from authorship.sg_evals_mirroring import action_plan_sha256
from authorship.sourcegraph_discovery_execution import (
    build_execution_units,
    execute_discovery,
    render_query,
    result_manifest_sha256,
    run_sourcegraph_query,
    validate_result_manifest,
)
from authorship.sourcegraph_discovery import specification_sha256

SHA_A = "a" * 40
INDEX_SHA = "b" * 64
AUDIT_SHA = "c" * 64
EXECUTED_AT = datetime(2026, 7, 27, 20, 45, tzinfo=timezone.utc)
SOURCEGRAPH_NAME = "github.com/sg-evals/org-repo.with+meta"


def exclusion_source_bytes(exclusions: list[dict]) -> bytes:
    repositories = [
        {
            "canonical_repository_id": exclusion["canonical_repository_id"],
            "action": exclusion["disposition"],
            "reason": exclusion["reason"],
            "force_required": False,
        }
        for exclusion in exclusions
    ]
    document = {
        "plan_version": 3,
        "status": "dry_run_external_approval_required",
        "observed_at": "2026-07-28T00:00:00Z",
        "outcomes_consulted": False,
        "repository_count": len(repositories),
        "action_counts": {
            action: sum(record["action"] == action for record in repositories)
            for action in sorted({record["action"] for record in repositories})
        },
        "source_artifacts": {
            "index_manifest_canonical_sha256": "1" * 64,
            "github_audit_sha256": "2" * 64,
            "license_audit_sha256": "3" * 64,
        },
        "repositories": repositories,
    }
    document["plan_sha256"] = action_plan_sha256(document)
    return json.dumps(document, sort_keys=True).encode()


def configure_exclusions(specification: dict, exclusions: list[dict]) -> bytes:
    source = exclusion_source_bytes(exclusions)
    specification["execution"]["repository_exclusions"] = {
        "source_artifact": "study/sg-evals-action-plan.v3.json",
        "source_artifact_sha256": hashlib.sha256(source).hexdigest(),
        "repositories": exclusions,
    }
    specification["specification_sha256"] = specification_sha256(specification)
    return source


@pytest.fixture
def specification() -> dict:
    path = Path(__file__).resolve().parents[1] / "study/sourcegraph-discovery.v3.json"
    document = json.loads(path.read_text())
    document["index_manifest_sha256"] = INDEX_SHA
    document["execution"]["repository_exclusions"]["repositories"] = []
    document["specification_sha256"] = specification_sha256(document)
    return document


@pytest.fixture
def index_manifest() -> dict:
    return {
        "outcomes_consulted": False,
        "repository_count": 1,
        "repositories": [
            {
                "canonical_repository_id": "org/repo",
                "cutoff_commit": SHA_A,
                "sourcegraph": {
                    "transport_status": "mirror_creation_required",
                    "selected_name": SOURCEGRAPH_NAME,
                },
            }
        ],
    }


@pytest.fixture
def index_audit() -> dict:
    return {
        "outcomes_consulted": False,
        "repository_count": 1,
        "repositories": [
            {
                "canonical_repository_id": "org/repo",
                "direct": {
                    "name": "github.com/org/repo",
                    "state": "not_indexed",
                    "cutoff_state": "not_accessible",
                    "cutoff_oid": None,
                },
                "mirror": {
                    "name": SOURCEGRAPH_NAME,
                    "state": "indexed",
                    "cutoff_state": "accessible",
                    "cutoff_oid": SHA_A,
                },
            }
        ],
    }


def file_match(path: str = "README.md") -> dict:
    return {
        "__typename": "FileMatch",
        "repository": {"name": SOURCEGRAPH_NAME, "url": "/repo"},
        "file": {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "url": f"/repo/-/blob/{SHA_A}/{path}",
            "commit": {"oid": SHA_A},
        },
        "lineMatches": [
            {
                "preview": "AI contributions are not allowed.",
                "lineNumber": 3,
                "offsetAndLengths": [[0, 2]],
                "limitHit": False,
            }
        ],
    }


def commit_match(oid: str = "d" * 40) -> dict:
    return {
        "__typename": "CommitSearchResult",
        "messagePreview": {"value": "Generated-by: Codex", "highlights": []},
        "diffPreview": None,
        "commit": {
            "repository": {"name": SOURCEGRAPH_NAME},
            "oid": oid,
            "url": f"/repo/-/commit/{oid}",
            "subject": "Generated-by: Codex",
            "author": {
                "date": "2025-01-01T00:00:00Z",
                "person": {"displayName": "Example"},
            },
        },
    }


def search_payload(
    results: list[dict],
    *,
    result_count: int | None = None,
    limit_hit: bool = False,
    timedout: list[dict] | None = None,
) -> dict:
    return {
        "search": {
            "results": {
                "results": results,
                "limitHit": limit_hit,
                "cloning": [],
                "missing": [],
                "timedout": timedout or [],
                "resultCount": len(results) if result_count is None else result_count,
                "elapsedMilliseconds": 12,
            }
        }
    }


def test_render_query_escapes_only_repository_regex_metacharacters(
    specification: dict,
):
    family = next(
        family
        for family in specification["query_families"]
        if family["id"] == "generated_code_disclosure_diffs"
    )

    rendered = render_query(family, SOURCEGRAPH_NAME, SHA_A)

    assert f"repo:^github\\.com/sg-evals/org-repo\\.with\\+meta$@{SHA_A}" in rendered
    assert "(generated|authored|implemented).{0,80}" in rendered
    assert "{sourcegraph_repo_regex}" not in rendered
    assert "{cutoff_commit}" not in rendered


@pytest.mark.parametrize(
    ("family", "message"),
    [
        ({"query_template": 1}, "must be a string"),
        (
            {"query_template": "{cutoff_commit} patternType:regexp"},
            "repository placeholder",
        ),
        (
            {
                "query_template": (
                    "{sourcegraph_repo_regex} {cutoff_commit} {cutoff_commit} "
                    "patternType:regexp"
                )
            },
            "cutoff placeholder",
        ),
        (
            {
                "query_template": (
                    "{sourcegraph_repo_regex} {cutoff_commit} no-pattern-type"
                )
            },
            "must declare patternType",
        ),
    ],
)
def test_render_query_rejects_malformed_templates(family: dict, message: str):
    with pytest.raises(ValueError, match=message):
        render_query(family, SOURCEGRAPH_NAME, SHA_A)


@pytest.mark.parametrize(
    ("family_id", "raw_result"),
    [
        ("adoption_announcement_files", file_match()),
        ("agent_trailer_commits", commit_match()),
        (
            "generated_code_disclosure_diffs",
            {**commit_match(), "diffPreview": {"value": "+ generated by Codex"}},
        ),
    ],
)
def test_sourcegraph_adapter_parses_official_result_shapes(
    specification: dict,
    family_id: str,
    raw_result: dict,
):
    family = next(
        family
        for family in specification["query_families"]
        if family["id"] == family_id
    )
    calls = []

    def api_runner(graphql_query: str, **variables: str) -> dict:
        calls.append((graphql_query, variables))
        return search_payload([raw_result])

    response = run_sourcegraph_query(
        "repo:^example$ patternType:regexp",
        family["result_type"],
        SOURCEGRAPH_NAME,
        SHA_A,
        api_runner=api_runner,
    )

    assert calls[0][1] == {"query": "repo:^example$ patternType:regexp"}
    assert "committer { date person { name email displayName } }" in calls[0][0]
    assert response["results"] == [raw_result]
    assert response["result_count"] == 1
    assert response["limit_hit"] is False
    assert response["timed_out_repositories"] == []


def test_sourcegraph_adapter_rejects_wrong_repository_and_malformed_counts():
    wrong_repository = file_match()
    wrong_repository["repository"]["name"] = "github.com/sg-evals/wrong"

    with pytest.raises(ValueError, match="unexpected repository"):
        run_sourcegraph_query(
            "query",
            "file",
            SOURCEGRAPH_NAME,
            SHA_A,
            api_runner=lambda *_args, **_kwargs: search_payload([wrong_repository]),
        )
    with pytest.raises(ValueError, match="resultCount"):
        run_sourcegraph_query(
            "query",
            "file",
            SOURCEGRAPH_NAME,
            SHA_A,
            api_runner=lambda *_args, **_kwargs: search_payload([], result_count=-1),
        )
    with pytest.raises(ValueError, match="fewer objects"):
        run_sourcegraph_query(
            "query",
            "file",
            SOURCEGRAPH_NAME,
            SHA_A,
            api_runner=lambda *_args, **_kwargs: search_payload(
                [file_match()], result_count=0
            ),
        )


def test_nested_file_match_limit_invalidates_response():
    limited = file_match()
    limited["lineMatches"][0]["limitHit"] = True

    response = run_sourcegraph_query(
        "query",
        "file",
        SOURCEGRAPH_NAME,
        SHA_A,
        api_runner=lambda *_args, **_kwargs: search_payload([limited]),
    )

    assert response["limit_hit"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["search"]["results"].update({"limitHit": "no"}),
        lambda payload: payload["search"]["results"].update({"missing": [1]}),
        lambda payload: payload["search"]["results"].update(
            {"results": [{"__typename": "Repository"}]}
        ),
        lambda payload: payload.update({"search": []}),
    ],
)
def test_sourcegraph_adapter_rejects_malformed_envelopes(mutation):
    payload = search_payload([])
    mutation(payload)

    with pytest.raises(ValueError):
        run_sourcegraph_query(
            "query",
            "file",
            SOURCEGRAPH_NAME,
            SHA_A,
            api_runner=lambda *_args, **_kwargs: payload,
        )


def test_zero_result_shards_are_checksummed_and_reused(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    calls = []

    def runner(query: str, *_args: str) -> dict:
        calls.append(query)
        return {
            "results": [],
            "result_count": 0,
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        }

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )

    assert first["status"] == "complete"
    assert first["unit_count"] == len(specification["query_families"])
    assert first["executed_unit_count"] == first["unit_count"]
    assert second["executed_unit_count"] == 0
    assert second["reused_unit_count"] == second["unit_count"]
    assert len(calls) == first["unit_count"]
    for summary in first["units"]:
        shard = json.loads((tmp_path / summary["shard_path"]).read_text())
        assert shard["sourcegraph_result_ids"] == []
        assert shard["result_count"] == 0
        assert shard["valid"] is True
        assert shard["result_manifest_sha256"] == result_manifest_sha256(shard)
        assert validate_result_manifest(shard, specification) == []


def test_raw_result_ids_and_manifest_are_stable_across_result_order(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    results = [commit_match("e" * 40), commit_match("d" * 40)]

    def run_with(order: list[dict], directory: Path) -> dict:
        return execute_discovery(
            specification,
            index_manifest,
            index_audit,
            directory,
            index_manifest_sha256=INDEX_SHA,
            index_audit_sha256=AUDIT_SHA,
            query_runner=lambda *_args: {
                "results": order,
                "result_count": 2,
                "limit_hit": False,
                "cloning_repositories": [],
                "missing_repositories": [],
                "timed_out_repositories": [],
            },
            clock=lambda: EXECUTED_AT,
        )

    first = run_with(results, tmp_path / "first")
    second = run_with(list(reversed(results)), tmp_path / "second")
    first_commit = next(
        unit for unit in first["units"] if unit["query_family_id"].endswith("commits")
    )
    second_commit = next(
        unit for unit in second["units"] if unit["query_family_id"].endswith("commits")
    )
    first_shard = json.loads(
        ((tmp_path / "first") / first_commit["shard_path"]).read_text()
    )
    second_shard = json.loads(
        ((tmp_path / "second") / second_commit["shard_path"]).read_text()
    )

    assert (
        first_shard["sourcegraph_result_ids"] == second_shard["sourcegraph_result_ids"]
    )
    assert (
        first_shard["result_manifest_sha256"] == second_shard["result_manifest_sha256"]
    )


@pytest.mark.parametrize(
    "response",
    [
        {
            "results": [],
            "result_count": 0,
            "limit_hit": True,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        },
        {
            "results": [],
            "result_count": 0,
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [{"name": SOURCEGRAPH_NAME}],
        },
    ],
)
def test_limit_or_timeout_invalidates_and_reruns_shard(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
    response: dict,
):
    calls = 0

    def runner(*_args: str) -> dict:
        nonlocal calls
        calls += 1
        return response

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )

    assert first["status"] == "incomplete"
    assert first["invalid_unit_count"] == first["unit_count"]
    assert second["executed_unit_count"] == second["unit_count"]
    assert calls == first["unit_count"] + second["unit_count"]


def test_corrupt_shard_is_rerun_and_replaced_atomically(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    calls = 0

    def runner(*_args: str) -> dict:
        nonlocal calls
        calls += 1
        return {
            "results": [],
            "result_count": 0,
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        }

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    damaged = tmp_path / first["units"][0]["shard_path"]
    damaged.write_text("{not-json")

    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )

    assert second["executed_unit_count"] == 1
    assert second["reused_unit_count"] == second["unit_count"] - 1
    assert json.loads(damaged.read_text())["valid"] is True
    assert list(tmp_path.rglob("*.tmp")) == []
    assert calls == first["unit_count"] + 1


def test_structurally_incomplete_shard_is_rerun(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    calls = 0

    def runner(*_args: str) -> dict:
        nonlocal calls
        calls += 1
        return {
            "results": [],
            "result_count": 0,
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        }

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    damaged = tmp_path / first["units"][0]["shard_path"]
    partial = json.loads(damaged.read_text())
    del partial["limit_hit"]
    damaged.write_text(json.dumps(partial))

    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )

    assert second["executed_unit_count"] == 1
    assert json.loads(damaged.read_text())["limit_hit"] is False
    assert calls == first["unit_count"] + 1


def test_readiness_requires_the_exact_selected_cutoff(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    index_audit["repositories"][0]["mirror"]["cutoff_oid"] = "f" * 40

    with pytest.raises(ValueError, match="not accessible at its frozen cutoff"):
        execute_discovery(
            specification,
            index_manifest,
            index_audit,
            tmp_path,
            index_manifest_sha256=INDEX_SHA,
            index_audit_sha256=AUDIT_SHA,
            query_runner=lambda *_args: {},
            clock=lambda: EXECUTED_AT,
        )


def test_execution_skips_only_frozen_repository_exclusions(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    held_repository_id = "held/repo"
    exclusions = [
        {
            "canonical_repository_id": held_repository_id,
            "disposition": "hold_redistribution",
            "reason": "No declared redistribution terms at the frozen cutoff.",
        }
    ]
    source = configure_exclusions(specification, exclusions)
    index_manifest["repository_count"] = 2
    index_manifest["repositories"].append(
        {
            "canonical_repository_id": held_repository_id,
            "cutoff_commit": "e" * 40,
            "sourcegraph": {
                "transport_status": "mirror_creation_required",
                "selected_name": "github.com/sg-evals/held-repo",
            },
        }
    )
    index_audit["repository_count"] = 2
    index_audit["repositories"].append(
        {
            "canonical_repository_id": held_repository_id,
            "direct": {
                "name": "github.com/held/repo",
                "state": "not_indexed",
                "cutoff_state": "not_accessible",
                "cutoff_oid": None,
            },
            "mirror": {
                "name": "github.com/sg-evals/held-repo",
                "state": "not_indexed",
                "cutoff_state": "not_accessible",
                "cutoff_oid": None,
            },
        }
    )

    units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=INDEX_SHA,
        repository_exclusion_source=source,
    )

    assert len(units) == len(specification["query_families"])
    assert {unit["canonical_repository_id"] for unit in units} == {"org/repo"}


@pytest.mark.parametrize(
    ("excluded_id", "message"),
    [
        ("missing/repo", "not present in the index manifest"),
        ("org/repo", "is accessible at its frozen cutoff"),
    ],
)
def test_frozen_repository_exclusions_fail_closed(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
    excluded_id: str,
    message: str,
):
    exclusions = [
        {
            "canonical_repository_id": excluded_id,
            "disposition": "hold_redistribution",
            "reason": "Frozen hold.",
        }
    ]
    source = configure_exclusions(specification, exclusions)

    with pytest.raises(ValueError, match=message):
        build_execution_units(
            specification,
            index_manifest,
            index_audit,
            index_manifest_sha256=INDEX_SHA,
            repository_exclusion_source=source,
        )


def test_frozen_repository_exclusions_require_the_exact_source_bytes(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    exclusions = [
        {
            "canonical_repository_id": "org/repo",
            "disposition": "hold_private",
            "reason": "Frozen hold.",
        }
    ]
    configure_exclusions(specification, exclusions)

    with pytest.raises(ValueError, match="source SHA-256"):
        build_execution_units(
            specification,
            index_manifest,
            index_audit,
            index_manifest_sha256=INDEX_SHA,
            repository_exclusion_source=b"{}",
        )


@pytest.mark.parametrize("malformation", ["missing_record", "wrong_location"])
def test_frozen_repository_exclusions_require_matching_audit_evidence(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
    malformation: str,
):
    exclusions = [
        {
            "canonical_repository_id": "org/repo",
            "disposition": "hold_private",
            "reason": "Frozen hold.",
        }
    ]
    source = configure_exclusions(specification, exclusions)
    index_audit["repositories"][0]["mirror"].update(
        {
            "state": "not_indexed",
            "cutoff_state": "not_accessible",
            "cutoff_oid": None,
        }
    )
    if malformation == "missing_record":
        index_audit["repositories"][0]["canonical_repository_id"] = "other/repo"
    else:
        index_audit["repositories"][0]["mirror"]["name"] = "github.com/wrong/repo"

    with pytest.raises(ValueError, match="audit"):
        build_execution_units(
            specification,
            index_manifest,
            index_audit,
            index_manifest_sha256=INDEX_SHA,
            repository_exclusion_source=source,
        )


def test_schema_valid_present_not_cloned_audit_can_support_a_frozen_exclusion(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    exclusions = [
        {
            "canonical_repository_id": "org/repo",
            "disposition": "hold_private",
            "reason": "Frozen hold.",
        }
    ]
    source = configure_exclusions(specification, exclusions)
    index_audit["repositories"][0]["mirror"].update(
        {
            "state": "present_not_cloned",
            "cutoff_state": "not_accessible",
            "cutoff_oid": None,
        }
    )

    units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=INDEX_SHA,
        repository_exclusion_source=source,
    )

    assert units == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest, _audit: manifest.update({"outcomes_consulted": True}),
        lambda _manifest, audit: audit.update({"outcomes_consulted": True}),
        lambda manifest, _audit: manifest.update({"repositories": []}),
        lambda manifest, _audit: manifest.update({"repository_count": 2}),
    ],
)
def test_execution_units_reject_invalid_population_boundaries(
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
    mutate,
):
    mutate(index_manifest, index_audit)

    with pytest.raises(ValueError):
        build_execution_units(
            specification,
            index_manifest,
            index_audit,
            index_manifest_sha256=INDEX_SHA,
        )


def test_runner_errors_are_recorded_and_never_reused(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    calls = 0

    def runner(*_args: str) -> dict:
        nonlocal calls
        calls += 1
        raise RuntimeError("transport failed")

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )

    assert first["status"] == "incomplete"
    assert second["reused_unit_count"] == 0
    assert calls == first["unit_count"] + second["unit_count"]
    shard = json.loads((tmp_path / first["units"][0]["shard_path"]).read_text())
    assert shard["invalid_reasons"] == ["execution_error"]
    assert shard["execution_error"].startswith("RuntimeError: transport failed")


def test_retry_execution_errors_only_reuses_other_validated_invalid_shards(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    calls = 0

    def first_runner(*_args: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "results": [],
                "result_count": 0,
                "limit_hit": False,
                "cloning_repositories": [],
                "missing_repositories": [],
                "timed_out_repositories": [{"name": SOURCEGRAPH_NAME}],
            }
        raise RuntimeError("transport failed")

    first = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=first_runner,
        clock=lambda: EXECUTED_AT,
    )
    retry_calls = 0

    def retry_runner(*_args: str) -> dict:
        nonlocal retry_calls
        retry_calls += 1
        return {
            "results": [],
            "result_count": 0,
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        }

    second = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=retry_runner,
        clock=lambda: EXECUTED_AT,
        retry_execution_errors_only=True,
    )

    assert first["status"] == "incomplete"
    assert second["status"] == "incomplete"
    assert second["reused_unit_count"] == 1
    assert second["executed_unit_count"] == first["unit_count"] - 1
    assert retry_calls == first["unit_count"] - 1
    reused = [unit for unit in second["units"] if unit["reused"]]
    assert reused[0]["invalid_reasons"] == ["timed_out_repositories"]


def test_clock_must_be_timezone_aware(
    tmp_path: Path,
    specification: dict,
    index_manifest: dict,
    index_audit: dict,
):
    with pytest.raises(ValueError, match="timezone-aware"):
        execute_discovery(
            specification,
            index_manifest,
            index_audit,
            tmp_path,
            index_manifest_sha256=INDEX_SHA,
            index_audit_sha256=AUDIT_SHA,
            query_runner=lambda *_args: {},
            clock=lambda: datetime(2026, 7, 27),
        )
