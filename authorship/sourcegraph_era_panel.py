"""Sourcegraph-backed panel planning and deterministic feature materialization."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from authorship.features import NAMES, features
from authorship.survival_lines import parse_added_patch

PANEL_PLAN_VERSION = 1
PERIOD_OFFSETS = (-3, -2, -1, 1)
SOURCEGRAPH_MATERIALIZATION = {
    "design": "exact_net_quarter_path_batches",
    "period_result_version": 4,
    "changed_files_page_size": 5000,
    "maximum_changed_file_pages": 20,
    "path_batch_size": 64,
    "required_fields": [
        "RepositoryComparison.changedFiles",
        "RepositoryComparison.fileDiffs.paths",
    ],
    "cap_result": "repository_period_incomplete",
}


class EraPanelError(ValueError):
    """The frozen Sourcegraph inputs cannot produce a valid era panel."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _quarter(timestamp: Any) -> int:
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise EraPanelError("event timestamp must be UTC")
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise EraPanelError("event timestamp is invalid") from error
    return value.year * 4 + (value.month - 1) // 3


def _rows_by_id(
    document: Mapping[str, Any], key: str, identity: str, label: str
) -> dict[str, Mapping[str, Any]]:
    rows = document.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise EraPanelError(f"{label} records are invalid")
    indexed = {row.get(identity): row for row in rows}
    if None in indexed or len(indexed) != len(rows):
        raise EraPanelError(f"{label} identities are invalid")
    return indexed


def _primary_adopters(
    freeze: Mapping[str, Any],
    adoption_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, int]]:
    event_study = freeze["cohorts"]["adoption_event_study"]
    adopters = {}
    for language, row in event_study["primary"]["languages"].items():
        for repository_id in row["repository_ids"]:
            adoption = adoption_by_id.get(repository_id)
            if adoption is None or adoption.get("language") != language:
                raise EraPanelError("primary adopter differs from adoption catalog")
            adopters[repository_id] = (
                language,
                _quarter(adoption.get("adoption_observed_at")),
            )
    return adopters


def _static_controls(
    freeze: Mapping[str, Any],
) -> dict[str, tuple[str, str, int | None]]:
    event_study = freeze["cohorts"]["adoption_event_study"]
    policies = {
        row["repository_id"]: _quarter(row["policy_effective_at"])
        for row in freeze["cohorts"]["human_evidence"]["H2_policy_human"][
            "repositories"
        ]
    }
    controls = {}
    for language, row in event_study["control_pool"]["languages"].items():
        for repository_id in row["h2_policy_repository_ids"]:
            controls[repository_id] = (
                language,
                "h2_ai_ban_control",
                policies[repository_id],
            )
        for repository_id in row["never_observed_repository_ids"]:
            controls[repository_id] = (
                language,
                "never_adopter_control",
                None,
            )
    return controls


def _index_location(record: Mapping[str, Any]) -> tuple[str, str]:
    sourcegraph = record.get("sourcegraph")
    if not isinstance(sourcegraph, Mapping):
        raise EraPanelError("Sourcegraph index location is invalid")
    name = sourcegraph.get("selected_name")
    cutoff = record.get("cutoff_commit")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(cutoff, str)
        or len(cutoff) != 40
    ):
        raise EraPanelError("Sourcegraph index pin is invalid")
    return name, cutoff


def _plan_row(
    repository_id: str,
    language: str,
    role: str,
    adoption_period: int | None,
    policy_period: int | None,
    index_record: Mapping[str, Any],
) -> dict[str, Any]:
    sourcegraph_name, cutoff = _index_location(index_record)
    return {
        "repository_id": repository_id,
        "language": language,
        "role": role,
        "adoption_period": adoption_period,
        "policy_effective_period": policy_period,
        "sourcegraph_name": sourcegraph_name,
        "cutoff_commit": cutoff,
    }


