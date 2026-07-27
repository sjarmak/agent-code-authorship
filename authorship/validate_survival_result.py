"""Validate the integrated survival-study machine result and report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def validate(document: dict[str, Any], report: str) -> list[str]:
    errors = []
    unresolved = document.get("unresolved_blinded_cases")
    validation = document.get("lineage_validation", {})
    validation_complete = validation.get("status") == "complete"
    gate_passed = bool(validation.get("structural_match_gate_passed"))
    structural_in_headline = bool(document.get("structural_matches_in_headline"))
    if unresolved:
        if document.get("status") != "pending_blinded_review":
            errors.append("unresolved blind cases require pending status")
        if structural_in_headline:
            errors.append("pending structural matches cannot enter headline")
    elif document.get("status") != "final":
        errors.append("resolved blind review requires final status")
    if document.get("status") == "final" and not validation_complete:
        errors.append("final status requires complete lineage validation")
    if structural_in_headline != (validation_complete and gate_passed):
        errors.append("structural headline flag disagrees with validation gate")
    if structural_in_headline:
        if "Validated structural matches enter headline results as `modified`." not in report:
            errors.append("report omits validated structural headline handling")
        if "Structural matches do not enter these headline results." in report:
            errors.append("report contradicts structural headline handling")
    if document.get("tier_1", {}).get("status") != "not_identified":
        errors.append("Tier 1 must be separately not_identified")
    if "Tier 1" not in report or "Tier 2" not in report:
        errors.append("report must separate Tier 1 and Tier 2")
    if "non_agent_attributed" not in report:
        errors.append("report lacks contextual label")
    if "human" in report.lower():
        errors.append("report uses forbidden contextual label")
    for name, artifact in document.get("artifacts", {}).items():
        path = Path(artifact["path"])
        if not path.exists():
            errors.append(f"{name}: artifact missing")
            continue
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != artifact["sha256"]:
            errors.append(f"{name}: checksum mismatch")
    for stratum in document.get("strata", []):
        for horizon, result in stratum["horizons"].items():
            if result["status"] not in {"identified", "not_identified"}:
                errors.append(
                    f"{stratum['language']}/{stratum['agent_family']}/"
                    f"{horizon}: invalid status"
                )
            if result["status"] == "not_identified" and not result["gate_reasons"]:
                errors.append(
                    f"{stratum['language']}/{stratum['agent_family']}/"
                    f"{horizon}: missing gate reason"
                )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--result",
        type=Path,
        default=Path("results/survival-study-result.v1.json"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("results/SURVIVAL_REPORT.v1.md"),
    )
    arguments = parser.parse_args()
    errors = validate(
        json.loads(arguments.result.read_text()), arguments.report.read_text()
    )
    if errors:
        raise SystemExit("\n".join(errors))
    print("survival study result valid")


if __name__ == "__main__":
    main()
