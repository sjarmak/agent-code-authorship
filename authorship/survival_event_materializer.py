"""Disk-backed materialization of exact survival event shards."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from authorship.survival_event_adapter import (
    HORIZONS,
    SurvivalEventAdapterError,
    build_line_history,
)
from authorship.survival_git import SurvivalGitError, git, git_with_stdin

CONTRACT_VERSION = 3
MINIMUM_STRUCTURAL_PRECISION = 0.9


class SurvivalEventMaterializationError(ValueError):
    """Raised when pinned lineage cannot be safely materialized."""


@dataclass(frozen=True)
class MaterializationResult:
    manifest: dict[str, Any]
    reused: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def survival_event_shard_manifest_sha256(document: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in document.items()
        if key != "survival_event_shard_manifest_sha256"
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SurvivalEventMaterializationError(
            "cutoff timestamp is invalid"
        ) from error
    if parsed.utcoffset() is None:
        raise SurvivalEventMaterializationError("cutoff timestamp is invalid")
    return parsed


def _lineage_validation_sha256(path: Path) -> str:
    try:
        validation = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SurvivalEventMaterializationError(
            "lineage validation gate is invalid"
        ) from error
    if not isinstance(validation, Mapping):
        raise SurvivalEventMaterializationError("lineage validation gate is invalid")
    minimum = validation.get("minimum_structural_precision")
    precision = validation.get("structural_precision")
    numeric = all(
        not isinstance(value, bool) and isinstance(value, (int, float))
        for value in (minimum, precision)
    )
    passed = (
        validation.get("status") == "complete"
        and validation.get("headline_handling")
        == "structural_candidates_map_to_modified"
        and validation.get("structural_match_gate_passed") is True
        and numeric
        and minimum >= MINIMUM_STRUCTURAL_PRECISION
        and precision >= minimum
    )
    if not passed:
        raise SurvivalEventMaterializationError("lineage validation gate did not pass")
    return _sha256(path)


def _inventory_values(
    repository_id: str,
    git_record: Mapping[str, Any],
    lineage_record: Mapping[str, Any],
    git_inventory_sha256: str,
    lineage_validation_sha256: str,
) -> dict[str, Any]:
    if (
        git_record.get("repository_id") != repository_id
        or lineage_record.get("repository_id") != repository_id
    ):
        raise SurvivalEventMaterializationError("inventory population does not match")
    if git_record.get("status") != "pinned":
        raise SurvivalEventMaterializationError("repository is not pinned")
    sha_fields = {
        "transitions": lineage_record.get("transition_sha256"),
        "structural_events": lineage_record.get("structural_event_sha256"),
        "git_inventory": git_inventory_sha256,
        "lineage_validation": lineage_validation_sha256,
    }
    if not all(_valid_sha256(value) for value in sha_fields.values()):
        raise SurvivalEventMaterializationError("inventory checksum is invalid")
    counts = {
        "lines": lineage_record.get("line_count"),
        "transitions": lineage_record.get("transition_count"),
        "structural_events": lineage_record.get("structural_event_count"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise SurvivalEventMaterializationError("inventory count is invalid")
    return {"sha256s": sha_fields, "counts": counts}


def _verify_inputs(
    transition_path: Path,
    structural_event_path: Path,
    expected_sha256s: Mapping[str, str],
) -> None:
    for name, path in (
        ("transitions", transition_path),
        ("structural_events", structural_event_path),
    ):
        if not path.is_file() or _sha256(path) != expected_sha256s[name]:
            raise SurvivalEventMaterializationError(f"{name} checksum does not match")


def _verify_pinned_git(repository_path: Path, record: Mapping[str, Any]) -> None:
    commit = record.get("cutoff_commit")
    tree = record.get("cutoff_tree")
    if not isinstance(commit, str) or len(commit) != 40:
        raise SurvivalEventMaterializationError("cutoff commit is invalid")
    if not isinstance(tree, str) or len(tree) != 40:
        raise SurvivalEventMaterializationError("cutoff tree is invalid")
    try:
        resolved = git(repository_path, "rev-parse", f"{commit}^{{commit}}")
        actual_tree = git(repository_path, "show", "-s", "--format=%T", commit)
    except SurvivalGitError as error:
        raise SurvivalEventMaterializationError(
            "cutoff commit is unavailable"
        ) from error
    if resolved != commit:
        raise SurvivalEventMaterializationError("cutoff commit does not match")
    if actual_tree != tree:
        raise SurvivalEventMaterializationError("cutoff tree does not match")


def _jsonl(path: Path, label: str) -> Iterator[tuple[int, Mapping[str, Any]]]:
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise SurvivalEventMaterializationError(
                    f"{label} line {number} is invalid JSON"
                ) from error
            if not isinstance(record, Mapping):
                raise SurvivalEventMaterializationError(
                    f"{label} line {number} must be an object"
                )
            yield number, record


def _create_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE transitions (
            line_id TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (line_id, horizon)
        ) WITHOUT ROWID;
        CREATE TABLE modifications (
            line_id TEXT NOT NULL,
            commit_oid TEXT NOT NULL,
            PRIMARY KEY (line_id, commit_oid)
        ) WITHOUT ROWID;
        CREATE TABLE commits (commit_oid TEXT PRIMARY KEY) WITHOUT ROWID;
        """)
    return connection


