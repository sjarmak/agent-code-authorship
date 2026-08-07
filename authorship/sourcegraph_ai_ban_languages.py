"""Collect Sourcegraph language metadata for the frozen AI-ban controls."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from authorship.sourcegraph_ai_ban_review import ai_ban_target_manifest_sha256
from authorship.sourcegraph_repository_languages import (
    RepositoryLanguageError,
    build_repository_language_inventory,
)

INVENTORY_VERSION = 1


class AiBanLanguageError(ValueError):
    """Raised when the AI-ban language inventory is not reproducible."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def ai_ban_language_inventory_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value for key, value in document.items() if key != "inventory_sha256"
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _language_frame(
    target: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    if target.get("target_manifest_sha256") != ai_ban_target_manifest_sha256(target):
        raise AiBanLanguageError("AI-ban target checksum does not match")
    if target.get("outcomes_consulted") is not False:
        raise AiBanLanguageError("AI-ban target is outcome exposed")
    repositories = target.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise AiBanLanguageError("AI-ban target repositories are invalid")
    names = {
        record["canonical_repository_id"]: record["sourcegraph_name"]
        for record in repositories
    }
    if len(names) != len(repositories) or any(
        not name.startswith("github.com/sg-evals/") for name in names.values()
    ):
        raise AiBanLanguageError("AI-ban target requires unique sg-evals repositories")
    frame = {
        "tranche_number": 1,
        "tranche_sha256": target["target_manifest_sha256"],
        "case_index_sha256": target["predecessors"]["case_index_sha256"],
        "outcomes_consulted": False,
        "tasks": [
            {
                "canonical_repository_id": repository,
                "sourcegraph_name": sourcegraph_name,
            }
            for repository, sourcegraph_name in sorted(names.items())
        ],
    }
    return frame, names


def _inventory_document(
    target: Mapping[str, Any],
    base: Mapping[str, Any],
    index_manifest_file_sha256: str,
) -> dict[str, Any]:
    rows = list(base["repositories"])
    return {
        "$schema": "sourcegraph-ai-ban-language-inventory.schema.json",
        "inventory_version": INVENTORY_VERSION,
        "target_manifest_sha256": target["target_manifest_sha256"],
        "index_manifest_file_sha256": index_manifest_file_sha256,
        "observed_at": base["observed_at"],
        "sourcegraph_capability": "Repository.language",
        "sourcegraph_scope": "github.com/sg-evals/*",
        "scip_required": False,
        "repository_name_map_sha256": base["repository_name_map_sha256"],
        "repository_count": len(rows),
        "language_counts": dict(
            sorted(Counter(row["language"] for row in rows).items())
        ),
        "repositories": rows,
        "outcomes_consulted": False,
    }


def build_ai_ban_language_inventory(
    target: Mapping[str, Any],
    query_api: Callable[..., Mapping[str, Any]],
    *,
    index_manifest_file_sha256: str,
    observed_at: str,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Query Repository.language for every preregistered AI-ban control."""
    frame, names = _language_frame(target)
    try:
        base = build_repository_language_inventory(
            frame,
            query_api,
            sourcegraph_names=names,
            index_manifest_sha256=index_manifest_file_sha256,
            observed_at=observed_at,
            batch_size=batch_size,
        )
    except RepositoryLanguageError as error:
        raise AiBanLanguageError(str(error)) from error
    document = _inventory_document(target, base, index_manifest_file_sha256)
    return {
        **document,
        "inventory_sha256": ai_ban_language_inventory_sha256(document),
    }
