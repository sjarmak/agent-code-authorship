import json
import copy
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_discovery import specification_sha256
from authorship.sourcegraph_discovery_execution import (
    build_execution_units,
    execute_discovery,
    execution_manifest_sha256,
)
from authorship.sourcegraph_discovery_inventory import (
    DiscoveryInventoryError,
    build_effective_inventory,
    effective_inventory_sha256,
    load_effective_result_manifests,
    terminal_failure_resolution,
    terminal_failure_status,
)
from authorship.sourcegraph_discovery_partition import (
    build_partition_plan,
    build_query_branch_refined_partition_plan,
    build_refined_partition_plan,
    execute_partition_plan,
    execute_query_branch_refined_partition_plan,
    execute_refined_partition_plan,
)

SHA_A = "a" * 40
INDEX_SHA = "b" * 64
AUDIT_SHA = "c" * 64
SOURCEGRAPH_NAME = "github.com/sg-evals/org-repo"
EXECUTED_AT = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("timeouts", "execution_errors", "status", "resolution"),
    [
        (0, 0, "complete", "partition"),
        (1, 0, "complete_with_terminal_timeouts", "partition_with_terminal_timeouts"),
        (
            0,
            1,
            "complete_with_terminal_execution_errors",
            "partition_with_terminal_execution_errors",
        ),
        (
            1,
            1,
            "complete_with_terminal_failures",
            "partition_with_terminal_failures",
        ),
    ],
)
def test_terminal_failure_names(
    timeouts: int,
    execution_errors: int,
    status: str,
    resolution: str,
):
    assert terminal_failure_status(timeouts, execution_errors) == status
    assert terminal_failure_resolution(timeouts, execution_errors) == resolution


def empty_response() -> dict:
    return {
        "results": [],
        "result_count": 0,
        "limit_hit": False,
        "cloning_repositories": [],
        "missing_repositories": [],
        "timed_out_repositories": [],
    }


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


def artifacts(tmp_path: Path) -> tuple:
    specification, index_manifest, index_audit = inputs()
    base_root = tmp_path / "base"
    partition_root = tmp_path / "partition"

    def base_runner(query: str, result_type: str, *_args: str) -> dict:
        response = empty_response()
        if result_type == "diff" and "generated|authored|implemented" in query:
            response["timed_out_repositories"] = [SOURCEGRAPH_NAME]
        return response

    base = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        base_root,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=base_runner,
        clock=lambda: EXECUTED_AT,
    )
    invalid_summary = next(summary for summary in base["units"] if not summary["valid"])
    source_parent = json.loads((base_root / invalid_summary["shard_path"]).read_text())
    plan = build_partition_plan(specification, [source_parent])
    partition = execute_partition_plan(
        plan,
        specification,
        partition_root,
        source_parents=[source_parent],
        query_runner=lambda *_args: empty_response(),
        clock=lambda: EXECUTED_AT,
    )
    expected_units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=INDEX_SHA,
    )
    return (
        specification,
        base,
        base_root,
        plan,
        partition,
        partition_root,
        expected_units,
        AUDIT_SHA,
    )


