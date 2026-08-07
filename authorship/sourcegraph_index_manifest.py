"""Canonical repository inventory for Sourcegraph-backed study execution.

Original GitHub owner/name pairs are scientific identities. Repositories in
``sg-evals`` are transport mirrors and never replace those identities.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from authorship.survival_candidates import canonical_repository_id

CUTOFF = "2026-07-24T23:59:59Z"
TARGET_FRAME = "study/targets.v1.json"
SURVIVAL_FRAME = "study/survival-candidates.v1.json"
REFERENCE_FRAME = "study/repositories.v1.json"
CONTROL_FRAME = "data/control_evidence.json"


class IndexManifestError(ValueError):
    """Raised when canonical repository evidence is internally inconsistent."""


def _snapshot(commit: str | None, tree: str | None) -> dict[str, str | None]:
    return {"cutoff_commit": commit, "cutoff_tree": tree}


def _membership(
    path: str,
    roles: list[str],
    snapshot: Mapping[str, Any] | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    source = snapshot or {}
    return {
        "path": path,
        "roles": sorted(roles),
        "source_url": source_url,
        **_snapshot(source.get("commit"), source.get("tree")),
    }


def _target_memberships(document: Mapping[str, Any]) -> list[tuple[str, dict]]:
    return [
        (
            canonical_repository_id(repository["id"]),
            _membership(
                TARGET_FRAME,
                ["prevalence_target"],
                repository["snapshot"],
                repository.get("url"),
            ),
        )
        for repository in document.get("repositories", [])
    ]


def _survival_memberships(
    frame: Mapping[str, Any], inventory: Mapping[str, Any]
) -> list[tuple[str, dict]]:
    pins = {
        canonical_repository_id(repository["repository_id"]): repository
        for repository in inventory.get("repositories", [])
    }
    memberships = []
    for candidate in frame.get("candidates", []):
        repository_id = canonical_repository_id(candidate["repository_id"])
        pin = pins.get(repository_id, {})
        snapshot = {
            "commit": pin.get("cutoff_commit"),
            "tree": pin.get("cutoff_tree"),
        }
        memberships.append(
            (
                repository_id,
                _membership(
                    SURVIVAL_FRAME,
                    ["adoption_agent_seed", "survival_target"],
                    snapshot,
                    candidate.get("repository_url"),
                ),
            )
        )
    return memberships


def _reference_roles(repository: Mapping[str, Any]) -> list[str]:
    label = repository.get("label")
    roles = [f"classifier_{label}_reference"]
    if label == "agent":
        roles.append("adoption_agent_seed")
    if repository.get("role") == "dedicated_validation":
        roles.append("dedicated_validation")
    return roles


def _reference_memberships(document: Mapping[str, Any]) -> list[tuple[str, dict]]:
    return [
        (
            canonical_repository_id(repository["id"]),
            _membership(
                REFERENCE_FRAME,
                _reference_roles(repository),
                repository["snapshot"],
                repository.get("url"),
            ),
        )
        for repository in document.get("repositories", [])
    ]


def _control_memberships(
    document: Mapping[str, Any], source_urls: Mapping[str, str]
) -> list[tuple[str, dict]]:
    return [
        (
            canonical_repository_id(repository_id),
            _membership(
                CONTROL_FRAME,
                ["adoption_ai_ban_control_seed"],
                source_url=source_urls.get(canonical_repository_id(repository_id)),
            ),
        )
        for repository_id in document.get("repos", {})
    ]


def _group_memberships(
    memberships: list[tuple[str, dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repository_id, membership in memberships:
        grouped[repository_id].append(membership)
    return {
        repository_id: sorted(records, key=lambda record: record["path"])
        for repository_id, records in grouped.items()
    }


def _resolve_snapshot(
    repository_id: str,
    memberships: list[dict[str, Any]],
    additional_snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, str | None]:
    extra = additional_snapshots.get(repository_id, {})
    commits = {
        value
        for value in [
            *(membership["cutoff_commit"] for membership in memberships),
            extra.get("commit"),
        ]
        if value
    }
    trees = {
        value
        for value in [
            *(membership["cutoff_tree"] for membership in memberships),
            extra.get("tree"),
        ]
        if value
    }
    if len(commits) > 1:
        raise IndexManifestError(
            f"{repository_id} has conflicting cutoff commits: {sorted(commits)}"
        )
    if len(trees) > 1:
        raise IndexManifestError(
            f"{repository_id} has conflicting cutoff trees: {sorted(trees)}"
        )
    return _snapshot(next(iter(commits), None), next(iter(trees), None))


def _resolve_source_url(
    repository_id: str, memberships: Sequence[Mapping[str, Any]]
) -> str:
    urls = {membership.get("source_url") for membership in memberships}
    urls.discard(None)
    if len(urls) > 1:
        raise IndexManifestError(
            f"{repository_id} has conflicting canonical source URLs: {sorted(urls)}"
        )
    return next(iter(urls), f"https://github.com/{repository_id}")


def sourcegraph_mirror_name(repository_id: str) -> str:
    owner, name = repository_id.split("/", 1)
    return f"github.com/sg-evals/{owner}-{name}"


def _sourcegraph_record(
    repository_id: str,
    mirror_name: str,
    audits: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    audit = audits.get(repository_id)
    if not audit:
        return {
            "audit_status": "missing",
            "direct": {
                "name": f"github.com/{repository_id}",
                "state": "not_audited",
                "head_oid": None,
                "cutoff_state": "not_audited",
                "cutoff_oid": None,
            },
            "mirror": {
                "name": mirror_name,
                "state": "not_audited",
                "head_oid": None,
                "cutoff_state": "not_audited",
                "cutoff_oid": None,
            },
            "transport_status": "not_audited",
            "selected_name": None,
            "required_action": "audit",
        }
    direct = dict(audit["direct"])
    audited_mirror = audit["mirror"]
    if audited_mirror.get("name") != mirror_name:
        raise IndexManifestError(
            f"{repository_id} audited mirror {audited_mirror.get('name')} "
            f"does not match intended mirror {mirror_name}"
        )
    mirror = dict(audited_mirror)
    plan = _transport_plan(direct, mirror, mirror_name)
    return {
        "audit_status": "complete",
        "direct": direct,
        "mirror": mirror,
        **plan,
    }


def _transport_plan(
    direct: Mapping[str, Any], mirror: Mapping[str, Any], mirror_name: str
) -> dict[str, str]:
    if direct.get("cutoff_state") == "accessible":
        return {
            "transport_status": "ready_direct",
            "selected_name": direct["name"],
            "required_action": "none",
        }
    if mirror.get("cutoff_state") == "accessible":
        return {
            "transport_status": "ready_mirror",
            "selected_name": mirror_name,
            "required_action": "none",
        }
    if mirror.get("state") == "indexed":
        return {
            "transport_status": "mirror_revision_sync_required",
            "selected_name": mirror_name,
            "required_action": "sync_mirror_to_cutoff",
        }
    return {
        "transport_status": "mirror_creation_required",
        "selected_name": mirror_name,
        "required_action": "create_and_index_mirror",
    }


def _repository_record(
    repository_id: str,
    memberships: list[dict[str, Any]],
    existing_forks: Mapping[str, str],
    additional_snapshots: Mapping[str, Mapping[str, Any]],
    repository_audits: Mapping[str, Mapping[str, Any]],
    sourcegraph_audits: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    snapshot = _resolve_snapshot(repository_id, memberships, additional_snapshots)
    source_url = _resolve_source_url(repository_id, memberships)
    mirror_name = existing_forks.get(
        repository_id, sourcegraph_mirror_name(repository_id)
    )
    audit = repository_audits.get(repository_id)
    roles = sorted({role for membership in memberships for role in membership["roles"]})
    return {
        "canonical_repository_id": repository_id,
        "canonical_source_url": source_url,
        "roles": roles,
        "source_frames": memberships,
        **snapshot,
        "license": (
            dict(audit["license"])
            if audit
            else {"status": "not_audited", "spdx_id": None, "url": None}
        ),
        "access": (
            dict(audit["access"])
            if audit
            else {
                "status": "not_audited",
                "archived": None,
                "default_branch": None,
            }
        ),
        "sourcegraph": _sourcegraph_record(
            repository_id, mirror_name, sourcegraph_audits
        ),
    }


def _coverage(
    paths_to_ids: Mapping[str, set[str]], included_ids: set[str]
) -> dict[str, dict[str, Any]]:
    return {
        path: {
            "expected": len(repository_ids),
            "included": len(repository_ids & included_ids),
            "missing_repository_ids": sorted(repository_ids - included_ids),
        }
        for path, repository_ids in sorted(paths_to_ids.items())
    }


def _duplicate_checks(repositories: list[dict[str, Any]]) -> dict[str, Any]:
    trees: dict[str, list[str]] = defaultdict(list)
    for repository in repositories:
        if repository["cutoff_tree"]:
            trees[repository["cutoff_tree"]].append(
                repository["canonical_repository_id"]
            )
    matches = [
        {"git_tree_sha1": tree, "repository_ids": sorted(repository_ids)}
        for tree, repository_ids in sorted(trees.items())
        if len(repository_ids) > 1
    ]
    return {
        "canonical_identity_duplicates": 0,
        "method": "canonical-id merge plus exact Git cutoff-tree comparison",
        "content_overlap_scope": "whole_repository_tree_exact",
        "whole_tree_exact_matches": matches,
    }


def _sourcegraph_coverage(repositories: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for repository in repositories:
        counts[repository["sourcegraph"]["transport_status"]] += 1
    return dict(sorted(counts.items()))


def _all_memberships(
    target_manifest: Mapping[str, Any],
    survival_frame: Mapping[str, Any],
    survival_inventory: Mapping[str, Any],
    reference_manifest: Mapping[str, Any],
    control_evidence: Mapping[str, Any],
    control_source_urls: Mapping[str, str],
) -> list[tuple[str, dict[str, Any]]]:
    return [
        *_target_memberships(target_manifest),
        *_survival_memberships(survival_frame, survival_inventory),
        *_reference_memberships(reference_manifest),
        *_control_memberships(control_evidence, control_source_urls),
    ]


def _normalized(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        canonical_repository_id(repository_id): value
        for repository_id, value in mapping.items()
    }


def _build_repository_records(
    grouped: Mapping[str, list[dict[str, Any]]],
    existing_forks: Mapping[str, str],
    additional_snapshots: Mapping[str, Mapping[str, Any]],
    repository_audits: Mapping[str, Mapping[str, Any]],
    sourcegraph_audits: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _repository_record(
            repository_id,
            grouped[repository_id],
            existing_forks,
            additional_snapshots,
            repository_audits,
            sourcegraph_audits,
        )
        for repository_id in sorted(grouped)
    ]


def build_index_manifest(
    *,
    target_manifest: Mapping[str, Any],
    survival_frame: Mapping[str, Any],
    survival_inventory: Mapping[str, Any],
    reference_manifest: Mapping[str, Any],
    control_evidence: Mapping[str, Any],
    existing_forks: Mapping[str, str],
    additional_snapshots: Mapping[str, Mapping[str, Any]],
    repository_audits: Mapping[str, Mapping[str, Any]],
    sourcegraph_audits: Mapping[str, Mapping[str, Any]],
    observed_at: str,
    control_source_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Merge frozen frames without allowing transport names to become identity."""
    memberships = _all_memberships(
        target_manifest,
        survival_frame,
        survival_inventory,
        reference_manifest,
        control_evidence,
        control_source_urls or {},
    )
    grouped = _group_memberships(memberships)
    repositories = _build_repository_records(
        grouped,
        _normalized(existing_forks),
        _normalized(additional_snapshots),
        _normalized(repository_audits),
        _normalized(sourcegraph_audits),
    )
    paths_to_ids: dict[str, set[str]] = defaultdict(set)
    for repository_id, membership in memberships:
        paths_to_ids[membership["path"]].add(repository_id)
    included_ids = {record["canonical_repository_id"] for record in repositories}
    return {
        "manifest_version": 3,
        "cutoff": target_manifest.get("cutoff") or survival_inventory.get("cutoff"),
        "observed_at": observed_at,
        "outcomes_consulted": False,
        "canonical_identity": "original_source_owner_and_name",
        "mirror_identity_role": "transport_only",
        "repository_count": len(repositories),
        "coverage": _coverage(paths_to_ids, included_ids),
        "sourcegraph_coverage": _sourcegraph_coverage(repositories),
        "duplicate_checks": _duplicate_checks(repositories),
        "repositories": repositories,
    }


