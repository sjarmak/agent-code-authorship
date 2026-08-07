"""Adapt the bounded anchor tranche into the cumulative reliability-audit frame."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from authorship.sourcegraph_adoption_anchor_completion import (
    anchor_completion_ledger_sha256,
    anchor_completion_manifest_sha256,
)
from authorship.sourcegraph_adoption_review import (
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_adoption_review_contracts import (
    AdoptionReviewError,
    adoption_review_ledger_sha256,
    adoption_review_response_sha256,
)

ADAPTER_VERSION = 1


class AdoptionAuditAdapterError(ValueError):
    """Raised when reviewed adoption sources cannot form one audit frame."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def audit_adapter_sha256(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key != "adapter_sha256"}
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _sequential_sources(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    try:
        decisions = validate_adoption_decision_ledger(
            ledger,
            case_index_sha256_value=str(ledger.get("case_index_sha256")),
            workflow_sha256_value=str(ledger.get("workflow_sha256")),
        )
    except AdoptionReviewError as error:
        raise AdoptionAuditAdapterError(str(error)) from error
    ordered = sorted(tranches, key=lambda item: item.get("tranche_number", 0))
    if [item.get("tranche_number") for item in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise AdoptionAuditAdapterError("sequential tranche sequence is incomplete")
    tasks = [task for tranche in ordered for task in _tranche_tasks(tranche)]
    if {task["task_id"] for task in tasks} != {item["task_id"] for item in decisions}:
        raise AdoptionAuditAdapterError("sequential tasks do not cover decisions")
    last = ordered[-1]
    if ledger.get("tranche_sha256") != last.get("tranche_sha256") or ledger.get(
        "tranche_id"
    ) != last.get("tranche_id"):
        raise AdoptionAuditAdapterError(
            "sequential final tranche binding does not match"
        )
    return ordered, decisions


def _tranche_tasks(tranche: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if tranche.get("tranche_sha256") != adoption_review_tranche_sha256(tranche):
        raise AdoptionAuditAdapterError("sequential tranche checksum does not match")
    tasks = tranche.get("tasks")
    if not isinstance(tasks, list) or tranche.get("task_count") != len(tasks):
        raise AdoptionAuditAdapterError("sequential tranche tasks are invalid")
    if any(
        task.get("task_sha256") != adoption_review_task_sha256(task) for task in tasks
    ):
        raise AdoptionAuditAdapterError("sequential task checksum does not match")
    return tasks


def _anchor_sources(
    sequential_ledger: Mapping[str, Any],
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if manifest.get("manifest_sha256") != anchor_completion_manifest_sha256(manifest):
        raise AdoptionAuditAdapterError("anchor manifest checksum does not match")
    if ledger.get("anchor_completion_ledger_sha256") != (
        anchor_completion_ledger_sha256(ledger)
    ):
        raise AdoptionAuditAdapterError("anchor ledger checksum does not match")
    if ledger.get("source_decision_ledger_sha256") != sequential_ledger.get(
        "decision_ledger_sha256"
    ):
        raise AdoptionAuditAdapterError("anchor source ledger binding does not match")
    if ledger.get("manifest_sha256") != manifest.get("manifest_sha256"):
        raise AdoptionAuditAdapterError("anchor manifest binding does not match")
    if any(
        manifest.get(field) != sequential_ledger.get(field)
        for field in ("case_index_sha256", "workflow_sha256")
    ):
        raise AdoptionAuditAdapterError("anchor study binding does not match")
    return _anchor_rows(manifest, ledger)


def _anchor_rows(
    manifest: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    tasks = manifest.get("tasks")
    decisions = ledger.get("decisions")
    if not isinstance(tasks, list) or not isinstance(decisions, list):
        raise AdoptionAuditAdapterError("anchor rows are invalid")
    tasks_by_id = {task.get("task_id"): task for task in tasks}
    decisions_by_id = {item.get("task_id"): item for item in decisions}
    if (
        len(tasks_by_id) != manifest.get("task_count")
        or len(decisions_by_id) != ledger.get("decision_count")
        or set(tasks_by_id) != set(decisions_by_id)
    ):
        raise AdoptionAuditAdapterError("anchor tasks do not cover decisions")
    for task_id, task in tasks_by_id.items():
        decision = decisions_by_id[task_id]
        if (
            task.get("task_sha256") != adoption_review_task_sha256(task)
            or decision.get("response_sha256")
            != adoption_review_response_sha256(decision)
            or decision.get("task_sha256") != task.get("task_sha256")
        ):
            raise AdoptionAuditAdapterError("anchor task decision binding is invalid")
    return tasks, decisions


def _anchor_tranche(
    manifest: Mapping[str, Any],
    sequential_ledger: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    document = {
        "review_version": 3,
        "tranche_id": manifest["tranche_id"],
        "tranche_number": manifest["tranche_number"],
        "case_index_sha256": manifest["case_index_sha256"],
        "workflow_sha256": manifest["workflow_sha256"],
        "prior_decision_ledger_sha256": sequential_ledger["decision_ledger_sha256"],
        "eligible_repository_count": manifest["eligible_repository_count"],
        "completed_repository_count": manifest["completed_repository_count"],
        "exhausted_repository_count": manifest["exhausted_repository_count"],
        "active_repository_count": len(tasks),
        "repository_count": len(tasks),
        "task_count": len(tasks),
        "tasks": deepcopy(list(tasks)),
        "outcomes_consulted": False,
    }
    return {**document, "tranche_sha256": adoption_review_tranche_sha256(document)}


def _combined_ledger(
    sequential: Mapping[str, Any],
    anchor_tranche: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prior = deepcopy(list(sequential["decisions"]))
    new_decisions = deepcopy(list(decisions))
    document = {
        "decision_ledger_version": 3,
        "tranche_id": anchor_tranche["tranche_id"],
        "tranche_sha256": anchor_tranche["tranche_sha256"],
        "case_index_sha256": sequential["case_index_sha256"],
        "workflow_sha256": sequential["workflow_sha256"],
        "prior_decision_ledger_sha256": sequential["decision_ledger_sha256"],
        "prior_decision_count": len(prior),
        "response_count": len(new_decisions),
        "decision_count": len(prior) + len(new_decisions),
        "decisions": [*prior, *new_decisions],
        "outcomes_consulted": False,
    }
    combined = {
        **document,
        "decision_ledger_sha256": adoption_review_ledger_sha256(document),
    }
    validate_adoption_decision_ledger(
        combined,
        case_index_sha256_value=combined["case_index_sha256"],
        workflow_sha256_value=combined["workflow_sha256"],
    )
    return combined


def build_combined_adoption_audit_inputs(
    sequential_ledger: Mapping[str, Any],
    sequential_tranches: Sequence[Mapping[str, Any]],
    anchor_manifest: Mapping[str, Any],
    anchor_ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Mapping[str, Any]], dict[str, Any]]:
    """Return a standard cumulative ledger/tranche sequence plus source bindings."""

    ordered, _sequential_decisions = _sequential_sources(
        sequential_ledger, sequential_tranches
    )
    tasks, anchor_decisions = _anchor_sources(
        sequential_ledger, anchor_manifest, anchor_ledger
    )
    if anchor_manifest.get("tranche_number") != len(ordered) + 1:
        raise AdoptionAuditAdapterError("anchor tranche is not sequentially adjacent")
    final_tranche = _anchor_tranche(anchor_manifest, sequential_ledger, tasks)
    combined = _combined_ledger(sequential_ledger, final_tranche, anchor_decisions)
    all_tranches = [*deepcopy(ordered), final_tranche]
    adapter_document = {
        "$schema": "sourcegraph-adoption-audit-adapter.schema.json",
        "adapter_version": ADAPTER_VERSION,
        "source_sequential_ledger_sha256": sequential_ledger["decision_ledger_sha256"],
        "source_sequential_tranche_sha256s": [
            tranche["tranche_sha256"] for tranche in ordered
        ],
        "source_anchor_manifest_sha256": anchor_manifest["manifest_sha256"],
        "source_anchor_ledger_sha256": anchor_ledger["anchor_completion_ledger_sha256"],
        "combined_anchor_tranche_sha256": final_tranche["tranche_sha256"],
        "combined_decision_ledger_sha256": combined["decision_ledger_sha256"],
        "sequential_decision_count": len(sequential_ledger["decisions"]),
        "anchor_decision_count": len(anchor_decisions),
        "combined_decision_count": len(combined["decisions"]),
        "outcomes_consulted": False,
    }
    adapter = {
        **adapter_document,
        "adapter_sha256": audit_adapter_sha256(adapter_document),
    }
    return combined, all_tranches, adapter