def test_effective_inventory_replaces_timed_out_parent_with_valid_children(
    tmp_path: Path,
):
    (
        specification,
        base,
        base_root,
        plan,
        partition,
        partition_root,
        expected_units,
        audit_sha,
    ) = artifacts(tmp_path)

    inventory, result_manifests = build_effective_inventory(
        specification,
        base,
        base_root,
        plan,
        partition,
        partition_root,
        expected_units,
        audit_sha,
    )

    assert inventory["status"] == "complete"
    assert inventory["logical_unit_count"] == 6
    assert inventory["base_result_manifest_count"] == 5
    assert inventory["recovered_parent_count"] == 1
    assert inventory["partition_child_result_manifest_count"] == 93
    assert inventory["effective_result_manifest_count"] == 98
    assert len(result_manifests) == 98
    assert (
        load_effective_result_manifests(
            inventory,
            specification=specification,
            base_root=base_root,
            partition_root=partition_root,
        )
        == result_manifests
    )
    assert inventory["effective_inventory_sha256"] == effective_inventory_sha256(
        inventory
    )
    recovered = next(
        unit for unit in inventory["units"] if unit["resolution"] == "partition"
    )
    assert len(recovered["effective_result_manifests"]) == 93
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (
            root / "study/sourcegraph-effective-discovery-inventory.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(inventory)


def test_effective_inventory_accepts_validated_branch_refinement(tmp_path: Path):
    specification, index_manifest, index_audit = inputs()
    base_root = tmp_path / "base"
    partition_root = tmp_path / "partition"

    def base_runner(query: str, result_type: str, *_args: str) -> dict:
        response = empty_response()
        if result_type == "diff" and "file:(CONTRIBUTING" in query:
            response["timed_out_repositories"] = [SOURCEGRAPH_NAME]
        return response

    base = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        base_root,
        index_manifest_sha256=INDEX_SHA,
        index_audit_sha256=AUDIT_SHA,
        query_runner=base_runner,
        clock=lambda: EXECUTED_AT,
    )
    invalid = next(summary for summary in base["units"] if not summary["valid"])
    source_parent = json.loads((base_root / invalid["shard_path"]).read_text())
    source_plan = build_partition_plan(specification, [source_parent])
    timeout = empty_response()
    timeout["timed_out_repositories"] = [SOURCEGRAPH_NAME]
    source_execution = execute_partition_plan(
        source_plan,
        specification,
        partition_root,
        source_parents=[source_parent],
        query_runner=lambda *_args: timeout,
        clock=lambda: EXECUTED_AT,
    )
    refined_plan = build_refined_partition_plan(
        specification,
        source_plan,
        source_execution,
        partition_root,
        source_parents=[source_parent],
    )

    def daily_runner(query: str, *_args: str) -> dict:
        return timeout if 'after:"2023-01-01"' in query else empty_response()

    daily_execution = execute_refined_partition_plan(
        refined_plan,
        specification,
        partition_root,
        source_plan=source_plan,
        source_execution=source_execution,
        source_parents=[source_parent],
        query_runner=daily_runner,
        clock=lambda: EXECUTED_AT,
    )
    branch_plan = build_query_branch_refined_partition_plan(
        specification,
        refined_plan,
        daily_execution,
        partition_root,
        source_parents=[source_parent],
        ancestor_plan=source_plan,
        ancestor_execution=source_execution,
    )

    def branch_runner(query: str, *_args: str) -> dict:
        if "file:(^|/)README" in query and "AI.{0,240}prohibit" in query:
            return timeout
        if "file:(^|/)README" in query and "LLM.{0,240}prohibit" in query:
            raise RuntimeError("Sourcegraph searcher unavailable")
        return empty_response()

    branch_execution = execute_query_branch_refined_partition_plan(
        branch_plan,
        specification,
        partition_root,
        source_plan=refined_plan,
        source_execution=daily_execution,
        source_parents=[source_parent],
        ancestor_plan=source_plan,
        ancestor_execution=source_execution,
        query_runner=branch_runner,
        clock=lambda: EXECUTED_AT,
    )
    expected_units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=INDEX_SHA,
    )

    inventory, manifests = build_effective_inventory(
        specification,
        base,
        base_root,
        branch_plan,
        branch_execution,
        partition_root,
        expected_units,
        AUDIT_SHA,
        refinement_source_plan=refined_plan,
        refinement_source_execution=daily_execution,
        refinement_ancestor_plan=source_plan,
        refinement_ancestor_execution=source_execution,
    )

    assert inventory["status"] == "complete_with_terminal_failures"
    assert inventory["terminal_timeout_result_manifest_count"] > 0
    assert inventory["terminal_execution_error_result_manifest_count"] > 0
    assert inventory["partition_plan_sha256"] == branch_plan["partition_plan_sha256"]
    assert len(manifests) == inventory["effective_result_manifest_count"]
    terminal_only_inventory = copy.deepcopy(inventory)
    terminal_unit = next(
        unit
        for unit in terminal_only_inventory["units"]
        if unit["resolution"] == "partition_with_terminal_failures"
    )
    removed_reference_count = len(terminal_unit["effective_result_manifests"])
    terminal_unit["effective_result_manifests"] = []
    terminal_only_inventory[
        "effective_result_manifest_count"
    ] -= removed_reference_count
    terminal_only_inventory["effective_inventory_sha256"] = effective_inventory_sha256(
        terminal_only_inventory
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-effective-discovery-inventory.schema.json"
        ).read_text()
    )
    jsonschema.validate(terminal_only_inventory, schema)
    loaded = load_effective_result_manifests(
        terminal_only_inventory,
        specification=specification,
        base_root=base_root,
        partition_root=partition_root,
    )
    assert len(loaded) == len(manifests) - removed_reference_count
    retry_calls = 0

    def should_reuse_terminal_failures(*_args: str) -> dict:
        nonlocal retry_calls
        retry_calls += 1
        return empty_response()

    execute_query_branch_refined_partition_plan(
        branch_plan,
        specification,
        partition_root,
        source_plan=refined_plan,
        source_execution=daily_execution,
        source_parents=[source_parent],
        ancestor_plan=source_plan,
        ancestor_execution=source_execution,
        query_runner=should_reuse_terminal_failures,
        clock=lambda: EXECUTED_AT,
    )
    assert retry_calls == 0
    execute_query_branch_refined_partition_plan(
        branch_plan,
        specification,
        partition_root,
        source_plan=refined_plan,
        source_execution=daily_execution,
        source_parents=[source_parent],
        ancestor_plan=source_plan,
        ancestor_execution=source_execution,
        query_runner=should_reuse_terminal_failures,
        clock=lambda: EXECUTED_AT,
        retry_terminal_execution_errors=True,
    )
    assert retry_calls > 0


