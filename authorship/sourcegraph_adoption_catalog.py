"""Freeze repository-level adoption dates from sequential and anchor review."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from authorship.sourcegraph_adoption_anchor_completion import (
    anchor_completion_ledger_sha256,
    anchor_completion_manifest_sha256,
)
from authorship.sourcegraph_adoption_review import (
    adoption_review_response_sha256,
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_repository_languages import (
    repository_language_inventory_sha256,
)

CATALOG_VERSION = 1
ACCEPTED = frozenset({"accept_confirmed", "accept_observed"})


class AdoptionCatalogError(ValueError):
    """Raised when catalog predecessors or repository states do not bind."""


def _canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def adoption_catalog_sha256(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "catalog_sha256"}
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _sequential_tasks(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    decisions = validate_adoption_decision_ledger(
        ledger,
        case_index_sha256_value=ledger.get("case_index_sha256"),
        workflow_sha256_value=ledger.get("workflow_sha256"),
    )
    tasks = {}
    for tranche in tranches:
        if tranche.get("tranche_sha256") != adoption_review_tranche_sha256(tranche):
            raise AdoptionCatalogError("sequential tranche checksum does not match")
        for task in tranche.get("tasks", []):
            task_id = task.get("task_id")
            if task_id in tasks or task.get(
                "task_sha256"
            ) != adoption_review_task_sha256(task):
                raise AdoptionCatalogError("sequential task binding is invalid")
            tasks[task_id] = task
    if {decision["task_id"] for decision in decisions} != set(tasks):
        raise AdoptionCatalogError("sequential tasks do not exactly cover decisions")
    for decision in decisions:
        task = tasks[decision["task_id"]]
        if (
            decision["task_sha256"] != task["task_sha256"]
            or decision["canonical_repository_id"] != task["canonical_repository_id"]
            or decision["event_id"] != task["event_id"]
        ):
            raise AdoptionCatalogError("sequential decision task binding is invalid")
    return tasks


def _anchor_inputs(
    sequential_ledger: Mapping[str, Any],
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    if manifest.get("manifest_sha256") != anchor_completion_manifest_sha256(manifest):
        raise AdoptionCatalogError("anchor manifest checksum does not match")
    if ledger.get("anchor_completion_ledger_sha256") != (
        anchor_completion_ledger_sha256(ledger)
    ):
        raise AdoptionCatalogError("anchor ledger checksum does not match")
    if ledger.get("source_decision_ledger_sha256") != sequential_ledger.get(
        "decision_ledger_sha256"
    ):
        raise AdoptionCatalogError("anchor source ledger binding does not match")
    if ledger.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise AdoptionCatalogError("anchor manifest binding does not match")
    tasks = {task["task_id"]: task for task in manifest.get("tasks", [])}
    decisions = {
        decision["task_id"]: decision for decision in ledger.get("decisions", [])
    }
    if len(tasks) != manifest.get("task_count") or set(tasks) != set(decisions):
        raise AdoptionCatalogError("anchor tasks do not exactly cover decisions")
    if len(decisions) != ledger.get("decision_count"):
        raise AdoptionCatalogError("anchor decision count does not match")
    for task_id, decision in decisions.items():
        task = tasks[task_id]
        if (
            decision.get("response_sha256") != adoption_review_response_sha256(decision)
            or decision.get("task_sha256") != task.get("task_sha256")
            or decision.get("canonical_repository_id")
            != task.get("canonical_repository_id")
            or decision.get("event_id") != task.get("event_id")
        ):
            raise AdoptionCatalogError("anchor decision task binding is invalid")
    return tasks, decisions


def _languages(
    inventory: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    if inventory.get("inventory_sha256") != repository_language_inventory_sha256(
        inventory
    ):
        raise AdoptionCatalogError("language inventory checksum does not match")
    rows = inventory.get("repositories")
    if not isinstance(rows, list):
        raise AdoptionCatalogError("language inventory repositories are invalid")
    indexed = {row.get("canonical_repository_id"): row for row in rows}
    if len(indexed) != len(rows) or len(rows) != inventory.get("repository_count"):
        raise AdoptionCatalogError("language repository identity is duplicated")
    return indexed


def _accepted_sequential(
    ledger: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    accepted = {}
    for decision in ledger["decisions"]:
        if decision["decision"] not in ACCEPTED:
            continue
        repository = decision["canonical_repository_id"]
        if repository in accepted:
            raise AdoptionCatalogError(
                "repository has multiple accepted adoption dates"
            )
        accepted[repository] = decision
    return accepted


def _prehistory(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    records = manifest.get("prehistory")
    if not isinstance(records, list):
        raise AdoptionCatalogError("anchor prehistory is invalid")
    indexed = {record.get("task_id"): record for record in records}
    if len(indexed) != len(records):
        raise AdoptionCatalogError("anchor prehistory task identity is duplicated")
    return indexed


def _dated_fields(
    task: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    interpretation: str,
    clean_prehistory: bool,
    unresolved_count: int,
) -> dict[str, Any]:
    return {
        "adoption_event_id": task["event_id"],
        "adoption_observed_at": task["candidate_event"]["observed_at"],
        "adoption_commit_oid": task["candidate_event"].get("commit_oid"),
        "adoption_decision": decision["decision"],
        "evidence_tier": decision["evidence_tier"],
        "adoption_interpretation": interpretation,
        "clean_prehistory": clean_prehistory,
        "unresolved_earlier_candidate_count": unresolved_count,
    }


def _undated_fields(clean_prehistory: bool, unresolved_count: int) -> dict[str, Any]:
    return {
        "adoption_event_id": None,
        "adoption_observed_at": None,
        "adoption_commit_oid": None,
        "adoption_decision": None,
        "evidence_tier": None,
        "adoption_interpretation": None,
        "clean_prehistory": clean_prehistory,
        "unresolved_earlier_candidate_count": unresolved_count,
    }


def _anchor_status_fields(
    anchor_task: Mapping[str, Any],
    decision: Mapping[str, Any],
    history: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    if decision["decision"] not in ACCEPTED:
        return (
            "no_credible_event",
            _undated_fields(
                history["clean_prehistory"],
                history["unresolved_earlier_candidate_count"],
            ),
        )
    return (
        "dated_explicit_anchor",
        _dated_fields(
            anchor_task,
            decision,
            interpretation="first_observed_explicit_use",
            clean_prehistory=history["clean_prehistory"],
            unresolved_count=history["unresolved_earlier_candidate_count"],
        ),
    )


def _adoption_status_fields(
    sequential_decision: Mapping[str, Any] | None,
    sequential_tasks: Mapping[str, Mapping[str, Any]],
    anchor_task: Mapping[str, Any] | None,
    anchor_decisions: Mapping[str, Mapping[str, Any]],
    prehistory: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if sequential_decision is not None:
        return (
            "dated_sequential",
            _dated_fields(
                sequential_tasks[sequential_decision["task_id"]],
                sequential_decision,
                interpretation="earliest_accepted_event_in_screened_frame",
                clean_prehistory=True,
                unresolved_count=0,
            ),
        )
    if anchor_task is None:
        return "no_credible_event", _undated_fields(True, 0)
    task_id = anchor_task["task_id"]
    return _anchor_status_fields(
        anchor_task,
        anchor_decisions[task_id],
        prehistory[task_id],
    )


def _repository_row(
    repository: str,
    language: Mapping[str, Any],
    sequential_decision: Mapping[str, Any] | None,
    sequential_tasks: Mapping[str, Mapping[str, Any]],
    anchor_tasks_by_repository: Mapping[str, Mapping[str, Any]],
    anchor_decisions: Mapping[str, Mapping[str, Any]],
    prehistory: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    status, fields = _adoption_status_fields(
        sequential_decision,
        sequential_tasks,
        anchor_tasks_by_repository.get(repository),
        anchor_decisions,
        prehistory,
    )
    in_scope = language["review_sourcegraph_name"].startswith("github.com/sg-evals/")
    return {
        "repository_id": repository,
        "language": language["language"],
        "status": status,
        "primary_scope_eligible": in_scope,
        "scope_exclusion_reason": (
            None if in_scope else "review_evidence_outside_sg_evals_org"
        ),
        **fields,
    }


def _catalog_document(
    sequential_ledger: Mapping[str, Any],
    anchor_ledger: Mapping[str, Any],
    language_inventory: Mapping[str, Any],
    repositories: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    statuses = Counter(row["status"] for row in repositories)
    return {
        "$schema": "sourcegraph-adoption-catalog.schema.json",
        "catalog_version": CATALOG_VERSION,
        "sequential_decision_ledger_sha256": sequential_ledger[
            "decision_ledger_sha256"
        ],
        "anchor_completion_ledger_sha256": anchor_ledger[
            "anchor_completion_ledger_sha256"
        ],
        "language_inventory_sha256": language_inventory["inventory_sha256"],
        "repository_count": len(repositories),
        "dated_repository_count": sum(
            row["status"].startswith("dated_") for row in repositories
        ),
        "primary_scope_eligible_count": sum(
            row["primary_scope_eligible"] for row in repositories
        ),
        "status_counts": dict(sorted(statuses.items())),
        "repositories": list(repositories),
        "outcomes_consulted": False,
    }


def build_adoption_catalog(
    sequential_ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    anchor_manifest: Mapping[str, Any],
    anchor_ledger: Mapping[str, Any],
    language_inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the repository-level adoption catalog and sensitivity flags."""

    sequential_tasks = _sequential_tasks(sequential_ledger, tranches)
    anchor_tasks, anchor_decisions = _anchor_inputs(
        sequential_ledger, anchor_manifest, anchor_ledger
    )
    anchor_tasks_by_repository = {
        task["canonical_repository_id"]: task for task in anchor_tasks.values()
    }
    if len(anchor_tasks_by_repository) != len(anchor_tasks):
        raise AdoptionCatalogError("repository has multiple anchor tasks")
    languages = _languages(language_inventory)
    accepted = _accepted_sequential(sequential_ledger)
    histories = _prehistory(anchor_manifest)
    repositories = [
        _repository_row(
            repository,
            language,
            accepted.get(repository),
            sequential_tasks,
            anchor_tasks_by_repository,
            anchor_decisions,
            histories,
        )
        for repository, language in sorted(languages.items())
    ]
    document = _catalog_document(
        sequential_ledger,
        anchor_ledger,
        language_inventory,
        repositories,
    )
    return {**document, "catalog_sha256": adoption_catalog_sha256(document)}