def _validate_sourcegraph(repository: Mapping[str, Any]) -> list[str]:
    repository_id = repository.get("canonical_repository_id", "<missing>")
    sourcegraph = repository.get("sourcegraph", {})
    if sourcegraph.get("audit_status") != "complete":
        return [f"{repository_id} Sourcegraph audit is missing"]
    errors = []
    cutoff_commit = repository.get("cutoff_commit")
    for endpoint_name in ("direct", "mirror"):
        endpoint = sourcegraph.get(endpoint_name, {})
        cutoff_state = endpoint.get("cutoff_state")
        cutoff_oid = endpoint.get("cutoff_oid")
        if cutoff_state == "accessible" and cutoff_oid != cutoff_commit:
            errors.append(
                f"{repository_id} {endpoint_name} accessible cutoff_oid "
                "must equal cutoff_commit"
            )
        if cutoff_state == "not_accessible" and cutoff_oid is not None:
            errors.append(
                f"{repository_id} {endpoint_name} inaccessible revision has cutoff_oid"
            )
    mirror = sourcegraph.get("mirror", {})
    if not str(mirror.get("name", "")).startswith("github.com/sg-evals/"):
        errors.append(f"{repository_id} mirror must use the sg-evals namespace")
    expected_plan = _transport_plan(
        sourcegraph.get("direct", {}), mirror, mirror.get("name", "")
    )
    for field, value in expected_plan.items():
        if sourcegraph.get(field) != value:
            errors.append(f"{repository_id} Sourcegraph {field} is inconsistent")
    return errors


