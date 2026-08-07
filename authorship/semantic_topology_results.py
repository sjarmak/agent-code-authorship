"""Assemble candidate-linked records and preregistered pilot utility gates."""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from typing import Any, Iterable

from authorship.semantic_topology_protocol import canonical_sha256

FINAL_MISSINGNESS_REASONS = {
    "pending_blinded_adjudication": "unavailable_no_blinded_task_adjudication",
    "pending_sourcegraph_code_navigation_verification": (
        "unavailable_no_record_keyed_deterministic_navigation"
    ),
    "pending_cutoff_codeowners_verification": (
        "unavailable_no_record_keyed_codeowners_verification"
    ),
    "pending_repository_held_out_matching": (
        "unavailable_no_validated_repository_held_out_match"
    ),
    "pending_cross_repository_search_verification": (
        "unavailable_no_deterministically_verified_cross_repository_match"
    ),
    "pending_field_reliability_estimation": (
        "unavailable_no_field_reliability_estimate"
    ),
}


def link_candidate_evidence(
    records: Iterable[dict[str, Any]],
    candidate_inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Link Deep Search candidates without promoting them to analytic evidence."""
    by_repository: dict[str, list[str]] = defaultdict(list)
    for candidate in candidate_inventory.get("candidates", []):
        if candidate.get("verification_status") != "candidate":
            continue
        by_repository[candidate["canonical_repository_id"]].append(
            candidate["candidate_id"]
        )
    enriched = copy.deepcopy(list(records))
    for record in enriched:
        repository_id = record["identity"]["canonical_repository_id"]
        record["deep_search_evidence_ids"] = sorted(
            set(by_repository.get(repository_id, []))
        )
    return enriched


def attach_reliability_uncertainty(
    records: Iterable[dict[str, Any]],
    primary_reliability: dict[str, Any],
    followup_count_reliability: dict[str, Any],
    distinct_author_reliability: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Attach frozen dataset-level reliability without inventing record labels."""
    enriched = copy.deepcopy(list(records))
    field_reliability = copy.deepcopy(primary_reliability.get("fields", {}))
    field_reliability["followup_commit_count"] = {
        key: followup_count_reliability.get(key)
        for key in ("observations", "coverage", "kappa", "agreement", "passes")
    }
    if distinct_author_reliability:
        field_reliability["distinct_followup_author_count"] = {
            key: distinct_author_reliability.get(key)
            for key in ("observations", "coverage", "kappa", "agreement", "passes")
        }
    for record in enriched:
        record["uncertainty"] = {
            "status": "observed",
            "evidence_routes": ["pinned_git"],
            "scope": "dataset_level_reliability_sample",
            "review_mode": "independent_blinded_agent_replication",
            "primary_result_sha256": primary_reliability.get("result_sha256"),
            "followup_count_result_sha256": followup_count_reliability.get(
                "result_sha256"
            ),
            "distinct_author_result_sha256": (distinct_author_reliability or {}).get(
                "result_sha256"
            ),
            "field_reliability": field_reliability,
        }
    return enriched


def finalize_record_missingness(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace provisional pending reasons with final typed missingness."""
    finalized = copy.deepcopy(list(records))
    for record in finalized:
        for field in record.values():
            if not isinstance(field, dict) or field.get("status") != "unavailable":
                continue
            reason = field.get("reason")
            if reason in FINAL_MISSINGNESS_REASONS:
                field["reason"] = FINAL_MISSINGNESS_REASONS[reason]
    return finalized


def build_record_summary(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build checksummed descriptive findings from admitted record fields."""
    records = list(records)
    path_classes = Counter(
        record["tests_and_docs"].get("path_class")
        for record in records
        if record["tests_and_docs"].get("status") == "observed"
        and record["tests_and_docs"].get("path_class") is not None
    )
    distinct_authors = Counter(
        record["human_assimilation"].get("distinct_followup_author_count")
        for record in records
        if record["human_assimilation"].get("status") == "observed"
        and record["human_assimilation"].get("distinct_followup_author_count")
        is not None
    )
    summary: dict[str, Any] = {
        "summary_version": 1,
        "record_count": len(records),
        "repository_count": len(
            {record["identity"]["canonical_repository_id"] for record in records}
        ),
        "path_class_counts": dict(sorted(path_classes.items())),
        "subsequent_change_present_count": sum(
            record["subsequent_changes"].get("status") == "observed"
            and record["subsequent_changes"].get("commit_count", 0) > 0
            for record in records
        ),
        "first_followup_author_differs_count": sum(
            record["human_assimilation"].get("status") == "observed"
            and record["human_assimilation"].get("first_followup_author_differs", False)
            for record in records
        ),
        "distinct_followup_author_count_distribution": {
            str(value): count for value, count in sorted(distinct_authors.items())
        },
        "outcomes_consulted": False,
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    return summary


def build_run_summary(
    runs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize preserved Deep Search evidence and API observability."""
    runs = list(runs)
    summary: dict[str, Any] = {
        "summary_version": 1,
        "run_count": len(runs),
        "terminal_status_counts": dict(
            sorted(Counter(run["terminal_status"] for run in runs).items())
        ),
        "conversation_identity_present_count": sum(
            bool(run.get("conversation_identity")) for run in runs
        ),
        "raw_answer_present_count": sum(bool(run.get("raw_answer")) for run in runs),
        "search_trace_status_counts": dict(
            sorted(Counter(run["search_trace_status"] for run in runs).items())
        ),
        "model_metadata_status_counts": dict(
            sorted(
                Counter(
                    run.get("model_metadata", {}).get("status", "unavailable")
                    for run in runs
                ).items()
            )
        ),
        "cited_file_count": sum(len(run.get("cited_files", [])) for run in runs),
        "proposed_query_count": sum(
            len(run.get("proposed_queries", [])) for run in runs
        ),
    }
    summary["summary_sha256"] = canonical_sha256(summary)
    return summary


def _optional_gate(
    result: dict[str, Any] | None,
    *,
    required_keys: tuple[str, ...],
) -> dict[str, Any]:
    if result is None:
        return {
            "status": "not_identified",
            "passes": False,
            "reason": "validated_result_unavailable",
        }
    passes = bool(result.get("passes")) and all(key in result for key in required_keys)
    return {
        **result,
        "status": "validated" if passes else "not_identified",
        "passes": passes,
    }


def assemble_utility_gates(
    primary_reliability: dict[str, Any],
    followup_count_reliability: dict[str, Any],
    distinct_author_reliability: dict[str, Any] | None,
    discovery_result: dict[str, Any] | None,
    matched_control_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute the frozen pilot gates without relaxing any threshold."""
    passing_fields = sorted(
        field
        for field, result in primary_reliability.get("fields", {}).items()
        if result.get("passes")
    )
    distinct_author_reliability = distinct_author_reliability or {}
    distinct_author_passes = (
        distinct_author_reliability.get("passes") is True
        and distinct_author_reliability.get("coverage", 0) >= 0.80
        and distinct_author_reliability.get("observations", 0) >= 50
        and distinct_author_reliability.get("kappa") is not None
        and distinct_author_reliability["kappa"] >= 0.70
    )
    if distinct_author_passes:
        passing_fields.append("distinct_followup_author_count")
    passing_fields.sort()
    topology_passes = (
        primary_reliability.get("coverage", 0) >= 0.80 and len(passing_fields) >= 3
    )
    gates = {
        "new_discovery_family": _optional_gate(
            discovery_result,
            required_keys=("precision", "reviewed_hits", "repository_count"),
        ),
        "task_matched_controls": _optional_gate(
            matched_control_result,
            required_keys=("coverage", "kappa"),
        ),
        "reliable_topology_fields": {
            "status": "validated" if topology_passes else "not_identified",
            "passes": topology_passes,
            "passing_fields": passing_fields,
            "required_field_count": 3,
            "minimum_observability": 0.80,
            "minimum_kappa": 0.70,
            "minimum_records": 50,
            "excluded_redundant_fields": ["followup_commit_count"],
            "primary_reliability_result_sha256": primary_reliability.get(
                "result_sha256"
            ),
            "followup_count_result_sha256": followup_count_reliability.get(
                "result_sha256"
            ),
            "distinct_author_result_sha256": distinct_author_reliability.get(
                "result_sha256"
            ),
        },
    }
    result: dict[str, Any] = {
        "result_version": 1,
        "gates": gates,
        "pilot_result": (
            "validated_utility"
            if any(gate["passes"] for gate in gates.values())
            else "not_identified"
        ),
    }
    result["result_sha256"] = canonical_sha256(result)
    return result
