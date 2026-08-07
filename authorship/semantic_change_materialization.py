"""Materialize hunk-keyed semantic change records from pinned Git history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from authorship.semantic_topology_protocol import (
    canonical_sha256,
    validate_semantic_change_record,
)

_HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_CODE_OR_STUDY_SUFFIXES = (
    ":(glob)**/*.py",
    ":(glob)**/*.go",
    ":(glob)**/*.md",
    ":(glob)**/*.rst",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _path_class(path: str) -> str:
    lowered = path.lower()
    name = Path(lowered).name
    if lowered.endswith((".md", ".rst")) or name.startswith(("readme", "changelog")):
        return "documentation"
    if (
        "/test" in f"/{lowered}"
        or "/tests/" in f"/{lowered}/"
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go"))
    ):
        return "test"
    return "source"


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "evidence_routes": [],
    }


def _normalized_author_identity(author: dict[str, str]) -> tuple[str, str]:
    name = " ".join(
        unicodedata.normalize("NFKC", author["author_name"]).casefold().split()
    )
    email = "".join(
        unicodedata.normalize("NFKC", author["author_email"]).casefold().split()
    )
    return name, email


def _parse_hunks(diff: str) -> list[dict[str, Any]]:
    path: str | None = None
    current: dict[str, Any] | None = None
    hunks = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        match = _HUNK.match(line)
        if match and path:
            if current and current["added_lines"]:
                hunks.append(current)
            current = {
                "path": path,
                **{
                    key: int(value or (1 if key.endswith("count") else 0))
                    for key, value in match.groupdict().items()
                },
                "added_lines": [],
            }
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            current["added_lines"].append(line[1:])
    if current and current["added_lines"]:
        hunks.append(current)
    return hunks


@lru_cache(maxsize=None)
def _introducer(repo: Path, introducing_commit: str) -> dict[str, str]:
    introducer_result = _git(
        repo,
        "show",
        "-s",
        "--format=%an%x00%ae",
        introducing_commit,
    )
    introducer_parts = introducer_result.stdout.strip().split("\0")
    introducer = {
        "name": introducer_parts[0] if introducer_parts else "",
        "email": introducer_parts[1] if len(introducer_parts) > 1 else "",
    }
    return introducer


@lru_cache(maxsize=None)
def _followups(
    repo: Path,
    introducing_commit: str,
    cutoff_commit: str,
    path: str,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    introducer = _introducer(repo, introducing_commit)
    result = _git(
        repo,
        "log",
        "--format=%H%x00%an%x00%ae%x00%cI",
        f"{introducing_commit}..{cutoff_commit}",
        "--",
        path,
    )
    followups = []
    for line in result.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) == 4:
            followups.append(
                {
                    "commit": parts[0],
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "committed_at": parts[3],
                }
            )
    followups.reverse()
    return followups, introducer


def extract_hunk_records(
    repo: Path,
    repository_id: str,
    sourcegraph_name: str,
    introducing_commit: str,
    diff_base: str,
    cutoff_commit: str,
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract added hunks and deterministic post-introduction context."""
    reachability = _git(
        repo, "merge-base", "--is-ancestor", introducing_commit, cutoff_commit
    )
    cutoff_reachable = reachability.returncode == 0
    diff = _git(
        repo,
        "diff",
        "--unified=0",
        "--no-ext-diff",
        "--no-renames",
        diff_base,
        introducing_commit,
        "--",
        *_CODE_OR_STUDY_SUFFIXES,
    )
    if diff.returncode:
        raise ValueError(f"git diff failed: {diff.stderr[:300]}")
    records = []
    for hunk in _parse_hunks(diff.stdout):
        identity_payload = {
            "repository": repository_id,
            "introducing_commit": introducing_commit,
            "diff_base": diff_base,
            "path": hunk["path"],
            "new_start": hunk["new_start"],
            "new_count": hunk["new_count"],
            "added_lines": hunk["added_lines"],
        }
        hunk_id = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    identity_payload, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
        )
        followups, introducer = _followups(
            repo, introducing_commit, cutoff_commit, hunk["path"]
        )
        first = followups[0] if followups else None
        record = {
            "record_version": 1,
            "identity": {
                "canonical_repository_id": repository_id,
                "sourcegraph_name": sourcegraph_name,
                "introducing_commit": introducing_commit,
                "diff_base": diff_base,
                "path": hunk["path"],
                "hunk_id": hunk_id,
                "cutoff_commit": cutoff_commit,
            },
            "provenance": provenance,
            "task_taxonomy": _unavailable("pending_blinded_adjudication"),
            "blast_radius": _unavailable(
                "pending_sourcegraph_code_navigation_verification"
            ),
            "ownership": _unavailable("pending_cutoff_codeowners_verification"),
            "tests_and_docs": {
                "status": "observed",
                "evidence_routes": ["pinned_git"],
                "path_class": _path_class(hunk["path"]),
                "added_line_count": len(hunk["added_lines"]),
                "new_start": hunk["new_start"],
                "new_count": hunk["new_count"],
            },
            "semantic_hard_negatives": _unavailable(
                "pending_repository_held_out_matching"
            ),
            "diffusion": _unavailable("pending_cross_repository_search_verification"),
            "subsequent_changes": {
                "status": "observed",
                "evidence_routes": ["pinned_git"],
                "commit_count": len(followups),
                "first_followup": first,
                "cutoff_reachable": cutoff_reachable,
            },
            "human_assimilation": {
                "status": "observed",
                "evidence_routes": ["pinned_git"],
                "first_followup_present": first is not None,
                "distinct_followup_author_count": len(
                    {_normalized_author_identity(item) for item in followups}
                ),
                "first_followup_author_differs": bool(
                    first
                    and (
                        first["author_name"] != introducer["name"]
                        or first["author_email"] != introducer["email"]
                    )
                ),
                "human_identity_inferred": False,
                "interpretation": (
                    "different_commit_identity_only_not_human_attribution"
                ),
            },
            "observability": {
                "status": "observed",
                "evidence_routes": ["pinned_git"],
                "cutoff_reachable": cutoff_reachable,
            },
            "uncertainty": _unavailable("pending_field_reliability_estimation"),
            "deep_search_evidence_ids": [],
        }
        errors = validate_semantic_change_record(record)
        if errors:
            raise ValueError("; ".join(errors))
        records.append(record)
    return records


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def materialize_pilot_records(
    plan: dict[str, Any],
    repository_root: Path,
    output_root: Path,
    event_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records = []
    failures = []
    catalog_by_repository = {
        item["repository_id"].lower(): item
        for item in (event_catalog or {}).get("candidates", [])
    }
    for repository in plan["repositories"]:
        if repository["stratum"] != "attributable_agent":
            continue
        repository_id = repository["canonical_repository_id"]
        repo = repository_root / repository_id.replace("/", "__")
        if not (repo / ".git").exists():
            failures.append(
                {
                    "canonical_repository_id": repository_id,
                    "reason": "repository_unavailable",
                }
            )
            continue
        provenance = {
            "class": "attributable_agent",
            "source_artifact": repository["source_artifact"],
            "source_sha256": repository["source_sha256"],
            "outcomes_consulted": False,
        }
        catalog_entry = catalog_by_repository.get(repository_id)
        merge_commits = [
            pull_request.get("merge_commit")
            for pull_request in (catalog_entry or {}).get("pull_requests", [])
            if pull_request.get("merge_commit")
        ]
        introducing_commits = list(
            dict.fromkeys(
                merge_commits or repository["introduction_event"]["commit_shas"]
            )
        )
        for commit in introducing_commits:
            commit_check = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
            if commit_check.returncode:
                failures.append(
                    {
                        "canonical_repository_id": repository_id,
                        "introducing_commit": commit,
                        "reason": "introducing_commit_unavailable",
                    }
                )
                continue
            parent = _git(repo, "rev-parse", f"{commit}^")
            if parent.returncode:
                failures.append(
                    {
                        "canonical_repository_id": repository_id,
                        "introducing_commit": commit,
                        "reason": "diff_base_unavailable",
                    }
                )
                continue
            records.extend(
                extract_hunk_records(
                    repo,
                    repository_id,
                    repository["sourcegraph_name"],
                    commit,
                    parent.stdout.strip(),
                    repository["cutoff_commit"],
                    provenance,
                )
            )
    records.sort(key=lambda item: item["identity"]["hunk_id"])
    output_root.mkdir(parents=True, exist_ok=True)
    records_path = output_root / "semantic-change-records.v1.jsonl"
    records_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    )
    inventory: dict[str, Any] = {
        "inventory_version": 1,
        "plan_sha256": plan["plan_sha256"],
        "protocol_sha256": plan["protocol_sha256"],
        "outcomes_consulted": False,
        "record_file": records_path.name,
        "record_file_sha256": hashlib.sha256(records_path.read_bytes()).hexdigest(),
        "record_count": len(records),
        "failure_count": len(failures),
        "failures": failures,
    }
    inventory["inventory_sha256"] = canonical_sha256(inventory)
    _write_json(output_root / "inventory.v1.json", inventory)
    return inventory


def main() -> None:  # pragma: no cover - exercised by reproduction workflow
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/semantic-topology-pilot-plan.v1.json"),
    )
    parser.add_argument(
        "--events",
        type=Path,
        default=Path("study/survival-candidates.v1.json"),
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path("/mnt/agent-code-authorship/survival-study/repositories"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/semantic-topology-records-v1"),
    )
    args = parser.parse_args()
    inventory = materialize_pilot_records(
        json.loads(args.plan.read_text()),
        args.repository_root,
        args.output,
        event_catalog=json.loads(args.events.read_text()),
    )
    print(json.dumps(inventory, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
