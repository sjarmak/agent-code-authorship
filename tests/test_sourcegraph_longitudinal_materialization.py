import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_longitudinal import LongitudinalExtractionError
from authorship.sourcegraph_longitudinal_batch import (
    longitudinal_batch_plan_sha256,
)
from authorship.sourcegraph_longitudinal_materialization import (
    execute_materialization_plan,
    materialization_sha256,
)
from authorship.sourcegraph_longitudinal_materialization_cli import main
from authorship.sourcegraph_longitudinal_probe import probe_manifest_sha256
from authorship.sourcegraph_longitudinal_probe_execution import (
    probe_execution_sha256,
)

FIXED_TIME = datetime(2026, 7, 28, 2, 0, tzinfo=timezone.utc)


def _planned_unit(tmp_path: Path, repository_id: str, digit: str) -> dict:
    slug = repository_id.replace("/", "__")
    return {
        "probe_unit_id": digit * 64,
        "repository": {
            "canonical_repository_id": repository_id,
            "sourcegraph_name": f"github.com/sg-evals/{slug}",
            "cutoff_commit": digit * 40,
            "cutoff_tree": chr(ord(digit) + 1) * 40,
            "bundle_sha256": chr(ord(digit) + 2) * 64,
            "cache_path": str(tmp_path / slug),
        },
        "introduction_shard": {
            "path": str(tmp_path / f"{slug}.introductions.jsonl"),
            "sha256": chr(ord(digit) + 3) * 64,
        },
        "transition_shard": {
            "path": str(tmp_path / f"{slug}.transitions.jsonl"),
            "sha256": chr(ord(digit) + 4) * 64,
        },
        "probe_status": "pending_sourcegraph",
        "probe_manifest_path": str(tmp_path / "probes" / f"{slug}.json"),
    }


def _plan(tmp_path: Path, *units: dict) -> dict:
    document = {
        "longitudinal_batch_plan_version": 3,
        "candidate_frame_sha256": "a" * 64,
        "input_inventory_sha256s": {
            "git": "b" * 64,
            "cohort": "c" * 64,
            "lineage": "d" * 64,
            "sourcegraph_index": "e" * 64,
        },
        "horizons_days": [30, 90, 180, 365],
        "repository_count": len(units),
        "units": list(units),
        "outcomes_consulted": False,
    }
    return {
        **document,
        "longitudinal_batch_plan_sha256": longitudinal_batch_plan_sha256(document),
    }


def _probe_manifest(unit: dict) -> dict:
    repository = unit["repository"]
    document = {
        "probe_manifest_version": 3,
        "probe_unit_id": unit["probe_unit_id"],
        **{
            key: repository[key]
            for key in (
                "canonical_repository_id",
                "sourcegraph_name",
                "cutoff_commit",
                "cutoff_tree",
                "bundle_sha256",
            )
        },
        "capabilities_used": [
            "blame",
            "diff_search",
            "revision_search",
        ],
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "hunk_count": 0,
        "hunks": [],
        "valid": True,
        "outcomes_consulted": False,
    }
    return {**document, "probe_manifest_sha256": probe_manifest_sha256(document)}


def _probe_execution(plan: dict, unit_summaries: list[dict]) -> dict:
    valid_count = sum(summary["valid"] for summary in unit_summaries)
    document = {
        "probe_execution_version": 3,
        "longitudinal_batch_plan_sha256": plan["longitudinal_batch_plan_sha256"],
        "started_at": "2026-07-28T01:00:00Z",
        "completed_at": "2026-07-28T01:01:00Z",
        "status": ("complete" if valid_count == len(unit_summaries) else "incomplete"),
        "unit_count": len(unit_summaries),
        "valid_unit_count": valid_count,
        "invalid_unit_count": len(unit_summaries) - valid_count,
        "executed_unit_count": len(unit_summaries),
        "reused_unit_count": 0,
        "outcomes_consulted": False,
        "units": unit_summaries,
    }
    return {**document, "probe_execution_sha256": probe_execution_sha256(document)}


