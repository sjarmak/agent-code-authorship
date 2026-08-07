import json
from pathlib import Path

from authorship import survival_estimate_execution_cli


def test_cli_executes_frozen_configuration(tmp_path: Path, monkeypatch, capsys):
    captured = {}

    def execute(**arguments):
        captured.update(arguments)
        return {
            "counts": {"strata": 20, "lines": 3},
            "survival_estimate_artifact_sha256": "a" * 64,
        }

    monkeypatch.setattr(
        survival_estimate_execution_cli, "execute_survival_estimates", execute
    )

    result = survival_estimate_execution_cli.main(
        [
            "--event-root",
            str(tmp_path / "events"),
            "--output",
            str(tmp_path / "estimate.json"),
            "--bootstrap-replicates",
            "25",
            "--seed",
            "77",
        ]
    )

    assert result == 0
    assert captured == {
        "event_root": tmp_path / "events",
        "output_path": tmp_path / "estimate.json",
        "bootstrap_replicates": 25,
        "seed": 77,
    }
    assert json.loads(capsys.readouterr().out) == {
        "counts": {"strata": 20, "lines": 3},
        "survival_estimate_artifact_sha256": "a" * 64,
    }
