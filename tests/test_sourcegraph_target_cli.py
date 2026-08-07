import json

from authorship import sourcegraph_target_cli


def _write(path, value):
    path.write_text(json.dumps(value))


def _inputs(tmp_path):
    targets = {
        "repositories": [
            {
                "id": "acme/alpha",
                "role": "target",
                "label": "unlabeled",
                "languages": ["Python"],
                "snapshot": {
                    "commit": "a" * 40,
                    "tree": "b" * 40,
                    "committed_at": "2026-01-01T00:00:00Z",
                },
                "effective_date_range": [
                    "2024-01-01T00:00:00Z",
                    "2026-01-01T23:59:59Z",
                ],
                "excluded_paths": ["vendored", "generated"],
            }
        ]
    }
    index = {
        "repositories": [
            {
                "canonical_repository_id": "acme/alpha",
                "cutoff_commit": "a" * 40,
                "cutoff_tree": "b" * 40,
                "roles": ["prevalence_target"],
                "sourcegraph": {
                    "selected_name": "github.com/sg-evals/acme-alpha",
                    "transport_status": "ready_mirror",
                },
            }
        ]
    }
    features = {"schema_version": 1, "names": ["log_lines"], "languages": ["Python"]}
    paths = {
        "targets": tmp_path / "targets.json",
        "index": tmp_path / "index.json",
        "features": tmp_path / "features.json",
    }
    _write(paths["targets"], targets)
    _write(paths["index"], index)
    _write(paths["features"], features)
    return paths


def _tree(_name, revision):
    return {"oid": revision, "paths": ["src/main.py"]}


def _file(task):
    return {
        "repository_name": task["sourcegraph_name"],
        "commit_oid": task["cutoff_commit"],
        "path": task["path"],
        "content": "value = 1\n",
        "blame": [
            {
                "startLine": 1,
                "endLine": 2,
                "commit": {
                    "oid": "c" * 40,
                    "author": {"date": "2025-01-01T00:00:00Z"},
                    "committer": {"date": "2025-01-01T00:00:00Z"},
                },
            }
        ],
    }


def test_cli_runs_plan_execute_and_materialize(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    plan = tmp_path / "plan.json"
    root = tmp_path / "execution"
    execution = root / "execution.json"
    materialization = tmp_path / "materialization.json"
    monkeypatch.setattr(sourcegraph_target_cli, "fetch_tree_files", _tree)
    monkeypatch.setattr(sourcegraph_target_cli, "fetch_target_file", _file)

    assert (
        sourcegraph_target_cli.main(
            [
                "plan",
                "--targets",
                str(paths["targets"]),
                "--index-manifest",
                str(paths["index"]),
                "--features",
                str(paths["features"]),
                "--output",
                str(plan),
            ]
        )
        == 0
    )
    assert (
        sourcegraph_target_cli.main(
            [
                "execute",
                "--plan",
                str(plan),
                "--targets",
                str(paths["targets"]),
                "--index-manifest",
                str(paths["index"]),
                "--features",
                str(paths["features"]),
                "--output-root",
                str(root),
                "--manifest",
                str(execution),
                "--workers",
                "1",
            ]
        )
        == 0
    )
    assert (
        sourcegraph_target_cli.main(
            [
                "materialize",
                "--plan",
                str(plan),
                "--targets",
                str(paths["targets"]),
                "--index-manifest",
                str(paths["index"]),
                "--features",
                str(paths["features"]),
                "--execution",
                str(execution),
                "--output-root",
                str(root),
                "--output",
                str(materialization),
            ]
        )
        == 0
    )
    assert json.loads(materialization.read_text())["unit_count"] == 1


def test_cli_persists_blocked_plan_and_returns_nonzero(tmp_path, monkeypatch):
    paths = _inputs(tmp_path)
    output = tmp_path / "plan.json"

    def unavailable(_name, _revision):
        raise RuntimeError("not indexed")

    monkeypatch.setattr(sourcegraph_target_cli, "fetch_tree_files", unavailable)

    status = sourcegraph_target_cli.main(
        [
            "plan",
            "--targets",
            str(paths["targets"]),
            "--index-manifest",
            str(paths["index"]),
            "--features",
            str(paths["features"]),
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert (
        json.loads(output.read_text())["status"] == "blocked_missing_indexed_revision"
    )
