"""Validate the outcome-blind contemporary reference candidate frame."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_CANDIDATE_FIELDS = {
    "id",
    "url",
    "language",
    "proposed_label",
    "source",
    "selection_key",
    "content_group",
    "metadata",
    "solicitation_status",
}
FORBIDDEN_FIELDS = {
    "classifier_score",
    "model_score",
    "feature_vector",
    "agent_probability",
    "predicted_label",
}
FORBIDDEN_METADATA = {
    "comment_rate",
    "docstring_rate",
    "style_score",
    "classifier_score",
    "feature_vector",
}


class CandidateError(ValueError):
    """Raised when candidate selection leaks outcomes or repository roles."""


def selection_key(candidate: dict[str, Any]) -> str:
    payload = "\0".join(
        (
            candidate["proposed_label"],
            candidate["language"],
            candidate["id"].lower(),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_candidates(
    document: dict[str, Any],
    target_manifest: dict[str, Any],
    reference_manifest: dict[str, Any],
) -> list[str]:
    errors = []
    if document.get("frame_version") != 2:
        errors.append("frame_version must equal 2")
    if document.get("status") != "frozen_before_solicitation":
        errors.append("candidate frame must be frozen before solicitation")
    candidates = document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return errors + ["candidates must be a non-empty list"]
    excluded = {
        entry["id"].lower()
        for manifest in (target_manifest, reference_manifest)
        for entry in manifest["repositories"]
    }
    seen_ids: set[str] = set()
    seen_groups: set[str] = set()
    previous_key = ""
    for index, candidate in enumerate(candidates):
        prefix = f"candidates[{index}]"
        missing = REQUIRED_CANDIDATE_FIELDS - set(candidate)
        if missing:
            errors.append(f"{prefix} missing fields: {sorted(missing)}")
            continue
        for field in FORBIDDEN_FIELDS & set(candidate):
            errors.append(f"candidate fields may not contain {field}")
        metadata = candidate["metadata"]
        if not isinstance(metadata, dict):
            errors.append(f"{prefix} metadata must be an object")
            continue
        for field in FORBIDDEN_METADATA & set(metadata):
            errors.append(f"candidate metadata may not contain {field}")
        repo_id = candidate["id"]
        canonical = repo_id.lower()
        if not isinstance(repo_id, str) or not REPO_ID.fullmatch(repo_id):
            errors.append(f"{prefix} has unsafe repository id")
        if canonical in excluded:
            errors.append(f"{repo_id} has target/reference role overlap")
        if canonical in seen_ids:
            errors.append(f"{repo_id} is duplicated")
        seen_ids.add(canonical)
        if metadata.get("is_fork") is not False:
            errors.append(f"{repo_id} fork candidates are forbidden")
        group = candidate["content_group"].lower()
        if group in seen_groups:
            errors.append(f"{repo_id} duplicates a content group")
        seen_groups.add(group)
        expected_key = selection_key(candidate)
        if candidate["selection_key"] != expected_key:
            errors.append(f"{repo_id} selection key mismatch")
        if candidate["selection_key"] < previous_key:
            errors.append("candidates must be ordered by selection key")
        previous_key = candidate["selection_key"]
        if candidate["language"] not in ("Python", "Go"):
            errors.append(f"{repo_id} has unsupported language")
        if candidate["proposed_label"] not in ("human", "agent"):
            errors.append(f"{repo_id} has unsupported proposed label")
        if candidate["solicitation_status"] != "unsolicited":
            errors.append(f"{repo_id} was not frozen before solicitation")
    return errors


def load_candidates(
    path: Path,
    target_manifest: dict[str, Any],
    reference_manifest: dict[str, Any],
) -> dict[str, Any]:
    document = json.loads(path.read_text())
    errors = validate_candidates(document, target_manifest, reference_manifest)
    if errors:
        raise CandidateError("; ".join(errors))
    return document
