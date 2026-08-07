"""Join frozen message evidence with pinned Git timing/process evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.commit_timing_features import (
    CommitTimingEvidenceError,
    extract_repository_evidence,
)
from authorship.multiview_evaluation import (
    evaluate_era_holdout,
    evaluate_leave_one_repository_out,
)

TIMING_IDENTITY_FIELDS = frozenset(
    {
        "commit",
        "authored_at",
        "committed_at",
        "author_identity_sha256",
        "committer_identity_sha256",
    }
)


class MultiviewCommitAnalysisError(ValueError):
    """Frozen inputs cannot support the declared multi-view analysis."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise MultiviewCommitAnalysisError(
            f"cannot load JSON from {path}"
        ) from error


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise MultiviewCommitAnalysisError(
            f"invalid ISO-8601 timestamp: {value}"
        ) from error
    if parsed.tzinfo is None:
        raise MultiviewCommitAnalysisError(
            f"timestamp has no UTC offset: {value}"
        )
    return parsed.astimezone(timezone.utc)


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    evaluation = protocol.get("evaluation")
    leakage = protocol.get("leakage_controls")
    if (
        protocol.get("protocol_version") != 1
        or protocol.get("status") != "frozen_before_multiview_scoring"
        or not isinstance(evaluation, Mapping)
        or evaluation.get("primary_split") != "leave_one_repository_out"
        or evaluation.get("views")
        != ["message", "timing_process", "combined"]
        or not isinstance(leakage, Mapping)
    ):
        raise MultiviewCommitAnalysisError(
            "execution must match the frozen multi-view v1 protocol"
        )
    excluded = set(leakage.get("excluded_model_inputs", []))
    required = {
        "explicit_agent_provenance",
        "reference_label",
        "repository_id",
        "author_identity_sha256",
        "committer_identity_sha256",
    }
    if not required <= excluded:
        raise MultiviewCommitAnalysisError(
            "protocol does not exclude all label and identity leakage fields"
        )


def _repository_path(
    repository_id: str,
    cutoff_commit: str,
    cache_roots: Sequence[Path],
) -> Path | None:
    expected = repository_id.replace("/", "__").casefold()
    for root in cache_roots:
        candidate = root / repository_id.replace("/", "__")
        candidates = [candidate] if candidate.is_dir() else []
        if root.is_dir() and not candidates:
            candidates = [
                child
                for child in root.iterdir()
                if child.is_dir() and child.name.casefold() == expected
            ]
        for path in sorted(candidates, key=str):
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "cat-file",
                    "-e",
                    f"{cutoff_commit}^{{commit}}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return path
    return None


