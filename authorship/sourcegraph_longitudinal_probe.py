"""Sourcegraph diff-search and blame evidence for longitudinal hunks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.languages import SKIP_PATH
from authorship.sg import api
from authorship.sourcegraph_discovery_execution import run_sourcegraph_query
from authorship.sourcegraph_discovery_graphql import (
    SEARCH_RESULT_FRAGMENTS,
    SEARCH_RESULTS_SELECTION,
)
from authorship.sourcegraph_longitudinal import (
    LongitudinalExtractionError,
    _group_introductions,
    _is_hex,
    _repository_fields,
)
from authorship.sourcegraph_longitudinal_execution import (
    _atomic_write,
    _load_jsonl_reference,
)

PROBE_MANIFEST_VERSION = 3
REGEX_METACHARACTERS = frozenset(r"\.^$|?*+()[]{}")
CAPABILITIES = ("blame", "diff_search", "revision_search")
DEFAULT_BATCH_SIZE = 15
MAX_BATCH_SIZE = 15

BLAME_GRAPHQL_TEMPLATE = """
query LongitudinalBlame($repo:String!,$rev:String!,$path:String!){
  repository(name:$repo){
    name
    commit(rev:$rev){
      oid
      blob(path:$path){
        path
        blame(startLine:%d,endLine:%d){
          startLine
          endLine
          commit{oid}
        }
      }
    }
  }
}
""".strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def probe_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "probe_manifest_sha256"
    }
    return _sha256(content)


def _escape_regex(value: str) -> str:
    return "".join(
        f"\\{character}" if character in REGEX_METACHARACTERS else character
        for character in value
    )


def render_diff_probe_query(
    repository: Mapping[str, Any], introduction: Mapping[str, Any]
) -> str:
    """Render one exact repository, revision-range, path, and line query."""
    fields = _repository_fields(repository)
    merge_commit = introduction.get("merge_commit")
    diff_base = introduction.get("diff_base")
    path = introduction.get("path")
    text = introduction.get("text")
    if not _is_hex(merge_commit, 40) or not _is_hex(diff_base, 40):
        raise LongitudinalExtractionError("probe revisions are invalid")
    if not isinstance(path, str) or not path:
        raise LongitudinalExtractionError("probe path is required")
    if not isinstance(text, str) or not text:
        raise LongitudinalExtractionError("probe line text is required")
    return " ".join(
        (
            f"repo:^{_escape_regex(fields['sourcegraph_name'])}$",
            f"rev:{merge_commit}:^{diff_base}",
            f"file:^{_escape_regex(path)}$",
            f"content:{json.dumps(text, ensure_ascii=False)}",
            "type:diff",
            "select:commit.diff.added",
            "count:all",
        )
    )


def _diff_match_count(results: Any) -> int | None:
    if not isinstance(results, list):
        return None
    match_count = 0
    for result in results:
        if (
            not isinstance(result, Mapping)
            or result.get("__typename") != "CommitSearchResult"
        ):
            return None
        for field in ("messagePreview", "diffPreview"):
            preview = result.get(field)
            if preview is None:
                continue
            if not isinstance(preview, Mapping) or not isinstance(
                preview.get("highlights"), list
            ):
                return None
            match_count += len(preview["highlights"])
    return match_count


def _complete_diff_response(response: Mapping[str, Any]) -> None:
    observed_match_count = _diff_match_count(response.get("results"))
    incomplete = (
        response.get("limit_hit") is not False
        or response.get("cloning_repositories") != []
        or response.get("missing_repositories") != []
        or response.get("timed_out_repositories") != []
        or observed_match_count is None
        or response.get("result_count") != observed_match_count
    )
    if incomplete:
        raise LongitudinalExtractionError("Sourcegraph diff search is incomplete")


def _result_records(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = [
        {
            "sourcegraph_result_id": f"sha256:{_sha256(result)}",
            "payload": result,
        }
        for result in response["results"]
    ]
    return sorted(records, key=lambda record: record["sourcegraph_result_id"])


def _blame_response(
    repository: Mapping[str, str],
    key: Sequence[Any],
    records: Sequence[Mapping[str, Any]],
    api_runner: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    start_line = min(record["line_number"] for record in records)
    end_line = max(record["line_number"] for record in records)
    query = BLAME_GRAPHQL_TEMPLATE % (start_line, end_line)
    response = api_runner(
        query,
        repo=repository["sourcegraph_name"],
        rev=key[0],
        path=key[1],
    )
    return _validate_blame(response, repository["sourcegraph_name"], key)


def _validate_blame(
    response: Any, sourcegraph_name: str, key: Sequence[Any]
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise LongitudinalExtractionError("Sourcegraph blame must return an object")
    repository = response.get("repository")
    if (
        not isinstance(repository, Mapping)
        or repository.get("name") != sourcegraph_name
    ):
        raise LongitudinalExtractionError("Sourcegraph blame repository does not match")
    commit = repository.get("commit")
    if not isinstance(commit, Mapping) or commit.get("oid") != key[0]:
        raise LongitudinalExtractionError("Sourcegraph blame revision does not match")
    blob = commit.get("blob")
    if not isinstance(blob, Mapping) or blob.get("path") != key[1]:
        raise LongitudinalExtractionError("Sourcegraph blame path does not match")
    ranges = blob.get("blame")
    if not isinstance(ranges, list):
        raise LongitudinalExtractionError("Sourcegraph blame ranges must be a list")
    for item in ranges:
        blame_commit = item.get("commit") if isinstance(item, Mapping) else None
        if not isinstance(blame_commit, Mapping) or not _is_hex(
            blame_commit.get("oid"), 40
        ):
            raise LongitudinalExtractionError("Sourcegraph blame commit is invalid")
    return json.loads(_canonical_json(response))


def _hunk_document(
    hunk_id: str,
    key: Sequence[Any],
    rendered_query: str,
    response: Mapping[str, Any],
    blame_response: Mapping[str, Any],
) -> dict[str, Any]:
    _complete_diff_response(response)
    result_records = _result_records(response)
    blame_ranges = blame_response["repository"]["commit"]["blob"]["blame"]
    return {
        "hunk_id": hunk_id,
        "introducing_commit_oid": key[0],
        "path": key[1],
        "rendered_diff_query": rendered_query,
        "sourcegraph_result_ids": [
            record["sourcegraph_result_id"] for record in result_records
        ],
        "raw_diff_results": result_records,
        "blame_commit_oids": sorted({item["commit"]["oid"] for item in blame_ranges}),
        "raw_blame_response": blame_response,
    }


def _probe_hunk(
    repository: Mapping[str, str],
    hunk_id: str,
    key: Sequence[Any],
    records: Sequence[Mapping[str, Any]],
    api_runner: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    rendered_query = render_diff_probe_query(repository, records[0])
    response = run_sourcegraph_query(
        rendered_query,
        "commit",
        repository["sourcegraph_name"],
        repository["cutoff_commit"],
        api_runner=api_runner,
    )
    blame_response = _blame_response(repository, key, records, api_runner)
    return _hunk_document(hunk_id, key, rendered_query, response, blame_response)


def _batch_query(
    repository: Mapping[str, str],
    groups: Sequence[tuple[str, Sequence[Any], Sequence[Mapping[str, Any]]]],
) -> tuple[str, list[str]]:
    selections = []
    rendered_queries = []
    for index, (_, key, records) in enumerate(groups):
        rendered_query = render_diff_probe_query(repository, records[0])
        rendered_queries.append(rendered_query)
        start_line = min(record["line_number"] for record in records)
        end_line = max(record["line_number"] for record in records)
        selections.extend(
            (
                f"s{index}:search(query:{json.dumps(rendered_query)})"
                f"{{{SEARCH_RESULTS_SELECTION}}}",
                _batch_blame_field(
                    index,
                    repository["sourcegraph_name"],
                    key,
                    start_line,
                    end_line,
                ),
            )
        )
    document = (
        f"{SEARCH_RESULT_FRAGMENTS}\n"
        "query LongitudinalProbeBatch{\n" + "\n".join(selections) + "\n}"
    )
    return document, rendered_queries


def _batch_blame_field(
    index: int,
    sourcegraph_name: str,
    key: Sequence[Any],
    start_line: int,
    end_line: int,
) -> str:
    return (
        f"b{index}:repository(name:{json.dumps(sourcegraph_name)})"
        "{name "
        f"commit(rev:{json.dumps(key[0])})"
        "{oid "
        f"blob(path:{json.dumps(key[1])})"
        "{path "
        f"blame(startLine:{start_line},endLine:{end_line})"
        "{startLine endLine commit{oid}}"
        "}"
        "}"
        "}"
    )


def _aliased_search_response(
    batch_response: Mapping[str, Any],
    alias: str,
    rendered_query: str,
    repository: Mapping[str, str],
) -> dict[str, Any]:
    if alias not in batch_response:
        raise LongitudinalExtractionError(f"Sourcegraph batch alias {alias} is missing")
    search = batch_response[alias]

    def cached_runner(graphql_query: str, **variables: str) -> Mapping[str, Any]:
        return {"search": search}

    return run_sourcegraph_query(
        rendered_query,
        "commit",
        repository["sourcegraph_name"],
        repository["cutoff_commit"],
        api_runner=cached_runner,
    )


def _aliased_blame_response(
    batch_response: Mapping[str, Any],
    alias: str,
    repository: Mapping[str, str],
    key: Sequence[Any],
) -> dict[str, Any]:
    if alias not in batch_response:
        raise LongitudinalExtractionError(f"Sourcegraph batch alias {alias} is missing")
    return _validate_blame(
        {"repository": batch_response[alias]},
        repository["sourcegraph_name"],
        key,
    )


def _probe_hunk_batch(
    repository: Mapping[str, str],
    groups: Sequence[tuple[str, Sequence[Any], Sequence[Mapping[str, Any]]]],
    batch_api_runner: Callable[..., Mapping[str, Any]],
) -> list[dict[str, Any]]:
    document, rendered_queries = _batch_query(repository, groups)
    response = batch_api_runner(document)
    if not isinstance(response, Mapping):
        raise LongitudinalExtractionError("Sourcegraph batch must return an object")
    hunks = []
    for index, ((hunk_id, key, _), rendered_query) in enumerate(
        zip(groups, rendered_queries, strict=True)
    ):
        search = _aliased_search_response(
            response, f"s{index}", rendered_query, repository
        )
        blame = _aliased_blame_response(response, f"b{index}", repository, key)
        hunks.append(_hunk_document(hunk_id, key, rendered_query, search, blame))
    return hunks


def _probe_hunk_batch_resilient(
    repository: Mapping[str, str],
    groups: Sequence[tuple[str, Sequence[Any], Sequence[Mapping[str, Any]]]],
    batch_api_runner: Callable[..., Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        return _probe_hunk_batch(repository, groups, batch_api_runner)
    except RuntimeError:
        if len(groups) == 1:
            hunk_id, key, records = groups[0]
            return [
                _probe_hunk(
                    repository,
                    hunk_id,
                    key,
                    records,
                    batch_api_runner,
                )
            ]
        midpoint = len(groups) // 2
        return _probe_hunk_batch_resilient(
            repository,
            groups[:midpoint],
            batch_api_runner,
        ) + _probe_hunk_batch_resilient(
            repository,
            groups[midpoint:],
            batch_api_runner,
        )


def _probe_hunks_batched(
    repository: Mapping[str, str],
    groups: Sequence[tuple[str, Sequence[Any], Sequence[Mapping[str, Any]]]],
    batch_api_runner: Callable[..., Mapping[str, Any]],
    batch_size: int,
) -> list[dict[str, Any]]:
    hunks = []
    for offset in range(0, len(groups), batch_size):
        hunks.extend(
            _probe_hunk_batch_resilient(
                repository,
                groups[offset : offset + batch_size],
                batch_api_runner,
            )
        )
    return hunks


def build_probe_manifest(
    repository: Mapping[str, Any],
    introductions: Sequence[Mapping[str, Any]],
    *,
    probe_unit_id: str | None = None,
    api_runner: Callable[..., Mapping[str, Any]] = api,
    batch_api_runner: Callable[..., Mapping[str, Any]] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Execute and freeze deterministic Sourcegraph evidence for one repository."""
    fields = _repository_fields(repository)
    identity = probe_unit_id or _sha256(
        {
            "repository": fields,
            "introductions": sorted(
                introductions, key=lambda record: _canonical_json(record)
            ),
        }
    )
    if not _is_hex(identity, 64):
        raise LongitudinalExtractionError("probe unit identity is invalid")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise LongitudinalExtractionError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )
    groups = [
        (hunk_id, key, records)
        for hunk_id, key, records in _group_introductions(
            fields["canonical_repository_id"], introductions
        )
        if not SKIP_PATH(key[1])
    ]
    selected_batch_runner = batch_api_runner
    if selected_batch_runner is None and api_runner is api:
        selected_batch_runner = api_runner
    if selected_batch_runner is None:
        hunks = [
            _probe_hunk(fields, hunk_id, key, records, api_runner)
            for hunk_id, key, records in groups
        ]
    else:
        hunks = _probe_hunks_batched(
            fields,
            groups,
            selected_batch_runner,
            batch_size,
        )
    document = {
        "probe_manifest_version": PROBE_MANIFEST_VERSION,
        "probe_unit_id": identity,
        **fields,
        "capabilities_used": list(CAPABILITIES),
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "hunk_count": len(hunks),
        "hunks": hunks,
        "valid": True,
        "outcomes_consulted": False,
    }
    return {**document, "probe_manifest_sha256": probe_manifest_sha256(document)}


