"""Build and execute cutoff-pinned Sourcegraph discovery blame enrichments."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from authorship.sg import check_auth
from authorship.sourcegraph_blame_execution import (
    build_blame_plan,
    execute_blame_plan,
    load_execution_enrichments,
    validate_blame_execution,
)
from authorship.sourcegraph_discovery_execution import atomic_write_json
from authorship.sourcegraph_discovery_inventory import (
    load_effective_result_manifests,
)
from authorship.sourcegraph_evidence_pipeline import build_packet_index


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot load {path}: {error}") from error
    if not isinstance(document, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich frozen Sourcegraph file matches with cutoff-pinned blame."
    )
    parser.add_argument(
        "--specification",
        type=Path,
        default=Path("study/sourcegraph-discovery.v3.json"),
    )
    parser.add_argument(
        "--index-manifest",
        type=Path,
        default=Path("study/sourcegraph-index-manifest.v3.json"),
    )
    parser.add_argument(
        "--effective-inventory",
        type=Path,
        default=Path("study/sourcegraph-effective-discovery-inventory.v3.json"),
    )
    parser.add_argument(
        "--base-root",
        type=Path,
        default=Path("results/sourcegraph-discovery-v3"),
    )
    parser.add_argument(
        "--partition-root",
        type=Path,
        default=Path("results/sourcegraph-discovery-partitions-v3"),
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("results/sourcegraph-blame-v3"),
    )
    parser.add_argument(
        "--plan-output",
        type=Path,
        default=Path("study/sourcegraph-blame-plan.v3.json"),
    )
    parser.add_argument(
        "--preliminary-packet-output",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.pre-blame.v3.json"),
    )
    parser.add_argument(
        "--final-packet-output",
        type=Path,
        default=Path("results/sourcegraph-evidence-packets.v3.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    specification = _load_json(arguments.specification)
    index_manifest = _load_json(arguments.index_manifest)
    inventory = _load_json(arguments.effective_inventory)
    result_manifests = load_effective_result_manifests(
        inventory,
        specification=specification,
        base_root=arguments.base_root,
        partition_root=arguments.partition_root,
    )
    preliminary = build_packet_index(
        specification,
        index_manifest,
        result_manifests,
        file_blame_enrichments=[],
    )
    atomic_write_json(arguments.preliminary_packet_output, preliminary)
    plan = build_blame_plan(preliminary)
    atomic_write_json(arguments.plan_output, plan)
    if plan["query_group_count"]:
        check_auth()
    execution = execute_blame_plan(
        plan,
        preliminary,
        arguments.output_directory,
    )
    errors = validate_blame_execution(
        execution,
        plan,
        preliminary,
        arguments.output_directory,
    )
    if errors:
        raise SystemExit("blame execution is invalid: " + "; ".join(errors))
    if execution["status"] != "complete":
        print(json.dumps(execution, indent=2, sort_keys=True))
        return 2
    enrichments = load_execution_enrichments(
        execution,
        plan,
        arguments.output_directory,
    )
    final = build_packet_index(
        specification,
        index_manifest,
        result_manifests,
        file_blame_enrichments=enrichments,
    )
    if final["pending_file_enrichment_count"]:
        raise SystemExit("final packet index still requires file blame enrichment")
    atomic_write_json(arguments.final_packet_output, final)
    print(json.dumps(execution, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
