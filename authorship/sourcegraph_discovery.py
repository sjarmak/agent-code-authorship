"""Validation for frozen Sourcegraph discovery and adjudication artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

REQUIRED_EVIDENCE_CHANNELS = {
    "adoption_announcement",
    "agent_identity",
    "commit",
    "diff",
    "generated_code_disclosure",
    "policy",
    "revision_history",
    "trailer",
}
DISALLOWED_REQUIRED_CAPABILITIES = {"SCIP", "precise_code_intelligence"}
REPOSITORY_EXCLUSION_DISPOSITIONS = {
    "hold_private",
    "hold_redistribution",
}
PACKET_TYPES = {"adoption_event", "ai_ban_policy"}


def _canonical_sha256(document: Mapping[str, Any], excluded_field: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded_field}
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def specification_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "specification_sha256")


def evidence_packet_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "packet_sha256")


def _query_errors(
    family: Mapping[str, Any], allowed_capabilities: set[str]
) -> list[str]:
    family_id = family.get("id", "<missing>")
    errors = []
    query = family.get("query_template")
    if not isinstance(query, str) or "patternType:" not in query:
        errors.append(f"{family_id} query_template must declare patternType")
    capabilities = set(family.get("required_capabilities", []))
    if capabilities - allowed_capabilities:
        errors.append(f"{family_id} requires an unapproved capability")
    if capabilities & DISALLOWED_REQUIRED_CAPABILITIES:
        errors.append(f"{family_id} requires precise code intelligence")
    if family.get("saved_search_eligible") is not True:
        errors.append(f"{family_id} must be saved-search eligible")
    return errors


def _adjudication_spec_errors(adjudication: Mapping[str, Any]) -> list[str]:
    errors = []
    if adjudication.get("independent_primary_reviews") != 2:
        errors.append("exactly two independent primary reviews must be frozen")
    if adjudication.get("outcome_blind") is not True:
        errors.append("adjudication must be outcome blind")
    if adjudication.get("peer_review_blind") is not True:
        errors.append("primary reviewers must be blind to each other")
    agreement = adjudication.get("agreement_audit", {})
    rate = agreement.get("sample_basis_points")
    if not isinstance(rate, int) or not 0 < rate <= 10_000:
        errors.append("agreement audit sample_basis_points must be in 1..10000")
    if agreement.get("deterministic") is not True:
        errors.append("agreement audit sampling must be deterministic")
    if adjudication.get("disagreement_resolution", {}).get("reviewers") != 1:
        errors.append("disagreement resolution must require one reviewer")
    return errors


def _repository_exclusion_errors(execution: Any) -> list[str]:
    if not isinstance(execution, Mapping):
        return ["execution must be an object"]
    exclusions = execution.get("repository_exclusions")
    if exclusions is None:
        return []
    if not isinstance(exclusions, Mapping):
        return ["repository exclusions must be an object"]
    errors = []
    source_artifact = exclusions.get("source_artifact")
    if not isinstance(source_artifact, str) or not source_artifact:
        errors.append("repository exclusion source artifact path must be non-empty")
    source_sha256 = exclusions.get("source_artifact_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_sha256
    ):
        errors.append("repository exclusion source artifact SHA-256 is invalid")
    repositories = exclusions.get("repositories")
    if not isinstance(repositories, list):
        return [*errors, "repository exclusions repositories must be a list"]
    identifiers = []
    for repository in repositories:
        if not isinstance(repository, Mapping):
            errors.append("every repository exclusion must be an object")
            continue
        repository_id = repository.get("canonical_repository_id")
        if not isinstance(repository_id, str) or repository_id.count("/") != 1:
            errors.append("repository exclusion identifier is invalid")
        else:
            identifiers.append(repository_id)
        if repository.get("disposition") not in REPOSITORY_EXCLUSION_DISPOSITIONS:
            errors.append("repository exclusion disposition is invalid")
        reason = repository.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("repository exclusion reason must be non-empty")
    if len(identifiers) != len(set(identifiers)):
        errors.append("repository exclusion identifiers must be unique")
    return errors


def validate_discovery_specification(document: Mapping[str, Any]) -> list[str]:
    errors = []
    if document.get("specification_version") != 3:
        errors.append("specification_version must equal 3")
    if document.get("outcomes_consulted") is not False:
        errors.append("discovery specification must be outcome blind")
    if document.get("specification_sha256") != specification_sha256(document):
        errors.append("specification_sha256 does not match")
    sourcegraph = document.get("sourcegraph", {})
    allowed = set(sourcegraph.get("allowed_capabilities", []))
    if allowed & DISALLOWED_REQUIRED_CAPABILITIES:
        errors.append(
            "allowed capabilities cannot require SCIP or precise intelligence"
        )
    families = document.get("query_families")
    if not isinstance(families, list) or not families:
        return [*errors, "query_families must be a non-empty list"]
    identifiers = [family.get("id") for family in families]
    if len(identifiers) != len(set(identifiers)):
        errors.append("query family identifiers must be unique")
    channels = {
        channel
        for family in families
        for channel in family.get("evidence_channels", [])
    }
    if channels != REQUIRED_EVIDENCE_CHANNELS:
        errors.append("query families do not cover the required evidence channels")
    for family in families:
        errors.extend(_query_errors(family, allowed))
    errors.extend(_repository_exclusion_errors(document.get("execution")))
    errors.extend(_adjudication_spec_errors(document.get("adjudication", {})))
    return errors


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _packet_query_errors(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    family_ids = {family["id"] for family in specification["query_families"]}
    errors = []
    if document.get("query_family_id") not in family_ids:
        errors.append("query_family_id is not frozen in the discovery specification")
    rendered_query = document.get("rendered_query")
    if not isinstance(rendered_query, str) or "patternType:" not in rendered_query:
        errors.append("rendered_query must declare patternType")
    elif (
        document.get("rendered_query_sha256")
        != hashlib.sha256(rendered_query.encode()).hexdigest()
    ):
        errors.append("rendered_query_sha256 does not match")
    if isinstance(rendered_query, str):
        match = re.search(r"(?:^|\s)repo:\^(.+?)\$(?:@|\s)", rendered_query)
        observed_name = match.group(1).replace("\\", "") if match else None
        if observed_name != document.get("sourcegraph_name"):
            errors.append("rendered_query does not pin sourcegraph_name")
        if document.get("cutoff_commit") not in rendered_query:
            errors.append("rendered_query does not pin cutoff_commit")
    return errors


def validate_evidence_packet(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    errors = []
    if document.get("packet_version") != 3:
        errors.append("packet_version must equal 3")
    if document.get("packet_type") not in PACKET_TYPES:
        errors.append("packet_type is invalid")
    if document.get("outcomes_consulted") is not False:
        errors.append("evidence packet must be outcome blind")
    if document.get("packet_sha256") != evidence_packet_sha256(document):
        errors.append("packet_sha256 does not match")
    if document.get("indexed_revision_oid") != document.get("cutoff_commit"):
        errors.append("indexed_revision_oid must equal cutoff_commit")
    if not document.get("sourcegraph_result_ids"):
        errors.append("sourcegraph_result_ids must be non-empty")
    if not document.get("raw_evidence"):
        errors.append("raw_evidence must be non-empty")
    errors.extend(_packet_query_errors(document, specification))
    event = document.get("candidate_event", {})
    try:
        if _parse_time(event["observed_at"]) > _parse_time(specification["cutoff"]):
            errors.append("candidate event is after the frozen cutoff")
    except (KeyError, TypeError, ValueError):
        errors.append("candidate event observed_at must be a valid timestamp")
    return errors


def agreement_audit_selected(
    packet_sha256: str, agreement_specification: Mapping[str, Any]
) -> bool:
    seed = agreement_specification["seed"]
    payload = f"{seed}\0{packet_sha256}".encode()
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 10_000
    return bucket < agreement_specification["sample_basis_points"]


def _review_errors(reviews: list[Mapping[str, Any]]) -> list[str]:
    errors = []
    reviewer_ids = [review.get("reviewer_id") for review in reviews]
    if len(reviewer_ids) != len(set(reviewer_ids)):
        errors.append("all adjudication reviewers must be independent")
    for review in reviews:
        if review.get("outcome_blind") is not True:
            errors.append("every review must be outcome blind")
        if review.get("peer_review_blind") is not True:
            errors.append("every review must be peer-review blind")
        if review.get("reviewer_kind") not in {"human", "model"}:
            errors.append("every review must record reviewer_kind")
        if not review.get("reviewer_version"):
            errors.append("every review must record reviewer_version")
        if review.get("reviewer_kind") == "model" and any(
            not review.get(field)
            for field in ("provider", "model_id", "model_version", "prompt_sha256")
        ):
            errors.append(
                "model reviews must freeze provider, model, and prompt versions"
            )
        if not review.get("rationale"):
            errors.append("every review must record a rationale")
    return errors


def _packet_decisions(packet_type: str, specification: Mapping[str, Any]) -> set[str]:
    return set(specification["adjudication"]["decision_sets"].get(packet_type, []))


def validate_adjudication_bundle(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    reviews = document.get("reviews")
    if not isinstance(reviews, list):
        return ["reviews must be a list"]
    errors = _review_errors(reviews)
    primary = [review for review in reviews if review.get("stage") == "primary"]
    if len(primary) != 2:
        errors.append("exactly two primary reviews are required")
        return errors
    if primary[0].get("reviewer_id") == primary[1].get("reviewer_id"):
        errors.append("primary reviewers must be independent")
    allowed = _packet_decisions(document.get("packet_type", ""), specification)
    if any(review.get("decision") not in allowed for review in reviews):
        errors.append("review decision is invalid for packet_type")
    agreement = primary[0].get("decision") == primary[1].get("decision")
    resolution = [review for review in reviews if review.get("stage") == "resolution"]
    audit = [review for review in reviews if review.get("stage") == "agreement_audit"]
    if any(
        review.get("stage") not in {"primary", "resolution", "agreement_audit"}
        for review in reviews
    ):
        errors.append("review stage is invalid")
    if not agreement and len(resolution) != 1:
        errors.append("disagreement requires one resolution review")
    if not agreement and audit:
        errors.append("agreement audit review is invalid for a disagreement")
    if agreement and resolution:
        errors.append("resolution review is invalid for an agreement")
    agreement_specification = specification["adjudication"]["agreement_audit"]
    selected = agreement and agreement_audit_selected(
        document.get("packet_sha256", ""), agreement_specification
    )
    if selected and len(audit) != 1:
        errors.append("selected agreement requires one audit review")
    if agreement and not selected and audit:
        errors.append("unselected agreement cannot receive an audit review")
    return errors
