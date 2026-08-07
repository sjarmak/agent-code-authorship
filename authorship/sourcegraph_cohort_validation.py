"""Validation and canonical hashing for frozen longitudinal cohorts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

EXPECTED_INPUTS = {
    "protocol",
    "adoption",
    "agent_commits",
    "ai_ban",
    "discovery",
    "targets",
    "survival",
}


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def cohort_freeze_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in document.items() if key != "cohort_freeze_sha256"
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _unit_identity(unit: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        unit.get(field)
        for field in ("repository_id", "commit_oid", "path", "hunk_sha256")
    )


def audit_materialized_unit_overlap(
    agent_units: list[Mapping[str, Any]],
    human_units: list[Mapping[str, Any]],
) -> list[str]:
    """Reject shared materialized identities or normalized introduced content."""
    errors = []
    units = [*agent_units, *human_units]
    if any(
        not all(_unit_identity(unit))
        or not isinstance(unit.get("content_sha256"), str)
        or len(unit["content_sha256"]) != 64
        for unit in units
    ):
        errors.append("materialized unit identity or content hash is incomplete")
        return errors
    agent_identities = {_unit_identity(unit) for unit in agent_units}
    human_identities = {_unit_identity(unit) for unit in human_units}
    if not agent_identities.isdisjoint(human_identities):
        errors.append("agent and human materialized units overlap")
    agent_content = {unit["content_sha256"] for unit in agent_units}
    human_content = {unit["content_sha256"] for unit in human_units}
    if not agent_content.isdisjoint(human_content):
        errors.append("agent and human materialized content overlaps")
    return errors


def _validate_language_gate(
    row: Mapping[str, Any], control: Mapping[str, Any]
) -> list[str]:
    errors = []
    repositories = row.get("repository_ids")
    if not isinstance(repositories, list) or len(repositories) != row.get(
        "adopter_count"
    ):
        errors.append("adopter count differs from repository identities")
        return errors
    required = row.get("minimum_adopter_repositories")
    identified = (
        isinstance(required, int)
        and len(repositories) >= required
        and control.get("status") == "identified"
    )
    expected = "identified" if identified else "not_identified"
    if row.get("status") != expected:
        errors.append("language identification gate is inconsistent")
    return errors


def _ids(row: Mapping[str, Any], key: str) -> set[str] | None:
    values = row.get(key)
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        return None
    return set(values)


def _validate_repository_roles(event_study: Mapping[str, Any]) -> list[str]:
    errors = []
    controls = event_study.get("control_pool", {}).get("languages", {})
    for language in ("Python", "Go"):
        control = controls.get(language, {})
        h2_ids = _ids(control, "h2_policy_repository_ids")
        never_ids = _ids(control, "never_observed_repository_ids")
        if h2_ids is None or never_ids is None:
            errors.append("static control repository identities are invalid")
            continue
        if not h2_ids.isdisjoint(never_ids):
            errors.append("static control repository roles overlap")
        if control.get("unique_control_count") != len(h2_ids | never_ids):
            errors.append("static control count differs from repository identities")
        for cohort_name in ("primary", "extended", "observed_tier_sensitivity"):
            treated = _ids(
                event_study.get(cohort_name, {}).get("languages", {}).get(language, {}),
                "repository_ids",
            )
            if treated is not None and not treated.isdisjoint(h2_ids | never_ids):
                errors.append("treated and static control repositories overlap")
    return errors


def _validate_event_study(event_study: Any) -> list[str]:
    if not isinstance(event_study, Mapping):
        return ["adoption event study is invalid"]
    errors = []
    controls = event_study.get("control_pool", {})
    if (
        controls.get("roles_are_disjoint") is not True
        or controls.get("h2_policy_rule")
        != "policy_effective_before_treated_pre_window"
    ):
        errors.append("control assignment contract is invalid")
    expected = {
        "primary": ([-180, 180], "confirmed"),
        "extended": ([-365, 365], "confirmed"),
        "observed_tier_sensitivity": ([-180, 180], "observed"),
    }
    for name, (window, tier) in expected.items():
        cohort = event_study.get(name, {})
        if cohort.get("window_days") != window or cohort.get("evidence_tier") != tier:
            errors.append(f"{name} window contract is invalid")
            continue
        for language in ("Python", "Go"):
            errors.extend(
                _validate_language_gate(
                    cohort.get("languages", {}).get(language, {}),
                    controls.get("languages", {}).get(language, {}),
                )
            )
    errors.extend(_validate_repository_roles(event_study))
    return errors


def _validate_external_roles(cohorts: Mapping[str, Any]) -> list[str]:
    external = cohorts.get("external_validation", {})
    positives = _ids(external, "agent_positive_repository_ids")
    negatives = _ids(external, "policy_negative_repository_ids")
    if positives is None or negatives is None:
        return ["external validation repository identities are invalid"]
    errors = []
    if external.get("agent_positive_repository_count") != len(
        positives
    ) or external.get("policy_negative_repository_count") != len(negatives):
        errors.append("external validation counts differ from repository identities")
    if not positives.isdisjoint(negatives):
        errors.append("external validation repository roles overlap")
    policy = cohorts.get("human_evidence", {}).get("H2_policy_human", {})
    repositories = policy.get("repositories")
    if not isinstance(repositories, list):
        errors.append("H2 policy repositories are invalid")
    else:
        policy_ids = {row.get("repository_id") for row in repositories}
        if policy.get("repository_count") != len(policy_ids):
            errors.append("H2 policy count differs from repository identities")
        if not positives.isdisjoint(policy_ids):
            errors.append("H2 policy and agent-positive repositories overlap")
    return errors


def _validate_unit_overlap_gate(document: Mapping[str, Any]) -> list[str]:
    expected = {
        "status": "pending_unit_materialization",
        "analysis_allowed": False,
        "unit_identity_fields": [
            "repository_id",
            "commit_oid",
            "path",
            "hunk_sha256",
        ],
        "content_identity": "sha256_normalized_introduced_content",
        "required_result": "zero_agent_human_unit_or_content_intersections",
        "enforcement_stage": "sourcegraph_longitudinal_panel_materialization",
        "failure_behavior": "block_analysis",
    }
    return (
        []
        if document.get("unit_overlap_gate") == expected
        else ["unit overlap gate is invalid"]
    )


def _validate_freeze_contract(document: Mapping[str, Any]) -> list[str]:
    errors = []
    if document.get("outcomes_consulted") is not False:
        errors.append("outcomes_consulted must remain false")
    simulation = document.get("prospective_precision_simulation", {})
    if (
        simulation.get("outcomes_consulted") is not False
        or simulation.get("selected_minimum_repositories") is None
        or simulation.get("test_statistic") != "paired_student_t_two_sided"
    ):
        errors.append("prospective precision gate is invalid")
    selection = document.get("selection", {})
    if (
        selection.get("primary_event_tier") != "confirmed"
        or selection.get("exact_content_overlap_rule")
        != "block_until_zero_shared_content_hashes"
    ):
        errors.append("selection contract is invalid")
    expected_invariants = {
        "selection_outcome_blind": True,
        "primary_uses_confirmed_sequential_events_only": True,
        "control_roles_disjoint": True,
        "external_validation_repository_roles_disjoint": True,
        "exact_content_overlap_gate": "pending_unit_materialization",
    }
    if document.get("invariants") != expected_invariants:
        errors.append("cohort invariants failed")
    errors.extend(_validate_unit_overlap_gate(document))
    return errors


def _validate_input_files(files: Any) -> list[str]:
    if not isinstance(files, list) or len(files) != len(EXPECTED_INPUTS):
        return ["input file pins must contain seven unique inputs"]
    identifiers = [row.get("input_id") for row in files if isinstance(row, Mapping)]
    if len(identifiers) != len(files) or set(identifiers) != EXPECTED_INPUTS:
        return ["input file pins must contain seven unique inputs"]
    if len(set(identifiers)) != len(identifiers):
        return ["input file pins must contain seven unique inputs"]
    return []


def validate_cohort_freeze(document: Mapping[str, Any]) -> list[str]:
    errors = []
    if document.get("cohort_freeze_sha256") != cohort_freeze_sha256(document):
        errors.append("checksum mismatch")
    errors.extend(_validate_freeze_contract(document))
    cohorts = document.get("cohorts", {})
    errors.extend(_validate_event_study(cohorts.get("adoption_event_study")))
    errors.extend(_validate_external_roles(cohorts))
    errors.extend(_validate_input_files(document.get("input_files")))
    return errors
