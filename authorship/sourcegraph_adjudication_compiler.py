"""Compile peer-blind event reviews into packet-bound adjudication bundles."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_freeze import (
    adjudication_bundle_sha256,
)
from authorship.sourcegraph_adjudication_queue import (
    validate_adjudication_work_queue,
)
from authorship.sourcegraph_discovery import (
    agreement_audit_selected,
    validate_adjudication_bundle,
    validate_discovery_specification,
)

COMPILER_VERSION = 3
DEFAULT_BUNDLE_SHARD_COUNT = 256
MAX_BUNDLE_SHARD_COUNT = 4_096
DECISION_FIELDS = frozenset({"task_id", "task_sha256", "decision", "rationale"})
RESPONSE_FIELDS = frozenset(
    {
        "review_response_version",
        "queue_manifest_sha256",
        "stage",
        "reviewer_id",
        "reviewer_kind",
        "reviewer_version",
        "decision_count",
        "decisions",
        "outcome_blind",
        "peer_review_blind",
        "review_response_sha256",
    }
)
MODEL_RESPONSE_FIELDS = frozenset(
    {"provider", "model_id", "model_version", "prompt_sha256"}
)
MANIFEST_FIELDS = frozenset(
    {
        "bundle_execution_version",
        "specification_sha256",
        "packet_index_sha256",
        "queue_manifest_sha256",
        "review_response_sha256s",
        "task_count",
        "packet_count",
        "disagreement_task_count",
        "agreement_audit_task_count",
        "configured_shard_count",
        "emitted_shard_count",
        "shards",
        "outcomes_consulted",
        "bundle_manifest_sha256",
    }
)


class AdjudicationCompilerError(ValueError):
    """Raised when reviews cannot support packet-level adjudication."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def review_response_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "review_response_sha256"
    }
    return _content_sha256(content)


def bundle_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "bundle_manifest_sha256"
    }
    return _content_sha256(content)


def followup_assignment_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "followup_assignment_sha256"
    }
    return _content_sha256(content)


@dataclass(frozen=True)
class _PrimaryReviewState:
    tasks: Mapping[str, Mapping[str, Any]]
    ordered_responses: tuple[Mapping[str, Any], Mapping[str, Any]]
    response_maps: tuple[
        Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]]
    ]
    disagreement_tasks: frozenset[str]
    audit_tasks: frozenset[str]


def _is_hex(value: Any, length: int) -> bool:
    if not isinstance(value, str) or len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _load_tasks(
    queue_manifest: Mapping[str, Any], queue_root: Path
) -> dict[str, Mapping[str, Any]]:
    errors = validate_adjudication_work_queue(queue_manifest, queue_root)
    if errors:
        raise AdjudicationCompilerError(f"work queue is invalid: {'; '.join(errors)}")
    tasks = {}
    for shard in queue_manifest["shards"]:
        path = queue_root / shard["shard_file"]
        for raw_line in path.read_bytes().split(b"\n"):
            if not raw_line:
                continue
            task = json.loads(raw_line)
            tasks[task["task_id"]] = task
    return tasks


def _response_fields(response: Mapping[str, Any]) -> frozenset[str]:
    fields = RESPONSE_FIELDS
    if response.get("reviewer_kind") == "model":
        fields |= MODEL_RESPONSE_FIELDS
    return fields


def _allowed_decisions(
    specification: Mapping[str, Any], task: Mapping[str, Any]
) -> set[str]:
    return set(
        specification["adjudication"]["decision_sets"].get(task["packet_type"], [])
    )


def _validate_response_identity(
    response: Mapping[str, Any],
    *,
    stage: str,
    queue_manifest_sha256: str,
) -> None:
    if set(response) != _response_fields(response):
        raise AdjudicationCompilerError("review response has unapproved fields")
    if response.get("review_response_sha256") != review_response_sha256(response):
        raise AdjudicationCompilerError("review response checksum does not match")
    if response.get("reviewer_kind") == "model" and (
        any(
            not isinstance(response.get(field), str) or not response[field]
            for field in ("provider", "model_id", "model_version")
        )
        or not _is_hex(response.get("prompt_sha256"), 64)
    ):
        raise AdjudicationCompilerError(f"{stage} model metadata is invalid")
    if (
        response.get("review_response_version") != COMPILER_VERSION
        or response.get("queue_manifest_sha256") != queue_manifest_sha256
        or response.get("stage") != stage
        or response.get("reviewer_kind") not in {"human", "model"}
        or not isinstance(response.get("reviewer_id"), str)
        or not isinstance(response.get("reviewer_version"), str)
        or response.get("outcome_blind") is not True
        or response.get("peer_review_blind") is not True
    ):
        raise AdjudicationCompilerError(
            f"{stage} review response identity or blinding is invalid"
        )


