"""Checkpointed exact-diff execution for Sourcegraph authorship units."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from authorship.sourcegraph_authorship_exact import (
    exact_hunk_units,
    fetch_commit_metadata,
    fetch_exact_commit_records,
    fetch_root_commit_records,
)

SHARD_VERSION = 1
EXECUTION_VERSION = 1
MetadataFetcher = Callable[..., dict[str, Any]]
RecordsFetcher = Callable[..., list[dict[str, Any]]]
RootRecordsFetcher = Callable[..., list[dict[str, Any]]]
TaskRunner = Callable[[dict[str, Any]], dict[str, Any]]


class AuthorshipExecutionError(RuntimeError):
    """Raised when an exact extraction task cannot be reproduced."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def extraction_shard_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key not in {"extraction_shard_sha256", "reused"}
    }
    return _sha256(content)


def _task_content(row: Mapping[str, Any]) -> dict[str, Any]:
    committed_at = row.get("committed_at") or row.get("observed_at")
    content = {
        "repository_id": row.get("repository_id"),
        "sourcegraph_name": row.get("sourcegraph_name"),
        "commit_oid": row.get("commit_oid"),
        "committed_at": committed_at,
        "language": row.get("language"),
        "authorship_role": row.get("authorship_role"),
        "evidence_tier": row.get("evidence_tier"),
        "resolve_commit_metadata": row.get("resolve_commit_metadata") is True,
    }
    if not content["resolve_commit_metadata"]:
        content.update(
            {
                "first_parent_oid": row.get("first_parent_oid"),
                "parent_count": row.get("parent_count"),
                "is_root_commit": row.get("is_root_commit"),
            }
        )
    for field in ("authorship_unit_plan_sha256", "candidate_manifest_sha256"):
        if row.get(field) is not None:
            content[field] = row[field]
    return content


