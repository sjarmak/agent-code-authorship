"""Read-only planning for approval-gated sg-evals repository operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from authorship.target_manifest import write_manifest

OPEN_REDISTRIBUTION = {"open_source"}
READY_TRANSPORTS = {"ready_direct", "ready_mirror"}
ROUTINE_REDISTRIBUTION_SPDX = frozenset(
    {
        "AGPL-3.0",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "CC0-1.0",
        "EPL-2.0",
        "GPL-2.0",
        "GPL-3.0",
        "ISC",
        "LGPL-2.1",
        "LGPL-3.0",
        "MIT",
        "MPL-2.0",
        "OFL-1.1",
        "Unlicense",
        "Zlib",
    }
)


class MirroringPlanError(ValueError):
    """Raised when read-only evidence cannot support a safe mirror action."""


def _canonical_sha256(document: Mapping[str, Any], excluded_field: str) -> str:
    content = {key: value for key, value in document.items() if key != excluded_field}
    payload = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def action_plan_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "plan_sha256")


def license_audit_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "audit_sha256")


def github_audit_sha256(document: Mapping[str, Any]) -> str:
    return _canonical_sha256(document, "audit_sha256")


def _license_override_map(
    index_manifest: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    repository_ids = {
        repository["canonical_repository_id"]
        for repository in index_manifest["repositories"]
    }
    records: dict[str, Mapping[str, Any]] = {}
    for override in overrides.get("overrides", []):
        repository_id = override["canonical_repository_id"]
        if repository_id in records:
            raise MirroringPlanError(f"duplicate license override for {repository_id}")
        if repository_id not in repository_ids:
            raise MirroringPlanError(
                f"license override references unknown repository {repository_id}"
            )
        records[repository_id] = override
    return records


def _default_license_record(repository: Mapping[str, Any]) -> dict[str, Any]:
    license_metadata = repository["license"]
    spdx_id = license_metadata.get("spdx_id")
    redistribution_authorized = (
        license_metadata["status"] == "declared"
        and spdx_id in ROUTINE_REDISTRIBUTION_SPDX
    )
    return {
        "canonical_repository_id": repository["canonical_repository_id"],
        "manifest_license_status": license_metadata["status"],
        "spdx_id": spdx_id,
        "redistribution_class": (
            "open_source" if redistribution_authorized else "no_declared_license"
        ),
        "license_expression": (
            license_metadata.get("spdx_id")
            or license_metadata.get("name")
            or "NOASSERTION"
        ),
        "override_applied": False,
        "mirror_transport_url": None,
        "mirror_transport_cutoff_oid": None,
        "evidence": {
            "kind": "frozen_source_license_metadata",
            "url": repository["canonical_source_url"],
            "sha256": None,
            "summary": (
                "Frozen source metadata records a recognized redistribution license."
                if redistribution_authorized
                else "Frozen source metadata does not establish routine redistribution rights."
            ),
        },
    }


def _license_record(
    repository: Mapping[str, Any],
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = _default_license_record(repository)
    if override is None:
        return record
    return {
        **record,
        "redistribution_class": override["redistribution_class"],
        "license_expression": override["license_expression"],
        "spdx_id": override.get("spdx_id"),
        "override_applied": True,
        "mirror_transport_url": override.get("mirror_transport_url"),
        "mirror_transport_cutoff_oid": override.get("mirror_transport_cutoff_oid"),
        "evidence": dict(override["evidence"]),
    }


def build_license_audit(
    index_manifest: Mapping[str, Any],
    overrides: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any]:
    """Expand frozen metadata plus reviewed exceptions into a complete audit."""
    override_map = _license_override_map(index_manifest, overrides)
    records = [
        _license_record(
            repository,
            override_map.get(repository["canonical_repository_id"]),
        )
        for repository in index_manifest["repositories"]
    ]
    records.sort(key=lambda record: record["canonical_repository_id"])
    counts = Counter(record["redistribution_class"] for record in records)
    document = {
        "audit_version": 3,
        "kind": "repository_license_redistribution_evidence",
        "observed_at": observed_at,
        "outcomes_consulted": False,
        "repository_count": len(records),
        "redistribution_counts": dict(sorted(counts.items())),
        "records": records,
    }
    return {**document, "audit_sha256": license_audit_sha256(document)}


def validate_license_audit(document: Mapping[str, Any]) -> list[str]:
    records = document.get("records")
    if not isinstance(records, list) or not records:
        return ["records must be a non-empty list"]
    errors = []
    repository_ids = [record.get("canonical_repository_id") for record in records]
    if len(repository_ids) != len(set(repository_ids)):
        errors.append("canonical repository IDs must be unique")
    if document.get("repository_count") != len(records):
        errors.append("repository_count does not match records")
    observed = dict(
        sorted(Counter(r.get("redistribution_class") for r in records).items())
    )
    if document.get("redistribution_counts") != observed:
        errors.append("redistribution_counts do not match records")
    unsafe_open_source = any(
        record.get("redistribution_class") == "open_source"
        and record.get("override_applied") is not True
        and record.get("spdx_id") not in ROUTINE_REDISTRIBUTION_SPDX
        for record in records
    )
    if unsafe_open_source:
        errors.append(
            "open_source records require recognized SPDX evidence or an override"
        )
    if document.get("audit_sha256") != license_audit_sha256(document):
        errors.append("audit_sha256 does not match")
    return errors


def _mirror_coordinates(repository: Mapping[str, Any]) -> tuple[str, str]:
    name = repository["sourcegraph"]["mirror"]["name"]
    prefix = "github.com/sg-evals/"
    if not name.startswith(prefix):
        raise MirroringPlanError(f"mirror is outside sg-evals: {name}")
    return name, name.removeprefix(prefix)


def _base_action(repository: Mapping[str, Any]) -> dict[str, Any]:
    mirror_name, mirror_slug = _mirror_coordinates(repository)
    return {
        "canonical_repository_id": repository["canonical_repository_id"],
        "canonical_source_url": repository["canonical_source_url"],
        "mirror_name": mirror_name,
        "mirror_slug": mirror_slug,
        "cutoff_commit": repository["cutoff_commit"],
    }


def _held_action(
    repository: Mapping[str, Any], action: str, reason: str
) -> dict[str, Any]:
    return {
        **_base_action(repository),
        "action": action,
        "approval_class": "additional_approval",
        "force_required": False,
        "reason": reason,
    }


def _standalone_replacement(
    action: Mapping[str, Any],
    *,
    source_default_branch: str,
    destination: str,
    destination_default_branch: str | None,
) -> dict[str, Any]:
    if not destination_default_branch:
        raise MirroringPlanError(
            f"missing destination default branch for {destination}"
        )
    return {
        **action,
        "destination_default_branch": destination_default_branch,
        "force_refspec": (
            f"+refs/heads/{source_default_branch}:"
            f"refs/heads/{destination_default_branch}"
        ),
    }


def _sync_action(
    repository: Mapping[str, Any],
    *,
    force_required: bool,
    destination_default_branch: str | None = None,
) -> dict[str, Any]:
    base = _base_action(repository)
    source = repository["canonical_repository_id"]
    destination = f"sg-evals/{base['mirror_slug']}"
    source_default_branch = repository["access"].get("default_branch")
    if not source_default_branch:
        raise MirroringPlanError(f"missing source default branch for {source}")
    action = {
        **base,
        "source_default_branch": source_default_branch,
        "action": (
            "replace_standalone_mirror" if force_required else "fast_forward_sync"
        ),
        "approval_class": (
            "destructive_external" if force_required else "external_mutation"
        ),
        "force_required": force_required,
        "reason": (
            "existing repository is not a fork of the canonical source"
            if force_required
            else "correct fork exists but its indexed default branch is stale"
        ),
    }
    if force_required:
        return _standalone_replacement(
            action,
            source_default_branch=source_default_branch,
            destination=destination,
            destination_default_branch=destination_default_branch,
        )
    return {
        **action,
        "argv": [
            "gh",
            "repo",
            "sync",
            destination,
            "--source",
            source,
            "--branch",
            source_default_branch,
        ],
    }


def _create_action(
    repository: Mapping[str, Any],
    license_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    base = _base_action(repository)
    canonical_source_url = repository["canonical_source_url"]
    if urlparse(canonical_source_url).netloc.lower() != "github.com":
        source_url = (
            license_evidence.get("mirror_transport_url") or canonical_source_url
        )
        action = {
            **base,
            "action": "create_git_mirror",
            "approval_class": "external_mutation",
            "force_required": False,
            "reason": "canonical source is not hosted on GitHub",
            "source_url": source_url,
        }
        if source_url != canonical_source_url:
            cutoff_oid = license_evidence.get("mirror_transport_cutoff_oid")
            if cutoff_oid != repository["cutoff_commit"]:
                raise MirroringPlanError(
                    "audited mirror transport does not match the frozen cutoff"
                )
            return {**action, "source_transport_cutoff_oid": cutoff_oid}
        return action
    return {
        **base,
        "action": "create_github_fork",
        "approval_class": "external_mutation",
        "force_required": False,
        "reason": "intended sg-evals mirror does not exist",
        "argv": [
            "gh",
            "repo",
            "fork",
            repository["canonical_repository_id"],
            "--org",
            "sg-evals",
            "--fork-name",
            base["mirror_slug"],
        ],
    }


def _classify_sync_action(
    repository: Mapping[str, Any], github_state: Mapping[str, Any]
) -> dict[str, Any]:
    if not github_state["exists"]:
        return _held_action(
            repository,
            "hold_missing_expected_mirror",
            "Sourcegraph reported a mirror that GitHub no longer exposes",
        )
    correct_parent = (
        str(github_state.get("parent") or "").lower()
        == repository["canonical_repository_id"].lower()
    )
    correct_fork = bool(github_state.get("is_fork") and correct_parent)
    if not correct_fork:
        return _sync_action(
            repository,
            force_required=True,
            destination_default_branch=github_state.get("default_branch"),
        )
    if github_state.get("cutoff_oid") != repository["cutoff_commit"]:
        return _held_action(
            repository,
            "hold_source_revision_missing",
            "correct fork exists but the frozen cutoff commit is unavailable",
        )
    return _sync_action(repository, force_required=False)


def classify_repository_action(
    repository: Mapping[str, Any],
    github_state: Mapping[str, Any],
    license_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Choose one safe action without performing any external mutation."""
    transport = repository["sourcegraph"]["transport_status"]
    if transport in READY_TRANSPORTS:
        return {
            **_base_action(repository),
            "action": "no_action",
            "approval_class": "none",
            "force_required": False,
            "reason": "frozen cutoff commit is already queryable in Sourcegraph",
        }
    if repository["access"]["status"] == "private":
        return _held_action(
            repository,
            "hold_private",
            "private source requires an explicit visibility decision",
        )
    if license_evidence["redistribution_class"] not in OPEN_REDISTRIBUTION:
        return _held_action(
            repository,
            "hold_redistribution",
            "license evidence does not authorize routine mirroring",
        )
    if repository["sourcegraph"]["required_action"] == "sync_mirror_to_cutoff":
        return _classify_sync_action(repository, github_state)
    if github_state["exists"]:
        return _held_action(
            repository,
            "hold_name_collision",
            "intended mirror name exists but is absent from the Sourcegraph audit",
        )
    return _create_action(repository, license_evidence)


