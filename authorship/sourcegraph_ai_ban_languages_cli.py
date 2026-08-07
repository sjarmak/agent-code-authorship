"""CLI for the frozen Sourcegraph language inventory of AI-ban controls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship import sg
from authorship.sourcegraph_ai_ban_languages import (
    AiBanLanguageError,
    build_ai_ban_language_inventory,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--expected-target-file-sha256", required=True)
    parser.add_argument("--expected-target-manifest-sha256", required=True)
    parser.add_argument("--index-manifest", type=Path, required=True)
    parser.add_argument("--expected-index-manifest-file-sha256", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _read_pinned(
    path: Path,
    expected_sha256: str,
    label: str,
) -> Mapping[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AiBanLanguageError(f"cannot read {label}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AiBanLanguageError(f"{label} differs from independent pin")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AiBanLanguageError(f"{label} JSON is invalid") from error
    if not isinstance(document, Mapping):
        raise AiBanLanguageError(f"{label} contract is invalid")
    return document


def _write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target = _read_pinned(
        args.target,
        args.expected_target_file_sha256,
        "AI-ban target",
    )
    manifest = _read_pinned(
        args.index_manifest,
        args.expected_index_manifest_file_sha256,
        "index manifest",
    )
    if target.get("target_manifest_sha256") != args.expected_target_manifest_sha256:
        raise AiBanLanguageError("AI-ban target differs from independent content pin")
    if (
        target.get("predecessors", {}).get("index_manifest_file_sha256")
        != args.expected_index_manifest_file_sha256
        or manifest.get("outcomes_consulted") is not False
    ):
        raise AiBanLanguageError("index manifest binding is invalid")
    inventory = build_ai_ban_language_inventory(
        target,
        sg.api,
        index_manifest_file_sha256=args.expected_index_manifest_file_sha256,
        observed_at=args.observed_at,
        batch_size=args.batch_size,
    )
    _write(args.output, inventory)
    print(
        json.dumps(
            {
                "inventory_sha256": inventory["inventory_sha256"],
                "repository_count": inventory["repository_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
