"""Freeze Sourcegraph's indexed primary-language metadata for review strata."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from typing import Any

INVENTORY_VERSION = 3


class RepositoryLanguageError(ValueError):
    """Raised when the frozen repository frame or Sourcegraph response drifts."""


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def repository_language_inventory_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in document.items() if key != "inventory_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def sg_evals_repository_names(
    index_manifest: Mapping[str, Any],
    *,
    repository_ids: Collection[str] | None = None,
) -> dict[str, str]:
    """Return the exact indexed sg-evals mirror name for every manifest row."""

    if index_manifest.get("outcomes_consulted") is not False:
        raise RepositoryLanguageError("index manifest must remain outcomes blind")
    records = index_manifest.get("repositories")
    if not isinstance(records, list) or not records:
        raise RepositoryLanguageError("index manifest has no repositories")
    names: dict[str, str] = {}
    expected = set(repository_ids) if repository_ids is not None else None
    for record in records:
        repository_id = record.get("canonical_repository_id")
        if expected is not None and repository_id not in expected:
            continue
        sourcegraph = record.get("sourcegraph")
        mirror = sourcegraph.get("mirror") if isinstance(sourcegraph, Mapping) else None
        name = mirror.get("name") if isinstance(mirror, Mapping) else None
        state = mirror.get("state") if isinstance(mirror, Mapping) else None
        if (
            not isinstance(repository_id, str)
            or not isinstance(name, str)
            or not name.startswith("github.com/sg-evals/")
            or state != "indexed"
        ):
            raise RepositoryLanguageError(
                f"{repository_id!r} lacks an indexed sg-evals mirror"
            )
        if repository_id in names:
            raise RepositoryLanguageError(
                "duplicate canonical repository in index manifest"
            )
        names[repository_id] = name
    if expected is not None and set(names) != expected:
        raise RepositoryLanguageError("index manifest does not cover the review frame")
    return dict(sorted(names.items()))


def _validate_observed_at(observed_at: str) -> None:
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise RepositoryLanguageError(
            "observed_at must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise RepositoryLanguageError("observed_at must include a timezone")


def _repositories(tranche: Mapping[str, Any]) -> list[dict[str, str]]:
    if tranche.get("tranche_number") != 1:
        raise RepositoryLanguageError("language inventory requires the first tranche")
    if tranche.get("outcomes_consulted") is not False:
        raise RepositoryLanguageError("language inventory must remain outcomes blind")
    tasks = tranche.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RepositoryLanguageError("first tranche has no review tasks")

    repositories = []
    repository_ids: set[str] = set()
    sourcegraph_names: set[str] = set()
    for task in tasks:
        repository_id = task.get("canonical_repository_id")
        sourcegraph_name = task.get("sourcegraph_name")
        if not isinstance(repository_id, str) or not repository_id:
            raise RepositoryLanguageError("task lacks a canonical repository")
        if not isinstance(sourcegraph_name, str) or not sourcegraph_name:
            raise RepositoryLanguageError("task lacks a Sourcegraph repository")
        if repository_id in repository_ids:
            raise RepositoryLanguageError(
                "duplicate canonical repository in first tranche"
            )
        if sourcegraph_name in sourcegraph_names:
            raise RepositoryLanguageError(
                "duplicate Sourcegraph repository in first tranche"
            )
        repository_ids.add(repository_id)
        sourcegraph_names.add(sourcegraph_name)
        repositories.append(
            {
                "canonical_repository_id": repository_id,
                "sourcegraph_name": sourcegraph_name,
            }
        )
    return sorted(repositories, key=lambda item: item["canonical_repository_id"])


def _query(batch_size: int) -> str:
    variables = ", ".join(f"$name{index}: String!" for index in range(batch_size))
    fields = " ".join(
        f"repository{index}: repository(name: $name{index}) {{ name language }}"
        for index in range(batch_size)
    )
    return f"query RepositoryLanguages({variables}) {{ {fields} }}"


def _language_row(
    repository: Mapping[str, str],
    language_sourcegraph_name: str,
    result: Any,
) -> dict[str, str]:
    if not isinstance(result, Mapping):
        raise RepositoryLanguageError(
            f"missing repository metadata for {language_sourcegraph_name}"
        )
    if result.get("name") != language_sourcegraph_name:
        raise RepositoryLanguageError(
            f"Sourcegraph repository identity mismatch for {language_sourcegraph_name}"
        )
    language = result.get("language")
    if not isinstance(language, str) or not language.strip():
        raise RepositoryLanguageError(
            f"missing language for {language_sourcegraph_name}"
        )
    return {
        "canonical_repository_id": repository["canonical_repository_id"],
        "review_sourcegraph_name": repository["sourcegraph_name"],
        "language_sourcegraph_name": language_sourcegraph_name,
        "language": language,
    }


def _language_rows(
    repositories: Sequence[Mapping[str, str]],
    sourcegraph_names: Mapping[str, str],
    query_api: Callable[..., Mapping[str, Any]],
    batch_size: int,
) -> list[dict[str, str]]:
    repository_ids = {
        repository["canonical_repository_id"] for repository in repositories
    }
    if set(sourcegraph_names) != repository_ids:
        raise RepositoryLanguageError(
            "sg-evals repository-name map does not match the first tranche"
        )
    if any(
        not isinstance(name, str) or not name.startswith("github.com/sg-evals/")
        for name in sourcegraph_names.values()
    ):
        raise RepositoryLanguageError("language lookup must use only sg-evals mirrors")
    rows = []
    for start in range(0, len(repositories), batch_size):
        batch = repositories[start : start + batch_size]
        variables = {
            f"name{index}": sourcegraph_names[repository["canonical_repository_id"]]
            for index, repository in enumerate(batch)
        }
        response = query_api(_query(len(batch)), **variables)
        if not isinstance(response, Mapping):
            raise RepositoryLanguageError("Sourcegraph returned a non-object response")
        for index, repository in enumerate(batch):
            language_sourcegraph_name = variables[f"name{index}"]
            rows.append(
                _language_row(
                    repository,
                    language_sourcegraph_name,
                    response.get(f"repository{index}"),
                )
            )
    return rows


def _validate_inventory_arguments(
    batch_size: int,
    index_manifest_sha256: str,
    observed_at: str,
) -> None:
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or batch_size < 1
    ):
        raise RepositoryLanguageError("batch size must be a positive integer")
    if (
        not isinstance(index_manifest_sha256, str)
        or len(index_manifest_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in index_manifest_sha256
        )
    ):
        raise RepositoryLanguageError(
            "index manifest checksum must be lowercase SHA-256"
        )
    _validate_observed_at(observed_at)


def _inventory_document(
    tranche: Mapping[str, Any],
    rows: Sequence[Mapping[str, str]],
    *,
    index_manifest_sha256: str,
    name_map_sha256: str,
    observed_at: str,
) -> dict[str, Any]:
    return {
        "$schema": "sourcegraph-repository-language-inventory.schema.json",
        "inventory_version": INVENTORY_VERSION,
        "case_index_sha256": tranche.get("case_index_sha256"),
        "source_tranche_sha256": tranche.get("tranche_sha256"),
        "observed_at": observed_at,
        "sourcegraph_capability": "Repository.language",
        "sourcegraph_scope": "github.com/sg-evals/*",
        "scip_required": False,
        "index_manifest_sha256": index_manifest_sha256,
        "repository_name_map_sha256": name_map_sha256,
        "repository_count": len(rows),
        "review_scope_exception_count": sum(
            not row["review_sourcegraph_name"].startswith("github.com/sg-evals/")
            for row in rows
        ),
        "language_counts": dict(
            sorted(Counter(row["language"] for row in rows).items())
        ),
        "repositories": list(rows),
        "outcomes_consulted": False,
    }


def build_repository_language_inventory(
    tranche: Mapping[str, Any],
    query_api: Callable[..., Mapping[str, Any]],
    *,
    sourcegraph_names: Mapping[str, str],
    index_manifest_sha256: str,
    observed_at: str,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Collect outcome-blind Repository.language values for the frozen frame."""

    _validate_inventory_arguments(batch_size, index_manifest_sha256, observed_at)
    repositories = _repositories(tranche)
    rows = _language_rows(repositories, sourcegraph_names, query_api, batch_size)
    name_map_sha256 = hashlib.sha256(
        _canonical_json(dict(sorted(sourcegraph_names.items()))).encode()
    ).hexdigest()
    inventory = _inventory_document(
        tranche,
        rows,
        index_manifest_sha256=index_manifest_sha256,
        name_map_sha256=name_map_sha256,
        observed_at=observed_at,
    )
    return {
        **inventory,
        "inventory_sha256": repository_language_inventory_sha256(inventory),
    }