def _by_repository(
    document: Mapping[str, Any], field: str
) -> dict[str, Mapping[str, Any]]:
    return {
        record["canonical_repository_id"]: record for record in document.get(field, [])
    }


def build_action_plan(
    index_manifest: Mapping[str, Any],
    github_audit: Mapping[str, Any],
    license_audit: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any]:
    github = _by_repository(github_audit, "repositories")
    licenses = _by_repository(license_audit, "records")
    repositories = []
    for repository in index_manifest["repositories"]:
        repository_id = repository["canonical_repository_id"]
        if repository_id not in github or repository_id not in licenses:
            raise MirroringPlanError(f"missing audit evidence for {repository_id}")
        repositories.append(
            classify_repository_action(
                repository, github[repository_id], licenses[repository_id]
            )
        )
    repositories.sort(key=lambda record: record["canonical_repository_id"])
    counts = Counter(record["action"] for record in repositories)
    document = {
        "plan_version": 3,
        "status": "dry_run_external_approval_required",
        "observed_at": observed_at,
        "outcomes_consulted": False,
        "repository_count": len(repositories),
        "action_counts": dict(sorted(counts.items())),
        "source_artifacts": {
            "index_manifest_canonical_sha256": _canonical_sha256(index_manifest, ""),
            "github_audit_sha256": github_audit_sha256(github_audit),
            "license_audit_sha256": license_audit_sha256(license_audit),
        },
        "repositories": repositories,
    }
    return {**document, "plan_sha256": action_plan_sha256(document)}


