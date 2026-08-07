"""Compile blind adoption reliability responses after exact rematerialization."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from authorship.sourcegraph_adoption_audit_adapter import (
    AdoptionAuditAdapterError,
    build_combined_adoption_audit_inputs,
)
from authorship.sourcegraph_adoption_audit_cli import (
    _load_pinned_inputs,
    add_adoption_audit_source_arguments,
)
from authorship.sourcegraph_adoption_audit_compiler import (
    compile_adoption_reliability_responses,
)
from authorship.sourcegraph_adoption_review_cli import write_adoption_review_json
from authorship.sourcegraph_adoption_review_compile_cli import _load_mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile the independently pinned adoption reliability audit."
    )
    add_adoption_audit_source_arguments(parser)
    parser.add_argument("--worksheet", required=True, type=Path)
    parser.add_argument("--expected-worksheet-sha256", required=True)
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--expected-key-sha256", required=True)
    parser.add_argument("--expected-adapter-sha256", required=True)
    parser.add_argument("--response-bundle", required=True, type=Path)
    parser.add_argument("--auditor-id", required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _responses(path: Path) -> list[dict]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdoptionAuditAdapterError(f"cannot load {path}: {error}") from error
    if not isinstance(document, list) or any(
        not isinstance(item, dict) for item in document
    ):
        raise AdoptionAuditAdapterError("audit response bundle must be an array")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sequential, tranches, manifest, anchor, inventory = _load_pinned_inputs(args)
    combined, all_tranches, adapter = build_combined_adoption_audit_inputs(
        sequential,
        tranches,
        manifest,
        anchor,
    )
    if adapter["adapter_sha256"] != args.expected_adapter_sha256:
        raise AdoptionAuditAdapterError(
            "adapter differs from independently frozen hash"
        )
    worksheet = _load_mapping(args.worksheet)
    key = _load_mapping(args.key)
    expected_auditors = {
        task["audit_task_id"]: args.auditor_id for task in worksheet.get("tasks", [])
    }
    compiled = compile_adoption_reliability_responses(
        worksheet,
        key,
        _responses(args.response_bundle),
        combined,
        all_tranches,
        inventory,
        seed=args.seed,
        expected_worksheet_sha256=args.expected_worksheet_sha256,
        expected_key_sha256=args.expected_key_sha256,
        expected_auditor_ids=expected_auditors,
    )
    write_adoption_review_json(args.output, compiled)
    print(
        json.dumps(
            {
                "compiled_audit_sha256": compiled["compiled_audit_sha256"],
                "output": str(args.output),
                "sample_count": compiled["response_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