def _validate_repository(repository: Mapping[str, Any]) -> list[str]:
    repository_id = repository.get("canonical_repository_id", "<missing>")
    errors = []
    if not repository.get("cutoff_commit"):
        errors.append(f"{repository_id} cutoff_commit is required")
    if not repository.get("cutoff_tree"):
        errors.append(f"{repository_id} cutoff_tree is required")
    if repository.get("license", {}).get("status") == "not_audited":
        errors.append(f"{repository_id} license audit is missing")
    if repository.get("access", {}).get("status") == "not_audited":
        errors.append(f"{repository_id} access audit is missing")
    if not repository.get("roles") or not repository.get("source_frames"):
        errors.append(f"{repository_id} roles and source_frames are required")
    return [*errors, *_validate_sourcegraph(repository)]


def validate_index_manifest(document: Mapping[str, Any]) -> list[str]:
    """Return every blocking manifest error instead of masking partial audits."""
    errors: list[str] = []
    repositories = document.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        return ["repositories must be a non-empty list"]
    repository_ids = [
        repository.get("canonical_repository_id") for repository in repositories
    ]
    if len(repository_ids) != len(set(repository_ids)):
        errors.append("canonical repository identities must be unique")
    if document.get("repository_count") != len(repositories):
        errors.append("repository_count does not match repositories")
    for repository in repositories:
        errors.extend(_validate_repository(repository))
    for path, coverage in document.get("coverage", {}).items():
        if coverage.get("included") != coverage.get("expected"):
            errors.append(f"{path} frame coverage is incomplete")
        if coverage.get("missing_repository_ids"):
            errors.append(f"{path} has missing repository identities")
    if document.get("coverage", {}).get(SURVIVAL_FRAME, {}).get("expected") != 126:
        errors.append("survival frame must contain exactly 126 repositories")
    if document.get("sourcegraph_coverage") != _sourcegraph_coverage(repositories):
        errors.append("sourcegraph_coverage does not match repositories")
    if document.get("duplicate_checks") != _duplicate_checks(repositories):
        errors.append("duplicate_checks do not match repository cutoff trees")
    return errors