def test_effective_inventory_rejects_incomplete_partition_execution(
    tmp_path: Path,
):
    documents = list(artifacts(tmp_path))
    documents[4]["status"] = "incomplete"

    with pytest.raises(DiscoveryInventoryError, match="partition execution"):
        build_effective_inventory(*documents)


def test_effective_inventory_rejects_unrecovered_base_failure(tmp_path: Path):
    documents = list(artifacts(tmp_path))
    documents[3] = {
        **documents[3],
        "parents": [],
        "parent_count": 0,
        "child_unit_count": 0,
    }

    with pytest.raises(DiscoveryInventoryError, match="partition plan"):
        build_effective_inventory(*documents)


def test_effective_inventory_rejects_rechecksummed_missing_base_unit(
    tmp_path: Path,
):
    documents = list(artifacts(tmp_path))
    base = documents[1]
    removed = next(summary for summary in base["units"] if summary["valid"])
    base["units"].remove(removed)
    base["unit_count"] -= 1
    base["valid_unit_count"] -= 1
    base["executed_unit_count"] -= 1
    base["execution_manifest_sha256"] = execution_manifest_sha256(base)

    with pytest.raises(DiscoveryInventoryError, match="frozen population"):
        build_effective_inventory(*documents)


def test_effective_inventory_rejects_base_shard_symlink_outside_root(
    tmp_path: Path,
):
    documents = list(artifacts(tmp_path))
    base, base_root = documents[1], documents[2]
    summary = next(unit for unit in base["units"] if unit["valid"])
    shard_path = base_root / summary["shard_path"]
    outside = tmp_path / "outside-base-shard.json"
    outside.write_bytes(shard_path.read_bytes())
    shard_path.unlink()
    shard_path.symlink_to(outside)

    with pytest.raises(DiscoveryInventoryError, match="outside output directory"):
        build_effective_inventory(*documents)


def test_effective_inventory_revalidates_partition_shard_after_manifest_check(
    tmp_path: Path,
    monkeypatch,
):
    documents = list(artifacts(tmp_path))
    partition, partition_root = documents[4], documents[5]
    summary = partition["parents"][0]["children"][0]
    target = partition_root / summary["shard_path"]
    good = target.read_text()
    evil = copy.deepcopy(json.loads(good))
    evil["unit_id"] = "f" * 64
    reads = 0
    original = Path.read_text

    def swapping_read(path: Path, *args, **kwargs):
        nonlocal reads
        if path == target:
            reads += 1
            if reads >= 2:
                return json.dumps(evil)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", swapping_read)

    with pytest.raises(DiscoveryInventoryError, match="partition child"):
        build_effective_inventory(*documents)


def test_effective_inventory_loader_rejects_rechecksummed_reference_tampering(
    tmp_path: Path,
):
    documents = artifacts(tmp_path)
    inventory, _manifests = build_effective_inventory(*documents)
    inventory["units"][0]["effective_result_manifests"][0]["result_manifest_sha256"] = (
        "f" * 64
    )
    inventory["effective_inventory_sha256"] = effective_inventory_sha256(inventory)

    with pytest.raises(DiscoveryInventoryError, match="reference"):
        load_effective_result_manifests(
            inventory,
            specification=documents[0],
            base_root=documents[2],
            partition_root=documents[5],
        )
