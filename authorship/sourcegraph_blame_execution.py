"""Cutoff-pinned Sourcegraph blame enrichment for discovery file matches."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.sg import api
from authorship.sourcegraph_discovery_execution import atomic_write_json
from authorship.sourcegraph_evidence_pipeline import packet_index_sha256

BLAME_PLAN_VERSION = 3
BLAME_EXECUTION_VERSION = 3
BLAME_SHARD_VERSION = 3
DEFAULT_BATCH_SIZE = 15
MAX_BATCH_SIZE = 15
DEFAULT_MAX_WORKERS = 4
SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RESULT_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
Clock = Callable[[], datetime]
ApiRunner = Callable[[str], Mapping[str, Any]]
SUMMARY_FIELDS = frozenset(
    {
        "group_id",
        "blame_shard_sha256",
        "valid",
        "error",
        "enrichment_count",
        "shard_path",
    }
)
SHARD_FIELDS = frozenset(
    {
        "blame_shard_version",
        "group_id",
        "canonical_repository_id",
        "sourcegraph_name",
        "cutoff_commit",
        "path",
        "requested_lines",
        "blame_api_lines",
        "pending_enrichments",
        "executed_at",
        "raw_blame_response",
        "enrichments",
        "valid",
        "error",
        "outcomes_consulted",
        "blame_shard_sha256",
    }
)
EXECUTION_FIELDS = frozenset(
    {
        "blame_execution_version",
        "blame_plan_sha256",
        "packet_index_sha256",
        "status",
        "group_count",
        "valid_group_count",
        "invalid_group_count",
        "enrichment_count",
        "precise_code_intelligence_used",
        "scip_used",
        "outcomes_consulted",
        "groups",
        "blame_execution_sha256",
    }
)


class BlameExecutionError(ValueError):
    """Blame inputs or persisted outputs violate the frozen contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _document_sha256(document: Mapping[str, Any], field: str) -> str:
    return _sha256({key: value for key, value in document.items() if key != field})


def blame_plan_sha256(document: Mapping[str, Any]) -> str:
    return _document_sha256(document, "blame_plan_sha256")


def blame_execution_sha256(document: Mapping[str, Any]) -> str:
    return _document_sha256(document, "blame_execution_sha256")