def validate_action_plan(
    document: Mapping[str, Any], *, validate_force_isolation: bool = True
) -> list[str]:
    errors = []
    repositories = document.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        return ["repositories must be a non-empty list"]
    repository_ids = [record.get("canonical_repository_id") for record in repositories]
    if len(repository_ids) != len(set(repository_ids)):
        errors.append("canonical repository IDs must be unique")
    if document.get("repository_count") != len(repositories):
        errors.append("repository_count does not match repositories")
    observed = dict(sorted(Counter(r.get("action") for r in repositories).items()))
    if document.get("action_counts") != observed:
        errors.append("action_counts do not match repositories")
    source_artifacts = document.get("source_artifacts")
    expected_sources = {
        "index_manifest_canonical_sha256",
        "github_audit_sha256",
        "license_audit_sha256",
    }
    if not isinstance(source_artifacts, Mapping) or set(source_artifacts) != (
        expected_sources
    ):
        errors.append("source_artifacts do not identify all canonical inputs")
    if document.get("plan_sha256") != action_plan_sha256(document):
        errors.append("plan_sha256 does not match")
    if validate_force_isolation:
        for record in repositories:
            if not record.get("force_required"):
                continue
            expected_refspec = (
                f"+refs/heads/{record.get('source_default_branch')}:"
                f"refs/heads/{record.get('destination_default_branch')}"
            )
            if (
                record.get("approval_class") != "destructive_external"
                or record.get("force_refspec") != expected_refspec
            ):
                errors.append(
                    f"{record.get('canonical_repository_id')} "
                    "force action is not isolated"
                )
    return errors


