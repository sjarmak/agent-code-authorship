"""Outcome-blind reliability review packets for semantic topology fields."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from authorship.semantic_topology_protocol import canonical_sha256

FIELDS = (
    "path_class",
    "subsequent_change_present",
    "first_followup_author_differs",
    "cutoff_reachable",
)


def build_blinded_packet(
    records: Iterable[dict[str, Any]],
    *,
    per_repository: int = 10,
    target_items: int | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["identity"]["canonical_repository_id"]].append(record)
    items = []
    for repository_id in sorted(grouped):
        selected = sorted(
            grouped[repository_id], key=lambda item: item["identity"]["hunk_id"]
        )[:per_repository]
        for record in selected:
            items.append(_review_item(record))
    if target_items and len(items) < target_items:
        selected_ids = {item["review_item_id"] for item in items}
        supplements = sorted(
            (
                record
                for records_in_repository in grouped.values()
                for record in records_in_repository
                if record["identity"]["hunk_id"] not in selected_ids
            ),
            key=lambda record: record["identity"]["hunk_id"],
        )
        for record in supplements[: target_items - len(items)]:
            items.append(_review_item(record))
        items.sort(key=lambda item: item["review_item_id"])
    packet: dict[str, Any] = {
        "packet_version": 1,
        "outcomes_consulted": False,
        "blinding": {
            "provenance_class_hidden": True,
            "agent_family_hidden": True,
            "deep_search_answers_hidden": True,
            "survival_outcomes_hidden": True,
            "peer_decisions_hidden": True,
        },
        "item_count": len(items),
        "items": items,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def _review_item(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_item_id": record["identity"]["hunk_id"],
        "identity": record["identity"],
        "fields_to_review": {
            "path_class": "source, test, or documentation from the pinned path",
            "subsequent_change_present": (
                "whether any commit in introducing..cutoff touches path"
            ),
            "first_followup_author_differs": (
                "whether first follow-up commit name/email differs from "
                "the introducing commit; do not infer human identity"
            ),
            "cutoff_reachable": ("whether introducing commit is an ancestor of cutoff"),
        },
    }


def build_followup_count_packet(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build an outcome-blind packet for independently counting follow-up commits."""
    items = [
        {
            "review_item_id": record["identity"]["hunk_id"],
            "identity": record["identity"],
            "field_to_review": {
                "followup_commit_count": (
                    "exact number of commits in introducing..cutoff that touch path"
                )
            },
        }
        for record in sorted(records, key=lambda item: item["identity"]["hunk_id"])
    ]
    packet: dict[str, Any] = {
        "packet_version": 1,
        "outcomes_consulted": False,
        "blinding": {
            "provenance_class_hidden": True,
            "agent_family_hidden": True,
            "deep_search_answers_hidden": True,
            "survival_outcomes_hidden": True,
            "peer_decisions_hidden": True,
        },
        "item_count": len(items),
        "items": items,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def build_distinct_author_count_packet(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a blinded packet for counting distinct follow-up author identities."""
    items = [
        {
            "review_item_id": record["identity"]["hunk_id"],
            "identity": record["identity"],
            "field_to_review": {
                "distinct_followup_author_count": (
                    "exact number of distinct normalized author name/email pairs "
                    "among commits in introducing..cutoff that touch path"
                )
            },
        }
        for record in sorted(records, key=lambda item: item["identity"]["hunk_id"])
    ]
    packet: dict[str, Any] = {
        "packet_version": 1,
        "outcomes_consulted": False,
        "blinding": {
            "provenance_class_hidden": True,
            "agent_family_hidden": True,
            "deep_search_answers_hidden": True,
            "survival_outcomes_hidden": True,
            "peer_decisions_hidden": True,
        },
        "item_count": len(items),
        "items": items,
    }
    packet["packet_sha256"] = canonical_sha256(packet)
    return packet


def _cohen_kappa(left: list[Any], right: list[Any]) -> tuple[float | None, float]:
    if not left:
        return None, 0.0
    agreement = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    left_counts = Counter(left)
    right_counts = Counter(right)
    categories = set(left_counts) | set(right_counts)
    expected = sum(
        (left_counts[value] / len(left)) * (right_counts[value] / len(right))
        for value in categories
    )
    if expected == 1:
        return None, agreement
    return (agreement - expected) / (1 - expected), agreement


def evaluate_reviews(
    packet: dict[str, Any],
    review_a: dict[str, Any],
    review_b: dict[str, Any],
) -> dict[str, Any]:
    if review_a.get("reviewer_id") == review_b.get("reviewer_id"):
        raise ValueError("reliability review requires distinct reviewers")
    expected_ids = {item["review_item_id"] for item in packet["items"]}
    left = {item["review_item_id"]: item for item in review_a.get("decisions", [])}
    right = {item["review_item_id"]: item for item in review_b.get("decisions", [])}
    common_ids = sorted(expected_ids & set(left) & set(right))
    missing = sorted(expected_ids - set(common_ids))
    fields = {}
    passing = 0
    for field in FIELDS:
        left_values = [left[identifier].get(field) for identifier in common_ids]
        right_values = [right[identifier].get(field) for identifier in common_ids]
        kappa, agreement = _cohen_kappa(left_values, right_values)
        observed = len(common_ids)
        field_passes = (
            observed >= 50 and kappa is not None and kappa >= 0.70 and agreement >= 0.80
        )
        passing += int(field_passes)
        fields[field] = {
            "observations": observed,
            "kappa": kappa,
            "agreement": agreement,
            "passes": field_passes,
            "kappa_status": (
                "identified"
                if kappa is not None
                else "not_identified_no_label_variation"
            ),
        }
    coverage = len(common_ids) / len(expected_ids) if expected_ids else 0.0
    result: dict[str, Any] = {
        "result_version": 1,
        "packet_sha256": packet["packet_sha256"],
        "reviewer_ids": sorted([review_a["reviewer_id"], review_b["reviewer_id"]]),
        "coverage": coverage,
        "missing_review_item_ids": missing,
        "fields": fields,
        "field_count_passing": passing,
        "reliable_topology_fields_gate": coverage >= 0.80 and passing >= 3,
    }
    result["result_sha256"] = canonical_sha256(result)
    return result


def evaluate_followup_count_reviews(
    packet: dict[str, Any],
    review_a: dict[str, Any],
    review_b: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate exact-count agreement from two independent blinded reviewers."""
    if review_a.get("reviewer_id") == review_b.get("reviewer_id"):
        raise ValueError("reliability review requires distinct reviewers")
    expected_ids = {item["review_item_id"] for item in packet["items"]}
    left = {item["review_item_id"]: item for item in review_a.get("decisions", [])}
    right = {item["review_item_id"]: item for item in review_b.get("decisions", [])}
    common_ids = sorted(expected_ids & set(left) & set(right))
    missing = sorted(expected_ids - set(common_ids))
    left_values = [
        left[identifier].get("followup_commit_count") for identifier in common_ids
    ]
    right_values = [
        right[identifier].get("followup_commit_count") for identifier in common_ids
    ]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in left_values + right_values
    ):
        raise ValueError("followup_commit_count must be a non-negative integer")
    kappa, agreement = _cohen_kappa(left_values, right_values)
    observations = len(common_ids)
    passes = (
        observations >= 50 and kappa is not None and kappa >= 0.70 and agreement >= 0.80
    )
    result: dict[str, Any] = {
        "result_version": 1,
        "packet_sha256": packet["packet_sha256"],
        "reviewer_ids": sorted([review_a["reviewer_id"], review_b["reviewer_id"]]),
        "observations": observations,
        "coverage": observations / len(expected_ids) if expected_ids else 0.0,
        "missing_review_item_ids": missing,
        "kappa": kappa,
        "agreement": agreement,
        "kappa_status": (
            "identified" if kappa is not None else "not_identified_no_label_variation"
        ),
        "passes": passes,
    }
    result["result_sha256"] = canonical_sha256(result)
    return result


def evaluate_distinct_author_count_reviews(
    packet: dict[str, Any],
    review_a: dict[str, Any],
    review_b: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate exact agreement for distinct follow-up author counts."""
    return _evaluate_exact_count_reviews(
        packet,
        review_a,
        review_b,
        field="distinct_followup_author_count",
    )


def _evaluate_exact_count_reviews(
    packet: dict[str, Any],
    review_a: dict[str, Any],
    review_b: dict[str, Any],
    *,
    field: str,
) -> dict[str, Any]:
    if review_a.get("reviewer_id") == review_b.get("reviewer_id"):
        raise ValueError("reliability review requires distinct reviewers")
    expected_ids = {item["review_item_id"] for item in packet["items"]}
    left = {item["review_item_id"]: item for item in review_a.get("decisions", [])}
    right = {item["review_item_id"]: item for item in review_b.get("decisions", [])}
    common_ids = sorted(expected_ids & set(left) & set(right))
    missing = sorted(expected_ids - set(common_ids))
    left_values = [left[identifier].get(field) for identifier in common_ids]
    right_values = [right[identifier].get(field) for identifier in common_ids]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in left_values + right_values
    ):
        raise ValueError(f"{field} must be a non-negative integer")
    kappa, agreement = _cohen_kappa(left_values, right_values)
    observations = len(common_ids)
    passes = (
        observations >= 50 and kappa is not None and kappa >= 0.70 and agreement >= 0.80
    )
    result: dict[str, Any] = {
        "result_version": 1,
        "field": field,
        "packet_sha256": packet["packet_sha256"],
        "reviewer_ids": sorted([review_a["reviewer_id"], review_b["reviewer_id"]]),
        "observations": observations,
        "coverage": observations / len(expected_ids) if expected_ids else 0.0,
        "missing_review_item_ids": missing,
        "kappa": kappa,
        "agreement": agreement,
        "kappa_status": (
            "identified" if kappa is not None else "not_identified_no_label_variation"
        ),
        "passes": passes,
    }
    result["result_sha256"] = canonical_sha256(result)
    return result
