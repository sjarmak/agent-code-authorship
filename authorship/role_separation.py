"""Deterministic repository-role separation and exact-content deduplication."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence


class RoleSeparationError(ValueError):
    """Raised when a repository or content group crosses data partitions."""


def _repository(record: Mapping[str, Any]) -> str:
    repository = record.get("repo")
    if not isinstance(repository, str) or not repository:
        raise RoleSeparationError("every record must have a non-empty repository")
    return repository


def _digest(record: Mapping[str, Any]) -> str:
    digest = record.get("content_sha256")
    if not isinstance(digest, str) or not digest:
        raise RoleSeparationError("every record must have a content_sha256")
    return digest


def enforce_role_separation(
    partitions: Mapping[str, Sequence[dict[str, Any]]],
    *,
    content_groups: Mapping[str, str] | None = None,
    target_partition: str = "target",
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Validate group separation and remove cross-group exact-content collisions.

    Repository and content-group overlap across partitions is fatal. Exact
    content shared with the target is retained in the target and removed from
    all labeled partitions. Exact content shared across independent labeled
    groups is removed from every labeled occurrence. Repetition confined to a
    single repository/group is retained.
    """
    names = sorted(partitions)
    groups = content_groups or {}
    repo_partitions: dict[str, set[str]] = defaultdict(set)
    group_partitions: dict[str, set[str]] = defaultdict(set)
    occurrences: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)

    for partition in names:
        for index, record in enumerate(partitions[partition]):
            repository = _repository(record)
            group = groups.get(repository, repository)
            if not isinstance(group, str) or not group:
                raise RoleSeparationError(
                    f"invalid content group for repository {repository}"
                )
            repo_partitions[repository].add(partition)
            group_partitions[group].add(partition)
            occurrences[_digest(record)].append(
                (partition, repository, group, index)
            )

    overlapping_repositories = sorted(
        repository
        for repository, occupied in repo_partitions.items()
        if len(occupied) > 1
    )
    if overlapping_repositories:
        raise RoleSeparationError(
            "repository spans partitions: " + ", ".join(overlapping_repositories)
        )
    overlapping_groups = sorted(
        group for group, occupied in group_partitions.items() if len(occupied) > 1
    )
    if overlapping_groups:
        raise RoleSeparationError(
            "content group spans partitions: " + ", ".join(overlapping_groups)
        )

    drop: dict[str, set[int]] = {name: set() for name in names}
    collisions: list[dict[str, Any]] = []
    for digest in sorted(occurrences):
        items = occurrences[digest]
        occupied = {partition for partition, _, _, _ in items}
        labeled_groups = {
            group
            for partition, _, group, _ in items
            if partition != target_partition
        }
        target_collision = target_partition in occupied and len(occupied) > 1
        labeled_collision = len(labeled_groups) > 1
        if not target_collision and not labeled_collision:
            continue
        removed: dict[str, int] = defaultdict(int)
        for partition, _, _, index in items:
            if partition == target_partition:
                continue
            drop[partition].add(index)
            removed[partition] += 1
        collisions.append(
            {
                "content_sha256": digest,
                "partitions": sorted(occupied),
                "groups": sorted({group for _, _, group, _ in items}),
                "removed_records": dict(sorted(removed.items())),
            }
        )

    filtered = {
        partition: [
            record
            for index, record in enumerate(partitions[partition])
            if index not in drop[partition]
        ]
        for partition in names
    }
    input_counts = {name: len(partitions[name]) for name in names}
    output_counts = {name: len(filtered[name]) for name in names}
    report = {
        "policy": "strict-role-separation-v1",
        "target_partition": target_partition,
        "input_records": input_counts,
        "output_records": output_counts,
        "dropped_records": {
            name: input_counts[name] - output_counts[name] for name in names
        },
        "collision_hashes": len(collisions),
        "collisions": collisions,
    }
    return filtered, report
