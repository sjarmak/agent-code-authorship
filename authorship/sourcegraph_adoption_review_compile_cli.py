"""CLI for compiling complete adoption-review responses into a decision ledger."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_adoption_review import (
    AdoptionReviewError,
    build_adoption_decision_ledger,
    materialize_adoption_review_tranche,
    validate_adoption_decision_ledger,
)
from authorship.sourcegraph_adoption_review_cli import (
    write_adoption_review_json,
)

RESPONSE_BUNDLE_FIELDS = frozenset(
    {
        "reviewer_id",
        "tranche_id",
        "task_start",
        "task_end",
        "responses",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile one complete adoption-review tranche."
    )
    parser.add_argument("--tranche", type=Path, required=True)
    parser.add_argument(
        "--response-bundle",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--prior-ledger", type=Path)
    parser.add_argument("--expected-tranche-sha256", required=True)
    parser.add_argument("--expected-prior-ledger-sha256")
    parser.add_argument(
        "--case-index",
        type=Path,
        default=Path("study/sourcegraph-repository-case-index.v3.json"),
    )
    parser.add_argument(
        "--case-root",
        type=Path,
        default=Path("results/sourcegraph-repository-cases-v3"),
    )
    parser.add_argument(
        "--review-protocol",
        type=Path,
        default=Path("study/sourcegraph-adoption-review-protocol.v3.json"),
    )
    parser.add_argument(
        "--discovery-specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--workflow-specification",
        type=Path,
        default=Path("study/sourcegraph-adjudication-workflow.v3.json"),
    )
    parser.add_argument(
        "--packet-index",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.v3.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdoptionReviewError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise AdoptionReviewError(f"{path} must contain a JSON object")
    return value


def _responses(
    paths: Sequence[Path],
    tranche: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    tasks = tranche.get("tasks")
    if not isinstance(tasks, list):
        raise AdoptionReviewError("review tranche tasks are invalid")
    task_positions = {task.get("task_id"): index for index, task in enumerate(tasks)}
    responses = []
    for path in paths:
        bundle = _load_mapping(path)
        if set(bundle) != RESPONSE_BUNDLE_FIELDS:
            raise AdoptionReviewError("response bundle contract is invalid")
        if bundle.get("tranche_id") != tranche.get("tranche_id"):
            raise AdoptionReviewError("response bundle tranche binding does not match")
        reviewer_id = bundle.get("reviewer_id")
        start = bundle.get("task_start")
        end = bundle.get("task_end")
        bundle_responses = bundle.get("responses")
        if not isinstance(bundle_responses, list) or any(
            not isinstance(response, Mapping) for response in bundle_responses
        ):
            raise AdoptionReviewError("response bundle responses are invalid")
        if (
            not isinstance(reviewer_id, str)
            or not reviewer_id.strip()
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
            or len(bundle_responses) != end - start + 1
            or any(
                response.get("reviewer_id") != reviewer_id
                for response in bundle_responses
            )
        ):
            raise AdoptionReviewError("response bundle assignment is invalid")
        positions = [
            task_positions.get(response.get("task_id")) for response in bundle_responses
        ]
        if positions != list(range(start, end + 1)):
            raise AdoptionReviewError("response bundle task range does not match")
        responses.extend(bundle_responses)
    return responses


def _prior_ledger(
    path: Path | None,
    expected_sha256: str | None,
    tranche: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    if path is None:
        if expected_sha256 is not None:
            raise AdoptionReviewError("expected prior ledger has no input file")
        return None, []
    if expected_sha256 is None:
        raise AdoptionReviewError("prior ledger requires its frozen expected checksum")
    ledger = _load_mapping(path)
    if ledger.get("decision_ledger_sha256") != expected_sha256:
        raise AdoptionReviewError("prior ledger differs from frozen expected checksum")
    decisions = validate_adoption_decision_ledger(
        ledger,
        case_index_sha256_value=tranche["case_index_sha256"],
        workflow_sha256_value=tranche["workflow_sha256"],
    )
    return ledger, decisions


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tranche = _load_mapping(args.tranche)
    case_index = _load_mapping(args.case_index)
    review_protocol = _load_mapping(args.review_protocol)
    discovery = _load_mapping(args.discovery_specification)
    workflow = _load_mapping(args.workflow_specification)
    prior_ledger, prior_decisions = _prior_ledger(
        args.prior_ledger,
        args.expected_prior_ledger_sha256,
        tranche,
    )
    expected_tranche = materialize_adoption_review_tranche(
        case_index,
        review_protocol,
        args.case_root,
        discovery,
        workflow,
        args.packet_index,
        prior_ledger,
        expected_decision_ledger_sha256=args.expected_prior_ledger_sha256,
        tranche_number=tranche["tranche_number"],
    )
    if (
        expected_tranche.get("tranche_sha256") != args.expected_tranche_sha256
        or tranche != expected_tranche
    ):
        raise AdoptionReviewError(
            "review tranche differs from frozen rematerialized evidence"
        )
    responses = _responses(args.response_bundle, tranche)
    ledger = build_adoption_decision_ledger(
        tranche,
        responses,
        prior_decisions,
        prior_decision_ledger_sha256=args.expected_prior_ledger_sha256,
    )
    write_adoption_review_json(args.output, ledger)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "response_count": ledger["response_count"],
                "decision_count": ledger["decision_count"],
                "decision_ledger_sha256": ledger["decision_ledger_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
