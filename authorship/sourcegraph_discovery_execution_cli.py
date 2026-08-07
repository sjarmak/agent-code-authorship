"""Command-line entry point for frozen Sourcegraph discovery execution."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sg import check_auth
from authorship.sourcegraph_discovery_execution import execute_discovery


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_exclusions_present(specification: Mapping[str, Any]) -> bool:
    execution = specification.get("execution")
    if not isinstance(execution, Mapping):
        return False
    exclusions = execution.get("repository_exclusions")
    if not isinstance(exclusions, Mapping):
        return False
    repositories = exclusions.get("repositories")
    return isinstance(repositories, list) and bool(repositories)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise SystemExit(f"cannot load {path}: {error}") from error


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute the frozen Sourcegraph discovery query families."
    )
    parser.add_argument(
        "--specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=Path("study/protocol.v3.json"),
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
    )
    parser.add_argument(
        "--index-audit",
        type=Path,
        default=Path("study/sourcegraph-index-audit.v3.json"),
    )
    parser.add_argument(
        "--repository-exclusion-source",
        type=Path,
        default=Path("study/sg-evals-action-plan.v3.json"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("results/sourcegraph-discovery-v3"),
    )
    parser.add_argument(
        "--retry-execution-errors-only",
        action="store_true",
        help=(
            "Reuse validated valid and non-execution-error shards, and rerun only "
            "shards whose invalid reason includes execution_error."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    specification = _load_json(arguments.specification)
    _load_json(arguments.protocol)
    if specification.get("protocol_sha256") != _file_sha256(arguments.protocol):
        raise SystemExit(
            "protocol SHA-256 does not match frozen discovery specification"
        )
    index_manifest = _load_json(arguments.index_manifest)
    index_audit = _load_json(arguments.index_audit)
    repository_exclusion_source = (
        _read_bytes(arguments.repository_exclusion_source)
        if _repository_exclusions_present(specification)
        else None
    )
    check_auth()
    manifest = execute_discovery(
        specification,
        index_manifest,
        index_audit,
        arguments.output_directory,
        index_manifest_sha256=_file_sha256(arguments.index_manifest),
        index_audit_sha256=_file_sha256(arguments.index_audit),
        repository_exclusion_source=repository_exclusion_source,
        retry_execution_errors_only=arguments.retry_execution_errors_only,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
