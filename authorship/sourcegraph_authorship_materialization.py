"""Role-disjoint exact-unit materialization and H3 structural matching."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from authorship.sourcegraph_authorship_execution import validate_extraction_shard
from authorship.sourcegraph_cohort_validation import audit_materialized_unit_overlap

MATERIALIZATION_VERSION = 1


class AuthorshipMaterializationError(RuntimeError):
    """Raised when exact units violate the frozen role or temporal design."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def authorship_materialization_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "authorship_materialization_sha256"
    }
    return _sha256(content)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AuthorshipMaterializationError("unit timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuthorshipMaterializationError("unit timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise AuthorshipMaterializationError("unit timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _stratum(unit: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return unit.get("language"), unit.get("path_type"), unit.get("code_age_days")


def _calendar_distance(agent: Mapping[str, Any], human: Mapping[str, Any]) -> float:
    distance = _timestamp(agent["calendar_time"]) - _timestamp(human["calendar_time"])
    if distance.total_seconds() <= 0:
        raise AuthorshipMaterializationError(
            "H3 temporal order is not strictly pre-adoption"
        )
    return distance.total_seconds() / 86_400


def _candidate_key(
    agent: Mapping[str, Any], human: Mapping[str, Any]
) -> tuple[float, float, str]:
    calendar = _calendar_distance(agent, human)
    change_size = abs(math.log1p(agent["line_count"]) - math.log1p(human["line_count"]))
    return calendar, change_size, human["hunk_sha256"]


def _paired_ids(pairs: Sequence[Mapping[str, Any]]) -> set[str]:
    repositories = [row.get("repository_id") for row in pairs]
    if any(not isinstance(value, str) for value in repositories):
        raise AuthorshipMaterializationError("H3 pair identity is invalid")
    if len(repositories) != len(set(repositories)):
        raise AuthorshipMaterializationError("H3 pair repositories are duplicated")
    return set(repositories)


def match_h3_units(
    agent_units: Sequence[Mapping[str, Any]],
    human_units: Sequence[Mapping[str, Any]],
    pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Greedily match exact strata, calendar first, without replacement."""
    paired = _paired_ids(pairs)
    agents = sorted(
        (unit for unit in agent_units if unit.get("repository_id") in paired),
        key=lambda unit: (unit["repository_id"], unit["hunk_sha256"]),
    )
    available = list(human_units)
    matches, selected, unmatched = [], [], []
    for agent in agents:
        candidates = [
            human
            for human in available
            if human.get("repository_id") == agent.get("repository_id")
            and _stratum(human) == _stratum(agent)
        ]
        if not candidates:
            unmatched.append(agent["hunk_sha256"])
            continue
        human = min(candidates, key=lambda row: _candidate_key(agent, row))
        calendar, size, _ = _candidate_key(agent, human)
        matches.append(
            {
                "repository_id": agent["repository_id"],
                "language": agent["language"],
                "path_type": agent["path_type"],
                "code_age_days": agent["code_age_days"],
                "agent_hunk_sha256": agent["hunk_sha256"],
                "human_hunk_sha256": human["hunk_sha256"],
                "calendar_distance_days": calendar,
                "log_change_size_distance": size,
            }
        )
        selected.append(human)
        available.remove(human)
    return {
        "matches": matches,
        "matched_human_units": selected,
        "unmatched_agent_hunk_sha256s": sorted(unmatched),
    }


def _expected_task_ids(execution: Mapping[str, Any]) -> set[str]:
    fixed, repositories = execution.get("fixed_shards"), execution.get(
        "h3_repositories"
    )
    if not isinstance(fixed, list) or not isinstance(repositories, list):
        raise AuthorshipMaterializationError("execution shard references are invalid")
    references = [
        *fixed,
        *[
            reference
            for repository in repositories
            for reference in repository.get("shards", [])
        ],
    ]
    task_ids = [reference.get("task_id") for reference in references]
    if any(not isinstance(value, str) for value in task_ids):
        raise AuthorshipMaterializationError("execution task identity is invalid")
    if len(task_ids) != len(set(task_ids)):
        raise AuthorshipMaterializationError("execution task identity is duplicated")
    return set(task_ids)


def _validated_units(
    execution: Mapping[str, Any], shards: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    expected = _expected_task_ids(execution)
    observed = {shard.get("task", {}).get("task_id") for shard in shards}
    if observed != expected or len(observed) != len(shards):
        raise AuthorshipMaterializationError("materialization shards are incomplete")
    units = []
    for shard in shards:
        errors = validate_extraction_shard(shard, shard.get("task", {}))
        if errors:
            raise AuthorshipMaterializationError("; ".join(errors))
        units.extend(shard["units"])
    return units


def _unit_partitions(
    units: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    agents = [unit for unit in units if unit.get("authorship_role") == "agent"]
    h2 = [unit for unit in units if unit.get("evidence_tier") == "H2_policy_human"]
    h3 = [
        unit
        for unit in units
        if unit.get("evidence_tier") == "H3_contemporary_pre_adoption"
    ]
    if len(units) != len(agents) + len(h2) + len(h3):
        raise AuthorshipMaterializationError("unit evidence tier is invalid")
    return agents, h2, h3


def _temporal_audit(
    h3: Sequence[Mapping[str, Any]], pairs: Sequence[Mapping[str, Any]]
) -> None:
    ends = {row["repository_id"]: _timestamp(row["end_exclusive"]) for row in pairs}
    for unit in h3:
        repository = unit.get("repository_id")
        if (
            repository not in ends
            or _timestamp(unit.get("calendar_time")) >= ends[repository]
        ):
            raise AuthorshipMaterializationError("H3 temporal role violation")


def _drop_cross_role_content(
    agents: Sequence[Mapping[str, Any]],
    humans: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], dict[str, Any]]:
    agent_hashes = {unit["content_sha256"] for unit in agents}
    human_hashes = {unit["content_sha256"] for unit in humans}
    collisions = agent_hashes & human_hashes
    filtered_agents = [
        unit for unit in agents if unit["content_sha256"] not in collisions
    ]
    filtered_humans = [
        unit for unit in humans if unit["content_sha256"] not in collisions
    ]
    return (
        filtered_agents,
        filtered_humans,
        {
            "cross_role_content_hash_count": len(collisions),
            "cross_role_content_sha256s": sorted(collisions),
            "dropped_agent_units": len(agents) - len(filtered_agents),
            "dropped_human_candidate_units": len(humans) - len(filtered_humans),
        },
    )


def _sort_units(units: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        units,
        key=lambda unit: (
            unit["repository_id"],
            unit["commit_oid"],
            unit["path"],
            unit["hunk_sha256"],
        ),
    )


def _counts(
    agents: Sequence[Mapping[str, Any]],
    h2: Sequence[Mapping[str, Any]],
    h3: Sequence[Mapping[str, Any]],
    matches: Mapping[str, Any],
    overlap: Mapping[str, Any],
) -> dict[str, int]:
    return {
        "agent_units": len(agents),
        "H2_units": len(h2),
        "H3_units": len(h3),
        "H3_matches": len(matches["matches"]),
        "H3_unmatched_agent_units": len(matches["unmatched_agent_hunk_sha256s"]),
        "cross_role_collision_hashes": overlap["cross_role_content_hash_count"],
    }


def _materialization_payload(
    *,
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    agents: Sequence[Mapping[str, Any]],
    h2: Sequence[Mapping[str, Any]],
    h3: Sequence[Mapping[str, Any]],
    matches: Mapping[str, Any],
    overlap: Mapping[str, Any],
    excluded: set[str],
    postfilter_errors: Sequence[str],
) -> dict[str, Any]:
    used = {unit["repository_id"] for unit in [*agents, *h2, *h3]}
    return {
        "authorship_materialization_version": MATERIALIZATION_VERSION,
        "status": "complete",
        "authorship_unit_plan_sha256": plan.get("authorship_unit_plan_sha256"),
        "authorship_execution_sha256": execution.get("authorship_execution_sha256"),
        "agent_units": _sort_units(agents),
        "human_units": {
            "H1_attested_human": [],
            "H2_policy_human": _sort_units(h2),
            "H3_contemporary_pre_adoption": _sort_units(h3),
        },
        "h3_matching": {
            "matches": matches["matches"],
            "unmatched_agent_hunk_sha256s": matches["unmatched_agent_hunk_sha256s"],
        },
        "overlap_audit": overlap,
        "role_audit": {
            "target_overlap_repository_count": len(excluded & used),
            "temporal_violation_count": 0,
            "postfilter_overlap_errors": list(postfilter_errors),
        },
        "counts": _counts(agents, h2, h3, matches, overlap),
    }


def materialize_authorship_units(
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    shards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if execution.get("status") != "complete":
        raise AuthorshipMaterializationError("authorship execution is incomplete")
    pairs = plan.get("h3_pairs")
    if not isinstance(pairs, list):
        raise AuthorshipMaterializationError("H3 plan pairs are invalid")
    units = _validated_units(execution, shards)
    raw_agents, raw_h2, raw_h3 = _unit_partitions(units)
    _temporal_audit(raw_h3, pairs)
    agents, humans, overlap = _drop_cross_role_content(raw_agents, [*raw_h2, *raw_h3])
    h2 = [unit for unit in humans if unit["evidence_tier"] == "H2_policy_human"]
    h3_candidates = [
        unit
        for unit in humans
        if unit["evidence_tier"] == "H3_contemporary_pre_adoption"
    ]
    matches = match_h3_units(agents, h3_candidates, pairs)
    h3 = matches["matched_human_units"]
    postfilter_errors = audit_materialized_unit_overlap(agents, [*h2, *h3])
    if postfilter_errors:
        raise AuthorshipMaterializationError("; ".join(postfilter_errors))
    excluded = set(plan.get("exclusions", {}).get("target_overlap_repository_ids", []))
    used = {unit["repository_id"] for unit in [*agents, *h2, *h3]}
    if excluded & used:
        raise AuthorshipMaterializationError("target repository entered model units")
    document = _materialization_payload(
        plan=plan,
        execution=execution,
        agents=agents,
        h2=h2,
        h3=h3,
        matches=matches,
        overlap=overlap,
        excluded=excluded,
        postfilter_errors=postfilter_errors,
    )
    return {
        **document,
        "authorship_materialization_sha256": authorship_materialization_sha256(
            document
        ),
    }


def _materialization_parent_errors(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> list[str]:
    errors = []
    if document.get("authorship_materialization_version") != MATERIALIZATION_VERSION:
        errors.append("authorship materialization version does not match")
    if document.get("authorship_unit_plan_sha256") != plan.get(
        "authorship_unit_plan_sha256"
    ):
        errors.append("authorship materialization plan does not match")
    if document.get("authorship_execution_sha256") != execution.get(
        "authorship_execution_sha256"
    ):
        errors.append("authorship materialization execution does not match")
    if document.get(
        "authorship_materialization_sha256"
    ) != authorship_materialization_sha256(document):
        errors.append("authorship materialization SHA-256 does not match")
    return errors


def validate_authorship_materialization(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> list[str]:
    """Validate materialized unit counts, parents, and postfilter overlap."""
    errors = _materialization_parent_errors(document, plan, execution)
    agents = document.get("agent_units")
    humans = document.get("human_units")
    matching = document.get("h3_matching")
    if not isinstance(agents, list) or not isinstance(humans, Mapping):
        return [*errors, "authorship materialization units are invalid"]
    h2, h3 = humans.get("H2_policy_human"), humans.get("H3_contemporary_pre_adoption")
    if (
        not isinstance(h2, list)
        or not isinstance(h3, list)
        or not isinstance(matching, Mapping)
    ):
        return [*errors, "authorship materialization human tiers are invalid"]
    overlap = document.get("overlap_audit", {})
    expected = _counts(
        agents,
        h2,
        h3,
        {
            "matches": matching.get("matches", []),
            "unmatched_agent_hunk_sha256s": matching.get(
                "unmatched_agent_hunk_sha256s", []
            ),
        },
        overlap,
    )
    if document.get("counts") != expected:
        errors.append("authorship materialization counts do not match")
    errors.extend(audit_materialized_unit_overlap(agents, [*h2, *h3]))
    if document.get("status") != "complete":
        errors.append("authorship materialization is incomplete")
    return errors
