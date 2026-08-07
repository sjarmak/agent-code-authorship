"""Deterministic execution of the frozen Sourcegraph discovery searches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.sg import api
from authorship.sg_evals_mirroring import validate_action_plan
from authorship.sourcegraph_discovery import (
    REPOSITORY_EXCLUSION_DISPOSITIONS,
    validate_discovery_specification,
)
from authorship.sourcegraph_discovery_graphql import SEARCH_GRAPHQL

RESULT_MANIFEST_VERSION = 3
EXECUTION_MANIFEST_VERSION = 3
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FAMILY_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
REGEX_METACHARACTERS = frozenset(r"\.^$*+?{}[]|()")
QueryRunner = Callable[[str, str, str, str], Mapping[str, Any]]
Clock = Callable[[], datetime]
REQUIRED_RESULT_FIELDS = frozenset(
    """result_manifest_version query_family_id result_type canonical_repository_id
    sourcegraph_name cutoff_commit rendered_query rendered_query_sha256 unit_id
    executed_at sourcegraph_result_ids result_count result_object_count limit_hit
    cloning_repositories missing_repositories timed_out_repositories valid
    invalid_reasons raw_results outcomes_consulted result_manifest_sha256""".split()
)
SUMMARY_SHARD_FIELDS = (
    "unit_id",
    "query_family_id",
    "canonical_repository_id",
    "sourcegraph_name",
    "cutoff_commit",
    "rendered_query",
    "rendered_query_sha256",
    "executed_at",
    "sourcegraph_result_ids",
    "result_count",
    "limit_hit",
    "valid",
    "invalid_reasons",
    "result_manifest_sha256",
)


def _canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(document: Mapping[str, Any], excluded_field: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded_field}
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def result_manifest_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "result_manifest_sha256")


def execution_manifest_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "execution_manifest_sha256")


def _document_sha256(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document).encode()).hexdigest()


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape_repository_name(value: str) -> str:
    return "".join(
        f"\\{character}" if character in REGEX_METACHARACTERS else character
        for character in value
    )


def render_query(
    family: Mapping[str, Any], sourcegraph_name: str, cutoff_commit: str
) -> str:
    template = family.get("query_template")
    if not isinstance(template, str):
        raise ValueError("query family query_template must be a string")
    if template.count("{sourcegraph_repo_regex}") != 1:
        raise ValueError("query template must contain one repository placeholder")
    if template.count("{cutoff_commit}") != 1:
        raise ValueError("query template must contain one cutoff placeholder")
    rendered = template.replace(
        "{sourcegraph_repo_regex}", _escape_repository_name(sourcegraph_name)
    ).replace("{cutoff_commit}", cutoff_commit)
    if "patternType:" not in rendered:
        raise ValueError("rendered query must declare patternType")
    return rendered


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _repository_name(raw_result: Mapping[str, Any]) -> str | None:
    if raw_result.get("__typename") == "FileMatch":
        repository = raw_result.get("repository")
    else:
        commit = raw_result.get("commit")
        repository = commit.get("repository") if isinstance(commit, Mapping) else None
    return repository.get("name") if isinstance(repository, Mapping) else None


def _validate_file_match(
    raw_result: Mapping[str, Any], sourcegraph_name: str, cutoff_commit: str
) -> None:
    repository = _require_mapping(raw_result.get("repository"), "repository")
    file_record = _require_mapping(raw_result.get("file"), "file")
    commit = _require_mapping(file_record.get("commit"), "file.commit")
    if repository.get("name") != sourcegraph_name:
        raise ValueError("search returned an unexpected repository")
    if commit.get("oid") != cutoff_commit:
        raise ValueError("file result is not pinned to the frozen cutoff")
    if not isinstance(file_record.get("path"), str):
        raise ValueError("file result path must be a string")
    for line_match in _require_list(raw_result.get("lineMatches"), "lineMatches"):
        match = _require_mapping(line_match, "line match")
        if not isinstance(match.get("limitHit"), bool):
            raise ValueError("line match limitHit must be a boolean")


def _validate_commit_match(
    raw_result: Mapping[str, Any], sourcegraph_name: str
) -> None:
    commit = _require_mapping(raw_result.get("commit"), "commit")
    repository = _require_mapping(commit.get("repository"), "commit.repository")
    if repository.get("name") != sourcegraph_name:
        raise ValueError("search returned an unexpected repository")
    if not isinstance(commit.get("oid"), str) or not SHA1_PATTERN.fullmatch(
        commit["oid"]
    ):
        raise ValueError("commit result oid must be a 40-character SHA-1")


def _validate_raw_result(
    raw_result: Any,
    result_type: str,
    sourcegraph_name: str,
    cutoff_commit: str,
) -> Mapping[str, Any]:
    result = _require_mapping(raw_result, "search result")
    expected_typename = "FileMatch" if result_type == "file" else "CommitSearchResult"
    if result.get("__typename") != expected_typename:
        raise ValueError(f"{result_type} query returned {result.get('__typename')!r}")
    if result_type == "file":
        _validate_file_match(result, sourcegraph_name, cutoff_commit)
    else:
        _validate_commit_match(result, sourcegraph_name)
    try:
        return json.loads(_canonical_json(result))
    except (TypeError, ValueError) as error:
        raise ValueError("search result must be JSON serializable") from error


def _repository_names(value: Any, field: str) -> list[str]:
    entries = _require_list(value, field)
    names = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field} entries must identify a repository")
        names.append(name)
    return sorted(set(names))


def _search_result_envelope(data: Mapping[str, Any]) -> Mapping[str, Any]:
    search = _require_mapping(data.get("search"), "search")
    return _require_mapping(search.get("results"), "search.results")


def _normalize_response(
    response: Mapping[str, Any],
    result_type: str,
    sourcegraph_name: str,
    cutoff_commit: str,
) -> dict[str, Any]:
    results = [
        _validate_raw_result(item, result_type, sourcegraph_name, cutoff_commit)
        for item in _require_list(response.get("results"), "results")
    ]
    count = response.get("result_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("resultCount must be a non-negative integer")
    if count < len(results):
        raise ValueError("resultCount reports fewer objects than the result payload")
    limit_hit = response.get("limit_hit")
    if not isinstance(limit_hit, bool):
        raise ValueError("limitHit must be a boolean")
    nested_limit_hit = any(
        line_match["limitHit"]
        for result in results
        if result.get("__typename") == "FileMatch"
        for line_match in result["lineMatches"]
    )
    return {
        "results": results,
        "result_count": count,
        "limit_hit": limit_hit or nested_limit_hit,
        "cloning_repositories": _repository_names(
            response.get("cloning_repositories"), "cloning"
        ),
        "missing_repositories": _repository_names(
            response.get("missing_repositories"), "missing"
        ),
        "timed_out_repositories": _repository_names(
            response.get("timed_out_repositories"), "timedout"
        ),
    }


def run_sourcegraph_query(
    rendered_query: str,
    result_type: str,
    sourcegraph_name: str,
    cutoff_commit: str,
    *,
    api_runner: Callable[..., Mapping[str, Any]] = api,
) -> dict[str, Any]:
    data = _require_mapping(
        api_runner(SEARCH_GRAPHQL, query=rendered_query), "GraphQL data"
    )
    envelope = _search_result_envelope(data)
    response = {
        "results": envelope.get("results"),
        "result_count": envelope.get("resultCount"),
        "limit_hit": envelope.get("limitHit"),
        "cloning_repositories": envelope.get("cloning"),
        "missing_repositories": envelope.get("missing"),
        "timed_out_repositories": envelope.get("timedout"),
    }
    return _normalize_response(response, result_type, sourcegraph_name, cutoff_commit)


def _audit_by_repository(index_audit: Mapping[str, Any]) -> dict[str, Mapping]:
    repositories = _require_list(index_audit.get("repositories"), "audit.repositories")
    by_id: dict[str, Mapping] = {}
    for repository in repositories:
        record = _require_mapping(repository, "audit repository")
        repository_id = record.get("canonical_repository_id")
        if not isinstance(repository_id, str) or not repository_id:
            raise ValueError("audit repository has no canonical_repository_id")
        if repository_id in by_id:
            raise ValueError(f"duplicate audit repository {repository_id}")
        by_id[repository_id] = record
    return by_id


def _validate_population_artifacts(
    index_manifest: Mapping[str, Any], index_audit: Mapping[str, Any]
) -> None:
    if index_manifest.get("outcomes_consulted") is not False:
        raise ValueError("index manifest must be outcome blind")
    if index_audit.get("outcomes_consulted") is not False:
        raise ValueError("index audit must be outcome blind")
    repositories = _require_list(
        index_manifest.get("repositories"), "index_manifest.repositories"
    )
    if not repositories:
        raise ValueError("index manifest must contain repositories")
    if index_manifest.get("repository_count") != len(repositories):
        raise ValueError("index manifest repository_count does not match")
    audit_repositories = _require_list(
        index_audit.get("repositories"), "audit.repositories"
    )
    if index_audit.get("repository_count") != len(audit_repositories):
        raise ValueError("index audit repository_count does not match")
    repository_ids = [
        repository.get("canonical_repository_id")
        for repository in repositories
        if isinstance(repository, Mapping)
    ]
    if len(repository_ids) != len(set(repository_ids)):
        raise ValueError("index manifest canonical repositories must be unique")


def _matching_audit_location(
    audit_record: Mapping[str, Any], sourcegraph_name: str
) -> Mapping[str, Any] | None:
    for location_name in ("direct", "mirror"):
        location = audit_record.get(location_name)
        if isinstance(location, Mapping) and location.get("name") == sourcegraph_name:
            return location
    return None


def _repository_is_ready(
    repository: Mapping[str, Any], audit_record: Mapping[str, Any] | None
) -> bool:
    repository_id = repository.get("canonical_repository_id")
    sourcegraph = _require_mapping(repository.get("sourcegraph"), "sourcegraph")
    sourcegraph_name = sourcegraph.get("selected_name")
    cutoff_commit = repository.get("cutoff_commit")
    if not isinstance(sourcegraph_name, str) or not sourcegraph_name:
        raise ValueError(f"{repository_id} has no selected Sourcegraph name")
    if not isinstance(cutoff_commit, str) or not SHA1_PATTERN.fullmatch(cutoff_commit):
        raise ValueError(f"{repository_id} has an invalid cutoff commit")
    if audit_record is None:
        raise ValueError(f"{repository_id} has no matching index audit record")
    location = _matching_audit_location(audit_record, sourcegraph_name)
    if location is None:
        raise ValueError(
            f"{repository_id} index audit has no location for {sourcegraph_name}"
        )
    state = location.get("state")
    cutoff_state = location.get("cutoff_state")
    cutoff_oid = location.get("cutoff_oid")
    if state not in {"indexed", "not_indexed", "present_not_cloned"}:
        raise ValueError(f"{repository_id} index audit state is invalid")
    if cutoff_state not in {"accessible", "not_accessible"}:
        raise ValueError(f"{repository_id} index audit cutoff state is invalid")
    if cutoff_state == "accessible" and not (
        isinstance(cutoff_oid, str) and SHA1_PATTERN.fullmatch(cutoff_oid)
    ):
        raise ValueError(f"{repository_id} index audit cutoff OID is invalid")
    if cutoff_state == "not_accessible" and cutoff_oid is not None:
        raise ValueError(f"{repository_id} inaccessible audit cutoff must have no OID")
    return (
        state == "indexed"
        and cutoff_state == "accessible"
        and cutoff_oid == cutoff_commit
    )


def _verify_repository_ready(
    repository: Mapping[str, Any], audit_record: Mapping[str, Any] | None
) -> None:
    if not _repository_is_ready(repository, audit_record):
        repository_id = repository.get("canonical_repository_id")
        sourcegraph = _require_mapping(repository.get("sourcegraph"), "sourcegraph")
        sourcegraph_name = sourcegraph.get("selected_name")
        raise ValueError(
            f"{repository_id} is not accessible at its frozen cutoff "
            f"through {sourcegraph_name}"
        )


def _frozen_repository_exclusion_document(
    specification: Mapping[str, Any],
) -> tuple[Mapping[str, Any], list[Any]]:
    execution = _require_mapping(specification.get("execution"), "execution")
    exclusions = execution.get("repository_exclusions")
    if exclusions is None:
        return {}, []
    exclusion_document = _require_mapping(exclusions, "execution.repository_exclusions")
    repositories = _require_list(
        exclusion_document.get("repositories"),
        "execution.repository_exclusions.repositories",
    )
    return exclusion_document, repositories


def _frozen_repository_exclusion_ids(
    specification: Mapping[str, Any],
    source: bytes | None,
) -> set[str]:
    exclusion_document, raw_repositories = _frozen_repository_exclusion_document(
        specification
    )
    if not raw_repositories:
        return set()
    if source is None:
        raise ValueError("repository exclusion source bytes are required")
    if hashlib.sha256(source).hexdigest() != exclusion_document.get(
        "source_artifact_sha256"
    ):
        raise ValueError("repository exclusion source SHA-256 does not match")
    try:
        source_document = json.loads(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("repository exclusion source must be valid JSON") from error
    source_plan = _require_mapping(source_document, "repository exclusion source")
    plan_errors = validate_action_plan(source_plan)
    if source_plan.get("outcomes_consulted") is not False:
        plan_errors.append("action plan must be outcome blind")
    if plan_errors:
        raise ValueError(
            "invalid repository exclusion source: " + "; ".join(plan_errors)
        )
    expected = sorted(
        (
            {
                "canonical_repository_id": repository["canonical_repository_id"],
                "disposition": repository["disposition"],
                "reason": repository["reason"],
            }
            for repository in raw_repositories
            if isinstance(repository, Mapping)
        ),
        key=lambda repository: repository["canonical_repository_id"],
    )
    observed = sorted(
        (
            {
                "canonical_repository_id": repository.get("canonical_repository_id"),
                "disposition": repository.get("action"),
                "reason": repository.get("reason"),
            }
            for repository in source_plan["repositories"]
            if repository.get("action") in REPOSITORY_EXCLUSION_DISPOSITIONS
        ),
        key=lambda repository: str(repository["canonical_repository_id"]),
    )
    if observed != expected:
        raise ValueError(
            "repository exclusions do not match the checksummed action plan holds"
        )
    return {repository["canonical_repository_id"] for repository in expected}


def discovery_unit_id(unit: Mapping[str, Any]) -> str:
    identity = {
        key: unit[key]
        for key in (
            "query_family_id",
            "canonical_repository_id",
            "sourcegraph_name",
            "cutoff_commit",
            "rendered_query_sha256",
        )
    }
    return _document_sha256(identity)


def _repository_units(
    repository: Mapping[str, Any], families: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    sourcegraph = _require_mapping(repository.get("sourcegraph"), "sourcegraph")
    units = []
    for family in families:
        if not isinstance(family.get("id"), str) or not FAMILY_ID_PATTERN.fullmatch(
            family["id"]
        ):
            raise ValueError("query family ID is unsafe for shard storage")
        rendered = render_query(
            family, sourcegraph["selected_name"], repository["cutoff_commit"]
        )
        unit = {
            "query_family_id": family["id"],
            "result_type": family["result_type"],
            "canonical_repository_id": repository["canonical_repository_id"],
            "sourcegraph_name": sourcegraph["selected_name"],
            "cutoff_commit": repository["cutoff_commit"],
            "rendered_query": rendered,
            "rendered_query_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }
        units.append({**unit, "unit_id": discovery_unit_id(unit)})
    return units


def build_execution_units(
    specification: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    index_audit: Mapping[str, Any],
    *,
    index_manifest_sha256: str,
    repository_exclusion_source: bytes | None = None,
) -> list[dict[str, Any]]:
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise ValueError(
            "invalid discovery specification: " + "; ".join(specification_errors)
        )
    if specification.get("index_manifest_sha256") != index_manifest_sha256:
        raise ValueError("index manifest SHA-256 does not match frozen specification")
    _validate_population_artifacts(index_manifest, index_audit)
    audit_by_id = _audit_by_repository(index_audit)
    repositories = _require_list(
        index_manifest.get("repositories"), "index_manifest.repositories"
    )
    exclusion_ids = _frozen_repository_exclusion_ids(
        specification, repository_exclusion_source
    )
    repository_ids = {
        repository.get("canonical_repository_id")
        for repository in repositories
        if isinstance(repository, Mapping)
    }
    missing_audit_ids = repository_ids - set(audit_by_id)
    if missing_audit_ids:
        raise ValueError("index audit is missing repositories from the index manifest")
    missing_exclusions = sorted(exclusion_ids - repository_ids)
    if missing_exclusions:
        raise ValueError(
            f"{missing_exclusions[0]} exclusion is not present in the index manifest"
        )
    units = []
    for raw_repository in repositories:
        repository = _require_mapping(raw_repository, "index repository")
        repository_id = repository.get("canonical_repository_id")
        if repository_id in exclusion_ids:
            if _repository_is_ready(repository, audit_by_id.get(repository_id)):
                raise ValueError(
                    f"{repository_id} exclusion is accessible at its frozen cutoff"
                )
            continue
        _verify_repository_ready(repository, audit_by_id.get(repository_id))
        units.extend(_repository_units(repository, specification["query_families"]))
    return sorted(
        units,
        key=lambda unit: (unit["canonical_repository_id"], unit["query_family_id"]),
    )


def _raw_result_record(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    result_id = f"sha256:{_document_sha256(raw_result)}"
    return {"sourcegraph_result_id": result_id, "payload": raw_result}


def _invalid_reasons(response: Mapping[str, Any]) -> list[str]:
    reasons = []
    if response.get("limit_hit") is True:
        reasons.append("limit_hit_requires_partition")
    for field in (
        "cloning_repositories",
        "missing_repositories",
        "timed_out_repositories",
    ):
        if response.get(field):
            reasons.append(field)
    return reasons


def _failed_response(error: Exception) -> dict[str, Any]:
    message = " ".join(str(error).split())[:300] or type(error).__name__
    return {
        "results": [],
        "result_count": 0,
        "limit_hit": False,
        "cloning_repositories": [],
        "missing_repositories": [],
        "timed_out_repositories": [],
        "execution_error": f"{type(error).__name__}: {message}",
    }


def _build_result_manifest(
    unit: Mapping[str, Any], response: Mapping[str, Any], executed_at: str
) -> dict[str, Any]:
    raw_records = sorted(
        (_raw_result_record(result) for result in response["results"]),
        key=lambda record: record["sourcegraph_result_id"],
    )
    reasons = _invalid_reasons(response)
    if response.get("execution_error"):
        reasons.append("execution_error")
    document = {
        "result_manifest_version": RESULT_MANIFEST_VERSION,
        **{key: unit[key] for key in unit if key != "unit_id"},
        "unit_id": unit["unit_id"],
        "executed_at": executed_at,
        "sourcegraph_result_ids": [
            record["sourcegraph_result_id"] for record in raw_records
        ],
        "result_count": response["result_count"],
        "result_object_count": len(raw_records),
        "limit_hit": response["limit_hit"],
        "cloning_repositories": response["cloning_repositories"],
        "missing_repositories": response["missing_repositories"],
        "timed_out_repositories": response["timed_out_repositories"],
        "valid": not reasons,
        "invalid_reasons": reasons,
        "raw_results": raw_records,
        "outcomes_consulted": False,
    }
    if response.get("execution_error"):
        document["execution_error"] = response["execution_error"]
    return {
        **document,
        "result_manifest_sha256": result_manifest_sha256(document),
    }


def _query_scope_errors(document: Mapping[str, Any]) -> list[str]:
    query = document.get("rendered_query")
    if not isinstance(query, str):
        return ["rendered_query must be a string"]
    errors = []
    if hashlib.sha256(query.encode()).hexdigest() != document.get(
        "rendered_query_sha256"
    ):
        errors.append("rendered_query_sha256 does not match")
    sourcegraph_name = document.get("sourcegraph_name")
    if not isinstance(sourcegraph_name, str):
        errors.append("sourcegraph_name must be a string")
    elif f"repo:^{_escape_repository_name(sourcegraph_name)}$" not in query:
        errors.append("rendered_query does not pin sourcegraph_name")
    cutoff_commit = document.get("cutoff_commit")
    if not isinstance(cutoff_commit, str) or not SHA1_PATTERN.fullmatch(cutoff_commit):
        errors.append("cutoff_commit must be a 40-character SHA-1")
    elif cutoff_commit not in query:
        errors.append("rendered_query does not pin cutoff_commit")
    return errors


def _raw_result_errors(document: Mapping[str, Any]) -> list[str]:
    raw_results = document.get("raw_results")
    if not isinstance(raw_results, list):
        return ["raw_results must be a list"]
    expected = []
    errors = []
    for record in raw_results:
        if not isinstance(record, Mapping) or not isinstance(
            record.get("payload"), Mapping
        ):
            errors.append("raw result records must contain an object payload")
            continue
        expected_record = _raw_result_record(record["payload"])
        if (
            record.get("sourcegraph_result_id")
            != expected_record["sourcegraph_result_id"]
        ):
            errors.append("sourcegraph result ID does not match raw payload")
        try:
            _validate_raw_result(
                record["payload"],
                document.get("result_type"),
                document.get("sourcegraph_name"),
                document.get("cutoff_commit"),
            )
        except ValueError as error:
            errors.append(f"raw result payload is invalid: {error}")
        expected.append(expected_record["sourcegraph_result_id"])
    if document.get("sourcegraph_result_ids") != sorted(expected):
        errors.append("sourcegraph_result_ids do not match raw results")
    if document.get("result_object_count") != len(raw_results):
        errors.append("result_object_count does not match raw results")
    count = document.get("result_count")
    if (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count < len(raw_results)
    ):
        errors.append("result_count cannot be smaller than raw result count")
    return errors


def _validity_errors(document: Mapping[str, Any]) -> list[str]:
    expected_reasons = _invalid_reasons(document)
    if document.get("execution_error"):
        expected_reasons.append("execution_error")
    errors = []
    if document.get("invalid_reasons") != expected_reasons:
        errors.append("invalid_reasons do not match execution state")
    if document.get("valid") is not (not expected_reasons):
        errors.append("valid does not match execution state")
    count = document.get("result_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        errors.append("result_count must be a non-negative integer")
    return errors


def validate_result_manifest(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    errors = [
        f"missing required field: {field}"
        for field in sorted(REQUIRED_RESULT_FIELDS - document.keys())
    ]
    if document.get("result_manifest_version") != RESULT_MANIFEST_VERSION:
        errors.append("result_manifest_version must equal 3")
    if document.get("outcomes_consulted") is not False:
        errors.append("result manifest must be outcome blind")
    if document.get("result_manifest_sha256") != result_manifest_sha256(document):
        errors.append("result_manifest_sha256 does not match")
    family_by_id = {
        family["id"]: family for family in specification.get("query_families", [])
    }
    family_id = document.get("query_family_id")
    family = family_by_id.get(family_id) if isinstance(family_id, str) else None
    if family is None:
        errors.append("query_family_id is not frozen")
    elif document.get("result_type") != family.get("result_type"):
        errors.append("result_type does not match query family")
    errors.extend(_query_scope_errors(document))
    errors.extend(_raw_result_errors(document))
    errors.extend(_validity_errors(document))
    return errors


def shard_path_for(unit: Mapping[str, Any]) -> Path:
    family_id = unit.get("query_family_id")
    unit_id = unit.get("unit_id")
    if not isinstance(family_id, str) or not FAMILY_ID_PATTERN.fullmatch(family_id):
        raise ValueError("unsafe query family ID in shard reference")
    if not isinstance(unit_id, str) or not SHA256_PATTERN.fullmatch(unit_id):
        raise ValueError("unsafe unit ID in shard reference")
    return Path("shards") / family_id / f"{unit_id}.json"


def _matches_unit(document: Mapping[str, Any], unit: Mapping[str, Any]) -> bool:
    return all(document.get(key) == value for key, value in unit.items())


def _load_reusable_shard(
    path: Path,
    unit: Mapping[str, Any],
    specification: Mapping[str, Any],
    *,
    retry_execution_errors_only: bool,
    reuse_terminal_failures: bool,
) -> Mapping[str, Any] | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping):
        return None
    if not _matches_unit(document, unit):
        return None
    if validate_result_manifest(document, specification):
        return None
    if not document.get("valid"):
        invalid_reasons = document["invalid_reasons"]
        if reuse_terminal_failures and invalid_reasons in (
            ["timed_out_repositories"],
            ["execution_error"],
        ):
            return document
        if not retry_execution_errors_only or "execution_error" in invalid_reasons:
            return None
    return document


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def execute_discovery_unit(
    unit: Mapping[str, Any],
    path: Path,
    specification: Mapping[str, Any],
    query_runner: QueryRunner,
    clock: Clock,
    *,
    retry_execution_errors_only: bool = False,
    reuse_terminal_failures: bool = False,
) -> tuple[Mapping[str, Any], bool]:
    reusable = _load_reusable_shard(
        path,
        unit,
        specification,
        retry_execution_errors_only=retry_execution_errors_only,
        reuse_terminal_failures=reuse_terminal_failures,
    )
    if reusable is not None:
        return reusable, True
    try:
        raw_response = _require_mapping(
            query_runner(
                unit["rendered_query"],
                unit["result_type"],
                unit["sourcegraph_name"],
                unit["cutoff_commit"],
            ),
            "query runner response",
        )
        response = _normalize_response(
            raw_response,
            unit["result_type"],
            unit["sourcegraph_name"],
            unit["cutoff_commit"],
        )
    except Exception as error:
        response = _failed_response(error)
    shard = _build_result_manifest(unit, response, _utc_timestamp(clock()))
    atomic_write_json(path, shard)
    return shard, False


def _unit_summary(
    shard: Mapping[str, Any], shard_path: Path, reused: bool
) -> dict[str, Any]:
    return {
        **{field: shard[field] for field in SUMMARY_SHARD_FIELDS},
        "shard_path": shard_path.as_posix(),
        "reused": reused,
    }


def _build_execution_manifest(
    specification: Mapping[str, Any],
    index_manifest_sha256: str,
    index_audit_sha256: str,
    units: list[dict[str, Any]],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    valid_count = sum(unit["valid"] for unit in units)
    reused_count = sum(unit["reused"] for unit in units)
    document = {
        "execution_manifest_version": EXECUTION_MANIFEST_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "index_manifest_sha256": index_manifest_sha256,
        "index_audit_sha256": index_audit_sha256,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": "complete" if valid_count == len(units) else "incomplete",
        "unit_count": len(units),
        "valid_unit_count": valid_count,
        "invalid_unit_count": len(units) - valid_count,
        "executed_unit_count": len(units) - reused_count,
        "reused_unit_count": reused_count,
        "outcomes_consulted": False,
        "units": units,
    }
    return {
        **document,
        "execution_manifest_sha256": execution_manifest_sha256(document),
    }


def execute_discovery(
    specification: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    index_audit: Mapping[str, Any],
    output_directory: Path,
    *,
    index_manifest_sha256: str,
    index_audit_sha256: str,
    repository_exclusion_source: bytes | None = None,
    query_runner: QueryRunner = run_sourcegraph_query,
    clock: Clock = lambda: datetime.now(timezone.utc),
    retry_execution_errors_only: bool = False,
) -> dict[str, Any]:
    units = build_execution_units(
        specification,
        index_manifest,
        index_audit,
        index_manifest_sha256=index_manifest_sha256,
        repository_exclusion_source=repository_exclusion_source,
    )
    started_at = _utc_timestamp(clock())
    summaries = []
    for unit in units:
        relative_path = shard_path_for(unit)
        shard, reused = execute_discovery_unit(
            unit,
            output_directory / relative_path,
            specification,
            query_runner,
            clock,
            retry_execution_errors_only=retry_execution_errors_only,
        )
        summaries.append(_unit_summary(shard, relative_path, reused))
    manifest = _build_execution_manifest(
        specification,
        index_manifest_sha256,
        index_audit_sha256,
        summaries,
        started_at,
        _utc_timestamp(clock()),
    )
    atomic_write_json(
        output_directory / "sourcegraph-discovery-execution.v3.json", manifest
    )
    return manifest