def _github_alias(index: int, mirror_slug: str, cutoff_commit: str) -> str:
    return (
        f'r{index}:repository(owner:"sg-evals",name:{json.dumps(mirror_slug)})'
        "{isFork visibility parent{nameWithOwner} "
        "defaultBranchRef{name target{... on Commit{oid}}} "
        f"cutoff:object(expression:{json.dumps(cutoff_commit)})"
        "{... on Commit{oid}}}"
    )


def _run_github_query_allow_missing(query: str) -> Mapping[str, Any]:
    completed = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MirroringPlanError(
            completed.stderr.strip() or "GitHub GraphQL returned invalid JSON"
        ) from error
    if payload.get("data") is None:
        raise MirroringPlanError(f"GitHub GraphQL failed: {payload.get('errors')}")
    return payload["data"]


def _github_state(repository: Mapping[str, Any] | None) -> dict[str, Any]:
    if repository is None:
        return {
            "exists": False,
            "is_fork": None,
            "parent": None,
            "visibility": None,
            "default_branch": None,
            "head_oid": None,
            "cutoff_oid": None,
        }
    branch = repository.get("defaultBranchRef") or {}
    return {
        "exists": True,
        "is_fork": bool(repository.get("isFork")),
        "parent": (repository.get("parent") or {}).get("nameWithOwner"),
        "visibility": repository.get("visibility"),
        "default_branch": branch.get("name"),
        "head_oid": (branch.get("target") or {}).get("oid"),
        "cutoff_oid": (repository.get("cutoff") or {}).get("oid"),
    }


