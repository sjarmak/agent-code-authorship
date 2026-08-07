"""Resumable Sourcegraph execution for frozen calendar-quarter era panels."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.features import NAMES
from authorship.sg import api
from authorship.sourcegraph_discovery_execution import atomic_write_json
from authorship.sourcegraph_era_exact import fetch_exact_period_summary

ApiRunner = Callable[..., Mapping[str, Any]]
Boundary = tuple[str, str] | None
BoundaryRunner = Callable[[str, str, Sequence[int]], Mapping[int, Boundary]]
ComparisonRunner = Callable[[str, str, str, str, str, str, int], Mapping[str, Any]]
Progress = Callable[[str], None]
EXTERNAL_ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError)
ERA_PANEL_EXECUTION_VERSION = 1
PERIOD_RESULT_VERSION = 4
SUMMARY_FIELDS = (
    "repository_id",
    "sourcegraph_name",
    "period",
    "base_oid",
    "head_oid",
    "raw_diff_sha256",
    "introduced_code_line_count",
    "eligible_path_count",
    "path_type_counts",
    "materialization_design",
    "code_age_days",
    "calendar_period",
    "observed",
    "observation_status",
    "complete",
    "error",
    "period_result_sha256",
)

COMPARISON_QUERY = """
query EraPanelComparison(
  $repo:String!,$rev:String!,$base:String!,$query:String!
) {
  repository(name:$repo) {
    commit(rev:$rev) {
      diff(base:$base) {
        fileDiffs(first:100,query:$query) {
          rawDiff
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""".strip()

COMPARISON_PAGE_QUERY = """
query EraPanelComparisonPage(
  $repo:String!,$rev:String!,$base:String!,$after:String!,$query:String!
) {
  repository(name:$repo) {
    commit(rev:$rev) {
      diff(base:$base) {
        fileDiffs(first:100,after:$after,query:$query) {
          rawDiff
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""".strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(document: Mapping[str, Any], excluded: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded}
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _quarter_start(period: int) -> str:
    year, quarter = divmod(period, 4)
    month = quarter * 3 + 1
    return f"{year:04d}-{month:02d}-01T00:00:00Z"


def _quarter_end(period: int) -> str:
    return _quarter_start(period + 1)


def _boundary_query(dates: Sequence[str]) -> str:
    fields = "\n".join(
        f'b{index}:ancestors(first:1,beforeDate:"{date}"){{nodes{{oid}}}}'
        for index, date in enumerate(dates)
    )
    return (
        "query EraPanelBoundaries($repo:String!,$rev:String!){"
        "repository(name:$repo){commit(rev:$rev){"
        f"{fields}"
        "}}}"
    )


def _commit_record(data: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    if not isinstance(commit, Mapping):
        raise TypeError("Sourcegraph boundary query returned no pinned commit")
    return commit


def _boundary_oid(commit: Mapping[str, Any], alias: str) -> str | None:
    connection = commit.get(alias)
    nodes = connection.get("nodes") if isinstance(connection, Mapping) else None
    if isinstance(nodes, list) and not nodes:
        return None
    oid = nodes[0].get("oid") if isinstance(nodes, list) else None
    if not isinstance(oid, str) or len(oid) != 40:
        raise RuntimeError("Sourcegraph boundary query returned an invalid commit")
    return oid


def _period_boundary(
    commit: Mapping[str, Any],
    aliases: Mapping[str, str],
    period: int,
) -> Boundary:
    base = _boundary_oid(commit, aliases[_quarter_start(period)])
    head = _boundary_oid(commit, aliases[_quarter_end(period)])
    return (base, head) if base is not None and head is not None else None


def fetch_period_boundaries(
    sourcegraph_name: str,
    cutoff_commit: str,
    periods: Sequence[int],
    *,
    api_runner: ApiRunner = api,
) -> dict[int, Boundary]:
    """Resolve the last indexed commit before each frozen quarter boundary."""
    dates = sorted(
        {
            boundary
            for period in periods
            for boundary in (_quarter_start(period), _quarter_end(period))
        }
    )
    aliases = {date: f"b{index}" for index, date in enumerate(dates)}
    commit = _commit_record(
        api_runner(
            _boundary_query(dates),
            repo=sourcegraph_name,
            rev=cutoff_commit,
        )
    )
    return {period: _period_boundary(commit, aliases, period) for period in periods}


def _comparison_page(data: Mapping[str, Any]) -> tuple[str, bool, str | None]:
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    diff = commit.get("diff") if isinstance(commit, Mapping) else None
    files = diff.get("fileDiffs") if isinstance(diff, Mapping) else None
    if not isinstance(files, Mapping) or not isinstance(files.get("rawDiff"), str):
        raise TypeError("Sourcegraph comparison returned no raw diff")
    page = files.get("pageInfo")
    if not isinstance(page, Mapping) or not isinstance(page.get("hasNextPage"), bool):
        raise TypeError("Sourcegraph comparison returned invalid pagination")
    cursor = page.get("endCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise RuntimeError("Sourcegraph comparison returned an invalid cursor")
    return files["rawDiff"], page["hasNextPage"], cursor


def fetch_comparison_raw_diff(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    language: str,
    *,
    api_runner: ApiRunner = api,
) -> str:
    """Fetch every raw file-diff page for one frozen quarter comparison."""
    if base_oid == head_oid:
        return ""
    suffix = {"Python": ".py", "Go": ".go"}.get(language)
    if suffix is None:
        raise ValueError("era comparison language must be Python or Go")
    variables = {
        "repo": sourcegraph_name,
        "rev": head_oid,
        "base": base_oid,
        "query": suffix,
    }
    chunks = []
    cursor = None
    for _ in range(1_000):
        query = COMPARISON_QUERY if cursor is None else COMPARISON_PAGE_QUERY
        current = variables if cursor is None else {**variables, "after": cursor}
        raw_diff, has_next, next_cursor = _comparison_page(api_runner(query, **current))
        chunks.append(raw_diff)
        if not has_next:
            return "".join(chunks)
        if next_cursor is None or next_cursor == cursor:
            raise RuntimeError("Sourcegraph comparison pagination did not advance")
        cursor = next_cursor
    raise RuntimeError("Sourcegraph comparison exceeded 1000 pages")


def fetch_period_summary(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    language: str,
    plan_sha256: str,
    repository_id: str,
    period: int,
) -> Mapping[str, Any]:
    """Fetch one exact, bounded repository-quarter feature summary."""
    del plan_sha256, repository_id
    return fetch_exact_period_summary(
        sourcegraph_name,
        base_oid,
        head_oid,
        language,
        period=period,
    )


def period_result_sha256(document: Mapping[str, Any]) -> str:
    return _sha256(document, "period_result_sha256")


def _shard_path(repository_id: str, period: int) -> Path:
    identity = hashlib.sha256(repository_id.encode()).hexdigest()
    return Path("shards") / identity[:2] / identity / f"{period}.json"


def _load_reusable(
    path: Path,
    plan_sha256: str,
    repository_id: str,
    period: int,
) -> Mapping[str, Any] | None:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, Mapping)
        or document.get("era_panel_plan_sha256") != plan_sha256
        or document.get("repository_id") != repository_id
        or document.get("period") != period
        or document.get("period_result_version") != PERIOD_RESULT_VERSION
        or document.get("complete") is not True
        or document.get("period_result_sha256") != period_result_sha256(document)
    ):
        return None
    return document


def _empty_summary(period: int = 0) -> dict[str, Any]:
    empty_sha = hashlib.sha256(b"").hexdigest()
    return {
        "raw_diff_sha256": empty_sha,
        "introduced_code_line_count": 0,
        "feature_values": {name: 0.0 for name in NAMES},
        "eligible_path_count": 0,
        "path_type_counts": {},
        "materialization_design": "exact_net_quarter_path_batches",
        "code_age_days": 0,
        "calendar_period": period,
    }


def _structural_summary(period: int) -> dict[str, Any]:
    return {
        key: None
        for key in (
            "raw_diff_sha256",
            "introduced_code_line_count",
            "feature_values",
            "eligible_path_count",
            "path_type_counts",
            "code_age_days",
        )
    } | {
        "materialization_design": "exact_net_quarter_path_batches",
        "calendar_period": period,
    }


def _validated_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    required = set(_empty_summary())
    if set(summary) != required:
        raise TypeError("era period summary fields are invalid")
    feature_values = summary.get("feature_values")
    if not isinstance(feature_values, Mapping) or set(feature_values) != set(NAMES):
        raise TypeError("era period feature fields are invalid")
    return dict(summary)


def _period_result(
    plan_sha256: str,
    repository: Mapping[str, Any],
    period: int,
    base_oid: str | None,
    head_oid: str | None,
    summary: Mapping[str, Any],
    error: str | None,
) -> dict[str, Any]:
    document = {
        "period_result_version": PERIOD_RESULT_VERSION,
        "era_panel_plan_sha256": plan_sha256,
        "repository_id": repository["repository_id"],
        "sourcegraph_name": repository["sourcegraph_name"],
        "period": period,
        "base_oid": base_oid,
        "head_oid": head_oid,
        **summary,
        "complete": error is None,
        "observed": error is None and base_oid is not None,
        "observation_status": (
            "error"
            if error is not None
            else "observed" if base_oid is not None else "structural_precreation"
        ),
        "error": error,
    }
    return {**document, "period_result_sha256": period_result_sha256(document)}


def _error_message(error: Exception) -> str:
    message = " ".join(str(error).split())[:300] or type(error).__name__
    return f"{type(error).__name__}: {message}"


def _execute_period(
    plan_sha256: str,
    repository: Mapping[str, Any],
    period: int,
    boundaries: Mapping[int, Boundary],
    comparison_runner: ComparisonRunner,
) -> dict[str, Any]:
    try:
        boundary = boundaries[period]
        if boundary is None:
            return _period_result(
                plan_sha256,
                repository,
                period,
                None,
                None,
                _structural_summary(period),
                None,
            )
        base_oid, head_oid = boundary
        summary = comparison_runner(
            repository["sourcegraph_name"],
            base_oid,
            head_oid,
            repository["language"],
            plan_sha256,
            repository["repository_id"],
            period,
        )
        return _period_result(
            plan_sha256,
            repository,
            period,
            base_oid,
            head_oid,
            _validated_summary(summary),
            None,
        )
    except EXTERNAL_ERRORS as error:
        return _period_result(
            plan_sha256,
            repository,
            period,
            None,
            None,
            _empty_summary(period),
            _error_message(error),
        )


def _missing_period(
    plan_sha256: str,
    repository: Mapping[str, Any],
    period: int,
    boundaries: Mapping[int, Boundary],
    comparison_runner: ComparisonRunner,
    boundary_error: str | None,
) -> dict[str, Any]:
    if boundary_error is not None:
        return _period_result(
            plan_sha256,
            repository,
            period,
            None,
            None,
            _empty_summary(period),
            boundary_error,
        )
    return _execute_period(
        plan_sha256, repository, period, boundaries, comparison_runner
    )


def _period_summary(
    document: Mapping[str, Any], relative_path: Path, reused: bool
) -> dict[str, Any]:
    return {key: document[key] for key in SUMMARY_FIELDS} | {
        "shard_path": relative_path.as_posix(),
        "reused": reused,
    }


def _repository_periods(
    plan: Mapping[str, Any],
    repository: Mapping[str, Any],
    output_directory: Path,
    boundary_runner: BoundaryRunner,
    comparison_runner: ComparisonRunner,
) -> list[dict[str, Any]]:
    reusable = {}
    for period in plan["required_periods"]:
        relative = _shard_path(repository["repository_id"], period)
        document = _load_reusable(
            output_directory / relative,
            plan["era_panel_plan_sha256"],
            repository["repository_id"],
            period,
        )
        if document is not None:
            reusable[period] = (document, relative)
    missing = [period for period in plan["required_periods"] if period not in reusable]
    boundaries = {}
    boundary_error = None
    if missing:
        try:
            boundaries = boundary_runner(
                repository["sourcegraph_name"],
                repository["cutoff_commit"],
                missing,
            )
        except EXTERNAL_ERRORS as error:
            boundary_error = _error_message(error)
    summaries = []
    for period in plan["required_periods"]:
        if period in reusable:
            document, relative = reusable[period]
            summaries.append(_period_summary(document, relative, True))
            continue
        relative = _shard_path(repository["repository_id"], period)
        document = _missing_period(
            plan["era_panel_plan_sha256"],
            repository,
            period,
            boundaries,
            comparison_runner,
            boundary_error,
        )
        atomic_write_json(output_directory / relative, document)
        summaries.append(_period_summary(document, relative, False))
    return summaries


def _execution_manifest(
    plan: Mapping[str, Any],
    periods: Sequence[Mapping[str, Any]],
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    complete = sum(period["complete"] for period in periods)
    observed = sum(period["observed"] for period in periods)
    reused = sum(period["reused"] for period in periods)
    document = {
        "era_panel_execution_version": ERA_PANEL_EXECUTION_VERSION,
        "era_panel_plan_sha256": plan["era_panel_plan_sha256"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": "complete" if complete == len(periods) else "incomplete",
        "period_count": len(periods),
        "complete_period_count": complete,
        "incomplete_period_count": len(periods) - complete,
        "observed_period_count": observed,
        "structural_unobserved_period_count": complete - observed,
        "reused_period_count": reused,
        "executed_period_count": len(periods) - reused,
        "periods": list(periods),
    }
    return {
        **document,
        "era_panel_execution_sha256": _sha256(document, "era_panel_execution_sha256"),
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def execute_era_panel_plan(
    plan: Mapping[str, Any],
    output_directory: Path,
    *,
    boundary_runner: BoundaryRunner = fetch_period_boundaries,
    comparison_runner: ComparisonRunner = fetch_period_summary,
    progress: Progress = lambda _message: None,
) -> dict[str, Any]:
    """Execute or reuse every frozen repository-quarter Sourcegraph unit."""
    started_at = _timestamp()
    periods = []
    for index, repository in enumerate(plan["repositories"], start=1):
        periods.extend(
            _repository_periods(
                plan,
                repository,
                output_directory,
                boundary_runner,
                comparison_runner,
            )
        )
        progress(f"{index}/{len(plan['repositories'])} {repository['repository_id']}")
    manifest = _execution_manifest(plan, periods, started_at, _timestamp())
    atomic_write_json(
        output_directory / "sourcegraph-era-panel-execution.v1.json",
        manifest,
    )
    return manifest


def load_complete_period_results(
    manifest: Mapping[str, Any],
    output_directory: Path,
    expected_plan_sha256: str,
) -> list[Mapping[str, Any]]:
    """Load content-bound raw period shards from a complete execution."""
    periods = _validated_execution_manifest(manifest, expected_plan_sha256)
    results = []
    for summary in periods:
        path = output_directory / summary["shard_path"]
        document = _load_bound_period(path, summary, expected_plan_sha256)
        results.append(document)
    return results


def _declared_counts(periods: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    complete = sum(period.get("complete") is True for period in periods)
    observed = sum(period.get("observed") is True for period in periods)
    reused = sum(period.get("reused") is True for period in periods)
    return {
        "period_count": len(periods),
        "complete_period_count": complete,
        "incomplete_period_count": len(periods) - complete,
        "observed_period_count": observed,
        "structural_unobserved_period_count": complete - observed,
        "reused_period_count": reused,
        "executed_period_count": len(periods) - reused,
    }


def _validated_execution_manifest(
    manifest: Mapping[str, Any], expected_plan_sha256: str
) -> Sequence[Mapping[str, Any]]:
    if manifest.get("era_panel_execution_version") != ERA_PANEL_EXECUTION_VERSION:
        raise RuntimeError("era panel execution manifest version is invalid")
    if manifest.get("era_panel_plan_sha256") != expected_plan_sha256:
        raise RuntimeError("era panel execution does not match the frozen plan")
    if manifest.get("era_panel_execution_sha256") != _sha256(
        manifest, "era_panel_execution_sha256"
    ):
        raise RuntimeError("era panel execution manifest hash is invalid")
    periods = manifest.get("periods")
    if not isinstance(periods, list) or manifest.get("status") != "complete":
        raise RuntimeError("era panel execution is incomplete")
    if any(not isinstance(period, Mapping) for period in periods):
        raise RuntimeError("era panel execution periods are invalid")
    counts = _declared_counts(periods)
    if any(manifest.get(key) != value for key, value in counts.items()):
        raise RuntimeError("era panel execution declared counts are invalid")
    if any(period.get("complete") is not True for period in periods):
        raise RuntimeError("era panel execution is incomplete")
    return periods


def _load_bound_period(
    path: Path,
    summary: Mapping[str, Any],
    expected_plan_sha256: str,
) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot load period shard {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise RuntimeError(f"period shard {path} is not a JSON object")
    if document.get("period_result_version") != PERIOD_RESULT_VERSION:
        raise RuntimeError(f"period result version is invalid for {path}")
    if document.get("era_panel_plan_sha256") != expected_plan_sha256:
        raise RuntimeError(f"period shard {path} differs from the frozen plan")
    if document.get("period_result_sha256") != period_result_sha256(document):
        raise RuntimeError(f"period shard {path} has an invalid content hash")
    if any(summary.get(field) != document.get(field) for field in SUMMARY_FIELDS):
        raise RuntimeError(f"period summary differs from shard {path}")
    return document
