"""Sourcegraph-verified, pinned-Git-authoritative longitudinal hunk shards."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from authorship.build_survival_transitions import line_id
from authorship.languages import SKIP_PATH
from authorship.protocol_v3 import validate_protocol_v3

SHARD_VERSION = 3
HORIZONS = (30, 90, 180, 365)
LINEAGE_STATES = (
    "unchanged",
    "modified_candidate",
    "deleted",
    "unobservable",
    "right_censored",
)
ALLOWED_SOURCEGRAPH_CAPABILITIES = frozenset(
    {
        "indexed_search",
        "revision_search",
        "commit_search",
        "diff_search",
        "blame",
        "structural_search",
        "repository_metadata",
    }
)


class LongitudinalExtractionError(ValueError):
    """Raised when evidence cannot support a reproducible longitudinal shard."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def longitudinal_shard_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "longitudinal_shard_sha256"
    }
    return _content_sha256(content)


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LongitudinalExtractionError(f"{field} must be an object")
    return value


def _required_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise LongitudinalExtractionError(f"{field} must be a list")
    return value


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    errors = validate_protocol_v3(protocol)
    if errors:
        raise LongitudinalExtractionError("; ".join(errors))
    if tuple(protocol["survival"]["horizons_days"]) != HORIZONS:
        raise LongitudinalExtractionError("protocol horizons do not match v3")


def _repository_fields(repository: Mapping[str, Any]) -> dict[str, str]:
    required = (
        "canonical_repository_id",
        "sourcegraph_name",
        "cutoff_commit",
        "cutoff_tree",
        "bundle_sha256",
    )
    values = {}
    for field in required:
        value = repository.get(field)
        if not isinstance(value, str) or not value:
            raise LongitudinalExtractionError(f"repository {field} is required")
        values[field] = value
    if values["canonical_repository_id"].count("/") != 1:
        raise LongitudinalExtractionError("canonical repository ID is invalid")
    if not values["sourcegraph_name"].startswith("github.com/sg-evals/"):
        raise LongitudinalExtractionError("Sourcegraph repository must be in sg-evals")
    for field in ("cutoff_commit", "cutoff_tree"):
        if not _is_hex(values[field], 40):
            raise LongitudinalExtractionError(f"repository {field} is invalid")
    if not _is_hex(values["bundle_sha256"], 64):
        raise LongitudinalExtractionError("repository bundle_sha256 is invalid")
    return values


def _hunk_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("merge_commit"),
        record.get("path"),
        record.get("diff_base"),
        record.get("pr_number"),
    )


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_introduction(record: Mapping[str, Any]) -> None:
    for field in ("merge_commit", "diff_base"):
        if not _is_hex(record.get(field), 40):
            raise LongitudinalExtractionError(f"introduction {field} is invalid")
    if not _is_hex(record.get("content_sha256"), 64):
        raise LongitudinalExtractionError("introduction content_sha256 is invalid")
    path = record.get("path")
    if not isinstance(path, str) or not path:
        raise LongitudinalExtractionError("introduction path is required")
    line_number = record.get("line_number")
    if (
        isinstance(line_number, bool)
        or not isinstance(line_number, int)
        or line_number < 1
    ):
        raise LongitudinalExtractionError("introduction line_number is invalid")
    _timestamp(record.get("merged_at"), "introduction merged_at")
    if record.get("language") not in {"Python", "Go"}:
        raise LongitudinalExtractionError("introduction language is invalid")
    tier = record.get("provenance_tier")
    if isinstance(tier, bool) or tier not in {1, 2}:
        raise LongitudinalExtractionError("introduction provenance_tier is invalid")
    if not isinstance(record.get("agent_family"), str) or not record.get(
        "agent_family"
    ):
        raise LongitudinalExtractionError("introduction agent_family is required")
    pr_number = record.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        raise LongitudinalExtractionError("introduction pr_number is invalid")


def _hunk_id(repository_id: str, key: Sequence[Any]) -> str:
    return _content_sha256(
        {
            "canonical_repository_id": repository_id,
            "introducing_commit_oid": key[0],
            "path": key[1],
            "diff_base": key[2],
            "pr_number": key[3],
        }
    )


