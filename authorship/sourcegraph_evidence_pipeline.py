"""Mechanical conversion of Sourcegraph search results into evidence packets."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlparse

from authorship.sourcegraph_discovery import (
    evidence_packet_sha256,
    validate_discovery_specification,
    validate_evidence_packet,
)
from authorship.sourcegraph_discovery_execution import validate_result_manifest

PACKET_INDEX_VERSION = 3
SHA1_LENGTH = 40
COMMIT_EVIDENCE_KINDS = {
    "agent_trailer_commits": "commit_message",
    "recognized_agent_identity_commits": "commit_identity",
}


class EvidencePipelineError(ValueError):
    """Raised when raw query evidence cannot support a valid packet."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def packet_index_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value for key, value in document.items() if key != "packet_index_sha256"
    }
    return _content_sha256(content)


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidencePipelineError(f"{field} must be an object")
    return value


def _required_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidencePipelineError(f"{field} must be a list")
    return value


def _repository_map(index_manifest: Mapping[str, Any]) -> dict[str, Mapping]:
    records = _required_list(index_manifest.get("repositories"), "repositories")
    repositories: dict[str, Mapping] = {}
    for raw_record in records:
        record = _required_mapping(raw_record, "repository")
        repository_id = record.get("canonical_repository_id")
        if not isinstance(repository_id, str) or not repository_id:
            raise EvidencePipelineError("canonical repository ID is missing")
        if repository_id in repositories:
            raise EvidencePipelineError(
                f"duplicate canonical repository {repository_id}"
            )
        repositories[repository_id] = record
    return repositories


def _family_map(specification: Mapping[str, Any]) -> dict[str, Mapping]:
    families = _required_list(specification.get("query_families"), "query_families")
    return {
        family["id"]: _required_mapping(family, "query family") for family in families
    }


def _enrichment_key(record: Mapping[str, Any]) -> tuple[str, str]:
    manifest_sha = record.get("result_manifest_sha256")
    result_id = record.get("sourcegraph_result_id")
    if not isinstance(manifest_sha, str) or not isinstance(result_id, str):
        raise EvidencePipelineError("file enrichment binding is incomplete")
    return manifest_sha, result_id


def _enrichment_map(
    records: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping]:
    enrichments: dict[tuple[str, str], Mapping] = {}
    for raw_record in records:
        record = _required_mapping(raw_record, "file blame enrichment")
        key = _enrichment_key(record)
        if key in enrichments:
            raise EvidencePipelineError("duplicate file blame enrichment")
        enrichments[key] = record
    return enrichments


def _source_url(repository: Mapping[str, Any], suffix: str = "") -> str:
    value = repository.get("canonical_source_url")
    parsed = urlparse(value) if isinstance(value, str) else None
    if parsed is None or not parsed.scheme or not parsed.netloc:
        raise EvidencePipelineError("canonical source URL must be absolute")
    if parsed.hostname != "github.com":
        return value.rstrip("/")
    return f"{value.rstrip('/')}{suffix}"


def _commit_url(repository: Mapping[str, Any], commit_oid: str) -> str:
    return _source_url(repository, f"/commit/{commit_oid}")


def _file_url(
    repository: Mapping[str, Any], cutoff_commit: str, path: str, line: int
) -> str:
    suffix = f"/blob/{cutoff_commit}/{quote(path, safe='/')}#L{line + 1}"
    return _source_url(repository, suffix)


def _event_time(commit: Mapping[str, Any]) -> str:
    committer = commit.get("committer")
    author = commit.get("author")
    for signature in (committer, author):
        if isinstance(signature, Mapping) and isinstance(signature.get("date"), str):
            return signature["date"]
    raise EvidencePipelineError("commit result has no author or committer date")


def _commit_evidence_value(
    family_id: str, result: Mapping[str, Any]
) -> tuple[str, str]:
    if family_id == "recognized_agent_identity_commits":
        commit = _required_mapping(result.get("commit"), "commit")
        identities = {
            "author": commit.get("author"),
            "committer": commit.get("committer"),
        }
        return "commit_identity", _canonical_json(identities)
    if family_id in COMMIT_EVIDENCE_KINDS:
        preview = result.get("messagePreview")
        commit = _required_mapping(result.get("commit"), "commit")
        value = preview.get("value") if isinstance(preview, Mapping) else None
        value = value or commit.get("subject")
        if not isinstance(value, str) or not value:
            raise EvidencePipelineError("commit match has no message evidence")
        return COMMIT_EVIDENCE_KINDS[family_id], value
    preview = result.get("diffPreview")
    value = preview.get("value") if isinstance(preview, Mapping) else None
    if not isinstance(value, str) or not value:
        raise EvidencePipelineError("diff match has no added-diff evidence")
    return "diff_added", value


