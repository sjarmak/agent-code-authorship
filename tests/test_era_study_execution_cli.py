import json
import hashlib
import sys

from authorship.era_study_execution_cli import execute_file, main


def test_cli_execution_writes_content_bound_artifact(tmp_path, monkeypatch, capsys):
    panel = {
        "panel_version": 1,
        "feature_id": "feature_a",
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": [
            {
                "repository_id": "treated",
                "language": "Python",
                "role": "adopter",
                "adoption_period": 2,
                "policy_effective_period": None,
                "observations": [
                    {"period": 0, "feature_value": 0},
                    {"period": 1, "feature_value": 1},
                    {"period": 3, "feature_value": 5},
                ],
            },
            {
                "repository_id": "never",
                "language": "Python",
                "role": "never_adopter_control",
                "adoption_period": None,
                "policy_effective_period": None,
                "observations": [
                    {"period": 0, "feature_value": 0},
                    {"period": 1, "feature_value": 1},
                    {"period": 3, "feature_value": 3},
                ],
            },
        ],
    }
    source = tmp_path / "panels.json"
    output = tmp_path / "result.json"
    materialization = {
        "era_panel_materialization_version": 1,
        "era_panel_plan_sha256": "b" * 64,
        "status": "complete",
        "period_result_count": 0,
        "period_results": [],
        "feature_count": 1,
        "panels": {"feature_a": panel},
    }
    materialization["era_panel_materialization_sha256"] = hashlib.sha256(
        json.dumps(
            materialization,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    source.write_text(json.dumps(materialization))

    document = execute_file(
        source,
        output,
        bootstrap_replicates=10,
        seed=1,
    )

    assert json.loads(output.read_text()) == document
    assert document["feature_count"] == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "era-study-execution",
            "--materialization",
            str(source),
            "--output",
            str(output),
            "--bootstrap-replicates",
            "10",
            "--seed",
            "1",
        ],
    )
    main()

    assert "sha256" in json.loads(capsys.readouterr().out)
