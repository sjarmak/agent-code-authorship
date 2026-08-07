"""Freeze blinded Sourcegraph candidate reviews into reproducible catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from authorship.sourcegraph_discovery import (
    validate_adjudication_bundle,
    validate_discovery_specification,
)
from authorship.sourcegraph_evidence_pipeline import validate_packet_index

FREEZE_VERSION = 3
CATALOG_TYPES = {
    "adoption_event": "adoption_events",
    "ai_ban_policy": "ai_ban_policies",
}
PRIMARY_DECISIONS = {
    "adoption_event": frozenset({"confirmed"}),
    "ai_ban_policy": frozenset({"admissible"}),
}
SENSITIVITY_DECISIONS = {
    "adoption_event": frozenset({"confirmed", "observed"}),
    "ai_ban_policy": frozenset({"admissible"}),
}
BUNDLE_FIELDS = {
    "bundle_version",
    "packet_id",
    "packet_sha256",
    "packet_type",
    "reviews",
    "bundle_sha256",
}
REVIEW_FIELDS = {
    "stage",
    "reviewer_id",
    "reviewer_kind",
    "reviewer_version",
    "decision",
    "outcome_blind",
    "peer_review_blind",
    "rationale",
}
MODEL_REVIEW_FIELDS = {
    "provider",
    "model_id",
    "model_version",
    "prompt_sha256",
}


class AdjudicationFreezeError(ValueError):
    """Raised when review artifacts cannot support a complete freeze."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _sha_without(document: Mapping[str, Any], field: str) -> str:
    return _content_sha256(
        {key: value for key, value in document.items() if key != field}
    )


def adjudication_bundle_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "bundle_sha256")


def adjudication_freeze_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "adjudication_freeze_sha256")


def _catalog_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "catalog_sha256")


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdjudicationFreezeError(f"{field} must be an object")
    return value


def _required_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AdjudicationFreezeError(f"{field} must be a list")
    return value


