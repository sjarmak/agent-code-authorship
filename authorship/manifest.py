"""Validation and eligibility reporting for the frozen repository manifest."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from authorship.protocol import load_protocol


class ManifestError(ValueError):
    """Raised when repository provenance is incomplete or inconsistent."""


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_manifest(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document.get("manifest_version") not in (1, 2):
        errors.append("manifest_version must equal 1 or 2")
    cutoff = document.get("cutoff")
    if not isinstance(cutoff, str):
        errors.append("cutoff is required")
        cutoff_time = None
    else:
        try:
            cutoff_time = _parse_time(cutoff)
        except ValueError:
            errors.append("cutoff must be an ISO-8601 timestamp")
            cutoff_time = None

    repositories = document.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        return errors + ["repositories must be a non-empty list"]

    roles_by_id: dict[str, set[str]] = {}
    required = {
        "id",
        "url",
        "role",
        "label",
        "languages",
        "snapshot",
        "evidence",
        "effective_date_range",
        "excluded_paths",
        "retrieved_at",
        "content_checksum",
    }
    for index, entry in enumerate(repositories):
        prefix = f"repositories[{index}]"
        missing = sorted(required - set(entry))
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
            continue
        repo_id = entry["id"]
        roles_by_id.setdefault(repo_id, set()).add(entry["role"])
        snapshot = entry["snapshot"]
        for field in ("commit", "committed_at", "tree"):
            if not snapshot.get(field):
                errors.append(f"{repo_id} snapshot.{field} is required")
        if cutoff_time and snapshot.get("committed_at"):
            try:
                if _parse_time(snapshot["committed_at"]) > cutoff_time:
                    errors.append(f"{repo_id} snapshot is after cutoff")
            except ValueError:
                errors.append(f"{repo_id} snapshot.committed_at is invalid")
        evidence = entry["evidence"]
        if evidence.get("tier") != 1:
            errors.append(f"{repo_id} evidence must be tier 1")
        if entry["label"] == "human":
            if evidence.get("repository_local") is not True:
                errors.append(f"{repo_id} human evidence must be repository-local")
            if not evidence.get("effective_from"):
                errors.append(f"{repo_id} human evidence requires effective_from")
            if not evidence.get("path") or not evidence.get("quote"):
                errors.append(f"{repo_id} human evidence requires path and quote")
        elif entry["label"] == "agent":
            if evidence.get("kind") not in ("maintainer_attestation", "commit_provenance"):
                errors.append(f"{repo_id} has unsupported agent evidence")
        else:
            errors.append(f"{repo_id} label must be agent or human")
    for repo_id, roles in roles_by_id.items():
        if len(roles) > 1:
            errors.append(f"{repo_id} has multiple roles in one specification")
    return errors


def load_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_manifest(document)
    if errors:
        raise ManifestError("; ".join(errors))
    return document


def eligibility_by_language(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    protocol_path = Path(__file__).resolve().parents[1] / "study" / "protocol.v1.json"
    protocol = load_protocol(protocol_path)
    minimum = protocol["identification_gates"]["minimum_repository_groups_per_side"]
    result: dict[str, dict[str, Any]] = {}
    for language in protocol["estimand"]["languages"]:
        totals = Counter()
        development = Counter()
        validation = Counter()
        for entry in document["repositories"]:
            if language not in entry["languages"]:
                continue
            totals[entry["label"]] += 1
            if entry["role"] == "dedicated_validation":
                validation[entry["label"]] += 1
            else:
                development[entry["label"]] += 1
        reasons = []
        for label in ("agent", "human"):
            if development[label] < minimum:
                reasons.append(
                    f"{label} has {development[label]} development groups, "
                    f"fewer than {minimum}"
                )
            if validation[label] < 1:
                reasons.append(f"{label} has no dedicated validation group")
        result[language] = {
            "identified": not reasons,
            "groups": dict(totals),
            "development_groups": dict(development),
            "dedicated_validation_groups": dict(validation),
            "reasons": reasons,
        }
    return result
