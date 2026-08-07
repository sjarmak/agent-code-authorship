"""Execute the frozen tier-specific Sourcegraph prevalence analysis."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from authorship.quantify import IdentificationError
from authorship.replication import (
    leave_one_target_group_out,
    run_quantification,
)
from authorship.sourcegraph_authorship_materialization import (
    authorship_materialization_sha256,
)
from authorship.sourcegraph_cohort_validation import cohort_freeze_sha256
from authorship.sourcegraph_prevalence_data import prepare_prevalence_arrays
from authorship.sourcegraph_prevalence_resampling import (
    repository_cluster_bootstrap,
    resample_repository_clusters,
)
from authorship.sourcegraph_prevalence_spec import (
    prevalence_analysis_spec_sha256,
    validate_prevalence_analysis_spec_structure,
)
from authorship.sourcegraph_prevalence_synthetic import (
    _select_synthetic_groups,
    repository_heldout_split,
    synthetic_diagnostic,
)
from authorship.sourcegraph_target_materialization import (
    target_materialization_sha256,
)
from authorship.sourcegraph_target_plan import target_unit_plan_sha256

EXECUTION_VERSION = 1
__all__ = [
    "_select_synthetic_groups",
    "repository_heldout_split",
    "resample_repository_clusters",
]


class PrevalenceExecutionError(RuntimeError):
    """Raised when frozen prevalence analysis inputs are invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _artifact_sha256(document: Mapping[str, Any], field: str) -> str:
    return _sha256({key: value for key, value in document.items() if key != field})


def prevalence_execution_sha256(document: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in document.items()
        if key != "prevalence_execution_sha256"
    }
    return _sha256(payload)


def _diagnostic_gate_values(
    bootstrap: Mapping[str, Any] | None,
    stability: Mapping[str, Any] | None,
    synthetic: Mapping[str, Any] | None,
    specification: Mapping[str, Any],
) -> dict[str, bool]:
    gates = specification["identification_gates"]
    interval = bootstrap.get("interval") if bootstrap else None
    interval_width = interval[1] - interval[0] if interval else float("inf")
    return {
        "maximum_leave_one_repository_out_shift": bool(
            stability
            and stability["maximum_shift"]
            <= gates["maximum_leave_one_repository_out_shift"]
        ),
        "maximum_bootstrap_interval_width": (
            interval_width <= gates["maximum_bootstrap_interval_width"]
        ),
        "maximum_synthetic_mean_absolute_error": bool(
            synthetic
            and synthetic["mean_absolute_error"]
            <= gates["maximum_synthetic_mean_absolute_error"]
        ),
        "minimum_synthetic_interval_coverage": bool(
            synthetic
            and synthetic["interval_coverage"]
            >= gates["minimum_synthetic_interval_coverage"]
        ),
    }


