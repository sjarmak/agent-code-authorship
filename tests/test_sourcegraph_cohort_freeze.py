import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from authorship.sourcegraph_cohort_freeze import (
    CohortFreezeError,
    build_cohort_freeze,
    simulate_paired_repository_power,
)
from authorship.sourcegraph_cohort_io import materialize_cohort_freeze
from authorship.sourcegraph_cohort_precision import CohortPrecisionError
from authorship.sourcegraph_cohort_validation import (
    audit_materialized_unit_overlap,
    cohort_freeze_sha256,
    validate_cohort_freeze,
)

CUTOFF = datetime(2026, 7, 24, 23, 59, 59, tzinfo=timezone.utc)


def _iso(days_before_cutoff: int) -> str:
    return (
        (CUTOFF - timedelta(days=days_before_cutoff)).isoformat().replace("+00:00", "Z")
    )


def _protocol() -> dict:
    return {
        "protocol_sha256": "1" * 64,
        "snapshot": {"cutoff": _iso(0)},
        "languages": ["Python", "Go"],
        "adoption": {
            "primary_event_tier": "confirmed",
            "clean_prehistory_days": 365,
            "event_windows_days": {
                "primary": [-180, 180],
                "extended": [-365, 365],
            },
        },
        "identification_gates": {
            "minimum_adopters_per_language": 20,
            "minimum_controls_per_language": 10,
            "minimum_agent_family_repositories": 8,
            "role_and_exact_content_overlap_forbidden": True,
            "failed_gate_result": "not_identified",
        },
    }


def _discovery() -> dict:
    return {
        "specification_sha256": "7" * 64,
        "outcomes_consulted": False,
        "query_families": [
            {
                "id": query_id,
                "query_template": 'repo:{repo} after:"2023-01-01" before:"2026-07-25"',
            }
            for query_id in (
                "agent_trailer_commits",
                "recognized_agent_identity_commits",
                "generated_code_disclosure_diffs",
            )
        ],
    }


def _adoption_row(
    repository: str,
    language: str,
    *,
    days_before_cutoff: int = 365,
    status: str = "dated_sequential",
    tier: str | None = "confirmed",
) -> dict:
    dated = status.startswith("dated")
    return {
        "repository_id": repository,
        "language": language,
        "status": status,
        "primary_scope_eligible": True,
        "clean_prehistory": True,
        "unresolved_earlier_candidate_count": 0,
        "evidence_tier": tier if dated else None,
        "adoption_observed_at": _iso(days_before_cutoff) if dated else None,
        "adoption_commit_oid": (
            repository.encode().hex()[:40].ljust(40, "0") if dated else None
        ),
    }


def _adoption_catalog() -> dict:
    rows = []
    for language in ("Python", "Go"):
        prefix = language.lower()
        rows.extend(
            _adoption_row(f"{prefix}/adopter-{index}", language) for index in range(20)
        )
        rows.extend(
            _adoption_row(
                f"{prefix}/control-{index}",
                language,
                status="no_credible_event",
                tier=None,
            )
            for index in range(10)
        )
    rows.extend(
        [
            _adoption_row(
                "python/incomplete-followup",
                "Python",
                days_before_cutoff=179,
            ),
            _adoption_row(
                "go/anchor",
                "Go",
                status="dated_explicit_anchor",
            ),
            _adoption_row(
                "go/observed",
                "Go",
                tier="observed",
            ),
        ]
    )
    return {
        "catalog_sha256": "2" * 64,
        "outcomes_consulted": False,
        "repositories": rows,
    }


def _ai_ban_catalog() -> dict:
    return {
        "catalog_sha256": "3" * 64,
        "outcomes_consulted": False,
        "repositories": [
            {
                "canonical_repository_id": "python/policy-control",
                "language": "Python",
                "status": "dated_policy",
                "scope_eligible": True,
                "accepted_policy": {
                    "commit_oid": "a" * 40,
                    "observed_at": _iso(500),
                    "evidence_tier": "datable_default_branch_policy",
                    "policy_scope": "repository_wide_code_contributions",
                },
            },
            {
                "canonical_repository_id": "go/adopter-0",
                "language": "Go",
                "status": "dated_policy",
                "scope_eligible": True,
                "accepted_policy": {
                    "commit_oid": "b" * 40,
                    "observed_at": _iso(500),
                    "evidence_tier": "datable_default_branch_policy",
                    "policy_scope": "repository_wide_code_contributions",
                },
            },
        ],
    }


