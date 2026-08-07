import copy
import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_probe import (
    build_probe_manifest,
    execute_probe_unit,
    manifest_observation,
    probe_manifest_sha256,
    render_diff_probe_query,
)

MERGE = "b" * 40
BASE = "c" * 40
CUTOFF = "a" * 40
SOURCEGRAPH_NAME = "github.com/sg-evals/org-repo"


def repository() -> dict:
    return {
        "canonical_repository_id": "org/repo",
        "sourcegraph_name": SOURCEGRAPH_NAME,
        "cutoff_commit": CUTOFF,
        "cutoff_tree": "1" * 40,
        "bundle_sha256": "2" * 64,
    }


def introduction(line_number: int = 10, *, path: str = "src/a+b.py") -> dict:
    return {
        "repository_id": "org/repo",
        "merge_commit": MERGE,
        "diff_base": BASE,
        "merged_at": "2025-01-10T00:00:00Z",
        "path": path,
        "line_number": line_number,
        "content_sha256": f"{line_number:064x}",
        "text": 'value = "agent"',
        "language": "Python",
        "agent_family": "OpenAI_Codex",
        "provenance_tier": 2,
        "pr_number": 7,
    }


def commit_match(oid: str = MERGE, *, diff_highlight_count: int = 1) -> dict:
    return {
        "__typename": "CommitSearchResult",
        "messagePreview": None,
        "diffPreview": {
            "value": '+value = "agent"',
            "highlights": [
                {"line": 0, "character": index, "length": 1}
                for index in range(diff_highlight_count)
            ],
        },
        "commit": {
            "repository": {"name": SOURCEGRAPH_NAME},
            "oid": oid,
            "url": f"/repo/-/commit/{oid}",
            "subject": "agent change",
            "author": {
                "date": "2025-01-10T00:00:00Z",
                "person": {"displayName": "Example"},
            },
        },
    }


def search_payload(
    results: list[dict],
    *,
    limit_hit: bool = False,
    result_count: int | None = None,
) -> dict:
    return {
        "search": {
            "results": {
                "results": results,
                "limitHit": limit_hit,
                "cloning": [],
                "missing": [],
                "timedout": [],
                "resultCount": len(results) if result_count is None else result_count,
                "elapsedMilliseconds": 1,
            }
        }
    }


def api_runner(graphql_query: str, **variables: str) -> dict:
    if "query" in variables:
        return search_payload([commit_match()])
    assert variables == {
        "repo": SOURCEGRAPH_NAME,
        "rev": MERGE,
        "path": "src/a+b.py",
    }
    return {
        "repository": {
            "name": SOURCEGRAPH_NAME,
            "commit": {
                "oid": MERGE,
                "blob": {
                    "path": "src/a+b.py",
                    "blame": [
                        {
                            "startLine": 10,
                            "endLine": 11,
                            "commit": {"oid": MERGE},
                        }
                    ],
                },
            },
        }
    }


def test_rendered_diff_query_is_exact_exhaustive_and_avoids_precise_features():
    query = render_diff_probe_query(repository(), introduction())

    assert f"rev:{MERGE}:^{BASE}" in query
    assert r"repo:^github\.com/sg-evals/org-repo$" in query
    assert r"file:^src/a\+b\.py$" in query
    assert 'content:"value = \\"agent\\""' in query
    assert "type:diff" in query
    assert "select:commit.diff.added" in query
    assert "count:all" in query
    assert "SCIP" not in query