def _insert_transition(
    connection: sqlite3.Connection,
    repository_id: str,
    record: Mapping[str, Any],
) -> None:
    line_id = record.get("line_id")
    horizon = record.get("horizon_days")
    if (
        record.get("repository_id") != repository_id
        or not isinstance(line_id, str)
        or not line_id
    ):
        raise SurvivalEventMaterializationError("transition population does not match")
    if (
        isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or horizon not in HORIZONS
    ):
        raise SurvivalEventMaterializationError("transition horizon is invalid")
    try:
        connection.execute(
            "INSERT INTO transitions VALUES (?, ?, ?)",
            (line_id, horizon, _canonical_json(record)),
        )
    except sqlite3.IntegrityError as error:
        raise SurvivalEventMaterializationError(
            "transition line/horizon is duplicated"
        ) from error
    terminal = record.get("terminal_commit")
    if terminal is not None:
        _insert_commit(connection, terminal, "terminal")


def _insert_commit(connection: sqlite3.Connection, commit: Any, label: str) -> None:
    if not isinstance(commit, str) or len(commit) != 40:
        raise SurvivalEventMaterializationError(f"{label} commit is invalid")
    connection.execute("INSERT OR IGNORE INTO commits VALUES (?)", (commit,))


def _load_transitions(
    connection: sqlite3.Connection,
    path: Path,
    repository_id: str,
    expected_count: int,
) -> int:
    count = 0
    for _, record in _jsonl(path, "transition"):
        _insert_transition(connection, repository_id, record)
        count += 1
    if count != expected_count:
        raise SurvivalEventMaterializationError("transition count does not match")
    return connection.execute(
        "SELECT COUNT(DISTINCT line_id) FROM transitions"
    ).fetchone()[0]


def _load_structural_events(
    connection: sqlite3.Connection,
    path: Path,
    repository_id: str,
    expected_count: int,
) -> None:
    count = 0
    for _, record in _jsonl(path, "structural event"):
        line_id = record.get("line_id")
        if record.get("repository_id") != repository_id or not isinstance(line_id, str):
            raise SurvivalEventMaterializationError(
                "structural event population does not match"
            )
        known = connection.execute(
            "SELECT 1 FROM transitions WHERE line_id = ? LIMIT 1", (line_id,)
        ).fetchone()
        if not known:
            raise SurvivalEventMaterializationError(
                "structural event population does not match"
            )
        if record.get("decision") == "modified_candidate":
            commit = record.get("commit")
            _insert_commit(connection, commit, "modification")
            connection.execute(
                "INSERT OR IGNORE INTO modifications VALUES (?, ?)",
                (line_id, commit),
            )
        count += 1
    if count != expected_count:
        raise SurvivalEventMaterializationError("structural event count does not match")


