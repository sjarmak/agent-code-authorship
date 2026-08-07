import json
from pathlib import Path

import pytest

import authorship.sourcegraph_discovery_partition_cli as partition_cli


@pytest.fixture(autouse=True)
def frozen_units(monkeypatch):
    monkeypatch.setattr(
        partition_cli,
        "build_execution_units",
        lambda *args, **kwargs: [],
    )


def test_cli_loads_only_timed_out_parent_shards_and_writes_plan(
    tmp_path: Path, monkeypatch, capsys
):
    specification = tmp_path / "specification.json"
    execution = tmp_path / "execution.json"
    result_root = tmp_path / "results"
    output = tmp_path / "partitions"
    plan_output = tmp_path / "plan.json"
    inventory_output = tmp_path / "inventory.json"
    index_manifest = tmp_path / "index-manifest.json"
    index_audit = tmp_path / "index-audit.json"
    index_manifest.write_text("{}")
    index_audit.write_text("{}")
    shard_path = Path("shards/family/unit.json")
    shard = {
        "unit_id": "a" * 64,
        "result_manifest_sha256": "b" * 64,
        "valid": False,
        "invalid_reasons": ["timed_out_repositories"],
    }
    (result_root / shard_path).parent.mkdir(parents=True)
    (result_root / shard_path).write_text(json.dumps(shard))
    specification.write_text(json.dumps({"specification_sha256": "c" * 64}))
    execution.write_text(
        json.dumps(
            {
                "specification_sha256": "c" * 64,
                "units": [
                    {
                        **shard,
                        "shard_path": shard_path.as_posix(),
                    }
                ],
            }
        )
    )
    observed = {}
    monkeypatch.setattr(partition_cli, "check_auth", lambda: "user")
    monkeypatch.setattr(
        partition_cli, "validate_execution_manifest", lambda *arguments, **kwargs: []
    )
    expected_units = [{"unit_id": "f" * 64}]
    monkeypatch.setattr(
        partition_cli,
        "build_execution_units",
        lambda *args, **kwargs: expected_units,
    )

    def build(specification_document, parents):
        observed["specification"] = specification_document
        observed["parents"] = parents
        return {
            "partition_plan_sha256": "d" * 64,
            "status": "frozen_before_partition_execution",
        }

    def execute(plan, specification_document, output_directory, *, source_parents):
        observed["plan"] = plan
        observed["output_directory"] = output_directory
        observed["source_parents"] = source_parents
        return {"status": "complete"}

    def build_inventory(
        specification_document,
        base_execution,
        base_root,
        plan,
        partition_execution,
        partition_root,
        frozen_units,
        index_audit_sha256,
        **kwargs,
    ):
        observed["inventory_arguments"] = (
            specification_document,
            base_execution,
            base_root,
            plan,
            partition_execution,
            partition_root,
            frozen_units,
            index_audit_sha256,
        )
        observed["inventory_kwargs"] = kwargs
        return {"status": "complete", "effective_inventory_sha256": "e" * 64}, []

    monkeypatch.setattr(partition_cli, "build_partition_plan", build)
    monkeypatch.setattr(partition_cli, "execute_partition_plan", execute)
    monkeypatch.setattr(partition_cli, "build_effective_inventory", build_inventory)

    result = partition_cli.main(
        [
            "--specification",
            str(specification),
            "--execution-manifest",
            str(execution),
            "--result-root",
            str(result_root),
            "--plan-output",
            str(plan_output),
            "--output-directory",
            str(output),
            "--index-manifest",
            str(index_manifest),
            "--index-audit",
            str(index_audit),
            "--inventory-output",
            str(inventory_output),
        ]
    )

    assert result == 0
    assert observed["parents"] == [shard]
    assert observed["source_parents"] == [shard]
    assert observed["output_directory"] == output
    assert json.loads(plan_output.read_text()) == observed["plan"]
    assert observed["inventory_arguments"][2] == result_root
    assert observed["inventory_arguments"][5] == output
    assert observed["inventory_arguments"][6] == expected_units
    assert observed["inventory_kwargs"] == {
        "refinement_source_plan": None,
        "refinement_source_execution": None,
        "refinement_ancestor_plan": None,
        "refinement_ancestor_execution": None,
    }
    assert json.loads(inventory_output.read_text())["status"] == "complete"
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


