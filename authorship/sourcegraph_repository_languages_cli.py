"""CLI for freezing Sourcegraph repository-language metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from authorship import sg
from authorship.sourcegraph_adoption_review import adoption_review_tranche_sha256
from authorship.sourcegraph_repository_languages import (
    RepositoryLanguageError,
    build_repository_language_inventory,
    sg_evals_repository_names,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze indexed Sourcegraph languages for the adoption frame."
    )
    parser.add_argument("--tranche", required=True, type=Path)
    parser.add_argument("--expected-tranche-sha256", required=True)
    parser.add_argument("--index-manifest", required=True, type=Path)
    parser.add_argument("--expected-index-manifest-sha256", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RepositoryLanguageError(f"cannot load {path}: {error}") from error
    if not isinstance(document, dict):
        raise RepositoryLanguageError(f"{path} must contain a JSON object")
    return document


def _validate_tranche(tranche: dict[str, Any], expected_sha256: str) -> None:
    actual = adoption_review_tranche_sha256(tranche)
    if tranche.get("tranche_sha256") != actual or expected_sha256 != actual:
        raise RepositoryLanguageError("expected tranche checksum does not match")


def _validate_index_manifest(path: Path, expected_sha256: str) -> None:
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise RepositoryLanguageError(f"cannot hash {path}: {error}") from error
    if actual != expected_sha256:
        raise RepositoryLanguageError("index manifest checksum does not match")


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tranche = _load_mapping(args.tranche)
    _validate_tranche(tranche, args.expected_tranche_sha256)
    _validate_index_manifest(
        args.index_manifest,
        args.expected_index_manifest_sha256,
    )
    index_manifest = _load_mapping(args.index_manifest)
    inventory = build_repository_language_inventory(
        tranche,
        sg.api,
        sourcegraph_names=sg_evals_repository_names(
            index_manifest,
            repository_ids={
                task["canonical_repository_id"] for task in tranche["tasks"]
            },
        ),
        index_manifest_sha256=args.expected_index_manifest_sha256,
        observed_at=args.observed_at,
        batch_size=args.batch_size,
    )
    _write_json(args.output, inventory)
    print(
        json.dumps(
            {
                "inventory_sha256": inventory["inventory_sha256"],
                "output": str(args.output),
                "repository_count": inventory["repository_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
