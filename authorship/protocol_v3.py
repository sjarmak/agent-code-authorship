"""Load and validate the Sourcegraph-first protocol-v3 preregistration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ProtocolV3Error(ValueError):
    """Raised when protocol v3 weakens a locked study-design decision."""


PARENT_ARTIFACTS = {
    "protocol_v1": Path("study/protocol.v1.json"),
    "amendment_v2": Path("study/protocol-amendment.v2.json"),
    "replication_v2": Path("results/replication.v2.json"),
    "prevalence_targets": Path("study/targets.v1.json"),
    "survival_protocol": Path("study/survival-protocol.v1.json"),
    "survival_candidates": Path("study/survival-candidates.v1.json"),
    "survival_inputs": Path("study/survival-inputs.v1.json"),
    "reference_manifest_v1": Path("study/repositories.v1.json"),
    "control_evidence": Path("data/control_evidence.json"),
    "cohort_forks": Path("data/cohort_forks.tsv"),
}


def _get(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def protocol_sha256(document: dict[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "protocol_sha256"
    }
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


_EXACT_RULES: dict[str, tuple[Any, str]] = {
    "protocol_version": (3, "protocol_version must equal 3"),
    "status": ("preregistered", "protocol status must be preregistered"),
    "parent.amendment_version": (
        2,
        "protocol v3 must remain additive to amendment v2",
    ),
    "parent.result_status": (
        "not_identified",
        "parent result status must remain not_identified",
    ),
    "parent.additive": (True, "protocol v3 must remain additive"),
    "parent.legacy_results_known": (
        True,
        "known legacy results must remain disclosed",
    ),
    "snapshot.cutoff": (
        "2026-07-24T23:59:59Z",
        "snapshot cutoff must remain 2026-07-24T23:59:59Z",
    ),
    "languages": (
        ["Python", "Go"],
        "primary languages must equal ['Python', 'Go']",
    ),
    "populations.prevalence.repository_count": (
        150,
        "prevalence population must remain 150 repositories",
    ),
    "populations.prevalence.frame": (
        "study/targets.v1.json",
        "prevalence frame must remain study/targets.v1.json",
    ),
    "populations.survival.repository_count": (
        126,
        "survival population must remain 126 repositories",
    ),
    "populations.survival.frame": (
        "study/survival-candidates.v1.json",
        "survival frame must remain study/survival-candidates.v1.json",
    ),
    "populations.adoption.freeze_required_before_outcome_extraction": (
        True,
        "adoption population must freeze before outcome extraction",
    ),
    "selection.outcome_blind": (
        True,
        "v3 candidate selection must remain outcome blind",
    ),
    "selection.legacy_results_known": (
        True,
        "selection must disclose known legacy results",
    ),
    "selection.outcomes_consulted_for_v3_candidate_selection": (
        False,
        "v3 candidate selection must not consult outcomes",
    ),
    "selection.new_v3_outcomes_extracted": (
        False,
        "new v3 outcomes must remain unextracted at preregistration",
    ),
    "selection.full_frame_indexed_before_eligibility_screening": (
        True,
        "the full frame must be indexed before eligibility screening",
    ),
    "selection.query_text_frozen_before_execution": (
        True,
        "Sourcegraph query text must freeze before execution",
    ),
    "sourcegraph.indexed_namespace": (
        "github.com/sg-evals/",
        "Sourcegraph namespace must be github.com/sg-evals/",
    ),
    "sourcegraph.full_frozen_frame_required": (
        True,
        "Sourcegraph must index the complete frozen frame",
    ),
    "sourcegraph.precise_code_intelligence_required": (
        False,
        "SCIP must not be required by protocol v3",
    ),
    "sourcegraph.canonical_identity": (
        "original_repository_owner_and_name",
        "canonical repository identity must remain the original owner and name",
    ),
    "sourcegraph.mirror_identity_role": (
        "transport_only",
        "sg-evals mirror identity must remain transport only",
    ),
    "sourcegraph.authoritative_lineage_source": (
        "pinned_git_history",
        "pinned Git history must remain authoritative for lineage",
    ),
    "human_evidence.tiers.H1.label": (
        "attested_human",
        "H1 must remain attested_human",
    ),
    "human_evidence.tiers.H1.contamination_range": (
        [0.0, 0.0],
        "H1 contamination range must equal [0.0, 0.0]",
    ),
    "human_evidence.tiers.H2.label": (
        "policy_human",
        "H2 must remain policy_human",
    ),
    "human_evidence.tiers.H2.contamination_range": (
        [0.0, 0.2],
        "H2 contamination range must equal [0.0, 0.2]",
    ),
    "human_evidence.tiers.H3.label": (
        "pre_observed_adoption_human_proxy",
        "H3 must remain pre_observed_adoption_human_proxy",
    ),
    "human_evidence.tiers.H3.era_adjustment_required": (
        True,
        "H3 evidence requires era adjustment",
    ),
    "human_evidence.tiers.H3.contamination_range": (
        [0.0, 0.25],
        "H3 contamination range must equal [0.0, 0.25]",
    ),
    "human_evidence.pool_as_certain": (
        False,
        "H1-H3 evidence must not be pooled as certain",
    ),
    "human_evidence.combined_estimate": (
        "partial_identification_envelope",
        "combined human evidence must use a partial-identification envelope",
    ),
    "adoption.event_name": (
        "first_observed_adoption",
        "adoption event must be first_observed_adoption",
    ),
    "adoption.primary_event_tier": (
        "confirmed",
        "confirmed adoption must remain the primary event tier",
    ),
    "adoption.unit": (
        "code_introducing_commit_hunk",
        "adoption unit must remain code_introducing_commit_hunk",
    ),
    "adoption.clean_prehistory_days": (
        365,
        "adoption events require 365 clean prehistory days",
    ),
    "adoption.all_post_adoption_code_is_agent": (
        False,
        "post-adoption code must not be labeled wholesale as agent",
    ),
    "adoption.event_windows_days.primary": (
        [-180, 180],
        "primary event window must equal [-180, 180]",
    ),
    "adoption.event_windows_days.extended": (
        [-365, 365],
        "extended event window must equal [-365, 365]",
    ),
    "adoption.survival_horizons_days": (
        [30, 90, 180, 365],
        "adoption survival horizons must equal [30, 90, 180, 365]",
    ),
    "era_adjustment.method": (
        "within_repository_difference_in_differences",
        "era adjustment must use within_repository_difference_in_differences",
    ),
    "era_adjustment.repository_cross_fitting": (
        True,
        "era adjustment must remain repository cross-fitted",
    ),
    "era_adjustment.failed_diagnostic_result": (
        "not_identified",
        "failed era diagnostics must report not_identified",
    ),
    "authorship.primary_model": (
        "l2_logistic_regression",
        "primary classifier must remain l2_logistic_regression",
    ),
    "authorship.primary_features": (
        "study/features.v1.json",
        "primary classifier features must remain frozen",
    ),
    "authorship.primary_weighting": (
        "line",
        "primary authorship weighting must remain line",
    ),
    "authorship.secondary_weighting": (
        "repository",
        "secondary authorship weighting must remain repository",
    ),
    "authorship.cross_validation_unit": (
        "repository",
        "authorship cross-validation unit must remain repository",
    ),
    "authorship.bootstrap_unit": (
        "repository",
        "authorship bootstrap unit must remain repository",
    ),
    "authorship.partial_identification.enabled": (
        True,
        "partial identification must remain enabled",
    ),
    "authorship.partial_identification.method": (
        "union_over_evidence_tiers_and_contamination_grid",
        "partial-identification method must remain the prespecified union",
    ),
    "authorship.partial_identification.contamination_grid_step": (
        0.05,
        "contamination grid step must equal 0.05",
    ),
    "authorship.partial_identification.maximum_headline_width": (
        0.3,
        "maximum headline width must equal 0.3",
    ),
    "survival.primary_estimator": (
        "repository_balanced_kaplan_meier",
        "primary survival estimator must be repository_balanced_kaplan_meier",
    ),
    "survival.secondary_estimator": (
        "aalen_johansen_state_occupancy",
        "secondary survival estimator must be aalen_johansen_state_occupancy",
    ),
    "survival.lineage_loss": (
        "right_censor_at_last_observation",
        "lineage loss must censor at last observation",
    ),
    "survival.horizons_days": (
        [30, 90, 180, 365],
        "survival horizons must equal [30, 90, 180, 365]",
    ),
    "survival.states": (
        ["unchanged", "modified", "deleted"],
        "survival states must equal unchanged, modified, and deleted",
    ),
    "survival.primary_weighting": (
        "repository",
        "primary survival weighting must be repository",
    ),
    "survival.secondary_weighting": (
        "line",
        "secondary survival weighting must remain line",
    ),
    "survival.bootstrap_unit": (
        "repository",
        "survival bootstrap unit must remain repository",
    ),
    "reporting.sourcegraph_assets_are_primary_project_deliverables": (
        True,
        "Sourcegraph assets must remain primary project deliverables",
    ),
}

_REQUIRED_SETS = {
    "human_evidence.tiers.H1.required_evidence": {
        "attestor_identity_and_authority",
        "exact_repository",
        "exact_commit_ids",
        "no_ai_or_llm_assistance",
        "scope_includes_code_and_tests",
    },
    "human_evidence.tiers.H2.required_evidence": {
        "repository_local_policy",
        "policy_effective_commit_and_date",
        "scope_includes_code_and_tests",
        "applies_to_sampled_contributors",
        "no_disclosure_exception",
        "enforcement_or_review_mechanism",
    },
    "sourcegraph.required_freezes": {
        "canonical_to_sg_evals_mapping",
        "indexed_commit_and_timestamp",
        "saved_query_text",
        "query_result_manifest",
        "adoption_event_catalog",
        "ai_ban_policy_catalog",
        "evidence_packets",
    },
    "era_adjustment.required_diagnostics": {
        "parallel_pre_trends",
        "placebo_adoption_dates",
        "repository_held_out_era_auc",
        "featurewise_standardized_mean_shift",
        "historical_anchor_sensitivity",
    },
    "survival.required_invariants": {
        "survival_is_monotone_nonincreasing",
        "risk_set_is_explicit_at_every_horizon",
        "right_censored_repositories_are_not_complete_case_dropped",
    },
}


def _validate_exact(document: dict[str, Any]) -> list[str]:
    return [
        message
        for path, (expected, message) in _EXACT_RULES.items()
        if _get(document, path) != expected
    ]


def _validate_sets(document: dict[str, Any]) -> list[str]:
    errors = []
    h3_subtypes = _get(document, "human_evidence.tiers.H3.subtypes")
    if set(h3_subtypes or []) != {
        "historical_pre_2023",
        "contemporary_pre_adoption",
    }:
        errors.append(
            "H3 must include historical_pre_2023 and contemporary_pre_adoption"
        )
    estimands = _get(document, "adoption.estimands")
    if set(estimands or []) != {
        "exact_agent_hunk_authorship_contrast",
        "whole_repository_adoption_effect",
    }:
        errors.append("both adoption estimands are required")
    capabilities = set(_get(document, "sourcegraph.allowed_capabilities") or [])
    required = {
        "indexed_search",
        "revision_search",
        "commit_search",
        "diff_search",
        "blame",
        "structural_search",
        "repository_metadata",
    }
    if not required.issubset(capabilities):
        errors.append("Sourcegraph capability set is incomplete")
    for path, expected in _REQUIRED_SETS.items():
        if not expected.issubset(set(_get(document, path) or [])):
            errors.append(f"{path} is incomplete")
    return errors


def _validate_lower_bounds(document: dict[str, Any]) -> list[str]:
    rules = {
        "human_evidence.tiers.H3.clean_prehistory_days": (
            365,
            "H3 requires at least 365 clean prehistory days",
        ),
        "identification_gates.minimum_adopters_per_language": (
            20,
            "minimum adopters per language must be >= 20",
        ),
        "identification_gates.minimum_controls_per_language": (
            10,
            "minimum controls per language must be >= 10",
        ),
        "identification_gates.minimum_agent_family_repositories": (
            8,
            "minimum agent-family repositories must be >= 8",
        ),
    }
    errors = []
    for path, (minimum, message) in rules.items():
        value = _get(document, path)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            errors.append(message)
    return errors


def _validate_parent_hashes(document: dict[str, Any]) -> list[str]:
    hashes = _get(document, "parent.sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(PARENT_ARTIFACTS):
        return ["parent hashes must pin every frozen v1/v2 input"]
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes.values()
    ):
        return ["parent hashes must be lowercase SHA-256 values"]
    return []


def validate_protocol_v3(document: dict[str, Any]) -> list[str]:
    required = {
        "protocol_version",
        "status",
        "purpose",
        "parent",
        "snapshot",
        "languages",
        "populations",
        "selection",
        "sourcegraph",
        "human_evidence",
        "adoption",
        "era_adjustment",
        "authorship",
        "survival",
        "identification_gates",
        "reporting",
        "protocol_sha256",
    }
    errors = [
        f"missing required section: {section}"
        for section in sorted(required - document.keys())
    ]
    errors.extend(_validate_exact(document))
    errors.extend(_validate_sets(document))
    errors.extend(_validate_lower_bounds(document))
    errors.extend(_validate_parent_hashes(document))
    if document.get("protocol_sha256") != protocol_sha256(document):
        errors.append("protocol_sha256 does not match canonical content")
    return errors


def validate_parent_artifacts(
    document: dict[str, Any], repository_root: Path
) -> list[str]:
    expected = _get(document, "parent.sha256")
    if not isinstance(expected, dict):
        return ["parent hashes are unavailable"]
    errors = []
    for name, relative_path in PARENT_ARTIFACTS.items():
        path = repository_root / relative_path
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            errors.append(f"parent {name} artifact unavailable: {error}")
            continue
        if expected.get(name) != actual:
            errors.append(f"parent {name} SHA-256 mismatch")
    return errors


def load_protocol_v3(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_protocol_v3(document)
    if errors:
        raise ProtocolV3Error("; ".join(errors))
    return document
