"""Freeze the amended study when reference-label acquisition cannot proceed."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from authorship.freeze_results import _atomic_json


class NonIdentificationFreezeError(ValueError):
    """Raised when the no-outreach fallback is not safe to freeze."""


QUANTITATIVE_GATES = (
    "minimum_auc",
    "plausible_mixture",
    "maximum_estimator_disagreement",
    "maximum_leave_one_group_out_shift",
    "maximum_bootstrap_interval_width",
    "maximum_synthetic_mean_absolute_error",
    "minimum_synthetic_interval_coverage",
)


def _reference_eligibility(v1_result: Mapping[str, Any]) -> dict[str, Any]:
    output = {}
    repositories = v1_result["reference_repositories"]
    for language in ("Python", "Go"):
        by_label = {}
        for label in ("agent", "human"):
            development = 0
            validation = 0
            if label == "human":
                # Amendment v2 explicitly excludes repository policy without
                # exact commit scope. The frozen no-outreach ledger contributes
                # no replacement human attestations.
                by_label[label] = {
                    "development": 0,
                    "validation": 0,
                    "eligible": False,
                }
                continue
            for summary in repositories.values():
                if (
                    summary["label"] != label
                    or language not in summary["substantial_languages"]
                ):
                    continue
                if summary["role"] == "dedicated_validation":
                    validation += 1
                else:
                    development += 1
            by_label[label] = {
                "development": development,
                "validation": validation,
                "eligible": development >= 5 and validation >= 1,
            }
        output[language] = by_label
    return output


def build_nonidentification_result(
    *,
    v1_result: Mapping[str, Any],
    candidate_frame: Mapping[str, Any],
    attestation_ledger: Mapping[str, Any],
    execution_decision: Mapping[str, Any],
    v2_corpus_report: Mapping[str, Any],
    artifact_hashes: Mapping[str, str],
) -> dict[str, Any]:
    """Build a deterministic result without treating missing labels as evidence."""
    if attestation_ledger.get("status") != "frozen_without_solicitation":
        raise NonIdentificationFreezeError("attestation ledger is not frozen")
    if any(
        record.get("disposition") == "eligible"
        for record in attestation_ledger.get("records", [])
    ):
        raise NonIdentificationFreezeError(
            "no-outreach fallback cannot discard eligible attestations"
        )
    consequences = execution_decision.get("consequences", {})
    if consequences.get("infer_labels_from_non_solicitation") is not False:
        raise NonIdentificationFreezeError(
            "execution decision must not infer labels from non-solicitation"
        )
    if execution_decision.get("decision") != "no_external_maintainer_outreach":
        raise NonIdentificationFreezeError("execution decision is not no-outreach")
    if v2_corpus_report.get("status") != "rebuilt_not_identified":
        raise NonIdentificationFreezeError("v2 corpus rebuild is not frozen")
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in artifact_hashes.values()
    ):
        raise NonIdentificationFreezeError("artifact hashes must be SHA-256 values")

    eligibility = _reference_eligibility(v1_result)
    upstream_passes = all(
        cell["eligible"]
        for language in eligibility.values()
        for cell in language.values()
    )
    if upstream_passes:
        raise NonIdentificationFreezeError(
            "reference gate passes; quantitative pipeline must run"
        )

    reason = (
        "The contemporary expansion acquired no eligible human attestations "
        "because external maintainer solicitation was declined. The locked "
        "five-development-plus-one-validation reference gate therefore fails."
    )
    locked_gates = {
        gate: {
            "status": "not_run",
            "passed": False,
            "reason": "upstream reference-diversity precondition failed",
        }
        for gate in QUANTITATIVE_GATES
    }
    locked_gates["systematic_bias_trend"] = {
        "status": "not_evaluable",
        "passed": False,
        "reason": (
            "protocol v1 forbids a systematic bias trend but preregisters "
            "neither a statistic nor a decision threshold; no post-outcome "
            "criterion was invented"
        ),
    }
    label_counts = Counter(
        record.get("label")
        for record in attestation_ledger.get("records", [])
        if record.get("disposition") == "eligible"
    )
    return {
        "result_version": 2,
        "status": "not_identified",
        "headline_estimate": None,
        "reason": reason,
        "frozen_at": execution_decision["recorded_at"],
        "estimand": {
            "target_cohort": "unchanged frozen v1 cohort",
            "languages": ["Python", "Go"],
            "generalize_to_all_github": False,
        },
        "artifact_sha256": dict(sorted(artifact_hashes.items())),
        "expansion": {
            "candidate_count": len(candidate_frame["candidates"]),
            "ledger_status": attestation_ledger["status"],
            "eligible_attestations": sum(label_counts.values()),
            "eligible_attestations_by_label": dict(sorted(label_counts.items())),
            "human_labels_inferred": 0,
            "external_requests_sent": 0,
            "agent_statement_treatment": execution_decision[
                "agent_provenance_statement"
            ]["admission"],
        },
        "reference_eligibility": eligibility,
        "reference_diversity_gate_passed": False,
        "locked_quantitative_gates": locked_gates,
        "temporal_diagnostics": {
            "historical_code_role": "diagnostic_and_sensitivity_only",
            "primary_model_use": False,
            "status": "not_run",
            "reason": "no admissible primary contemporary reference model",
        },
        "corpus": {
            "v2_rebuild_performed": True,
            "status": v2_corpus_report["status"],
            "introduced_on_or_after": v2_corpus_report[
                "introduced_on_or_after"
            ],
            "reference_shard_root": v2_corpus_report[
                "output_reference_shards"
            ],
            "exact_collision_hashes_removed": v2_corpus_report[
                "role_separation"
            ]["collision_hashes"],
            "reference_totals": v2_corpus_report["totals"],
            "v1_reference_repository_count": v1_result[
                "reference_repository_count"
            ],
            "target_repository_count": v1_result["target_repository_count"],
            "target_totals": v1_result["target_totals"],
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    inputs = {
        "protocol_v1": root / "study" / "protocol.v1.json",
        "protocol_amendment_v2": root / "study" / "protocol-amendment.v2.json",
        "features_v1": root / "study" / "features.v1.json",
        "reference_manifest_v1": root / "study" / "repositories.v1.json",
        "target_manifest_v1": root / "study" / "targets.v1.json",
        "candidate_frame_v2": root / "study" / "reference-candidates.v2.json",
        "attestation_ledger_v2": root / "study" / "attestations.v2.json",
        "execution_decision_v2": root / "study" / "execution-decision.v2.json",
        "human_audit_batch1_v2": (
            root / "study" / "human-candidate-audit-batch1.v2.json"
        ),
        "human_audit_batch2_v2": (
            root / "study" / "human-candidate-audit-batch2.v2.json"
        ),
        "agent_go_audit_v2": root / "study" / "agent-go-candidate-audit.v2.json",
        "reference_manifest_v2": root / "study" / "repositories.v2.json",
        "role_separation_v2": root / "study" / "role-separation.v2.json",
        "replication_v1": root / "results" / "replication.v1.json",
        "freeze_implementation": Path(__file__).resolve(),
        "role_separation_implementation": (
            root / "authorship" / "role_separation.py"
        ),
        "replication_implementation": root / "authorship" / "replication.py",
        "rebuild_implementation": root / "authorship" / "rebuild_v2_corpus.py",
    }
    v1_result = json.loads((root / "results" / "replication.v1.json").read_text())
    candidate_frame = json.loads(
        (root / "study" / "reference-candidates.v2.json").read_text()
    )
    attestation_ledger = json.loads(
        (root / "study" / "attestations.v2.json").read_text()
    )
    execution_decision = json.loads(
        (root / "study" / "execution-decision.v2.json").read_text()
    )
    v2_corpus_report = json.loads(
        (root / "study" / "role-separation.v2.json").read_text()
    )
    result = build_nonidentification_result(
        v1_result=v1_result,
        candidate_frame=candidate_frame,
        attestation_ledger=attestation_ledger,
        execution_decision=execution_decision,
        v2_corpus_report=v2_corpus_report,
        artifact_hashes={name: _sha256(path) for name, path in inputs.items()},
    )
    destination = root / "results" / "replication.v2.json"
    _atomic_json(destination, result)
    print(destination)


if __name__ == "__main__":
    main()
