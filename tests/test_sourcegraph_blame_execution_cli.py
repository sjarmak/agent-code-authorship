import json
from pathlib import Path

import authorship.sourcegraph_blame_execution_cli as blame_cli


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    specification = tmp_path / "specification.json"
    index_manifest = tmp_path / "index.json"
    inventory = tmp_path / "inventory.json"
    specification.write_text(json.dumps({"specification_sha256": "a" * 64}))
    index_manifest.write_text(json.dumps({"repositories": []}))
    inventory.write_text(
        json.dumps(
            {
                "effective_inventory_sha256": "b" * 64,
                "status": "complete",
            }
        )
    )
    return specification, index_manifest, inventory


def test_cli_builds_preliminary_blame_and_final_packet_indexes(
    tmp_path: Path, monkeypatch, capsys
):
    specification, index_manifest, inventory = write_inputs(tmp_path)
    base_root = tmp_path / "base"
    partition_root = tmp_path / "partition"
    output = tmp_path / "blame"
    plan_output = tmp_path / "plan.json"
    preliminary_output = tmp_path / "preliminary.json"
    final_output = tmp_path / "final.json"
    result_manifests = [{"result_manifest_sha256": "c" * 64}]
    enrichments = [{"sourcegraph_result_id": "sha256:" + "d" * 64}]
    observed = {}
    monkeypatch.setattr(
        blame_cli,
        "load_effective_result_manifests",
        lambda *args, **kwargs: result_manifests,
    )

    def build_packets(_spec, _index, manifests, *, file_blame_enrichments):
        observed.setdefault("packet_calls", []).append(
            (manifests, file_blame_enrichments)
        )
        return {
            "packet_index_sha256": (
                "e" * 64 if not file_blame_enrichments else "f" * 64
            ),
            "pending_file_enrichment_count": (1 if not file_blame_enrichments else 0),
            "pending_file_enrichments": (
                [{"sourcegraph_result_id": "sha256:" + "d" * 64}]
                if not file_blame_enrichments
                else []
            ),
        }

    plan = {
        "blame_plan_sha256": "1" * 64,
        "query_group_count": 1,
    }
    execution = {
        "status": "complete",
        "blame_execution_sha256": "2" * 64,
    }
    monkeypatch.setattr(blame_cli, "build_packet_index", build_packets)
    monkeypatch.setattr(blame_cli, "build_blame_plan", lambda _index: plan)
    monkeypatch.setattr(
        blame_cli, "check_auth", lambda: observed.setdefault("auth", True)
    )
    monkeypatch.setattr(
        blame_cli,
        "execute_blame_plan",
        lambda *args, **kwargs: execution,
    )
    monkeypatch.setattr(
        blame_cli,
        "validate_blame_execution",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        blame_cli,
        "load_execution_enrichments",
        lambda *args, **kwargs: enrichments,
    )

    result = blame_cli.main(
        [
            "--specification",
            str(specification),
            "--index-manifest",
            str(index_manifest),
            "--effective-inventory",
            str(inventory),
            "--base-root",
            str(base_root),
            "--partition-root",
            str(partition_root),
            "--output-directory",
            str(output),
            "--plan-output",
            str(plan_output),
            "--preliminary-packet-output",
            str(preliminary_output),
            "--final-packet-output",
            str(final_output),
        ]
    )

    assert result == 0
    assert observed["auth"] is True
    assert observed["packet_calls"] == [
        (result_manifests, []),
        (result_manifests, enrichments),
    ]
    assert json.loads(plan_output.read_text()) == plan
    assert json.loads(preliminary_output.read_text())["packet_index_sha256"] == "e" * 64
    assert json.loads(final_output.read_text())["packet_index_sha256"] == "f" * 64
    assert json.loads(capsys.readouterr().out) == execution


def test_cli_does_not_emit_final_index_for_incomplete_blame(
    tmp_path: Path, monkeypatch
):
    specification, index_manifest, inventory = write_inputs(tmp_path)
    final_output = tmp_path / "final.json"
    monkeypatch.setattr(
        blame_cli,
        "load_effective_result_manifests",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        blame_cli,
        "build_packet_index",
        lambda *args, **kwargs: {
            "packet_index_sha256": "e" * 64,
            "pending_file_enrichment_count": 0,
            "pending_file_enrichments": [],
        },
    )
    monkeypatch.setattr(
        blame_cli,
        "build_blame_plan",
        lambda _index: {"blame_plan_sha256": "1" * 64, "query_group_count": 0},
    )
    monkeypatch.setattr(
        blame_cli,
        "execute_blame_plan",
        lambda *args, **kwargs: {
            "status": "incomplete",
            "blame_execution_sha256": "2" * 64,
        },
    )
    monkeypatch.setattr(
        blame_cli,
        "validate_blame_execution",
        lambda *args, **kwargs: [],
    )

    result = blame_cli.main(
        [
            "--specification",
            str(specification),
            "--index-manifest",
            str(index_manifest),
            "--effective-inventory",
            str(inventory),
            "--output-directory",
            str(tmp_path / "blame"),
            "--plan-output",
            str(tmp_path / "plan.json"),
            "--preliminary-packet-output",
            str(tmp_path / "preliminary.json"),
            "--final-packet-output",
            str(final_output),
        ]
    )

    assert result == 2
    assert not final_output.exists()
