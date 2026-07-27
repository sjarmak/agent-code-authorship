"""Freeze corpus integrity, coverage, and preregistered identification gates."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from authorship.corpus.pinned import FEATURE_SCHEMA


class FreezeError(RuntimeError):
    """Raised when gathered artifacts are incomplete or inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize_shards(
    shard_root: Path, entries: list[dict], *, substantial_lines: int
) -> dict:
    by_id = {entry["id"]: entry for entry in entries}
    summaries = {}
    for repo_id, entry in by_id.items():
        stem = repo_id.replace("/", "__")
        shard = shard_root / f"{stem}.jsonl"
        meta_path = shard.with_suffix(".jsonl.meta.json")
        if not shard.exists() or not meta_path.exists():
            raise FreezeError(f"missing shard for {repo_id}")
        meta = json.loads(meta_path.read_text())
        if meta["sha256"] != _sha256(shard):
            raise FreezeError(f"shard checksum mismatch for {repo_id}")
        if meta["feature_schema"] != FEATURE_SCHEMA:
            raise FreezeError(f"stale feature schema for {repo_id}")
        languages: dict[str, dict[str, int]] = defaultdict(
            lambda: {"hunks": 0, "lines": 0}
        )
        with shard.open() as stream:
            for raw in stream:
                row = json.loads(raw)
                languages[row["lang"]]["hunks"] += 1
                languages[row["lang"]]["lines"] += row["line_count"]
        summaries[repo_id] = {
            "role": entry["role"],
            "label": entry["label"],
            "snapshot_commit": entry["snapshot"]["commit"],
            "shard_sha256": meta["sha256"],
            "hunks": sum(value["hunks"] for value in languages.values()),
            "lines": sum(value["lines"] for value in languages.values()),
            "languages": dict(sorted(languages.items())),
            "substantial_languages": sorted(
                language
                for language, value in languages.items()
                if value["lines"] >= substantial_lines
            ),
        }
    return summaries


def freeze(
    reference_manifest: dict,
    target_manifest: dict,
    reference_shards: Path,
    target_shards: Path,
    *,
    protocol_sha256: str,
    feature_manifest_sha256: str,
    substantial_lines: int = 2000,
    minimum_groups: int = 5,
) -> dict:
    reference = summarize_shards(
        reference_shards,
        reference_manifest["repositories"],
        substantial_lines=substantial_lines,
    )
    target = summarize_shards(
        target_shards,
        target_manifest["repositories"],
        substantial_lines=substantial_lines,
    )
    overlap = sorted(set(reference) & set(target))
    if overlap:
        raise FreezeError(f"reference/target role overlap: {overlap}")
    group_counts = {}
    gates = {}
    for language in ("Python", "Go"):
        counts = {}
        for label in ("agent", "human"):
            counts[label] = sum(
                language in summary["substantial_languages"]
                and summary["label"] == label
                for summary in reference.values()
            )
            gates[f"{language.lower()}_{label}_minimum_groups"] = (
                counts[label] >= minimum_groups
            )
        group_counts[language] = counts
    target_totals = {}
    for language in ("Python", "Go"):
        rows = [
            summary["languages"][language]
            for summary in target.values()
            if language in summary["languages"]
        ]
        target_totals[language] = {
            "repositories": len(rows),
            "hunks": sum(row["hunks"] for row in rows),
            "lines": sum(row["lines"] for row in rows),
        }
    identified = all(gates.values())
    return {
        "result_version": 1,
        "status": "identified" if identified else "not_identified",
        "headline_estimate": None,
        "reason": None
        if identified
        else "Insufficient substantial reference repository groups under locked gate.",
        "protocol_sha256": protocol_sha256,
        "feature_manifest_sha256": feature_manifest_sha256,
        "substantial_line_threshold": substantial_lines,
        "minimum_repository_groups_per_side": minimum_groups,
        "reference_substantial_groups": group_counts,
        "identification_gates": gates,
        "target_totals": target_totals,
        "target_repository_count": len(target),
        "reference_repository_count": len(reference),
        "reference_repositories": reference,
        "target_shard_checksums": {
            repo: summary["shard_sha256"] for repo, summary in target.items()
        },
    }


def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    reference_path = root / "study" / "repositories.v1.json"
    target_path = root / "study" / "targets.v1.json"
    feature_path = root / "study" / "features.v1.json"
    protocol_path = root / "study" / "protocol.v1.json"
    result = freeze(
        json.loads(reference_path.read_text()),
        json.loads(target_path.read_text()),
        Path("/mnt/agent-code-authorship/reference-shards"),
        Path("/mnt/agent-code-authorship/target-shards"),
        protocol_sha256=_sha256(protocol_path),
        feature_manifest_sha256=_sha256(feature_path),
    )
    destination = root / "results" / "replication.v1.json"
    _atomic_json(destination, result)
    print(destination)


if __name__ == "__main__":
    main()
