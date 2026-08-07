"""Deterministic verification boundary for Deep Search candidate evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from authorship.semantic_topology_protocol import canonical_sha256

_OID = re.compile(r"^[0-9a-f]{40}$")


def _candidate_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_candidate_inventory(
    run_artifacts: Iterable[dict[str, Any]],
    plan_sha256: str,
    protocol_sha256: str,
) -> dict[str, Any]:
    """Project raw Deep Search outputs into candidate-only evidence.

    This function intentionally cannot mark a candidate verified. Verification
    requires a separate deterministic or blinded-adjudication operation.
    """
    candidates: dict[str, dict[str, Any]] = {}
    for artifact in sorted(run_artifacts, key=lambda item: item["run_id"]):
        if artifact.get("terminal_status") != "completed":
            continue
        common = {
            "run_id": artifact["run_id"],
            "run_artifact_sha256": artifact["artifact_sha256"],
            "canonical_repository_id": artifact["canonical_repository_id"],
            "prompt_family_id": artifact["prompt_family_id"],
        }
        for query in artifact.get("proposed_queries", []):
            payload = {
                **common,
                "candidate_kind": "proposed_query",
                "candidate_value": query,
            }
            identifier = _candidate_id(payload)
            candidates[identifier] = {
                "candidate_id": identifier,
                **payload,
                "verification_status": "candidate",
                "evidence_routes": ["deep_search"],
            }
        for citation in artifact.get("cited_files", []):
            payload = {
                **common,
                "candidate_kind": "cited_file",
                "candidate_value": citation,
            }
            identifier = _candidate_id(payload)
            candidates[identifier] = {
                "candidate_id": identifier,
                **payload,
                "verification_status": "candidate",
                "evidence_routes": ["deep_search"],
            }
    inventory: dict[str, Any] = {
        "inventory_version": 1,
        "plan_sha256": plan_sha256,
        "protocol_sha256": protocol_sha256,
        "outcomes_consulted": False,
        "candidate_count": len(candidates),
        "candidates": [candidates[key] for key in sorted(candidates)],
    }
    inventory["inventory_sha256"] = canonical_sha256(inventory)
    return inventory


def build_matched_control_candidates(
    run_artifacts: Iterable[dict[str, Any]],
    sourcegraph_to_canonical: dict[str, str],
) -> dict[str, Any]:
    """Project held-out control citations without assigning authorship labels."""
    candidates: dict[str, dict[str, Any]] = {}
    control_runs = sorted(
        (
            artifact
            for artifact in run_artifacts
            if artifact.get("prompt_family_id") == "semantic_controls"
        ),
        key=lambda item: item["run_id"],
    )
    for artifact in control_runs:
        if artifact.get("terminal_status") != "completed":
            continue
        source_repository = artifact["canonical_repository_id"]
        for citation in artifact.get("cited_files", []):
            cited_name = citation.get("repository") or citation.get("repo")
            control_repository = sourcegraph_to_canonical.get(
                str(cited_name), str(cited_name)
            )
            if control_repository == source_repository:
                continue
            payload = {
                "source_run_id": artifact["run_id"],
                "source_repository_id": source_repository,
                "control_repository_id": control_repository,
                "citation": citation,
            }
            candidate_id = _candidate_id(payload)
            candidates[candidate_id] = {
                "candidate_id": candidate_id,
                **payload,
                "repository_held_out": True,
                "status": "candidate",
                "authorship_label_assigned": False,
                "evidence_routes": ["deep_search"],
            }
    candidate_counts = Counter(item["source_run_id"] for item in candidates.values())
    source_repository_statuses = []
    for artifact in control_runs:
        count = candidate_counts[artifact["run_id"]]
        completed = artifact.get("terminal_status") == "completed"
        source_repository_statuses.append(
            {
                "source_repository_id": artifact["canonical_repository_id"],
                "run_id": artifact["run_id"],
                "status": ("candidate_generated" if count else "unavailable"),
                "candidate_count": count,
                "reason": (
                    None
                    if count
                    else (
                        "no_repository_held_out_candidate_extracted"
                        if completed
                        else "terminal_"
                        + str(artifact.get("terminal_status"))
                        + ":"
                        + str((artifact.get("error") or {}).get("code", "unknown"))
                    )
                ),
            }
        )
    result: dict[str, Any] = {
        "artifact_version": 1,
        "outcomes_consulted": False,
        "repository_held_out": True,
        "candidate_count": len(candidates),
        "candidates": [candidates[key] for key in sorted(candidates)],
        "source_repository_statuses": source_repository_statuses,
    }
    result["artifact_sha256"] = canonical_sha256(result)
    return result


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def verify_citation_with_pinned_git(
    citation: dict[str, Any], repo: Path
) -> dict[str, Any]:
    """Verify that a cited path exists at the exact cited commit."""
    commit = str(citation.get("commit", "")).lower()
    path = citation.get("path")
    base = {
        "citation": citation,
        "verification_method": "git_cat_file_at_exact_commit",
    }
    if not _OID.fullmatch(commit) or not isinstance(path, str) or not path:
        return {
            **base,
            "verification_status": "unavailable",
            "reason": "invalid_commit_or_path_identity",
            "evidence_routes": [],
        }
    commit_check = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    if commit_check.returncode:
        return {
            **base,
            "verification_status": "unavailable",
            "reason": "commit_not_found_in_pinned_git",
            "evidence_routes": [],
        }
    blob = _git(repo, "rev-parse", "--verify", f"{commit}:{path}")
    if blob.returncode or not _OID.fullmatch(blob.stdout.strip().lower()):
        return {
            **base,
            "verification_status": "unavailable",
            "reason": "blob_not_found_at_pinned_commit",
            "evidence_routes": [],
        }
    return {
        **base,
        "verification_status": "verified",
        "evidence_routes": ["pinned_git"],
        "commit": commit,
        "path": path,
        "blob_sha1": blob.stdout.strip().lower(),
    }


def verify_candidate_inventory(
    inventory: dict[str, Any],
    repository_map: dict[str, str],
    repository_root: Path,
) -> dict[str, Any]:
    """Verify cited-file candidates against pinned local Git objects.

    Proposed queries remain candidates until a separate deterministic search
    executor records their complete result set.
    """
    verifications = []
    for candidate in inventory.get("candidates", []):
        base = {
            "candidate_id": candidate["candidate_id"],
            "candidate_kind": candidate["candidate_kind"],
        }
        if candidate["candidate_kind"] != "cited_file":
            verifications.append(
                {
                    **base,
                    "verification_status": "candidate",
                    "reason": "query_requires_separate_deterministic_execution",
                    "evidence_routes": ["deep_search"],
                }
            )
            continue
        citation = candidate["candidate_value"]
        cited_repository = citation.get("repository") or citation.get("repo")
        canonical_repository = repository_map.get(str(cited_repository))
        if canonical_repository is None:
            verifications.append(
                {
                    **base,
                    "verification_status": "unavailable",
                    "reason": "repository_not_in_pinned_pilot_map",
                    "evidence_routes": [],
                }
            )
            continue
        repo = repository_root / canonical_repository.replace("/", "__")
        if not (repo / ".git").exists():
            verifications.append(
                {
                    **base,
                    "verification_status": "unavailable",
                    "reason": "pinned_repository_clone_unavailable",
                    "evidence_routes": [],
                }
            )
            continue
        verifications.append(
            {
                **base,
                **verify_citation_with_pinned_git(citation, repo),
            }
        )
    verified_count = sum(
        item["verification_status"] == "verified" for item in verifications
    )
    unavailable_count = sum(
        item["verification_status"] == "unavailable" for item in verifications
    )
    candidate_only_count = sum(
        item["verification_status"] == "candidate" for item in verifications
    )
    result: dict[str, Any] = {
        "verification_version": 1,
        "candidate_inventory_sha256": inventory["inventory_sha256"],
        "verified_count": verified_count,
        "unavailable_count": unavailable_count,
        "candidate_only_count": candidate_only_count,
        "verifications": verifications,
    }
    result["verification_sha256"] = canonical_sha256(result)
    return result
