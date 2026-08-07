"""Read-only inventory collection for the Sourcegraph indexing manifest."""

from __future__ import annotations

import json
import subprocess
import tempfile
from argparse import ArgumentParser
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from authorship.corpus.control import CONTROL_REPOS, repo_name
from authorship.sg import api as sourcegraph_api
from authorship.sg import check_auth, fork_map
from authorship.sourcegraph_index_manifest import (
    CONTROL_FRAME,
    CUTOFF,
    REFERENCE_FRAME,
    SURVIVAL_FRAME,
    TARGET_FRAME,
    IndexManifestError,
    build_index_manifest,
    sourcegraph_mirror_name,
    validate_index_manifest,
)
from authorship.survival_candidates import canonical_repository_id
from authorship.target_manifest import resolve_github_snapshot, write_manifest


def _batches(values: Sequence[str], size: int) -> list[Sequence[str]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def _sourcegraph_alias(alias: str, name: str, cutoff_commit: str) -> str:
    return (
        f"{alias}:repository(name:{json.dumps(name)})"
        '{name mirrorInfo{cloned} head:commit(rev:"HEAD"){oid} '
        f"cutoff:commit(rev:{json.dumps(cutoff_commit)})"
        "{oid}}"
    )


def _sourcegraph_state(name: str, repository: Mapping[str, Any] | None) -> dict:
    if repository is None:
        return {
            "name": name,
            "state": "not_indexed",
            "head_oid": None,
            "cutoff_state": "not_accessible",
            "cutoff_oid": None,
        }
    cloned = bool((repository.get("mirrorInfo") or {}).get("cloned"))
    head = repository.get("head") or {}
    cutoff = repository.get("cutoff") or {}
    state = "indexed" if cloned and head.get("oid") else "present_not_cloned"
    return {
        "name": name,
        "state": state,
        "head_oid": head.get("oid"),
        "cutoff_state": "accessible" if cutoff.get("oid") else "not_accessible",
        "cutoff_oid": cutoff.get("oid"),
    }


def collect_sourcegraph_audits(
    repository_ids: Sequence[str],
    mirrors: Mapping[str, str],
    cutoff_commits: Mapping[str, str],
    *,
    direct_names: Mapping[str, str] | None = None,
    query_api: Callable[[str], Mapping[str, Any]] = sourcegraph_api,
    batch_size: int = 40,
) -> dict[str, dict[str, Any]]:
    """Read direct, mirror, HEAD, and frozen-revision states without mutations."""
    normalized = sorted(canonical_repository_id(value) for value in repository_ids)
    names = direct_names or {}
    audits: dict[str, dict[str, Any]] = {}
    for batch in _batches(normalized, batch_size):
        fields = []
        for index, repository_id in enumerate(batch):
            direct = names.get(repository_id, f"github.com/{repository_id}")
            mirror = mirrors.get(repository_id, sourcegraph_mirror_name(repository_id))
            commit = cutoff_commits[repository_id]
            fields.extend(
                [
                    _sourcegraph_alias(f"d{index}", direct, commit),
                    _sourcegraph_alias(f"m{index}", mirror, commit),
                ]
            )
        response = query_api("query{" + " ".join(fields) + "}")
        for index, repository_id in enumerate(batch):
            direct = names.get(repository_id, f"github.com/{repository_id}")
            mirror = mirrors.get(repository_id, sourcegraph_mirror_name(repository_id))
            audits[repository_id] = {
                "direct": _sourcegraph_state(direct, response.get(f"d{index}")),
                "mirror": _sourcegraph_state(mirror, response.get(f"m{index}")),
            }
    return audits


def _github_alias(alias: str, repository_id: str) -> str:
    owner, name = repository_id.split("/", 1)
    return (
        f"{alias}:repository(owner:{json.dumps(owner)},name:{json.dumps(name)})"
        "{isPrivate isArchived defaultBranchRef{name} licenseInfo{spdxId name}}"
    )


def _run_github_query(query: str) -> Mapping[str, Any]:
    completed = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode:
        raise IndexManifestError(
            completed.stderr.strip() or "GitHub GraphQL query failed"
        )
    payload = json.loads(completed.stdout)
    if payload.get("errors"):
        raise IndexManifestError(f"GitHub GraphQL errors: {payload['errors']}")
    return payload["data"]


def _github_audit(repository: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if repository is None:
        return {
            "license": {"status": "unreachable", "spdx_id": None, "name": None},
            "access": {
                "status": "unreachable",
                "archived": None,
                "default_branch": None,
            },
        }
    license_info = repository.get("licenseInfo")
    return {
        "license": {
            "status": "declared" if license_info else "not_declared",
            "spdx_id": (license_info or {}).get("spdxId"),
            "name": (license_info or {}).get("name"),
        },
        "access": {
            "status": "private" if repository.get("isPrivate") else "public",
            "archived": bool(repository.get("isArchived")),
            "default_branch": (repository.get("defaultBranchRef") or {}).get("name"),
        },
    }


def collect_github_audits(
    repository_ids: Sequence[str],
    *,
    query_runner: Callable[[str], Mapping[str, Any]] = _run_github_query,
    batch_size: int = 40,
) -> dict[str, dict[str, Any]]:
    """Audit repository access and declared license metadata in batches."""
    normalized = sorted(canonical_repository_id(value) for value in repository_ids)
    audits: dict[str, dict[str, Any]] = {}
    for batch in _batches(normalized, batch_size):
        query = (
            "query{"
            + " ".join(
                _github_alias(f"r{index}", repository_id)
                for index, repository_id in enumerate(batch)
            )
            + "}"
        )
        response = query_runner(query)
        for index, repository_id in enumerate(batch):
            audits[repository_id] = _github_audit(response.get(f"r{index}"))
    return audits


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _control_source_urls() -> dict[str, str]:
    return {
        canonical_repository_id(repo_name(url)): url.rstrip("/")
        for url in CONTROL_REPOS
    }


def _source_url_pairs(
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
    references: Mapping[str, Any],
    control_urls: Mapping[str, str],
) -> list[tuple[str, str]]:
    return [
        *(
            (canonical_repository_id(repository["id"]), repository["url"])
            for repository in targets["repositories"]
        ),
        *(
            (
                canonical_repository_id(repository["repository_id"]),
                repository["repository_url"],
            )
            for repository in survival["candidates"]
        ),
        *(
            (canonical_repository_id(repository["id"]), repository["url"])
            for repository in references["repositories"]
        ),
        *control_urls.items(),
    ]


def _source_urls(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for repository_id, url in pairs:
        grouped[repository_id].add(url.rstrip("/"))
    conflicts = {
        repository_id: sorted(urls)
        for repository_id, urls in grouped.items()
        if len(urls) > 1
    }
    if conflicts:
        raise IndexManifestError(f"conflicting canonical source URLs: {conflicts}")
    return {repository_id: next(iter(urls)) for repository_id, urls in grouped.items()}


def _sourcegraph_names(source_urls: Mapping[str, str]) -> dict[str, str]:
    return {
        repository_id: (
            f"{urlparse(url).netloc}/{urlparse(url).path.strip('/').removesuffix('.git')}"
        )
        for repository_id, url in source_urls.items()
    }


def _resolve_remote_git_snapshot(
    source_url: str, cutoff: str = CUTOFF
) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="authorship-index-") as temporary:
        repository = Path(temporary) / "repository"
        _run_git(
            [
                "git",
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--no-checkout",
                source_url,
                str(repository),
            ],
            timeout=300,
            error_context=f"cannot clone {source_url}",
        )
        default_branch = _run_git(
            [
                "git",
                "-C",
                str(repository),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            timeout=60,
            error_context=f"cannot resolve default branch for {source_url}",
        ).strip()
        if not default_branch.startswith("origin/") or any(
            character.isspace() for character in default_branch
        ):
            raise IndexManifestError(f"default branch is invalid for {source_url}")
        commit = _run_git(
            [
                "git",
                "-C",
                str(repository),
                "rev-list",
                "-1",
                f"--before={cutoff}",
                default_branch,
            ],
            timeout=60,
            error_context=f"cannot resolve cutoff for {source_url}",
        ).strip()
        if not commit:
            raise IndexManifestError(f"no commit exists before cutoff for {source_url}")
        metadata = _run_git(
            [
                "git",
                "-C",
                str(repository),
                "show",
                "-s",
                "--format=%cI%n%T",
                commit,
            ],
            timeout=60,
            error_context=f"cannot read cutoff metadata for {source_url}",
        )
        committed_at, tree = metadata.splitlines()
        return {"commit": commit, "committed_at": committed_at, "tree": tree}


def _run_git(command: list[str], *, timeout: int, error_context: str) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise IndexManifestError(f"{error_context}: {detail[:300]}")
    return completed.stdout


def _pinned_repository_ids(
    targets: Mapping[str, Any],
    survival_inventory: Mapping[str, Any],
    references: Mapping[str, Any],
) -> set[str]:
    return {
        *(canonical_repository_id(row["id"]) for row in targets["repositories"]),
        *(
            canonical_repository_id(row["repository_id"])
            for row in survival_inventory["repositories"]
        ),
        *(canonical_repository_id(row["id"]) for row in references["repositories"]),
    }


def _collect_missing_snapshots(
    repository_ids: Sequence[str],
    source_urls: Mapping[str, str],
    known_ids: set[str],
) -> dict[str, dict[str, Any]]:
    snapshots = {}
    for repository_id in repository_ids:
        if repository_id in known_ids:
            continue
        source_url = source_urls[repository_id]
        snapshots[repository_id] = (
            resolve_github_snapshot(repository_id, CUTOFF)
            if urlparse(source_url).netloc.lower() == "github.com"
            else _resolve_remote_git_snapshot(source_url, CUTOFF)
        )
    return snapshots


def _cutoff_commits(
    targets: Mapping[str, Any],
    survival_inventory: Mapping[str, Any],
    references: Mapping[str, Any],
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    pairs = [
        *(
            (canonical_repository_id(row["id"]), row["snapshot"]["commit"])
            for row in targets["repositories"]
        ),
        *(
            (canonical_repository_id(row["repository_id"]), row["cutoff_commit"])
            for row in survival_inventory["repositories"]
        ),
        *(
            (canonical_repository_id(row["id"]), row["snapshot"]["commit"])
            for row in references["repositories"]
        ),
        *((repository_id, row["commit"]) for repository_id, row in snapshots.items()),
    ]
    grouped: dict[str, set[str]] = defaultdict(set)
    for repository_id, commit in pairs:
        grouped[repository_id].add(commit)
    conflicts = {
        repository_id: sorted(commits)
        for repository_id, commits in grouped.items()
        if len(commits) > 1
    }
    if conflicts:
        raise IndexManifestError(f"conflicting cutoff commits: {conflicts}")
    return {
        repository_id: next(iter(commits)) for repository_id, commits in grouped.items()
    }


def _repository_records(
    repository_ids: Sequence[str], source_urls: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    github_ids = [
        repository_id
        for repository_id in repository_ids
        if urlparse(source_urls[repository_id]).netloc.lower() == "github.com"
    ]
    github_records = collect_github_audits(github_ids)
    non_github = {
        repository_id: {
            "license": {
                "status": "not_recorded_in_frozen_source",
                "spdx_id": None,
                "name": None,
            },
            "access": {
                "status": "public_at_frozen_snapshot",
                "archived": None,
                "default_branch": None,
            },
        }
        for repository_id in repository_ids
        if repository_id not in github_records
    }
    return {**github_records, **non_github}


def _audit_document(
    *,
    kind: str,
    observed_at: str,
    records: Mapping[str, Mapping[str, Any]],
    sourcegraph_user: str | None = None,
) -> dict[str, Any]:
    document = {
        "audit_version": 3,
        "kind": kind,
        "observed_at": observed_at,
        "outcomes_consulted": False,
        "repository_count": len(records),
        "repositories": [
            {"canonical_repository_id": repository_id, **records[repository_id]}
            for repository_id in sorted(records)
        ],
    }
    return (
        {**document, "sourcegraph_user": sourcegraph_user}
        if sourcegraph_user
        else document
    )


def _load_inputs(
    root: Path, survival_inventory_path: Path
) -> tuple[dict[str, Any], ...]:
    return (
        _load_json(root / TARGET_FRAME),
        _load_json(root / SURVIVAL_FRAME),
        _load_json(root / REFERENCE_FRAME),
        _load_json(root / CONTROL_FRAME),
        _load_json(survival_inventory_path),
    )


def _collect_inventory_evidence(
    targets: Mapping[str, Any],
    survival: Mapping[str, Any],
    references: Mapping[str, Any],
    survival_inventory: Mapping[str, Any],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    control_urls = _control_source_urls()
    source_urls = _source_urls(
        _source_url_pairs(targets, survival, references, control_urls)
    )
    repository_ids = sorted(source_urls)
    known_ids = _pinned_repository_ids(targets, survival_inventory, references)
    snapshots = _collect_missing_snapshots(repository_ids, source_urls, known_ids)
    cutoff_commits = _cutoff_commits(targets, survival_inventory, references, snapshots)
    existing_forks = {
        canonical_repository_id(key): value for key, value in fork_map().items()
    }
    repository_records = _repository_records(repository_ids, source_urls)
    sourcegraph_records = collect_sourcegraph_audits(
        repository_ids,
        existing_forks,
        cutoff_commits,
        direct_names=_sourcegraph_names(source_urls),
    )
    return (
        control_urls,
        existing_forks,
        snapshots,
        repository_records,
        sourcegraph_records,
    )


def generate_index_manifest(
    *,
    root: Path,
    survival_inventory_path: Path,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Collect current read-only audits and build the canonical manifest."""
    targets, survival, references, controls, survival_inventory = _load_inputs(
        root, survival_inventory_path
    )
    (
        control_urls,
        existing_forks,
        snapshots,
        repository_records,
        sourcegraph_records,
    ) = _collect_inventory_evidence(targets, survival, references, survival_inventory)
    manifest = build_index_manifest(
        target_manifest=targets,
        survival_frame=survival,
        survival_inventory=survival_inventory,
        reference_manifest=references,
        control_evidence=controls,
        existing_forks=existing_forks,
        additional_snapshots=snapshots,
        repository_audits=repository_records,
        sourcegraph_audits=sourcegraph_records,
        observed_at=observed_at,
        control_source_urls=control_urls,
    )
    repository_audit = _audit_document(
        kind="repository_metadata_and_license",
        observed_at=observed_at,
        records=repository_records,
    )
    sourcegraph_audit = _audit_document(
        kind="sourcegraph_direct_and_sg_evals_index_state",
        observed_at=observed_at,
        records=sourcegraph_records,
        sourcegraph_user=check_auth(),
    )
    return manifest, repository_audit, sourcegraph_audit


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument(
        "--survival-inventory",
        type=Path,
        required=True,
        help="Frozen git-inventory.v1.json containing cutoff commits and trees.",
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Refresh only the live Sourcegraph audit; preserve frozen manifests.",
    )
    arguments = parser.parse_args()
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest, repository_audit, sourcegraph_audit = generate_index_manifest(
        root=arguments.root,
        survival_inventory_path=arguments.survival_inventory,
        observed_at=observed_at,
    )
    errors = validate_index_manifest(manifest)
    if errors:
        raise IndexManifestError("; ".join(errors))
    audit_path = arguments.root / "study" / "sourcegraph-index-audit.v3.json"
    outputs = (
        {audit_path: sourcegraph_audit}
        if arguments.audit_only
        else {
            arguments.root / "study" / "sourcegraph-index-manifest.v3.json": manifest,
            arguments.root / "study" / "repository-audit.v3.json": repository_audit,
            audit_path: sourcegraph_audit,
        }
    )
    for path, document in outputs.items():
        write_manifest(path, document)
        print(path)


if __name__ == "__main__":
    main()
