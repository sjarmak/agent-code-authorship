"""Requirement-level completion audit for the semantic-topology pilot."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from authorship.semantic_topology_protocol import canonical_sha256

EXPECTED_STRATA = {
    "agent_native": 5,
    "attributable_agent": 5,
    "policy_control": 5,
    "pre_2023_context": 5,
}
EXPECTED_PROMPT_FAMILIES = {
    "adoption_configuration",
    "delegated_task_intent",
    "review_validation",
    "semantic_controls",
    "change_topology",
}


def audit_documents(
    *,
    protocol: dict[str, Any],
    plan: dict[str, Any],
    deep_inventory: dict[str, Any],
    record_inventory: dict[str, Any],
    enriched_inventory: dict[str, Any],
    gates: dict[str, Any],
    finalization: dict[str, Any],
) -> dict[str, Any]:
    """Audit objective-wide cardinality, leakage, data, and utility evidence."""
    errors = []
    repositories = plan.get("repositories", [])
    runs = plan.get("runs", [])
    if len(repositories) != 20:
        errors.append("pilot must contain exactly 20 repositories")
    if Counter(item.get("stratum") for item in repositories) != Counter(
        EXPECTED_STRATA
    ):
        errors.append("pilot strata must contain exactly five repositories each")
    if len(runs) != 100:
        errors.append("pilot must contain exactly 100 frozen investigations")
    per_repository = Counter(item.get("canonical_repository_id") for item in runs)
    if runs and set(per_repository.values()) != {5}:
        errors.append("every repository must have exactly five investigations")
    if (
        runs
        and {item.get("prompt_family_id") for item in runs} != EXPECTED_PROMPT_FAMILIES
    ):
        errors.append("all five prompt families must be represented")
    if (
        protocol.get("outcomes_consulted") is not False
        or plan.get("outcomes_consulted") is not False
    ):
        errors.append("protocol and plan must remain outcome blind")
    if (
        deep_inventory.get("planned_run_count") != 100
        or deep_inventory.get("materialized_run_count") != 100
        or sum(deep_inventory.get("counts", {}).values()) != 100
    ):
        errors.append("Deep Search inventory is incomplete")
    record_count = record_inventory.get("record_count", 0)
    if record_count <= 0:
        errors.append("semantic change record dataset is empty")
    if record_inventory.get("failure_count", 0):
        errors.append("semantic change record materialization has failures")
    if (
        enriched_inventory.get("record_count") != record_count
        or enriched_inventory.get("outcomes_consulted") is not False
    ):
        errors.append("enriched record inventory does not preserve the cohort")
    if enriched_inventory.get("schema_validated_record_count") != record_count:
        errors.append("not all enriched records are schema validated")
    if enriched_inventory.get("pending_reason_count") != 0:
        errors.append("enriched records retain provisional pending reasons")
    if not any(gate.get("passes") for gate in gates.get("gates", {}).values()):
        errors.append("no preregistered utility gate passed")
    if gates.get("pilot_result") != "validated_utility":
        errors.append("pilot result is not validated_utility")
    if finalization.get("deep_search_run_count") != 100:
        errors.append("finalization does not bind all Deep Search runs")
    if finalization.get("semantic_record_count") != record_count:
        errors.append("finalization record count mismatch")
    if finalization.get("deep_search_inventory_sha256") != deep_inventory.get(
        "inventory_sha256"
    ):
        errors.append("finalization Deep Search inventory binding mismatch")
    if finalization.get("enriched_inventory_sha256") != enriched_inventory.get(
        "inventory_sha256"
    ):
        errors.append("finalization enriched inventory binding mismatch")
    if finalization.get("utility_gates_sha256") != gates.get("result_sha256"):
        errors.append("finalization utility-gate binding mismatch")
    result: dict[str, Any] = {
        "audit_version": 1,
        "passes": not errors,
        "errors": sorted(set(errors)),
        "requirements_checked": {
            "repository_count": len(repositories),
            "stratum_counts": dict(
                sorted(Counter(item.get("stratum") for item in repositories).items())
            ),
            "investigation_count": len(runs),
            "deep_search_terminal_count": sum(
                deep_inventory.get("counts", {}).values()
            ),
            "semantic_record_count": record_count,
            "utility_gate_pass_count": sum(
                bool(gate.get("passes")) for gate in gates.get("gates", {}).values()
            ),
        },
    }
    result["audit_sha256"] = canonical_sha256(result)
    return result


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def main() -> None:  # pragma: no cover - exercised as a final CLI gate
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/semantic-topology-completion-audit.v1.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    result = audit_documents(
        protocol=_read(root / "study/semantic-topology-protocol.v1.json"),
        plan=_read(root / "study/semantic-topology-pilot-plan.v1.json"),
        deep_inventory=_read(
            root / "results/semantic-topology-deep-search-v1/inventory.v1.json"
        ),
        record_inventory=_read(
            root / "results/semantic-topology-records-v1/inventory.v1.json"
        ),
        enriched_inventory=_read(
            root / "results/semantic-change-records.enriched.inventory.v1.json"
        ),
        gates=_read(root / "results/semantic-topology-utility-gates.v1.json"),
        finalization=_read(root / "results/semantic-topology-finalization.v1.json"),
    )
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passes"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