def _valid_probe_summary(unit: dict, manifest: dict) -> dict:
    path = Path(unit["probe_manifest_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))
    return {
        "probe_unit_id": unit["probe_unit_id"],
        "canonical_repository_id": unit["repository"]["canonical_repository_id"],
        "probe_manifest_path": path.as_posix(),
        "probe_manifest_sha256": manifest["probe_manifest_sha256"],
        "hunk_count": manifest["hunk_count"],
        "reused": True,
        "valid": True,
    }


def _fake_executor(
    _protocol: dict,
    unit: dict,
    output_directory: Path,
    *,
    sourcegraph_probe,
) -> dict:
    observation = sourcegraph_probe(unit)
    assert observation["scip_used"] is False
    return {
        "canonical_repository_id": unit["repository"]["canonical_repository_id"],
        "shard_path": (
            output_directory / "shards" / f"{unit['unit_id']}.json"
        ).as_posix(),
        "longitudinal_shard_sha256": "f" * 64,
        "hunk_count": 0,
        "excluded_hunk_count": 0,
        "sourcegraph_verification_status": "no_included_hunks",
        "reused": False,
    }


def test_materialization_is_checksummed_schema_valid_and_content_bound(
    tmp_path: Path,
):
    unit = _planned_unit(tmp_path, "org/repo", "1")
    plan = _plan(tmp_path, unit)
    manifest = _probe_manifest(unit)
    probes = _probe_execution(plan, [_valid_probe_summary(unit, manifest)])

    result = execute_materialization_plan(
        plan,
        probes,
        {},
        tmp_path / "output",
        unit_executor=_fake_executor,
        clock=lambda: FIXED_TIME,
    )

    assert result["status"] == "complete"
    assert result["materialized_unit_count"] == 1
    assert result["units"][0]["valid"] is True
    assert result["units"][0]["execution_unit_id"]
    assert result["materialization_sha256"] == materialization_sha256(result)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-longitudinal-materialization.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(result)


def test_materialization_preserves_probe_missingness_and_continues_failures(
    tmp_path: Path,
):
    valid = _planned_unit(tmp_path, "org/valid", "1")
    missing = _planned_unit(tmp_path, "org/missing", "2")
    plan = _plan(tmp_path, valid, missing)
    manifest = _probe_manifest(valid)
    probes = _probe_execution(
        plan,
        [
            _valid_probe_summary(valid, manifest),
            {
                "probe_unit_id": missing["probe_unit_id"],
                "canonical_repository_id": "org/missing",
                "valid": False,
                "reused": False,
                "error": "repository absent from Sourcegraph index",
            },
        ],
    )

    def failing_executor(*args, **kwargs):
        raise LongitudinalExtractionError("pinned Git cutoff missing")

    result = execute_materialization_plan(
        plan,
        probes,
        {},
        tmp_path / "output",
        unit_executor=failing_executor,
        clock=lambda: FIXED_TIME,
    )

    assert result["status"] == "incomplete"
    assert result["materialized_unit_count"] == 0
    assert result["invalid_unit_count"] == 2
    assert [record["failure_stage"] for record in result["units"]] == [
        "longitudinal_materialization",
        "sourcegraph_probe",
    ]
    assert "pinned Git" in result["units"][0]["error"]
    assert "absent" in result["units"][1]["error"]


def test_materialization_rejects_tampered_plan_probe_and_manifest(tmp_path: Path):
    unit = _planned_unit(tmp_path, "org/repo", "1")
    plan = _plan(tmp_path, unit)
    manifest = _probe_manifest(unit)
    probes = _probe_execution(plan, [_valid_probe_summary(unit, manifest)])

    changed_plan = copy.deepcopy(plan)
    changed_plan["repository_count"] = 2
    with pytest.raises(LongitudinalExtractionError, match="batch plan checksum"):
        execute_materialization_plan(
            changed_plan,
            probes,
            {},
            tmp_path,
            unit_executor=_fake_executor,
        )

    changed_probes = copy.deepcopy(probes)
    changed_probes["valid_unit_count"] = 0
    with pytest.raises(LongitudinalExtractionError, match="probe execution checksum"):
        execute_materialization_plan(
            plan,
            changed_probes,
            {},
            tmp_path,
            unit_executor=_fake_executor,
        )

    mismatched_identity = copy.deepcopy(probes)
    mismatched_identity["units"][0]["canonical_repository_id"] = "other/repo"
    mismatched_identity["probe_execution_sha256"] = probe_execution_sha256(
        mismatched_identity
    )
    with pytest.raises(LongitudinalExtractionError, match="repository identities"):
        execute_materialization_plan(
            plan,
            mismatched_identity,
            {},
            tmp_path,
            unit_executor=_fake_executor,
        )

    Path(unit["probe_manifest_path"]).write_text("{}")
    result = execute_materialization_plan(
        plan,
        probes,
        {},
        tmp_path,
        unit_executor=_fake_executor,
        clock=lambda: FIXED_TIME,
    )
    assert result["invalid_unit_count"] == 1
    assert result["units"][0]["failure_stage"] == "longitudinal_materialization"
    assert "probe manifest" in result["units"][0]["error"]


def test_materialization_cli_writes_atomically_and_returns_partial_status(
    tmp_path: Path,
):
    valid = _planned_unit(tmp_path, "org/valid", "1")
    missing = _planned_unit(tmp_path, "org/missing", "2")
    plan = _plan(tmp_path, valid, missing)
    manifest = _probe_manifest(valid)
    probes = _probe_execution(
        plan,
        [
            _valid_probe_summary(valid, manifest),
            {
                "probe_unit_id": missing["probe_unit_id"],
                "canonical_repository_id": "org/missing",
                "valid": False,
                "reused": False,
                "error": "not indexed",
            },
        ],
    )
    plan_path = tmp_path / "plan.json"
    probes_path = tmp_path / "probes.json"
    protocol_path = tmp_path / "protocol.json"
    output_path = tmp_path / "inventory" / "result.json"
    plan_path.write_text(json.dumps(plan))
    probes_path.write_text(json.dumps(probes))
    protocol_path.write_text("{}")

    exit_code = main(
        [
            "--plan",
            str(plan_path),
            "--probe-execution",
            str(probes_path),
            "--protocol",
            str(protocol_path),
            "--output-directory",
            str(tmp_path / "shards"),
            "--output",
            str(output_path),
        ],
        unit_executor=_fake_executor,
        clock=lambda: FIXED_TIME,
    )

    assert exit_code == 1
    assert json.loads(output_path.read_text())["invalid_unit_count"] == 1


def test_materialization_cli_rejects_non_object_input(tmp_path: Path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]")

    with pytest.raises(LongitudinalExtractionError, match="must be an object"):
        main(
            [
                "--plan",
                str(invalid),
                "--probe-execution",
                str(invalid),
                "--protocol",
                str(invalid),
            ],
            unit_executor=_fake_executor,
        )
