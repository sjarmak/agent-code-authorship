"""Build the gate-driven machine result and Tier-separated study report."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f}%"


def build_report(
    estimates: dict[str, Any],
    comparison: dict[str, Any],
    *,
    validation_pending: bool,
    structural_gate_passed: bool = False,
) -> str:
    status = (
        "**pending blinded structural-lineage review**"
        if validation_pending
        else "**final**"
    )
    handling = (
        "Structural candidates are mapped to `unobservable` until the frozen "
        "200-case review reaches at least 90% precision."
        if validation_pending
        else (
            "The blinded structural-lineage precision gate passed; structural "
            "candidates are mapped to `modified`."
            if structural_gate_passed
            else "The blinded structural-lineage precision gate failed; the "
            "final headline remains exact-only and structural candidates are "
            "mapped to `unobservable`."
        )
    )
    lines = [
        "# Agent-authored code survival study",
        "",
        f"Status: {status}.",
        "",
        handling,
        "",
        "## Scope and provenance",
        "",
        "- Frozen cutoff: 2026-07-24T23:59:59Z.",
        "- Languages: Python and Go.",
        "- Time zero: first default-branch commit containing the change.",
        "- Sourcegraph role: discovery and snapshot audit. The connected "
        "instance indexed 0 frozen candidates, so checksummed pinned Git "
        "histories are authoritative for lineage.",
        "- Physical-line deduplication excluded 228 duplicate attributions "
        "from Mochi PR overlaps; 656,070 unique eligible lines remain.",
        "",
        "## Tier 1 — explicit generation provenance",
        "",
        "**not_identified**: zero repositories remained eligible after the "
        "preregistered role-overlap exclusion. Tier 1 is not pooled with Tier 2.",
        "",
        "## Tier 2 — AIDev agent-attributed pull requests",
        "",
        "Primary estimates weight repositories equally. Secondary estimates "
        "pool lines. Cells failing either five-repository or 80% lineage "
        "coverage gates are reported as `not_identified`.",
        "",
        "| Language | Agent | Repositories | 30d | 90d | 180d | 365d |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    order = ("30", "90", "180", "365")
    for stratum in estimates["strata"]:
        cells = []
        for horizon in order:
            result = stratum["horizons"][horizon]
            if result["status"] != "identified":
                cells.append("not_identified")
            else:
                observable = result["point_estimates"]["repository_weighted"][
                    "conditional_on_observable_lineage"
                ]
                survival = observable["unchanged"] + observable["modified"]
                cells.append(_fmt(survival))
        lines.append(
            "| "
            + " | ".join(
                [
                    stratum["language"],
                    stratum["agent_family"],
                    str(stratum["repository_count"]),
                    *cells,
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Values are repository-weighted headline survival among "
            "observable unchanged/modified/deleted lines, with survival defined "
            "as `unchanged + modified`.",
            "",
            "## Matched contemporaneous context",
            "",
            "The secondary comparison is labeled `non_agent_attributed`, uses "
            "the same repositories, language/file type, ±90-day code-age "
            "caliper, and fourfold change-size caliper, without replacement. "
            "It is descriptive and has no causal interpretation.",
            "",
            "| Language | Paired repositories | Horizon | Status | "
            "Repository-weighted agent-minus-context difference (95% CI) |",
            "|---|---:|---:|---|---:|",
        ]
    )
    for language in comparison["languages"]:
        for horizon in order:
            result = language["horizons"][horizon]
            if result["status"] == "identified":
                estimate = result["paired_estimate"][
                    "repository_weighted_primary"
                ]
                value = (
                    f"{100 * estimate['point']:.1f} pp "
                    f"({100 * estimate['bootstrap_95_ci'][0]:.1f}, "
                    f"{100 * estimate['bootstrap_95_ci'][1]:.1f})"
                )
            else:
                value = "—"
            lines.append(
                f"| {language['language']} | "
                f"{language['paired_repository_count']} | {horizon}d | "
                f"{result['status']} | {value} |"
            )
    lines.extend(
        [
            "",
            "## Report figures",
            "",
            "![Tier 2 repository-weighted survival trajectories]"
            "(figures/tier2-survival-small-multiples.svg)",
            "",
            "*Figure 1. Repository-weighted `unchanged + modified` survival "
            "among observable lineage. Shared-scale small multiples show only "
            "identified cells; the tables retain every gate-driven "
            "`not_identified` result.*",
            "",
            "![Matched contextual survival differences]"
            "(figures/contextual-effects.svg)",
            "",
            "*Figure 2. Repository-weighted agent-minus-"
            "`non_agent_attributed` survival differences with 95% paired-"
            "repository bootstrap intervals. This comparison is descriptive, "
            "not causal.*",
        ]
    )
    lines.extend(
        [
            "",
            "## Identification and limitations",
            "",
            "- Tier 1 is `not_identified` because no role-separated eligible "
            "repository remained.",
            "- Tier 2 cells below five repositories or 80% lineage coverage "
            "are `not_identified`; descriptive point values are retained only "
            "in the machine artifact.",
            (
                "- Validated structural matches enter headline results as "
                "`modified`."
                if structural_gate_passed and not validation_pending
                else "- Structural matches do not enter these headline results."
            ),
            "- Mochi dominates line-weighted totals, so repository weighting is "
            "the primary estimand.",
            "- The contextual comparison is secondary and does not identify "
            "causal authorship effects.",
            "",
            "## Reproducibility",
            "",
            "Raw AIDev inputs, Git bundles, transition shards, contextual "
            "cohorts, and private blind mappings are checksummed on NAS. "
            "Repository artifacts contain the frozen protocol, candidate "
            "manifest, public blind-review file, estimates, and this report.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--estimates",
        type=Path,
        default=Path("results/survival-estimates.v1.json"),
    )
    parser.add_argument(
        "--comparison",
        type=Path,
        default=Path("results/contextual-comparison.v1.json"),
    )
    parser.add_argument(
        "--adjudication",
        type=Path,
        default=Path("study/lineage-adjudication.v1.json"),
    )
    parser.add_argument(
        "--validation",
        type=Path,
        default=Path("study/lineage-validation.v1.json"),
    )
    parser.add_argument(
        "--machine-output",
        type=Path,
        default=Path("results/survival-study-result.v1.json"),
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path("results/SURVIVAL_REPORT.v1.md"),
    )
    arguments = parser.parse_args()
    estimates = json.loads(arguments.estimates.read_text())
    comparison = json.loads(arguments.comparison.read_text())
    adjudication = json.loads(arguments.adjudication.read_text())
    validation = (
        json.loads(arguments.validation.read_text())
        if arguments.validation.exists()
        else {
            "status": "pending_blinded_review",
            "structural_match_gate_passed": False,
        }
    )
    unresolved = sum(
        case["is_same_lineage"] is None for case in adjudication["cases"]
    )
    artifacts = {
        "protocol": Path("study/survival-protocol.v1.json"),
        "candidate_frame": Path("study/survival-candidates.v1.json"),
        "input_inventory": Path("study/survival-inputs.v1.json"),
        "execution_inventory": Path("study/survival-execution-inventory.v1.json"),
        "lineage_inventory": Path(
            "/mnt/agent-code-authorship/survival-study/lineage-v1/"
            "lineage-inventory.v1.json"
        ),
        "context_lineage_inventory": Path(
            "/mnt/agent-code-authorship/survival-study/contextual-v1/lineage-v1/"
            "lineage-inventory.v1.json"
        ),
        "matched_agent_lineage_inventory": Path(
            "/mnt/agent-code-authorship/survival-study/contextual-v1/"
            "matched-agent-lineage-v1/lineage-inventory.v1.json"
        ),
        "adjudication_public": arguments.adjudication,
        "lineage_validation": arguments.validation,
        "survival_estimates": arguments.estimates,
        "contextual_comparison": arguments.comparison,
    }
    machine = {
        "artifact": "survival-study-result",
        "version": 1,
        "status": (
            "pending_blinded_review"
            if validation["status"] != "complete"
            else "final"
        ),
        "unresolved_blinded_cases": unresolved,
        "structural_matches_in_headline": bool(
            validation.get("status") == "complete"
            and validation.get("structural_match_gate_passed")
        ),
        "lineage_validation": validation,
        "tier_1": estimates["tier_results"]["1"],
        "tier_2": estimates["tier_results"]["2"],
        "strata": estimates["strata"],
        "contextual_comparison": comparison,
        "artifacts": {
            name: {"path": str(path), "sha256": _sha(path)}
            for name, path in artifacts.items()
            if path.exists()
        },
    }
    arguments.machine_output.write_text(
        json.dumps(machine, indent=2, sort_keys=True) + "\n"
    )
    arguments.report_output.write_text(
        build_report(
            estimates,
            comparison,
            validation_pending=validation["status"] != "complete",
            structural_gate_passed=bool(
                validation.get("structural_match_gate_passed")
            ),
        )
    )
    print(arguments.machine_output)
    print(arguments.report_output)


if __name__ == "__main__":
    main()
