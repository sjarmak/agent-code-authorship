from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from authorship.sourcegraph_adoption_review import (
    adoption_review_task_sha256,
    adoption_review_tranche_sha256,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_adoption_review_contracts import (
    FINAL_DECISIONS,
    AdoptionReviewError,
)
from authorship.sourcegraph_repository_languages import (
    repository_language_inventory_sha256,
)

AUDIT_VERSION = 1
AUDIT_DECISIONS = frozenset({*FINAL_DECISIONS, "unresolved"})
AUDIT_RESPONSE_FIELDS = frozenset(
    """response_version worksheet_sha256 audit_task_id task_id task_sha256
    canonical_repository_id event_id reviewer_id decision default_branch_supported
    evidence_tier rationale evidence_citations outcomes_consulted response_sha256""".split()
)
INVENTORY_FIELDS = frozenset(
    """$schema inventory_version case_index_sha256 source_tranche_sha256 observed_at
    sourcegraph_capability sourcegraph_scope scip_required index_manifest_sha256
    repository_name_map_sha256 repository_count review_scope_exception_count
    language_counts repositories outcomes_consulted inventory_sha256""".split()
)


class AdoptionAuditError(ValueError):
    """Raised when a reliability audit cannot proceed without study drift."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _sha_without(document: Mapping[str, Any], field: str) -> str:
    return _content_sha256(
        {key: value for key, value in document.items() if key != field}
    )


def audit_worksheet_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "worksheet_sha256")


def audit_key_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "key_sha256")


def audit_response_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "response_sha256")


def compiled_audit_sha256(document: Mapping[str, Any]) -> str:
    return _sha_without(document, "compiled_audit_sha256")


def _sampling_policy() -> dict[str, Any]:
    return {
        "fraction": "0.10",
        "rounding": "ceiling",
        "minimum_per_joint_stratum": 1,
        "maximum_per_joint_stratum": 3,
        "repository_event_limit": 1,
        "repository_repeat_exception": "joint_stratum_target_would_be_unmet",
        "evidence_channel_collapse": (
            "sorted unique query_family_ids joined with '+'; no semantic grouping"
        ),
    }


def _sample_size(population_count: int) -> int:
    return min(3, max(1, (population_count + 9) // 10))


def _validate_tranche(tranche: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if tranche.get("tranche_sha256") != adoption_review_tranche_sha256(tranche):
        raise AdoptionAuditError("tranche checksum does not match")
    if tranche.get("outcomes_consulted") is not False:
        raise AdoptionAuditError("tranche is outcome exposed")
    tasks = tranche.get("tasks")
    if not isinstance(tasks, list) or any(
        not isinstance(task, Mapping) for task in tasks
    ):
        raise AdoptionAuditError("tranche tasks are invalid")
    if tranche.get("task_count") != len(tasks):
        raise AdoptionAuditError("tranche task count does not match")
    for task in tasks:
        if task.get("task_sha256") != adoption_review_task_sha256(task):
            raise AdoptionAuditError("task checksum does not match")
        if task.get("outcomes_consulted") is not False:
            raise AdoptionAuditError("task is outcome exposed")
    return tasks


def _ordered_tranches(
    tranches: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    if not tranches or any(not isinstance(item, Mapping) for item in tranches):
        raise AdoptionAuditError("frozen tranche manifests are invalid")
    ordered = sorted(tranches, key=lambda item: item.get("tranche_number", 0))
    numbers = [item.get("tranche_number") for item in ordered]
    if numbers != list(range(1, len(ordered) + 1)):
        raise AdoptionAuditError("frozen tranche sequence is incomplete")
    if len({item.get("tranche_id") for item in ordered}) != len(ordered):
        raise AdoptionAuditError("frozen tranche IDs are duplicated")
    return ordered


def _tranche_tasks(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    ordered = _ordered_tranches(tranches)
    tasks = [task for tranche in ordered for task in _validate_tranche(tranche)]
    task_ids = [task.get("task_id") for task in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise AdoptionAuditError("frozen tranche task IDs are duplicated")
    decisions = ledger.get("decisions")
    decision_ids = [decision.get("task_id") for decision in decisions]
    if set(task_ids) != set(decision_ids) or len(task_ids) != len(decision_ids):
        raise AdoptionAuditError("frozen tranches must exactly cover ledger tasks")
    last = ordered[-1]
    if ledger.get("tranche_id") != last.get("tranche_id") or ledger.get(
        "tranche_sha256"
    ) != last.get("tranche_sha256"):
        raise AdoptionAuditError("final ledger tranche binding does not match")
    return ordered, tasks


def _validate_task_decision(
    task: Mapping[str, Any],
    decision: Mapping[str, Any],
    tranche: Mapping[str, Any],
) -> None:
    bindings = (
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "event_id",
        "review_role",
    )
    if any(task.get(field) != decision.get(field) for field in bindings):
        raise AdoptionAuditError("task and decision binding does not match")
    if decision.get("tranche_id") != tranche.get("tranche_id") or decision.get(
        "tranche_number"
    ) != tranche.get("tranche_number"):
        raise AdoptionAuditError("task decision tranche binding does not match")
    if task.get("case_index_sha256") != tranche.get("case_index_sha256") or task.get(
        "workflow_sha256"
    ) != tranche.get("workflow_sha256"):
        raise AdoptionAuditError("task study binding does not match")
    if not isinstance(task.get("candidate_event"), Mapping) or not task.get("evidence"):
        raise AdoptionAuditError("audit requires real repository-event tasks")


def _validated_source_rows(
    inventory: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    rows = inventory.get("repositories")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise AdoptionAuditError("language inventory repositories are invalid")
    expected_fields = {
        "canonical_repository_id",
        "review_sourcegraph_name",
        "language_sourcegraph_name",
        "language",
    }
    if any(set(row) != expected_fields for row in rows):
        raise AdoptionAuditError("language inventory row contract is invalid")
    indexed = {row.get("canonical_repository_id"): row for row in rows}
    if len(indexed) != len(rows):
        raise AdoptionAuditError("language inventory repositories are duplicated")
    if any(
        not isinstance(row["language"], str)
        or not row["language"].strip()
        or not row["language_sourcegraph_name"].startswith("github.com/sg-evals/")
        for row in rows
    ):
        raise AdoptionAuditError("language inventory binding is invalid")
    return indexed


def _validate_language_inventory(
    inventory: Mapping[str, Any],
    ordered_tranches: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if set(inventory) != INVENTORY_FIELDS:
        raise AdoptionAuditError("language inventory contract is invalid")
    if inventory.get("inventory_sha256") != repository_language_inventory_sha256(
        inventory
    ):
        raise AdoptionAuditError("language inventory checksum does not match")
    if (
        inventory.get("outcomes_consulted") is not False
        or inventory.get("sourcegraph_capability") != "Repository.language"
        or inventory.get("sourcegraph_scope") != "github.com/sg-evals/*"
        or inventory.get("scip_required") is not False
    ):
        raise AdoptionAuditError("language inventory provenance is invalid")
    rows = _validated_source_rows(inventory)
    _validate_language_counts(inventory, rows)
    _validate_language_names(inventory, rows)
    task_repositories = {task.get("canonical_repository_id") for task in tasks}
    if set(rows) != task_repositories:
        raise AdoptionAuditError(
            "language inventory must exactly cover task repositories"
        )
    first = ordered_tranches[0]
    if inventory.get("case_index_sha256") != first.get(
        "case_index_sha256"
    ) or inventory.get("source_tranche_sha256") != first.get("tranche_sha256"):
        raise AdoptionAuditError("language inventory study binding does not match")
    return rows


def _validate_language_counts(
    inventory: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
) -> None:
    counts = dict(sorted(Counter(row["language"] for row in rows.values()).items()))
    exceptions = sum(
        not row["review_sourcegraph_name"].startswith("github.com/sg-evals/")
        for row in rows.values()
    )
    if (
        inventory.get("repository_count") != len(rows)
        or inventory.get("language_counts") != counts
        or inventory.get("review_scope_exception_count") != exceptions
    ):
        raise AdoptionAuditError("language inventory counts do not match")


def _validate_language_names(
    inventory: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
) -> None:
    name_map = {
        repository: row["language_sourcegraph_name"]
        for repository, row in sorted(rows.items())
    }
    if inventory.get("repository_name_map_sha256") != _content_sha256(name_map):
        raise AdoptionAuditError("language inventory name-map checksum does not match")


def _validate_bindings(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    try:
        decisions = validate_adoption_decision_ledger(
            ledger,
            case_index_sha256_value=str(ledger.get("case_index_sha256")),
            workflow_sha256_value=str(ledger.get("workflow_sha256")),
        )
    except AdoptionReviewError as error:
        raise AdoptionAuditError(str(error)) from error
    ordered, tasks = _tranche_tasks(ledger, tranches)
    by_tranche = {item["tranche_number"]: item for item in ordered}
    by_task = {task["task_id"]: task for task in tasks}
    for decision in decisions:
        tranche = by_tranche.get(decision["tranche_number"])
        if tranche is None:
            raise AdoptionAuditError("decision references a missing tranche")
        _validate_task_decision(by_task[decision["task_id"]], decision, tranche)
    rows = _validate_language_inventory(inventory, ordered, tasks)
    for task in tasks:
        row = rows[task["canonical_repository_id"]]
        if row["review_sourcegraph_name"] != task.get("sourcegraph_name"):
            raise AdoptionAuditError(
                "task and language identity binding does not match"
            )
    return tasks, rows


def _evidence_channel(task: Mapping[str, Any]) -> str:
    identifiers = task.get("query_family_ids")
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or any(
            not isinstance(item, str) or not item or "+" in item for item in identifiers
        )
    ):
        raise AdoptionAuditError("query-family binding is invalid")
    return "+".join(sorted(set(identifiers)))


def _candidate(
    task: Mapping[str, Any],
    decision: Mapping[str, Any],
    language: str,
) -> dict[str, Any]:
    channel = _evidence_channel(task)
    labels = {
        "primary_decision": decision["decision"],
        "evidence_channel": channel,
        "language": language,
    }
    return {
        "task": task,
        "decision": decision,
        **labels,
        "stratum_id": _content_sha256(labels),
    }


def _audit_candidates(
    ledger: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    languages: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_task = {task["task_id"]: task for task in tasks}
    return [
        _candidate(
            by_task[decision["task_id"]],
            decision,
            languages[decision["canonical_repository_id"]]["language"],
        )
        for decision in ledger["decisions"]
        if decision["review_role"] == "primary"
        and decision["decision"] in FINAL_DECISIONS
    ]


def _rank(seed: str, stratum_id: str, task_id: str) -> tuple[str, str]:
    return _content_sha256([seed, stratum_id, task_id]), task_id


def _unseen_unique(
    ranked: Sequence[Mapping[str, Any]], seen_repositories: set[str]
) -> list[Mapping[str, Any]]:
    available = []
    reserved = set(seen_repositories)
    for item in ranked:
        repository = item["task"]["canonical_repository_id"]
        if repository not in reserved:
            available.append(item)
            reserved.add(repository)
    return available


def _select_candidates(
    candidates: Sequence[Mapping[str, Any]], seed: str
) -> tuple[list[Mapping[str, Any]], list[dict[str, str]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate["stratum_id"]].append(candidate)
    selected: list[Mapping[str, Any]] = []
    seen_repositories: set[str] = set()
    exceptions: list[dict[str, str]] = []
    for stratum_id in sorted(grouped, key=lambda key: (len(grouped[key]), key)):
        ranked = sorted(
            grouped[stratum_id],
            key=lambda item: _rank(seed, stratum_id, item["task"]["task_id"]),
        )
        target = _sample_size(len(ranked))
        chosen = _unseen_unique(ranked, seen_repositories)[:target]
        chosen_repositories = {
            item["task"]["canonical_repository_id"] for item in chosen
        }
        cross_stratum = _unseen_unique(ranked, chosen_repositories)[
            : target - len(chosen)
        ]
        chosen.extend(cross_stratum)
        within_stratum = [item for item in ranked if item not in chosen][
            : target - len(chosen)
        ]
        repeats = [*cross_stratum, *within_stratum]
        selected.extend([*chosen, *within_stratum])
        seen_repositories.update(
            item["task"]["canonical_repository_id"]
            for item in [*chosen, *within_stratum]
        )
        for item in repeats:
            repository = item["task"]["canonical_repository_id"]
            seen_repositories.add(repository)
            exceptions.append(
                {
                    "canonical_repository_id": repository,
                    "stratum_id": stratum_id,
                    "reason": "joint_stratum_target_would_be_unmet",
                }
            )
    return selected, exceptions


def _predecessors(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    tranche_hashes = [item["tranche_sha256"] for item in tranches]
    return {
        "decision_ledger_sha256": ledger["decision_ledger_sha256"],
        "tranche_sha256s": tranche_hashes,
        "tranche_set_sha256": _content_sha256(tranche_hashes),
        "language_inventory_sha256": inventory["inventory_sha256"],
        "case_index_sha256": ledger["case_index_sha256"],
        "workflow_sha256": ledger["workflow_sha256"],
    }


def _worksheet_task(
    candidate: Mapping[str, Any],
    predecessors: Mapping[str, Any],
    seed: str,
) -> dict[str, Any]:
    task = candidate["task"]
    identity = {
        "task_id": task["task_id"],
        "decision_ledger_sha256": predecessors["decision_ledger_sha256"],
        "seed": seed,
    }
    fields = (
        "task_id",
        "task_sha256",
        "canonical_repository_id",
        "sourcegraph_name",
        "event_id",
        "candidate_event",
        "candidate_kind",
        "query_family_ids",
        "packet_ids",
        "evidence",
        "review_question",
    )
    return {
        "audit_task_id": _content_sha256(identity),
        **{field: deepcopy(task[field]) for field in fields},
        "allowed_decisions": sorted(AUDIT_DECISIONS),
        "outcomes_consulted": False,
    }


def _key_item(candidate: Mapping[str, Any], audit_task_id: str) -> dict[str, Any]:
    decision = candidate["decision"]
    return {
        "audit_task_id": audit_task_id,
        "task_id": decision["task_id"],
        "task_sha256": decision["task_sha256"],
        "canonical_repository_id": decision["canonical_repository_id"],
        "event_id": decision["event_id"],
        "primary_response_sha256": decision["response_sha256"],
        "primary_decision": decision["decision"],
        "primary_reviewer_id": decision["reviewer_id"],
        "primary_rationale": decision["rationale"],
        "primary_default_branch_supported": decision["default_branch_supported"],
        "primary_evidence_tier": decision["evidence_tier"],
        "primary_evidence_citations": deepcopy(decision["evidence_citations"]),
        "stratum_id": candidate["stratum_id"],
        "evidence_channel": candidate["evidence_channel"],
        "language": candidate["language"],
    }


def _strata(
    candidates: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    populations = Counter(item["stratum_id"] for item in candidates)
    samples = Counter(item["stratum_id"] for item in selected)
    representative = {item["stratum_id"]: item for item in candidates}
    return [
        {
            "stratum_id": stratum_id,
            "primary_decision": representative[stratum_id]["primary_decision"],
            "evidence_channel": representative[stratum_id]["evidence_channel"],
            "language": representative[stratum_id]["language"],
            "population_count": count,
            "target_sample_count": _sample_size(count),
            "actual_sample_count": samples[stratum_id],
        }
        for stratum_id, count in sorted(populations.items())
    ]


def build_adoption_reliability_audit(
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    language_inventory: Mapping[str, Any],
    *,
    seed: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Emit a blind worksheet and a separately checksummed decision key."""
    if not isinstance(seed, str) or not seed:
        raise AdoptionAuditError("audit seed is required")
    tasks, languages = _validate_bindings(ledger, tranches, language_inventory)
    ordered = _ordered_tranches(tranches)
    candidates = _audit_candidates(ledger, tasks, languages)
    if not candidates:
        raise AdoptionAuditError("no final primary decisions are auditable")
    selected, exceptions = _select_candidates(candidates, seed)
    predecessors = _predecessors(ledger, ordered, language_inventory)
    worksheet_tasks = [
        _worksheet_task(candidate, predecessors, seed) for candidate in selected
    ]
    worksheet_document = _worksheet_document(
        seed, predecessors, worksheet_tasks, exceptions, candidates
    )
    worksheet = {
        **worksheet_document,
        "worksheet_sha256": audit_worksheet_sha256(worksheet_document),
    }
    key_document = _key_document(seed, predecessors, worksheet, candidates, selected)
    key = {**key_document, "key_sha256": audit_key_sha256(key_document)}
    return worksheet, key


def _worksheet_document(
    seed: str,
    predecessors: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    exceptions: Sequence[Mapping[str, str]],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "$schema": "sourcegraph-adoption-audit-worksheet.schema.json",
        "audit_version": AUDIT_VERSION,
        "seed": seed,
        "sampling_policy": _sampling_policy(),
        "predecessors": deepcopy(dict(predecessors)),
        "population_count": len(candidates),
        "joint_stratum_count": len({item["stratum_id"] for item in candidates}),
        "sample_count": len(tasks),
        "repository_repeat_exception_count": len(exceptions),
        "repository_repeat_exceptions": list(exceptions),
        "tasks": list(tasks),
        "scip_required": False,
        "model_or_api_calls": False,
        "outcomes_consulted": False,
    }


def _key_document(
    seed: str,
    predecessors: Mapping[str, Any],
    worksheet: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    selected: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    items = [
        _key_item(candidate, task["audit_task_id"])
        for candidate, task in zip(selected, worksheet["tasks"], strict=True)
    ]
    return {
        "$schema": "sourcegraph-adoption-audit-key.schema.json",
        "audit_version": AUDIT_VERSION,
        "seed": seed,
        "worksheet_sha256": worksheet["worksheet_sha256"],
        "predecessors": deepcopy(dict(predecessors)),
        "sample_count": len(items),
        "joint_stratum_count": worksheet["joint_stratum_count"],
        "strata": _strata(candidates, selected),
        "items": items,
        "outcomes_consulted": False,
    }


def compile_adoption_reliability_responses(
    worksheet: Mapping[str, Any],
    key: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
    tranches: Sequence[Mapping[str, Any]],
    language_inventory: Mapping[str, Any],
    *,
    seed: str,
    expected_worksheet_sha256: str,
    expected_key_sha256: str,
    expected_auditor_ids: Mapping[str, str],
) -> dict[str, Any]:
    """Rematerialize frozen sources and compile independently pinned responses."""
    from authorship.sourcegraph_adoption_audit_compiler import (
        compile_adoption_reliability_responses as compile_responses,
    )

    return compile_responses(
        worksheet,
        key,
        responses,
        ledger,
        tranches,
        language_inventory,
        seed=seed,
        expected_worksheet_sha256=expected_worksheet_sha256,
        expected_key_sha256=expected_key_sha256,
        expected_auditor_ids=expected_auditor_ids,
    )
