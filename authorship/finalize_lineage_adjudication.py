"""Score the completed blinded lineage review and freeze its gate result."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def score_review(
    public: dict[str, Any],
    private: dict[str, Any],
    *,
    minimum_precision: float = 0.9,
) -> dict[str, Any]:
    decisions = {
        case["case_id"]: case["is_same_lineage"] for case in public["cases"]
    }
    unresolved = [case_id for case_id, decision in decisions.items() if decision is None]
    if unresolved:
        return {
            "status": "pending_blinded_review",
            "unresolved_cases": len(unresolved),
            "structural_match_gate_passed": False,
            "headline_handling": "structural_candidates_remain_unobservable",
        }
    private_by_id = {
        case["case_key"]: case for case in private["cases"]
    }
    if set(decisions) != set(private_by_id):
        raise ValueError("public and private adjudication case IDs differ")
    accepted_ids = [
        case_id
        for case_id, case in private_by_id.items()
        if case["selection_class"] == "accepted"
    ]
    rejected_ids = [
        case_id
        for case_id, case in private_by_id.items()
        if case["selection_class"] == "rejected"
    ]
    if len(accepted_ids) != 100 or len(rejected_ids) != 100:
        raise ValueError("adjudication mapping is not balanced 100/100")
    accepted_true = sum(decisions[case_id] is True for case_id in accepted_ids)
    rejected_true = sum(decisions[case_id] is True for case_id in rejected_ids)
    precision = accepted_true / len(accepted_ids)
    gate_passed = precision >= minimum_precision
    return {
        "status": "complete",
        "unresolved_cases": 0,
        "accepted_sample_size": len(accepted_ids),
        "accepted_same_lineage": accepted_true,
        "structural_precision": precision,
        "rejected_sample_size": len(rejected_ids),
        "rejected_same_lineage": rejected_true,
        "rejected_same_lineage_rate": rejected_true / len(rejected_ids),
        "minimum_structural_precision": minimum_precision,
        "structural_match_gate_passed": gate_passed,
        "headline_handling": (
            "structural_candidates_map_to_modified"
            if gate_passed
            else "structural_candidates_map_to_unobservable"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--public",
        type=Path,
        default=Path("study/lineage-adjudication.v1.json"),
    )
    parser.add_argument(
        "--private",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/"
            "lineage-adjudication-private.v1.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("study/lineage-validation.v1.json"),
    )
    arguments = parser.parse_args()
    public = json.loads(arguments.public.read_text())
    observed_private_sha256 = hashlib.sha256(
        arguments.private.read_bytes()
    ).hexdigest()
    if observed_private_sha256 != public["private_mapping_sha256"]:
        raise SystemExit("private adjudication mapping checksum mismatch")
    private = json.loads(arguments.private.read_text())
    result = score_review(public, private)
    document = {
        "artifact": "lineage-validation",
        "version": 1,
        "public_adjudication_sha256": hashlib.sha256(
            arguments.public.read_bytes()
        ).hexdigest(),
        "private_mapping_sha256": observed_private_sha256,
        **result,
    }
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    print(arguments.output)


if __name__ == "__main__":
    main()