def build_extraction_task(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build one content-addressed commit-language extraction task."""
    content = _task_content(row)
    required = (
        "repository_id",
        "sourcegraph_name",
        "commit_oid",
        "committed_at",
        "language",
        "authorship_role",
        "evidence_tier",
    )
    if any(not content.get(field) for field in required):
        raise AuthorshipExecutionError("extraction task identity is incomplete")
    if content["language"] not in {"Go", "Python"}:
        raise AuthorshipExecutionError("extraction task language is invalid")
    if content["authorship_role"] not in {"agent", "human"}:
        raise AuthorshipExecutionError("extraction task role is invalid")
    if not content["resolve_commit_metadata"] and not content.get("first_parent_oid"):
        raise AuthorshipExecutionError("extraction task diff base is missing")
    return {**content, "task_id": _sha256(content)}


def _agent_tasks(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = plan.get("agent_commits")
    if not isinstance(rows, list):
        raise AuthorshipExecutionError("agent plan rows are invalid")
    return [
        build_extraction_task(
            {
                **row,
                "language": language,
                "resolve_commit_metadata": True,
                "authorship_unit_plan_sha256": plan.get("authorship_unit_plan_sha256"),
            }
        )
        for row in rows
        for language in row["languages"]
    ]


def _h2_tasks(
    plan: Mapping[str, Any], candidates: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = candidates.get("commits")
    if not isinstance(rows, list):
        raise AuthorshipExecutionError("candidate manifest rows are invalid")
    return [
        build_extraction_task(
            {
                **row,
                "language": language,
                "resolve_commit_metadata": False,
                "authorship_unit_plan_sha256": plan.get("authorship_unit_plan_sha256"),
                "candidate_manifest_sha256": candidates.get(
                    "candidate_manifest_sha256"
                ),
            }
        )
        for row in rows
        if row.get("evidence_tier") == "H2_policy_human"
        for language in row["languages"]
    ]


def build_fixed_tasks(
    plan: Mapping[str, Any], candidates: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Build all non-adaptive agent and H2 extraction tasks."""
    tasks = [*_agent_tasks(plan), *_h2_tasks(plan, candidates)]
    ids = [task["task_id"] for task in tasks]
    if len(ids) != len(set(ids)):
        raise AuthorshipExecutionError("fixed extraction tasks are duplicated")
    return sorted(
        tasks,
        key=lambda task: (
            0 if task["authorship_role"] == "agent" else 1,
            task["repository_id"],
            task["commit_oid"],
            task["language"],
        ),
    )


def _metadata(task: Mapping[str, Any], fetcher: MetadataFetcher) -> dict[str, Any]:
    if task["resolve_commit_metadata"]:
        metadata = fetcher(task["sourcegraph_name"], task["commit_oid"])
    else:
        metadata = {
            key: task[key]
            for key in (
                "commit_oid",
                "first_parent_oid",
                "parent_count",
                "is_root_commit",
                "committed_at",
            )
        }
    if metadata.get("committed_at") != task.get("committed_at"):
        raise AuthorshipExecutionError("Sourcegraph commit timestamp drifted")
    if metadata.get("commit_oid") != task.get("commit_oid"):
        raise AuthorshipExecutionError("Sourcegraph commit identity drifted")
    return metadata


def extract_task(
    task: Mapping[str, Any],
    *,
    metadata_fetcher: MetadataFetcher = fetch_commit_metadata,
    records_fetcher: RecordsFetcher = fetch_exact_commit_records,
    root_records_fetcher: RootRecordsFetcher = fetch_root_commit_records,
) -> dict[str, Any]:
    """Execute one exact commit-language task and bind its raw records."""
    metadata = _metadata(task, metadata_fetcher)
    if metadata["is_root_commit"]:
        records = root_records_fetcher(
            task["sourcegraph_name"], task["commit_oid"], task["language"]
        )
    else:
        records = records_fetcher(
            task["sourcegraph_name"],
            metadata["first_parent_oid"],
            task["commit_oid"],
            task["language"],
        )
    units = exact_hunk_units(
        repository_id=task["repository_id"],
        sourcegraph_name=task["sourcegraph_name"],
        commit_oid=task["commit_oid"],
        first_parent_oid=metadata["first_parent_oid"],
        committed_at=metadata["committed_at"],
        language=task["language"],
        records=records,
        authorship_role=task["authorship_role"],
        evidence_tier=task["evidence_tier"],
    )
    document = {
        "extraction_shard_version": SHARD_VERSION,
        "task": dict(task),
        "commit_metadata": metadata,
        "raw_records": records,
        "units": units,
        "counts": {"raw_records": len(records), "units": len(units)},
        "status": "complete",
    }
    return {
        **document,
        "extraction_shard_sha256": extraction_shard_sha256(document),
    }


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _shard_path(root: Path, task: Mapping[str, Any]) -> Path:
    task_id = task["task_id"]
    return root / "shards" / task_id[:2] / f"{task_id}.json"


def _load_valid(path: Path, task: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or validate_extraction_shard(document, task):
        return None
    return dict(document)


def run_task(
    task: dict[str, Any],
    output_root: Path,
    *,
    task_runner: TaskRunner = extract_task,
) -> dict[str, Any]:
    """Reuse a valid shard or atomically replace it with an exact execution."""
    path = _shard_path(output_root, task)
    current = _load_valid(path, task)
    if current is not None:
        return {**current, "reused": True, "shard_path": str(path)}
    document = task_runner(task)
    errors = validate_extraction_shard(document, task)
    if errors:
        raise AuthorshipExecutionError("; ".join(errors))
    _atomic_json(path, document)
    return {**document, "reused": False, "shard_path": str(path)}


def _stratum(unit: Mapping[str, Any]) -> tuple[str, str, int]:
    language, path_type, age = (
        unit.get("language"),
        unit.get("path_type"),
        unit.get("code_age_days"),
    )
    if (
        language not in {"Go", "Python"}
        or path_type not in {"source", "test"}
        or not isinstance(age, int)
    ):
        raise AuthorshipExecutionError("unit matching stratum is invalid")
    return language, path_type, age


def _capacity_document(counter: Counter[tuple[str, str, int]]) -> dict[str, int]:
    return {
        f"{language}|{path_type}|{age}": count
        for (language, path_type, age), count in sorted(counter.items())
    }


def _capacity_satisfied(
    required: Counter[tuple[str, str, int]],
    observed: Counter[tuple[str, str, int]],
) -> bool:
    return all(observed[stratum] >= count for stratum, count in required.items())


def _h3_task(candidate: Mapping[str, Any], language: str) -> dict[str, Any]:
    return build_extraction_task(
        {
            **candidate,
            "language": language,
            "resolve_commit_metadata": False,
        }
    )


def scan_h3_repository(
    repository_id: str,
    candidates: Sequence[Mapping[str, Any]],
    agent_units: Sequence[Mapping[str, Any]],
    *,
    task_runner: TaskRunner,
) -> dict[str, Any]:
    """Scan backward only until exact without-replacement stratum capacity exists."""
    relevant_agents = [
        unit for unit in agent_units if unit.get("repository_id") == repository_id
    ]
    required = Counter(_stratum(unit) for unit in relevant_agents)
    observed: Counter[tuple[str, str, int]] = Counter()
    units, shards = [], []
    ordered = sorted(
        (
            row
            for row in candidates
            if row.get("repository_id") == repository_id
            and row.get("evidence_tier") == "H3_contemporary_pre_adoption"
        ),
        key=lambda row: (row["committed_at"], row["commit_oid"]),
        reverse=True,
    )
    required_languages = sorted({stratum[0] for stratum in required})
    for candidate in ordered:
        for language in required_languages:
            if language not in candidate["languages"]:
                continue
            shard = task_runner(_h3_task(candidate, language))
            shards.append(shard)
            units.extend(shard["units"])
            observed.update(_stratum(unit) for unit in shard["units"])
            if _capacity_satisfied(required, observed):
                break
        if _capacity_satisfied(required, observed):
            break
    satisfied = _capacity_satisfied(required, observed)
    return {
        "repository_id": repository_id,
        "required_capacity": _capacity_document(required),
        "observed_capacity": _capacity_document(observed),
        "capacity_satisfied": satisfied,
        "exhausted_window": not satisfied,
        "scanned_task_count": len(shards),
        "shards": shards,
        "units": units,
    }


def authorship_execution_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "authorship_execution_sha256"
    }
    return _sha256(content)


def _execute_tasks(
    tasks: Sequence[dict[str, Any]],
    root: Path,
    *,
    max_workers: int,
    task_runner: TaskRunner,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    successes, failures = [], []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {
            executor.submit(run_task, task, root, task_runner=task_runner): task
            for task in tasks
        }
        for future in as_completed(pending):
            task = pending[future]
            try:
                successes.append(future.result())
            except Exception as error:  # external trust boundary
                failures.append(
                    {
                        "task_id": task["task_id"],
                        "repository_id": task["repository_id"],
                        "language": task["language"],
                        "authorship_role": task["authorship_role"],
                        "evidence_tier": task["evidence_tier"],
                        "error": str(error),
                    }
                )
    return sorted(successes, key=lambda row: row["task"]["task_id"]), sorted(
        failures, key=lambda row: row["task_id"]
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _shard_reference(shard: Mapping[str, Any], root: Path) -> dict[str, Any]:
    path = Path(shard["shard_path"])
    relative = path.relative_to(root)
    return {
        "task_id": shard["task"]["task_id"],
        "repository_id": shard["task"]["repository_id"],
        "language": shard["task"]["language"],
        "authorship_role": shard["task"]["authorship_role"],
        "evidence_tier": shard["task"]["evidence_tier"],
        "shard": relative.as_posix(),
        "file_sha256": _file_sha256(path),
        "extraction_shard_sha256": shard["extraction_shard_sha256"],
        "unit_count": shard["counts"]["units"],
    }


def _h3_scan(
    pair: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    agent_units: Sequence[Mapping[str, Any]],
    root: Path,
    task_runner: TaskRunner,
) -> dict[str, Any]:
    return scan_h3_repository(
        pair["repository_id"],
        candidates,
        agent_units,
        task_runner=lambda task: run_task(task, root, task_runner=task_runner),
    )


def _execute_h3(
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
    agent_units: Sequence[Mapping[str, Any]],
    root: Path,
    *,
    max_workers: int,
    task_runner: TaskRunner,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results, failures = [], []
    pairs = plan.get("h3_pairs", [])
    rows = candidates.get("commits", [])
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {
            executor.submit(_h3_scan, pair, rows, agent_units, root, task_runner): pair
            for pair in pairs
        }
        for future in as_completed(pending):
            pair = pending[future]
            try:
                results.append(future.result())
            except Exception as error:  # external trust boundary
                failures.append(
                    {
                        "task_id": None,
                        "repository_id": pair["repository_id"],
                        "language": None,
                        "error": str(error),
                    }
                )
    return sorted(results, key=lambda row: row["repository_id"]), sorted(
        failures, key=lambda row: row["repository_id"]
    )


def _h3_summary(result: Mapping[str, Any], root: Path) -> dict[str, Any]:
    return {
        key: value for key, value in result.items() if key not in {"shards", "units"}
    } | {"shards": [_shard_reference(shard, root) for shard in result["shards"]]}


def _execution_counts(
    fixed_tasks: Sequence[Mapping[str, Any]],
    fixed: Sequence[Mapping[str, Any]],
    h3: Sequence[Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    fixed_units = [unit for shard in fixed for unit in shard["units"]]
    h3_units = [unit for result in h3 for unit in result["units"]]
    h3_tasks = sum(len(result["shards"]) for result in h3)
    return {
        "fixed_tasks": len(fixed_tasks),
        "h3_scanned_tasks": h3_tasks,
        "successful_tasks": len(fixed) + h3_tasks,
        "failed_tasks": len(failures),
        "agent_units": sum(unit["authorship_role"] == "agent" for unit in fixed_units),
        "H2_units": sum(
            unit["evidence_tier"] == "H2_policy_human" for unit in fixed_units
        ),
        "H3_candidate_units": len(h3_units),
    }


def _validate_max_workers(max_workers: int) -> None:
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
    ):
        raise AuthorshipExecutionError("max_workers must be a positive integer")


def execute_authorship_units(
    plan: Mapping[str, Any],
    candidates: Mapping[str, Any],
    output_root: Path,
    *,
    max_workers: int,
    task_runner: TaskRunner = extract_task,
) -> dict[str, Any]:
    """Execute fixed units, then adaptive H3 scans, with resumable shards."""
    _validate_max_workers(max_workers)
    fixed_tasks = build_fixed_tasks(plan, candidates)
    fixed, failures = _execute_tasks(
        fixed_tasks, output_root, max_workers=max_workers, task_runner=task_runner
    )
    agent_units = [
        unit
        for shard in fixed
        for unit in shard["units"]
        if unit["authorship_role"] == "agent"
    ]
    h3 = []
    if not any(row.get("authorship_role") == "agent" for row in failures):
        h3, h3_failures = _execute_h3(
            plan,
            candidates,
            agent_units,
            output_root,
            max_workers=max_workers,
            task_runner=task_runner,
        )
        failures.extend(h3_failures)
    document = {
        "authorship_execution_version": EXECUTION_VERSION,
        "status": "complete" if not failures else "incomplete",
        "authorship_unit_plan_sha256": plan.get("authorship_unit_plan_sha256"),
        "candidate_manifest_sha256": candidates.get("candidate_manifest_sha256"),
        "fixed_shards": [_shard_reference(shard, output_root) for shard in fixed],
        "h3_repositories": [_h3_summary(result, output_root) for result in h3],
        "failures": sorted(
            failures, key=lambda row: (row["repository_id"], str(row["language"]))
        ),
        "counts": _execution_counts(fixed_tasks, fixed, h3, failures),
    }
    return {
        **document,
        "authorship_execution_sha256": authorship_execution_sha256(document),
    }


from authorship.sourcegraph_authorship_execution_validation import (  # noqa: E402
    load_execution_shards as load_execution_shards,
    validate_execution_manifest as validate_execution_manifest,
    validate_extraction_shard as validate_extraction_shard,
)
