"""Verified stratified execution of censoring-correct survival estimates."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.censoring_estimators import estimate_survival
from authorship.survival_event_batch import survival_event_inventory_sha256
from authorship.survival_event_materializer import (
    survival_event_shard_manifest_sha256,
)

AGENT_FAMILIES = (
    "Claude_Code",
    "Copilot",
    "Cursor",
    "Devin",
    "OpenAI_Codex",
)
LANGUAGES = ("Go", "Python")
EVIDENCE_TIERS = ("tier_1", "tier_2")
HORIZONS = (30, 90, 180, 365)
MINIMUM_REPOSITORIES_PER_STRATUM = 5


class SurvivalEstimateExecutionError(ValueError):
    """Raised when verified event shards cannot support frozen estimates."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def survival_estimate_artifact_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "survival_estimate_artifact_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SurvivalEstimateExecutionError(f"{label} is invalid") from error
    if not isinstance(document, Mapping):
        raise SurvivalEstimateExecutionError(f"{label} must be an object")
    return document


def _load_inventory(event_root: Path) -> Mapping[str, Any]:
    inventory = _load_json(
        event_root / "survival-event-inventory.v1.json", "event inventory"
    )
    if inventory.get("status") != "complete":
        raise SurvivalEstimateExecutionError("event inventory must be complete")
    checksum = inventory.get("survival_event_inventory_sha256")
    if checksum != survival_event_inventory_sha256(inventory):
        raise SurvivalEstimateExecutionError("event inventory checksum does not match")
    entries = inventory.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise SurvivalEstimateExecutionError("event inventory repositories are invalid")
    if any(
        not isinstance(entry, Mapping) or entry.get("status") != "valid"
        for entry in entries
    ):
        raise SurvivalEstimateExecutionError("event inventory repository is invalid")
    return inventory


def _verified_manifest(
    event_root: Path,
    entry: Mapping[str, Any],
    lineage_validation_sha256: str,
) -> Mapping[str, Any]:
    filename = entry.get("manifest_file")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise SurvivalEstimateExecutionError("manifest filename is invalid")
    manifest = _load_json(event_root / "manifests" / filename, "shard manifest")
    checksum = manifest.get("survival_event_shard_manifest_sha256")
    if manifest.get("contract_version") != 3:
        raise SurvivalEstimateExecutionError("shard manifest contract is invalid")
    if checksum != entry.get("manifest_sha256"):
        raise SurvivalEstimateExecutionError("shard manifest checksum is inconsistent")
    if checksum != survival_event_shard_manifest_sha256(manifest):
        raise SurvivalEstimateExecutionError("shard manifest checksum does not match")
    if manifest.get("repository_id") != entry.get("repository_id"):
        raise SurvivalEstimateExecutionError("shard manifest population does not match")
    input_sha256s = manifest.get("input_sha256s")
    if (
        not isinstance(input_sha256s, Mapping)
        or input_sha256s.get("lineage_validation") != lineage_validation_sha256
    ):
        raise SurvivalEstimateExecutionError(
            "shard lineage validation provenance does not match"
        )
    return manifest


def _validate_line(
    line: Any, repository_id: str, identities: set[tuple[str, str]]
) -> Mapping[str, Any]:
    if not isinstance(line, Mapping) or line.get("repository_id") != repository_id:
        raise SurvivalEstimateExecutionError("event line population does not match")
    line_id = line.get("line_id")
    identity = (repository_id, line_id)
    if not isinstance(line_id, str) or not line_id or identity in identities:
        raise SurvivalEstimateExecutionError("event line identity is invalid")
    identities.add(identity)
    if line.get("agent_family") not in AGENT_FAMILIES:
        raise SurvivalEstimateExecutionError("event line agent family is invalid")
    if line.get("language") not in LANGUAGES:
        raise SurvivalEstimateExecutionError("event line language is invalid")
    if line.get("evidence_tier") not in EVIDENCE_TIERS:
        raise SurvivalEstimateExecutionError("event line evidence tier is invalid")
    return line


