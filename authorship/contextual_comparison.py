"""Paired repository-bootstrap comparison of matched lineage cohorts."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from authorship.survival_estimates import HORIZONS, analysis_counts, coverage


def _observable_survival(row: dict[str, int]) -> float:
    denominator = row["unchanged"] + row["modified"] + row["deleted"]
    return (
        (row["unchanged"] + row["modified"]) / denominator
        if denominator
        else float("nan")
    )


def paired_estimate(
    pairs: list[tuple[dict[str, int], dict[str, int]]],
    *,
    replicates: int,
    seed: int,
    include_structural: bool = False,
) -> dict[str, Any]:
    repository_differences = [
        _observable_survival(agent) - _observable_survival(context)
        for agent, context in pairs
    ]
    agent_pooled = {
        state: sum(agent[state] for agent, _context in pairs)
        for state in ("unchanged", "modified", "deleted")
    }
    context_pooled = {
        state: sum(context[state] for _agent, context in pairs)
        for state in ("unchanged", "modified", "deleted")
    }
    point_repository = float(np.nanmean(repository_differences))
    point_line = _observable_survival(agent_pooled) - _observable_survival(
        context_pooled
    )
    rng = np.random.default_rng(seed)
    repository_boots = []
    line_boots = []
    for _ in range(replicates):
        indices = rng.integers(0, len(pairs), size=len(pairs))
        sample = [pairs[index] for index in indices]
        repository_boots.append(
            float(
                np.nanmean(
                    [
                        _observable_survival(agent)
                        - _observable_survival(context)
                        for agent, context in sample
                    ]
                )
            )
        )
        agent_counts = {
            state: sum(agent[state] for agent, _context in sample)
            for state in ("unchanged", "modified", "deleted")
        }
        context_counts = {
            state: sum(context[state] for _agent, context in sample)
            for state in ("unchanged", "modified", "deleted")
        }
        line_boots.append(
            _observable_survival(agent_counts)
            - _observable_survival(context_counts)
        )
    return {
        "estimand": "agent_minus_non_agent_attributed_observable_survival",
        "repository_weighted_primary": {
            "point": point_repository,
            "bootstrap_95_ci": [
                float(np.nanpercentile(repository_boots, 2.5)),
                float(np.nanpercentile(repository_boots, 97.5)),
            ],
        },
        "line_weighted_secondary": {
            "point": point_line,
            "bootstrap_95_ci": [
                float(np.nanpercentile(line_boots, 2.5)),
                float(np.nanpercentile(line_boots, 97.5)),
            ],
        },
    }


def build_comparison(
    agent_inventory: dict[str, Any],
    context_inventory: dict[str, Any],
    frame: dict[str, Any],
    *,
    replicates: int,
    seed: int,
    include_structural: bool = False,
) -> dict[str, Any]:
    agent = {
        row["repository_id"]: row for row in agent_inventory["repositories"]
    }
    context = {
        row["repository_id"]: row for row in context_inventory["repositories"]
    }
    if set(agent) != set(context):
        raise ValueError("paired inventories must contain identical repositories")
    languages = {
        candidate["repository_id"]: candidate["language"]
        for candidate in frame["candidates"]
    }
    by_language: dict[str, list[str]] = defaultdict(list)
    for repository_id in sorted(agent):
        by_language[languages[repository_id]].append(repository_id)
    results = []
    for language_index, (language, repositories) in enumerate(
        sorted(by_language.items())
    ):
        horizons = {}
        for horizon in HORIZONS:
            agent_rows = [
                analysis_counts(
                    agent[repository]["counts"][str(horizon)],
                    include_structural=include_structural,
                )
                for repository in repositories
            ]
            context_rows = [
                analysis_counts(
                    context[repository]["counts"][str(horizon)],
                    include_structural=include_structural,
                )
                for repository in repositories
            ]
            agent_coverage = coverage(agent_rows)
            context_coverage = coverage(context_rows)
            reasons = []
            if len(repositories) < 5:
                reasons.append("fewer_than_5_paired_repositories")
            if agent_coverage is None or agent_coverage < 0.8:
                reasons.append("agent_lineage_coverage_below_0.80")
            if context_coverage is None or context_coverage < 0.8:
                reasons.append("context_lineage_coverage_below_0.80")
            horizons[str(horizon)] = {
                "status": "identified" if not reasons else "not_identified",
                "gate_reasons": reasons,
                "agent_lineage_coverage": agent_coverage,
                "context_lineage_coverage": context_coverage,
                "paired_estimate": (
                    paired_estimate(
                        list(zip(agent_rows, context_rows, strict=True)),
                        replicates=replicates,
                        seed=seed + language_index * 10_000 + horizon,
                    )
                    if not reasons
                    else None
                ),
            }
        results.append(
            {
                "language": language,
                "paired_repository_count": len(repositories),
                "horizons": horizons,
            }
        )
    return {
        "artifact": "contextual-comparison",
        "version": 1,
        "role": "secondary_context_only",
        "context_label": "non_agent_attributed",
        "causal_interpretation": False,
        "structural_candidate_handling": (
            "mapped_to_modified"
            if include_structural
            else "mapped_to_unobservable"
        ),
        "bootstrap": {
            "unit": "paired_repository",
            "replicates": replicates,
            "seed": seed,
            "interval": "percentile_95",
        },
        "languages": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-inventory", type=Path, required=True)
    parser.add_argument("--context-inventory", type=Path, required=True)
    parser.add_argument("--frame", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/contextual-comparison.v1.json"),
    )
    parser.add_argument("--replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260724)
    parser.add_argument("--validation", type=Path)
    arguments = parser.parse_args()
    agent = json.loads(arguments.agent_inventory.read_text())
    context = json.loads(arguments.context_inventory.read_text())
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
    document = build_comparison(
        agent,
        context,
        frame,
        replicates=arguments.replicates,
        seed=arguments.seed,
        include_structural=include_structural,
    )
    document["lineage_validation_sha256"] = validation_sha256
    document["agent_inventory_sha256"] = hashlib.sha256(
        arguments.agent_inventory.read_bytes()
    ).hexdigest()
    document["context_inventory_sha256"] = hashlib.sha256(
        arguments.context_inventory.read_bytes()
    ).hexdigest()
    document["frame_sha256"] = hashlib.sha256(
        arguments.frame.read_bytes()
    ).hexdigest()
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    print(arguments.output)


if __name__ == "__main__":
    main()
