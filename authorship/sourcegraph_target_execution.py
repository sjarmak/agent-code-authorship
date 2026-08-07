"""Checkpointed Sourcegraph blob-and-blame execution for fixed target units."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from authorship.sg import api
from authorship.sourcegraph_target_exact import (
    materialize_target_file,
    target_file_shard_sha256,
    validate_target_file_shard,
)
from authorship.sourcegraph_target_plan import target_unit_plan_sha256

EXECUTION_VERSION = 1
BLAME_PAGE_LINES = 5000
ApiRunner = Callable[..., Mapping[str, Any]]
TaskRunner = Callable[[Mapping[str, Any]], Mapping[str, Any]]

TREE_QUERY = """
query TargetTree($repo:String!,$rev:String!) {
  repository(name:$repo) {
    commit(rev:$rev) {
      oid
      tree(path:"") { files(recursive:true) { path } }
    }
  }
}
""".strip()

INITIAL_QUERY = """
query TargetBlobInitial($repo:String!,$rev:String!,$path:String!) {
  repository(name:$repo) {
    name
    commit(rev:$rev) {
      oid
      blob(path:$path) {
        path content
        blame(startLine:1,endLine:5000) {
          startLine endLine
          commit { oid author { date } committer { date } }
        }
      }
    }
  }
}
""".strip()


class TargetExecutionError(RuntimeError):
    """Raised when the frozen target extraction cannot be reproduced."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def target_execution_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "target_execution_sha256"
    }
    return _sha256(content)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(_canonical_json(document))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fetch_tree_files(
    sourcegraph_name: str,
    revision: str,
    *,
    api_runner: ApiRunner = api,
) -> dict[str, Any]:
    data = api_runner(TREE_QUERY, repo=sourcegraph_name, rev=revision)
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    tree = commit.get("tree") if isinstance(commit, Mapping) else None
    files = tree.get("files") if isinstance(tree, Mapping) else None
    if not isinstance(commit, Mapping) or not isinstance(tree, Mapping):
        raise RuntimeError("indexed target revision or tree is unavailable")
    if not isinstance(files, list):
        raise RuntimeError("indexed target tree file list is unavailable")
    return {
        "oid": commit.get("oid"),
        "paths": sorted(
            row["path"]
            for row in files
            if isinstance(row, Mapping) and isinstance(row.get("path"), str)
        ),
    }


def _page_query(start_line: int, end_line: int) -> str:
    return f"""
query TargetBlamePage($repo:String!,$rev:String!,$path:String!) {{
  repository(name:$repo) {{
    commit(rev:$rev) {{
      oid
      blob(path:$path) {{
        path
        blame(startLine:{start_line},endLine:{end_line}) {{
          startLine endLine
          commit {{ oid author {{ date }} committer {{ date }} }}
        }}
      }}
    }}
  }}
}}
""".strip()


