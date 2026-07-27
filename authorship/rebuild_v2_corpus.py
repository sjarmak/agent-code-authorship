"""Rebuild the admissible amended reference corpus from pinned v1 shards."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from authorship.corpus.pinned import (
    FEATURE_SCHEMA,
    load_completed_shard,
    write_completed_shard,
)
from authorship.freeze_results import _atomic_json
from authorship.role_separation import enforce_role_separation


class CorpusRebuildError(RuntimeError):
    """Raised when pinned input shards cannot support a verified rebuild."""


def build_v2_manifest(
    v1_manifest: dict[str, Any], *, protocol_sha256: str
) -> dict[str, Any]:
    repositories = []
    for original in v1_manifest["repositories"]:
        if original["label"] != "agent":
            continue
        entry = json.loads(json.dumps(original))
        entry["effective_date_range"][0] = "2024-01-01T00:00:00Z"
        repositories.append(entry)
    return {
        "$schema": "repositories.schema.json",
        "manifest_version": 2,
        "protocol_sha256": protocol_sha256,
        "cutoff": v1_manifest["cutoff"],
        "selection": {
            "outcome_blind": True,
            "agent_frame": (
                "Pinned v1 Tier-1 maintainer-attested repositories; no new "
                "agent project admitted without exact cutoff/history scope."
            ),
            "human_frame": (
                "Frozen amendment-v2 candidate frame requiring exact-commit "
                "maintainer attestations."
            ),
            "human_frame_status": (
                "closed_without_solicitation_no_admissible_labels"
            ),
            "minimum_groups_per_side": 5,
            "reserved_validation_groups_per_side": 1,
            "substantial_line_threshold": 2000,
            "line_eligibility_status": "rebuilt_and_verified",
            "model_outputs_consulted": False,
        },
        "repositories": repositories,
        "excluded_candidates": [
            {
                "frame": "all amendment-v2 human candidates",
                "reason": "no external solicitation; no label inferred",
            },
            {
                "repository": "gastownhall/gastown",
                "reason": (
                    "agent provenance statement lacks exact cutoff or "
                    "entire-history-through-cutoff scope"
                ),
            },
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _target_hash_records(
    shard_root: Path, target_manifest: dict[str, Any]
) -> list[dict[str, str]]:
    records = []
    for entry in target_manifest["repositories"]:
        shard = shard_root / f"{entry['id'].replace('/', '__')}.jsonl"
        meta_path = shard.with_suffix(".jsonl.meta.json")
        if not shard.exists() or not meta_path.exists():
            raise CorpusRebuildError(f"missing target shard for {entry['id']}")
        meta = json.loads(meta_path.read_text())
        if (
            meta.get("sha256") != _sha256(shard)
            or meta.get("snapshot_commit") != entry["snapshot"]["commit"]
            or meta.get("feature_schema") != FEATURE_SCHEMA
        ):
            raise CorpusRebuildError(f"invalid target shard for {entry['id']}")
        with shard.open() as stream:
            for raw in stream:
                row = json.loads(raw)
                records.append(
                    {
                        "repo": row["repo"],
                        "content_sha256": row["content_sha256"],
                        "role": "target",
                    }
                )
    return records


def rebuild(
    *,
    v1_manifest: dict[str, Any],
    target_manifest: dict[str, Any],
    protocol_sha256: str,
    v1_shards: Path,
    target_shards: Path,
    v2_shards: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = build_v2_manifest(v1_manifest, protocol_sha256=protocol_sha256)
    full_by_repo = {}
    partitions: dict[str, list[dict[str, Any]]] = {
        "modeling": [],
        "validation": [],
        "target": _target_hash_records(target_shards, target_manifest),
    }
    for entry in manifest["repositories"]:
        shard = v1_shards / f"{entry['id'].replace('/', '__')}.jsonl"
        records = load_completed_shard(shard, entry)
        if records is None:
            # Snapshot is unchanged, but effective range intentionally changed;
            # validate the v1 shard against its original entry before filtering.
            original = next(
                item
                for item in v1_manifest["repositories"]
                if item["id"] == entry["id"]
            )
            records = load_completed_shard(shard, original)
        if records is None:
            raise CorpusRebuildError(f"invalid reference shard for {entry['id']}")
        records = [
            record
            for record in records
            if record["introduced_at"] >= "2024-01-01"
        ]
        full_by_repo[entry["id"]] = records
        partition = (
            "validation"
            if entry["role"] == "dedicated_validation"
            else "modeling"
        )
        partitions[partition].extend(
            {
                "repo": record["repo"],
                "content_sha256": record["content_sha256"],
                "role": record["role"],
            }
            for record in records
        )

    _, separation = enforce_role_separation(partitions)
    dropped_hashes = {
        collision["content_sha256"] for collision in separation["collisions"]
    }
    v2_shards.mkdir(parents=True, exist_ok=True)
    repository_summary = {}
    totals = defaultdict(lambda: {"hunks": 0, "lines": 0, "groups": 0})
    for entry in manifest["repositories"]:
        filtered = [
            record
            for record in full_by_repo[entry["id"]]
            if record["content_sha256"] not in dropped_hashes
        ]
        shard = v2_shards / f"{entry['id'].replace('/', '__')}.jsonl"
        write_completed_shard(shard, filtered, entry)
        lines_by_language = CounterLike()
        for record in filtered:
            lines_by_language.add(record["lang"], record["line_count"])
        substantial = sorted(
            language
            for language, lines in lines_by_language.items()
            if lines >= 2000
        )
        repository_summary[entry["id"]] = {
            "role": entry["role"],
            "label": entry["label"],
            "hunks": len(filtered),
            "lines": sum(record["line_count"] for record in filtered),
            "lines_by_language": dict(sorted(lines_by_language.items())),
            "substantial_languages": substantial,
            "shard_sha256": _sha256(shard),
        }
        for language in substantial:
            totals[language]["groups"] += 1
        for record in filtered:
            totals[record["lang"]]["hunks"] += 1
            totals[record["lang"]]["lines"] += record["line_count"]
    report = {
        "rebuild_version": 2,
        "status": "rebuilt_not_identified",
        "cutoff": manifest["cutoff"],
        "introduced_on_or_after": "2024-01-01",
        "source_reference_shards": str(v1_shards),
        "source_target_shards": str(target_shards),
        "output_reference_shards": str(v2_shards),
        "role_separation": separation,
        "repositories": repository_summary,
        "totals": dict(sorted(totals.items())),
    }
    return manifest, report


class CounterLike(dict[str, int]):
    def add(self, key: str, amount: int) -> None:
        self[key] = self.get(key, 0) + amount


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    v1_manifest = json.loads((root / "study" / "repositories.v1.json").read_text())
    target_manifest = json.loads((root / "study" / "targets.v1.json").read_text())
    amendment = root / "study" / "protocol-amendment.v2.json"
    manifest, report = rebuild(
        v1_manifest=v1_manifest,
        target_manifest=target_manifest,
        protocol_sha256=_sha256(amendment),
        v1_shards=Path("/mnt/agent-code-authorship/reference-shards"),
        target_shards=Path("/mnt/agent-code-authorship/target-shards"),
        v2_shards=Path("/mnt/agent-code-authorship/reference-shards-v2"),
    )
    _atomic_json(root / "study" / "repositories.v2.json", manifest)
    _atomic_json(root / "study" / "role-separation.v2.json", report)
    print(root / "study" / "repositories.v2.json")
    print(root / "study" / "role-separation.v2.json")


if __name__ == "__main__":
    main()