def _batches(
    values: Sequence[Mapping[str, Any]], size: int
) -> list[Sequence[Mapping[str, Any]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def collect_github_mirror_audit(
    index_manifest: Mapping[str, Any],
    *,
    observed_at: str,
    query_runner: Callable[[str], Mapping[str, Any]] = _run_github_query_allow_missing,
    batch_size: int = 20,
) -> dict[str, Any]:
    repositories = sorted(
        index_manifest["repositories"],
        key=lambda record: record["canonical_repository_id"],
    )
    records = []
    for batch in _batches(repositories, batch_size):
        query = (
            "query{"
            + " ".join(
                _github_alias(
                    index,
                    _mirror_coordinates(repository)[1],
                    repository["cutoff_commit"],
                )
                for index, repository in enumerate(batch)
            )
            + "}"
        )
        response = query_runner(query)
        for index, repository in enumerate(batch):
            records.append(
                {
                    "canonical_repository_id": repository["canonical_repository_id"],
                    "mirror_name": repository["sourcegraph"]["mirror"]["name"],
                    **_github_state(response.get(f"r{index}")),
                }
            )
    document = {
        "audit_version": 3,
        "kind": "sg_evals_github_repository_state",
        "observed_at": observed_at,
        "outcomes_consulted": False,
        "repository_count": len(records),
        "repositories": records,
    }
    return {**document, "audit_sha256": github_audit_sha256(document)}


def validate_github_audit(document: Mapping[str, Any]) -> list[str]:
    repositories = document.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        return ["repositories must be a non-empty list"]
    errors = []
    repository_ids = [record.get("canonical_repository_id") for record in repositories]
    if len(repository_ids) != len(set(repository_ids)):
        errors.append("canonical repository IDs must be unique")
    if document.get("repository_count") != len(repositories):
        errors.append("repository_count does not match repositories")
    if document.get("audit_sha256") != github_audit_sha256(document):
        errors.append("audit_sha256 does not match")
    return errors


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
    )
    parser.add_argument(
        "--license-overrides",
        type=Path,
        default=Path("study/repository-license-overrides.v3.json"),
    )
    parser.add_argument(
        "--license-audit-output",
        type=Path,
        default=Path("study/repository-license-audit.v3.json"),
    )
    parser.add_argument(
        "--github-audit-output",
        type=Path,
        default=Path("study/sg-evals-github-audit.v3.json"),
    )
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=Path("study/sg-evals-action-plan.v3.json"),
    )
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    index_manifest = json.loads(arguments.index_manifest.read_text())
    license_overrides = json.loads(arguments.license_overrides.read_text())
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    license_audit = build_license_audit(
        index_manifest,
        license_overrides,
        observed_at=observed_at,
    )
    license_errors = validate_license_audit(license_audit)
    if license_errors:
        raise MirroringPlanError("; ".join(license_errors))
    github_audit = collect_github_mirror_audit(index_manifest, observed_at=observed_at)
    github_errors = validate_github_audit(github_audit)
    if github_errors:
        raise MirroringPlanError("; ".join(github_errors))
    plan = build_action_plan(
        index_manifest, github_audit, license_audit, observed_at=observed_at
    )
    errors = validate_action_plan(plan)
    if errors:
        raise MirroringPlanError("; ".join(errors))
    write_manifest(arguments.license_audit_output, license_audit)
    write_manifest(arguments.github_audit_output, github_audit)
    write_manifest(arguments.plan_output, plan)
    print(arguments.license_audit_output)
    print(arguments.github_audit_output)
    print(arguments.plan_output)


if __name__ == "__main__":
    main()
