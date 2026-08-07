"""Compile one frozen AI-ban response tranche into a cumulative ledger."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sourcegraph_ai_ban_review import (
    AiBanReviewError,
    compile_ai_ban_review_responses,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--expected-worksheet-sha256", required=True)
    parser.add_argument("--response-bundle", type=Path, required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--prior-ledger", type=Path)
    parser.add_argument("--expected-prior-ledger-sha256")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except OSError as error:
        raise AiBanReviewError(f"cannot read {label}: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiBanReviewError(f"{label} JSON is invalid") from error


def _mapping(path: Path, label: str) -> Mapping[str, Any]:
    document = _read_json(path, label)
    if not isinstance(document, Mapping):
        raise AiBanReviewError(f"{label} contract is invalid")
    return document


def _responses(path: Path) -> list[Mapping[str, Any]]:
    document = _read_json(path, "response bundle")
    if not isinstance(document, list) or any(
        not isinstance(response, Mapping) for response in document
    ):
        raise AiBanReviewError("response bundle contract is invalid")
    return document


def _prior_ledger(
    path: Path | None,
    expected_sha256: str | None,
) -> Mapping[str, Any] | None:
    if path is None:
        if expected_sha256 is not None:
            raise AiBanReviewError("expected prior ledger pin has no input")
        return None
    if expected_sha256 is None:
        raise AiBanReviewError("prior ledger requires an independent pin")
    ledger = _mapping(path, "prior ledger")
    if ledger.get("decision_ledger_sha256") != expected_sha256:
        raise AiBanReviewError("prior ledger differs from independent pin")
    return ledger


def _write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prior = _prior_ledger(
        args.prior_ledger,
        args.expected_prior_ledger_sha256,
    )
    worksheet = _mapping(args.worksheet, "worksheet")
    responses = _responses(args.response_bundle)
    expected_reviewers = {
        task["task_id"]: args.reviewer_id for task in worksheet.get("tasks", [])
    }
    ledger = compile_ai_ban_review_responses(
        worksheet,
        responses,
        prior_ledger=prior,
        expected_worksheet_sha256=args.expected_worksheet_sha256,
        expected_reviewer_ids=expected_reviewers,
    )
    _write(args.output, ledger)
    print(
        json.dumps(
            {
                "decision_count": ledger["decision_count"],
                "decision_ledger_sha256": ledger["decision_ledger_sha256"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