def _bundle_map(
    bundles: Sequence[Mapping[str, Any]], packets: Sequence[Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    by_packet: dict[str, Mapping[str, Any]] = {}
    for raw_bundle in bundles:
        bundle = _required_mapping(raw_bundle, "adjudication bundle")
        packet_id = bundle.get("packet_id")
        if not isinstance(packet_id, str) or packet_id in by_packet:
            raise AdjudicationFreezeError(
                "exactly one adjudication bundle per packet is required"
            )
        by_packet[packet_id] = bundle
    packet_ids = [packet["packet_id"] for packet in packets]
    if set(by_packet) != set(packet_ids) or len(by_packet) != len(packet_ids):
        raise AdjudicationFreezeError(
            "exactly one adjudication bundle per packet is required"
        )
    return by_packet


def _review_list(bundle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_reviews = _required_list(bundle.get("reviews"), "reviews")
    if any(not isinstance(review, Mapping) for review in raw_reviews):
        raise AdjudicationFreezeError("every review must be an object")
    for review in raw_reviews:
        allowed = REVIEW_FIELDS | (
            MODEL_REVIEW_FIELDS if review.get("reviewer_kind") == "model" else set()
        )
        if set(review) - allowed:
            raise AdjudicationFreezeError("review contains unapproved fields")
    return raw_reviews


def _validate_bundle(
    bundle: Mapping[str, Any],
    packet: Mapping[str, Any],
    specification: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if set(bundle) != BUNDLE_FIELDS:
        raise AdjudicationFreezeError("adjudication bundle contains unapproved fields")
    if bundle.get("bundle_version") != FREEZE_VERSION:
        raise AdjudicationFreezeError("bundle_version must equal 3")
    if bundle.get("bundle_sha256") != adjudication_bundle_sha256(bundle):
        raise AdjudicationFreezeError("bundle_sha256 does not match")
    for field in ("packet_id", "packet_sha256", "packet_type"):
        if bundle.get(field) != packet.get(field):
            raise AdjudicationFreezeError(f"adjudication bundle {field} does not match")
    reviews = _review_list(bundle)
    errors = validate_adjudication_bundle(bundle, specification)
    if errors:
        raise AdjudicationFreezeError("; ".join(errors))
    return reviews


def _final_review(
    reviews: Sequence[Mapping[str, Any]],
) -> tuple[str, str, Mapping[str, Any] | None]:
    primary = [review for review in reviews if review["stage"] == "primary"]
    if primary[0]["decision"] == primary[1]["decision"]:
        audit = next(
            (review for review in reviews if review["stage"] == "agreement_audit"),
            None,
        )
        return primary[0]["decision"], "primary_agreement", audit
    resolution = next(review for review in reviews if review["stage"] == "resolution")
    return resolution["decision"], "resolution", None


def _catalog_record(
    packet: Mapping[str, Any],
    bundle: Mapping[str, Any],
    reviews: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    decision, basis, audit = _final_review(reviews)
    packet_type = packet["packet_type"]
    audit_decision = audit["decision"] if audit is not None else None
    return {
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["packet_sha256"],
        "bundle_sha256": bundle["bundle_sha256"],
        "packet_type": packet_type,
        "canonical_repository_id": packet["canonical_repository_id"],
        "canonical_source_url": packet["canonical_source_url"],
        "query_family_id": packet["query_family_id"],
        "candidate_event": packet["candidate_event"],
        "final_decision": decision,
        "final_basis": basis,
        "primary_analysis_included": decision in PRIMARY_DECISIONS[packet_type],
        "sensitivity_analysis_included": decision in SENSITIVITY_DECISIONS[packet_type],
        "agreement_audit_decision": audit_decision,
        "agreement_audit_matches_final": (
            audit_decision == decision if audit is not None else None
        ),
    }


def _catalog(name: str, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    document = {
        "catalog_type": name,
        "record_count": len(records),
        "primary_analysis_included_count": sum(
            record["primary_analysis_included"] for record in records
        ),
        "sensitivity_analysis_included_count": sum(
            record["sensitivity_analysis_included"] for record in records
        ),
        "records": list(records),
    }
    return {**document, "catalog_sha256": _catalog_sha256(document)}


def _audit_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "primary_agreement_count": sum(
            record["final_basis"] == "primary_agreement" for record in records
        ),
        "resolution_count": sum(
            record["final_basis"] == "resolution" for record in records
        ),
        "selected_agreement_audit_count": sum(
            record["agreement_audit_decision"] is not None for record in records
        ),
        "decision_mismatch_count": sum(
            record["agreement_audit_matches_final"] is False for record in records
        ),
    }


def build_adjudication_freeze(
    specification: Mapping[str, Any],
    packet_index: Mapping[str, Any],
    adjudication_bundles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze complete blinded reviews; never infer decisions from evidence."""
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise AdjudicationFreezeError("; ".join(specification_errors))
    index_errors = validate_packet_index(packet_index, specification)
    if index_errors:
        raise AdjudicationFreezeError("; ".join(index_errors))
    if packet_index.get("pending_file_enrichment_count") != 0:
        raise AdjudicationFreezeError(
            "pending file enrichments block adjudication freeze"
        )
    packets = _required_list(packet_index.get("packets"), "packets")
    bundles = _bundle_map(adjudication_bundles, packets)
    records = []
    for packet in packets:
        bundle = bundles[packet["packet_id"]]
        reviews = _validate_bundle(bundle, packet, specification)
        records.append(_catalog_record(packet, bundle, reviews))
    catalogs = {
        name: _catalog(name, [r for r in records if r["packet_type"] == packet_type])
        for packet_type, name in CATALOG_TYPES.items()
    }
    document = {
        "adjudication_freeze_version": FREEZE_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "packet_index_sha256": packet_index["packet_index_sha256"],
        "packet_count": len(packets),
        "bundle_sha256s": sorted(
            bundle["bundle_sha256"] for bundle in bundles.values()
        ),
        "catalogs": catalogs,
        "agreement_audit": _audit_summary(records),
        "outcomes_consulted": False,
    }
    return {
        **document,
        "adjudication_freeze_sha256": adjudication_freeze_sha256(document),
    }


def _record_order(record: Mapping[str, Any]) -> tuple[Any, ...]:
    event = record.get("candidate_event")
    event = event if isinstance(event, Mapping) else {}
    return (
        event.get("observed_at", ""),
        event.get("commit_oid", ""),
        record.get("query_family_id", ""),
        record.get("packet_id", ""),
    )


def _catalog_errors(
    name: str, catalog: Any, packet_type: str, specification: Mapping[str, Any]
) -> tuple[list[str], list[Mapping[str, Any]]]:
    if not isinstance(catalog, Mapping):
        return [f"{name} catalog must be an object"], []
    records = catalog.get("records")
    if not isinstance(records, list) or any(
        not isinstance(record, Mapping) for record in records
    ):
        return [f"{name} catalog records must be objects"], []
    errors = []
    if catalog.get("catalog_type") != name:
        errors.append(f"{name} catalog_type does not match")
    if catalog.get("catalog_sha256") != _catalog_sha256(catalog):
        errors.append(f"{name} catalog checksum does not match")
    if catalog.get("record_count") != len(records):
        errors.append(f"{name} record_count does not match")
    if records != sorted(records, key=_record_order):
        errors.append(f"{name} records are not in frozen order")
    errors.extend(_record_errors(name, records, packet_type, specification))
    expected_primary = sum(
        record.get("primary_analysis_included") is True for record in records
    )
    expected_sensitivity = sum(
        record.get("sensitivity_analysis_included") is True for record in records
    )
    if catalog.get("primary_analysis_included_count") != expected_primary:
        errors.append(f"{name} primary_analysis_included_count does not match")
    if catalog.get("sensitivity_analysis_included_count") != expected_sensitivity:
        errors.append(f"{name} sensitivity_analysis_included_count does not match")
    return errors, records


def _record_errors(
    name: str,
    records: Sequence[Mapping[str, Any]],
    packet_type: str,
    specification: Mapping[str, Any],
) -> list[str]:
    errors = []
    decisions = specification["adjudication"]["decision_sets"][packet_type]
    if len({record.get("packet_id") for record in records}) != len(records):
        errors.append(f"{name} packet IDs must be unique")
    if any(record.get("packet_type") != packet_type for record in records):
        errors.append(f"{name} packet_type does not match")
    if any(record.get("final_decision") not in decisions for record in records):
        errors.append(f"{name} final_decision is invalid")
    if any(
        record.get("final_basis") not in {"primary_agreement", "resolution"}
        for record in records
    ):
        errors.append(f"{name} final_basis is invalid")
    if any(_audit_fields_inconsistent(record, decisions) for record in records):
        errors.append(f"{name} agreement audit fields are inconsistent")
    return errors + _inclusion_errors(name, records, packet_type)


def _audit_fields_inconsistent(
    record: Mapping[str, Any], decisions: Sequence[str]
) -> bool:
    audit_decision = record.get("agreement_audit_decision")
    audit_matches = record.get("agreement_audit_matches_final")
    if audit_decision is None:
        return audit_matches is not None
    return (
        record.get("final_basis") != "primary_agreement"
        or audit_decision not in decisions
        or audit_matches != (audit_decision == record.get("final_decision"))
    )


def _inclusion_errors(
    name: str,
    records: Sequence[Mapping[str, Any]],
    packet_type: str,
) -> list[str]:
    errors = []
    if any(
        record.get("primary_analysis_included")
        != (record.get("final_decision") in PRIMARY_DECISIONS[packet_type])
        for record in records
    ):
        errors.append(f"{name} primary inclusion does not match final decision")
    if any(
        record.get("sensitivity_analysis_included")
        != (record.get("final_decision") in SENSITIVITY_DECISIONS[packet_type])
        for record in records
    ):
        errors.append(f"{name} sensitivity inclusion does not match final decision")
    return errors


def _summary_errors(
    document: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> list[str]:
    errors = []
    summary = document.get("agreement_audit")
    if not isinstance(summary, Mapping):
        return ["agreement_audit must be an object"]
    expected = _audit_summary(records)
    if dict(summary) != expected:
        errors.append("agreement_audit summary does not match records")
    if document.get("packet_count") != len(records):
        errors.append("packet_count does not match catalogs")
    bundle_sha256s = document.get("bundle_sha256s")
    if not isinstance(bundle_sha256s, list):
        errors.append("bundle_sha256s must be a list")
    elif bundle_sha256s != sorted({record.get("bundle_sha256") for record in records}):
        errors.append("bundle_sha256s do not match catalogs")
    return errors


def validate_adjudication_freeze(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    """Validate a frozen catalog without trusting nested artifact shapes."""
    if not isinstance(document, Mapping):
        return ["adjudication freeze must be an object"]
    errors = []
    if document.get("adjudication_freeze_version") != FREEZE_VERSION:
        errors.append("adjudication_freeze_version must equal 3")
    if document.get("specification_sha256") != specification.get(
        "specification_sha256"
    ):
        errors.append("specification_sha256 does not match")
    if document.get("outcomes_consulted") is not False:
        errors.append("adjudication freeze must be outcome blind")
    if document.get("adjudication_freeze_sha256") != adjudication_freeze_sha256(
        document
    ):
        errors.append("adjudication_freeze_sha256 does not match")
    catalogs = document.get("catalogs")
    if not isinstance(catalogs, Mapping):
        return errors + ["catalogs must be an object"]
    records = []
    for packet_type, name in CATALOG_TYPES.items():
        catalog_errors, catalog_records = _catalog_errors(
            name, catalogs.get(name), packet_type, specification
        )
        errors.extend(catalog_errors)
        records.extend(catalog_records)
    errors.extend(_summary_errors(document, records))
    return errors
