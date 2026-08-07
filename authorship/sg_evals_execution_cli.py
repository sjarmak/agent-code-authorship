"""Command-line boundary for the approval-scoped sg-evals executor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from authorship.sg_evals_execution import (
    APPROVAL_SCOPES,
    ExecutionError,
    execute_plan,
    execution_preview,
)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("study/sg-evals-action-plan.v3.json"),
    )
    parser.add_argument("--scope", choices=sorted(APPROVAL_SCOPES), required=True)
    parser.add_argument(
        "--journal",
        type=Path,
        default=Path("study/sg-evals-execution-journal.v3.json"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-sha")
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    plan = json.loads(arguments.plan.read_text())
    if not arguments.apply:
        print(json.dumps(execution_preview(plan, arguments.scope), indent=2))
        return
    if not arguments.confirm_plan_sha:
        raise ExecutionError("--apply requires --confirm-plan-sha")
    journal = execute_plan(
        plan,
        scope=arguments.scope,
        confirmed_plan_sha=arguments.confirm_plan_sha,
        journal_path=arguments.journal,
    )
    print(arguments.journal)
    print(journal["journal_sha256"])
