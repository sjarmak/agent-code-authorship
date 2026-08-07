import copy
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import jsonschema
import pytest

from authorship.build_survival_transitions import line_id
from authorship.sourcegraph_longitudinal import (
    LongitudinalExtractionError,
    build_longitudinal_shard,
    longitudinal_shard_sha256,
)
from authorship.sourcegraph_longitudinal_execution import (
    _file_age_days,
    _file_age_record,
    build_longitudinal_unit,
    execute_longitudinal_unit,
    shard_path_for,
)
from authorship.sourcegraph_longitudinal_validation import (
    validate_longitudinal_shard,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_A = "1" * 40


@pytest.fixture
def protocol() -> dict:
    path = Path(__file__).resolve().parents[1] / "study/protocol.v3.json"
    return json.loads(path.read_text())


def repository() -> dict:
    return {
        "canonical_repository_id": "org/repo",
        "sourcegraph_name": "github.com/sg-evals/org-repo",
        "cutoff_commit": SHA_A,
        "cutoff_tree": TREE_A,
        "bundle_sha256": "2" * 64,
    }


def introduction(path="src/main.py", line_number=10) -> dict:
    return {
        "repository_id": "org/repo",
        "merge_commit": SHA_B,
        "diff_base": "c" * 40,
        "merged_at": "2025-01-10T00:00:00Z",
        "path": path,
        "line_number": line_number,
        "content_sha256": f"{line_number:064x}",
        "text": f"value_{line_number} = True",
        "language": "Python",
        "agent_family": "OpenAI_Codex",
        "provenance_tier": 2,
        "pr_number": 7,
    }


def transitions_for(record: dict) -> list[dict]:
    identifier = line_id(record)
    states = {
        30: "unchanged",
        90: "modified_candidate",
        180: "deleted",
        365: "right_censored",
    }
    return [
        {
            "line_id": identifier,
            "repository_id": record["repository_id"],
            "horizon_days": horizon,
            "horizon_status": (
                "right_censored" if state == "right_censored" else "observed"
            ),
            "state": state,
        }
        for horizon, state in states.items()
    ]


def sourcegraph_observation(*records: dict, mismatch=False) -> dict:
    hunks = []
    seen = set()
    for record in records:
        key = (record["merge_commit"], record["path"])
        if key in seen:
            continue
        seen.add(key)
        hunks.append(
            {
                "introducing_commit_oid": (
                    "d" * 40 if mismatch else record["merge_commit"]
                ),
                "path": record["path"],
                "sourcegraph_result_ids": ["sha256:" + "4" * 64],
                "blame_commit_oids": [record["merge_commit"]],
            }
        )
    return {
        "indexed_revision_oid": SHA_A,
        "capabilities_used": [
            "revision_search",
            "diff_search",
            "blame",
        ],
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "result_manifest_sha256s": ["3" * 64],
        "hunks": hunks,
    }


def build(
    protocol: dict,
    records: list[dict],
    *,
    observation: dict | None = None,
) -> dict:
    transitions = [
        transition for record in records for transition in transitions_for(record)
    ]
    ages = {f"{record['merge_commit']}\0{record['path']}": 120.5 for record in records}
    return build_longitudinal_shard(
        protocol,
        repository(),
        records,
        transitions,
        execution_unit_id="5" * 64,
        file_age_days=ages,
        sourcegraph_observation=(
            observation
            if observation is not None
            else sourcegraph_observation(*records)
        ),
    )


def test_groups_lines_into_hunks_and_captures_required_metadata(protocol: dict):
    first = introduction(line_number=10)
    second = introduction(line_number=11)

    shard = build(protocol, [first, second])

    assert shard["hunk_count"] == 1
    hunk = shard["hunks"][0]
    assert hunk["introducing_commit_oid"] == SHA_B
    assert hunk["event_time"] == "2025-01-10T00:00:00Z"
    assert hunk["path"] == "src/main.py"
    assert hunk["language"] == "Python"
    assert hunk["change_size_lines"] == 2
    assert hunk["code_age_days"] == 120.5
    assert hunk["evidence_tier"] == "tier_2"
    assert hunk["lineage"]["30"]["unchanged"] == 2
    assert hunk["lineage"]["365"]["right_censored"] == 2
    assert hunk["observability"]["365"]["right_censored_lines"] == 2
    assert hunk["sourcegraph_evidence"] == {
        "sourcegraph_result_ids": ["sha256:" + "4" * 64],
        "blame_commit_oids": [SHA_B],
    }


def test_vendored_and_generated_paths_are_retained_as_exclusions(protocol: dict):
    included = introduction()
    excluded = introduction("vendor/generated.pb.go", 20)

    shard = build(protocol, [included, excluded])

    assert shard["hunk_count"] == 1
    assert shard["excluded_hunk_count"] == 1
    assert shard["exclusions"][0]["reason"] == "vendored_or_generated_path"
    assert shard["exclusions"][0]["path"] == "vendor/generated.pb.go"


def test_sourcegraph_disagreement_is_explicit_and_git_remains_authoritative(
    protocol: dict,
):
    record = introduction()

    shard = build(
        protocol,
        [record],
        observation=sourcegraph_observation(record, mismatch=True),
    )

    assert shard["sourcegraph"]["verification_status"] == "disagreement"
    assert shard["sourcegraph_git_disagreement_count"] == 1
    assert shard["sourcegraph_git_disagreements"][0]["fields"] == [
        "introducing_commit_oid"
    ]
    assert shard["pinned_git"]["authoritative_for_lineage"] is True


def test_missing_sourcegraph_hunk_is_incomplete_not_silently_verified(protocol: dict):
    record = introduction()
    observation = sourcegraph_observation()

    shard = build(protocol, [record], observation=observation)

    assert shard["sourcegraph"]["verification_status"] == "incomplete"
    assert shard["hunks"][0]["sourcegraph_verification"] == "missing"
    assert shard["hunks"][0]["sourcegraph_evidence"] is None


def test_requires_complete_horizon_cardinality(protocol: dict):
    record = introduction()
    transitions = transitions_for(record)[:-1]

    with pytest.raises(LongitudinalExtractionError, match="all frozen horizons"):
        build_longitudinal_shard(
            protocol,
            repository(),
            [record],
            transitions,
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )


def test_rejects_transition_repository_and_status_inconsistency(protocol: dict):
    record = introduction()
    wrong_repository = transitions_for(record)
    wrong_repository[0]["repository_id"] = "other/repo"
    with pytest.raises(LongitudinalExtractionError, match="transition repository"):
        build_longitudinal_shard(
            protocol,
            repository(),
            [record],
            wrong_repository,
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )
    wrong_status = transitions_for(record)
    wrong_status[-1]["horizon_status"] = "observed"
    with pytest.raises(LongitudinalExtractionError, match="horizon status"):
        build_longitudinal_shard(
            protocol,
            repository(),
            [record],
            wrong_status,
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )
    invalid_status = transitions_for(record)
    invalid_status[0]["horizon_status"] = "unknown"
    with pytest.raises(LongitudinalExtractionError, match="horizon status"):
        build_longitudinal_shard(
            protocol,
            repository(),
            [record],
            invalid_status,
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )


def test_rejects_mutated_protocol_and_cutoff_mismatch(protocol: dict):
    record = introduction()
    changed = copy.deepcopy(protocol)
    changed["survival"]["horizons_days"] = [30]

    with pytest.raises(LongitudinalExtractionError, match="protocol_sha256"):
        build(changed, [record])
    wrong_revision = sourcegraph_observation(record)
    wrong_revision["indexed_revision_oid"] = "e" * 40
    with pytest.raises(LongitudinalExtractionError, match="indexed revision"):
        build(protocol, [record], observation=wrong_revision)
    forbidden = sourcegraph_observation(record)
    forbidden["capabilities_used"] = ["SCIP"]
    with pytest.raises(LongitudinalExtractionError, match="capabilities"):
        build(protocol, [record], observation=forbidden)
    duplicates = sourcegraph_observation(record)
    duplicates["result_manifest_sha256s"] *= 2
    with pytest.raises(LongitudinalExtractionError, match="checksums"):
        build(protocol, [record], observation=duplicates)
    naive_timestamp = {**record, "merged_at": "2025-01-10T00:00:00"}
    with pytest.raises(LongitudinalExtractionError, match="timezone"):
        build(protocol, [naive_timestamp])
    invalid_repository = repository()
    invalid_repository["sourcegraph_name"] = "github.com/other/repo"
    with pytest.raises(LongitudinalExtractionError, match="sg-evals"):
        build_longitudinal_shard(
            protocol,
            invalid_repository,
            [record],
            transitions_for(record),
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )


def test_shard_is_deterministic_checksummed_and_matches_schema(protocol: dict):
    first = introduction(line_number=10)
    second = introduction(line_number=11)

    forward = build(protocol, [first, second])
    reverse = build(protocol, [second, first])

    assert forward == reverse
    assert forward["longitudinal_shard_sha256"] == longitudinal_shard_sha256(forward)
    assert validate_longitudinal_shard(forward, protocol) == []
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "study" / "sourcegraph-longitudinal-shard.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(forward)


def test_validator_rejects_corruption_without_crashing(protocol: dict):
    shard = build(protocol, [introduction()])
    changed = copy.deepcopy(shard)
    changed["hunks"][0]["change_size_lines"] = 99

    assert "longitudinal_shard_sha256 does not match" in (
        validate_longitudinal_shard(changed, protocol)
    )
    assert validate_longitudinal_shard({"hunks": [None]}, protocol)
    malformed = copy.deepcopy(shard)
    malformed["hunks"][0]["lineage"]["30"] = None
    assert "hunk lineage counts must be objects" in validate_longitudinal_shard(
        malformed, protocol
    )
    malformed = copy.deepcopy(shard)
    malformed["pinned_git"]["authoritative_for_lineage"] = False
    assert "pinned Git must be authoritative for lineage" in (
        validate_longitudinal_shard(malformed, protocol)
    )
    malformed = copy.deepcopy(shard)
    malformed["hunks"][0]["lineage"]["30"]["unchanged"] = "one"
    assert validate_longitudinal_shard(malformed, protocol)


def test_validator_reports_top_level_contract_failures(protocol: dict):
    shard = build(protocol, [introduction()])
    assert validate_longitudinal_shard(None, protocol) == [
        "longitudinal shard must be an object"
    ]
    for field, value, expected in (
        (
            "longitudinal_shard_version",
            2,
            "longitudinal_shard_version must equal 3",
        ),
        ("execution_unit_id", "invalid", "execution_unit_id is invalid"),
        ("protocol_sha256", "f" * 64, "protocol_sha256 does not match"),
        ("outcomes_consulted", True, "longitudinal shard must be outcome blind"),
        ("hunk_count", 99, "hunk_count does not match"),
        ("excluded_hunk_count", 99, "excluded_hunk_count does not match"),
        (
            "sourcegraph_git_disagreement_count",
            99,
            "sourcegraph_git_disagreement_count does not match",
        ),
    ):
        changed = copy.deepcopy(shard)
        changed[field] = value
        assert expected in validate_longitudinal_shard(changed, protocol)
    malformed = copy.deepcopy(shard)
    malformed["exclusions"] = None
    assert validate_longitudinal_shard(malformed, protocol) == [
        "hunks, exclusions, and disagreements must be lists"
    ]
    duplicated = copy.deepcopy(shard)
    duplicated["hunks"].append(copy.deepcopy(duplicated["hunks"][0]))
    duplicated["hunk_count"] = 2
    assert "hunk IDs must be unique" in validate_longitudinal_shard(
        duplicated, protocol
    )


def test_rejects_unused_transitions_and_malformed_introductions(protocol: dict):
    record = introduction()
    unused = {**transitions_for(record)[0], "line_id": "f" * 64}
    with pytest.raises(LongitudinalExtractionError, match="unused transitions"):
        build_longitudinal_shard(
            protocol,
            repository(),
            [record],
            [*transitions_for(record), unused],
            execution_unit_id="5" * 64,
            file_age_days={f"{SHA_B}\0src/main.py": 2.0},
            sourcegraph_observation=sourcegraph_observation(record),
        )
    malformed = {**record, "line_number": True}
    with pytest.raises(LongitudinalExtractionError, match="line_number"):
        build(protocol, [malformed])


def test_all_excluded_hunks_are_not_reported_as_sourcegraph_verified(protocol: dict):
    excluded = introduction("vendor/generated.pb.go", 20)

    shard = build(protocol, [excluded])

    assert shard["sourcegraph"]["verification_status"] == "no_included_hunks"


def test_executor_reuses_only_valid_identity_bound_shards(
    protocol: dict, tmp_path: Path
):
    record = introduction()
    calls = {"sourcegraph": 0, "git": 0}

    def sourcegraph_probe(_unit):
        calls["sourcegraph"] += 1
        return sourcegraph_observation(record)

    def git_extractor(_unit):
        calls["git"] += 1
        return {
            "introductions": [record],
            "transitions": transitions_for(record),
            "file_age_days": {f"{SHA_B}\0src/main.py": 12.0},
        }

    unit = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "introductions", "sha256": "6" * 64},
        transition_shard={"path": "transitions", "sha256": "7" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64],
    )
    first = execute_longitudinal_unit(
        protocol,
        unit,
        tmp_path,
        sourcegraph_probe=sourcegraph_probe,
        git_extractor=git_extractor,
    )
    second = execute_longitudinal_unit(
        protocol,
        unit,
        tmp_path,
        sourcegraph_probe=sourcegraph_probe,
        git_extractor=git_extractor,
    )

    assert first["reused"] is False
    assert second["reused"] is True
    assert calls == {"sourcegraph": 1, "git": 1}
    changed_unit = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "introductions", "sha256": "6" * 64},
        transition_shard={"path": "transitions", "sha256": "8" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64],
    )
    changed = execute_longitudinal_unit(
        protocol,
        changed_unit,
        tmp_path,
        sourcegraph_probe=sourcegraph_probe,
        git_extractor=git_extractor,
    )
    assert changed["reused"] is False
    assert calls == {"sourcegraph": 2, "git": 2}
    path = tmp_path / shard_path_for(unit)
    path.write_text("{}")
    third = execute_longitudinal_unit(
        protocol,
        unit,
        tmp_path,
        sourcegraph_probe=sourcegraph_probe,
        git_extractor=git_extractor,
    )
    assert third["reused"] is False
    assert calls == {"sourcegraph": 3, "git": 3}


