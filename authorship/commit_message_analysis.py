"""Materialize the exploratory commit-message authorship proxy from pinned Git."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.commit_message_heuristic import (
    baseline_from_messages,
    classify_message,
    normalize_message,
)

CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
        ".zig",
    }
)
_EXCLUDED_PATH = re.compile(
    r"(?i)(?:^|/)(?:vendor|vendored|third_party|node_modules|fixtures?|"
    r"snapshots?|generated|dist|build)(?:/|$)|"
    r"(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|"
    r"cargo\.lock)$|(?:\.min\.(?:js|css))$"
)
_EXPLICIT_AGENT_PROVENANCE = re.compile(
    r"(?im)^(?:co-authored-by|generated-by|assisted-by|ai-generated-by|"
    r"ai-assisted-by):[^\n]*(?:claude|chatgpt|codex|copilot|cursor|devin|"
    r"gemini|openai|anthropic)"
)


class CommitMessageAnalysisError(ValueError):
    """A pinned Git input cannot support the declared analysis."""


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CommitMessageAnalysisError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError) as error:
        raise CommitMessageAnalysisError(f"invalid ISO-8601 time: {value}") from error


def _commit_metadata(
    path: Path, cutoff_commit: str, start_at: str
) -> tuple[dict[str, Any], ...]:
    raw = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        f"--since={start_at}",
        "--format=%H%x00%P%x00%cI%x00%B%x00",
        cutoff_commit,
    )
    fields = raw.split("\x00")
    if fields and not fields[-1].strip():
        fields.pop()
    if len(fields) % 4:
        raise CommitMessageAnalysisError("git log returned malformed commit metadata")
    records = tuple(
        {
                "commit": commit.strip(),
                "parents": tuple(parents.strip().split()),
                "committed_at": committed_at.strip(),
                "message": message.strip(),
        }
        for commit, parents, committed_at, message in (
            fields[index : index + 4] for index in range(0, len(fields), 4)
        )
    )
    return tuple(
        sorted(
            records,
            key=lambda record: (
                _parse_time(str(record["committed_at"])),
                str(record["commit"]),
            ),
        )
    )


def _eligible_code_path(path: str) -> bool:
    return not _EXCLUDED_PATH.search(path) and Path(path).suffix.lower() in CODE_SUFFIXES


def _rename_destination(path: str) -> str:
    normalized = re.sub(
        r"\{[^{}]* => ([^{}]*)\}",
        lambda match: match.group(1),
        path,
    )
    return normalized.rsplit(" => ", 1)[-1]


def _added_code_lines_by_commit(
    path: Path, cutoff_commit: str, start_at: str
) -> dict[str, int]:
    raw = _git(
        path,
        "log",
        "--no-merges",
        "--reverse",
        f"--since={start_at}",
        "--format=%x1e%H",
        "--numstat",
        "--find-renames=50%",
        cutoff_commit,
    )
    additions: dict[str, int] = {}
    current_commit: str | None = None
    for line in raw.split("\n"):
        if line.startswith("\x1e"):
            current_commit = line[1:].strip()
            additions[current_commit] = 0
            continue
        if not line.strip():
            continue
        if current_commit is None:
            raise CommitMessageAnalysisError("numstat row appeared before commit header")
        parts = line.split("\t", 2)
        if len(parts) != 3:
            raise CommitMessageAnalysisError(
                f"malformed numstat row for {current_commit}: {line}"
            )
        added, _deleted, changed_path = parts
        if added == "-" or not _eligible_code_path(
            _rename_destination(changed_path)
        ):
            continue
        additions[current_commit] = additions[current_commit] + int(added)
    return additions


def _first_explicit_agent_commit(
    records: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    return next(
        (
            record
            for record in records
            if _EXPLICIT_AGENT_PROVENANCE.search(str(record["message"]))
        ),
        None,
    )


def _period_summary(
    commits: Sequence[Mapping[str, Any]], threshold: int
) -> dict[str, Any]:
    detected = tuple(
        commit
        for commit in commits
        if commit["classifications"][f"threshold_{threshold}"]
    )
    eligible_lines = sum(int(commit["added_code_lines"]) for commit in commits)
    attributed_lines = sum(int(commit["added_code_lines"]) for commit in detected)
    return {
        "eligible_commit_count": len(commits),
        "detected_commit_count": len(detected),
        "detected_commit_share": len(detected) / len(commits) if commits else None,
        "eligible_added_line_count": eligible_lines,
        "agent_attributed_added_line_count": attributed_lines,
        "agent_attributed_added_line_share": (
            attributed_lines / eligible_lines if eligible_lines else None
        ),
    }


def _within_repository_contrast(
    commits: Sequence[Mapping[str, Any]],
    adoption_at: str | None,
    thresholds: Sequence[int],
) -> dict[str, Any]:
    if adoption_at is None:
        return {"status": "unavailable", "reason": "no datable adoption point"}
    adoption_time = _parse_time(adoption_at)
    pre = tuple(
        commit
        for commit in commits
        if _parse_time(str(commit["committed_at"])) < adoption_time
    )
    post = tuple(
        commit
        for commit in commits
        if _parse_time(str(commit["committed_at"])) >= adoption_time
    )
    results = {}
    for threshold in thresholds:
        pre_summary = _period_summary(pre, threshold)
        post_summary = _period_summary(post, threshold)
        pre_commit_share = pre_summary["detected_commit_share"]
        post_commit_share = post_summary["detected_commit_share"]
        pre_line_share = pre_summary["agent_attributed_added_line_share"]
        post_line_share = post_summary["agent_attributed_added_line_share"]
        results[str(threshold)] = {
            "pre": pre_summary,
            "post": post_summary,
            "commit_share_change": (
                post_commit_share - pre_commit_share
                if pre_commit_share is not None and post_commit_share is not None
                else None
            ),
            "added_line_share_change": (
                post_line_share - pre_line_share
                if pre_line_share is not None and post_line_share is not None
                else None
            ),
        }
    return {
        "status": "available" if pre and post else "insufficient_period_coverage",
        "adoption_at": adoption_at,
        "thresholds": results,
    }


def _adoption_details(
    records: Sequence[Mapping[str, Any]],
    adoption_at: str | None,
    infer_adoption_from_provenance: bool,
) -> tuple[str | None, str]:
    explicit = _first_explicit_agent_commit(records)
    if adoption_at:
        return adoption_at, "provided"
    if infer_adoption_from_provenance and explicit is not None:
        return str(explicit["committed_at"]), "first_explicit_agent_provenance"
    return None, "unavailable"


def _classified_commit(
    record: Mapping[str, Any],
    added_code_lines: int,
    baseline: Mapping[str, Any],
    thresholds: Sequence[int],
) -> dict[str, Any]:
    message = str(record["message"])
    classification = classify_message(
        message,
        baseline=baseline,
        thresholds=thresholds,
    )
    return {
        "commit": record["commit"],
        "committed_at": record["committed_at"],
        "added_code_lines": added_code_lines,
        "normalized_message_sha256": classification["features"][
            "normalized_message_sha256"
        ],
        "features": classification["features"],
        "signals": classification["signals"],
        "score": classification["score"],
        "classifications": classification["classifications"],
        "explicit_agent_provenance": bool(
            _EXPLICIT_AGENT_PROVENANCE.search(message)
        ),
    }


def _classified_commits(
    records: Sequence[Mapping[str, Any]],
    additions_by_commit: Mapping[str, int],
    baseline: Mapping[str, Any],
    thresholds: Sequence[int],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        _classified_commit(
            record,
            additions_by_commit.get(str(record["commit"]), 0),
            baseline,
            thresholds,
        )
        for record in records
    )


def _eligible_records_and_baseline(
    records: Sequence[Mapping[str, Any]],
    repository_id: str,
    adoption_at: str | None,
    infer_adoption_from_provenance: bool,
) -> tuple[tuple[Mapping[str, Any], ...], str | None, str, Mapping[str, Any]]:
    eligible = tuple(
        record
        for record in records
        if len(record["parents"]) <= 1 and normalize_message(record["message"])
    )
    resolved_adoption, adoption_source = _adoption_details(
        eligible, adoption_at, infer_adoption_from_provenance
    )
    baseline_messages = (
        tuple(
            str(record["message"])
            for record in eligible
            if _parse_time(str(record["committed_at"]))
            < _parse_time(resolved_adoption)
        )
        if resolved_adoption
        else ()
    )
    baseline = baseline_from_messages(
        baseline_messages,
        repository_id=repository_id,
    )
    return eligible, resolved_adoption, adoption_source, baseline


def _repository_threshold_results(
    commits: Sequence[Mapping[str, Any]], thresholds: Sequence[int]
) -> dict[str, Any]:
    excluded = {"eligible_commit_count", "eligible_added_line_count"}
    return {
        str(threshold): {
            key: value
            for key, value in _period_summary(commits, threshold).items()
            if key not in excluded
        }
        for threshold in thresholds
    }


def analyze_git_repository(
    *,
    repository_id: str,
    path: Path,
    cutoff_commit: str,
    start_at: str,
    adoption_at: str | None,
    infer_adoption_from_provenance: bool = True,
    thresholds: Sequence[int] = (3, 4, 5),
) -> dict[str, Any]:
    """Analyze one repository at an exact pinned cutoff."""
    try:
        _git(path, "cat-file", "-e", f"{cutoff_commit}^{{commit}}")
    except CommitMessageAnalysisError as error:
        raise CommitMessageAnalysisError(
            f"{repository_id}: cutoff commit is not available"
        ) from error
    records = _commit_metadata(path, cutoff_commit, start_at)
    eligible, resolved_adoption, adoption_source, baseline = (
        _eligible_records_and_baseline(
            records,
            repository_id,
            adoption_at,
            infer_adoption_from_provenance,
        )
    )
    additions_by_commit = _added_code_lines_by_commit(path, cutoff_commit, start_at)
    commits = _classified_commits(eligible, additions_by_commit, baseline, thresholds)
    eligible_lines = sum(commit["added_code_lines"] for commit in commits)
    return {
        "repository_id": repository_id,
        "cutoff_commit": cutoff_commit,
        "start_at": start_at,
        "adoption_at": resolved_adoption,
        "adoption_source": adoption_source,
        "baseline": baseline,
        "eligible_commit_count": len(commits),
        "eligible_added_line_count": eligible_lines,
        "thresholds": _repository_threshold_results(commits, thresholds),
        "within_repository": _within_repository_contrast(
            commits, resolved_adoption, thresholds
        ),
        "commits": commits,
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CommitMessageAnalysisError(f"cannot load JSON from {path}") from error


def _repository_path(
    repository_id: str,
    cutoff_commit: str,
    cache_roots: Sequence[Path],
) -> Path | None:
    expected = repository_id.replace("/", "__").casefold()
    for root in cache_roots:
        if not root.is_dir():
            continue
        candidates = sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and child.name.casefold() == expected
            ),
            key=lambda path: str(path),
        )
        for candidate in candidates:
            try:
                _git(candidate, "cat-file", "-e", f"{cutoff_commit}^{{commit}}")
            except CommitMessageAnalysisError:
                continue
            return candidate
    return None


def _effective_start(reference: Mapping[str, Any]) -> str | None:
    date_range = reference.get("effective_date_range")
    if not isinstance(date_range, list) or len(date_range) != 2:
        return None
    return date_range[0] if isinstance(date_range[0], str) else None


def _reference_index(reference_manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    repositories = reference_manifest.get("repositories", [])
    return {
        str(repository["id"]).casefold(): repository
        for repository in repositories
        if isinstance(repository, Mapping) and isinstance(repository.get("id"), str)
    }


def _validate_protocol(
    protocol: Mapping[str, Any], thresholds: Sequence[int]
) -> None:
    decision_rule = protocol.get("decision_rule")
    if (
        protocol.get("protocol_version") != 1
        or protocol.get("status") != "frozen_before_scoring"
        or not isinstance(decision_rule, Mapping)
        or decision_rule.get("primary_threshold") != 4
        or decision_rule.get("sensitivity_thresholds") != [3, 5]
        or tuple(sorted(thresholds)) != (3, 4, 5)
    ):
        raise CommitMessageAnalysisError(
            "protocol and execution thresholds must match the frozen v1 rule"
        )


def _validation_reference(
    reference: Mapping[str, Any],
) -> tuple[str, str | None, str | None]:
    label = reference.get("label")
    date_range = reference.get("effective_date_range")
    if label not in {"agent", "human"}:
        raise CommitMessageAnalysisError("reference label must be agent or human")
    if not isinstance(date_range, list) or len(date_range) != 2:
        raise CommitMessageAnalysisError(
            "reference effective_date_range must contain two bounds"
        )
    return label, date_range[0], date_range[1]


def _validation_cell(label: str, detected: bool) -> str:
    if label == "agent":
        return "tp" if detected else "fn"
    return "fp" if detected else "tn"


def _validation_counts(
    repositories: Sequence[Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Any]],
    thresholds: Sequence[int],
) -> dict[str, Any]:
    outcomes = {
        str(threshold): {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
        for threshold in thresholds
    }
    for repository in repositories:
        reference = references.get(str(repository["repository_id"]).casefold())
        if not reference:
            continue
        label, start, end = _validation_reference(reference)
        for commit in repository["commits"]:
            committed_at = _parse_time(commit["committed_at"])
            if start and committed_at < _parse_time(start):
                continue
            if end and committed_at > _parse_time(end):
                continue
            for threshold in thresholds:
                detected = commit["classifications"][f"threshold_{threshold}"]
                cell = _validation_cell(label, detected)
                outcomes[str(threshold)][cell] += 1
    for counts in outcomes.values():
        precision_denominator = counts["tp"] + counts["fp"]
        recall_denominator = counts["tp"] + counts["fn"]
        counts["precision"] = (
            counts["tp"] / precision_denominator if precision_denominator else None
        )
        counts["recall"] = (
            counts["tp"] / recall_denominator if recall_denominator else None
        )
    return outcomes


def _analyze_selected_repositories(
    selected: Sequence[Mapping[str, Any]],
    references: Mapping[str, Mapping[str, Any]],
    cache_roots: Sequence[Path],
    thresholds: Sequence[int],
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    analyzed = []
    unavailable = []
    for repository in selected:
        repository_id = repository["canonical_repository_id"]
        cutoff_commit = repository["cutoff_commit"]
        path = _repository_path(repository_id, cutoff_commit, cache_roots)
        if path is None:
            unavailable.append(repository_id)
            continue
        reference = references.get(repository_id.casefold(), {})
        analyzed.append(
            analyze_git_repository(
                repository_id=repository_id,
                path=path,
                cutoff_commit=cutoff_commit,
                start_at="2023-01-01T00:00:00Z",
                adoption_at=(
                    _effective_start(reference)
                    if reference.get("label") == "agent"
                    else None
                ),
                infer_adoption_from_provenance=(
                    "adoption_agent_seed" in repository["roles"]
                ),
                thresholds=thresholds,
            )
        )
    return tuple(analyzed), tuple(unavailable)


def _aggregate_thresholds(
    repositories: Sequence[Mapping[str, Any]], thresholds: Sequence[int]
) -> dict[str, Any]:
    commits = sum(repository["eligible_commit_count"] for repository in repositories)
    lines = sum(repository["eligible_added_line_count"] for repository in repositories)
    return {
        str(threshold): _aggregate_one_threshold(
            repositories, threshold, commits, lines
        )
        for threshold in thresholds
    }


def _aggregate_one_threshold(
    repositories: Sequence[Mapping[str, Any]],
    threshold: int,
    commits: int,
    lines: int,
) -> dict[str, Any]:
    detected = sum(
        repository["thresholds"][str(threshold)]["detected_commit_count"]
        for repository in repositories
    )
    attributed = sum(
        repository["thresholds"][str(threshold)][
            "agent_attributed_added_line_count"
        ]
        for repository in repositories
    )
    return {
        "detected_commit_count": detected,
        "eligible_commit_count": commits,
        "detected_commit_share": detected / commits if commits else None,
        "agent_attributed_added_line_count": attributed,
        "eligible_added_line_count": lines,
        "agent_attributed_added_line_share": attributed / lines if lines else None,
    }


def _sourcegraph_asset(
    index_manifest: Mapping[str, Any],
    index_manifest_path: Path,
    roles: Sequence[str],
) -> dict[str, Any]:
    coverage = index_manifest.get("sourcegraph_coverage", {})
    ready_count = (
        sum(
            int(count)
            for status, count in coverage.items()
            if str(status).startswith("ready_")
        )
        if isinstance(coverage, Mapping)
        else 0
    )
    return {
        "index_manifest": str(index_manifest_path),
        "canonical_repository_count": index_manifest["repository_count"],
        "indexed_repository_count": ready_count
        or index_manifest["repository_count"],
        "coverage": dict(coverage) if isinstance(coverage, Mapping) else {},
        "selected_roles": list(roles),
    }


def build_analysis(
    *,
    index_manifest_path: Path,
    reference_manifest_path: Path,
    protocol_path: Path,
    cache_roots: Sequence[Path],
    roles: Sequence[str],
    thresholds: Sequence[int] = (3, 4, 5),
) -> dict[str, Any]:
    index_manifest = _load_json(index_manifest_path)
    reference_manifest = _load_json(reference_manifest_path)
    protocol_bytes = protocol_path.read_bytes()
    protocol = _load_json(protocol_path)
    _validate_protocol(protocol, thresholds)
    references = _reference_index(reference_manifest)
    selected = tuple(
        repository
        for repository in index_manifest["repositories"]
        if any(role in repository["roles"] for role in roles)
    )
    analyzed, unavailable = _analyze_selected_repositories(
        selected, references, cache_roots, thresholds
    )
    return {
        "analysis_version": 1,
        "study_role": "exploratory",
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "proxy_assumption": protocol["proxy_assumption"],
        "sourcegraph_asset": _sourcegraph_asset(
            index_manifest, index_manifest_path, roles
        ),
        "execution": {
            "selected_repository_count": len(selected),
            "analyzed_repository_count": len(analyzed),
            "unavailable_repository_ids": unavailable,
        },
        "threshold_totals": _aggregate_thresholds(analyzed, thresholds),
        "validation": _validation_counts(analyzed, references, thresholds),
        "repositories": analyzed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-manifest", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--cache-root", action="append", type=Path, required=True)
    parser.add_argument(
        "--role",
        action="append",
        dest="roles",
        default=[],
        help="Select repositories with this Sourcegraph manifest role.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    roles = arguments.roles or [
        "classifier_agent_reference",
        "classifier_human_reference",
    ]
    result = build_analysis(
        index_manifest_path=arguments.index_manifest,
        reference_manifest_path=arguments.reference_manifest,
        protocol_path=arguments.protocol,
        cache_roots=arguments.cache_root,
        roles=roles,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
