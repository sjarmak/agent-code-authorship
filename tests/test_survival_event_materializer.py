import hashlib
import json
import os
import subprocess
from pathlib import Path

import jsonschema
import pytest

from authorship.survival_event_materializer import (
    SurvivalEventMaterializationError,
    materialize_repository,
    survival_event_shard_manifest_sha256,
)


def git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, date: str, content: str | None) -> str:
    path = repo / "sample.py"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    git(repo, "add", "-A")
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    git(repo, "commit", "-q", "-m", message, env=environment)
    return git(repo, "rev-parse", "HEAD")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(f"{json.dumps(record, sort_keys=True)}\n" for record in records)
    )


def transition(
    line_id: str,
    horizon: int,
    *,
    merge_commit: str,
    terminal_commit: str | None,
    state: str,
) -> dict:
    return {
        "repository_id": "org/repo",
        "line_id": line_id,
        "merge_commit": merge_commit,
        "merged_at": "2025-01-01T00:00:00Z",
        "horizon_days": horizon,
        "horizon_status": "right_censored" if horizon == 365 else "observed",
        "state": state,
        "terminal_commit": terminal_commit,
        "terminal_reason": "file_deleted" if terminal_commit else None,
        "language": "Python",
        "agent_family": "Cursor",
        "provenance_tier": 2,
    }


@pytest.fixture
def materialization_inputs(tmp_path: Path) -> dict:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    merge = commit(repository, "merge", "2025-01-01T00:00:00Z", "value = 'original'\n")
    modification = commit(
        repository, "modify", "2025-03-02T00:00:00Z", "value = 'changed'\n"
    )
    deletion = commit(repository, "delete", "2025-05-01T00:00:00Z", None)
    transitions = tmp_path / "transitions.jsonl"
    records = [
        transition(
            "line-1",
            horizon,
            merge_commit=merge,
            terminal_commit=deletion,
            state="right_censored" if horizon == 365 else "unchanged",
        )
        for horizon in (365, 30, 180, 90)
    ]
    write_jsonl(transitions, records)
    structural = tmp_path / "structural.jsonl"
    write_jsonl(
        structural,
        [
            {
                "repository_id": "org/repo",
                "line_id": "line-1",
                "decision": "modified_candidate",
                "commit": modification,
            }
        ],
    )
    validation = tmp_path / "lineage-validation.json"
    validation.write_text(
        json.dumps(
            {
                "status": "complete",
                "headline_handling": "structural_candidates_map_to_modified",
                "minimum_structural_precision": 0.9,
                "structural_match_gate_passed": True,
                "structural_precision": 1.0,
            }
        )
    )
    return {
        "repository": repository,
        "transitions": transitions,
        "structural": structural,
        "validation": validation,
        "merge": merge,
        "modification": modification,
        "deletion": deletion,
    }


def run_materialization(tmp_path: Path, inputs: dict, **overrides):
    git_record = {
        "repository_id": "org/repo",
        "status": "pinned",
        "cutoff": "2025-07-20T00:00:00Z",
        "cutoff_commit": inputs["deletion"],
        "cutoff_tree": git(
            inputs["repository"], "show", "-s", "--format=%T", inputs["deletion"]
        ),
    }
    lineage_record = {
        "repository_id": "org/repo",
        "line_count": 1,
        "transition_count": 4,
        "transition_sha256": sha256(inputs["transitions"]),
        "structural_event_count": 1,
        "structural_event_sha256": sha256(inputs["structural"]),
    }
    parameters = {
        "repository_id": "org/repo",
        "repository_path": inputs["repository"],
        "transition_path": inputs["transitions"],
        "structural_event_path": inputs["structural"],
        "lineage_validation_path": inputs["validation"],
        "git_inventory_record": git_record,
        "lineage_inventory_record": lineage_record,
        "git_inventory_sha256": "a" * 64,
        "output_path": tmp_path / "events.jsonl",
        "manifest_path": tmp_path / "manifest.json",
        "work_root": tmp_path / "work",
        "commit_batch_size": 1,
    }
    return materialize_repository(**{**parameters, **overrides})