def test_execution_unit_is_deterministic_and_input_content_bound():
    first = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "/one", "sha256": "6" * 64},
        transition_shard={"path": "/two", "sha256": "7" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64, "4" * 64],
    )
    reverse = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "/one", "sha256": "6" * 64},
        transition_shard={"path": "/two", "sha256": "7" * 64},
        sourcegraph_result_manifest_sha256s=["4" * 64, "3" * 64],
    )
    changed = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "/one", "sha256": "6" * 64},
        transition_shard={"path": "/two", "sha256": "8" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64, "4" * 64],
    )
    changed_repository = repository()
    changed_repository["bundle_sha256"] = "9" * 64
    changed_bundle = build_longitudinal_unit(
        changed_repository,
        introduction_shard={"path": "/one", "sha256": "6" * 64},
        transition_shard={"path": "/two", "sha256": "7" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64, "4" * 64],
    )

    assert first == reverse
    assert first["unit_id"] != changed["unit_id"]
    assert first["unit_id"] != changed_bundle["unit_id"]
    with pytest.raises(LongitudinalExtractionError, match="SHA-256"):
        build_longitudinal_unit(
            repository(),
            introduction_shard={"path": "/one", "sha256": "invalid"},
            transition_shard={"path": "/two", "sha256": "7" * 64},
            sourcegraph_result_manifest_sha256s=["3" * 64],
        )
    with pytest.raises(LongitudinalExtractionError, match="checksums"):
        build_longitudinal_unit(
            repository(),
            introduction_shard={"path": "/one", "sha256": "6" * 64},
            transition_shard={"path": "/two", "sha256": "7" * 64},
            sourcegraph_result_manifest_sha256s=["3" * 64, "3" * 64],
        )