def _reference_index(
    document: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    return {
        str(repository["id"]).casefold(): repository
        for repository in document.get("repositories", [])
        if isinstance(repository, Mapping) and isinstance(repository.get("id"), str)
    }


def _reference_label(
    reference: Mapping[str, Any] | None,
    committed_at: str,
) -> str | None:
    if reference is None or reference.get("label") not in {"agent", "human"}:
        return None
    date_range = reference.get("effective_date_range")
    if not isinstance(date_range, list) or len(date_range) != 2:
        raise MultiviewCommitAnalysisError(
            "reference effective_date_range must have two bounds"
        )
    committed = _utc(committed_at)
    start, end = date_range
    if start is not None and committed < _utc(start):
        return None
    if end is not None and committed > _utc(end):
        return None
    return str(reference["label"])


def _outcome_status(
    explicit_agent_provenance: bool,
    reference_label: str | None,
) -> str:
    if explicit_agent_provenance:
        return "verified_agent_provenance"
    if reference_label == "human":
        return "human_control_reference"
    return "unresolved"


def _outcome_category(
    explicit_agent_provenance: bool,
    reference_label: str | None,
) -> str:
    if explicit_agent_provenance or reference_label == "agent":
        return "verified_agent"
    return "unknown_or_human_control"


def _joined_commit(
    repository_id: str,
    message_commit: Mapping[str, Any],
    timing: Mapping[str, Any],
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if message_commit["committed_at"] != timing["committed_at"]:
        if _utc(message_commit["committed_at"]) != _utc(timing["committed_at"]):
            raise MultiviewCommitAnalysisError(
                f"{repository_id}: commit timestamp does not match pinned Git"
            )
    label = _reference_label(reference, str(timing["committed_at"]))
    explicit = bool(message_commit.get("explicit_agent_provenance"))
    return {
        "repository_id": repository_id,
        "commit": message_commit["commit"],
        "authored_at": timing["authored_at"],
        "committed_at": timing["committed_at"],
        "author_identity_sha256": timing["author_identity_sha256"],
        "committer_identity_sha256": timing["committer_identity_sha256"],
        "reference_label": label,
        "explicit_agent_provenance": explicit,
        "outcome_status": _outcome_status(explicit, label),
        "outcome_category": _outcome_category(explicit, label),
        "message_score": message_commit["score"],
        "message_features": {
            key: message_commit["features"][key]
            for key in (
                "body_word_count",
                "bullet_count",
                "paragraph_count",
                "sentence_count",
                "sentence_line_rate",
                "structure_count",
                "explicit_validation",
            )
        },
        "timing_process_features": {
            key: value
            for key, value in timing.items()
            if key not in TIMING_IDENTITY_FIELDS
        },
    }


def _analyze_repository(
    message_repository: Mapping[str, Any],
    reference: Mapping[str, Any] | None,
    cache_roots: Sequence[Path],
) -> dict[str, Any] | None:
    repository_id = str(message_repository["repository_id"])
    cutoff_commit = str(message_repository["cutoff_commit"])
    path = _repository_path(repository_id, cutoff_commit, cache_roots)
    if path is None:
        return None
    commits = message_repository["commits"]
    try:
        timing = extract_repository_evidence(
            repository_id=repository_id,
            path=path,
            cutoff_commit=cutoff_commit,
            start_at=str(message_repository["start_at"]),
            eligible_commits={str(commit["commit"]) for commit in commits},
        )
    except CommitTimingEvidenceError as error:
        raise MultiviewCommitAnalysisError(str(error)) from error
    joined = tuple(
        _joined_commit(
            repository_id,
            commit,
            timing[str(commit["commit"])],
            reference,
        )
        for commit in commits
    )
    return {
        "repository_id": repository_id,
        "cutoff_commit": cutoff_commit,
        "start_at": message_repository["start_at"],
        "eligible_commit_count": len(joined),
        "labeled_commit_count": sum(
            commit["reference_label"] is not None for commit in joined
        ),
        "commits": joined,
    }


def _evaluations(
    repositories: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    labeled = tuple(
        commit
        for repository in repositories
        for commit in repository["commits"]
        if commit["reference_label"] is not None
    )
    views = protocol["evaluation"]["views"]
    cutoff = protocol["evaluation"]["era_cutoff"]
    return {
        "leave_one_repository_out": {
            view: evaluate_leave_one_repository_out(labeled, view=view)
            for view in views
        },
        "era_holdout": {
            view: evaluate_era_holdout(labeled, view=view, cutoff=cutoff)
            for view in views
        },
    }


def build_multiview_analysis(
    *,
    message_analysis_path: Path,
    reference_manifest_path: Path,
    protocol_path: Path,
    cache_roots: Sequence[Path],
) -> dict[str, Any]:
    """Materialize the frozen v1 multi-view evidence and evaluation."""
    message_bytes = message_analysis_path.read_bytes()
    reference_bytes = reference_manifest_path.read_bytes()
    protocol_bytes = protocol_path.read_bytes()
    message_analysis = json.loads(message_bytes)
    references_document = json.loads(reference_bytes)
    protocol = json.loads(protocol_bytes)
    _validate_protocol(protocol)
    references = _reference_index(references_document)
    repositories = []
    unavailable = []
    for message_repository in message_analysis["repositories"]:
        repository_id = str(message_repository["repository_id"])
        analyzed = _analyze_repository(
            message_repository,
            references.get(repository_id.casefold()),
            cache_roots,
        )
        if analyzed is None:
            unavailable.append(repository_id)
        else:
            repositories.append(analyzed)
    repositories = sorted(repositories, key=lambda item: item["repository_id"])
    outcome_counts = {
        outcome: sum(
            commit["outcome_category"] == outcome
            for repository in repositories
            for commit in repository["commits"]
        )
        for outcome in protocol["outcome_taxonomy"]
    }
    return {
        "analysis_version": 1,
        "study_role": "exploratory",
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "input_sha256": {
            "message_analysis": hashlib.sha256(message_bytes).hexdigest(),
            "reference_manifest": hashlib.sha256(reference_bytes).hexdigest(),
        },
        "sourcegraph_asset": message_analysis["sourcegraph_asset"],
        "execution": {
            "selected_repository_count": len(message_analysis["repositories"]),
            "analyzed_repository_count": len(repositories),
            "unavailable_repository_ids": sorted(unavailable),
            "labeled_commit_count": sum(
                repository["labeled_commit_count"]
                for repository in repositories
            ),
        },
        "outcome_counts": outcome_counts,
        "evaluations": _evaluations(repositories, protocol),
        "repositories": repositories,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-analysis", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = build_multiview_analysis(
        message_analysis_path=arguments.message_analysis,
        reference_manifest_path=arguments.reference_manifest,
        protocol_path=arguments.protocol,
        cache_roots=arguments.cache_root,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