def _blame_shard_sha256(document: Mapping[str, Any]) -> str:
    return _document_sha256(document, "blame_shard_sha256")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BlameExecutionError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_string(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise BlameExecutionError(f"{field} must be a non-empty string")
    return value


def _validated_pending(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise BlameExecutionError("pending enrichment must be an object")
    manifest_sha = _required_string(record, "result_manifest_sha256")
    result_id = _required_string(record, "sourcegraph_result_id")
    cutoff = _required_string(record, "cutoff_commit")
    if not SHA256_PATTERN.fullmatch(manifest_sha):
        raise BlameExecutionError("result_manifest_sha256 is invalid")
    if not RESULT_ID_PATTERN.fullmatch(result_id):
        raise BlameExecutionError("sourcegraph_result_id is invalid")
    if not SHA1_PATTERN.fullmatch(cutoff):
        raise BlameExecutionError("cutoff_commit is invalid")
    lines = record.get("sourcegraph_line_numbers")
    if (
        not isinstance(lines, list)
        or any(
            isinstance(line, bool) or not isinstance(line, int) or line < 0
            for line in lines
        )
        or lines != sorted(set(lines))
    ):
        raise BlameExecutionError(
            "sourcegraph_line_numbers must be sorted unique integers"
        )
    if record.get("required_enrichment") != "cutoff_pinned_blame":
        raise BlameExecutionError("pending enrichment requirement is invalid")
    return {
        "result_manifest_sha256": manifest_sha,
        "sourcegraph_result_id": result_id,
        "canonical_repository_id": _required_string(record, "canonical_repository_id"),
        "sourcegraph_name": _required_string(record, "sourcegraph_name"),
        "cutoff_commit": cutoff,
        "path": _required_string(record, "path"),
        "sourcegraph_line_numbers": lines,
    }


def _group_pending(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    seen_bindings: set[tuple[str, str]] = set()
    for raw_record in records:
        record = _validated_pending(raw_record)
        binding = (
            record["result_manifest_sha256"],
            record["sourcegraph_result_id"],
        )
        if binding in seen_bindings:
            raise BlameExecutionError("pending enrichment bindings must be unique")
        seen_bindings.add(binding)
        key = (
            record["canonical_repository_id"],
            record["sourcegraph_name"],
            record["cutoff_commit"],
            record["path"],
        )
        grouped[key].append(record)
    groups = []
    for key, members in grouped.items():
        ordered = sorted(
            members,
            key=lambda item: (
                item["result_manifest_sha256"],
                item["sourcegraph_result_id"],
            ),
        )
        content = {
            "canonical_repository_id": key[0],
            "sourcegraph_name": key[1],
            "cutoff_commit": key[2],
            "path": key[3],
            "requested_lines": sorted(
                {
                    line
                    for member in ordered
                    for line in member["sourcegraph_line_numbers"]
                }
            ),
            "pending_enrichments": ordered,
        }
        content["blame_api_lines"] = [line + 1 for line in content["requested_lines"]]
        groups.append({**content, "group_id": _sha256(content)})
    return sorted(groups, key=lambda item: item["group_id"])


def build_blame_plan(packet_index: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze cutoff/path groups from a preliminary evidence packet index."""
    packet_sha = packet_index.get("packet_index_sha256")
    if not isinstance(packet_sha, str) or not SHA256_PATTERN.fullmatch(packet_sha):
        raise BlameExecutionError("packet index SHA-256 is invalid")
    if packet_sha != packet_index_sha256(packet_index):
        raise BlameExecutionError("packet index SHA-256 does not match")
    pending = packet_index.get("pending_file_enrichments")
    if not isinstance(pending, list):
        raise BlameExecutionError("pending_file_enrichments must be a list")
    if packet_index.get("pending_file_enrichment_count") != len(pending):
        raise BlameExecutionError("pending enrichment count does not match")
    groups = _group_pending(pending)
    document = {
        "blame_plan_version": BLAME_PLAN_VERSION,
        "status": "frozen_before_blame_execution",
        "packet_index_sha256": packet_sha,
        "group_count": len(groups),
        "query_group_count": sum(bool(group["requested_lines"]) for group in groups),
        "pending_file_enrichment_count": len(pending),
        "path_only_enrichment_count": sum(
            not record["sourcegraph_line_numbers"]
            for group in groups
            for record in group["pending_enrichments"]
        ),
        "line_number_basis": "zero_based_search_one_based_blame_api",
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "outcomes_consulted": False,
        "groups": groups,
    }
    return {**document, "blame_plan_sha256": blame_plan_sha256(document)}


def _blame_field(alias: str, group: Mapping[str, Any]) -> str:
    lines = group["blame_api_lines"]
    return (
        f"{alias}:repository(name:{json.dumps(group['sourcegraph_name'])})"
        "{name "
        f"commit(rev:{json.dumps(group['cutoff_commit'])})"
        "{oid "
        f"blob(path:{json.dumps(group['path'])})"
        "{path "
        f"blame(startLine:{min(lines)},endLine:{max(lines)})"
        "{startLine endLine commit{oid author{date} committer{date}}}"
        "}"
        "}"
        "}"
    )


def _batch_query(groups: Sequence[Mapping[str, Any]]) -> str:
    selections = [
        _blame_field(f"b{index}", group) for index, group in enumerate(groups)
    ]
    return "query DiscoveryBlameBatch{\n" + "\n".join(selections) + "\n}"


def _commit_timestamp(commit: Mapping[str, Any]) -> str:
    for field in ("committer", "author"):
        signature = commit.get(field)
        value = signature.get("date") if isinstance(signature, Mapping) else None
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return value
    raise BlameExecutionError("blame commit has no valid timestamp")


def _line_attribution(
    response: Any, group: Mapping[str, Any]
) -> dict[int, tuple[str, str]]:
    if not isinstance(response, Mapping):
        raise BlameExecutionError("blame repository response must be an object")
    if response.get("name") != group["sourcegraph_name"]:
        raise BlameExecutionError("blame repository does not match")
    commit = response.get("commit")
    if not isinstance(commit, Mapping) or commit.get("oid") != group["cutoff_commit"]:
        raise BlameExecutionError("blame revision does not match cutoff")
    blob = commit.get("blob")
    if not isinstance(blob, Mapping) or blob.get("path") != group["path"]:
        raise BlameExecutionError("blame path does not match")
    ranges = blob.get("blame")
    if not isinstance(ranges, list):
        raise BlameExecutionError("blame ranges must be a list")
    attribution = {}
    for line, blame_api_line in zip(
        group["requested_lines"], group["blame_api_lines"], strict=True
    ):
        matches = [
            item
            for item in ranges
            if isinstance(item, Mapping)
            and isinstance(item.get("startLine"), int)
            and isinstance(item.get("endLine"), int)
            and item["startLine"] <= blame_api_line < item["endLine"]
        ]
        if len(matches) != 1:
            raise BlameExecutionError(
                f"blame does not uniquely cover requested line {line}"
            )
        blame_commit = matches[0].get("commit")
        if not isinstance(blame_commit, Mapping) or not SHA1_PATTERN.fullmatch(
            str(blame_commit.get("oid", ""))
        ):
            raise BlameExecutionError("blame commit OID is invalid")
        attribution[line] = (
            blame_commit["oid"],
            _commit_timestamp(blame_commit),
        )
    return attribution


def _enrichments(
    group: Mapping[str, Any], attribution: Mapping[int, tuple[str, str]]
) -> list[dict[str, Any]]:
    return [
        {
            "result_manifest_sha256": record["result_manifest_sha256"],
            "sourcegraph_result_id": record["sourcegraph_result_id"],
            "indexed_revision_oid": group["cutoff_commit"],
            "default_branch_reachable": True,
            "lines": [
                {
                    "line": line,
                    "commit_oid": attribution[line][0],
                    "observed_at": attribution[line][1],
                }
                for line in record["sourcegraph_line_numbers"]
            ],
        }
        for record in group["pending_enrichments"]
    ]


def _shard_document(
    group: Mapping[str, Any],
    *,
    executed_at: str,
    response: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    valid = error is None
    attribution = (
        _line_attribution(response, group) if valid and group["requested_lines"] else {}
    )
    document = {
        "blame_shard_version": BLAME_SHARD_VERSION,
        "group_id": group["group_id"],
        "canonical_repository_id": group["canonical_repository_id"],
        "sourcegraph_name": group["sourcegraph_name"],
        "cutoff_commit": group["cutoff_commit"],
        "path": group["path"],
        "requested_lines": group["requested_lines"],
        "blame_api_lines": group["blame_api_lines"],
        "pending_enrichments": group["pending_enrichments"],
        "executed_at": executed_at,
        "raw_blame_response": response,
        "enrichments": _enrichments(group, attribution) if valid else [],
        "valid": valid,
        "error": error,
        "outcomes_consulted": False,
    }
    return {**document, "blame_shard_sha256": _blame_shard_sha256(document)}


def _shard_path(group: Mapping[str, Any]) -> Path:
    group_id = group["group_id"]
    return Path("blame-shards") / group_id[:2] / f"{group_id}.json"


def _matches_group(shard: Mapping[str, Any], group: Mapping[str, Any]) -> bool:
    return all(
        shard.get(field) == group.get(field)
        for field in (
            "group_id",
            "canonical_repository_id",
            "sourcegraph_name",
            "cutoff_commit",
            "path",
            "requested_lines",
            "blame_api_lines",
            "pending_enrichments",
        )
    )


def _load_reusable_shard(
    path: Path, group: Mapping[str, Any], output_directory: Path
) -> Mapping[str, Any] | None:
    try:
        resolved = path.resolve()
        resolved.relative_to(output_directory.resolve())
        shard = json.loads(resolved.read_text())
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(shard, Mapping)
        or _shard_errors(shard, group)
        or shard.get("valid") is not True
    ):
        return None
    return shard


def _summary(shard: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {
        "group_id": shard["group_id"],
        "blame_shard_sha256": shard["blame_shard_sha256"],
        "valid": shard["valid"],
        "error": shard["error"],
        "enrichment_count": len(shard["enrichments"]),
        "shard_path": path.as_posix(),
    }


def _execute_batch(
    groups: Sequence[Mapping[str, Any]],
    output_directory: Path,
    api_runner: ApiRunner,
    clock: Clock,
) -> list[dict[str, Any]]:
    if not groups:
        return []
    try:
        response = api_runner(_batch_query(groups))
    except RuntimeError as error:
        if len(groups) > 1:
            midpoint = len(groups) // 2
            return _execute_batch(
                groups[:midpoint], output_directory, api_runner, clock
            ) + _execute_batch(groups[midpoint:], output_directory, api_runner, clock)
        group = groups[0]
        shard = _shard_document(
            group,
            executed_at=_timestamp(clock()),
            error=str(error)[:300],
        )
        path = _shard_path(group)
        atomic_write_json(output_directory / path, shard)
        return [_summary(shard, path)]
    summaries = []
    for index, group in enumerate(groups):
        try:
            alias_response = (
                response.get(f"b{index}") if isinstance(response, Mapping) else None
            )
            shard = _shard_document(
                group,
                executed_at=_timestamp(clock()),
                response=alias_response,
            )
        except BlameExecutionError as error:
            shard = _shard_document(
                group,
                executed_at=_timestamp(clock()),
                response=alias_response,
                error=str(error),
            )
        path = _shard_path(group)
        atomic_write_json(output_directory / path, shard)
        summaries.append(_summary(shard, path))
    return summaries


def execute_blame_plan(
    plan: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    output_directory: Path,
    *,
    api_runner: ApiRunner = api,
    clock: Clock = lambda: datetime.now(timezone.utc),
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """Execute or resume a frozen blame plan without SCIP/precise code intel."""
    expected_plan = build_blame_plan(packet_index)
    if plan != expected_plan:
        raise BlameExecutionError("blame plan does not match packet index")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise BlameExecutionError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
    ):
        raise BlameExecutionError("max_workers must be a positive integer")
    summaries = _execute_groups(
        expected_plan["groups"],
        output_directory,
        api_runner,
        clock,
        batch_size,
        max_workers,
    )
    valid_count = sum(summary["valid"] is True for summary in summaries)
    document = {
        "blame_execution_version": BLAME_EXECUTION_VERSION,
        "blame_plan_sha256": expected_plan["blame_plan_sha256"],
        "packet_index_sha256": expected_plan["packet_index_sha256"],
        "status": "complete" if valid_count == len(summaries) else "incomplete",
        "group_count": len(summaries),
        "valid_group_count": valid_count,
        "invalid_group_count": len(summaries) - valid_count,
        "enrichment_count": sum(summary["enrichment_count"] for summary in summaries),
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "outcomes_consulted": False,
        "groups": summaries,
    }
    execution = {
        **document,
        "blame_execution_sha256": blame_execution_sha256(document),
    }
    atomic_write_json(
        output_directory / "sourcegraph-blame-execution.v3.json", execution
    )
    return execution


def _execute_groups(
    groups: Sequence[Mapping[str, Any]],
    output_directory: Path,
    api_runner: ApiRunner,
    clock: Clock,
    batch_size: int,
    max_workers: int,
) -> list[dict[str, Any]]:
    summaries_by_id = {}
    pending_query_groups = []
    for group in groups:
        path = _shard_path(group)
        reusable = _load_reusable_shard(
            output_directory / path, group, output_directory
        )
        if reusable is not None:
            summaries_by_id[group["group_id"]] = _summary(reusable, path)
        elif group["requested_lines"]:
            pending_query_groups.append(group)
        else:
            shard = _shard_document(group, executed_at=_timestamp(clock()))
            atomic_write_json(output_directory / path, shard)
            summaries_by_id[group["group_id"]] = _summary(shard, path)
    batches = [
        pending_query_groups[offset : offset + batch_size]
        for offset in range(0, len(pending_query_groups), batch_size)
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _execute_batch,
                batch,
                output_directory,
                api_runner,
                clock,
            )
            for batch in batches
        ]
        for future in futures:
            summaries = future.result()
            summaries_by_id.update(
                (summary["group_id"], summary) for summary in summaries
            )
    return [summaries_by_id[group["group_id"]] for group in groups]


def _load_shard_inside(
    output_directory: Path, path: Path
) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        resolved = (output_directory / path).resolve()
        resolved.relative_to(output_directory.resolve())
        document = json.loads(resolved.read_text())
    except ValueError:
        return None, "shard resolves outside output directory"
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, str(error)
    return (
        (document, None)
        if isinstance(document, Mapping)
        else (None, "shard is not an object")
    )


def _shard_errors(shard: Mapping[str, Any], group: Mapping[str, Any]) -> list[str]:
    errors = []
    if set(shard) != SHARD_FIELDS:
        errors.append("blame shard has missing or unexpected fields")
    if shard.get("blame_shard_version") != BLAME_SHARD_VERSION:
        errors.append("blame_shard_version must equal 3")
    if shard.get("blame_shard_sha256") != _blame_shard_sha256(shard):
        errors.append("blame shard SHA-256 does not match")
    if not _matches_group(shard, group):
        errors.append("blame shard does not match group")
    if shard.get("outcomes_consulted") is not False:
        errors.append("blame shard must be outcome blind")
    try:
        datetime.fromisoformat(str(shard.get("executed_at", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("blame shard executed_at is invalid")
    valid = shard.get("valid")
    enrichments = shard.get("enrichments")
    if not isinstance(enrichments, list):
        return [*errors, "blame shard enrichments must be a list"]
    if valid is True:
        if shard.get("error") is not None:
            errors.append("valid blame shard must not contain an error")
        try:
            attribution = (
                _line_attribution(shard.get("raw_blame_response"), group)
                if group["requested_lines"]
                else {}
            )
            expected_enrichments = _enrichments(group, attribution)
        except BlameExecutionError as error:
            errors.append(f"blame shard raw response is invalid: {error}")
        else:
            if enrichments != expected_enrichments:
                errors.append("blame shard enrichments do not match group")
        if not group["requested_lines"] and shard.get("raw_blame_response") is not None:
            errors.append("path-only blame shard must not contain a raw response")
    elif valid is False:
        if not isinstance(shard.get("error"), str) or not shard["error"]:
            errors.append("invalid blame shard must contain an error")
        if enrichments:
            errors.append("invalid blame shard must not contain enrichments")
    else:
        errors.append("blame shard valid must be a boolean")
    return errors


def validate_blame_execution(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    output_directory: Path,
) -> list[str]:
    """Validate an execution and every referenced group shard."""
    errors = []
    try:
        expected_plan = build_blame_plan(packet_index)
    except BlameExecutionError as error:
        return [f"packet index is invalid: {error}"]
    if plan != expected_plan:
        errors.append("blame plan does not match packet index")
    if document.get("blame_execution_version") != BLAME_EXECUTION_VERSION:
        errors.append("blame_execution_version must equal 3")
    if set(document) != EXECUTION_FIELDS:
        errors.append("blame execution has missing or unexpected fields")
    if document.get("blame_execution_sha256") != blame_execution_sha256(document):
        errors.append("blame execution SHA-256 does not match")
    if document.get("blame_plan_sha256") != expected_plan["blame_plan_sha256"]:
        errors.append("blame plan SHA-256 does not match")
    if document.get("packet_index_sha256") != expected_plan["packet_index_sha256"]:
        errors.append("packet index SHA-256 does not match")
    summaries = document.get("groups")
    if not isinstance(summaries, list) or len(summaries) != len(
        expected_plan["groups"]
    ):
        return [*errors, "blame execution group count does not match"]
    valid_count = 0
    enrichment_count = 0
    for summary, group in zip(summaries, expected_plan["groups"]):
        group_errors, valid, group_enrichments = _validate_group_summary(
            summary, group, output_directory
        )
        errors.extend(group_errors)
        valid_count += valid
        enrichment_count += group_enrichments
    expected_counts = {
        "group_count": len(summaries),
        "valid_group_count": valid_count,
        "invalid_group_count": len(summaries) - valid_count,
        "enrichment_count": enrichment_count,
    }
    if any(document.get(field) != value for field, value in expected_counts.items()):
        errors.append("blame execution counts do not match")
    expected_status = "complete" if valid_count == len(summaries) else "incomplete"
    if document.get("status") != expected_status:
        errors.append("blame execution status does not match")
    if (
        document.get("precise_code_intelligence_used") is not False
        or document.get("scip_used") is not False
        or document.get("outcomes_consulted") is not False
    ):
        errors.append("blame execution capability or outcome flags are invalid")
    return errors


def _validate_group_summary(
    summary: Any,
    group: Mapping[str, Any],
    output_directory: Path,
) -> tuple[list[str], int, int]:
    if not isinstance(summary, Mapping):
        return ["blame group summary must be an object"], 0, 0
    errors = []
    if set(summary) != SUMMARY_FIELDS:
        errors.append("blame group summary has missing or unexpected fields")
    expected_path = _shard_path(group)
    if summary.get("shard_path") != expected_path.as_posix():
        errors.append("blame shard path does not match group")
    shard, load_error = _load_shard_inside(output_directory, expected_path)
    if shard is None:
        return [*errors, f"blame shard is unreadable: {load_error}"], 0, 0
    errors.extend(_shard_errors(shard, group))
    if summary != _summary(shard, expected_path):
        errors.append("blame group summary does not match shard")
    if shard.get("valid") is not True:
        return errors, 0, 0
    return errors, 1, len(shard.get("enrichments", []))


def load_execution_enrichments(
    execution: Mapping[str, Any],
    plan: Mapping[str, Any],
    output_directory: Path,
) -> list[Mapping[str, Any]]:
    """Load enrichments only from a complete, valid execution."""
    if execution.get("status") != "complete":
        raise BlameExecutionError("blame execution is not complete")
    enrichments = []
    for summary, group in zip(execution["groups"], plan["groups"]):
        path = _shard_path(group)
        shard, error = _load_shard_inside(output_directory, path)
        if (
            shard is None
            or summary != _summary(shard, path)
            or _shard_errors(shard, group)
            or not shard.get("valid")
        ):
            raise BlameExecutionError(f"blame shard is invalid: {error or path}")
        enrichments.extend(shard["enrichments"])
    return enrichments
