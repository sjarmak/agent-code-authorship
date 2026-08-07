"""Freeze reviewed explicit-provenance commits as positive agent labels."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from authorship.sourcegraph_adoption_anchor_completion import (
    anchor_completion_ledger_sha256,
    anchor_completion_manifest_sha256,
)
from authorship.sourcegraph_adoption_audit import (
    audit_response_sha256,
    compiled_audit_sha256,
)
from authorship.sourcegraph_adoption_catalog import build_adoption_catalog
from authorship.sourcegraph_adoption_review import (
    adoption_review_response_sha256,
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
    validate_adoption_decision_ledger,
)

CATALOG_VERSION = 1
EXPLICIT_PROVENANCE_KINDS = frozenset(
    {"explicit_provenance_anchor", "explicit_provenance_candidate"}
)
EXCLUSION_KEYS = (
    "decision_not_accept_confirmed",
    "confirmed_non_explicit_provenance",
    "confirmed_missing_commit_oid",
)
COMMIT_OID = re.compile(r"^[0-9a-f]{40}$")


class AgentCommitCatalogError(ValueError):
    """Raised when reviewed commit evidence or predecessor bindings drift."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def agent_commit_catalog_sha256(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "catalog_sha256"}
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _task_decision_binding(
    task: Mapping[str, Any], decision: Mapping[str, Any], label: str
) -> None:
    if (
        task.get("outcomes_consulted") is not False
        or decision.get("outcomes_consulted") is not False
    ):
        raise AgentCommitCatalogError(f"{label} task or decision is outcome exposed")
    fields = ("task_id", "task_sha256", "canonical_repository_id", "event_id")
    if any(task.get(field) != decision.get(field) for field in fields):
        raise AgentCommitCatalogError(f"{label} decision task binding is invalid")
    if decision.get("response_sha256") != adoption_review_response_sha256(decision):
        raise AgentCommitCatalogError(f"{label} response checksum does not match")


def _sequential_records(
    ledger: Mapping[str, Any], tranches: Sequence[Mapping[str, Any]]
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], str]]:
    decisions = validate_adoption_decision_ledger(
        ledger,
        case_index_sha256_value=ledger.get("case_index_sha256"),
        workflow_sha256_value=ledger.get("workflow_sha256"),
    )
    tasks: dict[str, Mapping[str, Any]] = {}
    for tranche in tranches:
        if tranche.get("tranche_sha256") != adoption_review_tranche_sha256(tranche):
            raise AgentCommitCatalogError("sequential tranche checksum does not match")
        for task in tranche.get("tasks", []):
            task_id = task.get("task_id")
            if task_id in tasks or task.get(
                "task_sha256"
            ) != adoption_review_task_sha256(task):
                raise AgentCommitCatalogError("sequential task binding is invalid")
            tasks[task_id] = task
    if {decision["task_id"] for decision in decisions} != set(tasks):
        raise AgentCommitCatalogError("sequential task coverage is incomplete")
    for decision in decisions:
        _task_decision_binding(tasks[decision["task_id"]], decision, "sequential")
    return [(tasks[item["task_id"]], item, "sequential") for item in decisions]


def _anchor_records(
    sequential: Mapping[str, Any],
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], str]]:
    if (
        manifest.get("outcomes_consulted") is not False
        or ledger.get("outcomes_consulted") is not False
    ):
        raise AgentCommitCatalogError("anchor artifact is outcome exposed")
    if manifest.get("manifest_sha256") != anchor_completion_manifest_sha256(manifest):
        raise AgentCommitCatalogError("anchor manifest checksum does not match")
    if ledger.get("anchor_completion_ledger_sha256") != (
        anchor_completion_ledger_sha256(ledger)
    ):
        raise AgentCommitCatalogError("anchor ledger checksum does not match")
    if ledger.get("source_decision_ledger_sha256") != sequential.get(
        "decision_ledger_sha256"
    ):
        raise AgentCommitCatalogError("anchor source ledger binding does not match")
    if ledger.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise AgentCommitCatalogError("anchor manifest binding does not match")
    tasks = {task["task_id"]: task for task in manifest.get("tasks", [])}
    decisions = {item["task_id"]: item for item in ledger.get("decisions", [])}
    if len(tasks) != manifest.get("task_count") or set(tasks) != set(decisions):
        raise AgentCommitCatalogError("anchor task coverage is incomplete")
    if len(decisions) != ledger.get("decision_count"):
        raise AgentCommitCatalogError("anchor decision count does not match")
    for task_id, decision in decisions.items():
        task = tasks[task_id]
        if task.get("task_sha256") != adoption_review_task_sha256(task):
            raise AgentCommitCatalogError("anchor task checksum does not match")
        _task_decision_binding(task, decision, "anchor")
    return [(tasks[key], decisions[key], "anchor_completion") for key in tasks]