def _attempt(
    operation,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return operation(), None
    except (IdentificationError, ValueError, np.linalg.LinAlgError) as error:
        return None, str(error)


def _model_arguments(arrays: Mapping[str, Any]) -> tuple[np.ndarray, ...]:
    reference, target = arrays["reference"], arrays["target"]
    return (
        reference["x"],
        reference["labels"],
        reference["weights"],
        reference["groups"],
        target["x"],
        target["weights"],
        target["groups"],
    )


def _cell_diagnostics(
    arrays: Mapping[str, Any],
    specification: Mapping[str, Any],
    arguments: tuple[np.ndarray, ...],
    point: Mapping[str, Any],
) -> dict[str, Any]:
    model = specification["model"]
    bootstrap, bootstrap_error = _attempt(
        lambda: repository_cluster_bootstrap(
            *arguments,
            replicates=specification["uncertainty"]["bootstrap_replicates"],
            seed=model["seed"],
            bins=model["score_mixture_bins"],
        )
    )
    stability, stability_error = _attempt(
        lambda: leave_one_target_group_out(
            *arguments,
            baseline_share=point["mixture_share"],
            seed=model["seed"],
            bins=model["score_mixture_bins"],
        )
    )
    synthetic, synthetic_error = _attempt(
        lambda: synthetic_diagnostic(arrays, specification)
    )
    return {
        "bootstrap": bootstrap,
        "bootstrap_error": bootstrap_error,
        "leave_one_target_repository_out": stability,
        "leave_one_target_repository_out_error": stability_error,
        "synthetic_mixture": synthetic,
        "synthetic_mixture_error": synthetic_error,
    }


def _estimate_cell(
    arrays: Mapping[str, Any], specification: Mapping[str, Any]
) -> dict[str, Any]:
    model = specification["model"]
    arguments = _model_arguments(arrays)
    point = run_quantification(
        *arguments, seed=model["seed"], bins=model["score_mixture_bins"]
    )
    diagnostics = _cell_diagnostics(arrays, specification, arguments, point)
    diagnostic_gates = _diagnostic_gate_values(
        diagnostics["bootstrap"],
        diagnostics["leave_one_target_repository_out"],
        diagnostics["synthetic_mixture"],
        specification,
    )
    return {
        "status": "numeric_diagnostic_complete",
        "diagnostic_estimate": {
            "mixture_share": point["mixture_share"],
            "threshold_adjusted_share": point["threshold_adjusted_share"],
        },
        "point_gates": point["gates"],
        "diagnostic_gates": diagnostic_gates,
        "diagnostics": {"point": point, **diagnostics},
    }


def apply_identification_gates(
    *,
    tier: str,
    counts: Mapping[str, int],
    point_gates: Mapping[str, bool],
    diagnostic_gates: Mapping[str, bool],
    era_diagnostics_identified: bool,
    era_adjustment_applied: bool,
    agent_family_identified: bool,
    target_complete: bool,
    minimum_adopters: int,
    minimum_controls: int,
) -> dict[str, Any]:
    minimum = minimum_adopters if tier == "H3" else minimum_controls
    gates = {
        **dict(point_gates),
        **dict(diagnostic_gates),
        "minimum_reference_repositories": counts["human_repositories"] >= minimum,
        "minimum_agent_family_repositories": agent_family_identified,
        "complete_fixed_target_frame": target_complete,
        "era_adjustment_diagnostics_identified": era_diagnostics_identified,
        "era_adjustment_applied_to_model_input": era_adjustment_applied,
    }
    identified = all(gates.values())
    return {
        "identified": identified,
        "status": "identified" if identified else "not_identified",
        "gates": gates,
        "minimum_required_reference_repositories": minimum,
    }


def _not_identified_cell(reason: str, counts: Mapping[str, int]) -> dict[str, Any]:
    return {
        "status": reason,
        "headline_estimate": None,
        "diagnostic_estimate": None,
        "counts": dict(counts),
        "identification": {"identified": False, "status": "not_identified"},
    }


def _run_cell(
    arrays: Mapping[str, Any],
    specification: Mapping[str, Any],
    *,
    era_diagnostics_identified: bool,
    era_adjustment_applied: bool,
    family_identified: bool,
    target_complete: bool,
) -> dict[str, Any]:
    counts = arrays["counts"]
    if not counts["human_units"]:
        return _not_identified_cell("not_identified_no_human_reference", counts)
    if not counts["agent_units"] or not counts["target_units"]:
        return _not_identified_cell("not_identified_empty_model_role", counts)
    estimated, error = _attempt(lambda: _estimate_cell(arrays, specification))
    if estimated is None:
        return {
            **_not_identified_cell("not_identified_model_unavailable", counts),
            "model_error": error,
        }
    gates = specification["identification_gates"]
    identification = apply_identification_gates(
        tier=arrays["tier"],
        counts=counts,
        point_gates=estimated["point_gates"],
        diagnostic_gates=estimated["diagnostic_gates"],
        era_diagnostics_identified=era_diagnostics_identified,
        era_adjustment_applied=era_adjustment_applied,
        agent_family_identified=family_identified,
        target_complete=target_complete,
        minimum_adopters=gates["minimum_adopters_per_language"],
        minimum_controls=gates["minimum_controls_per_language"],
    )
    return {
        **estimated,
        "status": identification["status"],
        "headline_estimate": None,
        "identified_tier_estimate": (
            estimated["diagnostic_estimate"] if identification["identified"] else None
        ),
        "counts": dict(counts),
        "role_separation": arrays["role_separation"],
        "identification": identification,
    }


def _input_pins(
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    features: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "authorship_materialization_sha256": authorship.get(
            "authorship_materialization_sha256"
        ),
        "target_materialization_sha256": target.get("target_materialization_sha256"),
        "era_study_execution_sha256": era.get("era_study_execution_sha256"),
        "cohort_freeze_sha256": cohort.get("cohort_freeze_sha256"),
        "feature_manifest_document_sha256": _sha256(features),
    }


def _validate_execution_inputs(
    specification: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    features: Mapping[str, Any],
) -> None:
    _validate_specification_input(specification, authorship, era, cohort, features)
    _validate_target_lineage(specification, target_plan, target)
    _validate_upstream_artifacts(authorship, target, era, cohort)
    if not _target_frame_complete(target, target_plan):
        raise PrevalenceExecutionError("fixed target frame is not fully processed")


def _validate_specification_input(
    specification: Mapping[str, Any],
    authorship: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    features: Mapping[str, Any],
) -> None:
    if specification.get("prevalence_analysis_spec_sha256") != (
        prevalence_analysis_spec_sha256(specification)
    ):
        raise PrevalenceExecutionError("analysis specification hash does not match")
    specification_errors = validate_prevalence_analysis_spec_structure(
        specification, features
    )
    if specification_errors:
        raise PrevalenceExecutionError("; ".join(specification_errors))
    pins = specification.get("input_pins", {})
    expected_pins = {
        "authorship_materialization_sha256": authorship.get(
            "authorship_materialization_sha256"
        ),
        "era_study_execution_sha256": era.get("era_study_execution_sha256"),
        "cohort_freeze_sha256": cohort.get("cohort_freeze_sha256"),
    }
    if pins != expected_pins:
        raise PrevalenceExecutionError(
            "analysis input lineage differs from specification"
        )


def _validate_target_lineage(
    specification: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    if target.get("target_unit_plan_sha256") != specification.get(
        "target_unit_plan_sha256"
    ):
        raise PrevalenceExecutionError(
            "target materialization uses another target plan"
        )
    if (
        target_plan.get("target_unit_plan_sha256")
        != target_unit_plan_sha256(target_plan)
        or target_plan.get("target_unit_plan_sha256")
        != specification.get("target_unit_plan_sha256")
        or _sha256(target_plan) != specification.get("target_plan_document_sha256")
    ):
        raise PrevalenceExecutionError("target plan differs from frozen lineage")


def _validate_upstream_artifacts(
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> None:
    self_hashes = (
        (
            authorship,
            "authorship_materialization_sha256",
            authorship_materialization_sha256(authorship),
        ),
        (
            target,
            "target_materialization_sha256",
            target_materialization_sha256(target),
        ),
        (
            era,
            "era_study_execution_sha256",
            _artifact_sha256(era, "era_study_execution_sha256"),
        ),
        (cohort, "cohort_freeze_sha256", cohort_freeze_sha256(cohort)),
    )
    if any(
        document.get(field) != expected for document, field, expected in self_hashes
    ):
        raise PrevalenceExecutionError("upstream artifact self-hash does not match")
    if authorship.get("status") != "complete":
        raise PrevalenceExecutionError("authorship materialization is incomplete")
    if target.get("status") != "complete":
        raise PrevalenceExecutionError("target materialization is incomplete")


def _planned_repository_ids(target_plan: Mapping[str, Any]) -> set[str]:
    rows = [
        *target_plan.get("repositories", []),
        *target_plan.get("pending_repositories", []),
    ]
    return {
        row["repository_id"]
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("repository_id"), str)
    }


def _target_frame_complete(
    target: Mapping[str, Any], target_plan: Mapping[str, Any]
) -> bool:
    repository_counts = target.get("repository_unit_counts")
    units = target.get("units")
    if not isinstance(repository_counts, Mapping) or not isinstance(units, list):
        return False
    zero_repositories = sorted(
        repository for repository, count in repository_counts.items() if count == 0
    )
    observed_counts = Counter(unit.get("repository_id") for unit in units)
    planned_repositories = _planned_repository_ids(target_plan)
    observed = sum(count > 0 for count in repository_counts.values())
    return all(
        (
            target.get("status") == "complete",
            target.get("repository_count") == 150,
            target.get("processed_repository_count") == 150,
            len(repository_counts) == 150,
            set(repository_counts) == planned_repositories,
            all(
                isinstance(count, int) and count >= 0
                for count in repository_counts.values()
            ),
            dict(observed_counts)
            == {
                repository: count
                for repository, count in repository_counts.items()
                if count > 0
            },
            sum(repository_counts.values()) == target.get("unit_count") == len(units),
            observed == target.get("observed_repository_count"),
            zero_repositories == sorted(target.get("zero_unit_repositories", [])),
            sum(unit.get("line_count", 0) for unit in units)
            == target.get("line_count"),
            np.isclose(
                sum(unit.get("weighted_line_count", 0.0) for unit in units),
                target.get("weighted_line_count", float("nan")),
            ),
            all(unit.get("repository_id") in repository_counts for unit in units),
        )
    )


def partial_identification_envelopes(
    results: Mapping[str, Any], specification: Mapping[str, Any]
) -> dict[str, Any]:
    """Union identified primary tier intervals over contamination grids."""
    design = specification["partial_identification"]
    envelopes = {}
    for language in specification["languages"]:
        candidates, tier_inputs = [], []
        for tier, grid in design["tier_contamination_grids"].items():
            cell = results[tier][language]["line"]
            bootstrap = cell.get("diagnostics", {}).get("bootstrap")
            if not cell.get("identification", {}).get("identified") or not bootstrap:
                continue
            lower, upper = bootstrap["interval"]
            transformed = [[c + (1 - c) * lower, c + (1 - c) * upper] for c in grid]
            candidates.extend(transformed)
            tier_inputs.append(
                {
                    "tier": tier,
                    "bootstrap_interval": [lower, upper],
                    "contamination_grid": grid,
                }
            )
        envelopes[language] = _partial_envelope(candidates, tier_inputs, design)
    return envelopes


def _partial_envelope(
    candidates: Sequence[Sequence[float]],
    tier_inputs: Sequence[Mapping[str, Any]],
    design: Mapping[str, Any],
) -> dict[str, Any]:
    if not candidates:
        return {
            "identified": False,
            "status": "not_identified_no_eligible_primary_tier",
            "headline_estimate": None,
            "tier_inputs": [],
        }
    interval = [
        float(min(candidate[0] for candidate in candidates)),
        float(max(candidate[1] for candidate in candidates)),
    ]
    width = interval[1] - interval[0]
    maximum_width = design["maximum_headline_width"]
    identified = width <= maximum_width or bool(np.isclose(width, maximum_width))
    return {
        "identified": identified,
        "status": "identified" if identified else design["failed_width_result"],
        "interval": interval,
        "interval_width": width,
        "headline_estimate": {"interval": interval} if identified else None,
        "tier_inputs": list(tier_inputs),
    }


def _analysis_gates(
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    target: Mapping[str, Any],
    target_plan: Mapping[str, Any],
) -> dict[str, bool]:
    family_gates = cohort.get("agent_family_gates", [])
    return {
        "era_diagnostics_identified": era.get("headline_inference_allowed") is True,
        "era_adjustment_applied": False,
        "family_identified": bool(family_gates)
        and all(gate.get("status") == "identified" for gate in family_gates),
        "target_complete": _target_frame_complete(target, target_plan),
    }


def _run_analysis_cells(
    specification: Mapping[str, Any],
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    gates: Mapping[str, bool],
) -> dict[str, Any]:
    results = {}
    for tier in specification["evidence_tiers"]:
        results[tier] = {}
        for language in specification["languages"]:
            results[tier][language] = {}
            for weighting in ("line", "repository"):
                arrays = prepare_prevalence_arrays(
                    authorship,
                    target,
                    specification["features"],
                    tier=tier,
                    language=language,
                    weighting=weighting,
                )
                results[tier][language][weighting] = _run_cell(
                    arrays, specification, **gates
                )
    return results


def run_prevalence_analysis(
    specification: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    features: Mapping[str, Any],
) -> dict[str, Any]:
    """Run all frozen tier/language/estimand cells without pooling tiers."""
    _validate_execution_inputs(
        specification, target_plan, authorship, target, era, cohort, features
    )
    gates = _analysis_gates(era, cohort, target, target_plan)
    results = _run_analysis_cells(specification, authorship, target, gates)
    partial_identification = partial_identification_envelopes(results, specification)
    headline_allowed = all(
        partial_identification[language]["identified"]
        for language in specification["languages"]
    )
    document = {
        "prevalence_execution_version": EXECUTION_VERSION,
        "status": "complete" if headline_allowed else "complete_not_identified",
        "prevalence_analysis_spec_sha256": specification[
            "prevalence_analysis_spec_sha256"
        ],
        "input_pins": _input_pins(authorship, target, era, cohort, features),
        "headline_inference_allowed": headline_allowed,
        "era_adjusted_model_input": gates["era_adjustment_applied"],
        "partial_identification": partial_identification,
        "tier_pooling_performed": False,
        "unadjusted_estimates_role": "diagnostic_only",
        "historical_anchors_in_primary": False,
        "results": results,
    }
    return {
        **document,
        "prevalence_execution_sha256": prevalence_execution_sha256(document),
    }


def validate_prevalence_execution(
    execution: Mapping[str, Any],
    specification: Mapping[str, Any],
    target_plan: Mapping[str, Any],
    authorship: Mapping[str, Any],
    target: Mapping[str, Any],
    era: Mapping[str, Any],
    cohort: Mapping[str, Any],
    features: Mapping[str, Any],
) -> list[str]:
    errors = []
    if execution.get("prevalence_execution_sha256") != (
        prevalence_execution_sha256(execution)
    ):
        errors.append("prevalence execution SHA-256 does not match")
    try:
        expected = run_prevalence_analysis(
            specification,
            target_plan,
            authorship,
            target,
            era,
            cohort,
            features,
        )
    except PrevalenceExecutionError as error:
        return [*errors, str(error)]
    if _canonical_json(execution) != _canonical_json(expected):
        errors.append(
            "prevalence execution differs from independently recomputed result"
        )
    return errors