def _group_introductions(
    repository_id: str, introductions: Sequence[Mapping[str, Any]]
) -> list[tuple[str, tuple[Any, ...], list[Mapping[str, Any]]]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for raw_record in introductions:
        record = _required_mapping(raw_record, "introduction")
        _validate_introduction(record)
        if record.get("repository_id") != repository_id:
            raise LongitudinalExtractionError("introduction repository does not match")
        groups[_hunk_key(record)].append(record)
    grouped = [
        (_hunk_id(repository_id, key), key, sorted(records, key=_line_order))
        for key, records in groups.items()
    ]
    return sorted(grouped, key=lambda item: item[0])


def _line_order(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("line_number", -1),
        record.get("content_sha256", ""),
    )


def _transition_map(
    transitions: Sequence[Mapping[str, Any]],
    repository_id: str,
) -> dict[str, list[Mapping[str, Any]]]:
    by_line: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw_transition in transitions:
        transition = _required_mapping(raw_transition, "transition")
        identifier = transition.get("line_id")
        if not isinstance(identifier, str) or not identifier:
            raise LongitudinalExtractionError("transition line_id is required")
        if transition.get("repository_id") != repository_id:
            raise LongitudinalExtractionError("transition repository does not match")
        state = transition.get("state")
        status = transition.get("horizon_status")
        if status not in {"observed", "right_censored"} or (
            (state == "right_censored") != (status == "right_censored")
        ):
            raise LongitudinalExtractionError(
                "transition horizon status is inconsistent with state"
            )
        by_line[identifier].append(transition)
    return by_line


def _lineage(
    records: Sequence[Mapping[str, Any]],
    transitions: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, int]], str, list[str]]:
    counts = {str(horizon): Counter() for horizon in HORIZONS}
    selected = []
    identifiers = []
    for record in records:
        identifier = line_id(dict(record))
        identifiers.append(identifier)
        line_transitions = list(transitions.get(identifier, []))
        horizons = [transition.get("horizon_days") for transition in line_transitions]
        if sorted(horizons) != list(HORIZONS) or len(set(horizons)) != len(HORIZONS):
            raise LongitudinalExtractionError(
                "every line must contain all frozen horizons exactly once"
            )
        for transition in line_transitions:
            state = transition.get("state")
            if state not in LINEAGE_STATES:
                raise LongitudinalExtractionError("transition state is invalid")
            counts[str(transition["horizon_days"])][state] += 1
            selected.append(dict(transition))
    selected.sort(key=lambda item: (item["line_id"], item["horizon_days"]))
    lineage = {
        horizon: {state: counter[state] for state in LINEAGE_STATES}
        for horizon, counter in counts.items()
    }
    return lineage, _content_sha256(selected), sorted(identifiers)


def _observability(
    lineage: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    return {
        horizon: {
            "observable_lines": sum(
                counts[state]
                for state in ("unchanged", "modified_candidate", "deleted")
            ),
            "unobservable_lines": counts["unobservable"],
            "right_censored_lines": counts["right_censored"],
        }
        for horizon, counts in lineage.items()
    }


def _observation_map(
    observation: Mapping[str, Any], cutoff_commit: str
) -> dict[str, list[Mapping[str, Any]]]:
    if observation.get("indexed_revision_oid") != cutoff_commit:
        raise LongitudinalExtractionError(
            "Sourcegraph indexed revision does not match cutoff"
        )
    if observation.get("scip_used") is not False:
        raise LongitudinalExtractionError("SCIP must not be used")
    if observation.get("precise_code_intelligence_used") is not False:
        raise LongitudinalExtractionError("precise code intelligence must not be used")
    capabilities = observation.get("capabilities_used")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or len(capabilities) != len(set(capabilities))
        or not set(capabilities) <= ALLOWED_SOURCEGRAPH_CAPABILITIES
    ):
        raise LongitudinalExtractionError("Sourcegraph capabilities are invalid")
    result_shas = observation.get("result_manifest_sha256s")
    if (
        not isinstance(result_shas, list)
        or not result_shas
        or len(result_shas) != len(set(result_shas))
        or any(not _is_hex(value, 64) for value in result_shas)
    ):
        raise LongitudinalExtractionError(
            "Sourcegraph result manifest checksums are invalid"
        )
    by_path: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw_hunk in _required_list(observation.get("hunks"), "Sourcegraph hunks"):
        hunk = _required_mapping(raw_hunk, "Sourcegraph hunk")
        path = hunk.get("path")
        if not isinstance(path, str) or not path:
            raise LongitudinalExtractionError("Sourcegraph hunk path is required")
        by_path[path].append(hunk)
    return by_path


