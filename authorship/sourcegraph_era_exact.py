"""Exact net-quarter Sourcegraph materialization through changed-path batches."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.features import features
from authorship.languages import SKIP_PATH, ext_of
from authorship.sg import api

ApiRunner = Callable[..., Mapping[str, Any]]
PATH_BATCH_SIZE = 64
MAX_CHANGED_FILE_PAGES = 20

CHANGED_FILES_QUERY = """
query EraPanelChangedFiles($repo:String!,$rev:String!,$base:String!) {
  repository(name:$repo) {
    commit(rev:$rev) {
      diff(base:$base) {
        changedFiles(first:5000) {
          nodes { path status }
          totalCount
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""".strip()

CHANGED_FILES_PAGE_QUERY = CHANGED_FILES_QUERY.replace(
    "$base:String!", "$base:String!,$after:String!"
).replace(
    "changedFiles(first:5000)",
    "changedFiles(first:5000,after:$after)",
)

PATH_DIFFS_QUERY = """
query EraPanelPathDiffs(
  $repo:String!,$rev:String!,$base:String!,$paths:[String!]!
) {
  repository(name:$repo) {
    commit(rev:$rev) {
      diff(base:$base) {
        fileDiffs(first:100,paths:$paths) {
          nodes { oldPath newPath hunks { body } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""".strip()


def _page(connection: Mapping[str, Any]) -> tuple[bool, str | None]:
    page = connection.get("pageInfo")
    if not isinstance(page, Mapping) or not isinstance(page.get("hasNextPage"), bool):
        raise TypeError("Sourcegraph returned invalid pagination")
    cursor = page.get("endCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise TypeError("Sourcegraph returned an invalid cursor")
    return page["hasNextPage"], cursor


def _comparison(data: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    diff = commit.get("diff") if isinstance(commit, Mapping) else None
    if not isinstance(diff, Mapping):
        raise TypeError("Sourcegraph returned no repository comparison")
    return diff


def _changed_page(
    data: Mapping[str, Any],
) -> tuple[list[dict[str, str]], int, bool, str | None]:
    connection = _comparison(data).get("changedFiles")
    if not isinstance(connection, Mapping) or not isinstance(
        connection.get("nodes"), list
    ):
        raise TypeError("Sourcegraph returned invalid changed files")
    total = connection.get("totalCount")
    if not isinstance(total, int) or total < 0:
        raise TypeError("Sourcegraph returned invalid changed-file count")
    records = [_changed_file(node) for node in connection["nodes"]]
    has_next, cursor = _page(connection)
    return records, total, has_next, cursor


def _changed_file(node: Any) -> dict[str, str]:
    if not isinstance(node, Mapping):
        raise TypeError("Sourcegraph returned an invalid changed file")
    path, status = node.get("path"), node.get("status")
    if not isinstance(path, str) or not path or not isinstance(status, str):
        raise TypeError("Sourcegraph returned invalid changed-file metadata")
    return {"path": path, "status": status}


def fetch_changed_files(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    *,
    api_runner: ApiRunner = api,
) -> list[dict[str, str]]:
    """Enumerate exact changed-path metadata without materializing diffs."""
    variables = {"repo": sourcegraph_name, "rev": head_oid, "base": base_oid}
    records: list[dict[str, str]] = []
    expected_total = None
    cursor = None
    for _ in range(MAX_CHANGED_FILE_PAGES):
        query = CHANGED_FILES_QUERY if cursor is None else CHANGED_FILES_PAGE_QUERY
        current = variables if cursor is None else {**variables, "after": cursor}
        page_records, total, has_next, next_cursor = _changed_page(
            api_runner(query, **current)
        )
        expected_total = total if expected_total is None else expected_total
        if total != expected_total:
            raise RuntimeError(
                "Sourcegraph changed-file count changed during pagination"
            )
        records.extend(page_records)
        if not has_next:
            if len(records) != total:
                raise RuntimeError("Sourcegraph changed-file enumeration is incomplete")
            paths = [record["path"] for record in records]
            if len(paths) != len(set(paths)):
                raise RuntimeError("Sourcegraph returned duplicate changed paths")
            return sorted(records, key=lambda record: record["path"])
        if next_cursor is None or next_cursor == cursor:
            raise RuntimeError("Sourcegraph changed-file pagination did not advance")
        cursor = next_cursor
    raise RuntimeError("Sourcegraph changed-file enumeration exceeded page cap")


def eligible_changed_paths(
    files: Sequence[Mapping[str, Any]], language: str
) -> list[str]:
    """Apply exact language and frozen generated/vendor path exclusions."""
    try:
        extension = {"Python": ".py", "Go": ".go"}[language]
    except KeyError as error:
        raise ValueError("era comparison language must be Python or Go") from error
    paths = []
    for row in files:
        path, status = row.get("path"), row.get("status")
        if not isinstance(path, str) or not isinstance(status, str):
            raise TypeError("changed-file record is invalid")
        if status != "DELETED" and ext_of(path) == extension and not SKIP_PATH(path):
            paths.append(path)
    if len(paths) != len(set(paths)):
        raise RuntimeError("eligible changed paths contain duplicates")
    return sorted(paths)


def _path_batch(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    paths: Sequence[str],
    *,
    api_runner: ApiRunner,
) -> list[dict[str, Any]]:
    data = api_runner(
        PATH_DIFFS_QUERY,
        repo=sourcegraph_name,
        rev=head_oid,
        base=base_oid,
        paths=list(paths),
    )
    connection = _comparison(data).get("fileDiffs")
    if not isinstance(connection, Mapping) or not isinstance(
        connection.get("nodes"), list
    ):
        raise TypeError("Sourcegraph returned invalid exact path diffs")
    has_next, _cursor = _page(connection)
    if has_next:
        raise RuntimeError("exact path-diff batch requires unexpected pagination")
    return _validated_path_nodes(connection["nodes"], paths)


def _validated_path_nodes(
    nodes: Sequence[Any], requested_paths: Sequence[str]
) -> list[dict[str, Any]]:
    requested = set(requested_paths)
    seen = set()
    records = []
    for node in nodes:
        record, matched = _path_node(node, requested)
        seen.update(matched)
        records.append(record)
    if seen != requested:
        raise RuntimeError("Sourcegraph exact path-diff batch is incomplete")
    return records


def _path_node(node: Any, requested: set[str]) -> tuple[dict[str, Any], set[str]]:
    if not isinstance(node, Mapping):
        raise TypeError("Sourcegraph returned an invalid exact path diff")
    candidates = {
        value for value in (node.get("oldPath"), node.get("newPath")) if value
    }
    matched = requested & candidates
    if not matched:
        raise RuntimeError("Sourcegraph returned an unrequested path diff")
    hunks = node.get("hunks")
    if not isinstance(hunks, list):
        raise TypeError("Sourcegraph returned invalid exact path hunks")
    bodies = []
    for hunk in hunks:
        body = hunk.get("body") if isinstance(hunk, Mapping) else None
        if not isinstance(body, str):
            raise TypeError("Sourcegraph returned an invalid exact hunk body")
        bodies.append(body)
    path = node.get("newPath") if node.get("newPath") in matched else min(matched)
    return {"path": path, "hunks": bodies}, matched


def _path_type(path: str) -> str:
    lowered = path.lower()
    name = Path(lowered).name
    if (
        "/test" in f"/{lowered}"
        or "/tests/" in f"/{lowered}/"
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
    ):
        return "test"
    return "source"


def _added_lines(records: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        line[1:]
        for record in records
        for body in record["hunks"]
        for line in body.splitlines()
        if line.startswith("+")
    ]


def _summary(
    records: Sequence[Mapping[str, Any]],
    language: str,
    period: int,
    eligible_count: int,
) -> dict[str, Any]:
    content = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    lines = _added_lines(records)
    path_types = Counter(_path_type(record["path"]) for record in records)
    return {
        "raw_diff_sha256": digest,
        "introduced_code_line_count": len(lines),
        "feature_values": features(lines, language),
        "eligible_path_count": eligible_count,
        "path_type_counts": dict(sorted(path_types.items())),
        "materialization_design": "exact_net_quarter_path_batches",
        "code_age_days": 0,
        "calendar_period": period,
    }


def fetch_exact_period_summary(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    language: str,
    *,
    period: int,
    api_runner: ApiRunner = api,
) -> dict[str, Any]:
    """Materialize every eligible net-quarter hunk through exact path batches."""
    if base_oid == head_oid:
        return _summary([], language, period, 0)
    files = fetch_changed_files(
        sourcegraph_name, base_oid, head_oid, api_runner=api_runner
    )
    paths = eligible_changed_paths(files, language)
    records = []
    for start in range(0, len(paths), PATH_BATCH_SIZE):
        records.extend(
            _path_batch(
                sourcegraph_name,
                base_oid,
                head_oid,
                paths[start : start + PATH_BATCH_SIZE],
                api_runner=api_runner,
            )
        )
    return _summary(records, language, period, len(paths))