def _blob(data: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    blob = commit.get("blob") if isinstance(commit, Mapping) else None
    if not isinstance(commit, Mapping) or not isinstance(blob, Mapping):
        raise TargetExecutionError("target blob is unavailable at the pinned revision")
    return commit, blob


def _merge_blame(hunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for hunk in sorted(hunks, key=lambda row: row.get("startLine", 0)):
        current = dict(hunk)
        if (
            merged
            and merged[-1].get("endLine", 0) == current.get("startLine")
            and _canonical_json(merged[-1].get("commit"))
            == _canonical_json(current.get("commit"))
        ):
            merged[-1] = {**merged[-1], "endLine": current.get("endLine")}
        else:
            merged.append(current)
    return merged


def fetch_target_file(
    task: Mapping[str, Any],
    *,
    api_runner: ApiRunner = api,
) -> dict[str, Any]:
    variables = {
        "repo": task["sourcegraph_name"],
        "rev": task["cutoff_commit"],
        "path": task["path"],
    }
    data = api_runner(INITIAL_QUERY, **variables)
    repository = data.get("repository")
    commit, blob = _blob(data)
    content = blob.get("content")
    if not isinstance(content, str):
        raise TargetExecutionError("target blob content is missing")
    line_count = len(content.splitlines()) if content else 0
    hunks = [dict(row) for row in (blob.get("blame") or [])]
    for start in range(BLAME_PAGE_LINES + 1, line_count + 1, BLAME_PAGE_LINES):
        end = min(start + BLAME_PAGE_LINES - 1, line_count)
        page_commit, page_blob = _blob(api_runner(_page_query(start, end), **variables))
        if page_commit.get("oid") != commit.get("oid"):
            raise TargetExecutionError("target revision drifted between blame pages")
        hunks.extend(dict(row) for row in (page_blob.get("blame") or []))
    return {
        "repository_name": (
            repository.get("name") if isinstance(repository, Mapping) else None
        ),
        "commit_oid": commit.get("oid"),
        "path": blob.get("path"),
        "content": content,
        "blame": _merge_blame(hunks),
    }


def _shard_relative_path(task: Mapping[str, Any]) -> Path:
    return Path("shards") / f"{task['task_sha256']}.json"


def _load_reusable(path: Path, task: Mapping[str, Any]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        shard = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if shard.get("status") != "success":
        return None
    return shard if not validate_target_file_shard(shard, task) else None


def _failure_shard(task: Mapping[str, Any], error: Exception) -> dict[str, Any]:
    document = {
        "target_file_shard_version": 1,
        "status": "failure",
        "task_sha256": task.get("task_sha256"),
        "repository_id": task.get("repository_id"),
        "path": task.get("path"),
        "error": str(error)[:300],
        "units": [],
        "retained_unit_count": 0,
    }
    return {**document, "target_file_shard_sha256": target_file_shard_sha256(document)}


def _run_task(
    task: Mapping[str, Any], output: Path, runner: TaskRunner
) -> dict[str, Any]:
    relative = _shard_relative_path(task)
    absolute = output / relative
    shard = _load_reusable(absolute, task)
    if shard is None:
        try:
            shard = materialize_target_file(task, runner(task))
        except Exception as error:  # external trust boundary; preserved in the shard
            shard = _failure_shard(task, error)
        atomic_write_json(absolute, shard)
    return {
        "task_sha256": task["task_sha256"],
        "repository_id": task["repository_id"],
        "path": task["path"],
        "status": shard["status"],
        "shard_path": relative.as_posix(),
        "file_sha256": _file_sha256(absolute),
        "unit_count": shard.get("retained_unit_count", 0),
        **({"error": shard["error"]} if shard["status"] == "failure" else {}),
    }


def execute_target_unit_plan(
    plan: Mapping[str, Any],
    output_directory: Path,
    task_runner: TaskRunner = fetch_target_file,
    *,
    workers: int = 8,
) -> dict[str, Any]:
    if plan.get("status") != "frozen_before_target_outcome_extraction":
        raise TargetExecutionError("target unit plan is not execution-ready")
    if plan.get("target_unit_plan_sha256") != target_unit_plan_sha256(plan):
        raise TargetExecutionError("target unit plan SHA-256 does not match")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or plan.get("file_count") != len(tasks):
        raise TargetExecutionError("target plan tasks do not match file count")
    if workers < 1:
        raise TargetExecutionError("workers must be positive")
    references = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_task, task, output_directory, task_runner): task
            for task in tasks
        }
        for future in as_completed(futures):
            references.append(future.result())
    references.sort(key=lambda row: row["task_sha256"])
    success = sum(row["status"] == "success" for row in references)
    document = {
        "target_execution_version": EXECUTION_VERSION,
        "status": "complete" if success == len(tasks) else "incomplete",
        "target_unit_plan_sha256": plan["target_unit_plan_sha256"],
        "task_count": len(tasks),
        "success_count": success,
        "failure_count": len(tasks) - success,
        "unit_count": sum(row["unit_count"] for row in references),
        "references": references,
    }
    return {**document, "target_execution_sha256": target_execution_sha256(document)}


def _reference_errors(
    task: Mapping[str, Any], reference: Mapping[str, Any], root: Path
) -> list[str]:
    errors = []
    relative = Path(str(reference.get("shard_path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        return ["target shard path is unsafe"]
    path = root / relative
    if not path.is_file():
        return [f"target shard is missing: {relative}"]
    if reference.get("file_sha256") != _file_sha256(path):
        errors.append(f"target shard file SHA-256 does not match: {relative}")
        return errors
    try:
        shard = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return [f"target shard is not valid JSON: {relative}"]
    if reference.get("status") == "success":
        errors.extend(validate_target_file_shard(shard, task))
    elif shard.get("status") != "failure":
        errors.append(f"target failure shard status does not match: {relative}")
    return errors


def validate_target_execution(
    plan: Mapping[str, Any],
    execution: Mapping[str, Any],
    output_directory: Path,
) -> list[str]:
    errors = []
    if execution.get("target_execution_sha256") != target_execution_sha256(execution):
        errors.append("target execution SHA-256 does not match")
    tasks = {
        task["task_sha256"]: task
        for task in plan.get("tasks", [])
        if isinstance(task, Mapping) and isinstance(task.get("task_sha256"), str)
    }
    references = execution.get("references")
    if not isinstance(references, list):
        return [*errors, "target execution references must be a list"]
    reference_ids = [row.get("task_sha256") for row in references]
    if set(reference_ids) != set(tasks) or len(reference_ids) != len(tasks):
        errors.append("target execution does not contain the exact task set")
    for reference in references:
        task = tasks.get(reference.get("task_sha256"))
        if task is not None:
            errors.extend(_reference_errors(task, reference, output_directory))
    success = sum(row.get("status") == "success" for row in references)
    units = sum(row.get("unit_count", 0) for row in references)
    expected = {
        "task_count": len(tasks),
        "success_count": success,
        "failure_count": len(references) - success,
        "unit_count": units,
        "status": "complete" if success == len(tasks) else "incomplete",
        "target_unit_plan_sha256": plan.get("target_unit_plan_sha256"),
    }
    for field, value in expected.items():
        if execution.get(field) != value:
            errors.append(f"target execution {field} does not match")
    referenced = {str(row.get("shard_path")) for row in references}
    on_disk = {
        path.relative_to(output_directory).as_posix()
        for path in (output_directory / "shards").glob("*.json")
    }
    if referenced != on_disk:
        errors.append("target execution shard inventory does not match references")
    return errors
