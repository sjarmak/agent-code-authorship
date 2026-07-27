"""Repository-bootstrap multistate estimates from frozen lineage counts."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np


HORIZONS = (30, 90, 180, 365)
STATES = ("unchanged", "modified", "deleted", "unobservable", "right_censored")


def analysis_counts(
    counts: dict[str, int], *, include_structural: bool = False
) -> dict[str, int]:
    structural = counts.get("modified_candidate", 0)
    return {
        "unchanged": counts.get("unchanged", 0),
        "modified": structural if include_structural else 0,
        "deleted": counts.get("deleted", 0),
        "unobservable": (
            counts.get("unobservable", 0)
            + (0 if include_structural else structural)
        ),
        "right_censored": counts.get("right_censored", 0),
    }


def exact_only_counts(counts: dict[str, int]) -> dict[str, int]:
    return analysis_counts(counts, include_structural=False)


def coverage(rows: list[dict[str, int]]) -> float | None:
    pooled = sum((Counter(row) for row in rows), Counter())
    observed = sum(pooled[state] for state in STATES if state != "right_censored")
    if not observed:
        return None
    return (observed - pooled["unobservable"]) / observed


def _estimate(rows: list[dict[str, int]]) -> dict[str, Any]:
    repo_unconditional: dict[str, list[float]] = defaultdict(list)
    repo_conditional: dict[str, list[float]] = defaultdict(list)
    pooled = sum((Counter(row) for row in rows), Counter())
    for row in rows:
        total = sum(row.values())
        observable = sum(row[state] for state in ("unchanged", "modified", "deleted"))
        for state in STATES:
            repo_unconditional[state].append(row[state] / total if total else np.nan)
        for state in ("unchanged", "modified", "deleted"):
            repo_conditional[state].append(
                row[state] / observable if observable else np.nan
            )
    pooled_total = sum(pooled.values())
    pooled_observable = sum(
        pooled[state] for state in ("unchanged", "modified", "deleted")
    )
    return {
        "repository_weighted": {
            "unconditional_state_occupancy": {
                state: float(np.nanmean(repo_unconditional[state]))
                for state in STATES
            },
            "conditional_on_observable_lineage": {
                state: float(np.nanmean(repo_conditional[state]))
                for state in ("unchanged", "modified", "deleted")
            },
        },
        "line_weighted": {
            "unconditional_state_occupancy": {
                state: pooled[state] / pooled_total for state in STATES
            },
            "conditional_on_observable_lineage": {
                state: pooled[state] / pooled_observable
                for state in ("unchanged", "modified", "deleted")
            },
        },
    }


def _bootstrap(
    rows: list[dict[str, int]], *, replicates: int, seed: int
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    samples: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for _ in range(replicates):
        picked = rng.integers(0, len(rows), size=len(rows))
        estimate = _estimate([rows[index] for index in picked])
        for weighting in ("repository_weighted", "line_weighted"):
            for estimand, values in estimate[weighting].items():
                for state, value in values.items():
                    samples[(weighting, estimand, state)].append(value)
    result: dict[str, Any] = {}
    for (weighting, estimand, state), values in samples.items():
        result.setdefault(weighting, {}).setdefault(estimand, {})[state] = [
            float(np.nanpercentile(values, 2.5)),
            float(np.nanpercentile(values, 97.5)),
        ]
    return result


def estimate_stratum(
    repositories: list[dict[str, Any]],
    *,
    replicates: int,
    seed: int,
    minimum_repositories: int = 5,
    minimum_coverage: float = 0.8,
    include_structural: bool = False,
) -> dict[str, Any]:
    horizons: dict[str, Any] = {}
    for horizon in HORIZONS:
        rows = [
            analysis_counts(
                repository["counts"].get(str(horizon), {}),
                include_structural=include_structural,
            )
            for repository in repositories
        ]
        lineage_coverage = coverage(rows)
        gate_reasons = []
        if len(repositories) < minimum_repositories:
            gate_reasons.append("fewer_than_5_repositories")
        if lineage_coverage is None or lineage_coverage < minimum_coverage:
            gate_reasons.append("lineage_coverage_below_0.80")
        point = _estimate(rows)
        horizons[str(horizon)] = {
            "status": "identified" if not gate_reasons else "not_identified",
            "gate_reasons": gate_reasons,
            "lineage_coverage": lineage_coverage,
            "point_estimates": point,
            "bootstrap_95_ci": (
                _bootstrap(rows, replicates=replicates, seed=seed + horizon)
                if not gate_reasons
                else None
            ),
        }
    return {"repository_count": len(repositories), "horizons": horizons}


def build_estimates(
    inventory: dict[str, Any],
    frame: dict[str, Any],
    *,
    replicates: int,
    seed: int,
    include_structural: bool = False,
) -> dict[str, Any]:
    candidates = {
        candidate["repository_id"]: candidate for candidate in frame["candidates"]
    }
    strata: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for repository in inventory["repositories"]:
        candidate = candidates[repository["repository_id"]]
        strata[
            (
                candidate["language"],
                candidate["agent_family"],
                candidate["provenance_tier"],
            )
        ].append(repository)
    results = []
    for index, (key, repositories) in enumerate(sorted(strata.items())):
        language, agent_family, tier = key
        results.append(
            {
                "language": language,
                "agent_family": agent_family,
                "provenance_tier": tier,
                **estimate_stratum(
                    repositories,
                    replicates=replicates,
                    seed=seed + index * 10_000,
                    include_structural=include_structural,
                ),
            }
        )
    contextual = frame.get("label") == "non_agent_attributed"
    return {
        "artifact": (
            "contextual-survival-estimates" if contextual else "survival-estimates"
        ),
        "version": 1,
        "analysis_status": (
            "validated_structural_lineage"
            if include_structural
            else "exact_only_structural_lineage_excluded"
        ),
        "structural_candidate_handling": (
            "mapped_to_modified"
            if include_structural
            else "mapped_to_unobservable"
        ),
        "bootstrap": {
            "unit": "repository",
            "replicates": replicates,
            "seed": seed,
            "interval": "percentile_95",
        },
        "tier_results": (
            {
                "contextual": {
                    "status": "secondary_context_only",
                    "label": "non_agent_attributed",
                }
            }
            if contextual
            else {
                "1": {
                    "status": "not_identified",
                    "reason": "zero_eligible_role_separated_repositories",
                },
                "2": {"status": "partially_identified_by_stratum_and_horizon"},
            }
        ),
        "strata": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(
            "/mnt/agent-code-authorship/survival-study/lineage-v1/"
            "lineage-inventory.v1.json"
        ),
    )
    parser.add_argument(
        "--frame", type=Path, default=Path("study/survival-candidates.v1.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("results/survival-estimates.v1.json")
    )
    parser.add_argument("--replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--validation", type=Path)
    arguments = parser.parse_args()
    inventory = json.loads(arguments.inventory.read_text())
    frame = json.loads(arguments.frame.read_text())
    include_structural = False
    validation_sha256 = None
    if arguments.validation is not None:
        validation = json.loads(arguments.validation.read_text())
        include_structural = bool(
            validation.get("status") == "complete"
            and validation.get("structural_match_gate_passed")
        )
        validation_sha256 = hashlib.sha256(
            arguments.validation.read_bytes()
        ).hexdigest()
    document = build_estimates(
        inventory,
        frame,
        replicates=arguments.replicates,
        seed=arguments.seed,
        include_structural=include_structural,
    )
    document["lineage_validation_sha256"] = validation_sha256
    document["lineage_inventory_sha256"] = hashlib.sha256(
        arguments.inventory.read_bytes()
    ).hexdigest()
    document["candidate_frame_sha256"] = hashlib.sha256(
        arguments.frame.read_bytes()
    ).hexdigest()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    print(arguments.output)


if __name__ == "__main__":
    main()
