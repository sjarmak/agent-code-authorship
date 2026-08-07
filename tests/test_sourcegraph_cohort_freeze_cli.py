import json
import sys
from pathlib import Path

from authorship import sourcegraph_cohort_freeze_cli


def test_cli_materializes_frozen_configuration(monkeypatch, tmp_path: Path, capsys):
    captured = {}

    def fake_materialize(paths, pins, **options):
        captured.update({"paths": paths, "pins": pins, **options})
        return {
            "cohort_freeze_sha256": "a" * 64,
            "cohorts": {
                "adoption_event_study": {
                    "primary": {
                        "languages": {
                            "Python": {"status": "identified", "adopter_count": 26},
                            "Go": {"status": "identified", "adopter_count": 21},
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(
        sourcegraph_cohort_freeze_cli,
        "materialize_cohort_freeze",
        fake_materialize,
    )
    pins = {
        name: str(index) * 64
        for index, name in enumerate(
            (
                "protocol",
                "discovery",
                "adoption",
                "agent_commits",
                "ai_ban",
                "targets",
                "survival",
            ),
            start=1,
        )
    }
    pins_path = tmp_path / "pins.json"
    pins_path.write_text(json.dumps(pins))
    output = tmp_path / "freeze.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sourcegraph-cohort-freeze",
            "--protocol",
            "protocol.json",
            "--discovery",
            "discovery.json",
            "--adoption",
            "adoption.json",
            "--agent-commits",
            "agent.json",
            "--ai-ban",
            "ban.json",
            "--targets",
            "targets.json",
            "--survival",
            "survival.json",
            "--pins",
            str(pins_path),
            "--output",
            str(output),
            "--simulation-replicates",
            "2500",
            "--simulation-seed",
            "77",
        ],
    )

    sourcegraph_cohort_freeze_cli.main()

    assert captured["output_path"] == output
    assert captured["simulation_replicates"] == 2500
    assert captured["simulation_seed"] == 77
    assert captured["discovery_start"] == "2023-01-01T00:00:00Z"
    assert captured["pins"] == pins
    assert captured["paths"]["agent_commits"] == Path("agent.json")
    assert json.loads(capsys.readouterr().out) == {
        "cohort_freeze_sha256": "a" * 64,
        "primary": {
            "Python": {"status": "identified", "adopter_count": 26},
            "Go": {"status": "identified", "adopter_count": 21},
        },
    }
