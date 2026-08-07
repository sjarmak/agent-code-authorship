import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from authorship.survival_estimate_execution import (
    SurvivalEstimateExecutionError,
    execute_survival_estimates,
    survival_estimate_artifact_sha256,
)
from authorship.survival_event_batch import survival_event_inventory_sha256
from authorship.survival_event_materializer import (
    survival_event_shard_manifest_sha256,
)


def canonical_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def line(
    repository_id: str,
    line_id: str,
    *,
    family: str,
    language: str,
    evidence_tier: str = "tier_2",
    transitions: list[dict] | None = None,
) -> dict:
    return {
        "repository_id": repository_id,
        "line_id": line_id,
        "censor_time_days": 400.0,
        "transitions": transitions or [],
        "language": language,
        "agent_family": family,
        "evidence_tier": evidence_tier,
    }


def create_event_inventory(
    tmp_path: Path,
    *,
    repositories_per_family: int = 5,
    mixed_evidence_tiers: bool = False,
) -> Path:
    root = tmp_path / "events"
    (root / "shards").mkdir(parents=True)
    (root / "manifests").mkdir()
    repositories = {}
    for index in range(repositories_per_family):
        cursor_id = f"org/cursor-{index}"
        claude_id = f"org/claude-{index}"
        repositories[cursor_id] = [
            line(
                cursor_id,
                "deleted",
                family="Cursor",
                language="Python",
                evidence_tier="tier_1" if mixed_evidence_tiers else "tier_2",
                transitions=[
                    {
                        "time_days": 100.0,
                        "from_state": "unchanged",
                        "to_state": "deleted",
                    }
                ],
            ),
            line(
                cursor_id,
                "retained",
                family="Cursor",
                language="Python",
                evidence_tier="tier_1" if mixed_evidence_tiers else "tier_2",
            ),
        ]
        repositories[claude_id] = [
            line(
                claude_id,
                "modified",
                family="Claude_Code",
                language="Go",
                transitions=[
                    {
                        "time_days": 40.0,
                        "from_state": "unchanged",
                        "to_state": "modified",
                    }
                ],
            )
        ]
    entries = []
    for repository_id, lines in repositories.items():
        slug = repository_id.replace("/", "__")
        shard = root / "shards" / f"{slug}.jsonl"
        shard.write_text("".join(f"{canonical_json(value)}\n" for value in lines))
        manifest = {
            "contract_version": 3,
            "repository_id": repository_id,
            "input_sha256s": {
                "transitions": "1" * 64,
                "structural_events": "2" * 64,
                "git_inventory": "3" * 64,
                "lineage_validation": "4" * 64,
            },
            "cutoff": {
                "timestamp": "2026-07-24T23:59:59Z",
                "commit": "a" * 40,
                "tree": "b" * 40,
            },
            "line_count": len(lines),
            "censoring_audit": {
                "study_cutoff": len(lines),
                "lineage_loss": 0,
                "terminal_deletion": 0,
            },
            "event_shard_file": shard.name,
            "event_shard_sha256": sha256(shard),
            "outcomes_consulted": False,
        }
        manifest["survival_event_shard_manifest_sha256"] = (
            survival_event_shard_manifest_sha256(manifest)
        )
        manifest_path = root / "manifests" / f"{slug}.json"
        manifest_path.write_text(canonical_json(manifest))
        entries.append(
            {
                "repository_id": repository_id,
                "status": "valid",
                "line_count": len(lines),
                "event_shard_file": shard.name,
                "event_shard_sha256": sha256(shard),
                "manifest_file": manifest_path.name,
                "manifest_sha256": manifest["survival_event_shard_manifest_sha256"],
            }
        )
    inventory = {
        "contract_version": 1,
        "status": "complete",
        "git_inventory_sha256": "3" * 64,
        "lineage_inventory_sha256": "4" * 64,
        "lineage_validation_sha256": "4" * 64,
        "counts": {
            "repositories": len(repositories),
            "valid": len(repositories),
            "invalid": 0,
            "lines": sum(len(lines) for lines in repositories.values()),
        },
        "repositories": entries,
        "outcomes_consulted": False,
    }
    inventory["survival_event_inventory_sha256"] = survival_event_inventory_sha256(
        inventory
    )
    (root / "survival-event-inventory.v1.json").write_text(canonical_json(inventory))
    return root


