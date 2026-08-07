"""Exact commit and hunk extraction from the indexed sg-evals namespace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.language_features import primary_features
from authorship.languages import SKIP_PATH, ext_of
from authorship.sg import api
from authorship.sourcegraph_era_exact import (
    PATH_BATCH_SIZE,
    eligible_changed_paths,
    fetch_changed_files,
)

ApiRunner = Callable[..., Mapping[str, Any]]
MAX_ANCESTOR_PAGES = 20
EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

COMMIT_QUERY = """
query AuthorshipCommit($repo:String!,$rev:String!) {
  repository(name:$repo) {
    commit(rev:$rev) { oid committer { date } parents { oid } }
  }
}
""".strip()

ANCESTORS_QUERY = """
query AuthorshipAncestors(
  $repo:String!,$rev:String!,$afterDate:String!,$beforeDate:String!
) {
  repository(name:$repo) {
    commit(rev:$rev) {
      ancestors(first:1000,afterDate:$afterDate,beforeDate:$beforeDate) {
        nodes { oid committer { date } parents { oid } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()

ANCESTORS_PAGE_QUERY = ANCESTORS_QUERY.replace(
    "$beforeDate:String!", "$beforeDate:String!,$after:String!"
).replace(
    "ancestors(first:1000,afterDate:$afterDate,beforeDate:$beforeDate)",
    "ancestors(first:1000,after:$after,afterDate:$afterDate,beforeDate:$beforeDate)",
)

PATH_DIFFS_QUERY = """
query AuthorshipPathDiffs(
  $repo:String!,$rev:String!,$base:String!,$paths:[String!]!
) {
  repository(name:$repo) {
    commit(rev:$rev) {
      diff(base:$base) {
        fileDiffs(first:100,paths:$paths) {
          nodes {
            oldPath
            newPath
            hunks { body newRange { startLine lines } }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
""".strip()

ROOT_FILES_QUERY = """
query AuthorshipRootFiles($repo:String!,$rev:String!) {
  repository(name:$repo) {
    commit(rev:$rev) { tree(path:"") { files(recursive:true) { path } } }
  }
}
""".strip()

ROOT_BLOB_QUERY = """
query AuthorshipRootBlob($repo:String!,$rev:String!,$path:String!) {
  repository(name:$repo) { commit(rev:$rev) { blob(path:$path) { content } } }
}
""".strip()


class ExactAuthorshipError(RuntimeError):
    """Raised when Sourcegraph cannot provide a complete exact unit."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _hex(value: Any, length: int = 40) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ExactAuthorshipError("Sourcegraph commit timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExactAuthorshipError("Sourcegraph commit timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ExactAuthorshipError("Sourcegraph commit timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _commit_node(data: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = data.get("repository")
    commit = repository.get("commit") if isinstance(repository, Mapping) else None
    if not isinstance(commit, Mapping):
        raise ExactAuthorshipError("Sourcegraph returned no exact commit")
    return commit


def _commit_record(node: Mapping[str, Any]) -> dict[str, Any]:
    oid = node.get("oid")
    committer = node.get("committer")
    committed_at = committer.get("date") if isinstance(committer, Mapping) else None
    parents = node.get("parents")
    if not _hex(oid) or not isinstance(parents, list):
        raise ExactAuthorshipError("Sourcegraph commit identity is invalid")
    parent_oids = [
        parent.get("oid") for parent in parents if isinstance(parent, Mapping)
    ]
    if len(parent_oids) != len(parents) or any(
        not _hex(value) for value in parent_oids
    ):
        raise ExactAuthorshipError("Sourcegraph commit parent identity is invalid")
    timestamp = _timestamp(committed_at)
    return {
        "commit_oid": oid,
        "committed_at": timestamp.isoformat().replace("+00:00", "Z"),
        "first_parent_oid": parent_oids[0] if parent_oids else EMPTY_TREE_OID,
        "parent_count": len(parent_oids),
        "is_root_commit": not parent_oids,
    }


def fetch_commit_metadata(
    sourcegraph_name: str,
    commit_oid: str,
    *,
    api_runner: ApiRunner = api,
) -> dict[str, Any]:
    """Resolve one exact commit, its timestamp, and its first-parent diff base."""
    record = _commit_record(
        _commit_node(api_runner(COMMIT_QUERY, repo=sourcegraph_name, rev=commit_oid))
    )
    if record["commit_oid"] != commit_oid:
        raise ExactAuthorshipError("Sourcegraph resolved a different commit")
    return record


def _comparison(data: Mapping[str, Any]) -> Mapping[str, Any]:
    diff = _commit_node(data).get("diff")
    if not isinstance(diff, Mapping):
        raise ExactAuthorshipError("Sourcegraph returned no exact comparison")
    return diff


def _page(connection: Mapping[str, Any]) -> tuple[bool, str | None]:
    page = connection.get("pageInfo")
    if not isinstance(page, Mapping) or not isinstance(page.get("hasNextPage"), bool):
        raise ExactAuthorshipError("Sourcegraph pagination is invalid")
    cursor = page.get("endCursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ExactAuthorshipError("Sourcegraph pagination cursor is invalid")
    return page["hasNextPage"], cursor


def _hunk(hunk: Any) -> dict[str, Any]:
    if not isinstance(hunk, Mapping) or not isinstance(hunk.get("body"), str):
        raise ExactAuthorshipError("Sourcegraph exact hunk body is invalid")
    new_range = hunk.get("newRange")
    if (
        not isinstance(new_range, Mapping)
        or not isinstance(new_range.get("startLine"), int)
        or not isinstance(new_range.get("lines"), int)
    ):
        raise ExactAuthorshipError("Sourcegraph exact hunk range is invalid")
    return {
        "body": hunk["body"],
        "new_range": {
            "start_line": new_range["startLine"],
            "lines": new_range["lines"],
        },
    }


def _path_node(node: Any, requested: set[str]) -> tuple[dict[str, Any], set[str]]:
    if not isinstance(node, Mapping):
        raise ExactAuthorshipError("Sourcegraph exact path diff is invalid")
    candidates = {
        value for value in (node.get("oldPath"), node.get("newPath")) if value
    }
    matched = requested & candidates
    if not matched:
        raise ExactAuthorshipError("Sourcegraph returned an unrequested path diff")
    hunks = node.get("hunks")
    if not isinstance(hunks, list):
        raise ExactAuthorshipError("Sourcegraph exact path hunks are invalid")
    path = node.get("newPath") if node.get("newPath") in matched else min(matched)
    return {"path": path, "hunks": [_hunk(hunk) for hunk in hunks]}, matched


def _path_batch(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    paths: Sequence[str],
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
        raise ExactAuthorshipError("Sourcegraph exact path diffs are invalid")
    has_next, _ = _page(connection)
    if has_next:
        raise ExactAuthorshipError("exact path-diff batch requires pagination")
    requested, seen, records = set(paths), set(), []
    for node in connection["nodes"]:
        record, matched = _path_node(node, requested)
        records.append(record)
        seen.update(matched)
    if seen != requested:
        raise ExactAuthorshipError("Sourcegraph exact path-diff batch is incomplete")
    return records


def fetch_exact_commit_records(
    sourcegraph_name: str,
    base_oid: str,
    head_oid: str,
    language: str,
    *,
    api_runner: ApiRunner = api,
) -> list[dict[str, Any]]:
    """Fetch every eligible language hunk in one exact commit comparison."""
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
                api_runner,
            )
        )
    return sorted(records, key=lambda row: row["path"])


def _root_paths(data: Mapping[str, Any], language: str) -> list[str]:
    tree = _commit_node(data).get("tree")
    files = tree.get("files") if isinstance(tree, Mapping) else None
    if not isinstance(files, list):
        raise ExactAuthorshipError("Sourcegraph root tree files are invalid")
    extension = {"Go": ".go", "Python": ".py"}[language]
    paths = []
    for row in files:
        path = row.get("path") if isinstance(row, Mapping) else None
        if not isinstance(path, str):
            raise ExactAuthorshipError("Sourcegraph root tree path is invalid")
        if ext_of(path) == extension and not SKIP_PATH(path):
            paths.append(path)
    if len(paths) != len(set(paths)):
        raise ExactAuthorshipError("Sourcegraph root tree paths are duplicated")
    return sorted(paths)


def _root_blob(data: Mapping[str, Any]) -> str:
    blob = _commit_node(data).get("blob")
    content = blob.get("content") if isinstance(blob, Mapping) else None
    if not isinstance(content, str):
        raise ExactAuthorshipError("Sourcegraph root blob content is invalid")
    return content


def _root_record(path: str, content: str) -> dict[str, Any]:
    lines = content.splitlines()
    hunks = []
    if lines:
        hunks.append(
            {
                "body": "".join(f"+{line}\n" for line in lines),
                "new_range": {"start_line": 1, "lines": len(lines)},
            }
        )
    return {"path": path, "hunks": hunks}


def fetch_root_commit_records(
    sourcegraph_name: str,
    commit_oid: str,
    language: str,
    *,
    api_runner: ApiRunner = api,
) -> list[dict[str, Any]]:
    """Materialize a root commit from its exact pinned Sourcegraph blobs."""
    if language not in {"Go", "Python"}:
        raise ExactAuthorshipError("root commit language is invalid")
    paths = _root_paths(
        api_runner(ROOT_FILES_QUERY, repo=sourcegraph_name, rev=commit_oid),
        language,
    )
    records = []
    for path in paths:
        content = _root_blob(
            api_runner(
                ROOT_BLOB_QUERY,
                repo=sourcegraph_name,
                rev=commit_oid,
                path=path,
            )
        )
        records.append(_root_record(path, content))
    return records


def _added_lines(hunk: Mapping[str, Any]) -> tuple[list[str], list[int]]:
    new_range = hunk.get("new_range")
    if not isinstance(new_range, Mapping):
        raise ExactAuthorshipError("Sourcegraph hunk range is missing")
    start, expected = new_range.get("start_line"), new_range.get("lines")
    if not isinstance(start, int) or start < 1 or not isinstance(expected, int):
        raise ExactAuthorshipError("Sourcegraph hunk range is invalid")
    cursor, lines, numbers = start, [], []
    for line in hunk.get("body", "").splitlines():
        if line.startswith("+"):
            lines.append(line[1:])
            numbers.append(cursor)
            cursor += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        elif line.startswith(" ") or not line:
            cursor += 1
        else:
            raise ExactAuthorshipError("Sourcegraph hunk line prefix is invalid")
    if cursor - start != expected:
        raise ExactAuthorshipError("Sourcegraph hunk range does not match its body")
    return lines, numbers


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


def _unit(
    *,
    identity: Mapping[str, Any],
    sourcegraph_name: str,
    first_parent_oid: str,
    committed_at: str,
    language: str,
    lines: Sequence[str],
    line_numbers: Sequence[int],
    role: str,
    tier: str,
    source_record: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = "\n".join(line.rstrip() for line in lines)
    return {
        **identity,
        "sourcegraph_name": sourcegraph_name,
        "first_parent_oid": first_parent_oid,
        "committed_at": committed_at,
        "language": language,
        "line_numbers": list(line_numbers),
        "line_count": len(lines),
        "path_type": _path_type(identity["path"]),
        "code_age_days": 0,
        "calendar_time": committed_at,
        "authorship_role": role,
        "evidence_tier": tier,
        "hunk_sha256": _sha256(identity),
        "content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "sourcegraph_record_sha256": _sha256(source_record),
        "feature_values": primary_features(lines, language),
    }


def _record_units(
    record: Mapping[str, Any],
    *,
    identity_base: Mapping[str, Any],
    unit_base: Mapping[str, Any],
) -> list[dict[str, Any]]:
    path, hunks = record.get("path"), record.get("hunks")
    if not isinstance(path, str) or not isinstance(hunks, list):
        raise ExactAuthorshipError("exact Sourcegraph path record is invalid")
    units = []
    for hunk_index, hunk in enumerate(hunks):
        lines, numbers = _added_lines(hunk)
        if not lines:
            continue
        identity = {
            **identity_base,
            "path": path,
            "hunk_index": hunk_index,
            "new_range": hunk["new_range"],
            "line_numbers": numbers,
        }
        units.append(
            _unit(
                identity=identity,
                lines=lines,
                line_numbers=numbers,
                source_record={"path": path, "hunk": hunk},
                **unit_base,
            )
        )
    return units


def exact_hunk_units(
    *,
    repository_id: str,
    sourcegraph_name: str,
    commit_oid: str,
    first_parent_oid: str,
    committed_at: str,
    language: str,
    records: Sequence[Mapping[str, Any]],
    authorship_role: str,
    evidence_tier: str,
) -> list[dict[str, Any]]:
    """Convert exact Sourcegraph ranges into concrete introduced-code hunks."""
    if (
        not _hex(commit_oid)
        or not _hex(first_parent_oid)
        or language not in {"Go", "Python"}
        or authorship_role not in {"agent", "human"}
    ):
        raise ExactAuthorshipError("exact authorship identity is invalid")
    _timestamp(committed_at)
    identity_base = {"repository_id": repository_id, "commit_oid": commit_oid}
    unit_base = {
        "sourcegraph_name": sourcegraph_name,
        "first_parent_oid": first_parent_oid,
        "committed_at": committed_at,
        "language": language,
        "role": authorship_role,
        "tier": evidence_tier,
    }
    units = []
    for record in sorted(records, key=lambda row: str(row.get("path"))):
        units.extend(
            _record_units(record, identity_base=identity_base, unit_base=unit_base)
        )
    return units


def _ancestor_connection(data: Mapping[str, Any]) -> Mapping[str, Any]:
    connection = _commit_node(data).get("ancestors")
    if not isinstance(connection, Mapping) or not isinstance(
        connection.get("nodes"), list
    ):
        raise ExactAuthorshipError("Sourcegraph ancestor page is invalid")
    return connection


def fetch_window_commits(
    sourcegraph_name: str,
    head_oid: str,
    *,
    start_inclusive: str,
    end_exclusive: str,
    api_runner: ApiRunner = api,
) -> list[dict[str, Any]]:
    """Enumerate all reachable concrete commits in one half-open time window."""
    start, end = _timestamp(start_inclusive), _timestamp(end_exclusive)
    if start >= end:
        raise ExactAuthorshipError("commit window is empty")
    variables = {
        "repo": sourcegraph_name,
        "rev": head_oid,
        "afterDate": start_inclusive,
        "beforeDate": end_exclusive,
    }
    cursor, records = None, []
    for _ in range(MAX_ANCESTOR_PAGES):
        query = ANCESTORS_QUERY if cursor is None else ANCESTORS_PAGE_QUERY
        current = variables if cursor is None else {**variables, "after": cursor}
        connection = _ancestor_connection(api_runner(query, **current))
        for node in connection["nodes"]:
            record = _commit_record(node)
            timestamp = _timestamp(record["committed_at"])
            if start <= timestamp < end:
                records.append(record)
        has_next, next_cursor = _page(connection)
        if not has_next:
            if len(records) != len({row["commit_oid"] for row in records}):
                raise ExactAuthorshipError("Sourcegraph returned duplicate commits")
            return sorted(
                records, key=lambda row: (row["committed_at"], row["commit_oid"])
            )
        if next_cursor is None or next_cursor == cursor:
            raise ExactAuthorshipError("Sourcegraph pagination did not advance")
        cursor = next_cursor
    raise ExactAuthorshipError("Sourcegraph ancestor enumeration exceeded page cap")