def _packet_identifier(
    *,
    repository_id: str,
    packet_type: str,
    commit_oid: str,
    family_id: str,
    result_ids: Sequence[str],
) -> str:
    return _content_sha256(
        {
            "canonical_repository_id": repository_id,
            "packet_type": packet_type,
            "candidate_event.commit_oid": commit_oid,
            "query_family_id": family_id,
            "sourcegraph_result_ids": sorted(result_ids),
        }
    )


def _packet(
    shard: Mapping[str, Any],
    family: Mapping[str, Any],
    repository: Mapping[str, Any],
    *,
    packet_type: str,
    commit_oid: str,
    observed_at: str,
    result_ids: list[str],
    raw_evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    packet_id = _packet_identifier(
        repository_id=shard["canonical_repository_id"],
        packet_type=packet_type,
        commit_oid=commit_oid,
        family_id=family["id"],
        result_ids=result_ids,
    )
    document = {
        "packet_version": 3,
        "packet_id": packet_id,
        "packet_type": packet_type,
        "canonical_repository_id": shard["canonical_repository_id"],
        "canonical_source_url": repository["canonical_source_url"],
        "sourcegraph_name": shard["sourcegraph_name"],
        "cutoff_commit": shard["cutoff_commit"],
        "indexed_revision_oid": shard["cutoff_commit"],
        "query_family_id": family["id"],
        "rendered_query": shard["rendered_query"],
        "rendered_query_sha256": shard["rendered_query_sha256"],
        "sourcegraph_result_ids": sorted(result_ids),
        "candidate_event": {
            "commit_oid": commit_oid,
            "observed_at": observed_at,
        },
        "raw_evidence": raw_evidence,
        "outcomes_consulted": False,
    }
    return {**document, "packet_sha256": evidence_packet_sha256(document)}


def _commit_packets(
    shard: Mapping[str, Any],
    family: Mapping[str, Any],
    repository: Mapping[str, Any],
    result_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = _required_mapping(result_record.get("payload"), "result payload")
    commit = _required_mapping(result.get("commit"), "commit")
    commit_oid = commit.get("oid")
    if not isinstance(commit_oid, str) or len(commit_oid) != SHA1_LENGTH:
        raise EvidencePipelineError("commit result has an invalid OID")
    kind, value = _commit_evidence_value(family["id"], result)
    evidence = {
        "kind": kind,
        "commit_oid": commit_oid,
        "path": None,
        "line": None,
        "value": value,
        "source_url": _commit_url(repository, commit_oid),
    }
    result_id = result_record["sourcegraph_result_id"]
    return [
        _packet(
            shard,
            family,
            repository,
            packet_type=packet_type,
            commit_oid=commit_oid,
            observed_at=_event_time(commit),
            result_ids=[result_id],
            raw_evidence=[evidence],
        )
        for packet_type in family["packet_types"]
    ]


def _validated_blame_lines(
    shard: Mapping[str, Any],
    result: Mapping[str, Any],
    enrichment: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    if enrichment.get("indexed_revision_oid") != shard["cutoff_commit"]:
        raise EvidencePipelineError("file enrichment revision does not match cutoff")
    if enrichment.get("default_branch_reachable") is not True:
        raise EvidencePipelineError(
            "file enrichment commit is not default-branch reachable"
        )
    line_matches = _required_list(result.get("lineMatches"), "lineMatches")
    match_lines = [line_match.get("lineNumber") for line_match in line_matches]
    if any(isinstance(line, bool) or not isinstance(line, int) for line in match_lines):
        raise EvidencePipelineError("Sourcegraph line numbers must be integers")
    raw_lines = _required_list(enrichment.get("lines"), "enrichment lines")
    lines = [_required_mapping(line, "enrichment line") for line in raw_lines]
    enriched_lines = [line.get("line") for line in lines]
    if any(
        isinstance(line, bool) or not isinstance(line, int) for line in enriched_lines
    ):
        raise EvidencePipelineError("enrichment line numbers must be integers")
    if len(enriched_lines) != len(set(enriched_lines)):
        raise EvidencePipelineError("file enrichment line numbers must be unique")
    if sorted(enriched_lines) != sorted(match_lines):
        raise EvidencePipelineError("file enrichment does not cover every matched line")
    return lines


def _file_evidence(
    repository: Mapping[str, Any],
    shard: Mapping[str, Any],
    result: Mapping[str, Any],
    blame_line: Mapping[str, Any],
) -> list[dict[str, Any]]:
    file_record = _required_mapping(result.get("file"), "file")
    path = file_record.get("path")
    line = blame_line.get("line")
    commit_oid = blame_line.get("commit_oid")
    if not isinstance(path, str) or not isinstance(line, int):
        raise EvidencePipelineError("file evidence has an invalid path or line")
    line_matches = {
        match["lineNumber"]: match
        for match in _required_list(result.get("lineMatches"), "lineMatches")
    }
    preview = line_matches[line].get("preview")
    if not isinstance(preview, str) or not preview:
        raise EvidencePipelineError("file match has no preview evidence")
    return [
        {
            "kind": "file_content",
            "commit_oid": commit_oid,
            "path": path,
            "line": line,
            "value": preview,
            "source_url": _file_url(repository, shard["cutoff_commit"], path, line),
        },
        {
            "kind": "blame",
            "commit_oid": commit_oid,
            "path": path,
            "line": line,
            "value": (
                f"cutoff blame attributes Sourcegraph line {line} "
                f"to commit {commit_oid}"
            ),
            "source_url": _commit_url(repository, commit_oid),
        },
    ]


def _file_packets(
    shard: Mapping[str, Any],
    family: Mapping[str, Any],
    repository: Mapping[str, Any],
    result_record: Mapping[str, Any],
    enrichment: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result = _required_mapping(result_record.get("payload"), "result payload")
    lines = _validated_blame_lines(shard, result, enrichment)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for line in lines:
        commit_oid = line.get("commit_oid")
        observed_at = line.get("observed_at")
        if not isinstance(commit_oid, str) or len(commit_oid) != SHA1_LENGTH:
            raise EvidencePipelineError("blame enrichment has an invalid commit OID")
        if not isinstance(observed_at, str):
            raise EvidencePipelineError("blame enrichment has no observed_at timestamp")
        grouped[(commit_oid, observed_at)].append(line)
    packets = []
    result_id = result_record["sourcegraph_result_id"]
    for (commit_oid, observed_at), event_lines in sorted(grouped.items()):
        raw_evidence = [
            evidence
            for line in sorted(event_lines, key=lambda item: item["line"])
            for evidence in _file_evidence(repository, shard, result, line)
        ]
        packets.extend(
            _packet(
                shard,
                family,
                repository,
                packet_type=packet_type,
                commit_oid=commit_oid,
                observed_at=observed_at,
                result_ids=[result_id],
                raw_evidence=raw_evidence,
            )
            for packet_type in family["packet_types"]
        )
    return packets


def _pending_record(
    shard: Mapping[str, Any], result_record: Mapping[str, Any]
) -> dict[str, Any]:
    result = _required_mapping(result_record.get("payload"), "result payload")
    file_record = _required_mapping(result.get("file"), "file")
    return {
        "result_manifest_sha256": shard["result_manifest_sha256"],
        "sourcegraph_result_id": result_record["sourcegraph_result_id"],
        "canonical_repository_id": shard["canonical_repository_id"],
        "sourcegraph_name": shard["sourcegraph_name"],
        "cutoff_commit": shard["cutoff_commit"],
        "path": file_record["path"],
        "sourcegraph_line_numbers": sorted(
            match["lineNumber"] for match in result["lineMatches"]
        ),
        "required_enrichment": "cutoff_pinned_blame",
    }


def _validated_shard(
    shard: Mapping[str, Any],
    specification: Mapping[str, Any],
    repositories: Mapping[str, Mapping],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    errors = validate_result_manifest(shard, specification)
    if errors or shard.get("valid") is not True:
        detail = errors[0] if errors else "query result is marked invalid"
        raise EvidencePipelineError(f"invalid query result: {detail}")
    repository = repositories.get(shard.get("canonical_repository_id"))
    if repository is None:
        raise EvidencePipelineError(
            "canonical repository is absent from index manifest"
        )
    if repository.get("cutoff_commit") != shard.get("cutoff_commit"):
        raise EvidencePipelineError("query result cutoff differs from index manifest")
    return shard, repository


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


def _deduplicate_packets(packets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for packet in packets:
        packet_id = packet["packet_id"]
        prior = by_id.get(packet_id)
        candidate = dict(packet)
        if prior is not None and prior != candidate:
            provenance_fields = {
                "rendered_query",
                "rendered_query_sha256",
                "packet_sha256",
            }
            prior_evidence = {
                key: value
                for key, value in prior.items()
                if key not in provenance_fields
            }
            candidate_evidence = {
                key: value
                for key, value in candidate.items()
                if key not in provenance_fields
            }
            if prior_evidence != candidate_evidence:
                raise EvidencePipelineError(f"packet ID collision for {packet_id}")
            candidate = min(
                (prior, candidate),
                key=lambda item: (
                    item["rendered_query_sha256"],
                    item["packet_sha256"],
                ),
            )
        by_id[packet_id] = candidate
    return sorted(by_id.values(), key=_packet_order)


def _collect_evidence(
    result_manifests: Sequence[Mapping[str, Any]],
    specification: Mapping[str, Any],
    repositories: Mapping[str, Mapping],
    families: Mapping[str, Mapping],
    enrichments: Mapping[tuple[str, str], Mapping],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str], set[tuple[str, str]]]:
    packets = []
    pending = []
    source_manifests = set()
    used_enrichments = set()
    for raw_shard in result_manifests:
        shard, repository = _validated_shard(
            _required_mapping(raw_shard, "result manifest"),
            specification,
            repositories,
        )
        family = families[shard["query_family_id"]]
        source_manifests.add(shard["result_manifest_sha256"])
        for result_record in shard["raw_results"]:
            if shard["result_type"] != "file":
                packets.extend(
                    _commit_packets(shard, family, repository, result_record)
                )
                continue
            key = (
                shard["result_manifest_sha256"],
                result_record["sourcegraph_result_id"],
            )
            enrichment = enrichments.get(key)
            if enrichment is None:
                pending.append(_pending_record(shard, result_record))
            else:
                used_enrichments.add(key)
                packets.extend(
                    _file_packets(shard, family, repository, result_record, enrichment)
                )
    return packets, pending, source_manifests, used_enrichments


def build_packet_index(
    specification: Mapping[str, Any],
    index_manifest: Mapping[str, Any],
    result_manifests: Sequence[Mapping[str, Any]],
    *,
    file_blame_enrichments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build candidate packets without assigning any semantic decision."""
    specification_errors = validate_discovery_specification(specification)
    if specification_errors:
        raise EvidencePipelineError("; ".join(specification_errors))
    repositories = _repository_map(index_manifest)
    families = _family_map(specification)
    enrichments = _enrichment_map(file_blame_enrichments)
    packets, pending, source_manifests, used_enrichments = _collect_evidence(
        result_manifests,
        specification,
        repositories,
        families,
        enrichments,
    )
    if set(enrichments) != used_enrichments:
        raise EvidencePipelineError("unused file blame enrichment")
    ordered_packets = _deduplicate_packets(packets)
    ordered_pending = sorted(
        pending,
        key=lambda item: (
            item["canonical_repository_id"],
            item["path"],
            item["sourcegraph_result_id"],
        ),
    )
    document = {
        "packet_index_version": PACKET_INDEX_VERSION,
        "specification_sha256": specification["specification_sha256"],
        "source_result_manifest_sha256s": sorted(source_manifests),
        "packet_count": len(ordered_packets),
        "pending_file_enrichment_count": len(ordered_pending),
        "sourcegraph_line_number_basis": "zero_based_as_returned_by_graphql",
        "source_url_line_anchor_basis": "one_based",
        "packets": ordered_packets,
        "pending_file_enrichments": ordered_pending,
        "outcomes_consulted": False,
    }
    return {**document, "packet_index_sha256": packet_index_sha256(document)}


def validate_packet_index(
    document: Mapping[str, Any], specification: Mapping[str, Any]
) -> list[str]:
    packets = document.get("packets")
    pending = document.get("pending_file_enrichments")
    if not isinstance(packets, list) or not isinstance(pending, list):
        return ["packets and pending_file_enrichments must be lists"]
    if any(not isinstance(packet, Mapping) for packet in packets):
        return ["packets must contain objects"]
    errors = []
    if document.get("packet_index_version") != PACKET_INDEX_VERSION:
        errors.append("packet_index_version must equal 3")
    if document.get("specification_sha256") != specification.get(
        "specification_sha256"
    ):
        errors.append("specification_sha256 does not match")
    if document.get("outcomes_consulted") is not False:
        errors.append("packet index must be outcome blind")
    if document.get("packet_count") != len(packets):
        errors.append("packet_count does not match packets")
    if document.get("pending_file_enrichment_count") != len(pending):
        errors.append("pending_file_enrichment_count does not match")
    if document.get("packet_index_sha256") != packet_index_sha256(document):
        errors.append("packet_index_sha256 does not match")
    identifiers = [packet.get("packet_id") for packet in packets]
    if len(identifiers) != len(set(identifiers)):
        errors.append("packet IDs must be unique")
    if packets != sorted(packets, key=_packet_order):
        errors.append("packets are not in frozen candidate-event order")
    for packet in packets:
        errors.extend(validate_evidence_packet(packet, specification))
    return errors