def _batches(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _commit_timestamps(
    connection: sqlite3.Connection,
    repository_path: Path,
    batch_size: int,
) -> dict[str, str]:
    commits = [
        row[0]
        for row in connection.execute(
            "SELECT commit_oid FROM commits ORDER BY commit_oid"
        )
    ]
    timestamps = {}
    try:
        for batch in _batches(commits, batch_size):
            output = git_with_stdin(
                repository_path,
                "\n".join(batch) + "\n",
                "log",
                "--no-walk",
                "--format=%H%x00%cI",
                "--stdin",
            )
            for line in output.splitlines():
                commit, separator, timestamp = line.partition("\0")
                if separator:
                    timestamps[commit] = timestamp
    except SurvivalGitError as error:
        raise SurvivalEventMaterializationError(
            "event commit timestamp is unavailable"
        ) from error
    if set(timestamps) != set(commits):
        raise SurvivalEventMaterializationError(
            "event commit timestamps are incomplete"
        )
    return timestamps


def _line_records(
    connection: sqlite3.Connection, line_id: str
) -> tuple[list[Mapping[str, Any]], list[str]]:
    records = [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT payload FROM transitions WHERE line_id = ? ORDER BY horizon",
            (line_id,),
        )
    ]
    commits = [
        row[0]
        for row in connection.execute(
            "SELECT commit_oid FROM modifications WHERE line_id = ? ORDER BY commit_oid",
            (line_id,),
        )
    ]
    return records, commits


def _write_event_shard(
    connection: sqlite3.Connection,
    output_path: Path,
    timestamps: Mapping[str, str],
    cutoff: datetime,
) -> tuple[int, Counter[str], str]:
    digest = hashlib.sha256()
    reasons: Counter[str] = Counter()
    count = 0
    with output_path.open("wb") as destination:
        rows = connection.execute(
            "SELECT DISTINCT line_id FROM transitions ORDER BY line_id"
        )
        for (line_id,) in rows:
            records, commits = _line_records(connection, line_id)
            try:
                history, reason = build_line_history(
                    line_id, records, commits, timestamps, cutoff
                )
            except SurvivalEventAdapterError as error:
                raise SurvivalEventMaterializationError(
                    f"line {line_id} cannot be materialized: {error}"
                ) from error
            payload = f"{_canonical_json(history)}\n".encode()
            destination.write(payload)
            digest.update(payload)
            reasons[reason] += 1
            count += 1
        destination.flush()
        os.fsync(destination.fileno())
    return count, reasons, digest.hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as destination:
            destination.write(f"{_canonical_json(document)}\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _reusable_manifest(
    manifest_path: Path,
    output_path: Path,
    expected: Mapping[str, Any],
) -> dict[str, Any] | None:
    if not manifest_path.is_file() or not output_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, dict):
        return None
    checks = (
        manifest.get("contract_version") == CONTRACT_VERSION,
        manifest.get("repository_id") == expected["repository_id"],
        manifest.get("input_sha256s") == expected["input_sha256s"],
        manifest.get("cutoff") == expected["cutoff"],
        manifest.get("event_shard_file") == output_path.name,
        manifest.get("event_shard_sha256") == _sha256(output_path),
        manifest.get("survival_event_shard_manifest_sha256")
        == survival_event_shard_manifest_sha256(manifest),
    )
    return manifest if all(checks) else None


def _manifest(
    expected: Mapping[str, Any],
    output_path: Path,
    line_count: int,
    reasons: Counter[str],
    shard_sha256: str,
) -> dict[str, Any]:
    document = {
        "contract_version": CONTRACT_VERSION,
        **expected,
        "line_count": line_count,
        "censoring_audit": {
            reason: reasons[reason]
            for reason in ("study_cutoff", "lineage_loss", "terminal_deletion")
        },
        "event_shard_file": output_path.name,
        "event_shard_sha256": shard_sha256,
        "outcomes_consulted": False,
    }
    return {
        **document,
        "survival_event_shard_manifest_sha256": (
            survival_event_shard_manifest_sha256(document)
        ),
    }


