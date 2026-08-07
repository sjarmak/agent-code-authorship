"""Approval-scoped, resumable execution of a frozen sg-evals action plan."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from authorship.sg_evals_mirroring import validate_action_plan
from authorship.sg_evals_execution_validation import (
    legacy_sync_argv,
    safe_branch,
)
from authorship.target_manifest import write_manifest

ROUTINE_APPROVAL = "external_mutation"
DESTRUCTIVE_APPROVAL = "destructive_external"
APPROVAL_SCOPES = {
    "routine": ROUTINE_APPROVAL,
    "destructive": DESTRUCTIVE_APPROVAL,
}
NOT_FOUND_FRAGMENT = "could not resolve to a repository"
COMMAND_TIMEOUT_SECONDS = 1800
REPOSITORY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MIRROR_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
ACTION_APPROVAL_POLICY = {
    "create_github_fork": (ROUTINE_APPROVAL, False),
    "create_git_mirror": (ROUTINE_APPROVAL, False),
    "fast_forward_sync": (ROUTINE_APPROVAL, False),
    "replace_standalone_mirror": (DESTRUCTIVE_APPROVAL, True),
}

CommandRunner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]
Clock = Callable[[], str]


class ExecutionError(RuntimeError):
    """Raised when an execution request or repository action is unsafe."""


class CommandExecutionError(ExecutionError):
    """Raised after a command failure, retaining its journal trace."""

    def __init__(self, message: str, traces: Sequence[Mapping[str, Any]]):
        super().__init__(message)
        self.traces = [dict(trace) for trace in traces]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_sha256(document: Mapping[str, Any], excluded: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded}
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def execution_journal_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "journal_sha256")


def select_actions(plan: Mapping[str, Any], scope: str) -> list[Mapping[str, Any]]:
    """Return only actions covered by one explicit approval class."""
    if scope not in APPROVAL_SCOPES:
        raise ExecutionError(f"unknown approval scope: {scope}")
    errors = validate_action_plan(plan, validate_force_isolation=scope == "destructive")
    if errors:
        raise ExecutionError(f"invalid action plan: {'; '.join(errors)}")
    approval_class = APPROVAL_SCOPES[scope]
    selected = [
        action
        for action in plan["repositories"]
        if action["approval_class"] == approval_class
    ]
    if scope == "routine" and any(action["force_required"] for action in selected):
        raise ExecutionError("routine scope contains a force-required action")
    if scope == "destructive" and any(
        not action["force_required"] for action in selected
    ):
        raise ExecutionError("destructive scope contains a non-force action")
    return selected


def execution_preview(plan: Mapping[str, Any], scope: str) -> dict[str, Any]:
    selected = select_actions(plan, scope)
    counts = Counter(action["action"] for action in selected)
    return {
        "execution_version": 3,
        "mode": "dry_run",
        "plan_sha256": plan["plan_sha256"],
        "approval_scope": scope,
        "selected_count": len(selected),
        "action_counts": dict(sorted(counts.items())),
        "canonical_repository_ids": [
            action["canonical_repository_id"] for action in selected
        ],
    }


def execution_run_manifest_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "manifest_sha256")


def _scope_manifest(plan: Mapping[str, Any], scope: str) -> dict[str, Any]:
    preview = execution_preview(plan, scope)
    return {
        "selected_count": preview["selected_count"],
        "action_counts": preview["action_counts"],
        "canonical_repository_ids": preview["canonical_repository_ids"],
    }


def build_execution_run_manifest(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the two disjoint approval scopes derived from one plan digest."""
    document = {
        "execution_version": 3,
        "status": "external_approval_required",
        "outcomes_consulted": False,
        "plan_sha256": plan["plan_sha256"],
        "approval_scopes": {
            scope: _scope_manifest(plan, scope) for scope in APPROVAL_SCOPES
        },
        "safety_invariants": [
            "dry_run_is_the_default",
            "exact_plan_digest_is_required_for_apply",
            "routine_scope_excludes_force_required_actions",
            "destructive_scope_contains_only_force_required_actions",
            "successful_actions_are_skipped_on_resume",
            "every_attempt_is_atomically_journaled",
        ],
    }
    return {
        **document,
        "manifest_sha256": execution_run_manifest_sha256(document),
    }