def test_materializes_exact_events_from_shuffled_snapshots_and_pinned_git(
    tmp_path: Path, materialization_inputs: dict
):
    result = run_materialization(tmp_path, materialization_inputs)

    line = json.loads((tmp_path / "events.jsonl").read_text())
    assert line["censor_time_days"] == 200.0
    assert line["transitions"] == [
        {
            "from_state": "unchanged",
            "time_days": 60.0,
            "to_state": "modified",
        },
        {
            "from_state": "modified",
            "time_days": 120.0,
            "to_state": "deleted",
        },
    ]
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert result.reused is False
    assert result.manifest == manifest
    assert manifest["contract_version"] == 3
    assert manifest["input_sha256s"]["lineage_validation"] == sha256(
        materialization_inputs["validation"]
    )
    assert manifest["line_count"] == 1
    assert manifest["censoring_audit"]["terminal_deletion"] == 1
    assert manifest["event_shard_sha256"] == sha256(tmp_path / "events.jsonl")
    assert manifest["survival_event_shard_manifest_sha256"] == (
        survival_event_shard_manifest_sha256(manifest)
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/survival-event-shard-manifest.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(manifest)

    stale = {**manifest, "contract_version": 1}
    stale["survival_event_shard_manifest_sha256"] = (
        survival_event_shard_manifest_sha256(stale)
    )
    (tmp_path / "manifest.json").write_text(json.dumps(stale))
    refreshed = run_materialization(tmp_path, materialization_inputs)
    assert refreshed.reused is False
    assert refreshed.manifest["contract_version"] == 3

    reused = run_materialization(tmp_path, materialization_inputs)
    assert reused.reused is True
    assert reused.manifest == refreshed.manifest


def test_rejects_tampered_input_before_replacing_outputs(
    tmp_path: Path, materialization_inputs: dict
):
    original_sha256 = sha256(materialization_inputs["transitions"])
    materialization_inputs["transitions"].write_text("{}\n")

    with pytest.raises(SurvivalEventMaterializationError, match="checksum"):
        run_materialization(
            tmp_path,
            materialization_inputs,
            lineage_inventory_record={
                "repository_id": "org/repo",
                "line_count": 1,
                "transition_count": 4,
                "transition_sha256": original_sha256,
                "structural_event_count": 1,
                "structural_event_sha256": sha256(materialization_inputs["structural"]),
            },
        )

    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_rejects_pinned_tree_mismatch(tmp_path: Path, materialization_inputs: dict):
    with pytest.raises(SurvivalEventMaterializationError, match="cutoff tree"):
        run_materialization(
            tmp_path,
            materialization_inputs,
            git_inventory_record={
                "repository_id": "org/repo",
                "status": "pinned",
                "cutoff": "2025-07-20T00:00:00Z",
                "cutoff_commit": materialization_inputs["deletion"],
                "cutoff_tree": "f" * 40,
            },
        )


def test_rejects_structural_promotion_when_validation_gate_fails(
    tmp_path: Path, materialization_inputs: dict
):
    validation = json.loads(materialization_inputs["validation"].read_text())
    validation["structural_match_gate_passed"] = False
    validation["structural_precision"] = 0.5
    materialization_inputs["validation"].write_text(json.dumps(validation))

    with pytest.raises(SurvivalEventMaterializationError, match="validation gate"):
        run_materialization(tmp_path, materialization_inputs)

    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_rejects_validation_artifact_that_weakens_locked_precision(
    tmp_path: Path, materialization_inputs: dict
):
    validation = json.loads(materialization_inputs["validation"].read_text())
    validation["minimum_structural_precision"] = 0.0
    validation["structural_precision"] = 0.0
    materialization_inputs["validation"].write_text(json.dumps(validation))

    with pytest.raises(SurvivalEventMaterializationError, match="validation gate"):
        run_materialization(tmp_path, materialization_inputs)


def test_rejects_missing_horizon_and_structural_population_mismatch(
    tmp_path: Path, materialization_inputs: dict
):
    records = [
        json.loads(line)
        for line in materialization_inputs["transitions"].read_text().splitlines()
    ]
    write_jsonl(materialization_inputs["transitions"], records[:-1])
    with pytest.raises(SurvivalEventMaterializationError, match="transition count"):
        run_materialization(tmp_path, materialization_inputs)

    write_jsonl(materialization_inputs["transitions"], records)
    structural = json.loads(materialization_inputs["structural"].read_text())
    structural["line_id"] = "unknown"
    write_jsonl(materialization_inputs["structural"], [structural])
    with pytest.raises(SurvivalEventMaterializationError, match="population"):
        run_materialization(tmp_path, materialization_inputs)
