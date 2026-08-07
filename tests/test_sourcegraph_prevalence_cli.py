import json
from types import SimpleNamespace

import authorship.sourcegraph_prevalence_cli as cli
from authorship.sourcegraph_prevalence_cli import execute_file, freeze_file


def test_freeze_cli_writes_content_bound_specification(tmp_path):
    protocol = {
        "protocol_version": 3,
        "status": "preregistered",
        "protocol_sha256": "a" * 64,
        "languages": ["Python", "Go"],
        "authorship": {
            "partial_identification": {
                "enabled": True,
                "method": "union_over_evidence_tiers_and_contamination_grid",
                "contamination_grid_step": 0.05,
                "maximum_headline_width": 0.3,
                "failed_width_result": "not_identified",
            }
        },
        "human_evidence": {
            "tiers": {
                "H1": {"contamination_range": [0.0, 0.0]},
                "H2": {"contamination_range": [0.0, 0.2]},
                "H3": {"contamination_range": [0.0, 0.25]},
            }
        },
        "identification_gates": {
            "minimum_adopters_per_language": 20,
            "minimum_controls_per_language": 10,
            "minimum_agent_family_repositories": 8,
            "failed_gate_result": "not_identified",
        },
    }
    features = {"status": "frozen", "names": ["f1"]}
    plan = {
        "target_unit_plan_sha256": "b" * 64,
        "outcomes_consulted": False,
        "primary_weight": "line_count_times_file_sampling_weight",
    }
    authorship = {
        "status": "complete",
        "authorship_materialization_sha256": "c" * 64,
    }
    era = {
        "headline_inference_allowed": False,
        "era_study_execution_sha256": "d" * 64,
    }
    cohort = {"cohort_freeze_sha256": "e" * 64, "agent_family_gates": []}
    paths = {
        "protocol": tmp_path / "protocol.json",
        "features": tmp_path / "features.json",
        "plan": tmp_path / "plan.json",
        "authorship": tmp_path / "authorship.json",
        "era": tmp_path / "era.json",
        "cohort": tmp_path / "cohort.json",
        "output": tmp_path / "spec.json",
    }
    for key, document in (
        ("protocol", protocol),
        ("features", features),
        ("plan", plan),
        ("authorship", authorship),
        ("era", era),
        ("cohort", cohort),
    ):
        paths[key].write_text(json.dumps(document))

    result = freeze_file(
        paths["protocol"],
        paths["features"],
        paths["plan"],
        paths["authorship"],
        paths["era"],
        paths["cohort"],
        paths["output"],
    )

    assert json.loads(paths["output"].read_text()) == result
    assert result["prevalence_analysis_spec_sha256"]


def test_execute_file_loads_every_bound_input_and_writes_result(tmp_path, monkeypatch):
    names = (
        "specification",
        "target_plan",
        "authorship",
        "target",
        "era",
        "cohort",
        "features",
    )
    paths = {name: tmp_path / f"{name}.json" for name in names}
    output = tmp_path / "execution.json"
    for name, path in paths.items():
        path.write_text(json.dumps({"name": name}))

    def fake_run(*documents):
        assert [document["name"] for document in documents] == list(names)
        return {"prevalence_execution_sha256": "a" * 64}

    monkeypatch.setattr(cli, "run_prevalence_analysis", fake_run)
    result = execute_file(
        *(paths[name] for name in names),
        output,
    )

    assert json.loads(output.read_text()) == result


def test_main_dispatches_freeze_and_prints_hash(monkeypatch, capsys):
    arguments = SimpleNamespace(
        command="freeze",
        protocol="protocol",
        features="features",
        target_plan="plan",
        authorship="authorship",
        era="era",
        cohort="cohort",
        output="output",
    )
    monkeypatch.setattr(
        cli, "_parser", lambda: SimpleNamespace(parse_args=lambda: arguments)
    )
    monkeypatch.setattr(
        cli,
        "freeze_file",
        lambda *_: {"prevalence_analysis_spec_sha256": "f" * 64},
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {"sha256": "f" * 64}