def _load_shard(
    event_root: Path,
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any],
    identities: set[tuple[str, str]],
) -> list[Mapping[str, Any]]:
    filename = entry.get("event_shard_file")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise SurvivalEstimateExecutionError("event shard filename is invalid")
    path = event_root / "shards" / filename
    if _sha256(path) != entry.get("event_shard_sha256"):
        raise SurvivalEstimateExecutionError("event shard checksum does not match")
    if entry.get("event_shard_sha256") != manifest.get("event_shard_sha256"):
        raise SurvivalEstimateExecutionError("event shard checksum is inconsistent")
    lines = []
    with path.open(encoding="utf-8") as source:
        for number, raw in enumerate(source, start=1):
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as error:
                raise SurvivalEstimateExecutionError(
                    f"event shard line {number} is invalid JSON"
                ) from error
            lines.append(_validate_line(line, entry["repository_id"], identities))
    if len(lines) != entry.get("line_count") or len(lines) != manifest.get(
        "line_count"
    ):
        raise SurvivalEstimateExecutionError("event shard line count does not match")
    if sum(manifest.get("censoring_audit", {}).values()) != len(lines):
        raise SurvivalEstimateExecutionError("censoring audit count does not match")
    return lines


def _load_verified_lines(
    event_root: Path, inventory: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    identities: set[tuple[str, str]] = set()
    lines = []
    entries = sorted(
        inventory["repositories"], key=lambda entry: entry["repository_id"]
    )
    lineage_validation_sha256 = inventory.get("lineage_validation_sha256")
    for entry in entries:
        manifest = _verified_manifest(event_root, entry, lineage_validation_sha256)
        lines.extend(_load_shard(event_root, entry, manifest, identities))
    counts = inventory.get("counts", {})
    if len(lines) != counts.get("lines"):
        raise SurvivalEstimateExecutionError(
            "event inventory line count does not match"
        )
    repositories = {line["repository_id"] for line in lines}
    if len(repositories) != counts.get("repositories"):
        raise SurvivalEstimateExecutionError(
            "event inventory repository count does not match"
        )
    return lines


def _stratum_specs() -> list[dict[str, Any]]:
    specs = [{"stratum_id": "overall", "dimensions": {}}]
    specs.extend(
        {
            "stratum_id": f"agent_family={family}",
            "dimensions": {"agent_family": family},
        }
        for family in AGENT_FAMILIES
    )
    specs.extend(
        {
            "stratum_id": f"language={language}",
            "dimensions": {"language": language},
        }
        for language in LANGUAGES
    )
    specs.extend(
        {
            "stratum_id": f"agent_family={family};language={language}",
            "dimensions": {"agent_family": family, "language": language},
        }
        for family in AGENT_FAMILIES
        for language in LANGUAGES
    )
    specs.extend(
        {
            "stratum_id": f"evidence_tier={tier}",
            "dimensions": {"evidence_tier": tier},
        }
        for tier in EVIDENCE_TIERS
    )
    return specs


def _group_lines(
    lines: Sequence[Mapping[str, Any]], specs: Sequence[Mapping[str, Any]]
) -> dict[str, list[Mapping[str, Any]]]:
    groups = {spec["stratum_id"]: [] for spec in specs}
    family_ids = {family: f"agent_family={family}" for family in AGENT_FAMILIES}
    language_ids = {language: f"language={language}" for language in LANGUAGES}
    tier_ids = {tier: f"evidence_tier={tier}" for tier in EVIDENCE_TIERS}
    for line in lines:
        family = line["agent_family"]
        language = line["language"]
        groups["overall"].append(line)
        groups[family_ids[family]].append(line)
        groups[language_ids[language]].append(line)
        groups[f"agent_family={family};language={language}"].append(line)
        groups[tier_ids[line["evidence_tier"]]].append(line)
    return groups


def _estimate_stratum(
    spec: Mapping[str, Any],
    lines: list[Mapping[str, Any]],
    bootstrap_replicates: int,
    seed: int,
) -> dict[str, Any]:
    base = {
        "stratum_id": spec["stratum_id"],
        "dimensions": spec["dimensions"],
        "line_count": len(lines),
        "repository_count": len({line["repository_id"] for line in lines}),
    }
    if not lines:
        return {**base, "status": "empty", "estimate": None}
    if base["repository_count"] < MINIMUM_REPOSITORIES_PER_STRATUM:
        return {**base, "status": "not_identified", "estimate": None}
    evidence_tiers = {line["evidence_tier"] for line in lines}
    if len(evidence_tiers) > 1 and "evidence_tier" not in spec["dimensions"]:
        return {**base, "status": "not_identified", "estimate": None}
    estimate = estimate_survival(
        {"contract_version": 1, "lines": lines},
        horizons_days=HORIZONS,
        bootstrap_replicates=bootstrap_replicates,
        seed=seed,
    )
    return {**base, "status": "identified", "estimate": estimate}


def _monotone(points: Sequence[Mapping[str, Any]]) -> bool:
    values = [point["survival"] for point in points]
    return all(left >= right for left, right in zip(values, values[1:]))


def _invariants(strata: Sequence[Mapping[str, Any]]) -> dict[str, bool | None]:
    identified = [entry for entry in strata if entry["status"] == "identified"]
    estimates = [entry["estimate"] for entry in identified]
    monotone = all(
        _monotone(estimate[weighting]["kaplan_meier"])
        for estimate in estimates
        for weighting in ("primary", "secondary")
    )
    mass = all(
        abs(sum(point["state_occupancy"].values()) - 1.0) <= 1e-9
        for estimate in estimates
        for weighting in ("primary", "secondary")
        for point in estimate[weighting]["aalen_johansen"]
    )
    explicit = all(
        "risk_set_weight" in point and "risk_set_lines" in point
        for estimate in estimates
        for weighting in ("primary", "secondary")
        for point in estimate[weighting]["kaplan_meier"]
    )
    cursor = [
        entry
        for entry in identified
        if entry["dimensions"].get("agent_family") == "Cursor"
    ]
    return {
        "all_kaplan_meier_curves_monotone": monotone,
        "all_aalen_johansen_masses_conserved": mass,
        "all_risk_sets_explicit": explicit,
        "cursor_365_rise_absent": (
            all(
                _monotone(entry["estimate"]["primary"]["kaplan_meier"])
                for entry in cursor
            )
            if cursor
            else None
        ),
    }


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as destination:
            destination.write(f"{_canonical_json(document)}\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _execute_strata(
    specs: Sequence[Mapping[str, Any]],
    groups: Mapping[str, list[Mapping[str, Any]]],
    bootstrap_replicates: int,
    seed: int,
) -> list[dict[str, Any]]:
    return [
        _estimate_stratum(
            spec,
            groups[spec["stratum_id"]],
            bootstrap_replicates,
            seed,
        )
        for spec in specs
    ]


def _artifact_document(
    inventory: Mapping[str, Any],
    line_count: int,
    specs: Sequence[Mapping[str, Any]],
    strata: Sequence[Mapping[str, Any]],
    invariants: Mapping[str, bool | None],
    bootstrap_replicates: int,
    seed: int,
) -> dict[str, Any]:
    identified = sum(entry["status"] == "identified" for entry in strata)
    not_identified = sum(entry["status"] == "not_identified" for entry in strata)
    empty = sum(entry["status"] == "empty" for entry in strata)
    return {
        "contract_version": 1,
        "source": {
            "event_inventory_file": "survival-event-inventory.v1.json",
            "event_inventory_sha256": inventory["survival_event_inventory_sha256"],
        },
        "horizons_days": list(HORIZONS),
        "bootstrap": {
            "unit": "repository",
            "replicates": bootstrap_replicates,
            "seed": seed,
            "confidence_level": 0.95,
        },
        "strata_definition": list(specs),
        "counts": {
            "repositories": inventory["counts"]["repositories"],
            "lines": line_count,
            "strata": len(strata),
            "identified_strata": identified,
            "not_identified_strata": not_identified,
            "empty_strata": empty,
        },
        "strata": list(strata),
        "invariants": invariants,
        "selection_outcomes_consulted": False,
    }


def execute_survival_estimates(
    *,
    event_root: Path,
    output_path: Path,
    bootstrap_replicates: int = 2000,
    seed: int = 20260724,
) -> dict[str, Any]:
    """Verify event shards and execute every frozen survival stratum."""
    inventory = _load_inventory(event_root)
    lines = _load_verified_lines(event_root, inventory)
    specs = _stratum_specs()
    groups = _group_lines(lines, specs)
    strata = _execute_strata(specs, groups, bootstrap_replicates, seed)
    invariants = _invariants(strata)
    if any(value is False for value in invariants.values()):
        raise SurvivalEstimateExecutionError("survival estimator invariant failed")
    document = _artifact_document(
        inventory,
        len(lines),
        specs,
        strata,
        invariants,
        bootstrap_replicates,
        seed,
    )
    artifact = {
        **document,
        "survival_estimate_artifact_sha256": (
            survival_estimate_artifact_sha256(document)
        ),
    }
    _atomic_json(output_path, artifact)
    return artifact
