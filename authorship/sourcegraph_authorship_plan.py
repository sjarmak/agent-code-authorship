"""Outcome-blind plan for exact Sourcegraph authorship units."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

PLAN_VERSION = 1
LANGUAGES = ("Go", "Python")


class AuthorshipPlanError(ValueError):
    """Raised when an exact-authorship plan cannot be frozen safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def authorship_unit_plan_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "authorship_unit_plan_sha256"
    }
    return _sha256(content)


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AuthorshipPlanError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuthorshipPlanError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise AuthorshipPlanError(f"{label} timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _repository_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or "/" not in value:
        raise AuthorshipPlanError(f"{label} repository identity is invalid")
    return value.lower()


def _hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _target_ids(targets: Mapping[str, Any]) -> set[str]:
    rows = targets.get("repositories")
    if not isinstance(rows, list):
        raise AuthorshipPlanError("target manifest repositories are missing")
    return {_repository_id(row.get("id"), "target") for row in rows}


def _index_rows(index: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    rows = index.get("repositories")
    if not isinstance(rows, list):
        raise AuthorshipPlanError("Sourcegraph index manifest is missing")
    result = {}
    for row in rows:
        repository = _repository_id(
            row.get("canonical_repository_id"), "index manifest"
        )
        sourcegraph = row.get("sourcegraph")
        name = (
            sourcegraph.get("selected_name")
            if isinstance(sourcegraph, Mapping)
            else None
        )
        cutoff = row.get("cutoff_commit")
        if not isinstance(name, str) or not name.startswith("github.com/sg-evals/"):
            continue
        if not _hex(cutoff, 40):
            raise AuthorshipPlanError(f"{repository} cutoff commit is invalid")
        if repository in result:
            raise AuthorshipPlanError(
                f"{repository} has duplicate Sourcegraph mappings"
            )
        result[repository] = {"sourcegraph_name": name, "cutoff_commit": cutoff}
    return result


def _require_mapping(
    index: Mapping[str, dict[str, str]], repository: str
) -> dict[str, str]:
    try:
        return index[repository]
    except KeyError as error:
        raise AuthorshipPlanError(
            f"{repository} has no usable Sourcegraph mapping"
        ) from error


def _agent_rows(
    catalog: Mapping[str, Any],
    targets: set[str],
    index: Mapping[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], set[str]]:
    rows = catalog.get("commits")
    if not isinstance(rows, list):
        raise AuthorshipPlanError("agent commit catalog is missing")
    agents, overlaps = [], set()
    for row in rows:
        if (
            row.get("decision") != "accept_confirmed"
            or row.get("evidence_tier") != "confirmed"
            or row.get("primary_scope_eligible") is not True
        ):
            continue
        repository = _repository_id(row.get("repository_id"), "agent")
        if repository in targets:
            overlaps.add(repository)
            continue
        commit_oid = row.get("commit_oid")
        if not _hex(commit_oid, 40):
            raise AuthorshipPlanError(f"{repository} agent commit is invalid")
        observed_at = _iso(_timestamp(row.get("observed_at"), repository))
        mapping = _require_mapping(index, repository)
        agents.append(
            {
                "repository_id": repository,
                "sourcegraph_name": mapping["sourcegraph_name"],
                "commit_oid": commit_oid,
                "observed_at": observed_at,
                "languages": list(LANGUAGES),
                "authorship_role": "agent",
                "evidence_tier": "confirmed_agent_commit",
            }
        )
    agents.sort(key=lambda row: (row["repository_id"], row["commit_oid"]))
    if len(agents) != len({row["repository_id"] for row in agents}):
        raise AuthorshipPlanError("agent repositories must be unique")
    return agents, overlaps


def _h3_rows(
    freeze: Mapping[str, Any],
    agents: Sequence[Mapping[str, Any]],
    targets: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        repositories = freeze["cohorts"]["human_evidence"]["H3_pre_adoption_proxy"][
            "contemporary_pre_adoption"
        ]["repository_ids"]
    except (KeyError, TypeError) as error:
        raise AuthorshipPlanError("H3 contemporary cohort is missing") from error
    agent_by_repository = {row["repository_id"]: row for row in agents}
    rows, overlaps = [], set()
    for value in repositories:
        repository = _repository_id(value, "H3")
        if repository in targets:
            overlaps.add(repository)
            continue
        if repository not in agent_by_repository:
            raise AuthorshipPlanError(f"{repository} has no confirmed agent event")
        agent = agent_by_repository[repository]
        end = _timestamp(agent["observed_at"], repository)
        rows.append(
            {
                "repository_id": repository,
                "sourcegraph_name": agent["sourcegraph_name"],
                "agent_commit_oid": agent["commit_oid"],
                "start_inclusive": _iso(end - timedelta(days=180)),
                "end_exclusive": _iso(end),
                "languages": list(LANGUAGES),
                "authorship_role": "human",
                "evidence_tier": "H3_contemporary_pre_adoption",
            }
        )
    return sorted(rows, key=lambda row: row["repository_id"]), overlaps


def _h2_rows(
    freeze: Mapping[str, Any],
    targets: set[str],
    index: Mapping[str, dict[str, str]],
    cutoff: datetime,
) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        repositories = freeze["cohorts"]["human_evidence"]["H2_policy_human"][
            "repositories"
        ]
    except (KeyError, TypeError) as error:
        raise AuthorshipPlanError("H2 policy cohort is missing") from error
    rows, overlaps = [], set()
    for source in repositories:
        language = source.get("language")
        if language not in LANGUAGES:
            continue
        repository = _repository_id(source.get("repository_id"), "H2")
        if repository in targets:
            overlaps.add(repository)
            continue
        policy_oid = source.get("policy_commit_oid")
        if not _hex(policy_oid, 40):
            raise AuthorshipPlanError(f"{repository} policy commit is invalid")
        effective = _timestamp(source.get("policy_effective_at"), repository)
        start = max(effective, cutoff - timedelta(days=180))
        mapping = _require_mapping(index, repository)
        rows.append(
            {
                "repository_id": repository,
                "sourcegraph_name": mapping["sourcegraph_name"],
                "head_oid": mapping["cutoff_commit"],
                "policy_commit_oid": policy_oid,
                "start_inclusive": _iso(start),
                "end_exclusive": _iso(cutoff),
                "languages": [language],
                "authorship_role": "human",
                "evidence_tier": "H2_policy_human",
            }
        )
    return sorted(rows, key=lambda row: row["repository_id"]), overlaps


def _outcome_blind(
    protocol: Mapping[str, Any],
    freeze: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> bool:
    selection = protocol.get("selection")
    return (
        isinstance(selection, Mapping)
        and selection.get("outcome_blind") is True
        and freeze.get("outcomes_consulted") is False
        and catalog.get("outcomes_consulted") is False
    )


def _materialization_rules() -> dict[str, Any]:
    return {
        "agent": "all_eligible_hunks_in_each_confirmed_commit",
        "H2_policy_human": "all_eligible_hunks_in_frozen_window",
        "H3_contemporary_pre_adoption": {
            "candidate_order": "reverse_chronological_from_adoption",
            "exact_strata": ["language", "path_type", "code_age_days"],
            "within_stratum_order": ["calendar_time", "change_size"],
            "without_replacement": True,
            "scan_stop": (
                "each_agent_stratum_has_equal_human_hunk_capacity_or_window_exhausted"
            ),
            "introduced_hunk_code_age_days": 0,
            "insufficient_capacity": "retain_unmatched_and_fail_identification_gate",
        },
    }


def build_authorship_unit_plan(
    protocol: Mapping[str, Any],
    freeze: Mapping[str, Any],
    catalog: Mapping[str, Any],
    targets: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze exact-unit population and windows before diff extraction."""
    if not _outcome_blind(protocol, freeze, catalog):
        raise AuthorshipPlanError("authorship unit selection must remain outcome blind")
    cutoff = _timestamp(protocol.get("snapshot", {}).get("cutoff"), "snapshot")
    target_ids = _target_ids(targets)
    index = _index_rows(index_manifest)
    agents, agent_overlap = _agent_rows(catalog, target_ids, index)
    h3, h3_overlap = _h3_rows(freeze, agents, target_ids)
    h2, h2_overlap = _h2_rows(freeze, target_ids, index, cutoff)
    overlaps = sorted(agent_overlap | h3_overlap | h2_overlap)
    document = {
        "authorship_unit_plan_version": PLAN_VERSION,
        "status": "frozen_before_outcome_extraction",
        "protocol_sha256": protocol.get("protocol_sha256"),
        "cohort_freeze_sha256": freeze.get("cohort_freeze_sha256"),
        "agent_catalog_sha256": catalog.get("catalog_sha256"),
        "target_manifest_content_sha256": _sha256(targets),
        "target_repository_ids": sorted(target_ids),
        "index_manifest_content_sha256": _sha256(index_manifest),
        "window_days": 180,
        "materialization_rules": _materialization_rules(),
        "agent_commits": agents,
        "h3_pairs": h3,
        "h2_windows": h2,
        "exclusions": {"target_overlap_repository_ids": overlaps},
        "counts": {
            "agent_commits": len(agents),
            "h3_pairs": len(h3),
            "h2_repositories": len(h2),
            "excluded_target_overlap_repositories": len(overlaps),
        },
        "outcomes_consulted": False,
    }
    return {
        **document,
        "authorship_unit_plan_sha256": authorship_unit_plan_sha256(document),
    }


def _target_population_errors(
    document: Mapping[str, Any],
    populations: Mapping[str, Sequence[Mapping[str, Any]]],
    targets: Mapping[str, Any] | None,
) -> list[str]:
    embedded = document.get("target_repository_ids")
    if (
        not isinstance(embedded, list)
        or embedded != sorted(set(embedded))
        or any(not isinstance(value, str) for value in embedded)
    ):
        return ["authorship unit plan target repository frame is invalid"]
    errors = []
    if targets is None:
        errors.append("authorship unit plan pinned target manifest is required")
    else:
        if document.get("target_manifest_content_sha256") != _sha256(targets):
            errors.append("authorship unit plan target manifest does not match")
        if embedded != sorted(_target_ids(targets)):
            errors.append("authorship unit plan target repository frame does not match")
    target_ids = set(embedded)
    population_ids = {
        str(row.get("repository_id")) for rows in populations.values() for row in rows
    }
    overlap = target_ids & population_ids
    exclusions = document.get("exclusions", {}).get("target_overlap_repository_ids", [])
    if overlap:
        errors.append("authorship unit plan contains target-overlap model populations")
    if not isinstance(exclusions, list) or not overlap <= set(exclusions):
        errors.append("authorship unit plan target exclusions do not match overlap")
    elif any(value not in target_ids for value in exclusions):
        errors.append(
            "authorship unit plan target exclusions are not target repositories"
        )
    return errors


def validate_authorship_unit_plan(
    document: Mapping[str, Any],
    targets: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate a persisted plan without consulting mutable external state."""
    errors = []
    if document.get("authorship_unit_plan_version") != PLAN_VERSION:
        errors.append("authorship unit plan version does not match")
    if document.get("authorship_unit_plan_sha256") != authorship_unit_plan_sha256(
        document
    ):
        errors.append("authorship unit plan SHA-256 does not match")
    lists = {
        "agent_commits": document.get("agent_commits"),
        "h3_pairs": document.get("h3_pairs"),
        "h2_windows": document.get("h2_windows"),
    }
    if any(not isinstance(value, list) for value in lists.values()):
        return [*errors, "authorship unit plan populations are invalid"]
    errors.extend(_target_population_errors(document, lists, targets))
    counts = document.get("counts")
    expected = {
        "agent_commits": len(lists["agent_commits"]),
        "h3_pairs": len(lists["h3_pairs"]),
        "h2_repositories": len(lists["h2_windows"]),
        "excluded_target_overlap_repositories": len(
            document.get("exclusions", {}).get("target_overlap_repository_ids", [])
        ),
    }
    if counts != expected:
        errors.append("authorship unit plan counts do not match")
    if document.get("outcomes_consulted") is not False:
        errors.append("authorship unit plan is not outcome blind")
    return errors