def _agent_catalog() -> dict:
    return {
        "catalog_sha256": "4" * 64,
        "outcomes_consulted": False,
        "commits": [
            {
                "repository_id": "python/agent-positive",
                "commit_oid": "c" * 40,
                "language": "Python",
            }
        ],
    }


def _survival() -> dict:
    candidates = []
    for family, count in (("Copilot", 8), ("Cursor", 7)):
        candidates.extend(
            {
                "repository_id": f"survival/{family}-{index}",
                "language": "Python",
                "agent_family": family,
            }
            for index in range(count)
        )
    return {"frame_sha256": "5" * 64, "candidates": candidates}


def _targets() -> dict:
    return {
        "manifest_sha256": "6" * 64,
        "repositories": [{"id": f"target/repo-{index}"} for index in range(3)],
    }


def _build() -> dict:
    return build_cohort_freeze(
        _protocol(),
        _discovery(),
        _adoption_catalog(),
        _agent_catalog(),
        _ai_ban_catalog(),
        _targets(),
        _survival(),
        discovery_start="2023-01-01T00:00:00Z",
        simulation_replicates=2_000,
        simulation_seed=20260729,
        input_files=[
            {"input_id": input_id, "file_sha256": str(index) * 64}
            for index, input_id in enumerate(
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
        ],
    )


def test_freezes_primary_and_extended_windows_without_relaxing_gates():
    artifact = _build()
    primary = artifact["cohorts"]["adoption_event_study"]["primary"]["languages"]
    extended = artifact["cohorts"]["adoption_event_study"]["extended"]["languages"]

    assert primary["Python"]["status"] == "identified"
    assert primary["Python"]["adopter_count"] == 20
    assert primary["Go"]["status"] == "identified"
    assert "python/incomplete-followup" not in primary["Python"]["repository_ids"]
    assert "go/anchor" not in primary["Go"]["repository_ids"]
    assert extended["Python"]["status"] == "identified"
    assert extended["Go"]["status"] == "identified"


def test_control_roles_are_disjoint_and_policy_adopter_is_excluded():
    controls = _build()["cohorts"]["adoption_event_study"]["control_pool"]
    python = controls["languages"]["Python"]
    go = controls["languages"]["Go"]

    assert python["unique_control_count"] == 11
    assert python["h2_policy_repository_ids"] == ["python/policy-control"]
    assert "python/policy-control" not in python["never_observed_repository_ids"]
    assert "go/adopter-0" not in go["h2_policy_repository_ids"]
    assert go["unique_control_count"] == 10


def test_agent_family_gate_uses_unique_repositories_and_fails_closed():
    gates = {row["agent_family"]: row for row in _build()["agent_family_gates"]}

    assert gates["Copilot"]["repository_count"] == 8
    assert gates["Copilot"]["status"] == "identified"
    assert gates["Cursor"]["repository_count"] == 7
    assert gates["Cursor"]["status"] == "not_identified"


def test_simulation_is_deterministic_and_sets_target_before_outcomes():
    first = simulate_paired_repository_power(
        minimum_repositories=20,
        maximum_repositories=22,
        standardized_effect=0.7,
        within_pair_correlation=0.5,
        target_power=0.8,
        replicates=5_000,
        seed=11,
    )
    second = simulate_paired_repository_power(
        minimum_repositories=20,
        maximum_repositories=22,
        standardized_effect=0.7,
        within_pair_correlation=0.5,
        target_power=0.8,
        replicates=5_000,
        seed=11,
    )

    assert first == second
    assert first["selected_minimum_repositories"] == 20
    assert first["test_statistic"] == "paired_student_t_two_sided"
    assert first["power_by_repository_count"][0]["critical_value"] == pytest.approx(
        2.093024, abs=2e-5
    )
    assert first["power_by_repository_count"][0]["power"] >= 0.8
    with pytest.raises(CohortPrecisionError, match="at least two"):
        simulate_paired_repository_power(
            minimum_repositories=1,
            maximum_repositories=2,
            standardized_effect=0.7,
            within_pair_correlation=0.5,
            target_power=0.8,
            replicates=10,
            seed=11,
        )


def test_checksum_validation_rejects_tampering():
    artifact = _build()
    assert artifact["cohort_freeze_sha256"] == cohort_freeze_sha256(artifact)
    assert validate_cohort_freeze(artifact) == []

    changed = copy.deepcopy(artifact)
    changed["outcomes_consulted"] = True
    assert "checksum mismatch" in validate_cohort_freeze(changed)


def test_rechecksummed_contract_forgery_is_rejected():
    changed = copy.deepcopy(_build())
    changed["cohorts"]["adoption_event_study"]["primary"]["window_days"] = [-90, 90]
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "primary window contract is invalid" in validate_cohort_freeze(changed)


def test_rechecksummed_treated_control_overlap_is_rejected():
    changed = copy.deepcopy(_build())
    primary = changed["cohorts"]["adoption_event_study"]["primary"]["languages"][
        "Python"
    ]
    primary["repository_ids"].append("python/policy-control")
    primary["adopter_count"] += 1
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "treated and static control repositories overlap" in validate_cohort_freeze(
        changed
    )


def test_rechecksummed_external_role_overlap_is_rejected():
    changed = copy.deepcopy(_build())
    validation = changed["cohorts"]["external_validation"]
    validation["policy_negative_repository_ids"].append("python/agent-positive")
    validation["policy_negative_repository_count"] += 1
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "external validation repository roles overlap" in validate_cohort_freeze(
        changed
    )


def test_rechecksummed_static_control_role_overlap_is_rejected():
    changed = copy.deepcopy(_build())
    control = changed["cohorts"]["adoption_event_study"]["control_pool"]["languages"][
        "Python"
    ]
    control["never_observed_repository_ids"].append("python/policy-control")
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "static control repository roles overlap" in validate_cohort_freeze(changed)


def test_rechecksummed_h2_agent_positive_overlap_is_rejected():
    changed = copy.deepcopy(_build())
    policy = changed["cohorts"]["human_evidence"]["H2_policy_human"]
    policy["repositories"][0]["repository_id"] = "python/agent-positive"
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "H2 policy and agent-positive repositories overlap" in (
        validate_cohort_freeze(changed)
    )


def test_unit_overlap_gate_is_pending_and_materialized_units_are_auditable():
    gate = _build()["unit_overlap_gate"]

    assert gate["status"] == "pending_unit_materialization"
    assert gate["analysis_allowed"] is False
    assert audit_materialized_unit_overlap(
        [
            {
                "repository_id": "repo/a",
                "commit_oid": "a" * 40,
                "path": "src/a.py",
                "hunk_sha256": "1" * 64,
                "content_sha256": "2" * 64,
            }
        ],
        [
            {
                "repository_id": "repo/b",
                "commit_oid": "b" * 40,
                "path": "src/b.py",
                "hunk_sha256": "3" * 64,
                "content_sha256": "2" * 64,
            }
        ],
    ) == ["agent and human materialized content overlaps"]
    shared_identity = {
        "repository_id": "repo/a",
        "commit_oid": "a" * 40,
        "path": "src/a.py",
        "hunk_sha256": "1" * 64,
        "content_sha256": "2" * 64,
    }
    assert audit_materialized_unit_overlap([shared_identity], [shared_identity]) == [
        "agent and human materialized units overlap",
        "agent and human materialized content overlaps",
    ]
    assert audit_materialized_unit_overlap([shared_identity], []) == []
    assert audit_materialized_unit_overlap(
        [{**shared_identity, "content_sha256": ""}], []
    ) == ["materialized unit identity or content hash is incomplete"]


def test_rechecksummed_pending_unit_gate_cannot_be_bypassed():
    changed = copy.deepcopy(_build())
    changed["unit_overlap_gate"]["analysis_allowed"] = True
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "unit overlap gate is invalid" in validate_cohort_freeze(changed)


@pytest.mark.parametrize(
    ("input_name", "collection"),
    [
        ("adoption", "repositories"),
        ("agent_commits", "commits"),
        ("ai_ban", "repositories"),
        ("targets", "repositories"),
        ("survival", "candidates"),
    ],
)
def test_duplicate_evidence_identities_are_rejected(input_name: str, collection: str):
    inputs = {
        "adoption": _adoption_catalog(),
        "agent_commits": _agent_catalog(),
        "ai_ban": _ai_ban_catalog(),
        "targets": _targets(),
        "survival": _survival(),
    }
    duplicate = copy.deepcopy(inputs[input_name])
    duplicate[collection].append(copy.deepcopy(duplicate[collection][0]))
    inputs[input_name] = duplicate

    with pytest.raises(CohortFreezeError, match="duplicate"):
        build_cohort_freeze(
            _protocol(),
            _discovery(),
            inputs["adoption"],
            inputs["agent_commits"],
            inputs["ai_ban"],
            inputs["targets"],
            inputs["survival"],
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=100,
            simulation_seed=3,
        )


def test_rechecksummed_incomplete_pins_are_rejected():
    changed = copy.deepcopy(_build())
    changed["input_files"] = []
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "input file pins must contain seven unique inputs" in validate_cohort_freeze(
        changed
    )

    duplicate = copy.deepcopy(_build())
    duplicate["input_files"][-1] = copy.deepcopy(duplicate["input_files"][0])
    duplicate["cohort_freeze_sha256"] = cohort_freeze_sha256(duplicate)
    assert "input file pins must contain seven unique inputs" in (
        validate_cohort_freeze(duplicate)
    )


def test_rechecksummed_precision_contract_is_rejected():
    changed = copy.deepcopy(_build())
    changed["prospective_precision_simulation"]["test_statistic"] = "normal_z"
    changed["cohort_freeze_sha256"] = cohort_freeze_sha256(changed)

    assert "prospective precision gate is invalid" in validate_cohort_freeze(changed)


def test_365_day_clean_prehistory_is_enforced():
    adoption = _adoption_catalog()
    record = next(
        row
        for row in adoption["repositories"]
        if row["repository_id"] == "python/adopter-0"
    )
    record["adoption_observed_at"] = "2023-12-31T00:00:00Z"

    artifact = build_cohort_freeze(
        _protocol(),
        _discovery(),
        adoption,
        _agent_catalog(),
        _ai_ban_catalog(),
        _targets(),
        _survival(),
        discovery_start="2023-01-01T00:00:00Z",
        simulation_replicates=500,
        simulation_seed=3,
    )
    python = artifact["cohorts"]["adoption_event_study"]["primary"]["languages"][
        "Python"
    ]
    assert python["adopter_count"] == 19
    assert python["status"] == "not_identified"


def test_materialization_requires_exact_independent_pins(tmp_path: Path, monkeypatch):
    documents = {
        "protocol": _protocol(),
        "discovery": _discovery(),
        "adoption": _adoption_catalog(),
        "agent_commits": _agent_catalog(),
        "ai_ban": _ai_ban_catalog(),
        "targets": _targets(),
        "survival": _survival(),
    }
    paths = {}
    pins = {}
    for input_id, document in documents.items():
        path = tmp_path / f"{input_id}.json"
        payload = json.dumps(document, sort_keys=True).encode()
        path.write_bytes(payload)
        paths[input_id] = path
        pins[input_id] = hashlib.sha256(payload).hexdigest()

    output = tmp_path / "cohorts.json"
    artifact = materialize_cohort_freeze(
        paths,
        pins,
        output_path=output,
        discovery_start="2023-01-01T00:00:00Z",
        simulation_replicates=500,
        simulation_seed=7,
    )
    assert json.loads(output.read_text()) == artifact

    bad_pins = {**pins, "adoption": "0" * 64}
    with pytest.raises(CohortFreezeError, match="adoption differs"):
        materialize_cohort_freeze(
            paths,
            bad_pins,
            output_path=output,
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=500,
            simulation_seed=7,
        )

    def reject_schema(_artifact):
        raise CohortFreezeError("schema validation failed")

    monkeypatch.setattr(
        "authorship.sourcegraph_cohort_io._validate_against_schema",
        reject_schema,
    )
    with pytest.raises(CohortFreezeError, match="schema validation failed"):
        materialize_cohort_freeze(
            paths,
            pins,
            output_path=output,
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=500,
            simulation_seed=7,
        )


def test_materialization_fails_closed_for_bad_roots_and_output(tmp_path: Path):
    documents = {
        "protocol": _protocol(),
        "discovery": _discovery(),
        "adoption": _adoption_catalog(),
        "agent_commits": _agent_catalog(),
        "ai_ban": _ai_ban_catalog(),
        "targets": _targets(),
        "survival": _survival(),
    }
    paths = {}
    pins = {}
    for input_id, document in documents.items():
        path = tmp_path / f"{input_id}.json"
        payload = json.dumps(document, sort_keys=True).encode()
        path.write_bytes(payload)
        paths[input_id] = path
        pins[input_id] = hashlib.sha256(payload).hexdigest()

    missing = {key: value for key, value in paths.items() if key != "discovery"}
    with pytest.raises(CohortFreezeError, match="exactly cover"):
        materialize_cohort_freeze(
            missing,
            pins,
            output_path=tmp_path / "unused.json",
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=10,
            simulation_seed=1,
        )

    malformed = b"[]"
    paths["discovery"].write_bytes(malformed)
    malformed_pins = {
        **pins,
        "discovery": hashlib.sha256(malformed).hexdigest(),
    }
    with pytest.raises(CohortFreezeError, match="root must be"):
        materialize_cohort_freeze(
            paths,
            malformed_pins,
            output_path=tmp_path / "unused.json",
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=10,
            simulation_seed=1,
        )

    valid = json.dumps(_discovery(), sort_keys=True).encode()
    paths["discovery"].write_bytes(valid)
    restored_pins = {**pins, "discovery": hashlib.sha256(valid).hexdigest()}
    with pytest.raises(CohortFreezeError, match="cannot write cohort freeze"):
        materialize_cohort_freeze(
            paths,
            restored_pins,
            output_path=tmp_path / "missing" / "freeze.json",
            discovery_start="2023-01-01T00:00:00Z",
            simulation_replicates=10,
            simulation_seed=1,
        )


def test_generated_artifact_satisfies_schema():
    artifact = _build()
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "study/sourcegraph-longitudinal-cohort-freeze.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(artifact)
    changed = copy.deepcopy(artifact)
    changed["cohorts"]["human_evidence"]["H3_pre_adoption_proxy"][
        "contemporary_pre_adoption"
    ]["unknown"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(changed)


def test_frozen_real_frame_is_exact_and_outcome_blind():
    root = Path(__file__).resolve().parents[1]
    artifact = json.loads(
        (root / "study/sourcegraph-longitudinal-cohort-freeze.v3.json").read_text()
    )
    schema = json.loads(
        (root / "study/sourcegraph-longitudinal-cohort-freeze.schema.json").read_text()
    )
    primary = artifact["cohorts"]["adoption_event_study"]["primary"]["languages"]
    extended = artifact["cohorts"]["adoption_event_study"]["extended"]["languages"]

    assert artifact["cohort_freeze_sha256"] == (
        "945a012805e159f386f32d7e10901dd6b5279040838fa1dacf850e21b676b12f"
    )
    assert primary["Python"]["adopter_count"] == 25
    assert primary["Go"]["adopter_count"] == 21
    assert primary["Python"]["status"] == primary["Go"]["status"] == "identified"
    assert extended["Python"]["status"] == "identified"
    assert extended["Go"]["status"] == "not_identified"
    assert (
        artifact["prospective_precision_simulation"]["selected_minimum_repositories"]
        == 20
    )
    assert validate_cohort_freeze(artifact) == []
    jsonschema.Draft202012Validator(schema).validate(artifact)