@pytest.mark.parametrize(
    "shard_path",
    ["../outside.json", "/tmp/outside.json"],
)
def test_cli_rejects_unsafe_shard_paths(tmp_path: Path, shard_path: str, monkeypatch):
    specification = tmp_path / "specification.json"
    execution = tmp_path / "execution.json"
    specification.write_text(json.dumps({"specification_sha256": "c" * 64}))
    execution.write_text(
        json.dumps(
            {
                "specification_sha256": "c" * 64,
                "units": [
                    {
                        "unit_id": "a" * 64,
                        "result_manifest_sha256": "b" * 64,
                        "valid": False,
                        "invalid_reasons": ["timed_out_repositories"],
                        "shard_path": shard_path,
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        partition_cli, "validate_execution_manifest", lambda *arguments, **kwargs: []
    )

    with pytest.raises(SystemExit, match="unsafe shard path"):
        partition_cli.main(
            [
                "--specification",
                str(specification),
                "--execution-manifest",
                str(execution),
                "--result-root",
                str(tmp_path / "results"),
            ]
        )


def test_cli_rejects_parent_shard_hash_mismatch(tmp_path: Path, monkeypatch):
    specification = tmp_path / "specification.json"
    execution = tmp_path / "execution.json"
    result_root = tmp_path / "results"
    shard_path = Path("shards/family/unit.json")
    (result_root / shard_path).parent.mkdir(parents=True)
    (result_root / shard_path).write_text(
        json.dumps(
            {
                "unit_id": "a" * 64,
                "result_manifest_sha256": "e" * 64,
                "valid": False,
                "invalid_reasons": ["timed_out_repositories"],
            }
        )
    )
    specification.write_text(json.dumps({"specification_sha256": "c" * 64}))
    execution.write_text(
        json.dumps(
            {
                "specification_sha256": "c" * 64,
                "units": [
                    {
                        "unit_id": "a" * 64,
                        "result_manifest_sha256": "b" * 64,
                        "valid": False,
                        "invalid_reasons": ["timed_out_repositories"],
                        "shard_path": shard_path.as_posix(),
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        partition_cli, "validate_execution_manifest", lambda *arguments, **kwargs: []
    )

    with pytest.raises(SystemExit, match="does not match execution manifest"):
        partition_cli.main(
            [
                "--specification",
                str(specification),
                "--execution-manifest",
                str(execution),
                "--result-root",
                str(result_root),
            ]
        )


def test_parent_loader_rejects_symlinked_shard_outside_result_root(tmp_path: Path):
    result_root = tmp_path / "results"
    shard_path = Path("shards/family/unit.json")
    outside = tmp_path / "outside-parent.json"
    outside.write_text(
        json.dumps(
            {
                "unit_id": "a" * 64,
                "result_manifest_sha256": "b" * 64,
                "valid": False,
                "invalid_reasons": ["timed_out_repositories"],
            }
        )
    )
    (result_root / shard_path).parent.mkdir(parents=True)
    (result_root / shard_path).symlink_to(outside)
    execution = {
        "units": [
            {
                "unit_id": "a" * 64,
                "result_manifest_sha256": "b" * 64,
                "valid": False,
                "invalid_reasons": ["timed_out_repositories"],
                "shard_path": shard_path.as_posix(),
            }
        ]
    }

    with pytest.raises(SystemExit, match="outside result root"):
        partition_cli._timed_out_parents(execution, result_root)


@pytest.mark.parametrize(
    ("execution", "message"),
    [
        ({}, "units must be a list"),
        (
            {
                "units": [
                    {
                        "valid": False,
                        "invalid_reasons": ["limit_hit_requires_partition"],
                        "unit_id": "a" * 64,
                    }
                ]
            },
            "non-timeout invalid units",
        ),
        (
            {
                "units": [
                    {
                        "valid": False,
                        "invalid_reasons": [
                            "timed_out_repositories",
                            "missing_repositories",
                        ],
                        "unit_id": "a" * 64,
                    }
                ]
            },
            "non-timeout invalid units",
        ),
        ({"units": []}, "no timed-out units"),
    ],
)
def test_parent_loader_rejects_incomplete_execution_boundaries(
    tmp_path: Path, execution: dict, message: str
):
    with pytest.raises(SystemExit, match=message):
        partition_cli._timed_out_parents(execution, tmp_path)


def test_cli_rejects_execution_from_another_specification(tmp_path: Path):
    specification = tmp_path / "specification.json"
    execution = tmp_path / "execution.json"
    specification.write_text(json.dumps({"specification_sha256": "a" * 64}))
    execution.write_text(json.dumps({"specification_sha256": "b" * 64, "units": []}))

    with pytest.raises(SystemExit, match="does not match"):
        partition_cli.main(
            [
                "--specification",
                str(specification),
                "--execution-manifest",
                str(execution),
            ]
        )


def test_cli_rejects_invalid_base_execution_manifest(tmp_path: Path, monkeypatch):
    specification = tmp_path / "specification.json"
    execution = tmp_path / "execution.json"
    specification.write_text(json.dumps({"specification_sha256": "a" * 64}))
    execution.write_text(json.dumps({"specification_sha256": "a" * 64, "units": []}))
    monkeypatch.setattr(
        partition_cli,
        "validate_execution_manifest",
        lambda *arguments, **kwargs: ["execution_manifest_sha256 does not match"],
    )

    with pytest.raises(SystemExit, match="base execution manifest is invalid"):
        partition_cli.main(
            [
                "--specification",
                str(specification),
                "--execution-manifest",
                str(execution),
                "--result-root",
                str(tmp_path / "results"),
            ]
        )