def _plan_repositories(
    adopters: Mapping[str, tuple[str, int]],
    controls: Mapping[str, tuple[str, str, int | None]],
    index_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = [
        _plan_row(
            repository_id, language, "adopter", period, None, index_by_id[repository_id]
        )
        for repository_id, (language, period) in adopters.items()
    ]
    rows.extend(
        _plan_row(
            repository_id,
            language,
            role,
            None,
            policy_period,
            index_by_id[repository_id],
        )
        for repository_id, (language, role, policy_period) in controls.items()
    )
    return sorted(rows, key=lambda row: row["repository_id"])


def build_era_panel_plan(
    freeze: Mapping[str, Any],
    adoption: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the primary adopter and contemporaneous-control panel population."""
    if any(
        document.get("outcomes_consulted") is not False
        for document in (freeze, adoption, index_manifest)
    ):
        raise EraPanelError("era panel inputs must remain outcome blind")
    adoption_by_id = _rows_by_id(adoption, "repositories", "repository_id", "adoption")
    index_by_id = _rows_by_id(
        index_manifest,
        "repositories",
        "canonical_repository_id",
        "index manifest",
    )
    adopters = _primary_adopters(freeze, adoption_by_id)
    controls = _static_controls(freeze)
    if set(adopters) & set(controls):
        raise EraPanelError("adopter and static control roles overlap")
    periods = sorted(
        {
            period + offset
            for _, period in adopters.values()
            for offset in PERIOD_OFFSETS
        }
    )
    repositories = _plan_repositories(adopters, controls, index_by_id)
    document = {
        "era_panel_plan_version": PANEL_PLAN_VERSION,
        "cohort_freeze_sha256": freeze.get("cohort_freeze_sha256"),
        "adoption_catalog_sha256": adoption.get("catalog_sha256"),
        "index_manifest_content_sha256": _sha256(index_manifest),
        "period_definition": "calendar_quarter",
        "adoption_quarter_excluded": True,
        "sourcegraph_materialization": SOURCEGRAPH_MATERIALIZATION,
        "required_periods": periods,
        "repository_count": len(repositories),
        "repositories": repositories,
        "outcomes_consulted": False,
    }
    return {**document, "era_panel_plan_sha256": _sha256(document)}


def _period_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return row.get("repository_id"), row.get("period")


def _validated_periods(
    plan: Mapping[str, Any],
    period_results: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    expected = {
        (repository["repository_id"], period)
        for repository in plan["repositories"]
        for period in plan["required_periods"]
    }
    indexed = {_period_key(row): row for row in period_results}
    if len(indexed) != len(period_results) or set(indexed) != expected:
        raise EraPanelError("period results must exactly cover the frozen plan")
    for row in indexed.values():
        if not _valid_period_result(row):
            raise EraPanelError("Sourcegraph period result is incomplete")
    return indexed


def _valid_period_result(row: Mapping[str, Any]) -> bool:
    if row.get("complete") is not True:
        return False
    if row.get("observed", True) is False:
        return (
            row.get("observation_status") == "structural_precreation"
            and row.get("base_oid") is None
            and row.get("head_oid") is None
            and row.get("feature_values") is None
        )
    return (
        isinstance(row.get("feature_values"), Mapping)
        and set(row["feature_values"]) == set(NAMES)
        and isinstance(row.get("introduced_code_line_count"), int)
        and isinstance(row.get("raw_diff_sha256"), str)
        and len(row["raw_diff_sha256"]) == 64
        and all(
            isinstance(row.get(field), str) and len(row[field]) == 40
            for field in ("base_oid", "head_oid")
        )
    )


def summarize_period_diff(raw_diff: str, language: str) -> dict[str, Any]:
    """Reduce one complete language-filtered raw diff to bounded feature data."""
    records = parse_added_patch(raw_diff, language)
    lines = [record["text"] for record in records]
    paths = sorted({record["path"] for record in records})
    return {
        "raw_diff_sha256": hashlib.sha256(raw_diff.encode()).hexdigest(),
        "introduced_code_line_count": len(lines),
        "feature_values": features(lines, language),
        "eligible_path_count": len(paths),
        "path_type_counts": _path_type_counts(paths),
        "materialization_design": "complete_raw_quarter_diff",
        "code_age_days": 0,
    }


def _path_type_counts(paths: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for path in paths:
        lowered = path.lower()
        name = lowered.rsplit("/", 1)[-1]
        is_test = (
            "/test" in f"/{lowered}"
            or "/tests/" in f"/{lowered}/"
            or name.startswith("test_")
            or name.endswith(("_test.py", "_test.go"))
        )
        counts["test" if is_test else "source"] += 1
    return dict(sorted(counts.items()))


def _panel_repository(
    plan_row: Mapping[str, Any],
    periods: Sequence[int],
    values: Mapping[tuple[str, int], Mapping[str, Any]],
    feature_id: str,
) -> dict[str, Any]:
    repository_id = plan_row["repository_id"]
    return {
        **{
            key: plan_row[key]
            for key in (
                "repository_id",
                "language",
                "role",
                "adoption_period",
                "policy_effective_period",
            )
        },
        "observations": [
            {
                "period": period,
                "calendar_period": period,
                "feature_value": values[(repository_id, period)]["feature_values"][
                    feature_id
                ],
                "change_size": values[(repository_id, period)][
                    "introduced_code_line_count"
                ],
                "code_age_days": values[(repository_id, period)].get(
                    "code_age_days", 0
                ),
                "path_type_counts": values[(repository_id, period)].get(
                    "path_type_counts", {}
                ),
            }
            for period in periods
            if (repository_id, period) in values
        ],
    }


def _feature_panel(
    plan: Mapping[str, Any],
    values: Mapping[tuple[str, int], Mapping[str, Any]],
    feature_id: str,
) -> dict[str, Any]:
    return {
        "panel_version": 1,
        "feature_id": feature_id,
        "estimand_id": "whole_repository_adoption_effect",
        "unit": "repository_calendar_period_introduced_code",
        "repositories": [
            _panel_repository(row, plan["required_periods"], values, feature_id)
            for row in plan["repositories"]
        ],
    }


def build_feature_panels(
    plan: Mapping[str, Any],
    period_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one complete DiD panel per frozen code-style feature."""
    indexed = _validated_periods(plan, period_results)
    values = {}
    summaries = []
    for key, row in sorted(indexed.items()):
        if row.get("observed", True):
            values[key] = row
        summaries.append(
            {
                "repository_id": key[0],
                "period": key[1],
                "base_oid": row["base_oid"],
                "head_oid": row["head_oid"],
                "raw_diff_sha256": row["raw_diff_sha256"],
                "introduced_code_line_count": row["introduced_code_line_count"],
                "eligible_path_count": row.get("eligible_path_count"),
                "path_type_counts": row.get("path_type_counts", {}),
                "materialization_design": row.get("materialization_design"),
                "code_age_days": row.get("code_age_days", 0),
                "observed": row.get("observed", True),
                "observation_status": row.get("observation_status", "observed"),
            }
        )
    panels = {name: _feature_panel(plan, values, name) for name in NAMES}
    document = {
        "era_panel_materialization_version": 1,
        "era_panel_plan_sha256": plan["era_panel_plan_sha256"],
        "status": "complete",
        "period_result_count": len(summaries),
        "period_results": summaries,
        "feature_count": len(panels),
        "panels": panels,
    }
    return {**document, "era_panel_materialization_sha256": _sha256(document)}


def _unit_document(
    repository_id: str,
    commit_oid: str,
    path: str,
    records: Sequence[Mapping[str, Any]],
    authorship_role: str,
    evidence_tier: str,
) -> dict[str, Any]:
    normalized = "\n".join(record["text"].rstrip() for record in records)
    identity = {
        "repository_id": repository_id,
        "commit_oid": commit_oid,
        "path": path,
        "line_numbers": [record["line_number"] for record in records],
    }
    return {
        **{key: identity[key] for key in ("repository_id", "commit_oid", "path")},
        "hunk_sha256": _sha256(identity),
        "content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "line_count": len(records),
        "authorship_role": authorship_role,
        "evidence_tier": evidence_tier,
    }


def materialize_exact_commit_units(
    *,
    repository_id: str,
    commit_oid: str,
    language: str,
    raw_diff: str,
    authorship_role: str,
    evidence_tier: str,
) -> list[dict[str, Any]]:
    """Materialize exact commit-file units for the authorship-only estimand."""
    if (
        len(commit_oid) != 40
        or any(character not in "0123456789abcdef" for character in commit_oid)
        or authorship_role not in {"agent", "human"}
    ):
        raise EraPanelError("exact authorship unit identity is invalid")
    by_path: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in parse_added_patch(raw_diff, language):
        by_path[record["path"]].append(record)
    return [
        _unit_document(
            repository_id,
            commit_oid,
            path,
            records,
            authorship_role,
            evidence_tier,
        )
        for path, records in sorted(by_path.items())
    ]