def test_executor_fails_closed_on_invalid_external_payload(
    protocol: dict, tmp_path: Path
):
    unit = build_longitudinal_unit(
        repository(),
        introduction_shard={"path": "introductions", "sha256": "6" * 64},
        transition_shard={"path": "transitions", "sha256": "7" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64],
    )

    with pytest.raises(LongitudinalExtractionError, match="git extractor"):
        execute_longitudinal_unit(
            protocol,
            unit,
            tmp_path,
            sourcegraph_probe=lambda _unit: {},
            git_extractor=lambda _unit: None,
        )
    tampered_unit = copy.deepcopy(unit)
    tampered_unit["repository"]["cutoff_tree"] = "9" * 40
    with pytest.raises(LongitudinalExtractionError, match="identity"):
        execute_longitudinal_unit(
            protocol,
            tampered_unit,
            tmp_path,
            sourcegraph_probe=lambda _unit: {},
            git_extractor=lambda _unit: {},
        )
    invalid_protocol = copy.deepcopy(protocol)
    invalid_protocol["survival"]["horizons_days"] = [30]
    calls = []
    with pytest.raises(LongitudinalExtractionError, match="protocol_sha256"):
        execute_longitudinal_unit(
            invalid_protocol,
            unit,
            tmp_path,
            sourcegraph_probe=lambda _unit: calls.append("sourcegraph"),
            git_extractor=lambda _unit: calls.append("git"),
        )
    assert calls == []


