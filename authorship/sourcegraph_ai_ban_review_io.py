"""Pinned file loading for the bounded Sourcegraph AI-ban review."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from authorship.sourcegraph_ai_ban_review import (
    AiBanReviewError,
    build_ai_ban_review_worksheet,
    build_ai_ban_target_manifest,
    required_ai_ban_packet_ids,
)
from authorship.sourcegraph_repository_case_stream import (
    validated_packet_stream_from_file,
)
from authorship.sourcegraph_repository_cases import repository_case_sha256


@dataclass(frozen=True)
class _MaterializationRequest:
    control_evidence_path: Path
    index_manifest_path: Path
    case_index_path: Path
    case_root: Path
    discovery_path: Path
    workflow_path: Path
    packet_index_path: Path
    decision_ledger_path: Path | None
    expected_control_evidence_sha256: str
    expected_index_manifest_sha256: str
    expected_case_index_file_sha256: str
    expected_case_index_sha256: str
    expected_packet_index_sha256: str
    expected_decision_ledger_sha256: str | None


def _read_pinned(path: Path, expected_sha256: str, label: str) -> Mapping[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AiBanReviewError(f"cannot read {label}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AiBanReviewError(f"{label} differs from independent pin")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiBanReviewError(f"{label} JSON is invalid") from error
    if not isinstance(document, Mapping):
        raise AiBanReviewError(f"{label} contract is invalid")
    return document


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_bytes())
    except OSError as error:
        raise AiBanReviewError(f"cannot read {label}: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiBanReviewError(f"{label} JSON is invalid") from error
    if not isinstance(document, Mapping):
        raise AiBanReviewError(f"{label} contract is invalid")
    return document


def _load_cases(
    control: Mapping[str, Any],
    case_index: Mapping[str, Any],
    case_root: Path,
) -> dict[str, Mapping[str, Any]]:
    control_ids = {value.lower() for value in control["repos"]}
    records = {
        record["canonical_repository_id"]: record
        for record in case_index["repositories"]
        if record["canonical_repository_id"] in control_ids
    }
    root = case_root.resolve()
    cases = {}
    for repository, record in records.items():
        path = (case_root / record["case_file"]).resolve()
        if not path.is_relative_to(root):
            raise AiBanReviewError("repository case path escapes case root")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise AiBanReviewError(f"cannot read repository case: {error}") from error
        if len(payload) != record.get("byte_count") or hashlib.sha256(
            payload
        ).hexdigest() != record.get("sha256"):
            raise AiBanReviewError("repository case file checksum does not match")
        try:
            case = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AiBanReviewError("repository case JSON is invalid") from error
        if not isinstance(case, Mapping) or case.get(
            "repository_case_sha256"
        ) != repository_case_sha256(case):
            raise AiBanReviewError("repository case content checksum does not match")
        cases[repository] = case
    return cases


def _selected_packets(
    discovery: Mapping[str, Any],
    workflow: Mapping[str, Any],
    packet_index_path: Path,
    required_ids: set[str],
    expected_packet_index_sha256: str,
) -> list[Mapping[str, Any]]:
    header, stream = validated_packet_stream_from_file(
        discovery, workflow, packet_index_path
    )
    if header.get("packet_index_sha256") != expected_packet_index_sha256:
        raise AiBanReviewError("packet index differs from independent pin")
    selected = {}
    for packet in stream:
        packet_id = packet["packet_id"]
        if packet_id in required_ids:
            selected[packet_id] = packet
    missing = required_ids - set(selected)
    if missing:
        raise AiBanReviewError(f"missing evidence packet {sorted(missing)[0]}")
    return [selected[packet_id] for packet_id in sorted(selected)]


def _review_context(
    request: _MaterializationRequest,
    control: Mapping[str, Any],
    case_index: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if case_index.get("case_index_sha256") != request.expected_case_index_sha256:
        raise AiBanReviewError("case index content differs from independent pin")
    discovery = _read_json(request.discovery_path, "discovery specification")
    workflow = _read_json(request.workflow_path, "adjudication workflow")
    cases = _load_cases(control, case_index, request.case_root)
    predecessors = _predecessors(
        case_index,
        request.expected_control_evidence_sha256,
        request.expected_index_manifest_sha256,
        request.expected_case_index_file_sha256,
        request.expected_case_index_sha256,
        request.expected_packet_index_sha256,
    )
    return discovery, workflow, cases, predecessors


def _materialize_request(
    request: _MaterializationRequest,
) -> tuple[dict[str, Any], dict[str, Any]]:
    decision_ledger = _optional_decision_ledger(
        request.decision_ledger_path,
        request.expected_decision_ledger_sha256,
    )
    control, index_manifest, case_index = _load_pinned_inputs(
        request.control_evidence_path,
        request.index_manifest_path,
        request.case_index_path,
        request.expected_control_evidence_sha256,
        request.expected_index_manifest_sha256,
        request.expected_case_index_file_sha256,
    )
    discovery, workflow, cases, predecessors = _review_context(
        request,
        control,
        case_index,
    )
    return _materialize_loaded(
        control,
        index_manifest,
        case_index,
        cases,
        predecessors,
        discovery,
        workflow,
        request.packet_index_path,
        request.expected_packet_index_sha256,
        decision_ledger,
    )


def materialize_ai_ban_review(
    *,
    control_evidence_path: Path,
    index_manifest_path: Path,
    case_index_path: Path,
    case_root: Path,
    discovery_path: Path,
    workflow_path: Path,
    packet_index_path: Path,
    decision_ledger_path: Path | None = None,
    expected_control_evidence_sha256: str,
    expected_index_manifest_sha256: str,
    expected_case_index_file_sha256: str,
    expected_case_index_sha256: str,
    expected_packet_index_sha256: str,
    expected_decision_ledger_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load pinned files and materialize the next blind review worksheet."""
    request = _MaterializationRequest(
        control_evidence_path=control_evidence_path,
        index_manifest_path=index_manifest_path,
        case_index_path=case_index_path,
        case_root=case_root,
        discovery_path=discovery_path,
        workflow_path=workflow_path,
        packet_index_path=packet_index_path,
        decision_ledger_path=decision_ledger_path,
        expected_control_evidence_sha256=expected_control_evidence_sha256,
        expected_index_manifest_sha256=expected_index_manifest_sha256,
        expected_case_index_file_sha256=expected_case_index_file_sha256,
        expected_case_index_sha256=expected_case_index_sha256,
        expected_packet_index_sha256=expected_packet_index_sha256,
        expected_decision_ledger_sha256=expected_decision_ledger_sha256,
    )
    return _materialize_request(request)


