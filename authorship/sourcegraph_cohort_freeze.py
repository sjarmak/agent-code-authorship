"""Outcome-blind longitudinal cohort and precision-gate freezing."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from authorship.sourcegraph_cohort_inputs import validate_unique_input_identities
from authorship.sourcegraph_cohort_precision import (
    SIMULATION_CORRELATION,
    SIMULATION_EFFECT,
    SIMULATION_TARGET_POWER,
    CohortPrecisionError,
    simulate_paired_repository_power,
)
from authorship.sourcegraph_cohort_validation import cohort_freeze_sha256

COHORT_FREEZE_VERSION = 1
PRIMARY_STATUS = "dated_sequential"


class CohortFreezeError(ValueError):
    """The frozen inputs cannot support a valid longitudinal cohort freeze."""


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CohortFreezeError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CohortFreezeError(f"{label} is invalid") from error
    return parsed


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CohortFreezeError(f"{label} must be a positive integer")
    return value


def _validate_input_roots(documents: Sequence[Mapping[str, Any]]) -> None:
    if any(not isinstance(document, Mapping) for document in documents):
        raise CohortFreezeError("every cohort input must be a JSON object")
    outcome_inputs = [
        document for document in documents if "outcomes_consulted" in document
    ]
    if any(
        document.get("outcomes_consulted") is not False for document in outcome_inputs
    ):
        raise CohortFreezeError("cohort inputs must remain outcome blind")


def _protocol_settings(protocol: Mapping[str, Any]) -> dict[str, Any]:
    try:
        windows = protocol["adoption"]["event_windows_days"]
        gates = protocol["identification_gates"]
        settings = {
            "cutoff": protocol["snapshot"]["cutoff"],
            "languages": tuple(protocol["languages"]),
            "primary_tier": protocol["adoption"]["primary_event_tier"],
            "clean_days": protocol["adoption"]["clean_prehistory_days"],
            "primary_window": tuple(windows["primary"]),
            "extended_window": tuple(windows["extended"]),
            "minimum_adopters": gates["minimum_adopters_per_language"],
            "minimum_controls": gates["minimum_controls_per_language"],
            "minimum_family_repositories": gates["minimum_agent_family_repositories"],
        }
    except (KeyError, TypeError) as error:
        raise CohortFreezeError("protocol cohort settings are incomplete") from error
    _validate_settings(settings)
    return settings


def _validate_discovery_start(
    discovery: Mapping[str, Any], discovery_start: str
) -> None:
    query_families = discovery.get("query_families")
    if not isinstance(query_families, list):
        raise CohortFreezeError("discovery query families are invalid")
    required = {
        "agent_trailer_commits",
        "recognized_agent_identity_commits",
        "generated_code_disclosure_diffs",
    }
    queries = {
        row.get("id"): row.get("query_template")
        for row in query_families
        if row.get("id") in required
    }
    date = discovery_start.split("T", 1)[0]
    if set(queries) != required or any(
        not isinstance(query, str) or f'after:"{date}"' not in query
        for query in queries.values()
    ):
        raise CohortFreezeError("discovery start differs from dated adoption queries")


def _validate_settings(settings: Mapping[str, Any]) -> None:
    if set(settings["languages"]) != {"Python", "Go"}:
        raise CohortFreezeError("primary cohort languages must be Python and Go")
    if settings["primary_tier"] != "confirmed":
        raise CohortFreezeError("primary adoption tier must remain confirmed")
    for field in ("clean_days", "minimum_adopters", "minimum_controls"):
        _positive_integer(settings[field], field)
    _positive_integer(settings["minimum_family_repositories"], "minimum family")
    if settings["primary_window"] != (-180, 180):
        raise CohortFreezeError("primary event window must remain -180/+180")
    if settings["extended_window"] != (-365, 365):
        raise CohortFreezeError("extended event window must remain -365/+365")


def _adoption_index(adoption: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records = adoption.get("repositories")
    if not isinstance(records, list):
        raise CohortFreezeError("adoption repository records are invalid")
    indexed = {record.get("repository_id"): record for record in records}
    if None in indexed or len(indexed) != len(records):
        raise CohortFreezeError("adoption repository identity is invalid")
    return indexed


def _eligible_event(
    record: Mapping[str, Any],
    *,
    language: str,
    tier: str,
    pre_days: int,
    post_days: int,
    discovery_start: datetime,
    cutoff: datetime,
    clean_days: int,
) -> bool:
    if (
        record.get("language") != language
        or record.get("status") != PRIMARY_STATUS
        or record.get("evidence_tier") != tier
        or record.get("primary_scope_eligible") is not True
        or record.get("clean_prehistory") is not True
        or record.get("unresolved_earlier_candidate_count") != 0
    ):
        return False
    observed = _parse_time(record.get("adoption_observed_at"), "adoption time")
    return (
        observed - timedelta(days=clean_days) >= discovery_start
        and observed - timedelta(days=pre_days) >= discovery_start
        and observed + timedelta(days=post_days) <= cutoff
    )


def _event_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    language: str,
    tier: str,
    window: tuple[int, int],
    discovery_start: datetime,
    cutoff: datetime,
    clean_days: int,
) -> list[str]:
    pre_days, post_days = abs(window[0]), window[1]
    return sorted(
        record["repository_id"]
        for record in records
        if _eligible_event(
            record,
            language=language,
            tier=tier,
            pre_days=pre_days,
            post_days=post_days,
            discovery_start=discovery_start,
            cutoff=cutoff,
            clean_days=clean_days,
        )
    )


def _safe_policy_rows(
    ai_ban: Mapping[str, Any],
    adoption_index: Mapping[str, Mapping[str, Any]],
    agent_repositories: set[str],
) -> list[Mapping[str, Any]]:
    rows = ai_ban.get("repositories")
    if not isinstance(rows, list):
        raise CohortFreezeError("AI-ban repository records are invalid")
    return [
        row
        for row in rows
        if row.get("status") == "dated_policy"
        and row.get("scope_eligible") is True
        and row.get("accepted_policy")
        and row.get("canonical_repository_id") not in agent_repositories
        and not str(
            adoption_index.get(row.get("canonical_repository_id"), {}).get("status", "")
        ).startswith("dated")
    ]


def _control_language(
    language: str,
    adoption_records: Sequence[Mapping[str, Any]],
    policy_rows: Sequence[Mapping[str, Any]],
    minimum_controls: int,
) -> dict[str, Any]:
    policy_ids = {
        row["canonical_repository_id"]
        for row in policy_rows
        if row.get("language") == language
    }
    never_ids = {
        row["repository_id"]
        for row in adoption_records
        if row.get("language") == language
        and row.get("status") == "no_credible_event"
        and row.get("primary_scope_eligible") is True
    } - policy_ids
    not_yet = {
        row["repository_id"]
        for row in adoption_records
        if row.get("language") == language
        and row.get("status") == PRIMARY_STATUS
        and row.get("clean_prehistory") is True
    }
    unique_count = len(policy_ids | never_ids)
    return {
        "status": (
            "identified" if unique_count >= minimum_controls else "not_identified"
        ),
        "minimum_control_repositories": minimum_controls,
        "unique_control_count": unique_count,
        "h2_policy_repository_ids": sorted(policy_ids),
        "never_observed_repository_ids": sorted(never_ids),
        "not_yet_adopting_candidate_repository_ids": sorted(not_yet),
        "not_yet_adopting_rule": "own_adoption_after_treated_post_window",
    }


def _control_pool(
    settings: Mapping[str, Any],
    adoption_records: Sequence[Mapping[str, Any]],
    policy_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    languages = {
        language: _control_language(
            language,
            adoption_records,
            policy_rows,
            settings["minimum_controls"],
        )
        for language in settings["languages"]
    }
    roles_are_disjoint = all(
        set(row["h2_policy_repository_ids"]).isdisjoint(
            row["never_observed_repository_ids"]
        )
        for row in languages.values()
    )
    return {
        "assignment": "time_varying_language_matched",
        "roles_are_disjoint": roles_are_disjoint,
        "h2_policy_rule": "policy_effective_before_treated_pre_window",
        "languages": languages,
    }


def _window_language(
    repository_ids: list[str],
    control: Mapping[str, Any],
    minimum_adopters: int,
    simulated_minimum: int | None,
) -> dict[str, Any]:
    required = max(minimum_adopters, simulated_minimum or minimum_adopters)
    reasons = []
    if len(repository_ids) < required:
        reasons.append("insufficient_adopters_for_tier")
    if control["status"] != "identified":
        reasons.append("insufficient_static_controls")
    return {
        "status": "identified" if not reasons else "not_identified",
        "adopter_count": len(repository_ids),
        "minimum_adopter_repositories": required,
        "repository_ids": repository_ids,
        "gate_reasons": reasons,
    }


def _event_window(
    settings: Mapping[str, Any],
    adoption_records: Sequence[Mapping[str, Any]],
    controls: Mapping[str, Any],
    *,
    window_name: str,
    tier: str,
    discovery_start: datetime,
    cutoff: datetime,
    simulated_minimum: int | None,
) -> dict[str, Any]:
    window = settings[f"{window_name}_window"]
    languages = {}
    for language in settings["languages"]:
        repository_ids = _event_ids(
            adoption_records,
            language=language,
            tier=tier,
            window=window,
            discovery_start=discovery_start,
            cutoff=cutoff,
            clean_days=settings["clean_days"],
        )
        languages[language] = _window_language(
            repository_ids,
            controls["languages"][language],
            settings["minimum_adopters"],
            simulated_minimum,
        )
    return {"window_days": list(window), "evidence_tier": tier, "languages": languages}


def _event_study(
    settings: Mapping[str, Any],
    adoption_records: Sequence[Mapping[str, Any]],
    controls: Mapping[str, Any],
    discovery_start: datetime,
    cutoff: datetime,
    simulation: Mapping[str, Any],
) -> dict[str, Any]:
    minimum = simulation["selected_minimum_repositories"]
    return {
        "primary": _event_window(
            settings,
            adoption_records,
            controls,
            window_name="primary",
            tier="confirmed",
            discovery_start=discovery_start,
            cutoff=cutoff,
            simulated_minimum=minimum,
        ),
        "extended": _event_window(
            settings,
            adoption_records,
            controls,
            window_name="extended",
            tier="confirmed",
            discovery_start=discovery_start,
            cutoff=cutoff,
            simulated_minimum=minimum,
        ),
        "observed_tier_sensitivity": _event_window(
            settings,
            adoption_records,
            controls,
            window_name="primary",
            tier="observed",
            discovery_start=discovery_start,
            cutoff=cutoff,
            simulated_minimum=minimum,
        ),
        "control_pool": controls,
    }


def _policy_records(policy_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "repository_id": row["canonical_repository_id"],
            "language": row["language"],
            "policy_commit_oid": row["accepted_policy"]["commit_oid"],
            "policy_effective_at": row["accepted_policy"]["observed_at"],
            "evidence_tier": row["accepted_policy"]["evidence_tier"],
            "policy_scope": row["accepted_policy"]["policy_scope"],
        }
        for row in sorted(
            policy_rows, key=lambda value: value["canonical_repository_id"]
        )
    ]


def _target_ids(targets: Mapping[str, Any]) -> list[str]:
    rows = targets.get("repositories")
    if not isinstance(rows, list):
        raise CohortFreezeError("prevalence target records are invalid")
    identifiers = sorted(row.get("id") for row in rows)
    if any(not identifier for identifier in identifiers) or len(
        set(identifiers)
    ) != len(identifiers):
        raise CohortFreezeError("prevalence target identity is invalid")
    return identifiers


def _human_evidence(
    event_study: Mapping[str, Any],
    policy_rows: Sequence[Mapping[str, Any]],
    target_ids: list[str],
) -> dict[str, Any]:
    contemporary = sorted(
        {
            repository
            for row in event_study["primary"]["languages"].values()
            for repository in row["repository_ids"]
        }
    )
    return {
        "H1_attested_human": {
            "status": "not_available",
            "repository_count": 0,
            "repository_ids": [],
        },
        "H2_policy_human": {
            "status": "available" if policy_rows else "not_available",
            "repository_count": len(policy_rows),
            "repositories": _policy_records(policy_rows),
            "unit": "post_policy_code_introducing_commit_hunk",
        },
        "H3_pre_adoption_proxy": {
            "contemporary_pre_adoption": {
                "repository_count": len(contemporary),
                "repository_ids": contemporary,
                "window_source": "primary_event_study_pre_window",
            },
            "historical_pre_2023": {
                "repository_count": len(target_ids),
                "repository_ids": target_ids,
                "end_exclusive": "2023-01-01T00:00:00Z",
                "role": "era_adjusted_sensitivity_only",
            },
        },
    }


def _agent_family_gates(
    survival: Mapping[str, Any], minimum_repositories: int
) -> list[dict[str, Any]]:
    candidates = survival.get("candidates")
    if not isinstance(candidates, list):
        raise CohortFreezeError("survival candidate records are invalid")
    repositories: dict[str, set[str]] = defaultdict(set)
    languages: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for candidate in candidates:
        family = candidate.get("agent_family")
        repository = candidate.get("repository_id")
        language = candidate.get("language")
        if not all(
            isinstance(value, str) and value for value in (family, repository, language)
        ):
            raise CohortFreezeError("survival candidate identity is invalid")
        repositories[family].add(repository)
        languages[family][language].add(repository)
    return [
        {
            "agent_family": family,
            "status": (
                "identified"
                if len(repository_ids) >= minimum_repositories
                else "not_identified"
            ),
            "minimum_repositories": minimum_repositories,
            "repository_count": len(repository_ids),
            "repositories_by_language": {
                language: len(ids)
                for language, ids in sorted(languages[family].items())
            },
        }
        for family, repository_ids in sorted(repositories.items())
    ]


def _source_content_pins(
    protocol: Mapping[str, Any],
    discovery: Mapping[str, Any],
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    survival: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "protocol_sha256": protocol.get("protocol_sha256"),
        "discovery_specification_sha256": discovery.get("specification_sha256"),
        "adoption_catalog_sha256": adoption.get("catalog_sha256"),
        "agent_commit_catalog_sha256": agent_commits.get("catalog_sha256"),
        "ai_ban_catalog_sha256": ai_ban.get("catalog_sha256"),
        "survival_frame_sha256": survival.get("frame_sha256"),
    }


def _frame_cohorts(
    target_ids: list[str],
    survival: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    policy_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    candidates = survival["candidates"]
    survival_ids = sorted({row["repository_id"] for row in candidates})
    commits = agent_commits.get("commits")
    if not isinstance(commits, list):
        raise CohortFreezeError("agent commit records are invalid")
    agent_ids = sorted({row["repository_id"] for row in commits})
    negative_ids = sorted(row["canonical_repository_id"] for row in policy_rows)
    return {
        "prevalence_reference": {
            "repository_count": len(target_ids),
            "repository_ids": target_ids,
            "role": "fixed_confirmatory_target",
        },
        "survival": {
            "repository_count": len(survival_ids),
            "candidate_record_count": len(candidates),
            "repository_ids": survival_ids,
            "role": "fixed_confirmatory_target",
        },
        "external_validation": {
            "agent_positive_commit_count": len(commits),
            "agent_positive_repository_count": len(agent_ids),
            "agent_positive_repository_ids": agent_ids,
            "policy_negative_repository_count": len(negative_ids),
            "policy_negative_repository_ids": negative_ids,
            "repository_roles_disjoint": not bool(set(agent_ids) & set(negative_ids)),
        },
    }


def _unit_overlap_gate() -> dict[str, Any]:
    return {
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


def _freeze_document(
    protocol: Mapping[str, Any],
    sources: Mapping[str, Any],
    settings: Mapping[str, Any],
    discovery_start: str,
    simulation: Mapping[str, Any],
    cohorts: Mapping[str, Any],
    family_gates: Sequence[Mapping[str, Any]],
    input_files: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    document = {
        "$schema": "sourcegraph-longitudinal-cohort-freeze.schema.json",
        "cohort_freeze_version": COHORT_FREEZE_VERSION,
        "protocol_sha256": protocol.get("protocol_sha256"),
        "source_content_pins": sources,
        "input_files": list(input_files),
        "selection": {
            "snapshot_cutoff": settings["cutoff"],
            "discovery_start": discovery_start,
            "clean_prehistory_days": settings["clean_days"],
            "primary_event_tier": settings["primary_tier"],
            "exact_content_overlap_rule": "block_until_zero_shared_content_hashes",
            "temporal_role_rule": "same_repository_allowed_only_in_disjoint_pre_post_units",
        },
        "prospective_precision_simulation": simulation,
        "cohorts": cohorts,
        "agent_family_gates": list(family_gates),
        "unit_overlap_gate": _unit_overlap_gate(),
        "invariants": {
            "selection_outcome_blind": True,
            "primary_uses_confirmed_sequential_events_only": True,
            "control_roles_disjoint": cohorts["adoption_event_study"]["control_pool"][
                "roles_are_disjoint"
            ],
            "external_validation_repository_roles_disjoint": cohorts[
                "external_validation"
            ]["repository_roles_disjoint"],
            "exact_content_overlap_gate": "pending_unit_materialization",
        },
        "outcomes_consulted": False,
    }
    return {**document, "cohort_freeze_sha256": cohort_freeze_sha256(document)}


def _primary_capacity(
    settings: Mapping[str, Any],
    adoption_records: Sequence[Mapping[str, Any]],
    discovery_start: datetime,
    cutoff: datetime,
) -> int:
    counts = [
        len(
            _event_ids(
                adoption_records,
                language=language,
                tier="confirmed",
                window=settings["primary_window"],
                discovery_start=discovery_start,
                cutoff=cutoff,
                clean_days=settings["clean_days"],
            )
        )
        for language in settings["languages"]
    ]
    return max(settings["minimum_adopters"], min(counts))


def _prospective_simulation(
    settings: Mapping[str, Any],
    adoption_records: Sequence[Mapping[str, Any]],
    discovery_start: datetime,
    cutoff: datetime,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    maximum = _primary_capacity(settings, adoption_records, discovery_start, cutoff)
    try:
        return simulate_paired_repository_power(
            minimum_repositories=settings["minimum_adopters"],
            maximum_repositories=maximum,
            standardized_effect=SIMULATION_EFFECT,
            within_pair_correlation=SIMULATION_CORRELATION,
            target_power=SIMULATION_TARGET_POWER,
            replicates=replicates,
            seed=seed,
        )
    except CohortPrecisionError as error:
        raise CohortFreezeError(str(error)) from error


def _cohort_sets(
    event_study: Mapping[str, Any],
    policy_rows: Sequence[Mapping[str, Any]],
    target_ids: list[str],
    survival: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "adoption_event_study": event_study,
        "human_evidence": _human_evidence(event_study, policy_rows, target_ids),
        **_frame_cohorts(target_ids, survival, agent_commits, policy_rows),
    }


def _analysis_cohorts(
    settings: Mapping[str, Any],
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
    start: datetime,
    cutoff: datetime,
    simulation_replicates: int,
    simulation_seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    adoption_by_repository = _adoption_index(adoption)
    adoption_records = list(adoption_by_repository.values())
    agent_ids = {row["repository_id"] for row in agent_commits.get("commits", [])}
    policy_rows = _safe_policy_rows(ai_ban, adoption_by_repository, agent_ids)
    controls = _control_pool(settings, adoption_records, policy_rows)
    simulation = _prospective_simulation(
        settings,
        adoption_records,
        start,
        cutoff,
        simulation_replicates,
        simulation_seed,
    )
    event_study = _event_study(
        settings, adoption_records, controls, start, cutoff, simulation
    )
    cohorts = _cohort_sets(
        event_study, policy_rows, _target_ids(targets), survival, agent_commits
    )
    return simulation, cohorts


def _freeze_settings(
    protocol: Mapping[str, Any],
    discovery: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    discovery_start: str,
) -> tuple[dict[str, Any], datetime, datetime]:
    _validate_input_roots(inputs)
    settings = _protocol_settings(protocol)
    _validate_discovery_start(discovery, discovery_start)
    start = _parse_time(discovery_start, "discovery_start")
    cutoff = _parse_time(settings["cutoff"], "snapshot cutoff")
    return settings, start, cutoff


def _complete_freeze(
    protocol: Mapping[str, Any],
    discovery: Mapping[str, Any],
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    survival: Mapping[str, Any],
    settings: Mapping[str, Any],
    discovery_start: str,
    simulation: Mapping[str, Any],
    cohorts: Mapping[str, Any],
    input_files: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    sources = _source_content_pins(
        protocol, discovery, adoption, agent_commits, ai_ban, survival
    )
    family_gates = _agent_family_gates(
        survival, settings["minimum_family_repositories"]
    )
    return _freeze_document(
        protocol,
        sources,
        settings,
        discovery_start,
        simulation,
        cohorts,
        family_gates,
        input_files,
    )


def _validate_unique_inputs(
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
) -> None:
    errors = validate_unique_input_identities(
        adoption, agent_commits, ai_ban, targets, survival
    )
    if errors:
        raise CohortFreezeError("; ".join(errors))


def build_cohort_freeze(
    protocol: Mapping[str, Any],
    discovery: Mapping[str, Any],
    adoption: Mapping[str, Any],
    agent_commits: Mapping[str, Any],
    ai_ban: Mapping[str, Any],
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
    *,
    discovery_start: str,
    simulation_replicates: int,
    simulation_seed: int,
    input_files: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build the complete outcome-blind longitudinal cohort contract."""
    _validate_unique_inputs(adoption, agent_commits, ai_ban, targets, survival)
    settings, start, cutoff = _freeze_settings(
        protocol,
        discovery,
        (protocol, discovery, adoption, agent_commits, ai_ban, targets, survival),
        discovery_start,
    )
    simulation, cohorts = _analysis_cohorts(
        settings,
        adoption,
        agent_commits,
        ai_ban,
        targets,
        survival,
        start,
        cutoff,
        simulation_replicates,
        simulation_seed,
    )
    return _complete_freeze(
        protocol,
        discovery,
        adoption,
        agent_commits,
        ai_ban,
        survival,
        settings,
        discovery_start,
        simulation,
        cohorts,
        input_files,
    )