def _git(repository_path: Path, *arguments: str, date: str | None = None) -> str:
    environment = dict(os.environ)
    if date is not None:
        environment["GIT_AUTHOR_DATE"] = date
        environment["GIT_COMMITTER_DATE"] = date
    result = subprocess.run(
        ["git", "-C", str(repository_path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _write_jsonl(path: Path, records: list[dict]) -> str:
    payload = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    ).encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_default_git_extractor_runs_end_to_end_on_pinned_repository(
    protocol: dict, tmp_path: Path
):
    git_repository = tmp_path / "repository"
    git_repository.mkdir()
    _git(git_repository, "init", "-q", "-b", "main")
    _git(git_repository, "config", "user.name", "Test")
    _git(git_repository, "config", "user.email", "test@example.com")
    source = git_repository / "main.py"
    source.write_text("base = True\n")
    _git(git_repository, "add", ".")
    _git(
        git_repository,
        "commit",
        "-q",
        "-m",
        "base",
        date="2025-01-01T00:00:00Z",
    )
    source.write_text("base = True\nagent = True\n")
    _git(git_repository, "add", ".")
    _git(
        git_repository,
        "commit",
        "-q",
        "-m",
        "agent",
        date="2025-01-11T00:00:00Z",
    )
    commit_oid = _git(git_repository, "rev-parse", "HEAD")
    cutoff_tree = _git(git_repository, "rev-parse", "HEAD^{tree}")
    record = {
        **introduction("main.py", 2),
        "merge_commit": commit_oid,
        "merged_at": "2025-01-11T00:00:00Z",
    }
    introduction_path = tmp_path / "introductions.jsonl"
    transition_path = tmp_path / "transitions.jsonl"
    introduction_sha = _write_jsonl(introduction_path, [record])
    transition_sha = _write_jsonl(transition_path, transitions_for(record))
    repository_record = {
        **repository(),
        "cutoff_commit": commit_oid,
        "cutoff_tree": cutoff_tree,
        "cache_path": str(git_repository),
    }
    unit = build_longitudinal_unit(
        repository_record,
        introduction_shard={
            "path": str(introduction_path),
            "sha256": introduction_sha,
        },
        transition_shard={
            "path": str(transition_path),
            "sha256": transition_sha,
        },
        sourcegraph_result_manifest_sha256s=["3" * 64],
    )
    observation = sourcegraph_observation(record)
    observation["indexed_revision_oid"] = commit_oid

    summary = execute_longitudinal_unit(
        protocol,
        unit,
        tmp_path / "output",
        sourcegraph_probe=lambda _unit: observation,
    )
    shard = json.loads(Path(summary["shard_path"]).read_text())

    assert summary["reused"] is False
    assert shard["hunks"][0]["code_age_days"] == 10.0
    assert shard["pinned_git"]["cutoff_tree"] == cutoff_tree


def test_default_git_extractor_rejects_tampered_input_shard(
    protocol: dict, tmp_path: Path
):
    path = tmp_path / "introductions.jsonl"
    path.write_text("{}\n")
    unit = build_longitudinal_unit(
        {**repository(), "cache_path": str(tmp_path)},
        introduction_shard={"path": str(path), "sha256": "f" * 64},
        transition_shard={"path": str(path), "sha256": "f" * 64},
        sourcegraph_result_manifest_sha256s=["3" * 64],
    )

    with pytest.raises(LongitudinalExtractionError, match="checksum"):
        execute_longitudinal_unit(
            protocol,
            unit,
            tmp_path / "output",
            sourcegraph_probe=lambda _unit: {},
        )


def test_file_age_history_queries_use_bounded_parallelism(monkeypatch, tmp_path: Path):
    records = [introduction(f"src/file_{index}.py", index + 1) for index in range(6)]
    active = 0
    peak_active = 0
    lock = threading.Lock()

    def fake_git(_repository: Path, *arguments: str) -> str:
        nonlocal active, peak_active
        assert arguments[:3] == ("log", "--follow", "--reverse")
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return "2025-01-01T00:00:00Z"

    monkeypatch.setattr("authorship.sourcegraph_longitudinal_execution.git", fake_git)

    ages = _file_age_days(tmp_path, records)

    assert len(ages) == len(records)
    assert peak_active > 1
    assert peak_active <= 8


def test_file_age_falls_back_for_path_created_by_merge_resolution(
    monkeypatch, tmp_path: Path
):
    record = introduction("src/merge_created.py", 1)
    calls = []

    def fake_git(_repository: Path, *arguments: str) -> str:
        calls.append(arguments)
        if arguments[:3] == ("log", "--follow", "--reverse"):
            return ""
        if arguments[:2] == ("cat-file", "-e"):
            return ""
        if arguments[:3] == ("log", "--full-history", "--reverse"):
            return "2025-01-09T00:00:00Z"
        raise AssertionError(arguments)

    monkeypatch.setattr("authorship.sourcegraph_longitudinal_execution.git", fake_git)

    key, age = _file_age_record(
        tmp_path,
        (record["merge_commit"], record["path"]),
        [record],
    )

    assert key == f"{record['merge_commit']}\0{record['path']}"
    assert age == 1.0
    assert calls[1] == (
        "cat-file",
        "-e",
        f"{record['merge_commit']}:{record['path']}",
    )