def _prepare_materialization(
    repository_id: str,
    repository_path: Path,
    transition_path: Path,
    structural_event_path: Path,
    lineage_validation_path: Path,
    git_record: Mapping[str, Any],
    lineage_record: Mapping[str, Any],
    git_inventory_sha256: str,
) -> tuple[dict[str, Any], datetime, dict[str, Any]]:
    validation_sha256 = _lineage_validation_sha256(lineage_validation_path)
    inventory = _inventory_values(
        repository_id,
        git_record,
        lineage_record,
        git_inventory_sha256,
        validation_sha256,
    )
    _verify_inputs(transition_path, structural_event_path, inventory["sha256s"])
    _verify_pinned_git(repository_path, git_record)
    cutoff = git_record.get("cutoff")
    if not isinstance(cutoff, str):
        raise SurvivalEventMaterializationError("cutoff timestamp is invalid")
    expected = {
        "repository_id": repository_id,
        "input_sha256s": inventory["sha256s"],
        "cutoff": {
            "timestamp": cutoff,
            "commit": git_record["cutoff_commit"],
            "tree": git_record["cutoff_tree"],
        },
    }
    return inventory, _timestamp(cutoff), expected


def _populate_store(
    connection: sqlite3.Connection,
    repository_id: str,
    transition_path: Path,
    structural_event_path: Path,
    inventory: Mapping[str, Any],
) -> None:
    counts = inventory["counts"]
    line_count = _load_transitions(
        connection, transition_path, repository_id, counts["transitions"]
    )
    if line_count != counts["lines"]:
        raise SurvivalEventMaterializationError("line count does not match")
    _load_structural_events(
        connection,
        structural_event_path,
        repository_id,
        counts["structural_events"],
    )


def _materialize_new(
    *,
    repository_id: str,
    repository_path: Path,
    transition_path: Path,
    structural_event_path: Path,
    output_path: Path,
    manifest_path: Path,
    work_root: Path,
    commit_batch_size: int,
    inventory: Mapping[str, Any],
    cutoff: datetime,
    expected: Mapping[str, Any],
) -> MaterializationResult:
    work_root.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=work_root, prefix="survival-events.", suffix=".sqlite"
    )
    os.close(handle)
    database_path = Path(name)
    temporary_output = output_path.with_name(f".{output_path.name}.materializing")
    try:
        with _create_store(database_path) as connection:
            _populate_store(
                connection,
                repository_id,
                transition_path,
                structural_event_path,
                inventory,
            )
            timestamps = _commit_timestamps(
                connection, repository_path, commit_batch_size
            )
            written, reasons, shard_sha256 = _write_event_shard(
                connection, temporary_output, timestamps, cutoff
            )
        if written != inventory["counts"]["lines"]:
            raise SurvivalEventMaterializationError("output line count does not match")
        os.replace(temporary_output, output_path)
        manifest = _manifest(expected, output_path, written, reasons, shard_sha256)
        _atomic_json(manifest_path, manifest)
        return MaterializationResult(manifest, False)
    finally:
        database_path.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)


def materialize_repository(
    *,
    repository_id: str,
    repository_path: Path,
    transition_path: Path,
    structural_event_path: Path,
    lineage_validation_path: Path,
    git_inventory_record: Mapping[str, Any],
    lineage_inventory_record: Mapping[str, Any],
    git_inventory_sha256: str,
    output_path: Path,
    manifest_path: Path,
    work_root: Path,
    commit_batch_size: int = 100_000,
) -> MaterializationResult:
    """Materialize one verified repository with bounded in-memory state."""
    if commit_batch_size < 1:
        raise SurvivalEventMaterializationError("commit_batch_size must be positive")
    inventory, cutoff, expected = _prepare_materialization(
        repository_id,
        repository_path,
        transition_path,
        structural_event_path,
        lineage_validation_path,
        git_inventory_record,
        lineage_inventory_record,
        git_inventory_sha256,
    )
    reusable = _reusable_manifest(manifest_path, output_path, expected)
    if reusable is not None:
        return MaterializationResult(reusable, True)
    return _materialize_new(
        repository_id=repository_id,
        repository_path=repository_path,
        transition_path=transition_path,
        structural_event_path=structural_event_path,
        output_path=output_path,
        manifest_path=manifest_path,
        work_root=work_root,
        commit_batch_size=commit_batch_size,
        inventory=inventory,
        cutoff=cutoff,
        expected=expected,
    )
