"""Memory-bounded reader for the frozen Sourcegraph packet index."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from authorship.sourcegraph_discovery import validate_evidence_packet
from authorship.sourcegraph_repository_cases import (
    RepositoryCaseError,
    build_repository_case_index_from_validated_packets,
    validate_repository_case_workflow,
)

PACKET_INDEX_FIELDS = frozenset(
    {
        "outcomes_consulted",
        "packet_count",
        "packet_index_sha256",
        "packet_index_version",
        "packets",
        "pending_file_enrichment_count",
        "pending_file_enrichments",
        "source_result_manifest_sha256s",
        "source_url_line_anchor_basis",
        "sourcegraph_line_number_basis",
        "specification_sha256",
    }
)


def _ijson():
    try:
        import ijson
    except ImportError as error:
        raise RepositoryCaseError(
            "ijson is required for memory-bounded packet processing; "
            "install requirements.txt"
        ) from error
    return ijson


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _packet_order(packet: Mapping[str, Any]) -> tuple[Any, ...]:
    event = packet["candidate_event"]
    return (
        event["observed_at"],
        event["commit_oid"],
        packet["canonical_repository_id"],
        packet["packet_type"],
        packet["query_family_id"],
        packet["packet_id"],
    )


def _packet_index_header(path: Path) -> dict[str, Any]:
    parser = _ijson()
    header: dict[str, Any] = {
        "source_result_manifest_sha256s": [],
        "pending_file_enrichments": [],
    }
    top_level_keys = set()
    pending_items = 0
    with path.open("rb") as handle:
        for prefix, event, value in parser.parse(handle, use_float=True):
            if prefix == "" and event == "map_key":
                top_level_keys.add(value)
            elif prefix == "source_result_manifest_sha256s.item":
                header["source_result_manifest_sha256s"].append(value)
            elif prefix.startswith("pending_file_enrichments.item"):
                if event == "start_map":
                    pending_items += 1
            elif "." not in prefix and event in {
                "boolean",
                "null",
                "number",
                "string",
            }:
                header[prefix] = value
    if top_level_keys != PACKET_INDEX_FIELDS:
        raise RepositoryCaseError("packet index top-level contract is invalid")
    if pending_items:
        raise RepositoryCaseError("packet index has pending file enrichments")
    return header


def _validate_header(
    header: Mapping[str, Any],
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
) -> None:
    validate_repository_case_workflow(
        discovery_specification,
        workflow_specification,
    )
    expected = workflow_specification["packet_index"]
    rules = (
        (header.get("packet_index_version") == 3, "packet index version is invalid"),
        (header.get("outcomes_consulted") is False, "packet index is outcome exposed"),
        (
            header.get("specification_sha256")
            == discovery_specification.get("specification_sha256"),
            "packet index specification does not match",
        ),
        (
            header.get("packet_index_sha256") == expected.get("packet_index_sha256"),
            "workflow packet index checksum does not match",
        ),
        (
            header.get("packet_count") == expected.get("packet_count"),
            "workflow packet count does not match",
        ),
        (
            header.get("pending_file_enrichment_count") == 0,
            "packet index has pending file enrichments",
        ),
    )
    for valid, message in rules:
        if not valid:
            raise RepositoryCaseError(message)


def _digest_prefix(header: Mapping[str, Any]) -> tuple[Any, list[str], int]:
    fields = sorted(PACKET_INDEX_FIELDS - {"packet_index_sha256"})
    packet_position = fields.index("packets")
    digest = hashlib.sha256()
    digest.update(b"{")
    for index, field in enumerate(fields[:packet_position]):
        if index:
            digest.update(b",")
        digest.update(
            f"{_canonical_json(field)}:{_canonical_json(header[field])}".encode()
        )
    if packet_position:
        digest.update(b",")
    digest.update(f"{_canonical_json('packets')}:[".encode())
    return digest, fields, packet_position


def _digest_suffix(
    digest: Any,
    header: Mapping[str, Any],
    fields: list[str],
    packet_position: int,
) -> str:
    digest.update(b"]")
    for field in fields[packet_position + 1 :]:
        digest.update(
            f",{_canonical_json(field)}:{_canonical_json(header[field])}".encode()
        )
    digest.update(b"}")
    return digest.hexdigest()


def _validated_packet_stream(
    path: Path,
    header: Mapping[str, Any],
    specification: Mapping[str, Any],
) -> Iterator[Mapping[str, Any]]:
    parser = _ijson()
    digest, fields, packet_position = _digest_prefix(header)
    identifiers = set()
    previous_order = None
    count = 0
    with path.open("rb") as handle:
        for packet in parser.items(handle, "packets.item", use_float=True):
            errors = validate_evidence_packet(packet, specification)
            if errors:
                raise RepositoryCaseError(
                    f"packet {packet.get('packet_id')} is invalid: {'; '.join(errors)}"
                )
            order = _packet_order(packet)
            if previous_order is not None and order < previous_order:
                raise RepositoryCaseError(
                    "packets are not in frozen candidate-event order"
                )
            identifier = packet["packet_id"]
            if identifier in identifiers:
                raise RepositoryCaseError("packet IDs must be unique")
            if count:
                digest.update(b",")
            digest.update(_canonical_json(packet).encode())
            identifiers.add(identifier)
            previous_order = order
            count += 1
            yield packet
    if count != header.get("packet_count"):
        raise RepositoryCaseError("packet count does not match packet stream")
    observed_sha256 = _digest_suffix(digest, header, fields, packet_position)
    if observed_sha256 != header.get("packet_index_sha256"):
        raise RepositoryCaseError("packet index checksum does not match")


def validated_packet_stream_from_file(
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index_path: Path,
) -> tuple[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """Return a validated header and a checksum-verifying packet iterator."""
    header = _packet_index_header(packet_index_path)
    _validate_header(
        header,
        discovery_specification,
        workflow_specification,
    )
    return (
        header,
        _validated_packet_stream(
            packet_index_path,
            header,
            discovery_specification,
        ),
    )


def build_repository_case_index_from_file(
    discovery_specification: Mapping[str, Any],
    workflow_specification: Mapping[str, Any],
    packet_index_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Stream the packet index and build compact repository cases."""
    header, packets = validated_packet_stream_from_file(
        discovery_specification,
        workflow_specification,
        packet_index_path,
    )
    return build_repository_case_index_from_validated_packets(
        packets,
        workflow_specification,
        output_root,
        specification_sha256=header["specification_sha256"],
        packet_index_sha256_value=header["packet_index_sha256"],
    )
