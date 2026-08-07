"""Export and import outcome-blind Sourcegraph adjudication model batches."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adjudication_compiler import review_response_sha256
from authorship.sourcegraph_adjudication_queue import (
    validate_adjudication_work_queue,
)
from authorship.sourcegraph_discovery import validate_discovery_specification

BATCH_VERSION = 3
DEFAULT_MAX_REQUESTS = 49_000
HARD_MAX_REQUESTS = 50_000
DEFAULT_MAX_BYTES = 190_000_000
HARD_MAX_BYTES = 200_000_000
MAX_OUTPUT_TOKENS = 512
REVIEWER_VERSION = "sourcegraph-adjudication-openai-batch-v3"
REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
REQUEST_FIELDS = frozenset({"custom_id", "method", "url", "body"})
BATCH_OUTPUT_FIELDS = frozenset({"id", "custom_id", "response", "error"})
RESPONSE_ENVELOPE_FIELDS = frozenset({"status_code", "request_id", "body"})
DECISION_FIELDS = frozenset({"task_id", "task_sha256", "decision", "rationale"})
FILE_FIELDS = frozenset(
    {
        "file",
        "request_count",
        "byte_count",
        "first_custom_id",
        "last_custom_id",
        "sha256",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "batch_export_version",
        "specification_sha256",
        "queue_manifest_sha256",
        "stage",
        "reviewer_id",
        "reviewer_kind",
        "reviewer_version",
        "provider",
        "model",
        "reasoning_effort",
        "prompt_sha256",
        "endpoint",
        "request_count",
        "max_requests_per_file",
        "max_bytes_per_file",
        "emitted_file_count",
        "files",
        "outcomes_consulted",
        "peer_reviews_consulted",
        "batch_manifest_sha256",
    }
)


class AdjudicationBatchError(ValueError):
    """Raised when review batches violate the frozen adjudication contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def batch_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "batch_manifest_sha256"
    }
    return _content_sha256(content)


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


def _jsonl_records(payload: bytes, label: str) -> list[Mapping[str, Any]]:
    records = []
    try:
        for line in payload.split(b"\n"):
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, Mapping):
                raise AdjudicationBatchError(f"{label} records must be objects")
            records.append(record)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdjudicationBatchError(f"{label} is not valid JSONL") from error
    return records


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise AdjudicationBatchError(f"cannot read {label}: {error}") from error


def _load_tasks(
    queue_manifest: Mapping[str, Any], queue_root: Path
) -> dict[str, Mapping[str, Any]]:
    errors = validate_adjudication_work_queue(queue_manifest, queue_root)
    if errors:
        raise AdjudicationBatchError(f"work queue is invalid: {'; '.join(errors)}")
    tasks = {}
    for shard in queue_manifest["shards"]:
        path = queue_root / shard["shard_file"]
        for task in _jsonl_records(_read_bytes(path, "work queue"), "work queue"):
            tasks[task["task_id"]] = task
    return tasks


def _review_prompt(specification: Mapping[str, Any]) -> str:
    rubrics = specification["adjudication"]["rubrics"]
    return (
        "You are an independent, outcome-blind research adjudicator. "
        "Review only the frozen Sourcegraph evidence task in the user input. "
        "Do not consult or infer classifier scores, code-survival outcomes, another "
        "reviewer's decision, or evidence outside the task. Apply the following "
        "rubrics exactly. If the frozen evidence cannot establish a required fact, "
        "choose insufficient rather than guessing.\n\n"
        f"adoption_event rubric:\n{_canonical_json(rubrics['adoption_event'])}\n\n"
        f"ai_ban_policy rubric:\n{_canonical_json(rubrics['ai_ban_policy'])}\n\n"
        "Return the task_id and task_sha256 unchanged, one allowed decision, and a "
        "brief evidence-specific rationale as JSON matching the supplied schema."
    )


def _decision_schema(decisions: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "task_sha256": {"type": "string"},
            "decision": {"type": "string", "enum": list(decisions)},
            "rationale": {"type": "string"},
        },
        "required": ["task_id", "task_sha256", "decision", "rationale"],
        "additionalProperties": False,
    }