def _selected_records(
    records: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], str]],
) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any], str]], dict[str, int]]:
    selected = []
    exclusions = Counter({key: 0 for key in EXCLUSION_KEYS})
    for task, decision, source in records:
        if decision.get("decision") != "accept_confirmed":
            exclusions["decision_not_accept_confirmed"] += 1
        elif task.get("candidate_kind") not in EXPLICIT_PROVENANCE_KINDS:
            exclusions["confirmed_non_explicit_provenance"] += 1
        elif not COMMIT_OID.fullmatch(
            str(task.get("candidate_event", {}).get("commit_oid", ""))
        ):
            exclusions["confirmed_missing_commit_oid"] += 1
        else:
            selected.append((task, decision, source))
    return selected, {key: exclusions[key] for key in EXCLUSION_KEYS}


def _validated_audit(
    audit: Mapping[str, Any], language_inventory: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    if audit.get("compiled_audit_sha256") != compiled_audit_sha256(audit):
        raise AgentCommitCatalogError("compiled audit checksum does not match")
    if (
        audit.get("outcomes_consulted") is not False
        or audit.get("model_or_api_calls") is not False
        or audit.get("scip_required") is not False
    ):
        raise AgentCommitCatalogError("compiled audit violates execution policy")
    if audit.get("predecessors", {}).get(
        "language_inventory_sha256"
    ) != language_inventory.get("inventory_sha256"):
        raise AgentCommitCatalogError("compiled audit language binding does not match")
    responses = audit.get("responses")
    if not isinstance(responses, list) or len(responses) != audit.get("response_count"):
        raise AgentCommitCatalogError("compiled audit response count does not match")
    indexed = {response.get("task_id"): response for response in responses}
    if len(indexed) != len(responses) or None in indexed:
        raise AgentCommitCatalogError("compiled audit task identity is duplicated")
    if any(
        response.get("response_sha256") != audit_response_sha256(response)
        for response in responses
    ):
        raise AgentCommitCatalogError("compiled audit response checksum does not match")
    if any(response.get("outcomes_consulted") is not False for response in responses):
        raise AgentCommitCatalogError("compiled audit response is outcome exposed")
    return indexed


def _cited_evidence(
    task: Mapping[str, Any], decision: Mapping[str, Any]
) -> list[dict[str, Any]]:
    citations = set(decision.get("evidence_citations", []))
    evidence = [
        {
            "packet_id": packet["packet_id"],
            "packet_sha256": packet["packet_sha256"],
            "query_family_id": packet["query_family_id"],
            **deepcopy(raw),
        }
        for packet in task.get("evidence", [])
        for raw in packet.get("raw_evidence", [])
        if raw.get("source_url") in citations
    ]
    covered = {item["source_url"] for item in evidence}
    if not citations or citations != covered:
        raise AgentCommitCatalogError("review citations are not task-local")
    return evidence


def _audit_fields(
    task: Mapping[str, Any],
    decision: Mapping[str, Any],
    audit_responses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    response = audit_responses.get(task["task_id"])
    if response is None:
        return {
            "sampled": False,
            "audit_task_id": None,
            "response_sha256": None,
            "reviewer_id": None,
            "decision": None,
            "exact_agreement": None,
        }
    fields = ("task_sha256", "canonical_repository_id", "event_id")
    if any(response.get(field) != task.get(field) for field in fields):
        raise AgentCommitCatalogError("audit response task binding does not match")
    return {
        "sampled": True,
        "audit_task_id": response["audit_task_id"],
        "response_sha256": response["response_sha256"],
        "reviewer_id": response["reviewer_id"],
        "decision": response["decision"],
        "exact_agreement": response["decision"] == decision["decision"],
    }


def _adoption_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "status",
        "adoption_event_id",
        "adoption_observed_at",
        "adoption_commit_oid",
        "adoption_decision",
        "evidence_tier",
        "adoption_interpretation",
        "clean_prehistory",
        "unresolved_earlier_candidate_count",
    )
    return {field: deepcopy(row[field]) for field in fields}


def _commit_row(
    task: Mapping[str, Any],
    decision: Mapping[str, Any],
    source: str,
    adoption: Mapping[str, Any],
    audit_responses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sourcegraph_name = task["sourcegraph_name"]
    primary_scope = sourcegraph_name.startswith("github.com/sg-evals/")
    if primary_scope != adoption["primary_scope_eligible"]:
        raise AgentCommitCatalogError("repository primary-scope binding does not match")
    return {
        "repository_id": task["canonical_repository_id"],
        "sourcegraph_name": sourcegraph_name,
        "language": adoption["language"],
        "primary_scope_eligible": primary_scope,
        "commit_oid": task["candidate_event"]["commit_oid"],
        "observed_at": task["candidate_event"]["observed_at"],
        "event_id": task["event_id"],
        "candidate_kind": task["candidate_kind"],
        "review_source": source,
        "tranche_id": decision["tranche_id"],
        "tranche_number": decision["tranche_number"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "response_sha256": decision["response_sha256"],
        "reviewer_id": decision["reviewer_id"],
        "review_role": decision["review_role"],
        "decision": decision["decision"],
        "evidence_tier": decision["evidence_tier"],
        "review_rationale": decision["rationale"],
        "evidence_citations": deepcopy(decision["evidence_citations"]),
        "query_family_ids": deepcopy(task["query_family_ids"]),
        "packet_ids": deepcopy(task["packet_ids"]),
        "cited_evidence": _cited_evidence(task, decision),
        "adoption_context": _adoption_fields(adoption),
        "reliability_audit": _audit_fields(task, decision, audit_responses),
    }


def _catalog_document(
    sequential: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    anchor: Mapping[str, Any],
    languages: Mapping[str, Any],
    audit: Mapping[str, Any],
    adoption_catalog: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    reviewed_count: int,
    exclusions: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "$schema": "sourcegraph-agent-commit-catalog.schema.json",
        "catalog_version": CATALOG_VERSION,
        "selection_rule": {
            "decision": "accept_confirmed",
            "candidate_kinds": sorted(EXPLICIT_PROVENANCE_KINDS),
            "requires_concrete_commit_oid": True,
            "label_interpretation": (
                "reviewed_positive_commit_only_not_post_adoption_inference"
            ),
        },
        "sequential_decision_ledger_sha256": sequential["decision_ledger_sha256"],
        "sequential_tranche_sha256s": [
            tranche["tranche_sha256"] for tranche in tranches
        ],
        "anchor_manifest_sha256": manifest["manifest_sha256"],
        "anchor_completion_ledger_sha256": anchor["anchor_completion_ledger_sha256"],
        "language_inventory_sha256": languages["inventory_sha256"],
        "compiled_audit_sha256": audit["compiled_audit_sha256"],
        "derived_adoption_catalog_sha256": adoption_catalog["catalog_sha256"],
        "reviewed_event_count": reviewed_count,
        "excluded_event_count": sum(exclusions.values()),
        "exclusion_counts": dict(exclusions),
        **_summary_fields(rows),
        "commits": list(rows),
        "outcomes_consulted": False,
    }


def _summary_fields(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sampled = [row for row in rows if row["reliability_audit"]["sampled"]]
    sources = Counter(row["review_source"] for row in rows)
    return {
        "commit_count": len(rows),
        "repository_count": len({row["repository_id"] for row in rows}),
        "primary_scope_commit_count": sum(
            row["primary_scope_eligible"] for row in rows
        ),
        "source_counts": dict(sorted(sources.items())),
        "audit_sampled_commit_count": len(sampled),
        "audit_exact_agreement_count": sum(
            row["reliability_audit"]["exact_agreement"] is True for row in sampled
        ),
        "audit_disagreement_count": sum(
            row["reliability_audit"]["exact_agreement"] is False for row in sampled
        ),
    }


def _materialized_rows(
    selected: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], str]],
    adoption: Mapping[str, Any],
    audits: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    contexts = {row["repository_id"]: row for row in adoption["repositories"]}
    rows = [
        _commit_row(
            task,
            decision,
            source,
            contexts[task["canonical_repository_id"]],
            audits,
        )
        for task, decision, source in selected
    ]
    return sorted(rows, key=lambda row: (row["repository_id"], row["commit_oid"]))


def _validate_commit_rows(rows: Any) -> None:
    if not isinstance(rows, list) or not rows:
        raise AgentCommitCatalogError("catalog commits are invalid")
    keys = [(row.get("repository_id"), row.get("commit_oid")) for row in rows]
    if len(set(keys)) != len(keys):
        raise AgentCommitCatalogError("duplicate repository commit")
    if any(
        row.get("decision") != "accept_confirmed"
        or row.get("candidate_kind") not in EXPLICIT_PROVENANCE_KINDS
        or not COMMIT_OID.fullmatch(str(row.get("commit_oid", "")))
        for row in rows
    ):
        raise AgentCommitCatalogError("catalog contains an ineligible commit")


def _validate_counts(
    document: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> None:
    sampled = [row for row in rows if row["reliability_audit"]["sampled"]]
    expected = {
        "commit_count": len(rows),
        "repository_count": len({row["repository_id"] for row in rows}),
        "primary_scope_commit_count": sum(
            row["primary_scope_eligible"] for row in rows
        ),
        "audit_sampled_commit_count": len(sampled),
        "audit_exact_agreement_count": sum(
            row["reliability_audit"]["exact_agreement"] is True for row in sampled
        ),
        "audit_disagreement_count": sum(
            row["reliability_audit"]["exact_agreement"] is False for row in sampled
        ),
    }
    if any(document.get(field) != value for field, value in expected.items()):
        raise AgentCommitCatalogError("catalog counts do not match rows")
    exclusions = document.get("exclusion_counts", {})
    if document.get("excluded_event_count") != sum(exclusions.values()):
        raise AgentCommitCatalogError("catalog exclusion counts do not match")
    if document.get("reviewed_event_count") != len(rows) + sum(exclusions.values()):
        raise AgentCommitCatalogError("catalog reviewed event count does not match")


def validate_agent_commit_catalog(document: Mapping[str, Any]) -> None:
    """Validate positive-label invariants and the canonical catalog checksum."""

    rows = document.get("commits")
    _validate_commit_rows(rows)
    _validate_counts(document, rows)
    if document.get("outcomes_consulted") is not False:
        raise AgentCommitCatalogError("catalog is outcome exposed")
    if document.get("catalog_sha256") != agent_commit_catalog_sha256(document):
        raise AgentCommitCatalogError("catalog checksum does not match")


def build_agent_commit_catalog(
    sequential_ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    anchor_manifest: Mapping[str, Any],
    anchor_ledger: Mapping[str, Any],
    language_inventory: Mapping[str, Any],
    compiled_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a checksummed catalog of reviewed positive agent commits."""

    adoption = build_adoption_catalog(
        sequential_ledger,
        tranches,
        anchor_manifest,
        anchor_ledger,
        language_inventory,
    )
    records = [
        *_sequential_records(sequential_ledger, tranches),
        *_anchor_records(sequential_ledger, anchor_manifest, anchor_ledger),
    ]
    selected, exclusions = _selected_records(records)
    audits = _validated_audit(compiled_audit, language_inventory)
    rows = _materialized_rows(selected, adoption, audits)
    document = _catalog_document(
        sequential_ledger,
        tranches,
        anchor_manifest,
        anchor_ledger,
        language_inventory,
        compiled_audit,
        adoption,
        rows,
        len(records),
        exclusions,
    )
    catalog = {**document, "catalog_sha256": agent_commit_catalog_sha256(document)}
    validate_agent_commit_catalog(catalog)
    return catalog
