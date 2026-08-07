"""Pinned input and artifact IO for the longitudinal cohort freeze."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema

from authorship.sourcegraph_cohort_freeze import (
    CohortFreezeError,
    build_cohort_freeze,
)
from authorship.sourcegraph_cohort_validation import (
    EXPECTED_INPUTS,
    canonical_json,
    validate_cohort_freeze,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "study/sourcegraph-longitudinal-cohort-freeze.schema.json"
)


def _read_pinned_inputs(
    paths: Mapping[str, Path], expected_sha256: Mapping[str, str]
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, str]]]:
    if set(paths) != EXPECTED_INPUTS or set(expected_sha256) != EXPECTED_INPUTS:
        raise CohortFreezeError("independent pin set must exactly cover inputs")
    documents = {}
    files = []
    for input_id in sorted(EXPECTED_INPUTS):
        try:
            payload = paths[input_id].read_bytes()
        except OSError as error:
            raise CohortFreezeError(f"cannot read {input_id}: {error}") from error
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256[input_id]:
            raise CohortFreezeError(f"{input_id} differs from independent pin")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CohortFreezeError(f"{input_id} JSON is invalid") from error
        if not isinstance(document, Mapping):
            raise CohortFreezeError(f"{input_id} root must be a JSON object")
        documents[input_id] = document
        files.append({"input_id": input_id, "file_sha256": actual})
    return documents, files


def _validate_against_schema(artifact: Mapping[str, Any]) -> None:
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        error = next(validator.iter_errors(artifact), None)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as error:
        raise CohortFreezeError(f"cannot validate cohort schema: {error}") from error
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise CohortFreezeError(f"cohort schema rejected {location}: {error.message}")


def materialize_cohort_freeze(
    paths: Mapping[str, Path],
    expected_sha256: Mapping[str, str],
    *,
    output_path: Path,
    discovery_start: str,
    simulation_replicates: int,
    simulation_seed: int,
) -> dict[str, Any]:
    """Verify independent pins, build the freeze, and write canonical JSON."""
    documents, files = _read_pinned_inputs(paths, expected_sha256)
    artifact = build_cohort_freeze(
        documents["protocol"],
        documents["discovery"],
        documents["adoption"],
        documents["agent_commits"],
        documents["ai_ban"],
        documents["targets"],
        documents["survival"],
        discovery_start=discovery_start,
        simulation_replicates=simulation_replicates,
        simulation_seed=simulation_seed,
        input_files=files,
    )
    errors = validate_cohort_freeze(artifact)
    if errors:
        raise CohortFreezeError("; ".join(errors))
    _validate_against_schema(artifact)
    try:
        output_path.write_text(canonical_json(artifact) + "\n")
    except OSError as error:
        raise CohortFreezeError(f"cannot write cohort freeze: {error}") from error
    return artifact