def _request(
    task: Mapping[str, Any],
    specification: Mapping[str, Any],
    prompt: str,
    model: str,
    reasoning_effort: str,
) -> dict[str, Any]:
    decisions = specification["adjudication"]["decision_sets"][task["packet_type"]]
    return {
        "custom_id": task["task_id"],
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": model,
            "instructions": prompt,
            "input": _canonical_json(task),
            "reasoning": {"effort": reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sourcegraph_adjudication_decision_v3",
                    "schema": _decision_schema(decisions),
                    "strict": True,
                }
            },
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        },
    }


def _validated_limit(value: Any, hard_limit: int, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= hard_limit
    ):
        raise AdjudicationBatchError(f"{label} must be between 1 and {hard_limit}")
    return value


def _write_request_files(
    requests: Iterable[Mapping[str, Any]],
    output_root: Path,
    max_requests: int,
    max_bytes: int,
) -> list[dict[str, Any]]:
    files = []
    payload = bytearray()
    custom_ids: list[str] = []
    for request in requests:
        line = f"{_canonical_json(request)}\n".encode()
        if len(line) > max_bytes:
            raise AdjudicationBatchError(
                "one batch request exceeds the file byte limit"
            )
        if payload and (
            len(custom_ids) >= max_requests or len(payload) + len(line) > max_bytes
        ):
            files.append(_write_request_file(output_root, files, payload, custom_ids))
            payload, custom_ids = bytearray(), []
        payload.extend(line)
        custom_ids.append(request["custom_id"])
    if payload:
        files.append(_write_request_file(output_root, files, payload, custom_ids))
    return files


def _write_request_file(
    output_root: Path,
    prior_files: Sequence[Mapping[str, Any]],
    payload: bytearray,
    custom_ids: Sequence[str],
) -> dict[str, Any]:
    file_name = f"requests/{len(prior_files):04d}.jsonl"
    immutable_payload = bytes(payload)
    _atomic_bytes(output_root / file_name, immutable_payload)
    return {
        "file": file_name,
        "request_count": len(custom_ids),
        "byte_count": len(immutable_payload),
        "first_custom_id": custom_ids[0],
        "last_custom_id": custom_ids[-1],
        "sha256": hashlib.sha256(immutable_payload).hexdigest(),
    }


def _export_document(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    reviewer_id: str,
    model: str,
    reasoning_effort: str,
    prompt: str,
    request_count: int,
    request_limit: int,
    byte_limit: int,
    files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "batch_export_version": BATCH_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "queue_manifest_sha256": queue_manifest["queue_manifest_sha256"],
        "stage": "primary",
        "reviewer_id": reviewer_id,
        "reviewer_kind": "model",
        "reviewer_version": REVIEWER_VERSION,
        "provider": "openai",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "endpoint": "/v1/responses",
        "request_count": request_count,
        "max_requests_per_file": request_limit,
        "max_bytes_per_file": byte_limit,
        "emitted_file_count": len(files),
        "files": list(files),
        "outcomes_consulted": False,
        "peer_reviews_consulted": False,
    }


def _export_limits(
    specification: Mapping[str, Any],
    reviewer_id: str,
    model: str,
    reasoning_effort: str,
    max_requests_per_file: int,
    max_bytes_per_file: int,
) -> tuple[int, int]:
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise AdjudicationBatchError(
            f"specification is invalid: {'; '.join(specification_errors)}"
        )
    if not reviewer_id or not model or reasoning_effort not in REASONING_EFFORTS:
        raise AdjudicationBatchError("reviewer, model, or reasoning effort is invalid")
    return (
        _validated_limit(
            max_requests_per_file, HARD_MAX_REQUESTS, "max_requests_per_file"
        ),
        _validated_limit(max_bytes_per_file, HARD_MAX_BYTES, "max_bytes_per_file"),
    )