def test_probe_manifest_preserves_raw_sourcegraph_evidence_deterministically():
    records = [introduction(10), introduction(11)]

    forward = build_probe_manifest(repository(), records, api_runner=api_runner)
    reverse = build_probe_manifest(
        repository(), list(reversed(records)), api_runner=api_runner
    )

    assert forward == reverse
    assert forward["probe_manifest_sha256"] == probe_manifest_sha256(forward)
    assert forward["valid"] is True
    assert forward["capabilities_used"] == [
        "blame",
        "diff_search",
        "revision_search",
    ]
    hunk = forward["hunks"][0]
    assert hunk["introducing_commit_oid"] == MERGE
    assert hunk["blame_commit_oids"] == [MERGE]
    assert hunk["sourcegraph_result_ids"][0].startswith("sha256:")
    observation = manifest_observation(forward)
    assert observation["result_manifest_sha256s"] == [forward["probe_manifest_sha256"]]
    assert observation["hunks"][0]["path"] == "src/a+b.py"
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "study"
        / "sourcegraph-longitudinal-probe-manifest.schema.json"
    )
    jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(
        forward
    )


def test_probe_accepts_complete_grouped_commit_diff_matches():
    def grouped_runner(graphql_query: str, **variables: str) -> dict:
        if "query" in variables:
            return search_payload(
                [commit_match(diff_highlight_count=3)],
                result_count=3,
            )
        return api_runner(graphql_query, **variables)

    manifest = build_probe_manifest(
        repository(),
        [introduction()],
        api_runner=grouped_runner,
    )

    hunk = manifest["hunks"][0]
    assert len(hunk["raw_diff_results"]) == 1
    assert len(hunk["raw_diff_results"][0]["payload"]["diffPreview"]["highlights"]) == 3


def test_probe_batches_search_and_blame_without_losing_hunk_evidence():
    paths = ["src/one.py", "src/two.py", "src/three.py"]
    calls = []

    def batch_runner(graphql_query: str, **variables: str) -> dict:
        calls.append((graphql_query, variables))
        response = {}
        batch_paths = [
            json.loads(value)
            for value in re.findall(r'blob\(path:("[^"]+")\)', graphql_query)
        ]
        for index, path in enumerate(batch_paths):
            response[f"s{index}"] = search_payload([commit_match()])["search"]
            response[f"b{index}"] = {
                "name": SOURCEGRAPH_NAME,
                "commit": {
                    "oid": MERGE,
                    "blob": {
                        "path": path,
                        "blame": [
                            {
                                "startLine": 10,
                                "endLine": 10,
                                "commit": {"oid": MERGE},
                            }
                        ],
                    },
                },
            }
        return response

    manifest = build_probe_manifest(
        repository(),
        [introduction(path=path) for path in paths],
        batch_api_runner=batch_runner,
        batch_size=10,
    )

    assert len(calls) == 1
    assert calls[0][1] == {}
    assert calls[0][0].count(":search(query:") == 3
    assert calls[0][0].count(":repository(name:") == 3
    assert {hunk["path"] for hunk in manifest["hunks"]} == set(paths)
    assert all(hunk["sourcegraph_result_ids"] for hunk in manifest["hunks"])
    assert all(hunk["blame_commit_oids"] == [MERGE] for hunk in manifest["hunks"])
    assert all(
        hunk["raw_blame_response"]["repository"]["commit"]["blob"]["path"]
        == hunk["path"]
        for hunk in manifest["hunks"]
    )


def test_probe_batch_fails_closed_when_an_alias_is_missing():
    def incomplete_batch_runner(graphql_query: str, **variables: str) -> dict:
        first_path = json.loads(
            re.search(r'blob\(path:("[^"]+")\)', graphql_query).group(1)
        )
        return {
            "s0": search_payload([commit_match()])["search"],
            "b0": {
                "name": SOURCEGRAPH_NAME,
                "commit": {
                    "oid": MERGE,
                    "blob": {
                        "path": first_path,
                        "blame": [{"commit": {"oid": MERGE}}],
                    },
                },
            },
        }

    with pytest.raises(LongitudinalExtractionError, match="batch alias"):
        build_probe_manifest(
            repository(),
            [introduction(path="src/one.py"), introduction(path="src/two.py")],
            batch_api_runner=incomplete_batch_runner,
            batch_size=10,
        )

    with pytest.raises(LongitudinalExtractionError, match="batch_size"):
        build_probe_manifest(
            repository(),
            [introduction(path="src/one.py")],
            batch_api_runner=incomplete_batch_runner,
            batch_size=16,
        )


