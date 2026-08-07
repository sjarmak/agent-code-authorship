import json
from pathlib import Path

from authorship import survival_event_batch_cli


def arguments(tmp_path: Path) -> list[str]:
    return [
        "--git-inventory",
        str(tmp_path / "git.json"),
        "--lineage-inventory",
        str(tmp_path / "lineage.json"),
        "--lineage-validation",
        str(tmp_path / "lineage-validation.json"),
        "--transition-root",
        str(tmp_path / "transitions"),
        "--structural-event-root",
        str(tmp_path / "structural"),
        "--output-root",
        str(tmp_path / "output"),
        "--work-root",
        str(tmp_path / "work"),
        "--commit-batch-size",
        "17",
    ]


def test_cli_reports_progress_and_returns_zero_for_complete(
    tmp_path: Path, monkeypatch, capsys
):
    captured = {}

    def execute(**values):
        captured.update(values)
        values["progress"]({"repository_id": "org/repo", "status": "valid"}, 1, 1)
        return {"status": "complete", "counts": {"valid": 1}}

    monkeypatch.setattr(
        survival_event_batch_cli, "execute_event_materialization", execute
    )

    assert survival_event_batch_cli.main(arguments(tmp_path)) == 0
    assert captured["commit_batch_size"] == 17
    assert captured["lineage_validation_path"] == (tmp_path / "lineage-validation.json")
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "complete"
    assert "[1/1] org/repo: valid" in output.err


def test_cli_returns_one_for_incomplete(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        survival_event_batch_cli,
        "execute_event_materialization",
        lambda **_: {"status": "incomplete", "counts": {"invalid": 1}},
    )

    assert survival_event_batch_cli.main(arguments(tmp_path)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
