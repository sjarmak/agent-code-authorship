import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from authorship.sourcegraph_discovery import specification_sha256
from authorship.sourcegraph_discovery_execution import (
    build_execution_units,
    execute_discovery,
    execution_manifest_sha256,
    result_manifest_sha256,
    validate_result_manifest,
)
from authorship.sourcegraph_discovery_validation import validate_execution_manifest

SHA_A = "a" * 40
INDEX_SHA = "b" * 64
AUDIT_SHA = "c" * 64
SOURCEGRAPH_NAME = "github.com/sg-evals/org-repo"
EXECUTED_AT = datetime(2026, 7, 27, 20, 45, tzinfo=timezone.utc)


def inputs() -> tuple[dict, dict, dict]:
    root = Path(__file__).resolve().parents[1]
    specification = json.loads(
        (root / "study/sourcegraph-discovery.v3.json").read_text()
    )
    specification["index_manifest_sha256"] = INDEX_SHA
    specification["execution"]["repository_exclusions"]["repositories"] = []
    specification["specification_sha256"] = specification_sha256(specification)
    index_manifest = {
        "outcomes_consulted": False,
        "repository_count": 1,
        "repositories": [
            {
                "canonical_repository_id": "org/repo",
                "cutoff_commit": SHA_A,
                "sourcegraph": {"selected_name": SOURCEGRAPH_NAME},
            }
        ],
    }
    index_audit = {
        "outcomes_consulted": False,
        "repository_count": 1,
        "repositories": [
            {
                "canonical_repository_id": "org/repo",
                "direct": {"name": "github.com/org/repo"},
                "mirror": {
                    "name": SOURCEGRAPH_NAME,
                    "state": "indexed",
                    "cutoff_state": "accessible",
                    "cutoff_oid": SHA_A,
                },
            }
        ],
    }
    return specification, index_manifest, index_audit


