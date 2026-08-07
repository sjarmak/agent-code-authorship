"""Validation for Sourcegraph longitudinal shard artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from authorship.sourcegraph_longitudinal import (
    HORIZONS,
    LINEAGE_STATES,
    SHARD_VERSION,
    longitudinal_shard_sha256,
)


def _is_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _hunk_validation_errors(hunks: Sequence[Mapping[str, Any]]) -> list[str]:
    errors = []
    identifiers = [hunk.get("hunk_id") for hunk in hunks]
    if len(identifiers) != len(set(identifiers)):
        errors.append("hunk IDs must be unique")
    if list(hunks) != sorted(hunks, key=lambda hunk: hunk.get("hunk_id", "")):
        errors.append("hunks must be in deterministic order")
    for hunk in hunks:
        lineage = hunk.get("lineage")
        if not isinstance(lineage, Mapping) or set(lineage) != {
            str(value) for value in HORIZONS
        }:
            errors.append("hunk lineage must contain all frozen horizons")
            continue
        if any(not isinstance(counts, Mapping) for counts in lineage.values()):
            errors.append("hunk lineage counts must be objects")
            continue
        if any(_invalid_state_counts(counts) for counts in lineage.values()):
            errors.append("hunk lineage counts are invalid")
            continue
        if any(
            sum(counts.values()) != hunk.get("change_size_lines")
            for counts in lineage.values()
        ):
            errors.append("hunk lineage counts must equal change_size_lines")
    return errors


def _invalid_state_counts(counts: Mapping[str, Any]) -> bool:
    return set(counts) != set(LINEAGE_STATES) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    )


def validate_longitudinal_shard(
    document: Mapping[str, Any], protocol: Mapping[str, Any]
) -> list[str]:
    """Validate a longitudinal shard without trusting nested shapes."""
    if not isinstance(document, Mapping):
        return ["longitudinal shard must be an object"]
    hunks = document.get("hunks")
    exclusions = document.get("exclusions")
    disagreements = document.get("sourcegraph_git_disagreements")
    values = (hunks, exclusions, disagreements)
    if not all(isinstance(value, list) for value in values):
        return ["hunks, exclusions, and disagreements must be lists"]
    if any(not isinstance(hunk, Mapping) for hunk in hunks):
        return ["hunks must contain objects"]
    errors = _top_level_errors(document, protocol, hunks, exclusions, disagreements)
    errors.extend(_hunk_validation_errors(hunks))
    return errors


def _top_level_errors(
    document: Mapping[str, Any],
    protocol: Mapping[str, Any],
    hunks: Sequence[Any],
    exclusions: Sequence[Any],
    disagreements: Sequence[Any],
) -> list[str]:
    errors = []
    if document.get("longitudinal_shard_version") != SHARD_VERSION:
        errors.append("longitudinal_shard_version must equal 3")
    if not _is_hex(document.get("execution_unit_id"), 64):
        errors.append("execution_unit_id is invalid")
    if document.get("protocol_sha256") != protocol.get("protocol_sha256"):
        errors.append("protocol_sha256 does not match")
    if document.get("outcomes_consulted") is not False:
        errors.append("longitudinal shard must be outcome blind")
    pinned_git = document.get("pinned_git")
    if not isinstance(pinned_git, Mapping):
        errors.append("pinned_git must be an object")
    elif pinned_git.get("authoritative_for_lineage") is not True:
        errors.append("pinned Git must be authoritative for lineage")
    if document.get("longitudinal_shard_sha256") != longitudinal_shard_sha256(document):
        errors.append("longitudinal_shard_sha256 does not match")
    for field, records in (
        ("hunk_count", hunks),
        ("excluded_hunk_count", exclusions),
        ("sourcegraph_git_disagreement_count", disagreements),
    ):
        if document.get(field) != len(records):
            errors.append(f"{field} does not match")
    return errors
