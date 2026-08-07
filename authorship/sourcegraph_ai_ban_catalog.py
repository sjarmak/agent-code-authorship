"""Compile the terminal AI-ban negative-control catalog and blind audit."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from authorship.sourcegraph_ai_ban_review import (
    AiBanReviewError,
    build_ai_ban_review_worksheet,
    compile_ai_ban_review_responses,
)
from authorship.sourcegraph_ai_ban_review_contracts import (
    ai_ban_ledger_sha256,
    ai_ban_target_manifest_sha256,
    ai_ban_worksheet_sha256,
)
from authorship.sourcegraph_repository_languages import (
    repository_language_inventory_sha256,
)

CATALOG_VERSION = 3
TRANCHE_COUNT = 9
EXPECTED_INPUT_COUNT = 39
PRIMARY_REVIEWER_ID = "ai-ban-primary-a"
AUDITOR_ID = "ai-ban-auditor-b"
AGREEMENT_FIELDS = (
    "decision",
    "default_branch_supported",
    "policy_scope",
    "evidence_tier",
)
BLIND_EXECUTION_FIELDS = (
    "outcomes_consulted",
    "classifier_outcomes_consulted",
    "survival_outcomes_consulted",
    "scip_required",
    "paid_api_used",
    "openai_api_key_used",
)


class AiBanCatalogError(ValueError):
    """Raised when catalog lineage or reliability evidence drifts."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def catalog_sha256(document: Mapping[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "catalog_sha256"}
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def catalog_input_paths(
    repo_root: Path,
    *,
    language_inventory_path: Path | None = None,
) -> dict[str, Path]:
    results = repo_root / "results/sourcegraph-ai-ban-review-v3"
    paths = {
        "target_manifest": repo_root
        / "study/sourcegraph-ai-ban-target-manifest.v3.json",
        "language_inventory": language_inventory_path
        or repo_root / "study/sourcegraph-ai-ban-languages.v3.json",
        "worksheet_001": repo_root
        / "study/sourcegraph-ai-ban-review-worksheet.v3.json",
    }
    paths.update(
        {
            f"worksheet_{index:03d}": results / f"worksheet-{index:03d}.json"
            for index in range(2, TRANCHE_COUNT + 1)
        }
    )
    paths["worksheet_010_terminal"] = results / "worksheet-010-terminal.json"
    paths.update(_tranche_paths(results, "decision_ledger", "decision-ledger", "json"))
    paths.update(
        _tranche_paths(
            results / "responses",
            "primary_responses",
            "worksheet",
            "primary-a.json",
        )
    )
    paths.update(
        _tranche_paths(
            results / "responses",
            "audit_responses",
            "worksheet",
            "audit-b.json",
        )
    )
    return paths


def _tranche_paths(
    root: Path,
    key_prefix: str,
    file_prefix: str,
    suffix: str,
) -> dict[str, Path]:
    separator = "-" if suffix != "json" else "."
    return {
        f"{key_prefix}_{index:03d}": (
            root / f"{file_prefix}-{index:03d}{separator}{suffix}"
        )
        for index in range(1, TRANCHE_COUNT + 1)
    }