def file_match() -> dict:
    return {
        "__typename": "FileMatch",
        "repository": {"name": SOURCEGRAPH_NAME, "url": "/repo"},
        "file": {
            "name": "README.md",
            "path": "README.md",
            "url": f"/repo/-/blob/{SHA_A}/README.md",
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


def commit_match() -> dict:
    oid = "d" * 40
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


def execute(tmp_path: Path, *, with_results: bool = False) -> tuple[dict, dict]:
    specification, index_manifest, index_audit = inputs()
    root = Path(__file__).resolve().parents[1]

    def runner(_query: str, result_type: str, *_args: str) -> dict:
        results = []
        if with_results:
            results = [file_match()] if result_type == "file" else [commit_match()]
        return {
            "results": results,
            "result_count": len(results),
            "limit_hit": False,
            "cloning_repositories": [],
            "missing_repositories": [],
            "timed_out_repositories": [],
        }

    manifest = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        tmp_path,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        repository_exclusion_source=(
            root / "study/sg-evals-action-plan.v3.json"
        ).read_bytes(),
        query_runner=runner,
        clock=lambda: EXECUTED_AT,
    )
    return specification, manifest


def test_manifest_checksums_detect_tampering(tmp_path: Path):
    specification, manifest = execute(tmp_path)

    assert manifest["execution_manifest_sha256"] == execution_manifest_sha256(manifest)
    assert validate_execution_manifest(manifest, specification, tmp_path) == []
    changed = copy.deepcopy(manifest)
    changed["valid_unit_count"] -= 1
    assert "execution_manifest_sha256 does not match" in validate_execution_manifest(
        changed, specification, tmp_path
    )
    shard_path = tmp_path / manifest["units"][0]["shard_path"]
    shard_path.unlink()
    assert any(
        "unit shard is unreadable" in error
        for error in validate_execution_manifest(manifest, specification, tmp_path)
    )


def test_result_validator_reports_payload_and_validity_tampering(tmp_path: Path):
    specification, manifest = execute(tmp_path, with_results=True)
    shard = json.loads((tmp_path / manifest["units"][0]["shard_path"]).read_text())
    shard["raw_results"][0]["payload"]["__typename"] = "Changed"
    shard["valid"] = False

    errors = validate_result_manifest(shard, specification)

    assert "sourcegraph result ID does not match raw payload" in errors
    assert "valid does not match execution state" in errors
    assert "result_manifest_sha256 does not match" in errors


def test_result_validator_revalidates_raw_payload_after_rehash(tmp_path: Path):
    specification, manifest = execute(tmp_path, with_results=True)
    shard = json.loads((tmp_path / manifest["units"][0]["shard_path"]).read_text())
    raw_record = shard["raw_results"][0]
    raw_record["payload"]["__typename"] = "Repository"
    payload = json.dumps(
        raw_record["payload"], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    raw_record["sourcegraph_result_id"] = (
        f"sha256:{hashlib.sha256(payload).hexdigest()}"
    )
    shard["sourcegraph_result_ids"] = [raw_record["sourcegraph_result_id"]]
    shard["result_manifest_sha256"] = result_manifest_sha256(shard)

    assert any(
        "raw result payload is invalid" in error
        for error in validate_result_manifest(shard, specification)
    )


def test_result_validator_reports_missing_scope_without_raising():
    specification, _index_manifest, _index_audit = inputs()

    errors = validate_result_manifest(
        {"raw_results": [], "rendered_query": "patternType:regexp"}, specification
    )

    assert "sourcegraph_name must be a string" in errors
    assert "cutoff_commit must be a 40-character SHA-1" in errors


def test_result_validator_reports_unhashable_family_without_raising():
    specification, _index_manifest, _index_audit = inputs()

    errors = validate_result_manifest(
        {
            "query_family_id": {},
            "raw_results": [],
            "rendered_query": "patternType:regexp",
        },
        specification,
    )

    assert "query_family_id is not frozen" in errors


def test_execution_validator_cross_checks_summary_and_shard_path(tmp_path: Path):
    specification, manifest = execute(tmp_path)
    changed = copy.deepcopy(manifest)
    changed["units"][0]["canonical_repository_id"] = "other/repo"
    changed["status"] = "incomplete"
    changed["execution_manifest_sha256"] = execution_manifest_sha256(changed)

    errors = validate_execution_manifest(changed, specification, tmp_path)

    assert any("summary field does not match shard" in error for error in errors)
    assert "status does not match units" in errors
    escaped = copy.deepcopy(manifest)
    escaped["units"][0]["shard_path"] = "shards/../../outside.json"
    escaped["execution_manifest_sha256"] = execution_manifest_sha256(escaped)
    assert any(
        "shard_path does not match unit identity" in error
        for error in validate_execution_manifest(escaped, specification, tmp_path)
    )


def test_execution_validator_rejects_symlinked_shard_outside_root(tmp_path: Path):
    specification, manifest = execute(tmp_path)
    shard_path = tmp_path / manifest["units"][0]["shard_path"]
    outside = tmp_path.parent / "outside-discovery-shard.json"
    outside.write_bytes(shard_path.read_bytes())
    shard_path.unlink()
    shard_path.symlink_to(outside)

    errors = validate_execution_manifest(manifest, specification, tmp_path)

    assert any("outside output directory" in error for error in errors)


def test_execution_validator_binds_exact_frozen_unit_population(tmp_path: Path):
    specification, manifest = execute(tmp_path)
    _specification, index_manifest, index_audit = inputs()
    expected_units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=INDEX_SHA,
    )
    changed = copy.deepcopy(manifest)
    changed["units"].pop()
    changed["unit_count"] -= 1
    changed["valid_unit_count"] -= 1
    changed["executed_unit_count"] -= 1
    changed["execution_manifest_sha256"] = execution_manifest_sha256(changed)

    errors = validate_execution_manifest(
        changed,
        specification,
        tmp_path,
        expected_units=expected_units,
        expected_index_audit_sha256=AUDIT_SHA,
    )

    assert "execution units do not match frozen population" in errors


def test_execution_validator_reports_non_list_units_without_raising(tmp_path: Path):
    specification, _manifest = execute(tmp_path)

    errors = validate_execution_manifest(
        {"units": None, "outcomes_consulted": False}, specification, tmp_path
    )

    assert "units must be a list" in errors


def test_generated_artifacts_match_machine_schemas(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    result_schema = json.loads(
        (root / "study/sourcegraph-query-result.schema.json").read_text()
    )
    execution_schema = json.loads(
        (root / "study/sourcegraph-discovery-execution.schema.json").read_text()
    )
    _specification, manifest = execute(tmp_path)

    jsonschema.Draft202012Validator(execution_schema).validate(manifest)
    for summary in manifest["units"]:
        shard = json.loads((tmp_path / summary["shard_path"]).read_text())
        jsonschema.Draft202012Validator(result_schema).validate(shard)