def validate_execution_run_manifest(document: Mapping[str, Any]) -> list[str]:
    errors = []
    scopes = document.get("approval_scopes")
    if not isinstance(scopes, Mapping) or set(scopes) != set(APPROVAL_SCOPES):
        errors.append("approval_scopes must contain routine and destructive")
    elif any(
        scope.get("selected_count") != len(scope.get("canonical_repository_ids", []))
        for scope in scopes.values()
    ):
        errors.append("approval scope selected_count does not match repository IDs")
    if document.get("manifest_sha256") != execution_run_manifest_sha256(document):
        errors.append("manifest_sha256 does not match")
    return errors


def _subprocess_runner(
    argv: Sequence[str], cwd: Path | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def _command_trace(
    argv: Sequence[str],
    cwd: Path | None,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    return {
        "argv": list(argv),
        "cwd": str(cwd) if cwd else None,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_checked(
    argv: Sequence[str],
    *,
    command_runner: CommandRunner,
    cwd: Path | None = None,
    prior_traces: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    completed = command_runner(argv, cwd)
    trace = _command_trace(argv, cwd, completed)
    traces = [*[dict(value) for value in prior_traces], trace]
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CommandExecutionError(detail or "command failed", traces)
    return traces


def _destination(action: Mapping[str, Any]) -> str:
    return f"sg-evals/{action['mirror_slug']}"


def _validate_action_identity(action: Mapping[str, Any]) -> None:
    repository_id = action.get("canonical_repository_id")
    if not isinstance(repository_id, str) or not REPOSITORY_ID_PATTERN.fullmatch(
        repository_id
    ):
        raise ExecutionError("unsafe canonical repository ID")
    mirror_slug = action.get("mirror_slug")
    if not isinstance(mirror_slug, str) or not MIRROR_SLUG_PATTERN.fullmatch(
        mirror_slug
    ):
        raise ExecutionError("unsafe mirror slug")
    if action.get("mirror_name") != f"github.com/sg-evals/{mirror_slug}":
        raise ExecutionError("mirror name does not match mirror slug")
    source_url = action.get("canonical_source_url")
    parsed = urlparse(source_url if isinstance(source_url, str) else "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise ExecutionError("canonical source URL must use HTTPS")


def _expected_argv(action: Mapping[str, Any]) -> list[str] | None:
    kind = action["action"]
    repository_id = action["canonical_repository_id"]
    destination = _destination(action)
    if kind == "create_github_fork":
        return [
            "gh",
            "repo",
            "fork",
            repository_id,
            "--org",
            "sg-evals",
            "--fork-name",
            action["mirror_slug"],
        ]
    if kind == "fast_forward_sync":
        branch = action["source_default_branch"]
        return [
            "gh",
            "repo",
            "sync",
            destination,
            "--source",
            repository_id,
            "--branch",
            branch,
        ]
    return None


def _validated_action(action: Mapping[str, Any]) -> dict[str, Any]:
    _validate_action_identity(action)
    expected_policy = ACTION_APPROVAL_POLICY.get(action["action"])
    observed_policy = (
        action.get("approval_class"),
        action.get("force_required"),
    )
    if expected_policy is not None and observed_policy != expected_policy:
        raise ExecutionError("action approval metadata does not match its kind")
    kind = action["action"]
    branch = action.get("source_default_branch")
    legacy_sync = kind == "fast_forward_sync" and branch is None
    if legacy_sync:
        if action.get("argv") != legacy_sync_argv(action, _destination(action)):
            raise ExecutionError("stored argv does not match legacy sync command")
    elif kind in {"fast_forward_sync", "replace_standalone_mirror"}:
        if not safe_branch(branch):
            raise ExecutionError("unsafe source default branch")
    if kind == "replace_standalone_mirror":
        destination_branch = action.get("destination_default_branch")
        if not safe_branch(destination_branch):
            raise ExecutionError("unsafe destination default branch")
        expected_refspec = (
            f"+refs/heads/{action['source_default_branch']}:"
            f"refs/heads/{destination_branch}"
        )
        if action.get("force_refspec") != expected_refspec:
            raise ExecutionError("force refspec does not match approved branches")
    expected_argv = None if legacy_sync else _expected_argv(action)
    if expected_argv is not None and action.get("argv") != expected_argv:
        raise ExecutionError("stored argv does not match the allowlisted command")
    if action["action"] == "create_git_mirror":
        source_url = action.get("source_url")
        parsed = urlparse(source_url if isinstance(source_url, str) else "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ExecutionError("git mirror source URL must use HTTPS")
        if (
            source_url != action["canonical_source_url"]
            and action.get("source_transport_cutoff_oid") != action["cutoff_commit"]
        ):
            raise ExecutionError(
                "alternate git mirror source lacks cutoff-identical evidence"
            )
    return {**action, **({"argv": expected_argv} if expected_argv else {})}


def _execute_fast_forward_sync(
    action: Mapping[str, Any], *, command_runner: CommandRunner
) -> list[dict[str, Any]]:
    if action.get("source_default_branch") is not None:
        return _run_checked(action["argv"], command_runner=command_runner)
    resolve_argv = [
        "gh",
        "repo",
        "view",
        action["canonical_repository_id"],
        "--json",
        "defaultBranchRef",
    ]
    completed = command_runner(resolve_argv, None)
    trace = _command_trace(resolve_argv, None, completed)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CommandExecutionError(detail or "default branch lookup failed", [trace])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CommandExecutionError(
            "default branch lookup returned invalid JSON", [trace]
        ) from error
    branch = (payload.get("defaultBranchRef") or {}).get("name")
    if not safe_branch(branch):
        raise CommandExecutionError("unsafe resolved source default branch", [trace])
    resolved = {
        **action,
        "source_default_branch": branch,
    }
    resolved = {**resolved, "argv": _expected_argv(resolved)}
    validated = _validated_action(resolved)
    return _run_checked(
        validated["argv"], command_runner=command_runner, prior_traces=[trace]
    )


def _repository_state(
    destination: str,
    *,
    command_runner: CommandRunner,
) -> tuple[Mapping[str, Any] | None, list[dict[str, Any]]]:
    argv = [
        "gh",
        "repo",
        "view",
        destination,
        "--json",
        "nameWithOwner,isFork,parent,description",
    ]
    completed = command_runner(argv, None)
    trace = _command_trace(argv, None, completed)
    if completed.returncode == 0:
        try:
            return json.loads(completed.stdout), [trace]
        except json.JSONDecodeError as error:
            raise CommandExecutionError(
                "gh repo view returned invalid JSON", [trace]
            ) from error
    detail = completed.stderr.strip() or completed.stdout.strip()
    if NOT_FOUND_FRAGMENT in detail.lower():
        return None, [trace]
    raise CommandExecutionError(detail or "gh repo view failed", [trace])


def _execute_github_fork(
    action: Mapping[str, Any],
    *,
    command_runner: CommandRunner,
) -> list[dict[str, Any]]:
    state, traces = _repository_state(
        _destination(action), command_runner=command_runner
    )
    if state is None:
        return _run_checked(
            action["argv"],
            command_runner=command_runner,
            prior_traces=traces,
        )
    parent = state.get("parent") or {}
    parent_id = parent.get("nameWithOwner")
    if parent_id is None and isinstance(parent.get("owner"), Mapping):
        parent_id = f"{parent['owner'].get('login')}/{parent.get('name')}"
    if (
        state.get("isFork")
        and str(parent_id).lower() == action["canonical_repository_id"].lower()
    ):
        return traces
    if state.get("isFork"):
        resolved, resolved_traces = _repository_state(
            action["canonical_repository_id"],
            command_runner=command_runner,
        )
        traces = [*traces, *resolved_traces]
        resolved_id = (resolved or {}).get("nameWithOwner")
        if str(parent_id).lower() == str(resolved_id).lower():
            return traces
    raise CommandExecutionError(
        f"name collision at {_destination(action)} is not the intended fork",
        traces,
    )


def _mirror_description(action: Mapping[str, Any]) -> str:
    return f"sg-evals study mirror of {action['source_url']}"


def _ensure_git_mirror_destination(
    action: Mapping[str, Any],
    *,
    command_runner: CommandRunner,
    prior_traces: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    destination = _destination(action)
    description = _mirror_description(action)
    state, state_traces = _repository_state(destination, command_runner=command_runner)
    traces = [*[dict(value) for value in prior_traces], *state_traces]
    if state is not None and state.get("description") != description:
        raise CommandExecutionError(
            f"name collision at {destination} lacks the study mirror marker",
            traces,
        )
    if state is not None:
        return traces
    return _run_checked(
        [
            "gh",
            "repo",
            "create",
            destination,
            "--public",
            "--description",
            description,
            "--disable-issues",
            "--disable-wiki",
        ],
        command_runner=command_runner,
        prior_traces=traces,
    )


def _execute_git_mirror(
    action: Mapping[str, Any],
    *,
    command_runner: CommandRunner,
) -> list[dict[str, Any]]:
    destination = _destination(action)
    with tempfile.TemporaryDirectory(prefix="sg-evals-mirror-") as directory:
        mirror_path = Path(directory) / f"{action['mirror_slug']}.git"
        traces = _run_checked(
            ["git", "clone", "--bare", action["source_url"], str(mirror_path)],
            command_runner=command_runner,
        )
        traces = _ensure_git_mirror_destination(
            action,
            command_runner=command_runner,
            prior_traces=traces,
        )
        return _run_checked(
            [
                "git",
                "-C",
                str(mirror_path),
                "push",
                "--mirror",
                f"https://github.com/{destination}.git",
            ],
            command_runner=command_runner,
            prior_traces=traces,
        )


def _execute_standalone_replacement(
    action: Mapping[str, Any],
    *,
    command_runner: CommandRunner,
) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="sg-evals-replace-") as directory:
        mirror_path = Path(directory) / f"{action['mirror_slug']}.git"
        traces = _run_checked(
            [
                "git",
                "clone",
                "--bare",
                "--single-branch",
                "--branch",
                action["source_default_branch"],
                action["canonical_source_url"],
                str(mirror_path),
            ],
            command_runner=command_runner,
        )
        return _run_checked(
            [
                "git",
                "-C",
                str(mirror_path),
                "push",
                f"https://github.com/{_destination(action)}.git",
                action["force_refspec"],
            ],
            command_runner=command_runner,
            prior_traces=traces,
        )


def execute_repository_action(
    action: Mapping[str, Any],
    *,
    command_runner: CommandRunner = _subprocess_runner,
) -> list[dict[str, Any]]:
    """Execute one already-approved action without invoking a shell."""
    action = _validated_action(action)
    kind = action["action"]
    if kind == "create_github_fork":
        return _execute_github_fork(action, command_runner=command_runner)
    if kind == "create_git_mirror":
        return _execute_git_mirror(action, command_runner=command_runner)
    if kind == "replace_standalone_mirror":
        return _execute_standalone_replacement(action, command_runner=command_runner)
    if kind == "fast_forward_sync":
        return _execute_fast_forward_sync(action, command_runner=command_runner)
    raise ExecutionError(f"action is not executable: {kind}")


def _with_journal_sha(document: Mapping[str, Any]) -> dict[str, Any]:
    content = {key: value for key, value in document.items() if key != "journal_sha256"}
    return {**content, "journal_sha256": execution_journal_sha256(content)}


def _new_journal(
    plan: Mapping[str, Any],
    scope: str,
    selected: Sequence[Mapping[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    document = {
        "execution_version": 3,
        "plan_sha256": plan["plan_sha256"],
        "approval_scope": scope,
        "started_at": timestamp,
        "updated_at": timestamp,
        "status": "running",
        "selected_count": len(selected),
        "selected_repository_ids": [
            action["canonical_repository_id"] for action in selected
        ],
        "current_action": None,
        "results": [],
    }
    return _with_journal_sha(document)


def validate_execution_journal(document: Mapping[str, Any]) -> list[str]:
    errors = []
    selected = document.get("selected_repository_ids")
    results = document.get("results")
    if not isinstance(selected, list) or len(selected) != len(set(selected)):
        errors.append("selected_repository_ids must be a unique list")
    if document.get("selected_count") != len(selected or []):
        errors.append("selected_count does not match selected_repository_ids")
    if not isinstance(results, list):
        errors.append("results must be a list")
    elif isinstance(selected, list) and any(
        result.get("canonical_repository_id") not in selected for result in results
    ):
        errors.append("result references a repository outside the approval scope")
    if document.get("journal_sha256") != execution_journal_sha256(document):
        errors.append("journal_sha256 does not match")
    return errors


def _load_journal(
    path: Path,
    plan: Mapping[str, Any],
    scope: str,
    selected: Sequence[Mapping[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    if not path.exists():
        return _new_journal(plan, scope, selected, timestamp)
    document = json.loads(path.read_text())
    errors = validate_execution_journal(document)
    if errors:
        raise ExecutionError(f"invalid execution journal: {'; '.join(errors)}")
    expected_ids = [action["canonical_repository_id"] for action in selected]
    if (
        document["plan_sha256"] != plan["plan_sha256"]
        or document["approval_scope"] != scope
        or document["selected_repository_ids"] != expected_ids
    ):
        raise ExecutionError("execution journal does not match the approved plan")
    return document


def _updated_journal(
    journal: Mapping[str, Any],
    *,
    timestamp: str,
    status: str,
    current_action: str | None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    results = [
        *journal["results"],
        *([dict(result)] if result is not None else []),
    ]
    return _with_journal_sha(
        {
            **journal,
            "updated_at": timestamp,
            "status": status,
            "current_action": current_action,
            "results": results,
        }
    )


def _result(
    action: Mapping[str, Any],
    *,
    status: str,
    started_at: str,
    completed_at: str,
    traces: Sequence[Mapping[str, Any]],
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "canonical_repository_id": action["canonical_repository_id"],
        "action": action["action"],
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "commands": [dict(trace) for trace in traces],
        "error": error,
    }


def _record_failed_attempt(
    journal: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    started_at: str,
    error: Exception,
    traces: Sequence[Mapping[str, Any]],
    completed_at: str,
    journal_path: Path,
) -> dict[str, Any]:
    result = _result(
        action,
        status="failed",
        started_at=started_at,
        completed_at=completed_at,
        traces=traces,
        error=str(error),
    )
    failed = _updated_journal(
        journal,
        timestamp=completed_at,
        status="failed",
        current_action=None,
        result=result,
    )
    write_manifest(journal_path, failed)
    return failed


def _run_journaled_action(
    running: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    started_at: str,
    journal_path: Path,
    command_runner: CommandRunner,
    clock: Clock,
) -> list[dict[str, Any]]:
    try:
        return execute_repository_action(action, command_runner=command_runner)
    except CommandExecutionError as error:
        _record_failed_attempt(
            running,
            action,
            started_at=started_at,
            error=error,
            traces=error.traces,
            completed_at=clock(),
            journal_path=journal_path,
        )
        raise
    except (OSError, subprocess.SubprocessError) as error:
        wrapped = ExecutionError(f"command execution failed: {error}")
        _record_failed_attempt(
            running,
            action,
            started_at=started_at,
            error=wrapped,
            traces=[],
            completed_at=clock(),
            journal_path=journal_path,
        )
        raise wrapped from error


def _execute_one(
    journal: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    journal_path: Path,
    command_runner: CommandRunner,
    clock: Clock,
) -> dict[str, Any]:
    started_at = clock()
    running = _updated_journal(
        journal,
        timestamp=started_at,
        status="running",
        current_action=action["canonical_repository_id"],
    )
    write_manifest(journal_path, running)
    traces = _run_journaled_action(
        running,
        action,
        started_at=started_at,
        journal_path=journal_path,
        command_runner=command_runner,
        clock=clock,
    )
    completed_at = clock()
    result = _result(
        action,
        status="succeeded",
        started_at=started_at,
        completed_at=completed_at,
        traces=traces,
    )
    succeeded = _updated_journal(
        running,
        timestamp=completed_at,
        status="running",
        current_action=None,
        result=result,
    )
    write_manifest(journal_path, succeeded)
    return succeeded


def execute_plan(
    plan: Mapping[str, Any],
    *,
    scope: str,
    confirmed_plan_sha: str,
    journal_path: Path,
    command_runner: CommandRunner = _subprocess_runner,
    clock: Clock = _utc_now,
) -> dict[str, Any]:
    """Execute one approval scope and atomically journal every result."""
    if confirmed_plan_sha != plan.get("plan_sha256"):
        raise ExecutionError("confirmed plan digest does not match the action plan")
    selected = select_actions(plan, scope)
    journal = _load_journal(journal_path, plan, scope, selected, timestamp=clock())
    succeeded = {
        result["canonical_repository_id"]
        for result in journal["results"]
        if result["status"] == "succeeded"
    }
    write_manifest(journal_path, journal)
    for action in selected:
        repository_id = action["canonical_repository_id"]
        if repository_id in succeeded:
            continue
        journal = _execute_one(
            journal,
            action,
            journal_path=journal_path,
            command_runner=command_runner,
            clock=clock,
        )
    journal = _updated_journal(
        journal,
        timestamp=clock(),
        status="complete",
        current_action=None,
    )
    write_manifest(journal_path, journal)
    return journal


def main() -> None:
    from authorship.sg_evals_execution_cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