def export_openai_review_batch(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    queue_root: Path,
    output_root: Path,
    *,
    reviewer_id: str,
    model: str,
    reasoning_effort: str = "low",
    max_requests_per_file: int = DEFAULT_MAX_REQUESTS,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Export deterministic OpenAI Batch API request files without submitting them."""
    request_limit, byte_limit = _export_limits(
        specification,
        reviewer_id,
        model,
        reasoning_effort,
        max_requests_per_file,
        max_bytes_per_file,
    )
    tasks = _load_tasks(queue_manifest, queue_root)
    prompt = _review_prompt(specification)
    files = _write_request_files(
        (
            _request(task, specification, prompt, model, reasoning_effort)
            for _, task in sorted(tasks.items())
        ),
        output_root,
        request_limit,
        byte_limit,
    )
    document = _export_document(
        specification,
        queue_manifest,
        reviewer_id,
        model,
        reasoning_effort,
        prompt,
        len(tasks),
        request_limit,
        byte_limit,
        files,
    )
    return {**document, "batch_manifest_sha256": batch_manifest_sha256(document)}


def _safe_batch_file(output_root: Path, value: Any) -> Path:
    relative = Path(value) if isinstance(value, str) else Path()
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "requests"
        or ".." in relative.parts
    ):
        raise AdjudicationBatchError("batch request file path is invalid")
    return output_root / relative


def _expected_request_ids(
    manifest: Mapping[str, Any],
    output_root: Path,
    tasks: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    files = _validated_manifest_files(manifest)
    request_ids = []
    for record in files:
        payload = _read_bytes(
            _safe_batch_file(output_root, record.get("file")),
            "batch request file",
        )
        if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
            raise AdjudicationBatchError("batch request checksum does not match")
        requests = _jsonl_records(payload, "batch request file")
        _validate_file_record(record, payload, requests, manifest)
        for request in requests:
            _validate_request(request, manifest, tasks)
            request_ids.append(request["custom_id"])
    if (
        len(request_ids) != manifest.get("request_count")
        or len(request_ids) != len(set(request_ids))
        or set(request_ids) != set(tasks)
    ):
        raise AdjudicationBatchError("batch request task coverage does not match")
    return set(request_ids)


def _validated_manifest_files(
    manifest: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]]:
    files = manifest.get("files")
    if (
        set(manifest) != MANIFEST_FIELDS
        or manifest.get("batch_manifest_sha256") != batch_manifest_sha256(manifest)
        or manifest.get("batch_export_version") != BATCH_VERSION
        or manifest.get("stage") != "primary"
        or manifest.get("reviewer_kind") != "model"
        or manifest.get("provider") != "openai"
        or manifest.get("endpoint") != "/v1/responses"
        or manifest.get("outcomes_consulted") is not False
        or manifest.get("peer_reviews_consulted") is not False
        or not isinstance(files, list)
        or any(not isinstance(record, Mapping) for record in files)
        or manifest.get("emitted_file_count") != len(files or [])
    ):
        raise AdjudicationBatchError("batch manifest is invalid")
    _validated_limit(
        manifest.get("max_requests_per_file"),
        HARD_MAX_REQUESTS,
        "max_requests_per_file",
    )
    _validated_limit(
        manifest.get("max_bytes_per_file"), HARD_MAX_BYTES, "max_bytes_per_file"
    )
    return files


def _validate_file_record(
    record: Mapping[str, Any],
    payload: bytes,
    requests: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> None:
    custom_ids = [request.get("custom_id") for request in requests]
    if (
        set(record) != FILE_FIELDS
        or len(payload) != record.get("byte_count")
        or len(requests) != record.get("request_count")
        or not custom_ids
        or custom_ids[0] != record.get("first_custom_id")
        or custom_ids[-1] != record.get("last_custom_id")
        or len(requests) > manifest["max_requests_per_file"]
        or len(payload) > manifest["max_bytes_per_file"]
    ):
        raise AdjudicationBatchError("batch file metadata does not match")


def _validate_request(
    request: Mapping[str, Any],
    manifest: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
) -> None:
    task = tasks.get(request.get("custom_id"))
    body = request.get("body")
    if (
        set(request) != REQUEST_FIELDS
        or request.get("method") != "POST"
        or request.get("url") != "/v1/responses"
        or not isinstance(body, Mapping)
        or task is None
        or body.get("model") != manifest.get("model")
        or body.get("store") is not False
        or hashlib.sha256(str(body.get("instructions", "")).encode()).hexdigest()
        != manifest.get("prompt_sha256")
        or body.get("input") != _canonical_json(task)
    ):
        raise AdjudicationBatchError("batch request contract is invalid")


def _output_text(record: Mapping[str, Any], model: str) -> str:
    if set(record) != BATCH_OUTPUT_FIELDS or record.get("error") is not None:
        raise AdjudicationBatchError("batch output contains an API error")
    response = record.get("response")
    if (
        not isinstance(response, Mapping)
        or set(response) != RESPONSE_ENVELOPE_FIELDS
        or response.get("status_code") != 200
    ):
        raise AdjudicationBatchError("batch response envelope is invalid")
    body = response.get("body")
    if (
        not isinstance(body, Mapping)
        or body.get("status") != "completed"
        or body.get("model") != model
    ):
        raise AdjudicationBatchError("model response is incomplete or mismatched")
    content = [
        part
        for item in body.get("output", [])
        if isinstance(item, Mapping) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, Mapping)
    ]
    if any(part.get("type") == "refusal" for part in content):
        raise AdjudicationBatchError("model response contains a refusal")
    texts = [part.get("text") for part in content if part.get("type") == "output_text"]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise AdjudicationBatchError("model response has no unique output text")
    return texts[0]


def _decision(
    record: Mapping[str, Any],
    task: Mapping[str, Any],
    specification: Mapping[str, Any],
    model: str,
) -> dict[str, str]:
    try:
        decision = json.loads(_output_text(record, model))
    except json.JSONDecodeError as error:
        raise AdjudicationBatchError("model output is not valid JSON") from error
    allowed = specification["adjudication"]["decision_sets"][task["packet_type"]]
    if (
        not isinstance(decision, Mapping)
        or set(decision) != DECISION_FIELDS
        or decision.get("task_id") != task["task_id"]
        or decision.get("task_sha256") != task["task_sha256"]
        or decision.get("decision") not in allowed
        or not isinstance(decision.get("rationale"), str)
        or not decision["rationale"]
    ):
        raise AdjudicationBatchError("model decision violates the frozen contract")
    return dict(decision)


def import_openai_review_batch(
    specification: Mapping[str, Any],
    queue_manifest: Mapping[str, Any],
    queue_root: Path,
    batch_manifest: Mapping[str, Any],
    batch_root: Path,
    output_files: Sequence[Path],
) -> dict[str, Any]:
    """Import completed Batch API files into one compiler-valid review response."""
    tasks = _load_tasks(queue_manifest, queue_root)
    expected_ids = _expected_request_ids(batch_manifest, batch_root, tasks)
    outputs = {}
    for path in output_files:
        payload = _read_bytes(path, "batch output")
        for record in _jsonl_records(payload, "batch output"):
            custom_id = record.get("custom_id")
            if custom_id in outputs:
                raise AdjudicationBatchError("batch output custom_id is duplicated")
            outputs[custom_id] = record
    if set(outputs) != expected_ids:
        raise AdjudicationBatchError("batch output task coverage does not match")
    decisions = [
        _decision(
            outputs[task_id],
            tasks[task_id],
            specification,
            batch_manifest["model"],
        )
        for task_id in sorted(expected_ids)
    ]
    document = {
        "review_response_version": BATCH_VERSION,
        "queue_manifest_sha256": queue_manifest["queue_manifest_sha256"],
        "stage": batch_manifest["stage"],
        "reviewer_id": batch_manifest["reviewer_id"],
        "reviewer_kind": "model",
        "reviewer_version": batch_manifest["reviewer_version"],
        "decision_count": len(decisions),
        "decisions": decisions,
        "outcome_blind": True,
        "peer_review_blind": True,
        "provider": batch_manifest["provider"],
        "model_id": batch_manifest["model"],
        "model_version": batch_manifest["model"],
        "prompt_sha256": batch_manifest["prompt_sha256"],
    }
    return {**document, "review_response_sha256": review_response_sha256(document)}