def test_probe_batch_falls_back_to_variable_queries_after_graphql_failure():
    paths = ["src/one.py", "src/two.py"]
    calls = []

    def fallback_runner(graphql_query: str, **variables: str) -> dict:
        calls.append((graphql_query, variables))
        if not variables:
            raise RuntimeError("panic occurred: invalid syntax")
        if "query" in variables:
            return search_payload([commit_match()])
        return {
            "repository": {
                "name": SOURCEGRAPH_NAME,
                "commit": {
                    "oid": MERGE,
                    "blob": {
                        "path": variables["path"],
                        "blame": [{"commit": {"oid": MERGE}}],
                    },
                },
            }
        }

    manifest = build_probe_manifest(
        repository(),
        [introduction(path=path) for path in paths],
        batch_api_runner=fallback_runner,
        batch_size=10,
    )

    assert manifest["valid"] is True
    assert {hunk["path"] for hunk in manifest["hunks"]} == set(paths)
    assert sum(not variables for _, variables in calls) == 3
    assert sum("query" in variables for _, variables in calls) == 2
    assert sum("repo" in variables for _, variables in calls) == 2


def test_probe_fails_closed_on_incomplete_search_or_wrong_blame_revision():
    def limited_runner(graphql_query: str, **variables: str) -> dict:
        if "query" in variables:
            return search_payload([commit_match()], limit_hit=True)
        return api_runner(graphql_query, **variables)

    with pytest.raises(LongitudinalExtractionError, match="incomplete"):
        build_probe_manifest(repository(), [introduction()], api_runner=limited_runner)

    def inconsistent_count_runner(graphql_query: str, **variables: str) -> dict:
        if "query" in variables:
            return search_payload(
                [commit_match(diff_highlight_count=3)],
                result_count=2,
            )
        return api_runner(graphql_query, **variables)

    with pytest.raises(LongitudinalExtractionError, match="incomplete"):
        build_probe_manifest(
            repository(),
            [introduction()],
            api_runner=inconsistent_count_runner,
        )

    def wrong_blame_runner(graphql_query: str, **variables: str) -> dict:
        response = api_runner(graphql_query, **variables)
        if "query" not in variables:
            response = copy.deepcopy(response)
            response["repository"]["commit"]["oid"] = CUTOFF
        return response

    with pytest.raises(LongitudinalExtractionError, match="revision"):
        build_probe_manifest(
            repository(), [introduction()], api_runner=wrong_blame_runner
        )


def test_manifest_observation_rejects_tampering():
    manifest = build_probe_manifest(
        repository(), [introduction()], api_runner=api_runner
    )
    manifest["hunks"][0]["blame_commit_oids"] = [BASE]

    with pytest.raises(LongitudinalExtractionError, match="checksum"):
        manifest_observation(manifest)


def test_probe_executor_reuses_only_identity_bound_valid_manifest(tmp_path: Path):
    record = introduction()
    payload = (json.dumps(record, sort_keys=True) + "\n").encode()
    cohort = tmp_path / "cohort.jsonl"
    cohort.write_bytes(payload)
    output = tmp_path / "probes/repo.json"
    unit = {
        "probe_unit_id": "6" * 64,
        "repository": repository(),
        "introduction_shard": {
            "path": str(cohort),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        "probe_manifest_path": str(output),
    }
    calls = []

    def counting_runner(graphql_query: str, **variables: str) -> dict:
        calls.append((graphql_query, variables))
        return api_runner(graphql_query, **variables)

    first = execute_probe_unit(unit, api_runner=counting_runner)
    call_count = len(calls)
    second = execute_probe_unit(unit, api_runner=counting_runner)

    assert first["reused"] is False
    assert second["reused"] is True
    assert len(calls) == call_count
    changed = copy.deepcopy(unit)
    changed["probe_unit_id"] = "7" * 64
    third = execute_probe_unit(changed, api_runner=counting_runner)
    assert third["reused"] is False
    assert len(calls) > call_count
