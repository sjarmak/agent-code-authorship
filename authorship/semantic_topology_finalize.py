"""Finalize the complete semantic-topology pilot into reusable artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from authorship.semantic_topology_protocol import (
    ANALYTIC_FIELDS,
    canonical_sha256,
    validate_semantic_change_record,
)
from authorship.semantic_topology_deep_search import validate_inventory
from authorship.semantic_topology_report import render_report
from authorship.semantic_topology_results import (
    assemble_utility_gates,
    attach_reliability_uncertainty,
    build_record_summary,
    build_run_summary,
    finalize_record_missingness,
    link_candidate_evidence,
)
from authorship.semantic_topology_verification import (
    build_candidate_inventory,
    build_matched_control_candidates,
    verify_candidate_inventory,
)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def finalize_pilot(root: Path, repository_root: Path) -> dict[str, Any]:
    """Require a complete corpus, then build all derived final artifacts."""
    protocol = _read(root / "study/semantic-topology-protocol.v1.json")
    plan = _read(root / "study/semantic-topology-pilot-plan.v1.json")
    deep_root = root / "results/semantic-topology-deep-search-v1"
    deep_inventory = _read(deep_root / "inventory.v1.json")
    run_paths = sorted((deep_root / "runs").glob("*.json"))
    expected = len(plan["runs"])
    if (
        len(run_paths) != expected
        or deep_inventory.get("materialized_run_count") != expected
    ):
        raise ValueError(
            "Deep Search corpus incomplete: "
            f"expected {expected}, found {len(run_paths)} files and "
            f"{deep_inventory.get('materialized_run_count')} inventoried"
        )
    inventory_errors = validate_inventory(deep_inventory, plan, deep_root)
    if inventory_errors:
        raise ValueError(
            "invalid Deep Search inventory: " + "; ".join(inventory_errors)
        )
    runs = [_read(path) for path in run_paths]
    run_summary = build_run_summary(runs)
    candidates = build_candidate_inventory(
        runs, plan["plan_sha256"], plan["protocol_sha256"]
    )
    sourcegraph_to_canonical = {
        item["sourcegraph_name"]: item["canonical_repository_id"]
        for item in plan["repositories"]
    }
    verification = verify_candidate_inventory(
        candidates, sourcegraph_to_canonical, repository_root
    )
    controls = build_matched_control_candidates(runs, sourcegraph_to_canonical)

    records_root = root / "results/semantic-topology-records-v1"
    record_inventory = _read(records_root / "inventory.v1.json")
    records = _read_jsonl(records_root / "semantic-change-records.v1.jsonl")
    enriched = link_candidate_evidence(records, candidates)
    primary = _read(root / "results/semantic-topology-reliability.v1.json")
    supplemental = _read(
        root / "results/semantic-topology-followup-count-reliability.v1.json"
    )
    distinct_author = _read(
        root / "results/semantic-topology-distinct-author-reliability.v1.json"
    )
    enriched = attach_reliability_uncertainty(
        enriched, primary, supplemental, distinct_author
    )
    enriched = finalize_record_missingness(enriched)
    for record in enriched:
        errors = validate_semantic_change_record(record)
        if errors:
            raise ValueError("; ".join(errors))

    output_root = root / "results"
    _write_json(output_root / "semantic-topology-candidates.v1.json", candidates)
    _write_json(
        output_root / "semantic-topology-candidate-verification.v1.json",
        verification,
    )
    _write_json(output_root / "semantic-topology-control-candidates.v1.json", controls)
    enriched_path = output_root / "semantic-change-records.enriched.v1.jsonl"
    enriched_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in enriched)
    )
    enriched_inventory: dict[str, Any] = {
        "inventory_version": 1,
        "record_count": len(enriched),
        "schema_validated_record_count": len(enriched),
        "pending_reason_count": sum("pending_" in str(record) for record in enriched),
        "record_file": enriched_path.name,
        "record_file_sha256": hashlib.sha256(enriched_path.read_bytes()).hexdigest(),
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "outcomes_consulted": False,
    }
    enriched_inventory["inventory_sha256"] = canonical_sha256(enriched_inventory)
    _write_json(
        output_root / "semantic-change-records.enriched.inventory.v1.json",
        enriched_inventory,
    )
    record_summary = build_record_summary(enriched)
    _write_json(
        output_root / "semantic-topology-record-summary.v1.json",
        record_summary,
    )
    _write_json(
        output_root / "semantic-topology-run-summary.v1.json",
        run_summary,
    )

    gates = assemble_utility_gates(primary, supplemental, distinct_author, None, None)
    _write_json(output_root / "semantic-topology-utility-gates.v1.json", gates)
    report = render_report(
        protocol=protocol,
        plan=plan,
        deep_inventory=deep_inventory,
        record_inventory=record_inventory,
        candidate_inventory=candidates,
        candidate_verification=verification,
        control_candidates=controls,
        gates=gates,
        terminal_failures=[
            {
                "canonical_repository_id": run["canonical_repository_id"],
                "prompt_family_id": run["prompt_family_id"],
                "error_code": (run.get("error") or {}).get(
                    "code", run["terminal_status"]
                ),
            }
            for run in runs
            if run["terminal_status"] != "completed"
        ],
        field_status_counts={
            field: dict(
                sorted(Counter(record[field]["status"] for record in enriched).items())
            )
            for field in ANALYTIC_FIELDS
        },
        record_summary=record_summary,
        run_summary=run_summary,
    )
    report_path = output_root / "SEMANTIC_TOPOLOGY_REPORT.v1.md"
    report_path.write_text(report)
    manifest: dict[str, Any] = {
        "finalization_version": 1,
        "deep_search_run_count": len(runs),
        "semantic_record_count": len(enriched),
        "deep_search_inventory_sha256": deep_inventory["inventory_sha256"],
        "enriched_inventory_sha256": enriched_inventory["inventory_sha256"],
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "candidate_verification_sha256": verification["verification_sha256"],
        "control_candidates_sha256": controls["artifact_sha256"],
        "utility_gates_sha256": gates["result_sha256"],
        "record_summary_sha256": record_summary["summary_sha256"],
        "run_summary_sha256": run_summary["summary_sha256"],
        "enriched_record_file_sha256": enriched_inventory["record_file_sha256"],
        "report_file_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    }
    manifest["finalization_sha256"] = canonical_sha256(manifest)
    _write_json(output_root / "semantic-topology-finalization.v1.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path("/mnt/agent-code-authorship/survival-study/repositories"),
    )
    args = parser.parse_args()
    manifest = finalize_pilot(args.root.resolve(), args.repository_root)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
