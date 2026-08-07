"""Freeze the outcome-blind matched-control design before outcome extraction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ControlProtocolError(ValueError):
    """A frozen matched-control research contract was violated."""


FORBIDDEN_OUTCOME_ARTIFACTS = {
    "results/semantic-topology-record-summary.v1.json",
    "results/semantic-topology-utility-gates.v1.json",
    "results/survival-estimates.v2.json",
}


def canonical_sha256(document: dict[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "protocol_sha256"
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_protocol(root: Path) -> dict[str, Any]:
    inputs = [
        "study/semantic-topology-pilot-plan.v1.json",
        "study/survival-candidates.v1.json",
    ]
    document: dict[str, Any] = {
        "protocol_version": 1,
        "purpose": "Outcome-blind within-repository task-matched controls for semantic topology",
        "input_artifacts": inputs,
        "input_sha256": {item: _sha256(root / item) for item in inputs},
        "unit": "introduced_diff_hunk",
        "treated_identity_construction": {
            "method": "recompute_hunks_from_frozen_introducing_commit_and_parent",
            "followup_artifacts_read": False,
            "expected_count_is_not_a_selection_constraint": True,
        },
        "exposure": {
            "treated": "confirmed_agent_attributed_introducing_commit",
            "control": "not_in_frozen_agent_commit_exclusion_set",
            "interpretation": "non_agent_attributed_not_proven_human",
        },
        "outcome_blindness": {
            "selection_stage_may_read_followup_history": False,
            "forbidden_match_inputs": [
                "subsequent_change_present",
                "time_to_first_followup_days",
                "first_followup_author_differs",
                "distinct_followup_author_count",
            ],
            "outcomes_unsealed_after": "matched_cohort_sha256_is_written",
        },
        "eligibility": {
            "primary_scope": "same_repository",
            "commit_window_days": 180,
            "first_parent_only": True,
            "exclude_merges": True,
            "exclude_known_agent_commits": True,
            "require_ancestor_of_cutoff": True,
            "minimum_followup_days": 0,
            "admissible_path_classes": ["source", "test", "documentation"],
        },
        "covariates": [
            "repository",
            "path_class",
            "task_class",
            "language",
            "log1p_added_lines",
            "log1p_deleted_lines",
            "file_age_days",
            "prior_path_commit_count_180d",
            "repository_commit_count_30d",
            "calendar_distance_days",
        ],
        "task_classification": {
            "method": "deterministic_commit_subject_and_path_rules",
            "classes": [
                "bug_fix",
                "documentation",
                "test",
                "refactor",
                "feature",
                "maintenance",
                "other",
            ],
            "fallback": "other",
        },
        "matching": {
            "method": "exact_plus_robust_scaled_euclidean_nearest_neighbor",
            "exact": ["repository", "path_class", "task_class"],
            "fallback_exact": ["repository", "path_class"],
            "ratio": 1,
            "replacement": False,
            "caliper": 2.5,
            "tie_breaker": "sha256_candidate_id",
            "seed": 20260729,
            "sensitivity_calipers": [1.5, 2.0, 3.0],
        },
        "observation": {
            "cutoff_rule": "treated_repository_frozen_cutoff",
            "minimum_followup_days": 0,
            "horizon_days": 180,
            "time_estimand": "restricted_mean_time_to_first_followup",
            "censoring": "right_censored_at_common_cutoff_or_180_days",
        },
        "estimands": [
            "risk_difference_subsequent_change_by_180d",
            "restricted_mean_time_to_first_followup_difference_180d",
            "risk_difference_first_followup_author_differs",
            "mean_difference_distinct_followup_author_count_180d",
        ],
        "uncertainty": {
            "method": "repository_cluster_bootstrap",
            "replicates": 2000,
            "confidence": 0.95,
            "seed": 20260729,
        },
        "identification_gates": {
            "maximum_absolute_smd": 0.1,
            "minimum_matched_fraction": 0.7,
            "minimum_effective_sample_size": 100,
            "minimum_repositories": 3,
            "require_all_exact_strata_overlap": False,
            "failure_result": "not_identified",
        },
        "sensitivity": {
            "leave_one_repository_out": True,
            "alternate_calipers": True,
            "fallback_task_match_reported_separately": True,
        },
    }
    document["protocol_sha256"] = canonical_sha256(document)
    return document


def validate_protocol(
    document: dict[str, Any], root: Path, *, raise_on_missing: bool = False
) -> list[str]:
    errors: list[str] = []
    if document.get("protocol_sha256") != canonical_sha256(document):
        errors.append("protocol_sha256 mismatch")
    if FORBIDDEN_OUTCOME_ARTIFACTS.intersection(document.get("input_artifacts", [])):
        errors.append("protocol includes forbidden outcome artifact")
    for relative, expected in document.get("input_sha256", {}).items():
        path = root / relative
        if not path.exists():
            if raise_on_missing:
                raise ControlProtocolError(f"missing protocol input: {relative}")
            errors.append(f"missing protocol input: {relative}")
        elif _sha256(path) != expected:
            errors.append(f"protocol input digest mismatch: {relative}")
    if (
        document.get("outcome_blindness", {}).get(
            "selection_stage_may_read_followup_history"
        )
        is not False
    ):
        errors.append("matching must be outcome blind")
    return errors


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output", type=Path, default=Path("study/semantic-control-protocol.v1.json")
    )
    args = parser.parse_args()
    document = freeze_protocol(args.root)
    errors = validate_protocol(document, args.root)
    if errors:
        raise ControlProtocolError("; ".join(errors))
    _write(args.output, document)
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