def _materialize_loaded(
    control: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    case_index: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    predecessors: Mapping[str, Any],
    discovery: Mapping[str, Any],
    workflow: Mapping[str, Any],
    packet_index_path: Path,
    expected_packet_index_sha256: str,
    decision_ledger: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = build_ai_ban_target_manifest(
        control, index_manifest, case_index, cases, predecessors
    )
    required_ids = required_ai_ban_packet_ids(target, decision_ledger)
    packets = _selected_packets(
        discovery,
        workflow,
        packet_index_path,
        required_ids,
        expected_packet_index_sha256,
    )
    worksheet = build_ai_ban_review_worksheet(
        target,
        packets,
        decision_ledger=decision_ledger,
        expected_target_manifest_sha256=target["target_manifest_sha256"],
    )
    return target, worksheet


def _optional_decision_ledger(
    path: Path | None,
    expected_sha256: str | None,
) -> Mapping[str, Any] | None:
    if path is None:
        if expected_sha256 is not None:
            raise AiBanReviewError("expected decision ledger pin has no input")
        return None
    if expected_sha256 is None:
        raise AiBanReviewError("decision ledger requires an independent pin")
    ledger = _read_json(path, "decision ledger")
    if ledger.get("decision_ledger_sha256") != expected_sha256:
        raise AiBanReviewError("decision ledger differs from independent pin")
    return ledger


def _load_pinned_inputs(
    control_path: Path,
    manifest_path: Path,
    case_index_path: Path,
    control_sha256: str,
    manifest_sha256: str,
    case_index_file_sha256: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    return (
        _read_pinned(control_path, control_sha256, "control evidence"),
        _read_pinned(manifest_path, manifest_sha256, "index manifest"),
        _read_pinned(case_index_path, case_index_file_sha256, "case index"),
    )


def _predecessors(
    case_index: Mapping[str, Any],
    control_sha256: str,
    manifest_sha256: str,
    case_index_file_sha256: str,
    case_index_sha256: str,
    packet_index_sha256: str,
) -> dict[str, str]:
    return {
        "control_evidence_file_sha256": control_sha256,
        "index_manifest_file_sha256": manifest_sha256,
        "case_index_file_sha256": case_index_file_sha256,
        "case_index_sha256": case_index_sha256,
        "packet_index_sha256": packet_index_sha256,
        "specification_sha256": case_index["specification_sha256"],
        "workflow_sha256": case_index["workflow_sha256"],
    }