def _response_map(
    response: Mapping[str, Any],
    *,
    stage: str,
    expected_task_ids: Set[str],
    tasks: Mapping[str, Mapping[str, Any]],
    queue_manifest_sha256: str,
    specification: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    _validate_response_identity(
        response,
        stage=stage,
        queue_manifest_sha256=queue_manifest_sha256,
    )
    decisions = response.get("decisions")
    if not isinstance(decisions, list) or any(
        not isinstance(decision, Mapping) for decision in decisions
    ):
        raise AdjudicationCompilerError(f"{stage} decisions must be objects")
    if response.get("decision_count") != len(decisions):
        raise AdjudicationCompilerError(f"{stage} decision count does not match")
    by_task = {}
    for decision in decisions:
        task_id = decision.get("task_id")
        task = tasks.get(task_id)
        if (
            set(decision) != DECISION_FIELDS
            or task is None
            or decision.get("task_sha256") != task["task_sha256"]
            or decision.get("decision") not in _allowed_decisions(specification, task)
            or not isinstance(decision.get("rationale"), str)
            or not decision["rationale"]
            or task_id in by_task
        ):
            raise AdjudicationCompilerError(f"{stage} decision is invalid")
        by_task[task_id] = decision
    if set(by_task) != expected_task_ids:
        raise AdjudicationCompilerError(f"{stage} task coverage does not match")
    return by_task


def _review(
    response: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    review = {
        "stage": response["stage"],
        "reviewer_id": response["reviewer_id"],
        "reviewer_kind": response["reviewer_kind"],
        "reviewer_version": response["reviewer_version"],
        "decision": decision["decision"],
        "outcome_blind": True,
        "peer_review_blind": True,
        "rationale": decision["rationale"],
    }
    if response["reviewer_kind"] == "model":
        review.update({field: response[field] for field in MODEL_RESPONSE_FIELDS})
    return review


def _selected_audit_tasks(
    tasks: Mapping[str, Mapping[str, Any]],
    agreements: Set[str],
    specification: Mapping[str, Any],
) -> set[str]:
    audit_specification = specification["adjudication"]["agreement_audit"]
    return {
        task_id
        for task_id in agreements
        if any(
            agreement_audit_selected(observation["packet_sha256"], audit_specification)
            for observation in tasks[task_id]["observations"]
        )
    }


def _optional_response_map(
    response: Mapping[str, Any] | None,
    *,
    stage: str,
    expected_task_ids: Set[str],
    tasks: Mapping[str, Mapping[str, Any]],
    queue_manifest_sha256: str,
    specification: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    if not expected_task_ids:
        if response is not None:
            raise AdjudicationCompilerError(
                f"unexpected {stage} response without assigned tasks"
            )
        return {}
    if response is None:
        raise AdjudicationCompilerError(f"{stage} response is required")
    return _response_map(
        response,
        stage=stage,
        expected_task_ids=expected_task_ids,
        tasks=tasks,
        queue_manifest_sha256=queue_manifest_sha256,
        specification=specification,
    )


def _packet_bundles(
    tasks: Mapping[str, Mapping[str, Any]],
    primary_responses: Sequence[Mapping[str, Any]],
    primary_maps: Sequence[Mapping[str, Mapping[str, Any]]],
    disagreement_tasks: set[str],
    resolution_response: Mapping[str, Any] | None,
    resolution_map: Mapping[str, Mapping[str, Any]],
    audit_response: Mapping[str, Any] | None,
    audit_map: Mapping[str, Mapping[str, Any]],
    specification: Mapping[str, Any],
) -> list[dict[str, Any]]:
    audit_specification = specification["adjudication"]["agreement_audit"]
    bundles = []
    for task_id, task in sorted(tasks.items()):
        base_reviews = [
            _review(response, decisions[task_id])
            for response, decisions in zip(primary_responses, primary_maps, strict=True)
        ]
        if task_id in disagreement_tasks:
            base_reviews.append(_review(resolution_response, resolution_map[task_id]))
        for observation in task["observations"]:
            reviews = list(base_reviews)
            selected = agreement_audit_selected(
                observation["packet_sha256"], audit_specification
            )
            if task_id in audit_map and selected:
                reviews.append(_review(audit_response, audit_map[task_id]))
            document = {
                "bundle_version": COMPILER_VERSION,
                "packet_id": observation["packet_id"],
                "packet_sha256": observation["packet_sha256"],
                "packet_type": task["packet_type"],
                "reviews": reviews,
            }
            bundle = {
                **document,
                "bundle_sha256": adjudication_bundle_sha256(document),
            }
            errors = validate_adjudication_bundle(bundle, specification)
            if errors:
                raise AdjudicationCompilerError(
                    f"compiled bundle is invalid: {'; '.join(errors)}"
                )
            bundles.append(bundle)
    return sorted(bundles, key=lambda bundle: bundle["packet_id"])


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_bundle_shards(
    bundles: Sequence[Mapping[str, Any]],
    output_root: Path,
    shard_count: int,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for bundle in bundles:
        grouped[int(bundle["packet_id"][:8], 16) % shard_count].append(bundle)
    width = max(4, len(str(shard_count - 1)))
    records = []
    for shard_number in sorted(grouped):
        selected = sorted(grouped[shard_number], key=lambda bundle: bundle["packet_id"])
        shard_id = f"{shard_number:0{width}d}"
        shard_file = f"shards/{shard_id}.jsonl"
        payload = "".join(
            f"{_canonical_json(bundle)}\n" for bundle in selected
        ).encode()
        _atomic_bytes(output_root / shard_file, payload)
        records.append(
            {
                "shard_id": shard_id,
                "shard_file": shard_file,
                "bundle_count": len(selected),
                "first_packet_id": selected[0]["packet_id"],
                "last_packet_id": selected[-1]["packet_id"],
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return records


def _validated_shard_count(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BUNDLE_SHARD_COUNT
    ):
        raise AdjudicationCompilerError(
            f"shard_count must be between 1 and {MAX_BUNDLE_SHARD_COUNT}"
        )
    return value


def _primary_review_state(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    queue_root: Path,
    primary_responses: Sequence[Mapping[str, Any]],
) -> _PrimaryReviewState:
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise AdjudicationCompilerError(
            f"specification is invalid: {'; '.join(specification_errors)}"
        )
    tasks = _load_tasks(queue_manifest, queue_root)
    if len(primary_responses) != 2:
        raise AdjudicationCompilerError(
            "exactly two independent primary responses are required"
        )
    if any(
        not isinstance(response, Mapping)
        or not isinstance(response.get("reviewer_id"), str)
        for response in primary_responses
    ):
        raise AdjudicationCompilerError("primary response identity is invalid")
    ordered = tuple(sorted(primary_responses, key=lambda item: item["reviewer_id"]))
    if ordered[0]["reviewer_id"] == ordered[1]["reviewer_id"]:
        raise AdjudicationCompilerError("primary reviewers must be independent")
    task_ids = frozenset(tasks)
    queue_sha = queue_manifest["queue_manifest_sha256"]
    response_maps = tuple(
        _response_map(
            response,
            stage="primary",
            expected_task_ids=task_ids,
            tasks=tasks,
            queue_manifest_sha256=queue_sha,
            specification=specification,
        )
        for response in ordered
    )
    disagreements = frozenset(
        task_id
        for task_id in task_ids
        if response_maps[0][task_id]["decision"]
        != response_maps[1][task_id]["decision"]
    )
    audits = frozenset(
        _selected_audit_tasks(tasks, task_ids - disagreements, specification)
    )
    return _PrimaryReviewState(tasks, ordered, response_maps, disagreements, audits)


def build_followup_assignments(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    queue_root: Path,
    primary_responses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build peer-blind resolver and agreement-auditor assignments."""
    state = _primary_review_state(
        specification, queue_manifest, queue_root, primary_responses
    )

    def assigned_tasks(task_ids: Set[str]) -> list[dict[str, str]]:
        return [
            {
                "task_id": task_id,
                "task_sha256": state.tasks[task_id]["task_sha256"],
            }
            for task_id in sorted(task_ids)
        ]

    document = {
        "followup_assignment_version": COMPILER_VERSION,
        "queue_manifest_sha256": queue_manifest["queue_manifest_sha256"],
        "primary_review_response_sha256s": sorted(
            response["review_response_sha256"] for response in state.ordered_responses
        ),
        "resolution_task_count": len(state.disagreement_tasks),
        "resolution_tasks": assigned_tasks(state.disagreement_tasks),
        "agreement_audit_task_count": len(state.audit_tasks),
        "agreement_audit_tasks": assigned_tasks(state.audit_tasks),
        "primary_decisions_exposed": False,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "followup_assignment_sha256": followup_assignment_sha256(document),
    }


def _followup_response_maps(
    state: _PrimaryReviewState,
    specification: Mapping[str, Any],
    queue_manifest_sha256: str,
    resolution_response: Mapping[str, Any] | None,
    audit_response: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]:
    resolution_map = _optional_response_map(
        resolution_response,
        stage="resolution",
        expected_task_ids=state.disagreement_tasks,
        tasks=state.tasks,
        queue_manifest_sha256=queue_manifest_sha256,
        specification=specification,
    )
    audit_map = _optional_response_map(
        audit_response,
        stage="agreement_audit",
        expected_task_ids=state.audit_tasks,
        tasks=state.tasks,
        queue_manifest_sha256=queue_manifest_sha256,
        specification=specification,
    )
    primary_ids = {response["reviewer_id"] for response in state.ordered_responses}
    if any(
        response is not None and response.get("reviewer_id") in primary_ids
        for response in (resolution_response, audit_response)
    ):
        raise AdjudicationCompilerError(
            "resolution and audit reviewers must be independent"
        )
    return resolution_map, audit_map


def _bundle_execution_document(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    state: _PrimaryReviewState,
    bundles: Sequence[Mapping[str, Any]],
    shards: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    shard_count: int,
) -> dict[str, Any]:
    return {
        "bundle_execution_version": COMPILER_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "packet_index_sha256": queue_manifest["packet_index_sha256"],
        "queue_manifest_sha256": queue_manifest["queue_manifest_sha256"],
        "review_response_sha256s": sorted(
            response["review_response_sha256"] for response in responses
        ),
        "task_count": len(state.tasks),
        "packet_count": len(bundles),
        "disagreement_task_count": len(state.disagreement_tasks),
        "agreement_audit_task_count": len(state.audit_tasks),
        "configured_shard_count": shard_count,
        "emitted_shard_count": len(shards),
        "shards": list(shards),
        "outcomes_consulted": False,
    }


def compile_adjudication_bundles(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    queue_root: Path,
    primary_responses: Sequence[Mapping[str, Any]],
    output_root: Path,
    *,
    resolution_response: Mapping[str, Any] | None = None,
    audit_response: Mapping[str, Any] | None = None,
    shard_count: int = DEFAULT_BUNDLE_SHARD_COUNT,
) -> dict[str, Any]:
    """Validate independent reviews and expand them to packet bundles."""
    selected_shard_count = _validated_shard_count(shard_count)
    state = _primary_review_state(
        specification, queue_manifest, queue_root, primary_responses
    )
    queue_sha = queue_manifest["queue_manifest_sha256"]
    resolution_map, audit_map = _followup_response_maps(
        state,
        specification,
        queue_sha,
        resolution_response,
        audit_response,
    )
    bundles = _packet_bundles(
        state.tasks,
        state.ordered_responses,
        state.response_maps,
        state.disagreement_tasks,
        resolution_response,
        resolution_map,
        audit_response,
        audit_map,
        specification,
    )
    shards = _write_bundle_shards(bundles, output_root, selected_shard_count)
    responses = [
        *state.ordered_responses,
        *([resolution_response] if resolution_response is not None else []),
        *([audit_response] if audit_response is not None else []),
    ]
    document = _bundle_execution_document(
        specification,
        queue_manifest,
        state,
        bundles,
        shards,
        responses,
        selected_shard_count,
    )
    return {
        **document,
        "bundle_manifest_sha256": bundle_manifest_sha256(document),
    }


def load_compiled_bundles(
    manifest: Mapping[str, Any], output_root: Path
) -> list[dict[str, Any]]:
    """Load and checksum-verify deterministic bundle shards."""
    if (
        set(manifest) != MANIFEST_FIELDS
        or manifest.get("bundle_manifest_sha256") != bundle_manifest_sha256(manifest)
        or manifest.get("bundle_execution_version") != COMPILER_VERSION
        or manifest.get("outcomes_consulted") is not False
    ):
        raise AdjudicationCompilerError("bundle manifest is invalid")
    bundles = []
    for shard in manifest["shards"]:
        path = output_root / shard["shard_file"]
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != shard["sha256"]:
            raise AdjudicationCompilerError("bundle shard checksum does not match")
        records = [json.loads(line) for line in payload.splitlines()]
        if len(records) != shard["bundle_count"] or [
            record["packet_id"] for record in records
        ] != sorted(record["packet_id"] for record in records):
            raise AdjudicationCompilerError("bundle shard contract is invalid")
        bundles.extend(records)
    packet_ids = [bundle["packet_id"] for bundle in bundles]
    if len(bundles) != manifest["packet_count"] or len(packet_ids) != len(
        set(packet_ids)
    ):
        raise AdjudicationCompilerError("compiled packet coverage is invalid")
    return sorted(bundles, key=lambda bundle: bundle["packet_id"])


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Write a canonical JSON document atomically."""
    _atomic_bytes(path, f"{_canonical_json(document)}\n".encode())