def _read_pinned_inputs(
    paths: Mapping[str, Path],
    expected_sha256: Mapping[str, str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if (
        len(paths) != EXPECTED_INPUT_COUNT
        or set(paths) != set(expected_sha256)
        or any(not _is_sha256(value) for value in expected_sha256.values())
    ):
        raise AiBanCatalogError("independent pin set does not exactly cover inputs")
    documents = {}
    files = []
    for input_id, path in paths.items():
        payload = _read_bytes(path, input_id)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != expected_sha256[input_id]:
            raise AiBanCatalogError(f"{input_id} differs from independent pin")
        document = _decode_json(payload, input_id)
        if not _valid_root_contract(input_id, document):
            raise AiBanCatalogError(f"{input_id} root contract is invalid")
        documents[input_id] = document
        files.append({"input_id": input_id, "file_sha256": actual_sha256})
    return documents, files


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise AiBanCatalogError(f"cannot read {label}: {error}") from error


def _decode_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiBanCatalogError(f"{label} JSON is invalid") from error


def _valid_root_contract(input_id: str, document: Any) -> bool:
    if input_id.startswith(("primary_responses_", "audit_responses_")):
        return isinstance(document, list)
    return isinstance(document, Mapping)


def materialize_ai_ban_catalog(
    input_paths: Mapping[str, Path],
    expected_file_sha256: Mapping[str, str],
) -> dict[str, Any]:
    """Read every independently pinned predecessor and compile the catalog."""
    inputs, files = _read_pinned_inputs(input_paths, expected_file_sha256)
    return build_ai_ban_catalog(
        inputs["target_manifest"],
        [inputs[f"worksheet_{index:03d}"] for index in range(1, 10)],
        inputs["worksheet_010_terminal"],
        [inputs[f"decision_ledger_{index:03d}"] for index in range(1, 10)],
        [inputs[f"primary_responses_{index:03d}"] for index in range(1, 10)],
        [inputs[f"audit_responses_{index:03d}"] for index in range(1, 10)],
        inputs["language_inventory"],
        files,
    )


def build_ai_ban_catalog(
    target: Mapping[str, Any],
    worksheets: Sequence[Mapping[str, Any]],
    terminal_worksheet: Mapping[str, Any],
    ledgers: Sequence[Mapping[str, Any]],
    primary_bundles: Sequence[Sequence[Mapping[str, Any]]],
    audit_bundles: Sequence[Sequence[Mapping[str, Any]]],
    language_inventory: Mapping[str, Any],
    predecessor_files: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Validate terminal review lineage and build the frozen negative controls."""
    target_records = _validate_target(target)
    languages = _validate_languages(language_inventory, target_records)
    review = _validate_review_chain(
        target,
        worksheets,
        ledgers,
        primary_bundles,
        audit_bundles,
    )
    _validate_terminal(target, terminal_worksheet, ledgers[-1])
    repositories = _catalog_records(
        target_records,
        languages,
        review["decisions"],
        review["tasks"],
    )
    document = _catalog_document(
        target,
        language_inventory,
        terminal_worksheet,
        ledgers[-1],
        predecessor_files,
        repositories,
        review,
    )
    return {**document, "catalog_sha256": catalog_sha256(document)}


def _validate_target(
    target: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    if target.get("target_manifest_sha256") != ai_ban_target_manifest_sha256(target):
        raise AiBanCatalogError("target manifest checksum does not match")
    records = target.get("repositories")
    if (
        not isinstance(records, list)
        or target.get("repository_count") != 17
        or len(records) != 17
    ):
        raise AiBanCatalogError("target manifest contract does not match")
    if (
        _violates_blind_execution(target)
        or target.get("semantic_policy_decisions_made") is not False
    ):
        raise AiBanCatalogError("target manifest violates execution policy")
    indexed = {record.get("canonical_repository_id"): record for record in records}
    if len(indexed) != 17 or None in indexed:
        raise AiBanCatalogError("target repository identities are invalid")
    return indexed


def _validate_languages(
    inventory: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    if inventory.get("inventory_sha256") != repository_language_inventory_sha256(
        inventory
    ):
        raise AiBanCatalogError("language inventory checksum does not match")
    records = inventory.get("repositories")
    if (
        not isinstance(records, list)
        or inventory.get("repository_count") != len(records)
        or inventory.get("outcomes_consulted") is not False
        or inventory.get("scip_required") is not False
    ):
        raise AiBanCatalogError("language inventory contract does not match")
    if _contains_execution_policy_violation(inventory):
        raise AiBanCatalogError("language inventory violates execution policy")
    indexed = {record.get("canonical_repository_id"): record for record in records}
    if set(indexed) != set(targets) or len(indexed) != len(records):
        raise AiBanCatalogError("language inventory must exactly cover target controls")
    for repository, record in indexed.items():
        _validate_language_record(record, targets[repository])
    return {repository: record["language"] for repository, record in indexed.items()}


def _validate_language_record(
    record: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    sourcegraph_name = target.get("sourcegraph_name")
    if (
        not isinstance(record.get("language"), str)
        or not record["language"].strip()
        or record.get("review_sourcegraph_name") != sourcegraph_name
        or record.get("language_sourcegraph_name") != sourcegraph_name
        or not str(sourcegraph_name).startswith("github.com/sg-evals/")
    ):
        raise AiBanCatalogError("language inventory target binding does not match")


def _validate_review_chain(
    target: Mapping[str, Any],
    worksheets: Sequence[Mapping[str, Any]],
    ledgers: Sequence[Mapping[str, Any]],
    primary_bundles: Sequence[Sequence[Mapping[str, Any]]],
    audit_bundles: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    if not all(
        len(values) == TRANCHE_COUNT
        for values in (worksheets, ledgers, primary_bundles, audit_bundles)
    ):
        raise AiBanCatalogError("review chain must contain exactly nine tranches")
    prior = None
    tasks = {}
    audit_cases = []
    for worksheet, ledger, primary, audit in zip(
        worksheets, ledgers, primary_bundles, audit_bundles, strict=True
    ):
        _validate_worksheet_binding(worksheet, target, prior)
        compiled = _compile_bundle(worksheet, primary, prior, PRIMARY_REVIEWER_ID)
        if compiled != ledger:
            raise AiBanCatalogError("primary ledger is not an exact rematerialization")
        _compile_bundle(worksheet, audit, prior, AUDITOR_ID)
        audit_cases.extend(_audit_cases(worksheet, primary, audit))
        tasks.update({task["task_id"]: task for task in worksheet["tasks"]})
        prior = ledger
    _validate_audit_totals(audit_cases, prior)
    return {
        "decisions": deepcopy(prior["decisions"]),
        "tasks": tasks,
        "audit_cases": audit_cases,
    }


def _validate_worksheet_binding(
    worksheet: Mapping[str, Any],
    target: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
) -> None:
    expected_prior = prior.get("decision_ledger_sha256") if prior else None
    if (
        worksheet.get("worksheet_sha256") != ai_ban_worksheet_sha256(worksheet)
        or worksheet.get("target_manifest_sha256")
        != target.get("target_manifest_sha256")
        or worksheet.get("predecessors", {}).get("prior_decision_ledger_sha256")
        != expected_prior
    ):
        raise AiBanCatalogError("worksheet predecessor binding does not match")
    if (
        _violates_blind_execution(worksheet)
        or worksheet.get("semantic_policy_decisions_made") is not False
    ):
        raise AiBanCatalogError("worksheet violates execution policy")


def _violates_blind_execution(document: Mapping[str, Any]) -> bool:
    return any(document.get(field) is not False for field in BLIND_EXECUTION_FIELDS)


def _contains_execution_policy_violation(document: Mapping[str, Any]) -> bool:
    return any(
        field in document and document[field] is not False
        for field in BLIND_EXECUTION_FIELDS
    )


def _compile_bundle(
    worksheet: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    prior: Mapping[str, Any] | None,
    reviewer_id: str,
) -> dict[str, Any]:
    if not isinstance(responses, list):
        raise AiBanCatalogError("review response bundle must be a JSON array")
    reviewer_ids = {task["task_id"]: reviewer_id for task in worksheet["tasks"]}
    try:
        return compile_ai_ban_review_responses(
            worksheet,
            responses,
            prior_ledger=prior,
            expected_worksheet_sha256=worksheet["worksheet_sha256"],
            expected_reviewer_ids=reviewer_ids,
        )
    except AiBanReviewError as error:
        raise AiBanCatalogError(
            f"{reviewer_id} response validation failed: {error}"
        ) from error


def _audit_cases(
    worksheet: Mapping[str, Any],
    primary: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    tasks = {task["task_id"]: task for task in worksheet["tasks"]}
    audited = {response["task_id"]: response for response in audit}
    cases = []
    for response in primary:
        task = tasks[response["task_id"]]
        audit_response = audited[response["task_id"]]
        cases.append(
            {
                "canonical_repository_id": task["canonical_repository_id"],
                "event_id": task["event_id"],
                "task_id": task["task_id"],
                "evidence_channel": "+".join(sorted(task["query_family_ids"])),
                "primary_decision": response["decision"],
                "audit_decision": audit_response["decision"],
                "exact_agreement": all(
                    response[field] == audit_response[field]
                    for field in AGREEMENT_FIELDS
                ),
                "primary_response_sha256": response["response_sha256"],
                "audit_response_sha256": audit_response["response_sha256"],
            }
        )
    return cases


def _validate_audit_totals(
    cases: Sequence[Mapping[str, Any]],
    final_ledger: Mapping[str, Any],
) -> None:
    exact_count = sum(case["exact_agreement"] for case in cases)
    if (
        len(cases) != 25
        or exact_count != 25
        or final_ledger.get("decision_count") != 25
        or final_ledger.get("decision_ledger_sha256")
        != ai_ban_ledger_sha256(final_ledger)
    ):
        raise AiBanCatalogError("blind reliability audit is not 25/25 exact")


def _validate_terminal(
    target: Mapping[str, Any],
    terminal: Mapping[str, Any],
    final_ledger: Mapping[str, Any],
) -> None:
    try:
        rematerialized = build_ai_ban_review_worksheet(
            target,
            [],
            decision_ledger=final_ledger,
            expected_target_manifest_sha256=target["target_manifest_sha256"],
        )
    except AiBanReviewError as error:
        raise AiBanCatalogError(
            f"terminal review validation failed: {error}"
        ) from error
    if (
        terminal.get("task_count") != 0
        or terminal.get("tasks") != []
        or terminal != rematerialized
    ):
        raise AiBanCatalogError("terminal worksheet is not an exact rematerialization")


def _catalog_records(
    targets: Mapping[str, Mapping[str, Any]],
    languages: Mapping[str, str],
    decisions: Sequence[Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_repository: dict[str, list[Mapping[str, Any]]] = {
        repository: [] for repository in targets
    }
    for decision in decisions:
        by_repository[decision["canonical_repository_id"]].append(decision)
    return [
        _catalog_record(target, languages[repository], by_repository[repository], tasks)
        for repository, target in targets.items()
    ]


def _catalog_record(
    target: Mapping[str, Any],
    language: str,
    decisions: Sequence[Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    accepted = next(
        (decision for decision in decisions if decision["decision"] == "accept_policy"),
        None,
    )
    status = _repository_status(target, accepted)
    return {
        "preregistered_repository_id": target["preregistered_repository_id"],
        "canonical_repository_id": target["canonical_repository_id"],
        "canonical_source_url": target["canonical_source_url"],
        "sourcegraph_name": target["sourcegraph_name"],
        "language": language,
        "status": status,
        "scope_eligible": status == "dated_policy",
        "scope_exclusion_reason": None if status == "dated_policy" else status,
        "reviewed_event_count": len(decisions),
        "target_evidence": _target_evidence(target),
        "review_evidence": [
            _decision_evidence(decision, tasks[decision["task_id"]])
            for decision in decisions
        ],
        "accepted_policy": (
            _accepted_policy(accepted, tasks[accepted["task_id"]]) if accepted else None
        ),
    }


def _repository_status(
    target: Mapping[str, Any],
    accepted: Mapping[str, Any] | None,
) -> str:
    if accepted is not None:
        return "dated_policy"
    if (
        target.get("status") == "excluded"
        and target.get("exclusion_reason") == "no_frozen_ai_ban_candidate"
    ):
        return "no_frozen_candidate"
    if target.get("status") == "eligible":
        return "no_admissible_policy"
    raise AiBanCatalogError("target exclusion cannot be represented in final catalog")


def _target_evidence(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_status": target["status"],
        "exclusion_reason": target["exclusion_reason"],
        "cutoff_commit": target["cutoff_commit"],
        "case_id": target["case_id"],
        "repository_case_sha256": target["repository_case_sha256"],
        "candidate_count": target["candidate_count"],
    }


def _decision_evidence(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": task["event_id"],
        "observed_at": task["candidate_event"]["observed_at"],
        "commit_oid": task["candidate_event"]["commit_oid"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "response_sha256": response["response_sha256"],
        "decision": response["decision"],
        "rationale": response["rationale"],
        "default_branch_supported": response["default_branch_supported"],
        "policy_scope": response["policy_scope"],
        "evidence_tier": response["evidence_tier"],
        "evidence_citations": list(response["evidence_citations"]),
        "packet_ids": list(task["packet_ids"]),
        "query_family_ids": list(task["query_family_ids"]),
    }


def _accepted_policy(
    response: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": task["event_id"],
        "observed_at": task["candidate_event"]["observed_at"],
        "commit_oid": task["candidate_event"]["commit_oid"],
        "task_id": task["task_id"],
        "task_sha256": task["task_sha256"],
        "response_sha256": response["response_sha256"],
        "policy_scope": response["policy_scope"],
        "evidence_tier": response["evidence_tier"],
        "evidence_citations": list(response["evidence_citations"]),
        "packet_ids": list(task["packet_ids"]),
        "packet_sha256": [evidence["packet_sha256"] for evidence in task["evidence"]],
        "query_family_ids": list(task["query_family_ids"]),
    }


def _catalog_document(
    target: Mapping[str, Any],
    languages: Mapping[str, Any],
    terminal: Mapping[str, Any],
    final_ledger: Mapping[str, Any],
    predecessor_files: Sequence[Mapping[str, str]],
    repositories: Sequence[Mapping[str, Any]],
    review: Mapping[str, Any],
) -> dict[str, Any]:
    status_counts = Counter(record["status"] for record in repositories)
    cases = _language_audit_cases(review["audit_cases"], repositories)
    return {
        "$schema": "sourcegraph-ai-ban-catalog.schema.json",
        "catalog_version": CATALOG_VERSION,
        "predecessors": _predecessors(
            target, languages, terminal, final_ledger, predecessor_files
        ),
        "repository_count": len(repositories),
        "status_counts": dict(sorted(status_counts.items())),
        "repositories": list(repositories),
        "review": _review_summary(review["decisions"]),
        "audit": _audit_summary(cases),
        "terminal": {
            "worksheet_sha256": terminal["worksheet_sha256"],
            "final_decision_ledger_sha256": final_ledger["decision_ledger_sha256"],
            "task_count": terminal["task_count"],
        },
        "scope": "preregistered_ai_ban_negative_controls",
        "outcomes_consulted": False,
        "classifier_outcomes_consulted": False,
        "survival_outcomes_consulted": False,
        "scip_required": False,
        "paid_api_used": False,
        "openai_api_key_used": False,
    }


def _predecessors(
    target: Mapping[str, Any],
    languages: Mapping[str, Any],
    terminal: Mapping[str, Any],
    final_ledger: Mapping[str, Any],
    files: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "target_manifest_sha256": target["target_manifest_sha256"],
        "language_inventory_sha256": languages["inventory_sha256"],
        "terminal_worksheet_sha256": terminal["worksheet_sha256"],
        "final_decision_ledger_sha256": final_ledger["decision_ledger_sha256"],
        "files": list(files),
    }


def _review_summary(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    decision_counts = Counter(decision["decision"] for decision in decisions)
    return {
        "reviewer_id": PRIMARY_REVIEWER_ID,
        "reviewed_event_count": len(decisions),
        "decision_counts": dict(sorted(decision_counts.items())),
    }


def _language_audit_cases(
    cases: Sequence[Mapping[str, Any]],
    repositories: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    languages = {
        record["canonical_repository_id"]: record["language"] for record in repositories
    }
    return [
        {**case, "language": languages[case["canonical_repository_id"]]}
        for case in cases
    ]


def _audit_summary(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exact_count = sum(case["exact_agreement"] for case in cases)
    return {
        "auditor_id": AUDITOR_ID,
        "audited_event_count": len(cases),
        "exact_agreement_count": exact_count,
        "exact_agreement_rate": exact_count / len(cases),
        "agreement_fields": list(AGREEMENT_FIELDS),
        "strata": _audit_strata(cases),
        "cases": list(cases),
    }


def _audit_strata(
    cases: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for case in cases:
        key = (
            case["language"],
            case["evidence_channel"],
            case["primary_decision"],
        )
        grouped.setdefault(key, []).append(case)
    return [
        {
            "language": key[0],
            "evidence_channel": key[1],
            "primary_decision": key[2],
            "reviewed_event_count": len(values),
            "audited_event_count": len(values),
            "exact_agreement_count": sum(value["exact_agreement"] for value in values),
        }
        for key, values in sorted(grouped.items())
    ]
