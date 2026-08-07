import json
import sys
from pathlib import Path

from authorship import sourcegraph_era_panel_cli


def test_plan_command_writes_frozen_artifact(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_plan(freeze, adoption, index):
        captured.update(freeze=freeze, adoption=adoption, index=index)
        return {"era_panel_plan_sha256": "a" * 64, "repositories": []}

    monkeypatch.setattr(sourcegraph_era_panel_cli, "build_era_panel_plan", fake_plan)
    paths = {}
    for name in ("freeze", "adoption", "index"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"name": name}))
        paths[name] = path
    output = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sourcegraph-era-panel",
            "plan",
            "--freeze",
            str(paths["freeze"]),
            "--adoption",
            str(paths["adoption"]),
            "--index",
            str(paths["index"]),
            "--output",
            str(output),
        ],
    )

    sourcegraph_era_panel_cli.main()

    assert captured["freeze"] == {"name": "freeze"}
    assert json.loads(output.read_text())["era_panel_plan_sha256"] == "a" * 64


def test_materialize_command_loads_content_bound_periods(monkeypatch, tmp_path: Path):
    plan_path = tmp_path / "plan.json"
    execution_path = tmp_path / "execution.json"
    plan_path.write_text(json.dumps({"era_panel_plan_sha256": "a" * 64}))
    execution_path.write_text(json.dumps({"status": "complete"}))
    captured = {}

    monkeypatch.setattr(
        sourcegraph_era_panel_cli,
        "load_complete_period_results",
        lambda manifest, root, plan_sha: [
            {
                "root": str(root),
                "manifest": manifest,
                "plan_sha256": plan_sha,
            }
        ],
    )

    def fake_build(plan, periods):
        captured.update(plan=plan, periods=periods)
        return {"era_panel_materialization_sha256": "b" * 64}

    monkeypatch.setattr(sourcegraph_era_panel_cli, "build_feature_panels", fake_build)
    output = tmp_path / "materialization.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sourcegraph-era-panel",
            "materialize",
            "--plan",
            str(plan_path),
            "--execution",
            str(execution_path),
            "--execution-root",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )

    sourcegraph_era_panel_cli.main()

    assert captured["plan"]["era_panel_plan_sha256"] == "a" * 64
    assert captured["periods"][0]["root"] == str(tmp_path)
    assert captured["periods"][0]["plan_sha256"] == "a" * 64
    assert (
        json.loads(output.read_text())["era_panel_materialization_sha256"] == "b" * 64
    )