def _matching_observation(
    path_observations: Sequence[Mapping[str, Any]], commit_oid: str
) -> Mapping[str, Any] | None:
    exact = [
        observation
        for observation in path_observations
        if observation.get("introducing_commit_oid") == commit_oid
    ]
    if len(exact) == 1:
        return exact[0]
    if not exact and len(path_observations) == 1:
        return path_observations[0]
    return None


def _sourcegraph_verification(
    hunk_id: str,
    key: Sequence[Any],
    observations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    candidate = _matching_observation(observations.get(key[1], []), key[0])
    if candidate is None:
        return "missing", None, None
    result_ids = candidate.get("sourcegraph_result_ids")
    blame_commit_oids = candidate.get("blame_commit_oids")
    if (
        not isinstance(result_ids, list)
        or not result_ids
        or any(
            not isinstance(value, str)
            or not value.startswith("sha256:")
            or not _is_hex(value.removeprefix("sha256:"), 64)
            for value in result_ids
        )
    ):
        raise LongitudinalExtractionError("Sourcegraph result IDs are invalid")
    if (
        not isinstance(blame_commit_oids, list)
        or not blame_commit_oids
        or any(not _is_hex(value, 40) for value in blame_commit_oids)
    ):
        raise LongitudinalExtractionError("Sourcegraph blame commit OIDs are invalid")
    evidence = {
        "sourcegraph_result_ids": sorted(set(result_ids)),
        "blame_commit_oids": sorted(set(blame_commit_oids)),
    }
    fields = []
    if candidate.get("introducing_commit_oid") != key[0]:
        fields.append("introducing_commit_oid")
    if candidate.get("path") != key[1]:
        fields.append("path")
    if fields:
        return (
            "disagreement",
            {"hunk_id": hunk_id, "fields": sorted(fields)},
            evidence,
        )
    return "verified", None, evidence


def _consistent_hunk_fields(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    fields = (
        "merged_at",
        "language",
        "agent_family",
        "provenance_tier",
    )
    first = records[0]
    if any(
        record.get(field) != first.get(field) for record in records for field in fields
    ):
        raise LongitudinalExtractionError("hunk introduction metadata is inconsistent")
    return first


def _included_hunk(
    hunk_id: str,
    key: Sequence[Any],
    records: Sequence[Mapping[str, Any]],
    transitions: Mapping[str, Sequence[Mapping[str, Any]]],
    file_age_days: Mapping[str, Any],
    observations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    first = _consistent_hunk_fields(records)
    age_key = f"{key[0]}\0{key[1]}"
    age = file_age_days.get(age_key)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0:
        raise LongitudinalExtractionError("file code age must be a nonnegative number")
    lineage, lineage_sha256, line_ids = _lineage(records, transitions)
    verification, disagreement, sourcegraph_evidence = _sourcegraph_verification(
        hunk_id, key, observations
    )
    return {
        "hunk_id": hunk_id,
        "introducing_commit_oid": key[0],
        "diff_base_oid": key[2],
        "event_time": first["merged_at"],
        "path": key[1],
        "language": first["language"],
        "change_size_lines": len(records),
        "code_age_days": float(age),
        "evidence_tier": f"tier_{first['provenance_tier']}",
        "agent_family": first["agent_family"],
        "line_ids": line_ids,
        "lineage": lineage,
        "lineage_sha256": lineage_sha256,
        "observability": _observability(lineage),
        "sourcegraph_verification": verification,
        "sourcegraph_evidence": sourcegraph_evidence,
    }, disagreement


def _excluded_hunk(hunk_id: str, key: Sequence[Any], records: Sequence[Any]) -> dict:
    return {
        "hunk_id": hunk_id,
        "introducing_commit_oid": key[0],
        "path": key[1],
        "change_size_lines": len(records),
        "reason": "vendored_or_generated_path",
    }


def _sourcegraph_status(hunks: Sequence[Mapping[str, Any]]) -> str:
    if not hunks:
        return "no_included_hunks"
    statuses = {hunk["sourcegraph_verification"] for hunk in hunks}
    if "disagreement" in statuses:
        return "disagreement"
    if "missing" in statuses:
        return "incomplete"
    return "verified"


def _build_hunks(
    repository_id: str,
    introductions: Sequence[Mapping[str, Any]],
    transition_records: Mapping[str, Sequence[Mapping[str, Any]]],
    file_age_days: Mapping[str, Any],
    observations: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    hunks = []
    exclusions = []
    disagreements = []
    for hunk_id, key, records in _group_introductions(repository_id, introductions):
        if SKIP_PATH(key[1]):
            exclusions.append(_excluded_hunk(hunk_id, key, records))
            continue
        hunk, disagreement = _included_hunk(
            hunk_id,
            key,
            records,
            transition_records,
            file_age_days,
            observations,
        )
        hunks.append(hunk)
        if disagreement is not None:
            disagreements.append(disagreement)
    return hunks, exclusions, disagreements


def build_longitudinal_shard(
    protocol: Mapping[str, Any],
    repository: Mapping[str, Any],
    introductions: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    *,
    execution_unit_id: str,
    file_age_days: Mapping[str, Any],
    sourcegraph_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine indexed verification with authoritative pinned-Git lineage."""
    _validate_protocol(protocol)
    if not _is_hex(execution_unit_id, 64):
        raise LongitudinalExtractionError("execution_unit_id is invalid")
    fields = _repository_fields(repository)
    observation = _required_mapping(sourcegraph_observation, "Sourcegraph observation")
    observations = _observation_map(observation, fields["cutoff_commit"])
    transition_records = _transition_map(transitions, fields["canonical_repository_id"])
    expected_line_ids = _validated_introduction_ids(
        fields["canonical_repository_id"], introductions
    )
    if set(transition_records) != expected_line_ids:
        raise LongitudinalExtractionError(
            "unused transitions or missing introduction transitions"
        )
    hunks, exclusions, disagreements = _build_hunks(
        fields["canonical_repository_id"],
        introductions,
        transition_records,
        file_age_days,
        observations,
    )
    document = _shard_document(
        protocol,
        fields,
        observation,
        execution_unit_id,
        hunks,
        exclusions,
        disagreements,
    )
    return {
        **document,
        "longitudinal_shard_sha256": longitudinal_shard_sha256(document),
    }


def _validated_introduction_ids(
    repository_id: str, introductions: Sequence[Mapping[str, Any]]
) -> set[str]:
    identifiers = set()
    for raw_record in introductions:
        record = _required_mapping(raw_record, "introduction")
        _validate_introduction(record)
        if record.get("repository_id") != repository_id:
            raise LongitudinalExtractionError("introduction repository does not match")
        identifiers.add(line_id(dict(record)))
    return identifiers


def _shard_document(
    protocol: Mapping[str, Any],
    repository: Mapping[str, str],
    observation: Mapping[str, Any],
    execution_unit_id: str,
    hunks: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
    disagreements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "longitudinal_shard_version": SHARD_VERSION,
        "execution_unit_id": execution_unit_id,
        "protocol_sha256": protocol["protocol_sha256"],
        "canonical_repository_id": repository["canonical_repository_id"],
        "sourcegraph_name": repository["sourcegraph_name"],
        "cutoff_commit": repository["cutoff_commit"],
        "pinned_git": {
            "cutoff_commit": repository["cutoff_commit"],
            "cutoff_tree": repository["cutoff_tree"],
            "bundle_sha256": repository["bundle_sha256"],
            "authoritative_for_lineage": True,
        },
        "sourcegraph": {
            "indexed_revision_oid": observation["indexed_revision_oid"],
            "capabilities_used": sorted(observation.get("capabilities_used", [])),
            "precise_code_intelligence_used": False,
            "scip_used": False,
            "result_manifest_sha256s": sorted(
                observation.get("result_manifest_sha256s", [])
            ),
            "verification_status": _sourcegraph_status(hunks),
        },
        "horizons_days": list(HORIZONS),
        "hunk_count": len(hunks),
        "excluded_hunk_count": len(exclusions),
        "sourcegraph_git_disagreement_count": len(disagreements),
        "hunks": list(hunks),
        "exclusions": list(exclusions),
        "sourcegraph_git_disagreements": list(disagreements),
        "outcomes_consulted": False,
    }


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise LongitudinalExtractionError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LongitudinalExtractionError(f"{field} must be a timestamp") from error
    if parsed.utcoffset() is None:
        raise LongitudinalExtractionError(f"{field} must include a timezone")
    return parsed
