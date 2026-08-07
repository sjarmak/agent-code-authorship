import hashlib
import json
from pathlib import Path

import jsonschema

from authorship.survival_event_batch import (
    execute_event_materialization,
    survival_event_inventory_sha256,
)
from authorship.survival_event_materializer import (
    MaterializationResult,
    SurvivalEventMaterializationError,
)


def canonical_json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_inventories(tmp_path: Path) -> tuple[Path, Path]:
    git_inventory_path = tmp_path / "git-inventory.json"
    git_inventory_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "repository_id": "b/two",
                        "cache_path": "/cache/b__two",
                    },
                    {
                        "repository_id": "a/one",
                        "cache_path": "/cache/a__one",
                    },
                    {
                        "repository_id": "c/three",
                        "cache_path": "/cache/c__three",
                    },
                ]
            }
        )
    )
    lineage_inventory_path = tmp_path / "lineage-inventory.json"
    lineage_inventory_path.write_text(
        json.dumps(
            {
                "git_inventory_sha256": sha256(git_inventory_path),
                "repositories": [
                    {"repository_id": "a/one", "line_count": 2},
                    {"repository_id": "b/two", "line_count": 3},
                ],
            }
        )
    )
    return git_inventory_path, lineage_inventory_path


def write_validation(tmp_path: Path) -> Path:
    path = tmp_path / "lineage-validation.json"
    path.write_text(
        json.dumps(
            {
                "status": "complete",
                "headline_handling": "structural_candidates_map_to_modified",
                "minimum_structural_precision": 0.9,
                "structural_match_gate_passed": True,
                "structural_precision": 1.0,
            }
        )
    )
    return path


def result(
    repository_id: str, output_path: Path, reused: bool
) -> MaterializationResult:
    document = {
        "contract_version": 1,
        "repository_id": repository_id,
        "line_count": 2 if repository_id == "a/one" else 3,
        "event_shard_file": output_path.name,
        "event_shard_sha256": "a" * 64,
        "survival_event_shard_manifest_sha256": "b" * 64,
    }
    return MaterializationResult(document, reused)


def test_executes_repositories_in_stable_order_and_writes_checksummed_inventory(
    tmp_path: Path,
):
    git_inventory, lineage_inventory = write_inventories(tmp_path)
    calls = []

    def materializer(**arguments):
        calls.append(arguments)
        return result(
            arguments["repository_id"],
            arguments["output_path"],
            reused=arguments["repository_id"] == "b/two",
        )

    inventory = execute_event_materialization(
        git_inventory_path=git_inventory,
        lineage_inventory_path=lineage_inventory,
        lineage_validation_path=write_validation(tmp_path),
        transition_root=tmp_path / "transitions",
        structural_event_root=tmp_path / "structural",
        output_root=tmp_path / "output",
        work_root=tmp_path / "work",
        materializer=materializer,
    )

    assert [call["repository_id"] for call in calls] == ["a/one", "b/two"]
    assert calls[0]["transition_path"].name == "a__one.jsonl"
    assert calls[0]["repository_path"] == Path("/cache/a__one")
    assert inventory["status"] == "complete"
    assert inventory["counts"] == {
        "repositories": 2,
        "valid": 2,
        "invalid": 0,
        "lines": 5,
    }
    assert inventory["survival_event_inventory_sha256"] == (
        survival_event_inventory_sha256(inventory)
    )
    persisted = json.loads(
        (tmp_path / "output/survival-event-inventory.v1.json").read_text()
    )
    assert persisted == inventory
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/survival-event-inventory.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(inventory)
    rerun = execute_event_materialization(
        git_inventory_path=git_inventory,
        lineage_inventory_path=lineage_inventory,
        lineage_validation_path=write_validation(tmp_path),
        transition_root=tmp_path / "transitions",
        structural_event_root=tmp_path / "structural",
        output_root=tmp_path / "output",
        work_root=tmp_path / "work",
        materializer=lambda **arguments: result(
            arguments["repository_id"],
            arguments["output_path"],
            reused=arguments["repository_id"] == "a/one",
        ),
    )
    assert rerun == inventory


def test_continues_after_failure_and_records_invalid_repository(tmp_path: Path):
    git_inventory, lineage_inventory = write_inventories(tmp_path)
    calls = []

    def materializer(**arguments):
        calls.append(arguments["repository_id"])
        if arguments["repository_id"] == "a/one":
            raise SurvivalEventMaterializationError("bad lineage")
        return result(arguments["repository_id"], arguments["output_path"], False)

    inventory = execute_event_materialization(
        git_inventory_path=git_inventory,
        lineage_inventory_path=lineage_inventory,
        lineage_validation_path=write_validation(tmp_path),
        transition_root=tmp_path / "transitions",
        structural_event_root=tmp_path / "structural",
        output_root=tmp_path / "output",
        work_root=tmp_path / "work",
        materializer=materializer,
    )

    assert calls == ["a/one", "b/two"]
    assert inventory["status"] == "incomplete"
    assert inventory["counts"]["invalid"] == 1
    assert inventory["repositories"][0] == {
        "repository_id": "a/one",
        "status": "invalid",
        "error": "bad lineage",
    }


def test_rejects_inventory_population_or_checksum_mismatch(tmp_path: Path):
    git_inventory, lineage_inventory = write_inventories(tmp_path)
    lineage = json.loads(lineage_inventory.read_text())
    lineage["repositories"].append({"repository_id": "unknown/repo"})
    lineage_inventory.write_text(json.dumps(lineage))

    try:
        execute_event_materialization(
            git_inventory_path=git_inventory,
            lineage_inventory_path=lineage_inventory,
            lineage_validation_path=write_validation(tmp_path),
            transition_root=tmp_path,
            structural_event_root=tmp_path,
            output_root=tmp_path / "output",
            work_root=tmp_path / "work",
            materializer=lambda **_: None,
        )
    except SurvivalEventMaterializationError as error:
        assert "population" in str(error)
    else:
        raise AssertionError("population mismatch was accepted")

    lineage["git_inventory_sha256"] = "f" * 64
    lineage["repositories"] = [{"repository_id": "a/one"}]
    lineage_inventory.write_text(json.dumps(lineage))
    try:
        execute_event_materialization(
            git_inventory_path=git_inventory,
            lineage_inventory_path=lineage_inventory,
            lineage_validation_path=write_validation(tmp_path),
            transition_root=tmp_path,
            structural_event_root=tmp_path,
            output_root=tmp_path / "output-2",
            work_root=tmp_path / "work",
            materializer=lambda **_: None,
        )
    except SurvivalEventMaterializationError as error:
        assert "checksum" in str(error)
    else:
        raise AssertionError("checksum mismatch was accepted")
