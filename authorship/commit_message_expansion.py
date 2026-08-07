"""Run the frozen message heuristic on adjudicated adoption candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from authorship.commit_message_analysis import (
    CommitMessageAnalysisError,
    analyze_git_repository,
)

ACCEPTED_ADJUDICATIONS = frozenset({"confirmed", "observed"})
THRESHOLDS = (3, 4, 5)


class ExpansionAnalysisError(ValueError):
    """Frozen expansion inputs cannot support the declared analysis."""


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ExpansionAnalysisError(f"invalid ISO-8601 timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise ExpansionAnalysisError(f"timestamp has no UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ExpansionAnalysisError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    decision = protocol.get("decision_rule")
    if (
        protocol.get("protocol_version") != 1
        or protocol.get("status") != "frozen_before_scoring"
        or not isinstance(decision, Mapping)
        or decision.get("primary_threshold") != 4
        or decision.get("sensitivity_thresholds") != [3, 5]
    ):
        raise ExpansionAnalysisError("expansion must use the frozen message v1 rule")


def _validate_adjudication(adjudication: Mapping[str, Any]) -> None:
    candidates = adjudication.get("candidates")
    if (
        adjudication.get("artifact_id")
        != "commit-message-era-candidate-adjudication"
        or adjudication.get("status")
        != "sourcegraph_evidence_adjudicated_for_expansion"
        or not isinstance(candidates, list)
    ):
        raise ExpansionAnalysisError("invalid expansion adjudication artifact")
    repository_ids = [
        str(candidate.get("canonical_repository_id"))
        for candidate in candidates
        if isinstance(candidate, Mapping)
    ]
    if len(repository_ids) != len(candidates) or len(set(repository_ids)) != len(
        repository_ids
    ):
        raise ExpansionAnalysisError("candidate repository IDs must be unique")
    for candidate in candidates:
        if _is_eligible(candidate):
            pre_count = candidate.get("eligible_pre_adoption_message_count")
            if not isinstance(pre_count, int) or pre_count < 30:
                raise ExpansionAnalysisError(
                    f"{candidate['canonical_repository_id']}: eligible candidate "
                    "requires at least 30 pre-adoption messages"
                )
    summary = adjudication.get("summary")
    if isinstance(summary, Mapping):
        status_counts = {
            status: sum(
                candidate.get("adoption_adjudication_status") == status
                for candidate in candidates
            )
            for status in ("confirmed", "observed", "rejected")
        }
        expected = {
            "candidate_count": len(candidates),
            "confirmed_count": status_counts["confirmed"],
            "observed_count": status_counts["observed"],
            "rejected_count": status_counts["rejected"],
            "era_eligible_count": sum(_is_eligible(item) for item in candidates),
        }
        if any(summary.get(key) != value for key, value in expected.items()):
            raise ExpansionAnalysisError("adjudication summary counts do not match")


def _repository_path(
    repository_id: str,
    cutoff_commit: str,
    cache_roots: Sequence[Path],
) -> Path | None:
    expected = repository_id.replace("/", "__").casefold()
    for root in cache_roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.iterdir(), key=str):
            if not candidate.is_dir() or candidate.name.casefold() != expected:
                continue
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(candidate),
                    "cat-file",
                    "-e",
                    f"{cutoff_commit}^{{commit}}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode == 0:
                return candidate
    return None


def _event_landing_time(path: Path, candidate: Mapping[str, Any]) -> str:
    event_commit = str(candidate["candidate_commit_oid"])
    cutoff_commit = str(candidate["cutoff_commit"])
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "merge-base",
            "--is-ancestor",
            event_commit,
            cutoff_commit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ExpansionAnalysisError(
            f"{candidate['canonical_repository_id']}: event is not an ancestor "
            "of the frozen cutoff"
        )
    authored_at, committed_at = _git(
        path, "show", "-s", "--format=%aI%x00%cI", event_commit
    ).split("\x00")
    expected = _utc(str(candidate["candidate_event_at"]))
    if expected not in {_utc(authored_at), _utc(committed_at)}:
        raise ExpansionAnalysisError(
            f"{candidate['canonical_repository_id']}: event timestamp does not "
            "match pinned Git"
        )
    return committed_at


def _is_eligible(candidate: Mapping[str, Any]) -> bool:
    return (
        candidate.get("adoption_adjudication_status") in ACCEPTED_ADJUDICATIONS
        and candidate.get("era_adjustment_eligibility") == "eligible"
    )


def _aggregate(
    repositories: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    eligible_commits = sum(
        int(repository["eligible_commit_count"]) for repository in repositories
    )
    eligible_lines = sum(
        int(repository["eligible_added_line_count"]) for repository in repositories
    )
    result = {}
    for threshold in THRESHOLDS:
        summaries = [
            repository["thresholds"][str(threshold)]
            for repository in repositories
        ]
        detected = sum(int(summary["detected_commit_count"]) for summary in summaries)
        attributed = sum(
            int(summary["agent_attributed_added_line_count"])
            for summary in summaries
        )
        result[str(threshold)] = {
            "eligible_commit_count": eligible_commits,
            "detected_commit_count": detected,
            "detected_commit_share": (
                detected / eligible_commits if eligible_commits else None
            ),
            "eligible_added_line_count": eligible_lines,
            "agent_attributed_added_line_count": attributed,
            "agent_attributed_added_line_share": (
                attributed / eligible_lines if eligible_lines else None
            ),
        }
    return result


def _analyze_candidate(
    candidate: Mapping[str, Any],
    path: Path,
) -> dict[str, Any]:
    adoption_at = _event_landing_time(path, candidate)
    try:
        analysis = analyze_git_repository(
            repository_id=str(candidate["canonical_repository_id"]),
            path=path,
            cutoff_commit=str(candidate["cutoff_commit"]),
            start_at="2023-01-01T00:00:00Z",
            adoption_at=adoption_at,
            infer_adoption_from_provenance=False,
            thresholds=THRESHOLDS,
        )
    except CommitMessageAnalysisError as error:
        raise ExpansionAnalysisError(str(error)) from error
    frozen_count = candidate.get("eligible_pre_adoption_message_count")
    observed_count = analysis["baseline"]["commit_count"]
    if frozen_count != observed_count:
        raise ExpansionAnalysisError(
            f"{candidate['canonical_repository_id']}: pre-adoption message count "
            f"{observed_count} does not match frozen {frozen_count}"
        )
    return {
        **analysis,
        "candidate_event_at": candidate["candidate_event_at"],
        "candidate_commit_oid": candidate["candidate_commit_oid"],
        "adoption_adjudication_status": candidate["adoption_adjudication_status"],
        "event_evidence_tier": candidate["event_evidence_tier"],
        "sourcegraph_repository": candidate["sourcegraph_repository"],
        "sourcegraph_evidence": candidate.get("sourcegraph_evidence"),
    }


def build_expansion_analysis(
    *,
    adjudication_path: Path,
    protocol_path: Path,
    cache_roots: Sequence[Path],
) -> dict[str, Any]:
    """Analyze every accessible, adjudicated, era-eligible candidate."""
    adjudication_bytes = adjudication_path.read_bytes()
    protocol_bytes = protocol_path.read_bytes()
    adjudication = json.loads(adjudication_bytes)
    protocol = json.loads(protocol_bytes)
    _validate_adjudication(adjudication)
    _validate_protocol(protocol)
    candidates = adjudication["candidates"]
    eligible = [candidate for candidate in candidates if _is_eligible(candidate)]
    repositories = []
    unavailable = []
    for candidate in eligible:
        repository_id = str(candidate["canonical_repository_id"])
        path = _repository_path(
            repository_id,
            str(candidate["cutoff_commit"]),
            cache_roots,
        )
        if path is None:
            unavailable.append(repository_id)
            continue
        repositories.append(_analyze_candidate(candidate, path))
    repositories = sorted(repositories, key=lambda item: item["repository_id"])
    return {
        "analysis_version": 1,
        "study_role": "exploratory",
        "proxy_assumption": protocol["proxy_assumption"],
        "sourcegraph_endpoint": adjudication["sourcegraph_endpoint"],
        "input_sha256": {
            "adjudication": hashlib.sha256(adjudication_bytes).hexdigest(),
            "protocol": hashlib.sha256(protocol_bytes).hexdigest(),
        },
        "execution": {
            "candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "analyzed_repository_count": len(repositories),
            "unavailable_repository_ids": sorted(unavailable),
        },
        "threshold_totals": _aggregate(repositories),
        "repositories": repositories,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjudication", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = build_expansion_analysis(
        adjudication_path=arguments.adjudication,
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