def _load_reusable_manifest(
    path: Path, planned_unit: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    try:
        manifest = json.loads(path.read_text())
        manifest_observation(manifest)
    except (OSError, json.JSONDecodeError, LongitudinalExtractionError):
        return None
    repository = planned_unit["repository"]
    expected = (
        planned_unit.get("probe_unit_id"),
        repository["canonical_repository_id"],
        repository["sourcegraph_name"],
        repository["cutoff_commit"],
    )
    actual = (
        manifest.get("probe_unit_id"),
        manifest.get("canonical_repository_id"),
        manifest.get("sourcegraph_name"),
        manifest.get("cutoff_commit"),
    )
    return manifest if actual == expected else None


def execute_probe_unit(
    planned_unit: Mapping[str, Any],
    *,
    api_runner: Callable[..., Mapping[str, Any]] = api,
) -> dict[str, Any]:
    """Execute or reuse one identity-bound Sourcegraph probe manifest."""
    repository = planned_unit.get("repository")
    output = planned_unit.get("probe_manifest_path")
    unit_id = planned_unit.get("probe_unit_id")
    if (
        not isinstance(repository, Mapping)
        or not isinstance(output, str)
        or not _is_hex(unit_id, 64)
    ):
        raise LongitudinalExtractionError("planned probe unit is invalid")
    path = Path(output)
    reusable = _load_reusable_manifest(path, planned_unit)
    if reusable is not None:
        return _probe_summary(reusable, path, True)
    introductions = _load_jsonl_reference(
        planned_unit.get("introduction_shard"), "introduction_shard"
    )
    manifest = build_probe_manifest(
        repository,
        introductions,
        probe_unit_id=unit_id,
        api_runner=api_runner,
    )
    _atomic_write(path, manifest)
    return _probe_summary(manifest, path, False)


def _probe_summary(
    manifest: Mapping[str, Any], path: Path, reused: bool
) -> dict[str, Any]:
    return {
        "probe_unit_id": manifest["probe_unit_id"],
        "canonical_repository_id": manifest["canonical_repository_id"],
        "probe_manifest_path": path.as_posix(),
        "probe_manifest_sha256": manifest["probe_manifest_sha256"],
        "hunk_count": manifest["hunk_count"],
        "reused": reused,
    }


def manifest_observation(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a valid probe manifest into the longitudinal executor contract."""
    if manifest.get("probe_manifest_sha256") != probe_manifest_sha256(manifest):
        raise LongitudinalExtractionError("probe manifest checksum does not match")
    if (
        manifest.get("valid") is not True
        or manifest.get("outcomes_consulted") is not False
    ):
        raise LongitudinalExtractionError("probe manifest is not admissible")
    hunks = [
        {
            "path": hunk["path"],
            "introducing_commit_oid": hunk["introducing_commit_oid"],
            "sourcegraph_result_ids": hunk["sourcegraph_result_ids"],
            "blame_commit_oids": hunk["blame_commit_oids"],
        }
        for hunk in manifest.get("hunks", [])
        if hunk.get("sourcegraph_result_ids")
    ]
    return {
        "indexed_revision_oid": manifest["cutoff_commit"],
        "capabilities_used": manifest["capabilities_used"],
        "precise_code_intelligence_used": False,
        "scip_used": False,
        "result_manifest_sha256s": [manifest["probe_manifest_sha256"]],
        "hunks": hunks,
    }
