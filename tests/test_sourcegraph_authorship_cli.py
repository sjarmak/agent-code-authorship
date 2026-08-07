from __future__ import annotations

import json

import pytest

from authorship import sourcegraph_authorship_cli


def _write(path, document: dict) -> None:
    path.write_text(json.dumps(document))


def test_cli_rejects_noncanonical_python_runtime() -> None:
    with pytest.raises(SystemExit, match="CPython 3.12.3"):
        sourcegraph_authorship_cli._require_runtime((3, 11, 15))


def test_plan_command_builds_validated_atomic_artifact(
    tmp_path, monkeypatch, capsys
) -> None:
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("protocol", "freeze", "agent_catalog", "targets", "index_manifest")
    }
    for path in paths.values():
        _write(path, {})
    output = tmp_path / "plan.json"
    generated = {"status": "frozen", "counts": {"agent_commits": 1}}
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "build_authorship_unit_plan",
        lambda *_args: generated,
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_authorship_unit_plan",
        lambda document, targets=None: [],
    )

    result = sourcegraph_authorship_cli.main(
        [
            "plan",
            "--protocol",
            str(paths["protocol"]),
            "--freeze",
            str(paths["freeze"]),
            "--agent-catalog",
            str(paths["agent_catalog"]),
            "--targets",
            str(paths["targets"]),
            "--index-manifest",
            str(paths["index_manifest"]),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text()) == generated
    assert json.loads(capsys.readouterr().out)["command"] == "plan"


def test_discover_and_execute_commands_write_validated_artifacts(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path, candidates_path = tmp_path / "plan.json", tmp_path / "candidates.json"
    targets_path = tmp_path / "targets.json"
    _write(plan_path, {"authorship_unit_plan_sha256": "a" * 64})
    _write(candidates_path, {"candidate_manifest_sha256": "b" * 64})
    _write(targets_path, {})
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_authorship_unit_plan",
        lambda document, targets=None: [],
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_candidate_manifest",
        lambda document, plan, **_kwargs: [],
    )
    discovered = {"status": "frozen", "counts": {"total_commits": 2}}
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "build_candidate_manifest",
        lambda plan: discovered,
    )
    discovered_path = tmp_path / "discovered.json"
    assert (
        sourcegraph_authorship_cli.main(
            [
                "discover",
                "--plan",
                str(plan_path),
                "--targets",
                str(targets_path),
                "--output",
                str(discovered_path),
            ]
        )
        == 0
    )
    executed = {"status": "complete", "counts": {"successful_tasks": 2}}
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "execute_authorship_units",
        lambda plan, candidates, root, max_workers: executed,
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_execution_manifest",
        lambda document, plan, candidates, root: [],
    )
    execution_path = tmp_path / "execution.json"
    assert (
        sourcegraph_authorship_cli.main(
            [
                "execute",
                "--plan",
                str(plan_path),
                "--targets",
                str(targets_path),
                "--candidates",
                str(candidates_path),
                "--output-root",
                str(tmp_path),
                "--manifest",
                str(execution_path),
                "--workers",
                "2",
            ]
        )
        == 0
    )
    assert json.loads(discovered_path.read_text()) == discovered
    assert json.loads(execution_path.read_text()) == executed
    assert len(capsys.readouterr().out.strip().splitlines()) == 2


def test_materialize_command_validates_loads_and_writes_frozen_units(
    tmp_path, monkeypatch, capsys
) -> None:
    plan_path = tmp_path / "plan.json"
    candidates_path = tmp_path / "candidates.json"
    execution_path = tmp_path / "execution.json"
    output_path = tmp_path / "materialization.json"
    targets_path = tmp_path / "targets.json"
    for path, document in (
        (plan_path, {"authorship_unit_plan_sha256": "a" * 64}),
        (candidates_path, {"candidate_manifest_sha256": "b" * 64}),
        (execution_path, {"authorship_execution_sha256": "c" * 64}),
        (targets_path, {}),
    ):
        path.write_text(json.dumps(document))
    materialization = {
        "authorship_materialization_version": 1,
        "counts": {"agent": 1, "H1": 0, "H2": 0, "H3": 1},
        "status": "complete",
    }
    loaded = [{"task": {"task_id": "d" * 64}, "units": []}]
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_authorship_unit_plan",
        lambda _doc, _targets: [],
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_candidate_manifest",
        lambda _doc, _plan, **_kwargs: [],
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "load_execution_shards",
        lambda execution, plan, candidates, root: loaded,
        raising=False,
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "materialize_authorship_units",
        lambda plan, execution, shards: materialization,
        raising=False,
    )
    monkeypatch.setattr(
        sourcegraph_authorship_cli,
        "validate_authorship_materialization",
        lambda document, plan, execution: [],
        raising=False,
    )

    result = sourcegraph_authorship_cli.main(
        [
            "materialize",
            "--plan",
            str(plan_path),
            "--targets",
            str(targets_path),
            "--candidates",
            str(candidates_path),
            "--execution",
            str(execution_path),
            "--output-root",
            str(tmp_path),
            "--output",
            str(output_path),
        ]
    )

    assert result == 0
    assert json.loads(output_path.read_text()) == materialization
    assert json.loads(capsys.readouterr().out)["counts"] == materialization["counts"]