def test_executes_frozen_strata_with_cluster_intervals_and_invariants(
    tmp_path: Path,
):
    event_root = create_event_inventory(tmp_path)
    output = tmp_path / "survival-estimates.v2.json"

    artifact = execute_survival_estimates(
        event_root=event_root,
        output_path=output,
        bootstrap_replicates=25,
        seed=20260724,
    )

    assert json.loads(output.read_text()) == artifact
    assert artifact["source"]["event_inventory_sha256"] == (
        json.loads((event_root / "survival-event-inventory.v1.json").read_text())[
            "survival_event_inventory_sha256"
        ]
    )
    assert artifact["counts"] == {
        "repositories": 10,
        "lines": 15,
        "strata": 20,
        "identified_strata": 8,
        "not_identified_strata": 0,
        "empty_strata": 12,
    }
    cursor = next(
        value
        for value in artifact["strata"]
        if value["stratum_id"] == "agent_family=Cursor"
    )
    survival = [
        point["survival"] for point in cursor["estimate"]["primary"]["kaplan_meier"]
    ]
    assert survival == [1.0, 1.0, 0.5, 0.5]
    assert survival == sorted(survival, reverse=True)
    assert artifact["invariants"] == {
        "all_kaplan_meier_curves_monotone": True,
        "all_aalen_johansen_masses_conserved": True,
        "all_risk_sets_explicit": True,
        "cursor_365_rise_absent": True,
    }
    assert artifact["survival_estimate_artifact_sha256"] == (
        survival_estimate_artifact_sha256(artifact)
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/survival-estimate-artifact.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(artifact)

    rerun = execute_survival_estimates(
        event_root=event_root,
        output_path=output,
        bootstrap_replicates=25,
        seed=20260724,
    )
    assert rerun == artifact


def test_repository_minimum_marks_underpowered_stratum_not_identified(tmp_path: Path):
    artifact = execute_survival_estimates(
        event_root=create_event_inventory(tmp_path, repositories_per_family=4),
        output_path=tmp_path / "output.json",
        bootstrap_replicates=5,
        seed=1,
    )

    cursor = next(
        value
        for value in artifact["strata"]
        if value["stratum_id"] == "agent_family=Cursor"
    )
    assert cursor["repository_count"] == 4
    assert cursor["status"] == "not_identified"
    assert cursor["estimate"] is None
    assert artifact["invariants"]["cursor_365_rise_absent"] is None


def test_mixed_evidence_tiers_are_not_pooled_into_overall_headline(tmp_path: Path):
    artifact = execute_survival_estimates(
        event_root=create_event_inventory(tmp_path, mixed_evidence_tiers=True),
        output_path=tmp_path / "output.json",
        bootstrap_replicates=5,
        seed=1,
    )

    overall = next(
        value for value in artifact["strata"] if value["stratum_id"] == "overall"
    )
    assert overall["repository_count"] == 10
    assert overall["status"] == "not_identified"
    assert overall["estimate"] is None


def test_rejects_tampered_shard_and_incomplete_inventory(tmp_path: Path):
    event_root = create_event_inventory(tmp_path)
    shard = next((event_root / "shards").glob("*.jsonl"))
    shard.write_text("{}\n")

    with pytest.raises(SurvivalEstimateExecutionError, match="shard checksum"):
        execute_survival_estimates(
            event_root=event_root,
            output_path=tmp_path / "output.json",
            bootstrap_replicates=5,
            seed=1,
        )

    event_root = create_event_inventory(tmp_path / "second")
    inventory_path = event_root / "survival-event-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["status"] = "incomplete"
    inventory["survival_event_inventory_sha256"] = survival_event_inventory_sha256(
        inventory
    )
    inventory_path.write_text(canonical_json(inventory))
    with pytest.raises(SurvivalEstimateExecutionError, match="complete"):
        execute_survival_estimates(
            event_root=event_root,
            output_path=tmp_path / "output.json",
            bootstrap_replicates=5,
            seed=1,
        )


def test_rejects_lineage_validation_hash_mismatch(tmp_path: Path):
    event_root = create_event_inventory(tmp_path)
    inventory_path = event_root / "survival-event-inventory.v1.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["lineage_validation_sha256"] = "5" * 64
    inventory["survival_event_inventory_sha256"] = survival_event_inventory_sha256(
        inventory
    )
    inventory_path.write_text(canonical_json(inventory))

    with pytest.raises(
        SurvivalEstimateExecutionError, match="lineage validation provenance"
    ):
        execute_survival_estimates(
            event_root=event_root,
            output_path=tmp_path / "output.json",
            bootstrap_replicates=5,
            seed=1,
        )


def test_artifact_checksum_detects_tampering(tmp_path: Path):
    artifact = execute_survival_estimates(
        event_root=create_event_inventory(tmp_path),
        output_path=tmp_path / "output.json",
        bootstrap_replicates=5,
        seed=1,
    )
    changed = copy.deepcopy(artifact)
    changed["strata"][0]["estimate"]["line_count"] = 999

    assert survival_estimate_artifact_sha256(changed) != (
        artifact["survival_estimate_artifact_sha256"]
    )
